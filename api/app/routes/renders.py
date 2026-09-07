"""Render queue endpoints, for the signed-in owner.

There used to be a second half here: token-authenticated endpoints for a rented
GPU box to claim jobs and upload results, built so that a machine from an
anonymous marketplace never held a Supabase credential. fal.ai renders the video
now, the box is gone, and so is that whole surface — no shared token, no
untrusted caller, no public write path to defend.
"""
from __future__ import annotations

import logging

from flask import Blueprint, g, jsonify, request

from .. import supabase
from ..auth import require_auth
from ..render import queue as render_queue, ready as render_ready, runner as render_runner
from ..render import remix, tiktok_pattern
from ..pipeline import formats

log = logging.getLogger(__name__)
bp = Blueprint("renders", __name__)


# ── Owner-facing ────────────────────────────────────────────────────────────
@bp.post("/api/campaigns/<campaign_id>/renders")
@require_auth
def start_renders(campaign_id: str):
    """Queue renders for a campaign's video assets.

    `?limit=N` renders only the first N. Rendering costs money per clip, so
    trying one before committing to the whole set is worth one query parameter.
    """
    campaign = supabase.campaign_for_user(campaign_id, g.user_id)
    if not campaign:
        return jsonify(error="not_found"), 404

    raw = request.args.get("limit")
    try:
        limit = max(1, int(raw)) if raw is not None else None
    except ValueError:
        return jsonify(error="limit must be a number"), 400

    # ?model= lets the same brief be tried on a different renderer. Prompt
    # adherence varies far more between models than any amount of prompt
    # tuning recovers, and that is worth being able to test rather than argue.
    asset_ids = {a for a in (request.args.get("assets") or "").split(",") if a.strip()}
    queued = render_queue.enqueue_campaign(
        campaign_id,
        limit=limit,
        model=(request.args.get("model") or "").strip() or None,
        asset_ids=asset_ids or None,
    )
    # Kick the drain thread. Idempotent — if one is already running it returns
    # immediately, and it picks up anything queued while it was working.
    render_runner.start()
    return jsonify(queued=len(queued), jobs=queued), 202


@bp.get("/api/campaigns/<campaign_id>/renders")
@require_auth
def list_renders(campaign_id: str):
    """Status of a campaign's renders, with a playable URL for finished ones."""
    campaign = supabase.campaign_for_user(campaign_id, g.user_id)
    if not campaign:
        return jsonify(error="not_found"), 404

    # Same trick as the campaign reaper: a stuck job matters precisely when
    # someone is looking at it, so the read path is where the check belongs
    # rather than in a scheduler nobody has built yet.
    render_queue.reap()

    jobs = supabase.select(
        "render_jobs",
        params={"campaign_id": f"eq.{campaign_id}", "order": "created_at.asc"},
    ) or []

    for job in jobs:
        job.pop("claimed_by", None)  # internal, and not the owner's business
        if job.get("status") == "done" and job.get("video_path"):
            try:
                job["video_url"] = supabase.signed_download_url(
                    render_queue.BUCKET, job["video_path"]
                )
            except Exception as e:
                # A finished render that cannot be signed right now is still a
                # finished render; don't fail the whole list over it.
                log.warning("[render] could not sign %s: %s", job["video_path"], e)
    return jsonify(jobs=jobs)


@bp.get("/api/campaigns/<campaign_id>/ready-to-post")
@require_auth
def ready_to_post(campaign_id: str):
    """TikTok / Reels packages: caption + overlay + signed video when rendered.

    One row per video asset. `ready` is true only when a finished render has a
    playable URL — captions alone are not ready-to-post.
    """
    campaign = supabase.campaign_for_user(campaign_id, g.user_id)
    if not campaign:
        return jsonify(error="not_found"), 404

    render_queue.reap()
    items = render_ready.pack_campaign(campaign_id)
    return jsonify(
        campaign_id=campaign_id,
        ready=sum(1 for i in items if i.get("ready")),
        total=len(items),
        items=items,
    )


@bp.post("/api/campaigns/<campaign_id>/tiktok-patterns")
@require_auth
def ingest_tiktok_pattern(campaign_id: str):
    """Pull public oEmbed for a TikTok URL and store the market pattern.

    Body: { "url": "https://www.tiktok.com/@…/video/…" }
    Does not download the video file.
    """
    campaign = supabase.campaign_for_user(campaign_id, g.user_id)
    if not campaign:
        return jsonify(error="not_found"), 404

    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    try:
        oembed, pattern = tiktok_pattern.ingest(url)
    except tiktok_pattern.TikTokPatternError as e:
        return jsonify(error="tiktok_pattern_failed", detail=str(e)), 422

    row = supabase.insert(
        "tiktok_patterns",
        {
            "campaign_id": campaign_id,
            "source_url": pattern["source_url"],
            "oembed": oembed,
            "pattern": pattern,
            "format_family": pattern.get("format_family"),
            "hook_style": pattern.get("hook_style"),
            "niche_tags": pattern.get("niche_tags") or [],
        },
    )
    return jsonify(pattern=row), 201


@bp.get("/api/campaigns/<campaign_id>/tiktok-patterns")
@require_auth
def list_tiktok_patterns(campaign_id: str):
    if not supabase.campaign_for_user(campaign_id, g.user_id):
        return jsonify(error="not_found"), 404
    rows = supabase.select(
        "tiktok_patterns",
        params={"campaign_id": f"eq.{campaign_id}", "order": "created_at.desc"},
    ) or []
    return jsonify(patterns=rows)


@bp.post("/api/campaigns/<campaign_id>/adapt")
@require_auth
def start_adapt(campaign_id: str):
    """Queue an ffmpeg adapt of an owned source to a stored (or inline) pattern.

    Body: {
      "asset_id": "...",
      "source_url": "https://…/owned-clip.mp4",   # must be fetchable by the API
      "pattern_id": "...",                        # optional — from tiktok_patterns
      "seconds": 15
    }
    """
    campaign = supabase.campaign_for_user(campaign_id, g.user_id)
    if not campaign:
        return jsonify(error="not_found"), 404

    body = request.get_json(silent=True) or {}
    asset_id = (body.get("asset_id") or "").strip()
    source_url = (body.get("source_url") or "").strip()
    if not asset_id or not source_url:
        return jsonify(error="asset_id_and_source_url_required"), 400

    pattern = None
    pattern_id = (body.get("pattern_id") or "").strip()
    if pattern_id:
        row = supabase.select(
            "tiktok_patterns",
            params={"id": f"eq.{pattern_id}", "campaign_id": f"eq.{campaign_id}"},
            single=True,
        )
        if not row:
            return jsonify(error="pattern_not_found"), 404
        pattern = row.get("pattern") or {}

    try:
        seconds = int(body.get("seconds") or 15)
    except (TypeError, ValueError):
        return jsonify(error="seconds_must_be_a_number"), 400

    try:
        job = render_queue.enqueue_adapt(
            campaign_id,
            asset_id=asset_id,
            source_url=source_url,
            pattern=pattern,
            seconds=max(3, min(seconds, 60)),
        )
    except ValueError as e:
        return jsonify(error=str(e)), 404

    render_runner.start()
    return jsonify(queued=1, job=job), 202


@bp.get("/api/video-production")
@require_auth
def video_production_modes():
    """Which formats generate vs adapt — so the UI can label cost correctly."""
    return jsonify(
        generate=formats.GENERATE,
        adapt=formats.ADAPT,
        remix=formats.REMIX,
        builtins=tiktok_pattern.list_builtins(),
        note=(
            "Remix (slideshow) is the cheap Fastlane-shaped default. "
            "Adapt = ffmpeg on owned footage. Generate (fal) = product UGC + motion only."
        ),
    )


@bp.get("/api/pattern-templates")
@require_auth
def pattern_templates():
    """Builtin rebuild templates (starter library) + optional family filter."""
    family = (request.args.get("family") or "").strip() or None
    return jsonify(templates=tiktok_pattern.list_builtins(family=family))


@bp.post("/api/campaigns/<campaign_id>/remix/slideshow")
@require_auth
def remix_slideshow(campaign_id: str):
    """Rebuild a slideshow from a builtin or ingested pattern + business copy.

    Body (all optional except we need a campaign):
    {
      "builtin_id": "builtin:slideshow-problem-agitate-solve",
      "pattern_id": "<uuid from tiktok-patterns>",
      "hook": "...",
      "cta": "...",
      "points": ["...", "..."],
      "channel": "tiktok"
    }
    """
    campaign = supabase.campaign_for_user(campaign_id, g.user_id)
    if not campaign:
        return jsonify(error="not_found"), 404

    business = supabase.select(
        "businesses",
        params={"id": f"eq.{campaign['business_id']}"},
        single=True,
    ) or {}

    body = request.get_json(silent=True) or {}
    try:
        result = remix.remix_slideshow(
            campaign_id,
            business=business,
            pattern_id=(body.get("pattern_id") or "").strip() or None,
            builtin_id=(body.get("builtin_id") or "").strip() or None,
            hook=(body.get("hook") or "").strip() or None,
            cta=(body.get("cta") or "").strip() or None,
            points=body.get("points") if isinstance(body.get("points"), list) else None,
            channel=(body.get("channel") or "tiktok").strip() or "tiktok",
        )
    except remix.RemixError as e:
        return jsonify(error="remix_failed", detail=str(e)), 422
    except Exception as e:
        log.exception("[remix] slideshow failed")
        return jsonify(error="remix_failed", detail=str(e)), 500

    return jsonify(result), 201
