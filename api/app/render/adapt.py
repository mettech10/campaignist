"""ffmpeg adapt — reshape an owned source clip to a market pattern.

Cost model: downloading/remixing someone else's TikTok is off the table
(no official download API, and it is their content). Instead we take a source
the business owns (phone footage, licensed stock, or a Campaignist UGC render)
and apply the pattern: 9:16 grade, trim, burned-in hook overlay, end CTA card.
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


class AdaptError(RuntimeError):
    pass


_SAFE = re.compile(r"[\\'\":]")


def _escape_drawtext(text: str) -> str:
    # ffmpeg drawtext: escape \ : '
    return _SAFE.sub(lambda m: "\\" + m.group(0), text.replace("\n", " "))


def adapt_file(
    source: Path,
    dest: Path,
    *,
    overlay: str,
    cta: str,
    seconds: int = 15,
    width: int = 1080,
    height: int = 1920,
) -> Path:
    """Write an adapted mp4 to dest. Raises AdaptError on ffmpeg failure."""
    if not source.is_file():
        raise AdaptError(f"source_missing: {source}")
    dest.parent.mkdir(parents=True, exist_ok=True)

    overlay_e = _escape_drawtext((overlay or "")[:80])
    cta_e = _escape_drawtext((cta or "")[:60])

    # Scale/crop to 9:16, trim, burn hook at top third, CTA near bottom.
    # fontcolor/box make it readable on busy footage without a separate ass file.
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},"
        f"drawtext=text='{overlay_e}':fontsize=48:fontcolor=white:borderw=3:"
        f"bordercolor=black:x=(w-text_w)/2:y=h*0.12,"
        f"drawtext=text='{cta_e}':fontsize=40:fontcolor=white:borderw=3:"
        f"bordercolor=black:x=(w-text_w)/2:y=h*0.82"
    )

    cmd = [
        FFMPEG, "-y",
        "-i", str(source),
        "-t", str(max(3, int(seconds))),
        "-vf", vf,
        "-an",  # pattern adapt is visual-first; keep audio in a later slice
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(dest),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except FileNotFoundError as e:
        raise AdaptError("ffmpeg_not_installed") from e
    except subprocess.TimeoutExpired as e:
        raise AdaptError("ffmpeg_timeout") from e
    if proc.returncode != 0:
        raise AdaptError(f"ffmpeg_failed: {proc.stderr[-400:]}")
    if not dest.is_file() or dest.stat().st_size < 1000:
        raise AdaptError("ffmpeg_empty_output")
    return dest


def adapt_bytes(
    source_bytes: bytes,
    *,
    overlay: str,
    cta: str,
    seconds: int = 15,
) -> bytes:
    """In-memory convenience for the render worker."""
    with tempfile.TemporaryDirectory(prefix="campaignist-adapt-") as tmp:
        src = Path(tmp) / "in.mp4"
        out = Path(tmp) / "out.mp4"
        src.write_bytes(source_bytes)
        adapt_file(src, out, overlay=overlay, cta=cta, seconds=seconds)
        return out.read_bytes()
