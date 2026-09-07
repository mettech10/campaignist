"""Campaign remix entrypoints — Phase 1 is slideshow-only."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from .. import supabase
from . import hook_demo, meme, queue, slideshow, tiktok_pattern

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


def remix_hook_demo(
    campaign_id: str,
    *,
    business: dict,
    source_url: str,
    pattern_id: str | None = None,
    builtin_id: str | None = None,
    hook: str | None = None,
    cta: str | None = None,
    hook_seconds: float = 2.2,
    demo_seconds: float = 10.0,
    channel: str = "tiktok",
) -> dict:
    """Hook card + owned demo clip → ready-to-post mp4."""
    if not (source_url or "").strip():
        raise RemixError("source_url_required")

    pattern = resolve_pattern(
        campaign_id,
        pattern_id=pattern_id,
        builtin_id=builtin_id or "builtin:hook-demo-open",
    )
    name = business.get("name") or "Your business"
    offer = business.get("offer_description") or business.get("industry") or name
    hook_line = (hook or pattern.get("hook") or f"See how {name} works").strip()
    cta_line = (cta or f"Try {name}").strip()

    source_bytes = hook_demo.fetch_source(source_url.strip())
    video = hook_demo.build_hook_demo_bytes(
        hook=hook_line,
        cta=cta_line,
        source_bytes=source_bytes,
        hook_seconds=hook_seconds,
        demo_seconds=demo_seconds,
    )

    content = {
        "hook": hook_line,
        "body": offer,
        "cta": cta_line,
        "hashtags": pattern.get("hashtags") or [],
        "format_family": "hook_demo",
        "pattern_source": pattern.get("source_url"),
        "source_url": source_url.strip(),
        "segments": [
            {"role": "hook", "seconds": hook_seconds, "text": hook_line},
            {"role": "demo", "seconds": demo_seconds, "text": offer},
            {"role": "cta", "text": cta_line},
        ],
    }

    asset = supabase.insert(
        "content_assets",
        {
            "campaign_id": campaign_id,
            "type": "social_post",
            "channel": channel,
            "format": "hook-demo",
            "variant": 1,
            "content": content,
            "image_brief": "Hook card + owned demo clip remixed from market pattern",
            "status": "generated",
        },
    )
    asset = asset[0] if isinstance(asset, list) else asset

    job = supabase.insert(
        "render_jobs",
        {
            "asset_id": asset["id"],
            "campaign_id": campaign_id,
            "format": "hook-demo",
            "aspect_ratio": "9:16",
            "prompt": {
                "mode": "remix_hook_demo",
                "pattern": pattern,
                "source_url": source_url.strip(),
                "hook": hook_line,
                "cta": cta_line,
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
        log.warning("[remix] hook-demo sign failed: %s", e)

    return {
        "asset": asset,
        "job": done or job,
        "segments": content["segments"],
        "video_url": video_url,
        "ready": bool(video_url),
        "caption": "\n\n".join([p for p in (hook_line, offer, cta_line) if p]),
    }


def remix_meme(
    campaign_id: str,
    *,
    business: dict,
    pattern_id: str | None = None,
    builtin_id: str | None = None,
    top_text: str | None = None,
    bottom_text: str | None = None,
    image_url: str | None = None,
    seconds: float = 5.0,
    channel: str = "tiktok",
) -> dict:
    """POV / Nobody: meme card from pattern + product copy."""
    pattern = resolve_pattern(
        campaign_id,
        pattern_id=pattern_id,
        builtin_id=builtin_id or "builtin:meme-pov-product",
    )
    name = business.get("name") or "Your business"
    offer = business.get("offer_description") or business.get("industry") or name
    product = offer if len(offer) < 60 else name

    top = (top_text or pattern.get("top_text") or "POV").strip()
    bottom_tmpl = bottom_text or pattern.get("bottom_text") or f"using {product}"
    bottom = meme.fill_template(bottom_tmpl, product=product)
    top = meme.fill_template(top, product=product)

    bg = meme.fetch_image((image_url or "").strip()) if image_url else None
    # Fall back to pattern thumbnail only as *visual reference colour plate* —
    # thumbnails are public oEmbed stills; we treat them as optional bg when
    # the owner did not supply an image. Prefer owner image_url when present.
    if bg is None and pattern.get("thumbnail_url"):
        bg = meme.fetch_image(pattern["thumbnail_url"])

    video = meme.render_meme_bytes(
        top_text=top, bottom_text=bottom, seconds=seconds, bg_image=bg,
    )

    content = {
        "hook": top,
        "body": bottom,
        "cta": f"Follow {name}",
        "hashtags": pattern.get("hashtags") or [],
        "format_family": "meme",
        "pattern_source": pattern.get("source_url"),
        "top_text": top,
        "bottom_text": bottom,
    }

    asset = supabase.insert(
        "content_assets",
        {
            "campaign_id": campaign_id,
            "type": "social_post",
            "channel": channel,
            "format": "meme-video",
            "variant": 1,
            "content": content,
            "image_brief": "Meme top/bottom captions remixed from market pattern",
            "status": "generated",
        },
    )
    asset = asset[0] if isinstance(asset, list) else asset

    job = supabase.insert(
        "render_jobs",
        {
            "asset_id": asset["id"],
            "campaign_id": campaign_id,
            "format": "meme-video",
            "aspect_ratio": "9:16",
            "prompt": {"mode": "remix_meme", "pattern": pattern, "top": top, "bottom": bottom},
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
        upload["url"], data=video,
        headers={"Content-Type": "video/mp4", "x-upsert": "true"}, timeout=600,
    )
    if put.status_code >= 400:
        supabase.update(
            "render_jobs",
            {"status": "error", "error": f"upload_failed:{put.status_code}"},
            params={"id": f"eq.{job['id']}"}, returning=False,
        )
        raise RemixError(f"upload_failed:{put.status_code}")

    done = supabase.update(
        "render_jobs",
        {
            "status": "done", "video_path": path, "error": None,
            "progress_at": _now(), "updated_at": _now(), "claimed_by": None,
        },
        params={"id": f"eq.{job['id']}"},
    )
    done = done[0] if isinstance(done, list) else done

    video_url = None
    try:
        video_url = supabase.signed_download_url(queue.BUCKET, path)
    except Exception as e:
        log.warning("[remix] meme sign failed: %s", e)

    return {
        "asset": asset,
        "job": done or job,
        "top_text": top,
        "bottom_text": bottom,
        "video_url": video_url,
        "ready": bool(video_url),
        "caption": "\n\n".join([p for p in (top, bottom) if p]),
    }
