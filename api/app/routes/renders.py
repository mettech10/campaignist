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
from ..render import queue as render_queue, runner as render_runner

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
    queued = render_queue.enqueue_campaign(
        campaign_id, limit=limit, model=(request.args.get("model") or "").strip() or None
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
