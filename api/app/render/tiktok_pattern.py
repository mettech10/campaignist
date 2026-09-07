"""TikTok *pattern* ingest — metadata only, never the other creator's file.

TikTok does not offer an official download API. oEmbed gives title, author,
thumbnail and embed HTML for a public URL. That is enough to learn the market
pattern (hook line, hashtags, author positioning) without copying the video.

Phase 1 also ships builtin pattern templates (slideshow / hook_demo / meme /
ugc) so remix works before a living trend crawl exists — same idea as Fastlane's
library, starting small and owned by us.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

OEMBED_URL = "https://www.tiktok.com/oembed"
_HASHTAG = re.compile(r"#([\w]+)", re.UNICODE)
_TIKTOK_HOSTS = {"tiktok.com", "www.tiktok.com", "vm.tiktok.com", "m.tiktok.com"}

# Structural families we know how to rebuild. Fastlane-shaped, Campaignist-owned.
FORMAT_FAMILIES = ("slideshow", "hook_demo", "meme", "ugc")


class TikTokPatternError(RuntimeError):
    pass


def normalize_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        raise TikTokPatternError("tiktok_url_required")
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host not in _TIKTOK_HOSTS and not host.endswith(".tiktok.com"):
        raise TikTokPatternError(f"not_a_tiktok_url: {host or raw}")
    return raw


def fetch_oembed(url: str, *, timeout: int = 20) -> dict:
    target = normalize_url(url)
    try:
        resp = requests.get(OEMBED_URL, params={"url": target}, timeout=timeout)
    except requests.RequestException as e:
        raise TikTokPatternError(f"oembed_unreachable: {e}") from e
    if resp.status_code == 404:
        raise TikTokPatternError("tiktok_not_found")
    if resp.status_code >= 400:
        raise TikTokPatternError(f"oembed_http_{resp.status_code}")
    data = resp.json()
    if data.get("type") != "video":
        raise TikTokPatternError("oembed_not_a_video")
    return data


def _guess_family(title: str, tags: list[str]) -> str:
    blob = f"{title} {' '.join(tags)}".lower()
    if any(w in blob for w in ("slideshow", "carousel", "photodump", "photo dump")):
        return "slideshow"
    if any(w in blob for w in ("meme", "pov", "when you", "nobody:")):
        return "meme"
    if any(w in blob for w in ("demo", "unbox", "tutorial", "how i", "watch me")):
        return "hook_demo"
    # Default volume format for small businesses — cheap to rebuild.
    return "slideshow"


def _guess_hook_style(hook: str) -> str:
    h = (hook or "").lower()
    if h.startswith(("stop", "don't", "dont", "never", "wait")):
        return "pattern_interrupt"
    if "?" in hook:
        return "question"
    if any(h.startswith(w) for w in ("how ", "why ", "what ")):
        return "how_why"
    if re.match(r"^\d+", hook):
        return "listicle"
    return "bold_claim"


def extract_pattern(oembed: dict, *, source_url: str) -> dict:
    """Compress oEmbed into a rebuildable structure (Fastlane-shaped)."""
    title = (oembed.get("title") or "").strip()
    tags = _HASHTAG.findall(title)
    hook = _HASHTAG.sub("", title).strip(" -–—|")
    family = _guess_family(title, tags)
    return {
        "source_url": source_url,
        "author_name": oembed.get("author_name") or "",
        "author_url": oembed.get("author_url") or "",
        "title": title,
        "hook": hook,
        "hashtags": [f"#{t}" for t in tags],
        "thumbnail_url": oembed.get("thumbnail_url") or "",
        "provider": oembed.get("provider_name") or "TikTok",
        "format_family": family,
        "hook_style": _guess_hook_style(hook),
        # Heuristics until we have real video analysis. Slideshows on TikTok
        # typically run 4–7 cards at ~2s; hook+demo often lands ~12–20s.
        "slide_count": 5 if family == "slideshow" else None,
        "seconds_per_slide": 2.5 if family == "slideshow" else None,
        "duration_hint_s": 12 if family == "slideshow" else 15,
        "caption_rhythm": "hook_then_hashtags",
        "pacing": "fast_cuts" if family in ("meme", "hook_demo") else "card_hold",
        "overlay_style": "bold_center",
        "notes": (
            "Pattern only — the source video file was not downloaded. "
            "Rebuild with owned media / slideshow remix / UGC generate."
        ),
    }


def ingest(url: str) -> tuple[dict, dict]:
    source_url = normalize_url(url)
    oembed = fetch_oembed(source_url)
    return oembed, extract_pattern(oembed, source_url=source_url)


# ── Builtin templates (starter “library”) ───────────────────────────────────
BUILTIN_PATTERNS: list[dict] = [
    {
        "id": "builtin:slideshow-problem-agitate-solve",
        "label": "Slideshow — problem → agitate → solve",
        "niche_tags": ["local-service", "ecommerce", "saas"],
        "pattern": {
            "source_url": "builtin:slideshow-problem-agitate-solve",
            "format_family": "slideshow",
            "hook_style": "pattern_interrupt",
            "hook": "Stop doing it the hard way",
            "hashtags": [],
            "slide_count": 5,
            "seconds_per_slide": 2.5,
            "duration_hint_s": 12,
            "caption_rhythm": "hook_then_hashtags",
            "pacing": "card_hold",
            "overlay_style": "bold_center",
            "slide_roles": ["hook", "problem", "agitate", "solution", "cta"],
            "notes": "Builtin template — no TikTok source.",
            "provider": "Campaignist",
        },
    },
    {
        "id": "builtin:slideshow-listicle-5",
        "label": "Slideshow — 5 things listicle",
        "niche_tags": ["education", "local-service", "creator"],
        "pattern": {
            "source_url": "builtin:slideshow-listicle-5",
            "format_family": "slideshow",
            "hook_style": "listicle",
            "hook": "5 things nobody tells you",
            "hashtags": [],
            "slide_count": 6,
            "seconds_per_slide": 2.2,
            "duration_hint_s": 13,
            "caption_rhythm": "hook_then_hashtags",
            "pacing": "card_hold",
            "overlay_style": "bold_center",
            "slide_roles": ["hook", "item", "item", "item", "item", "cta"],
            "notes": "Builtin template — no TikTok source.",
            "provider": "Campaignist",
        },
    },
    {
        "id": "builtin:hook-demo-open",
        "label": "Hook + demo — bold claim then product",
        "niche_tags": ["ecommerce", "saas", "app"],
        "pattern": {
            "source_url": "builtin:hook-demo-open",
            "format_family": "hook_demo",
            "hook_style": "bold_claim",
            "hook": "This is what actually works",
            "hashtags": [],
            "slide_count": None,
            "seconds_per_slide": None,
            "duration_hint_s": 12,
            "caption_rhythm": "hook_then_hashtags",
            "pacing": "fast_cuts",
            "overlay_style": "top_third",
            "slide_roles": ["hook", "demo", "cta"],
            "notes": "Builtin template — Phase 2 rebuilds with owned demo clip.",
            "provider": "Campaignist",
        },
    },
]


def list_builtins(*, family: str | None = None) -> list[dict]:
    rows = BUILTIN_PATTERNS
    if family:
        rows = [b for b in rows if b["pattern"].get("format_family") == family]
    return rows


def builtin_by_id(template_id: str) -> dict | None:
    for row in BUILTIN_PATTERNS:
        if row["id"] == template_id:
            return row
    return None
