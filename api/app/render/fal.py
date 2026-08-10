"""fal.ai client — submit a render, wait for it, fetch the bytes.

Replaces a rented GPU box. That box was £54/month whether or not anything
rendered, needed ComfyUI, torch, custom nodes and two 8GB checkpoints kept
alive on an interruptible instance, and every layer of it failed in a way that
named something other than the actual cause. This is an HTTP call.

The tradeoff, stated plainly: fal's models are fixed, so the pipeline no longer
controls the graph. For turning a shot brief into b-roll that is a gain, not a
loss — but a bespoke look would need the box back.
"""
from __future__ import annotations

import logging
import time

import requests

from ..config import Config

log = logging.getLogger(__name__)


class FalError(RuntimeError):
    pass


QUEUE_HOST = "https://queue.fal.run"

# How long to wait for one video before calling it lost. fal queues behind
# demand, so this covers queue time as well as render time.
TIMEOUT = 15 * 60
POLL_EVERY = 5

# Requests to fal itself should fail fast; TIMEOUT above governs the render.
HTTP_TIMEOUT = 60


def _headers() -> dict:
    key = Config.FAL_KEY
    if not key:
        raise FalError("FAL_KEY is not set")
    return {"Authorization": f"Key {key}", "Content-Type": "application/json"}


def submit(model: str, payload: dict) -> str:
    """Queue a render. Returns fal's request id."""
    resp = requests.post(
        f"{QUEUE_HOST}/{model}", headers=_headers(), json=payload, timeout=HTTP_TIMEOUT
    )
    if resp.status_code >= 400:
        raise FalError(f"submit {model}: {resp.status_code} {resp.text[:300]}")
    request_id = resp.json().get("request_id")
    if not request_id:
        raise FalError(f"submit {model}: no request_id in {resp.text[:200]}")
    return request_id


def wait(model: str, request_id: str, *, on_progress=None) -> dict:
    """Poll until the render finishes. Returns fal's result payload.

    `on_progress` is called on every poll so the caller can keep a lease alive —
    a render that takes ten minutes must not look like a dead worker.
    """
    deadline = time.monotonic() + TIMEOUT
    status_url = f"{QUEUE_HOST}/{model}/requests/{request_id}/status"

    while time.monotonic() < deadline:
        time.sleep(POLL_EVERY)
        if on_progress:
            on_progress()

        resp = requests.get(status_url, headers=_headers(), timeout=HTTP_TIMEOUT)
        if resp.status_code >= 400:
            raise FalError(f"status {request_id}: {resp.status_code} {resp.text[:200]}")

        status = resp.json().get("status")
        if status == "COMPLETED":
            result = requests.get(
                f"{QUEUE_HOST}/{model}/requests/{request_id}",
                headers=_headers(), timeout=HTTP_TIMEOUT,
            )
            if result.status_code >= 400:
                raise FalError(f"result {request_id}: {result.status_code} {result.text[:300]}")
            return result.json()
        if status in ("FAILED", "CANCELLED"):
            raise FalError(f"fal reported {status} for {request_id}: {resp.text[:300]}")

    raise FalError(f"render exceeded {TIMEOUT}s (fal request {request_id})")


def video_bytes(result: dict) -> bytes:
    """Pull the mp4 out of a fal result and download it.

    fal hands back a URL on its own CDN. We store our own copy: that URL is
    outside our control and outside the RLS that decides who may watch a
    customer's video.
    """
    video = result.get("video") or {}
    url = video.get("url") if isinstance(video, dict) else None

    if not url:  # some models return a list
        for key in ("videos", "output", "outputs"):
            items = result.get(key)
            if isinstance(items, list) and items:
                first = items[0]
                url = first.get("url") if isinstance(first, dict) else first
                if url:
                    break

    if not url:
        raise FalError(f"no video url in fal result: {str(result)[:300]}")

    blob = requests.get(url, timeout=600)
    if blob.status_code >= 400:
        raise FalError(f"download {url}: {blob.status_code}")
    return blob.content
