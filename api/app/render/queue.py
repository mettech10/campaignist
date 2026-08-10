"""The video render queue.

Campaignist writes shot briefs; this turns one into an actual video by calling
fal.ai. A job is still a *row* rather than a function call, because a render
takes minutes and the process running it can restart at any time — Render
redeploys mid-render exactly as a rented GPU box got interrupted mid-render.

This began as a queue for a GPU worker on Vast.ai, and the lease survived the
move to a hosted API for the same reason it existed then: work that vanishes
silently is worse than work that fails loudly, because nobody knows to look.
What went away was the GPU box, ComfyUI, torch, two 8GB checkpoints and the
worker that tended them.
"""
from __future__ import annotations

import logging
import requests
from datetime import datetime, timedelta, timezone

from .. import supabase
from ..config import Config
from ..pipeline import formats
from . import fal

log = logging.getLogger(__name__)

BUCKET = "renders"

# A job is abandoned this long after its last heartbeat. The processor beats on
# every fal poll (5s), so five minutes of silence means the thread is gone, not
# that fal is slow. Cheap to be wrong in this direction: a late reap costs
# latency, an early one pays twice for the same video.
LEASE_TIMEOUT = timedelta(minutes=5)

# Renders fail for transient reasons (interruption, OOM on a big frame count),
# so a job gets a few goes before it is called dead.
MAX_ATTEMPTS = 3

# Only these produce video. The rest of the taxonomy is stills and text.
VIDEO_FORMATS = {k for k, v in formats.FORMATS.items() if v["category"] == "video"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def enqueue_campaign(campaign_id: str) -> list[dict]:
    """Queue a render for every video asset in a campaign.

    Idempotent by construction: a partial unique index allows only one live job
    per asset, so calling this twice queues nothing the second time rather than
    paying to render everything again.
    """
    assets = supabase.select(
        "content_assets",
        params={"campaign_id": f"eq.{campaign_id}", "select": "id,format,content,image_brief"},
    ) or []

    # Assets already queued or rendering. PostgREST cannot express ON CONFLICT
    # against a *partial* unique index — Postgres needs the index predicate to
    # infer it — so the skip happens here and the index stays as the backstop
    # for two requests arriving at once.
    live = supabase.select(
        "render_jobs",
        params={"campaign_id": f"eq.{campaign_id}",
                "status": "in.(queued,claimed)", "select": "asset_id"},
    ) or []
    already = {row["asset_id"] for row in live}

    rows = []
    for asset in assets:
        if asset["id"] in already:
            continue
        fmt = formats.FORMATS.get(asset.get("format") or "")
        if not fmt or fmt["category"] != "video":
            continue
        content = asset.get("content") or {}
        rows.append({
            "asset_id": asset["id"],
            "campaign_id": campaign_id,
            "format": asset["format"],
            "aspect_ratio": fmt["aspect_ratio"],
            # Denormalised so the worker renders from one self-contained
            # payload and never needs a second call to find out what to make.
            "prompt": {
                "brief": asset.get("image_brief") or "",
                "hook": content.get("hook", ""),
                "cta": content.get("cta", ""),
                "format_label": fmt["label"],
                "guidance": fmt.get("brief_guidance", ""),
            },
        })

    if not rows:
        return []

    try:
        queued = supabase.insert("render_jobs", rows)
    except supabase.SupabaseError as e:
        # The unique index fired, so a concurrent request queued these first.
        # Nothing was lost and the work is already scheduled.
        if "render_jobs_one_live_per_asset" in str(e) or "duplicate key" in str(e):
            log.info("[render] campaign %s already queued by a concurrent request",
                     campaign_id)
            return []
        raise

    queued = queued if isinstance(queued, list) else [queued]
    log.info("[render] campaign %s queued %d video assets", campaign_id, len(queued))
    return queued


def claim(worker_id: str) -> dict | None:
    """Lease the oldest queued job, or return None if idle.

    The status filter in the update is the whole concurrency story: two threads
    racing for the same row means one of them patches a row that is no longer
    `queued` and gets nothing back, so it moves on.
    """
    candidates = supabase.select(
        "render_jobs",
        params={"status": "eq.queued", "order": "created_at.asc",
                "limit": "5", "select": "id"},
    ) or []

    for candidate in candidates:
        won = supabase.update(
            "render_jobs",
            {"status": "claimed", "claimed_by": worker_id,
             "claimed_at": _now(), "progress_at": _now(), "updated_at": _now()},
            params={"id": f"eq.{candidate['id']}", "status": "eq.queued"},
        )
        if won:
            job = won[0] if isinstance(won, list) else won
            log.info("[render] job %s claimed by %s", job["id"], worker_id)
            return job
    return None


def heartbeat(job_id: str, worker_id: str) -> bool:
    """Extend a lease. False means it is gone and the caller must stop.

    Scoping to claimed_by matters: if this job was reaped and re-claimed while
    the render was running, the beat must not steal it back from whoever holds
    it now.
    """
    updated = supabase.update(
        "render_jobs",
        {"progress_at": _now(), "updated_at": _now()},
        params={"id": f"eq.{job_id}", "status": "eq.claimed",
                "claimed_by": f"eq.{worker_id}"},
    )
    return bool(updated)


def complete(job_id: str, worker_id: str, video_path: str) -> dict | None:
    updated = supabase.update(
        "render_jobs",
        {"status": "done", "video_path": video_path, "error": None,
         "progress_at": _now(), "updated_at": _now()},
        params={"id": f"eq.{job_id}", "status": "eq.claimed",
                "claimed_by": f"eq.{worker_id}"},
    )
    if not updated:
        log.warning("[render] job %s finished but its lease was gone", job_id)
        return None
    return updated[0] if isinstance(updated, list) else updated


def fail(job_id: str, worker_id: str, reason: str) -> dict | None:
    """Record a failure, and requeue while attempts remain."""
    current = supabase.select("render_jobs", params={"id": f"eq.{job_id}"}, single=True)
    if not current:
        return None

    attempts = (current.get("attempts") or 0) + 1
    exhausted = attempts >= MAX_ATTEMPTS
    updated = supabase.update(
        "render_jobs",
        {"status": "error" if exhausted else "queued",
         "attempts": attempts,
         "error": reason[:500],
         "claimed_by": None, "claimed_at": None,
         "progress_at": _now(), "updated_at": _now()},
        params={"id": f"eq.{job_id}", "status": "eq.claimed",
                "claimed_by": f"eq.{worker_id}"},
    )
    if not updated:
        return None
    log.warning("[render] job %s failed (attempt %d/%d): %s",
                job_id, attempts, MAX_ATTEMPTS, reason[:120])
    return updated[0] if isinstance(updated, list) else updated


def _fal_payload(job: dict) -> dict:
    """Turn a shot brief into a fal request.

    The brief is written for a human with a camera — "straight-down perspective,
    an open sketchbook turned 45 degrees" — which is already close to what an
    image-to-video model wants, so it goes through nearly as written rather than
    being rewritten into keyword soup.
    """
    prompt = job.get("prompt") or {}
    text = " ".join(x for x in (prompt.get("brief"), prompt.get("guidance")) if x)
    return {
        "prompt": text[:1500],
        "aspect_ratio": job.get("aspect_ratio") or "9:16",
        "duration": Config.FAL_VIDEO_SECONDS,
    }


class LeaseLost(RuntimeError):
    """The job was reaped and re-claimed while this thread was rendering."""


class UploadFailed(RuntimeError):
    pass


def process(job: dict, worker_id: str) -> None:
    """Render one job end to end: submit, wait, store, mark done.

    Runs in a background thread, so nothing here may raise past the caller. A
    job that dies quietly is what the reaper is for; a job that fails loudly
    should record why while it still can.
    """
    job_id = job["id"]
    model = Config.FAL_VIDEO_MODEL
    try:
        request_id = fal.submit(model, _fal_payload(job))
        log.info("[render] job %s submitted to %s as %s", job_id, model, request_id)

        # Beat on every poll. A long queue at fal must not read as a dead
        # thread — and if the lease has gone, stop rather than upload over
        # whoever owns the job now.
        def beat() -> None:
            if not heartbeat(job_id, worker_id):
                raise LeaseLost(job_id)

        result = fal.wait(model, request_id, on_progress=beat)
        video = fal.video_bytes(result)

        # Campaign id first: the storage policy reads it to decide who may watch.
        path = f"{job['campaign_id']}/{job_id}.mp4"
        upload = supabase.signed_upload_url(BUCKET, path)
        resp = requests.put(
            upload["url"], data=video,
            headers={"Content-Type": "video/mp4", "x-upsert": "true"}, timeout=600,
        )
        if resp.status_code >= 400:
            raise UploadFailed(f"{resp.status_code} {resp.text[:200]}")

        complete(job_id, worker_id, path)
        log.info("[render] job %s done (%.1f MB)", job_id, len(video) / 1e6)

    except LeaseLost:
        # Someone else owns this now. Saying anything would only interfere.
        log.warning("[render] job %s: lease lost mid-render, leaving it alone", job_id)
    except Exception as e:
        log.exception("[render] job %s failed", job_id)
        fail(job_id, worker_id, f"{type(e).__name__}: {e}")


def reap(now: datetime | None = None) -> int:
    """Return abandoned leases to the queue.

    An interrupted Vast box cannot tell us it died, so nothing else will ever
    move these rows. Called from the read path, like the campaign reaper: the
    moment someone looks at a queue is exactly when a stuck job matters.
    """
    now = now or datetime.now(timezone.utc)
    stale_before = now - LEASE_TIMEOUT

    claimed = supabase.select(
        "render_jobs",
        params={"status": "eq.claimed",
                "select": "id,attempts,progress_at,claimed_by"},
    ) or []

    reaped = 0
    for job in claimed:
        beat = _parse(job.get("progress_at"))
        if beat is None or beat > stale_before:
            continue

        attempts = (job.get("attempts") or 0) + 1
        exhausted = attempts >= MAX_ATTEMPTS
        updated = supabase.update(
            "render_jobs",
            {"status": "error" if exhausted else "queued",
             "attempts": attempts,
             "error": "The GPU worker stopped responding partway through. "
                      + ("Giving up after repeated attempts."
                         if exhausted else "Requeued automatically."),
             "claimed_by": None, "claimed_at": None, "updated_at": _now()},
            # Still claimed by the same worker, so a job that finished between
            # the read and this write is not clobbered.
            params={"id": f"eq.{job['id']}", "status": "eq.claimed",
                    "claimed_by": f"eq.{job.get('claimed_by')}"},
        )
        if updated:
            reaped += 1
            log.warning("[render] job %s lease expired (attempt %d/%d)",
                        job["id"], attempts, MAX_ATTEMPTS)
    return reaped
