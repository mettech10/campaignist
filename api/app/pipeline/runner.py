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

from .. import supabase
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
    try:
        supabase.update("campaigns", {"status": "error", "error": message[:500]},
                        params={"id": f"eq.{campaign_id}"}, returning=False)
    except Exception:
        log.exception("[runner] campaign %s: could not record failure", campaign_id)
