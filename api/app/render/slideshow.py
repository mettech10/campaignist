"""Slideshow remix — cheapest Fastlane-shaped rebuild.

Takes a market pattern (builtin or oEmbed-derived) plus product copy/images and
builds a 9:16 card video with ffmpeg. No third-party TikTok file is used.

Slides are also returned as structured data so TikTok Photo Mode / a future
carousel export can use the same pack without re-rendering.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
WIDTH, HEIGHT = 1080, 1920


class SlideshowError(RuntimeError):
    pass


_SAFE = re.compile(r"[\\'\":]")


def _escape(text: str) -> str:
    return _SAFE.sub(lambda m: "\\" + m.group(0), (text or "").replace("\n", " "))


def build_slide_copy(
    *,
    pattern: dict,
    business_name: str,
    offer: str,
    hook: str | None = None,
    cta: str | None = None,
    points: list[str] | None = None,
) -> list[dict]:
    """Produce ordered slide cards from pattern roles + business copy."""
    roles = list(pattern.get("slide_roles") or ["hook", "problem", "solution", "cta"])
    n = int(pattern.get("slide_count") or len(roles))
    roles = (roles + ["cta"] * n)[:n]

    hook_line = (hook or pattern.get("hook") or f"Meet {business_name}").strip()
    cta_line = (cta or f"Try {business_name}").strip()
    offer_line = (offer or business_name).strip()
    pts = [p.strip() for p in (points or []) if p and str(p).strip()]

    slides: list[dict] = []
    item_i = 0
    for i, role in enumerate(roles):
        if role == "hook":
            text = hook_line
        elif role == "problem":
            text = pts[item_i] if item_i < len(pts) else f"Most people struggle with {offer_line}"
            item_i += 1 if item_i < len(pts) else 0
        elif role == "agitate":
            text = pts[item_i] if item_i < len(pts) else "And it costs more time and money every week"
            item_i += 1 if item_i < len(pts) else 0
        elif role == "solution":
            text = f"{business_name}: {offer_line}" if offer_line else business_name
        elif role == "item":
            text = pts[item_i] if item_i < len(pts) else f"Tip {i}: stay consistent"
            item_i += 1
        elif role == "cta":
            text = cta_line
        else:
            text = pts[item_i] if item_i < len(pts) else offer_line
            item_i += 1 if item_i < len(pts) else 0
        slides.append({"index": i, "role": role, "text": text[:120]})
    return slides


def render_slideshow_mp4(
    slides: list[dict],
    dest: Path,
    *,
    seconds_per_slide: float = 2.5,
    bg: str = "0x111827",
) -> Path:
    """Render solid-color cards with centered text — volume format, low cost."""
    if not slides:
        raise SlideshowError("no_slides")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dur = max(1.5, float(seconds_per_slide))

    with tempfile.TemporaryDirectory(prefix="campaignist-slides-") as tmp:
        tmp_path = Path(tmp)
        parts: list[Path] = []
        for slide in slides:
            out = tmp_path / f"slide_{slide['index']:02d}.mp4"
            text = _escape(slide["text"])
            # Wrap-ish: drawtext doesn't wrap well; keep lines short upstream.
            vf = (
                f"drawtext=text='{text}':fontsize=56:fontcolor=white:"
                f"borderw=4:bordercolor=black:x=(w-text_w)/2:y=(h-text_h)/2:"
                f"line_spacing=12"
            )
            cmd = [
                FFMPEG, "-y",
                "-f", "lavfi",
                "-i", f"color=c={bg}:s={WIDTH}x{HEIGHT}:d={dur}",
                "-vf", vf,
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-t", str(dur),
                str(out),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if proc.returncode != 0:
                raise SlideshowError(f"slide_ffmpeg_failed: {proc.stderr[-300:]}")
            parts.append(out)

        listing = tmp_path / "list.txt"
        listing.write_text("".join(f"file '{p}'\n" for p in parts))
        cmd = [
            FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
            "-c", "copy", str(dest),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            # Re-encode concat if copy fails across mismatched params
            cmd = [
                FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                str(dest),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if proc.returncode != 0:
                raise SlideshowError(f"concat_failed: {proc.stderr[-300:]}")

    if not dest.is_file() or dest.stat().st_size < 1000:
        raise SlideshowError("empty_output")
    return dest


def render_slideshow_bytes(slides: list[dict], *, seconds_per_slide: float = 2.5) -> bytes:
    with tempfile.TemporaryDirectory(prefix="campaignist-slides-out-") as tmp:
        dest = Path(tmp) / "slideshow.mp4"
        render_slideshow_mp4(slides, dest, seconds_per_slide=seconds_per_slide)
        return dest.read_bytes()
