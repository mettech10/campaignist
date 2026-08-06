"""Background execution for a campaign generation.

Thin: the orchestration lives in orchestrator.py. This module owns only the
thread and the failure bookkeeping.

Concurrency: one background thread per generation. Deliberate for MVP — one
Render web service, low volume. When concurrent generations start competing for
the worker pool, move to a Render background worker with a Postgres job queue
rather than growing the thread pool.
"""
import logging
import threading

from .. import credits, supabase
from . import orchestrator

log = logging.getLogger(__name__)


def start(campaign_id: str, from_agent: str | None = None) -> None:
    """Fire-and-forget. Progress is read back via GET /api/campaigns/<id>."""
    threading.Thread(
        target=_run_guarded, args=(campaign_id, from_agent),
        name=f"campaign-{campaign_id[:8]}", daemon=True,
    ).start()


def _run_guarded(campaign_id: str, from_agent: str | None) -> None:
    try:
        if from_agent:
            _clear_from(campaign_id, from_agent)
        orchestrator.generate(campaign_id)
    except Exception as e:
        log.exception("[runner] campaign %s failed: %s", campaign_id, e)
        _fail(campaign_id, f"{type(e).__name__}: {e}")


def _clear_from(campaign_id: str, from_agent: str) -> None:
    """Regenerating from an agent discards it and everything downstream, since
    every later agent consumed the output being replaced."""
    from . import agents

    campaign = supabase.select("campaigns", params={"id": f"eq.{campaign_id}"}, single=True)
    if not campaign:
        return
    order = [a.id for a in agents.ordered()]
    if from_agent not in order:
        raise ValueError(f"unknown agent {from_agent!r}")
    keep = set(order[: order.index(from_agent)])
    outputs = {k: v for k, v in (campaign.get("pipeline_outputs") or {}).items() if k in keep}
    supabase.update("campaigns", {"pipeline_outputs": outputs},
                    params={"id": f"eq.{campaign_id}"}, returning=False)


def _fail(campaign_id: str, message: str) -> None:
    """Mark the campaign failed, and return the credit if it produced nothing.

    A generation that dies on its first agent — a provider 400, a bad key — has
    cost the user a credit for an empty campaign. The reaper covers workers that
    vanish; this covers failures that are reported properly, which are the more
    common case and just as unfair to charge for.

    A run that produced some agents' output keeps the charge: there is real work
    on the campaign, and regenerating from a later agent does not re-spend.
    """
    campaign = None
    try:
        campaign = supabase.update(
            "campaigns", {"status": "error", "error": message[:500]},
            params={"id": f"eq.{campaign_id}"},
        )
    except Exception:
        log.exception("[runner] campaign %s: could not record failure", campaign_id)
        return

    if not campaign or (campaign.get("pipeline_outputs") or {}):
        return

    try:
        business = supabase.select(
            "businesses",
            params={"id": f"eq.{campaign['business_id']}", "select": "user_id"},
            single=True,
        )
        if business:
            credits.refund(business["user_id"], "failed_generation", campaign_id)
            log.info("[runner] campaign %s produced nothing — credit returned", campaign_id)
    except Exception as e:
        # The campaign is correctly marked failed either way; a refund problem
        # must not turn into an unhandled error in a background thread.
        log.error("[runner] campaign %s refund failed: %s", campaign_id, e)
