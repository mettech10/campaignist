"""Campaign Director — the orchestrator from CLAUDE.md §4.

Holds campaign state, calls specialist agents in dependency order, and assembles
their outputs into one campaign object.

The order is not hardcoded. `agents.stages()` derives it from each spec's
`stage` and `depends_on`, so adding an agent is a JSON file rather than an edit
here. Everything within a stage is independent by construction, so a stage runs
concurrently — that is what stops Copy/Social, Email, SEO and GEO queueing
behind one another for four sequential model calls.

Each agent's output is persisted under its own id the moment it lands, which is
what the generation view counts to show progress.
"""
from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .. import supabase
from . import agents, formats
from .agents import AgentError, AgentSpec

log = logging.getLogger(__name__)

VARIANTS_PER_FORMAT = 2
MAX_WORKERS = 6

# How often the run tells the reaper it is still alive, and the point past which
# it stops claiming so. The interval must stay comfortably under
# reaper.STALE_AFTER; the deadline is what stops a wedged-but-breathing worker
# from holding a campaign open forever, since a heartbeat proves the process is
# up, not that the work is progressing.
HEARTBEAT_EVERY = timedelta(seconds=45)
HEARTBEAT_DEADLINE = timedelta(minutes=25)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _heartbeat(campaign_id: str):
    """Keep `progress_at` fresh for as long as this block runs.

    Stage 3 runs four agents at once and writes nothing until all four land, so
    without this the heartbeat goes silent for the whole stage and the reaper
    reclaims a campaign that is working perfectly. That is not hypothetical — it
    is what killed the run measured at 1377s.

    The thread is a daemon in the worker's own process, so if the worker dies
    the beating stops with it and the reaper still does its job.
    """
    stop = threading.Event()
    started = datetime.now(timezone.utc)

    def beat() -> None:
        while not stop.wait(HEARTBEAT_EVERY.total_seconds()):
            if datetime.now(timezone.utc) - started > HEARTBEAT_DEADLINE:
                log.error("[orchestrator] campaign %s past %s — no longer claiming "
                          "liveness, leaving it to the reaper",
                          campaign_id, HEARTBEAT_DEADLINE)
                return
            try:
                supabase.update(
                    "campaigns", {"progress_at": _now()},
                    # Never resurrect a campaign that already failed or finished.
                    params={"id": f"eq.{campaign_id}", "status": "eq.generating"},
                    returning=False,
                )
            except Exception as e:
                log.warning("[orchestrator] heartbeat for %s failed: %s", campaign_id, e)

    thread = threading.Thread(target=beat, daemon=True, name=f"beat-{campaign_id[:8]}")
    thread.start()
    try:
        yield
    finally:
        stop.set()


@dataclass
class CampaignContext:
    campaign: dict
    business: dict
    outputs: dict = field(default_factory=dict)   # agent_id -> output
    cost_gbp: float = 0.0
    memories: list = field(default_factory=list)
    assets: list = field(default_factory=list)


def base_variables(ctx: CampaignContext) -> dict:
    """Everything an agent template may reference. Agent outputs are exposed
    under their own id, so a spec says {research} or {strategy}."""
    business = ctx.business or {}
    campaign = ctx.campaign or {}
    budget = campaign.get("budget")

    variables = {
        "name": business.get("name", ""),
        "industry": business.get("industry") or "not stated",
        "location": business.get("location") or "not stated",
        "offer_description": business.get("offer_description") or "not stated",
        "price_point": business.get("price_point") or "not stated",
        "website": business.get("website") or "not stated",
        "brand_voice": json.dumps(business.get("brand_voice") or {}, indent=2),
        "memories": _render_memories(ctx.memories),
        "goal": campaign.get("goal", ""),
        # £0 is a meaningful answer — don't let it read as "unset".
        "budget": f"£{budget}" if budget is not None else "not stated",
        "timeframe_days": campaign.get("timeframe_days") or 30,
        "format_catalogue": formats.catalogue_for_prompt(),
        "start_date": datetime.now(timezone.utc).date().strftime("%a %-d %b %Y"),
        "asset_index": _render_asset_index(ctx.assets),
    }
    for agent_id in agents.registry():
        variables[agent_id] = json.dumps(ctx.outputs.get(agent_id, {}), indent=2)
    return variables


def _render_memories(memories: list) -> str:
    if not memories:
        return "None yet — this is their first campaign with us."
    return "\n".join(f"- ({m.get('kind','fact')}) {m.get('content','')}" for m in memories)


def _render_asset_index(assets: list) -> str:
    if not assets:
        return "None yet."
    lines = []
    for a in assets:
        content = a.get("content") or {}
        hook = (content.get("hook") or "").replace("\n", " ")[:80]
        lines.append(
            f"- {a['id']} | {a.get('type')} | {a.get('channel')} | "
            f"{a.get('format')} | v{a.get('variant')} | \"{hook}\""
        )
    return "\n".join(lines)


# ── running one agent ───────────────────────────────────────────────────────
def run_agent(spec: AgentSpec, ctx: CampaignContext) -> tuple[dict, float, list]:
    """Returns (output, cost, asset_rows). Pure with respect to ctx."""
    if spec.fan_out == "per_format":
        return _run_per_format(spec, ctx)

    output, cost = agents.run(spec, base_variables(ctx))
    rows = _assets_from(spec, output, ctx) if spec.produces_assets else []
    return output, cost, rows


def _run_per_format(spec: AgentSpec, ctx: CampaignContext) -> tuple[dict, float, list]:
    """Fan out one call per (funnel stage × chosen format).

    The only execution mode beyond a single call. It exists because creative
    formats are genuinely different writing jobs — one call asked to produce
    twelve assets across six idioms produces twelve mediocre ones.
    """
    strategy = ctx.outputs.get("strategy") or {}
    jobs = [
        (stage, fid, formats.FORMATS[fid])
        for stage in strategy.get("funnel_stages", [])
        for fid in stage.get("formats", [])
        if fid in formats.FORMATS
        # Written formats belong to the Email and SEO agents, not here.
        and formats.FORMATS[fid]["category"] != "text"
    ]
    if not jobs:
        raise AgentError(f"{spec.id}: strategy chose no creative formats")

    log.info("[orchestrator] %s fanning out over %d format jobs", spec.id, len(jobs))
    base = base_variables(ctx)

    def one(job):
        stage, fid, fmt = job
        variables = base | {
            "stage": json.dumps(stage, indent=2),
            "format_id": fid,
            "format_label": fmt["label"],
            "format_aspect": fmt["aspect_ratio"],
            "format_guidance": fmt["brief_guidance"],
            "variant_count": VARIANTS_PER_FORMAT,
        }
        output, cost = agents.run(spec, variables)
        return fid, output.get("assets", []), cost

    rows, total, failures = [], 0.0, []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(one, j): j for j in jobs}
        for future in as_completed(futures):
            _stage, fid, _fmt = futures[future]
            try:
                fid, produced, cost = future.result()
            except Exception as e:
                log.warning("[orchestrator] %s leg %s failed: %s", spec.id, fid, e)
                failures.append(f"{fid}: {e}")
                continue
            total += cost
            for i, a in enumerate(produced, start=1):
                rows.append({
                    "campaign_id": ctx.campaign["id"],
                    "type": a["type"], "channel": a.get("channel"), "format": fid,
                    "variant": i,
                    "content": {"hook": a["hook"], "body": a["body"], "cta": a["cta"],
                                "hashtags": a.get("hashtags", [])},
                    "image_brief": a.get("image_brief"), "status": "generated",
                })

    if not rows:
        raise AgentError(f"{spec.id}: every format leg failed ({len(failures)} of {len(jobs)})")
    return {"asset_count": len(rows), "formats_used": sorted({r["format"] for r in rows}),
            "failed_legs": failures}, total, rows


def _assets_from(spec: AgentSpec, output: dict, ctx: CampaignContext) -> list:
    """Turn an agent's structured output into content_assets rows."""
    if spec.id != "email":
        return []
    return [{
        "campaign_id": ctx.campaign["id"],
        "type": "email", "channel": "Email", "format": "long-form-email",
        "variant": e.get("position", i + 1),
        "content": {
            "hook": e["subject"], "body": e["body"], "cta": e["cta"],
            "hashtags": [],
        },
        "image_brief": (
            f"Subject variant to test: {e['subject_variant']}. "
            f"Preview text: {e['preview_text']}. "
            f"Send {e['send_offset_days']} day(s) after the previous step. "
            f"Handles the objection: {e['objection_addressed']}."
        ),
        "status": "generated",
    } for i, e in enumerate(output.get("emails", []))]


# ── the run ─────────────────────────────────────────────────────────────────
def generate(campaign_id: str) -> None:
    """Run every agent for a campaign, persisting as we go."""
    campaign = supabase.select("campaigns", params={"id": f"eq.{campaign_id}"}, single=True)
    if not campaign:
        raise AgentError(f"campaign {campaign_id} not found")
    business = supabase.select(
        "businesses", params={"id": f"eq.{campaign['business_id']}"}, single=True
    )

    ctx = CampaignContext(
        campaign=campaign, business=business or {},
        outputs=dict(campaign.get("pipeline_outputs") or {}),
    )

    # Regenerating clears the assets a previous run wrote, so a re-run replaces
    # rather than duplicating.
    supabase.delete("content_assets", params={"campaign_id": f"eq.{campaign_id}"})

    def land(spec: AgentSpec, output: dict, cost: float, rows: list) -> None:
        """Record one agent's result and write it straight through.

        Persisting per agent rather than per stage is what makes the generation
        view count up during stage 3 instead of sitting still for four agents,
        and it means a worker that dies mid-stage keeps the agents that had
        already finished.
        """
        ctx.outputs[spec.id] = output
        ctx.cost_gbp += cost
        if rows:
            saved = supabase.insert("content_assets", rows)
            ctx.assets.extend(saved if isinstance(saved, list) else [saved])
        supabase.update(
            "campaigns",
            {"pipeline_outputs": ctx.outputs, "cost_gbp": round(ctx.cost_gbp, 4),
             "progress_at": _now()},
            params={"id": f"eq.{campaign_id}"}, returning=False,
        )

    with _heartbeat(campaign_id):
        for stage in agents.stages():
            pending = [s for s in stage if s.id not in ctx.outputs]
            if not pending:
                continue

            if len(pending) == 1:
                spec = pending[0]
                land(spec, *run_agent(spec, ctx))
                continue

            log.info("[orchestrator] stage %s running %d agents concurrently: %s",
                     pending[0].stage, len(pending), ", ".join(s.id for s in pending))
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                futures = {pool.submit(run_agent, s, ctx): s for s in pending}
                for future in as_completed(futures):
                    spec = futures[future]
                    # One channel agent failing must not lose the other three.
                    try:
                        output, cost, rows = future.result()
                    except Exception as e:
                        log.warning("[orchestrator] agent %s failed: %s", spec.id, e)
                        ctx.outputs[spec.id] = {"error": str(e)[:400]}
                        continue
                    land(spec, output, cost, rows)

    assembly = ctx.outputs.get("assembly") or {}
    calendar = assembly.get("30_day_calendar")
    if calendar:
        # The model is told to use real ids, but a scheduled entry pointing at a
        # missing asset renders as a dead link in the calendar UI.
        valid = {a["id"] for a in ctx.assets}
        kept = [e for e in calendar if e.get("asset_id") in valid]
        if len(kept) != len(calendar):
            log.warning("[orchestrator] dropped %d calendar entries with unknown asset_ids",
                        len(calendar) - len(kept))
            assembly["30_day_calendar"] = kept
            ctx.outputs["assembly"] = assembly
        calendar = kept

    supabase.update(
        "campaigns",
        {"status": "ready", "pipeline_outputs": ctx.outputs, "calendar": calendar,
         "cost_gbp": round(ctx.cost_gbp, 4), "error": None,
         "progress_at": _now()},
        params={"id": f"eq.{campaign_id}"}, returning=False,
    )
    log.info("[orchestrator] campaign %s complete — £%.4f, %d assets",
             campaign_id, ctx.cost_gbp, len(ctx.assets))


def regenerate_asset(asset: dict) -> dict:
    """Rewrite one asset in place — a single cheap call, no credit charged.

    Runs synchronously: one model call, so the editor can await it rather than
    poll. Reuses the copy_social agent so a regenerated asset is written to the
    same contract as the original.
    """
    campaign = supabase.select(
        "campaigns", params={"id": f"eq.{asset['campaign_id']}"}, single=True
    )
    if not campaign:
        raise AgentError("regenerate: campaign not found")
    business = supabase.select(
        "businesses", params={"id": f"eq.{campaign['business_id']}"}, single=True
    )

    format_id = asset.get("format")
    fmt = formats.FORMATS.get(format_id)
    if not fmt:
        raise AgentError(f"regenerate: unknown format {format_id!r}")

    ctx = CampaignContext(
        campaign=campaign, business=business or {},
        outputs=dict(campaign.get("pipeline_outputs") or {}),
    )
    strategy = ctx.outputs.get("strategy") or {}
    stage = next(
        (s for s in strategy.get("funnel_stages", []) if format_id in s.get("formats", [])),
        {"stage": "unknown", "objective": "", "channel": asset.get("channel")},
    )

    spec = agents.registry()["copy_social"]
    output, cost = agents.run(spec, base_variables(ctx) | {
        "stage": json.dumps(stage, indent=2),
        "format_id": format_id,
        "format_label": fmt["label"],
        "format_aspect": fmt["aspect_ratio"],
        "format_guidance": fmt["brief_guidance"],
        "variant_count": 1,
    })

    produced = output.get("assets") or []
    if not produced:
        raise AgentError("regenerate: model returned no asset")
    new = produced[0]

    updated = supabase.update(
        "content_assets",
        {
            "content": {"hook": new["hook"], "body": new["body"], "cta": new["cta"],
                        "hashtags": new.get("hashtags", [])},
            "image_brief": new.get("image_brief"),
            # A regenerated asset is a fresh draft, so any prior user edit no
            # longer applies and the approval it carried is void.
            "edited_content": None,
            "status": "generated",
        },
        params={"id": f"eq.{asset['id']}"},
    )
    supabase.update(
        "campaigns",
        {"cost_gbp": round(float(campaign.get("cost_gbp") or 0) + cost, 4)},
        params={"id": f"eq.{campaign['id']}"}, returning=False,
    )
    return updated
