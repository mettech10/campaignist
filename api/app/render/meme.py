"""Meme remix — top/bottom caption cards (POV / Nobody: style).

Uses owned product copy and optional owned/licensed background image.
Never downloads another creator's TikTok file.
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


class MemeError(RuntimeError):
    pass


def _escape(text: str) -> str:
    return _SAFE.sub(lambda m: "\\" + m.group(0), (text or "").replace("\n", " "))


def _wrap(text: str, width: int = 28) -> str:
    words = (text or "").split()
    lines, cur = [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if len(trial) <= width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return "\n".join(lines[:4]) or " "


def fill_template(template: str, *, product: str) -> str:
    return (template or "").replace("{product}", product).strip()


def fetch_image(url: str) -> bytes | None:
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=60)
        if resp.status_code >= 400 or len(resp.content) < 100:
            return None
        return resp.content
    except requests.RequestException:
        return None


def render_meme_bytes(
    *,
    top_text: str,
    bottom_text: str,
    seconds: float = 5.0,
    bg_image: bytes | None = None,
    bg_color: str = "0x111827",
) -> bytes:
    top = _escape(_wrap(top_text, 26))
    bottom = _escape(_wrap(bottom_text, 26))
    # drawtext with textfile is more reliable for multiline — write temp files
    with tempfile.TemporaryDirectory(prefix="campaignist-meme-") as tmp:
        root = Path(tmp)
        top_f = root / "top.txt"
        bot_f = root / "bot.txt"
        top_f.write_text(_wrap(top_text, 26) or " ")
        bot_f.write_text(_wrap(bottom_text, 26) or " ")
        out = root / "out.mp4"

        # Escape path for drawtext textfile=
        def tf(p: Path) -> str:
            return str(p).replace("\\", "/").replace(":", "\\:")

        common = (
            f"drawtext=textfile='{tf(top_f)}':fontsize=64:fontcolor=white:borderw=5:"
            f"bordercolor=black:x=(w-text_w)/2:y=h*0.12:line_spacing=10,"
            f"drawtext=textfile='{tf(bot_f)}':fontsize=56:fontcolor=white:borderw=5:"
            f"bordercolor=black:x=(w-text_w)/2:y=h*0.72:line_spacing=10"
        )

        if bg_image:
            img = root / "bg.jpg"
            img.write_bytes(bg_image)
            vf = (
                f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
                f"crop={WIDTH}:{HEIGHT},{common}"
            )
            cmd = [
                FFMPEG, "-y", "-loop", "1", "-i", str(img),
                "-t", str(max(3.0, float(seconds))),
                "-vf", vf,
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", str(out),
            ]
        else:
            vf = common
            cmd = [
                FFMPEG, "-y",
                "-f", "lavfi",
                "-i", f"color=c={bg_color}:s={WIDTH}x{HEIGHT}:d={max(3.0, float(seconds))}",
                "-vf", vf,
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", str(out),
            ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            raise MemeError(f"ffmpeg_failed: {proc.stderr[-400:]}")
        data = out.read_bytes()
        if len(data) < 1000:
            raise MemeError("empty_output")
        return data
