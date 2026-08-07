#!/usr/bin/env python3
"""Campaignist GPU render worker.

Runs on a rented Vast.ai box next to ComfyUI. Claims a job from the Campaignist
API, renders it, uploads the video, reports back. Then does it again.

Design notes, because the constraints here are unusual:

* The box is *interruptible*. That is what makes it £1.8/day rather than £6, and
  it means the machine can vanish between any two lines of this file. Nothing
  here may assume it gets to clean up. The server-side lease is what makes that
  safe: stop beating and the job returns to the queue for someone else.

* This worker holds no database credential. It has one token, talks only to the
  Campaignist API, and is handed a URL that can write exactly one object per
  job. Losing the box costs GPU time, not data.

* It pulls rather than being pushed to. A Vast instance has an ephemeral address
  and gets replaced on interruption, so anything that needed to reach *in* would
  need a tunnel babysat forever. Polling needs no inbound networking at all.

Environment:
    CAMPAIGNIST_API     https://campaignist.onrender.com
    WORKER_TOKEN        matches RENDER_WORKER_TOKEN on Render
    WORKER_ID           defaults to the hostname
    COMFY_URL           http://127.0.0.1:8188
    WORKFLOW_DIR        directory of ComfyUI workflow JSON, one per format

Run:  python render_worker.py
"""
from __future__ import annotations

import json
import logging
import os
import random
import signal
import socket
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import requests

API = os.environ.get("CAMPAIGNIST_API", "https://campaignist.onrender.com").rstrip("/")
TOKEN = os.environ.get("WORKER_TOKEN", "")
WORKER_ID = os.environ.get("WORKER_ID") or socket.gethostname()
COMFY = os.environ.get("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
WORKFLOWS = Path(os.environ.get("WORKFLOW_DIR", Path(__file__).parent / "workflows"))

IDLE_SLEEP = 15          # queue empty; the box is paid for by the hour either way
RENDER_TIMEOUT = 45 * 60  # a single render that runs longer than this is wedged

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("worker")

_stop = threading.Event()


def _auth() -> dict:
    return {"Authorization": f"Bearer {TOKEN}", "X-Worker-Id": WORKER_ID}


class LeaseLost(Exception):
    """The server gave this job to someone else. Stop work immediately."""


class Heartbeat:
    """Tells the server this worker is still alive, for as long as it is.

    A daemon thread on purpose: if the process is killed the beating stops with
    it, which is exactly the signal the server needs. It also watches for the
    lease being revoked, so a worker that was reaped while rendering finds out
    and abandons the job instead of uploading over whoever owns it now.
    """

    def __init__(self, job_id: str, every: int):
        self.job_id = job_id
        self.every = max(5, every)
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, daemon=True)

    def __enter__(self) -> "Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _beat(self) -> None:
        while not self._stop.wait(self.every):
            try:
                resp = requests.post(
                    f"{API}/api/worker/jobs/{self.job_id}/heartbeat",
                    headers=_auth(), timeout=20,
                )
                if resp.status_code == 409:
                    log.warning("lease on %s was revoked — abandoning", self.job_id)
                    self.lost.set()
                    return
            except requests.RequestException as e:
                # A blip is not a lost lease. The server's timeout is several
                # times this interval precisely so a flaky network does not cost
                # a render that is going fine.
                log.warning("heartbeat failed (retrying): %s", e)


def claim() -> dict | None:
    resp = requests.post(f"{API}/api/worker/claim", headers=_auth(), timeout=60)
    if resp.status_code == 204:
        return None
    resp.raise_for_status()
    return resp.json()


def load_workflow(fmt: str, aspect: str, prompt: dict) -> dict:
    """Build the ComfyUI graph for one job.

    Workflows are files, not code: adding a format is dropping in a JSON graph,
    the same way adding an agent is dropping in a spec. Falls back to a default
    so a new format degrades to a generic render rather than failing the job.
    """
    candidate = WORKFLOWS / f"{fmt}.json"
    if not candidate.exists():
        candidate = WORKFLOWS / "default.json"
    if not candidate.exists():
        raise FileNotFoundError(
            f"no workflow for {fmt!r} and no default.json in {WORKFLOWS}"
        )

    graph = json.loads(candidate.read_text())
    width, height = {"9:16": (576, 1024), "1:1": (768, 768),
                     "16:9": (1024, 576)}.get(aspect, (576, 1024))

    text = " ".join(x for x in (prompt.get("brief"), prompt.get("guidance")) if x)
    # Placeholder substitution, so a workflow file stays readable JSON rather
    # than a template language.
    #
    # A placeholder appears in two positions and they need different treatment.
    # As a whole value ("__WIDTH__") it is replaced by a JSON literal, so numbers
    # arrive as numbers. Embedded in a longer string ("cinematic, __PROMPT__")
    # the replacement is going *inside* a JSON string and has to be escaped:
    # briefs are model-written prose and routinely contain double quotes, which
    # inserted raw end the string early and make the whole graph unparseable.
    rendered = json.dumps(graph)
    for key, value in {
        "__PROMPT__": text,
        "__HOOK__": prompt.get("hook", ""),
        "__WIDTH__": width,
        "__HEIGHT__": height,
        "__SEED__": random.randint(1, 2**31 - 1),
    }.items():
        literal = json.dumps(value)
        # json.dumps of a string is the escaped form wrapped in quotes; strip
        # the quotes to get something safe to splice into an existing string.
        embedded = literal[1:-1] if isinstance(value, str) else literal
        rendered = rendered.replace(f'"{key}"', literal).replace(key, embedded)

    try:
        return json.loads(rendered)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"workflow {candidate.name} did not survive substitution: {e}"
        ) from e


def render(job: dict, beat: Heartbeat) -> bytes:
    """Drive ComfyUI over HTTP and return the finished video bytes."""
    graph = load_workflow(job["format"], job["aspect_ratio"], job["prompt"])

    queued = requests.post(f"{COMFY}/prompt",
                           json={"prompt": graph, "client_id": WORKER_ID}, timeout=60)
    queued.raise_for_status()
    prompt_id = queued.json()["prompt_id"]
    log.info("comfy accepted %s as %s", job["job_id"], prompt_id)

    deadline = time.monotonic() + RENDER_TIMEOUT
    while time.monotonic() < deadline:
        if beat.lost.is_set():
            raise LeaseLost(job["job_id"])
        if _stop.is_set():
            raise KeyboardInterrupt

        time.sleep(3)
        history = requests.get(f"{COMFY}/history/{prompt_id}", timeout=30)
        history.raise_for_status()
        entry = history.json().get(prompt_id)
        if not entry:
            continue

        status = (entry.get("status") or {})
        if status.get("status_str") == "error":
            raise RuntimeError(f"comfy failed: {json.dumps(status)[:300]}")
        if not status.get("completed"):
            continue

        for node in (entry.get("outputs") or {}).values():
            for item in node.get("gifs", []) + node.get("videos", []):
                params = urllib.parse.urlencode({
                    "filename": item["filename"],
                    "subfolder": item.get("subfolder", ""),
                    "type": item.get("type", "output"),
                })
                blob = requests.get(f"{COMFY}/view?{params}", timeout=300)
                blob.raise_for_status()
                return blob.content
        raise RuntimeError("comfy reported success but produced no video output")

    raise TimeoutError(f"render exceeded {RENDER_TIMEOUT}s")


def upload(url: str, video: bytes) -> None:
    resp = requests.put(
        url, data=video,
        headers={"Content-Type": "video/mp4", "x-upsert": "true"},
        timeout=600,
    )
    resp.raise_for_status()


def handle(job: dict) -> None:
    job_id = job["job_id"]
    log.info("job %s: %s %s (attempt %s)",
             job_id, job["format"], job["aspect_ratio"], job.get("attempt"))

    with Heartbeat(job_id, job.get("heartbeat_seconds", 60)) as beat:
        try:
            video = render(job, beat)
            if beat.lost.is_set():
                raise LeaseLost(job_id)
            upload(job["upload_url"], video)
        except LeaseLost:
            # Someone else owns this now. Saying anything would only interfere.
            log.warning("job %s: lease lost, leaving it alone", job_id)
            return
        except Exception as e:
            log.exception("job %s failed", job_id)
            _report(job_id, "fail", {"reason": f"{type(e).__name__}: {e}"[:500]})
            return

        _report(job_id, "complete", {"video_path": job["upload_path"]})
        log.info("job %s done (%.1f MB)", job_id, len(video) / 1e6)


def _report(job_id: str, action: str, body: dict) -> None:
    try:
        resp = requests.post(f"{API}/api/worker/jobs/{job_id}/{action}",
                             headers=_auth(), json=body, timeout=30)
        if resp.status_code == 409:
            log.warning("job %s: lease was already gone when reporting %s", job_id, action)
        else:
            resp.raise_for_status()
    except requests.RequestException as e:
        # Nothing else to do — the lease will expire and the server will requeue
        # it, which is the whole point of leasing rather than assigning.
        log.error("job %s: could not report %s: %s", job_id, action, e)


def main() -> int:
    if not TOKEN:
        log.error("WORKER_TOKEN is not set")
        return 2

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: _stop.set())

    try:
        hello = requests.get(f"{API}/api/worker/hello", headers=_auth(), timeout=30)
        hello.raise_for_status()
    except requests.RequestException as e:
        # Fail loudly at boot rather than polling a rejecting endpoint forever
        # and looking like an empty queue.
        log.error("cannot reach %s as worker %r: %s", API, WORKER_ID, e)
        return 2

    log.info("worker %s up, polling %s", WORKER_ID, API)
    backoff = IDLE_SLEEP
    while not _stop.is_set():
        try:
            job = claim()
            backoff = IDLE_SLEEP
        except requests.RequestException as e:
            log.warning("claim failed, backing off %ss: %s", backoff, e)
            _stop.wait(backoff)
            backoff = min(backoff * 2, 300)
            continue

        if job is None:
            _stop.wait(IDLE_SLEEP)
            continue

        try:
            handle(job)
        except KeyboardInterrupt:
            log.info("interrupted mid-job; the lease will expire and it requeues")
            break

    log.info("worker %s stopping", WORKER_ID)
    return 0


if __name__ == "__main__":
    sys.exit(main())
