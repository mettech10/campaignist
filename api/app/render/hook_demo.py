"""Hook + demo remix — Fastlane-shaped rebuild with owned footage.

Structure:
  1. Hook card (1.5–3s text on brand colour) from the market pattern
  2. Owned demo clip (phone footage / product screen / stock you license),
     graded to 9:16 with CTA burn-in
  3. Concat → one ready-to-post mp4

Never uses someone else's TikTok file — only the pattern + your source_url.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import requests

log = logging.getLogger(__name__)

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
WIDTH, HEIGHT = 1080, 1920
_SAFE = re.compile(r"[\\'\":]")


class HookDemoError(RuntimeError):
    pass


def _escape(text: str) -> str:
    return _SAFE.sub(lambda m: "\\" + m.group(0), (text or "").replace("\n", " "))


def _run(cmd: list[str], *, label: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise HookDemoError(f"{label}: {proc.stderr[-400:]}")


def render_hook_card(
    dest: Path,
    *,
    hook: str,
    seconds: float = 2.2,
    bg: str = "0x111827",
) -> Path:
    text = _escape((hook or "Watch this")[:90])
    vf = (
        f"drawtext=text='{text}':fontsize=58:fontcolor=white:borderw=4:"
        f"bordercolor=black:x=(w-text_w)/2:y=(h-text_h)/2"
    )
    _run(
        [
            FFMPEG, "-y",
            "-f", "lavfi",
            "-i", f"color=c={bg}:s={WIDTH}x{HEIGHT}:d={seconds}",
            "-vf", vf,
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-t", str(seconds),
            str(dest),
        ],
        label="hook_card",
    )
    return dest


def render_demo_clip(
    source: Path,
    dest: Path,
    *,
    cta: str,
    seconds: float = 10.0,
) -> Path:
    """Scale/crop owned footage to 9:16 and burn CTA near the bottom."""
    cta_e = _escape((cta or "")[:60])
    vf = (
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},"
        f"drawtext=text='{cta_e}':fontsize=42:fontcolor=white:borderw=3:"
        f"bordercolor=black:x=(w-text_w)/2:y=h*0.82"
    )
    _run(
        [
            FFMPEG, "-y",
            "-i", str(source),
            "-t", str(max(3.0, float(seconds))),
            "-vf", vf,
            "-an",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(dest),
        ],
        label="demo_clip",
    )
    return dest


def concat(parts: list[Path], dest: Path) -> Path:
    with tempfile.TemporaryDirectory(prefix="campaignist-hd-concat-") as tmp:
        listing = Path(tmp) / "list.txt"
        listing.write_text("".join(f"file '{p}'\n" for p in parts))
        try:
            _run(
                [
                    FFMPEG, "-y", "-f", "concat", "-safe", "0",
                    "-i", str(listing), "-c", "copy", str(dest),
                ],
                label="concat_copy",
            )
        except HookDemoError:
            _run(
                [
                    FFMPEG, "-y", "-f", "concat", "-safe", "0",
                    "-i", str(listing),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", str(dest),
                ],
                label="concat_reencode",
            )
    if not dest.is_file() or dest.stat().st_size < 1000:
        raise HookDemoError("empty_output")
    return dest


def fetch_source(url: str) -> bytes:
    try:
        resp = requests.get(url, timeout=120)
    except requests.RequestException as e:
        raise HookDemoError(f"source_fetch_failed: {e}") from e
    if resp.status_code >= 400:
        raise HookDemoError(f"source_http_{resp.status_code}")
    if len(resp.content) < 1000:
        raise HookDemoError("source_too_small")
    return resp.content


def build_hook_demo_bytes(
    *,
    hook: str,
    cta: str,
    source_bytes: bytes,
    hook_seconds: float = 2.2,
    demo_seconds: float = 10.0,
) -> bytes:
    with tempfile.TemporaryDirectory(prefix="campaignist-hook-demo-") as tmp:
        root = Path(tmp)
        src = root / "source.mp4"
        hook_p = root / "hook.mp4"
        demo_p = root / "demo.mp4"
        out = root / "out.mp4"
        src.write_bytes(source_bytes)
        render_hook_card(hook_p, hook=hook, seconds=hook_seconds)
        render_demo_clip(src, demo_p, cta=cta, seconds=demo_seconds)
        concat([hook_p, demo_p], out)
        return out.read_bytes()
