from urllib.parse import urlparse

from flask import Blueprint, jsonify

from ..config import Config
from .. import render_queue
from ..pipeline import llm, orchestrator, providers, reaper

bp = Blueprint("health", __name__)


@bp.get("/api/health")
def health():
    """Which integrations are wired up, and what each pipeline role resolves to."""
    roles = {}
    for role in ("strategy", "content"):
        try:
            resolved = llm.resolve_role(role)
            roles[role] = {
                "model": resolved.model,
                "provider": resolved.provider,
                "key_present": bool(_provider_key(resolved.provider)),
                # A model that ignores the schema loses calls outright rather
                # than degrading, so this is worth reading off a live deploy.
                "honours_schema": llm.MODELS[resolved.model].honours_schema,
            }
        except Exception as e:
            roles[role] = {"error": str(e)}

    return jsonify(
        status="ok",
        env=Config.ENV,
        models=roles,
        integrations={
            "supabase": bool(Config.SUPABASE_URL and Config.SUPABASE_SERVICE_KEY),
            "stripe": bool(Config.STRIPE_SECRET_KEY),
            "stripe_webhook": bool(Config.STRIPE_WEBHOOK_SECRET),
        },
        # A boolean cannot show that SUPABASE_URL points at the *wrong* project,
        # which presents as every valid token being rejected: JWKS returns a
        # different signing key, so signature verification fails. The host is
        # already public (the browser bundle carries it), so surfacing it costs
        # nothing and makes that misconfiguration self-diagnosing.
        supabase_host=urlparse(Config.SUPABASE_URL).netloc or None,
        # Which scheme incoming tokens will be verified under.
        jwt_verification="HS256 (shared secret)" if Config.SUPABASE_JWT_SECRET
        else "JWKS (asymmetric)",
        # A wrong CORS list presents to the user as "Failed to fetch" with no
        # server-side trace at all — the browser blocks the request before it
        # is sent. Showing the allowed origins makes that one lookup instead of
        # a guessing game. Origins are public by definition.
        cors_origins=Config.CORS_ORIGINS,
        # The knobs that decide how long a run can take and how hard it leans on
        # the provider. Both are tuned per instance size, and getting them wrong
        # shows up as campaigns reaped mid-flight rather than as an error, so
        # they are worth being able to read off a live deploy.
        pipeline={
            "max_inflight_calls": llm.MAX_INFLIGHT,
            "attempts_per_call": llm.RETRIES + 1,
            "attempts_after_timeout": llm.TIMEOUT_RETRIES + 1,
            "request_timeout_seconds": providers.REQUEST_TIMEOUT,
            "heartbeat_seconds": orchestrator.HEARTBEAT_EVERY.total_seconds(),
            "reaped_after_seconds": reaper.STALE_AFTER.total_seconds(),
        },
        # The GPU worker is a separate machine that can only be diagnosed from
        # here — if its token is unset the queue refuses it and the symptom is
        # simply that no video ever appears.
        renders={
            "worker_token_present": bool(Config.RENDER_WORKER_TOKEN),
            "lease_timeout_seconds": render_queue.LEASE_TIMEOUT.total_seconds(),
            "max_attempts": render_queue.MAX_ATTEMPTS,
            "video_formats": sorted(render_queue.VIDEO_FORMATS),
        },
    )


def _provider_key(provider: str) -> str:
    try:
        return Config.require_key(provider)
    except Exception:
        return ""
