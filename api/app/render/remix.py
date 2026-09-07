"""Campaign remix entrypoints — Phase 1 is slideshow-only."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from .. import supabase
from . import queue, slideshow, tiktok_pattern

log = logging.getLogger(__name__)


class RemixError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_pattern(
    campaign_id: str,
    *,
    pattern_id: str | None = None,
    builtin_id: str | None = None,
) -> dict:
    if builtin_id:
        row = tiktok_pattern.builtin_by_id(builtin_id)
        if not row:
            raise RemixError("builtin_not_found")
        return row["pattern"]
    if pattern_id:
        row = supabase.select(
            "tiktok_patterns",
            params={"id": f"eq.{pattern_id}", "campaign_id": f"eq.{campaign_id}"},
            single=True,
        )
        if not row:
            raise RemixError("pattern_not_found")
        return row.get("pattern") or {}
    # Default: cheapest high-volume builtin.
    return tiktok_pattern.builtin_by_id(
        "builtin:slideshow-problem-agitate-solve"
    )["pattern"]


def remix_slideshow(
    campaign_id: str,
    *,
    business: dict,
    pattern_id: str | None = None,
    builtin_id: str | None = None,
    hook: str | None = None,
    cta: str | None = None,
    points: list[str] | None = None,
    channel: str = "tiktok",
) -> dict:
    """Build slides, render mp4, store asset + done render_job, return pack item."""
    pattern = resolve_pattern(campaign_id, pattern_id=pattern_id, builtin_id=builtin_id)
    if pattern.get("format_family") not in (None, "slideshow"):
        # Still allow — slideshow rebuild can use any hook pattern.
        pass

    name = business.get("name") or "Your business"
    offer = business.get("offer_description") or business.get("industry") or name
    slides = slideshow.build_slide_copy(
        pattern=pattern,
        business_name=name,
        offer=offer,
        hook=hook,
        cta=cta,
        points=points,
    )
    seconds = float(pattern.get("seconds_per_slide") or 2.5)
    video = slideshow.render_slideshow_bytes(slides, seconds_per_slide=seconds)

    content = {
        "hook": slides[0]["text"] if slides else (hook or ""),
        "body": " → ".join(s["text"] for s in slides[1:-1]) if len(slides) > 2 else "",
        "cta": slides[-1]["text"] if slides else (cta or ""),
        "hashtags": pattern.get("hashtags") or [],
        "slides": slides,
        "format_family": "slideshow",
        "pattern_source": pattern.get("source_url"),
    }

    asset = supabase.insert(
        "content_assets",
        {
            "campaign_id": campaign_id,
            "type": "social_post",
            "channel": channel,
            "format": "slideshow",
            "variant": 1,
            "content": content,
            "image_brief": "Slideshow cards remixed from market pattern",
            "status": "generated",
        },
    )
    asset = asset[0] if isinstance(asset, list) else asset

    job = supabase.insert(
        "render_jobs",
        {
            "asset_id": asset["id"],
            "campaign_id": campaign_id,
            "format": "slideshow",
            "aspect_ratio": "9:16",
            "prompt": {
                "mode": "remix_slideshow",
                "pattern": pattern,
                "slides": slides,
            },
            "status": "claimed",
            "claimed_by": "remix-inline",
            "claimed_at": _now(),
            "progress_at": _now(),
        },
    )
    job = job[0] if isinstance(job, list) else job

    path = f"{campaign_id}/{job['id']}.mp4"
    upload = supabase.signed_upload_url(queue.BUCKET, path)
    put = requests.put(
        upload["url"],
        data=video,
        headers={"Content-Type": "video/mp4", "x-upsert": "true"},
        timeout=600,
    )
    if put.status_code >= 400:
        supabase.update(
            "render_jobs",
            {"status": "error", "error": f"upload_failed:{put.status_code}"},
            params={"id": f"eq.{job['id']}"},
            returning=False,
        )
        raise RemixError(f"upload_failed:{put.status_code}")

    done = supabase.update(
        "render_jobs",
        {
            "status": "done",
            "video_path": path,
            "error": None,
            "progress_at": _now(),
            "updated_at": _now(),
            "claimed_by": None,
        },
        params={"id": f"eq.{job['id']}"},
    )
    done = done[0] if isinstance(done, list) else done

    video_url = None
    try:
        video_url = supabase.signed_download_url(queue.BUCKET, path)
    except Exception as e:
        log.warning("[remix] sign failed: %s", e)

    return {
        "asset": asset,
        "job": done or job,
        "slides": slides,
        "video_url": video_url,
        "ready": bool(video_url),
        "caption": "\n\n".join(
            p for p in (content["hook"], content["body"], content["cta"]) if p
        ),
    }
