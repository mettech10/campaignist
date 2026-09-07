"""TikTok *pattern* ingest — metadata only, never the other creator's file.

TikTok does not offer an official download API. oEmbed gives title, author,
thumbnail and embed HTML for a public URL. That is enough to learn the market
pattern (hook line, hashtags, author positioning) without copying the video.

Adapted posts are built from *owned* sources (owner upload, stock the business
licensed, or Campaignist-generated UGC) shaped to that pattern via ffmpeg.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

OEMBED_URL = "https://www.tiktok.com/oembed"
_HASHTAG = re.compile(r"#([\w]+)", re.UNICODE)
# Accept www / vm / common share hosts; we only need a resolvable public URL.
_TIKTOK_HOSTS = {"tiktok.com", "www.tiktok.com", "vm.tiktok.com", "m.tiktok.com"}


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
    """Public oEmbed — no API key. Returns the raw TikTok payload."""
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


def extract_pattern(oembed: dict, *, source_url: str) -> dict:
    """Compress oEmbed into the fields an adapt edit actually uses."""
    title = (oembed.get("title") or "").strip()
    tags = _HASHTAG.findall(title)
    # Strip hashtags from the spoken/hook line so overlays stay readable.
    hook = _HASHTAG.sub("", title).strip(" -–—|")
    return {
        "source_url": source_url,
        "author_name": oembed.get("author_name") or "",
        "author_url": oembed.get("author_url") or "",
        "title": title,
        "hook": hook,
        "hashtags": [f"#{t}" for t in tags],
        "thumbnail_url": oembed.get("thumbnail_url") or "",
        "provider": oembed.get("provider_name") or "TikTok",
        # oEmbed does not expose duration; adapt defaults stay in config.
        "duration_hint_s": None,
        "notes": (
            "Pattern only — the source video file was not downloaded. "
            "Adapt an owned upload to this hook/hashtag shape."
        ),
    }


def ingest(url: str) -> tuple[dict, dict]:
    """Return (oembed, pattern) for a public TikTok URL."""
    source_url = normalize_url(url)
    oembed = fetch_oembed(source_url)
    return oembed, extract_pattern(oembed, source_url=source_url)
