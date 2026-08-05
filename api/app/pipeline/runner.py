"""Campaign pipeline orchestration (BLUEPRINT §4).

Five sequenced model calls, each with a strict JSON output contract. Every
step's output is persisted to campaigns.pipeline_outputs the moment it lands, so
the frontend's polling GET can reveal progress step-by-step.

Week 1 ships the runner, persistence, and cost logging. The step implementations
land in Week 2 (steps 1-3) and Week 3 (steps 4-5).

Concurrency: a background thread per generation. That is deliberate for MVP —
one Render web service, generations take <90s, and volume is low. When
concurrent generations start competing for the worker pool, move this to a
Render background worker with a Postgres job queue rather than growing the
thread pool.
"""
import logging
import threading

from .. import supabase
from .steps import STEPS, PipelineContext, StepNotImplemented

log = logging.getLogger(__name__)


def start(campaign_id: str, from_step: int = 1) -> None:
    """Fire-and-forget. Status and outputs are read back via GET /api/campaigns/<id>."""
    thread = threading.Thread(
        target=_run_guarded, args=(campaign_id, from_step),
        name=f"pipeline-{campaign_id[:8]}", daemon=True,
    )
    thread.start()


def _run_guarded(campaign_id: str, from_step: int) -> None:
    try:
        _run(campaign_id, from_step)
    except StepNotImplemented as e:
        log.warning("[pipeline %s] %s", campaign_id, e)
        _fail(campaign_id, str(e))
    except Exception as e:
        log.exception("[pipeline %s] failed: %s", campaign_id, e)
        _fail(campaign_id, f"{type(e).__name__}: {e}")


def _run(campaign_id: str, from_step: int) -> None:
    campaign = supabase.select(
        "campaigns", params={"id": f"eq.{campaign_id}"}, single=True
    )
    if not campaign:
        raise RuntimeError(f"campaign {campaign_id} not found")

    business = supabase.select(
        "businesses", params={"id": f"eq.{campaign['business_id']}"}, single=True
    )

    ctx = PipelineContext(
        campaign=campaign,
        business=business,
        # Regenerating from step N keeps steps 1..N-1 and discards the rest,
        # since every later step consumed the output being replaced.
        outputs={
            k: v for k, v in (campaign.get("pipeline_outputs") or {}).items()
            if int(k) < from_step
        },
        cost_gbp=0.0,
    )

    for step_no in range(from_step, 6):
        log.info("[pipeline %s] step %s starting", campaign_id, step_no)
        output = STEPS[step_no](ctx)
        ctx.outputs[str(step_no)] = output

        # Persist after each step — this is what makes the progress UI possible
        # and what lets a single step be regenerated later.
        supabase.update(
            "campaigns",
            {"pipeline_outputs": ctx.outputs, "cost_gbp": round(ctx.cost_gbp, 4)},
            params={"id": f"eq.{campaign_id}"},
            returning=False,
        )
        log.info("[pipeline %s] step %s done (£%.4f so far)",
                 campaign_id, step_no, ctx.cost_gbp)

    supabase.update(
        "campaigns",
        {
            "status": "ready",
            "calendar": ctx.outputs.get("5", {}).get("30_day_calendar"),
            "cost_gbp": round(ctx.cost_gbp, 4),
            "error": None,
        },
        params={"id": f"eq.{campaign_id}"},
        returning=False,
    )
    log.info("[pipeline %s] complete — total £%.4f", campaign_id, ctx.cost_gbp)


def _fail(campaign_id: str, message: str) -> None:
    try:
        supabase.update(
            "campaigns", {"status": "error", "error": message[:500]},
            params={"id": f"eq.{campaign_id}"}, returning=False,
        )
    except Exception:
        log.exception("[pipeline %s] could not record failure", campaign_id)
