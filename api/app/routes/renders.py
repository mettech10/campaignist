"""Render queue endpoints.

Two audiences, deliberately split:

* `/api/renders/...` — the signed-in owner, watching their videos appear.
* `/api/worker/...`  — the GPU box, holding one shared token and nothing else.

The worker half exists so that a rented machine from an anonymous marketplace
never needs a Supabase credential. It can claim a job, say it is still alive,
upload one object, and report the outcome. It cannot read another tenant's data,
cannot reach the database, and cannot mint its own upload URLs. Losing that token
costs you some wasted GPU time and nothing else, which is the most that should
ever be true of a box you do not own.
"""
from __future__ import annotations

import functools
import hmac
import logging
import uuid

from flask import Blueprint, g, jsonify, request

from .. import render_queue, supabase
from ..auth import require_auth
from ..config import Config

log = logging.getLogger(__name__)
bp = Blueprint("renders", __name__)


def require_worker(fn):
    """Authenticate the GPU worker by shared token.

    compare_digest rather than `==`: this is a long-lived secret checked on a
    public endpoint, and a timing oracle is exactly how you would recover one
    byte at a time.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        expected = Config.RENDER_WORKER_TOKEN
        if not expected:
            # Fail closed. An unset token must not mean an open queue.
            log.error("[worker] RENDER_WORKER_TOKEN is not set — refusing")
            return jsonify(error="worker_auth_unconfigured"), 503

        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip(), expected):
            return jsonify(error="invalid_worker_token"), 401

        worker_id = (request.headers.get("X-Worker-Id") or "").strip()[:64]
        if not worker_id:
            return jsonify(error="missing_worker_id"), 400
        g.worker_id = worker_id
        return fn(*args, **kwargs)
    return wrapper


# ── Owner-facing ────────────────────────────────────────────────────────────
@bp.post("/api/campaigns/<campaign_id>/renders")
@require_auth
def start_renders(campaign_id: str):
    """Queue renders for every video asset in a campaign."""
    campaign = supabase.campaign_for_user(campaign_id, g.user_id)
    if not campaign:
        return jsonify(error="not_found"), 404

    queued = render_queue.enqueue_campaign(campaign_id)
    return jsonify(queued=len(queued), jobs=queued), 202


@bp.get("/api/campaigns/<campaign_id>/renders")
@require_auth
def list_renders(campaign_id: str):
    """Status of a campaign's renders, with a playable URL for finished ones."""
    campaign = supabase.campaign_for_user(campaign_id, g.user_id)
    if not campaign:
        return jsonify(error="not_found"), 404

    # Same trick as the campaign reaper: a stuck job matters precisely when
    # someone is looking at it, so the read path is where the check belongs
    # rather than in a scheduler nobody has built yet.
    render_queue.reap()

    jobs = supabase.select(
        "render_jobs",
        params={"campaign_id": f"eq.{campaign_id}", "order": "created_at.asc"},
    ) or []

    for job in jobs:
        job.pop("claimed_by", None)  # internal, and not the owner's business
        if job.get("status") == "done" and job.get("video_path"):
            try:
                job["video_url"] = supabase.signed_download_url(
                    render_queue.BUCKET, job["video_path"]
                )
            except Exception as e:
                # A finished render that cannot be signed right now is still a
                # finished render; don't fail the whole list over it.
                log.warning("[render] could not sign %s: %s", job["video_path"], e)
    return jsonify(jobs=jobs)


# ── Worker-facing ───────────────────────────────────────────────────────────
@bp.post("/api/worker/claim")
@require_worker
def worker_claim():
    """Lease the next job, with a one-object upload URL attached.

    204 means the queue is empty, which is the normal answer most of the time
    and is not an error.
    """
    render_queue.reap()

    job = render_queue.claim(g.worker_id)
    if not job:
        return "", 204

    # Campaign id first in the path: that is what the storage policy reads to
    # decide who may watch the video.
    path = f"{job['campaign_id']}/{job['id']}.mp4"
    try:
        upload = supabase.signed_upload_url(render_queue.BUCKET, path)
    except Exception as e:
        # Hand the job straight back rather than leaving it leased to a worker
        # that has nowhere to put the result.
        render_queue.fail(job["id"], g.worker_id, f"could not sign upload: {e}")
        log.error("[worker] could not sign upload for job %s: %s", job["id"], e)
        return jsonify(error="upload_url_unavailable"), 503

    return jsonify(
        job_id=job["id"],
        format=job["format"],
        aspect_ratio=job["aspect_ratio"],
        prompt=job["prompt"],
        attempt=(job.get("attempts") or 0) + 1,
        upload_url=upload["url"],
        upload_path=path,
        heartbeat_seconds=int(render_queue.LEASE_TIMEOUT.total_seconds() // 4),
    )


@bp.post("/api/worker/jobs/<job_id>/heartbeat")
@require_worker
def worker_heartbeat(job_id: str):
    """Extend the lease. 409 tells the worker it lost the job and should stop."""
    if not render_queue.heartbeat(job_id, g.worker_id):
        return jsonify(error="lease_lost"), 409
    return jsonify(ok=True)


@bp.post("/api/worker/jobs/<job_id>/complete")
@require_worker
def worker_complete(job_id: str):
    body = request.get_json(silent=True) or {}
    path = (body.get("video_path") or "").strip()
    if not path:
        return jsonify(error="video_path required"), 400

    # The worker names the path in its own request, so it has to be the shape
    # we issued: <campaign_id>/<job_id>.mp4. Without this a worker could point a
    # job at any object in the bucket, including another tenant's video, and the
    # owner would be served it under a signed URL we minted ourselves.
    if ".." in path or path.count("/") != 1 or not path.endswith(f"/{job_id}.mp4"):
        log.warning("[worker] %s reported an unexpected path %r for job %s",
                    g.worker_id, path, job_id)
        return jsonify(error="unexpected_video_path"), 400

    done = render_queue.complete(job_id, g.worker_id, path)
    if not done:
        return jsonify(error="lease_lost"), 409
    return jsonify(ok=True)


@bp.post("/api/worker/jobs/<job_id>/fail")
@require_worker
def worker_fail(job_id: str):
    body = request.get_json(silent=True) or {}
    reason = (body.get("reason") or "unspecified worker failure").strip()
    if not render_queue.fail(job_id, g.worker_id, reason):
        return jsonify(error="lease_lost"), 409
    return jsonify(ok=True)


@bp.get("/api/worker/hello")
@require_worker
def worker_hello():
    """A worker's first call on boot, so a misconfigured box says so in its own
    logs rather than silently polling an endpoint that rejects it."""
    return jsonify(ok=True, worker_id=g.worker_id, run=str(uuid.uuid4())[:8])
