"""Assemble ready-to-post packages from rendered video assets.

Campaignist already writes the hook/body/CTA/hashtags and fal already produces
an mp4. What was missing is the join: one payload an owner can copy into TikTok
or Reels without hunting across assets and render jobs.

This is deliberately export-shaped, not a publisher. OAuth posting is Phase 2;
getting the package out of the app is the ready-to-post gap we can close now.
"""
from __future__ import annotations

import logging
from typing import Any

from .. import supabase
from ..pipeline import formats
from . import queue

log = logging.getLogger(__name__)

# TikTok's documented caption ceiling is higher in some regions; 2200 is the
# conservative limit that still pastes cleanly everywhere we care about.
CAPTION_LIMIT = 2200

# Overlay text on a 9:16 phone screen has to be skimmed in one glance.
OVERLAY_LIMIT = 48


def _copy(asset: dict) -> dict:
    """Prefer the owner's edit when they have one."""
    return asset.get("edited_content") or asset.get("content") or {}


def caption_for(asset: dict) -> str:
    """TikTok / Reels caption: hook, body, CTA, then hashtags."""
    copy = _copy(asset)
    parts = [
        (copy.get("hook") or "").strip(),
        (copy.get("body") or "").strip(),
        (copy.get("cta") or "").strip(),
    ]
    text = "\n\n".join(p for p in parts if p)

    tags = copy.get("hashtags") or []
    if isinstance(tags, list) and tags:
        cleaned = []
        for tag in tags:
            t = str(tag).strip()
            if not t:
                continue
            cleaned.append(t if t.startswith("#") else f"#{t.lstrip('#')}")
        if cleaned:
            text = f"{text}\n\n{' '.join(cleaned)}".strip()

    if len(text) <= CAPTION_LIMIT:
        return text
    return text[: CAPTION_LIMIT - 1].rstrip() + "…"


def overlay_for(asset: dict) -> str:
    """Short on-screen line derived from the hook — for editors / later burn-in."""
    hook = (_copy(asset).get("hook") or "").strip()
    if len(hook) <= OVERLAY_LIMIT:
        return hook
    return hook[: OVERLAY_LIMIT - 1].rstrip() + "…"


def platform_for(channel: str | None, fmt_id: str | None) -> str:
    """Best-guess destination for the package, from channel then format."""
    ch = (channel or "").lower()
    if "tiktok" in ch:
        return "tiktok"
    if "reel" in ch or "instagram" in ch:
        return "instagram_reels"
    if "meta" in ch or "facebook" in ch:
        return "meta_ads"
    if "linkedin" in ch:
        return "linkedin"
    # formats.py does not carry bestFor — that lives on the frontend. Fall
    # back to aspect: vertical video → tiktok-shaped default.
    fmt = formats.FORMATS.get(fmt_id or "") or {}
    ratio = (fmt.get("aspect_ratio") or "").replace(" ", "")
    if ratio.startswith("9:16") or "9:16" in ratio:
        return "tiktok"
    return "social_video"


def _latest_job(jobs: list[dict]) -> dict | None:
    """Prefer a finished render; otherwise the newest attempt."""
    if not jobs:
        return None
    done = [j for j in jobs if j.get("status") == "done" and j.get("video_path")]
    pool = done or jobs
    return sorted(pool, key=lambda j: j.get("updated_at") or j.get("created_at") or "")[-1]


def pack_campaign(campaign_id: str) -> list[dict[str, Any]]:
    """Video assets for a campaign, each joined to its render and captioned."""
    assets = supabase.select(
        "content_assets",
        params={
            "campaign_id": f"eq.{campaign_id}",
            "order": "created_at.asc",
        },
    ) or []

    jobs = supabase.select(
        "render_jobs",
        params={"campaign_id": f"eq.{campaign_id}", "order": "created_at.asc"},
    ) or []
    by_asset: dict[str, list[dict]] = {}
    for job in jobs:
        by_asset.setdefault(job["asset_id"], []).append(job)

    packed: list[dict[str, Any]] = []
    for asset in assets:
        fmt_id = asset.get("format") or ""
        fmt = formats.FORMATS.get(fmt_id) or {}
        if fmt.get("category") != "video":
            continue

        job = _latest_job(by_asset.get(asset["id"], []))
        copy = _copy(asset)
        item: dict[str, Any] = {
            "asset_id": asset["id"],
            "format": fmt_id,
            "format_label": fmt.get("label") or fmt_id,
            "channel": asset.get("channel"),
            "platform": platform_for(asset.get("channel"), fmt_id),
            "aspect_ratio": queue.aspect_ratio(fmt.get("aspect_ratio") or "9:16"),
            "variant": asset.get("variant"),
            "hook": copy.get("hook") or "",
            "body": copy.get("body") or "",
            "cta": copy.get("cta") or "",
            "hashtags": copy.get("hashtags") or [],
            "caption": caption_for(asset),
            "suggested_overlay": overlay_for(asset),
            "asset_status": asset.get("status"),
            "render_status": (job or {}).get("status") or "not_queued",
            "video_url": None,
            "error": (job or {}).get("error"),
            "ready": False,
        }

        if job and job.get("status") == "done" and job.get("video_path"):
            try:
                item["video_url"] = supabase.signed_download_url(
                    queue.BUCKET, job["video_path"]
                )
                item["ready"] = True
            except Exception as e:
                log.warning(
                    "[ready] could not sign %s: %s", job.get("video_path"), e
                )
                item["error"] = f"signed_url_failed: {e}"

        packed.append(item)

    return packed
