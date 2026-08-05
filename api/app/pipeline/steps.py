"""The five pipeline steps.

Each step loads a prompt spec from app/pipeline/prompts/<id>.json (the BA Copilot
pattern — prompts are data, editable without touching Python), renders the user
template from the pipeline context, and makes one schema-constrained model call.

A spec's `model` field is a *role* ("strategy" or "content"), not a model id —
the id comes from config, so swapping models is an env change.
"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from .. import supabase
from . import formats
from .llm import call_json

log = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).parent / "prompts"


class StepNotImplemented(RuntimeError):
    pass


@dataclass
class PipelineContext:
    campaign: dict
    business: dict
    outputs: dict = field(default_factory=dict)   # {"1": {...}, "2": {...}}
    cost_gbp: float = 0.0
    memories: list = field(default_factory=list)  # top-k retrieved in step 1
    assets: list = field(default_factory=list)    # content_assets rows from step 4


@lru_cache(maxsize=None)
def load_prompt(prompt_id: str) -> dict:
    path = PROMPT_DIR / f"{prompt_id}.json"
    if not path.exists():
        raise StepNotImplemented(f"prompt spec missing: {path.name}")
    return json.loads(path.read_text())


def _render_vars(ctx: PipelineContext) -> dict:
    """Everything a prompt template may reference. Templates use a subset."""
    business = ctx.business or {}
    campaign = ctx.campaign or {}
    budget = campaign.get("budget")

    return {
        "name": business.get("name", ""),
        "industry": business.get("industry") or "not stated",
        "location": business.get("location") or "not stated",
        "offer_description": business.get("offer_description") or "not stated",
        "price_point": business.get("price_point") or "not stated",
        "website": business.get("website") or "not stated",
        "brand_voice": json.dumps(business.get("brand_voice") or {}, indent=2),
        "memories": _render_memories(ctx.memories),
        "goal": campaign.get("goal", ""),
        # £0 is a meaningful, supported answer — don't let it read as "unset".
        "budget": f"£{budget}" if budget is not None else "not stated",
        "timeframe_days": campaign.get("timeframe_days") or 30,
        "format_catalogue": formats.catalogue_for_prompt(),
        "step1": json.dumps(ctx.outputs.get("1", {}), indent=2),
        "step2": json.dumps(ctx.outputs.get("2", {}), indent=2),
        "step3": json.dumps(ctx.outputs.get("3", {}), indent=2),
        "step4": json.dumps(ctx.outputs.get("4", {}), indent=2),
    }


def _render_memories(memories: list) -> str:
    if not memories:
        return "None yet — this is their first campaign with us."
    return "\n".join(f"- ({m.get('kind', 'fact')}) {m.get('content', '')}" for m in memories)


def call_prompt(
    prompt_id: str,
    ctx: PipelineContext,
    *,
    extra_vars: dict | None = None,
    model_key: str | None = None,
) -> tuple[dict, float]:
    """Render a prompt spec and call the model. Returns (output, cost).

    Does not mutate ctx — step 4 calls this from worker threads, where
    accumulating onto a shared float would race.
    """
    spec = load_prompt(prompt_id)
    role = model_key or spec.get("model", "strategy")
    variables = _render_vars(ctx) | (extra_vars or {})

    try:
        user = spec["user_template"].format(**variables)
    except KeyError as e:
        raise StepNotImplemented(f"{prompt_id}: template references unknown variable {e}") from e

    output, cost, resolved = call_json(
        role=role,
        system=spec["system"],
        user=user,
        schema=spec["schema"],
        schema_name=spec["id"],
    )
    log.info("[pipeline] %s on %s/%s — £%.4f",
             prompt_id, resolved.provider, resolved.model, cost)
    return output, cost


def run_prompt_step(prompt_id: str, ctx: PipelineContext) -> dict:
    """Single-call step: render, call, accumulate cost onto the context."""
    output, cost = call_prompt(prompt_id, ctx)
    ctx.cost_gbp += cost
    return output


# ── Step 1 ──────────────────────────────────────────────────────────────────
def step_1_business_understanding(ctx: PipelineContext) -> dict:
    """Retrieve past learnings, then establish what this business actually is.

    Memory retrieval is what makes campaign 2 better than campaign 1 — the
    retention mechanic from BLUEPRINT §4. Retrieval lands in Week 5 alongside
    the embedding decision; until then the step runs with no memories, which is
    exactly the cold-start path a first campaign takes anyway.
    """
    ctx.memories = []
    return run_prompt_step("step1_business_understanding", ctx)


def step_2_audience_positioning(ctx: PipelineContext) -> dict:
    return run_prompt_step("step2_audience_positioning", ctx)


def step_3_funnel_channel(ctx: PipelineContext) -> dict:
    output = run_prompt_step("step3_funnel_channel", ctx)

    # The schema can't express "these strings must be catalogue ids" or "these
    # percentages sum to 100", so check both here rather than letting a bad
    # format id reach step 4 and produce an asset the UI can't label.
    for stage in output.get("funnel_stages", []):
        unknown = [f for f in stage.get("formats", []) if f not in formats.FORMATS]
        if unknown:
            log.warning(
                "[pipeline] step 3 chose unknown format ids %s in stage %r — dropping",
                unknown, stage.get("stage"),
            )
            stage["formats"] = [f for f in stage["formats"] if f in formats.FORMATS]

    total = sum(b.get("percent", 0) for b in output.get("budget_split", []))
    if output.get("budget_split") and total != 100:
        log.warning("[pipeline] step 3 budget_split sums to %s, not 100", total)

    return output


# ── Step 4 — content fan-out ────────────────────────────────────────────────
VARIANTS_PER_FORMAT = 2
MAX_FANOUT_WORKERS = 6


def step_4_content_generation(ctx: PipelineContext) -> dict:
    """Fan out over (funnel stage × chosen format), then write content_assets.

    Model routing follows BLUEPRINT §4's cheap-for-short/strong-for-long split:
    written formats (email sequences, landing copy) go to the strategy model,
    everything else to the cheaper content model. Routing is by the format's
    category rather than a hardcoded list, so a new written format inherits the
    strategy path automatically.

    Calls run in parallel — this is what keeps the full pipeline under the 90s
    target, since each leg is an independent network round trip.
    """
    stages = ctx.outputs.get("3", {}).get("funnel_stages", [])
    if not stages:
        raise StepNotImplemented("step 4: step 3 produced no funnel stages")

    jobs = []
    for stage in stages:
        for format_id in stage.get("formats", []):
            spec = formats.FORMATS.get(format_id)
            if not spec:
                continue  # step 3 already filters these; belt and braces
            jobs.append((stage, format_id, spec))

    if not jobs:
        raise StepNotImplemented("step 4: step 3 chose no creative formats")

    log.info("[pipeline] step 4 fanning out over %d (stage × format) jobs", len(jobs))

    def run_job(job):
        stage, format_id, spec = job
        extra = {
            "stage": json.dumps(stage, indent=2),
            "format_id": format_id,
            "format_label": spec["label"],
            "format_aspect": spec["aspect_ratio"],
            "format_guidance": spec["brief_guidance"],
            "variant_count": VARIANTS_PER_FORMAT,
        }
        # Long-form lives in the "text" category; everything else is short copy.
        model_key = "strategy" if spec["category"] == "text" else "content"
        output, cost = call_prompt(
            "step4_content_generation", ctx, extra_vars=extra, model_key=model_key
        )
        return format_id, output.get("assets", []), cost

    results, total_cost, failures = [], 0.0, []
    with ThreadPoolExecutor(max_workers=MAX_FANOUT_WORKERS) as pool:
        futures = {pool.submit(run_job, job): job for job in jobs}
        for future in as_completed(futures):
            stage, format_id, _ = futures[future]
            try:
                fid, assets, cost = future.result()
            except Exception as e:
                # One bad leg shouldn't lose the other eleven.
                log.warning("[pipeline] step 4 leg %s/%s failed: %s",
                            stage.get("stage"), format_id, e)
                failures.append(f"{stage.get('stage')}/{format_id}: {e}")
                continue
            results.append((fid, assets))
            total_cost += cost

    ctx.cost_gbp += total_cost

    if not results:
        raise StepNotImplemented(
            f"step 4: every content leg failed ({len(failures)} of {len(jobs)})"
        )

    rows = []
    for format_id, assets in results:
        for index, asset in enumerate(assets, start=1):
            rows.append({
                "campaign_id": ctx.campaign["id"],
                "type": asset["type"],
                "channel": asset.get("channel"),
                "format": format_id,
                "variant": index,
                "content": {
                    "hook": asset["hook"],
                    "body": asset["body"],
                    "cta": asset["cta"],
                    "hashtags": asset.get("hashtags", []),
                },
                "image_brief": asset.get("image_brief"),
                "status": "generated",
            })

    # Regenerating step 4 replaces its assets rather than duplicating them.
    supabase.delete("content_assets", params={"campaign_id": f"eq.{ctx.campaign['id']}"})
    saved = supabase.insert("content_assets", rows) if rows else []
    ctx.assets = saved if isinstance(saved, list) else [saved]

    log.info("[pipeline] step 4 wrote %d assets (%d legs failed)",
             len(ctx.assets), len(failures))

    return {
        "asset_count": len(ctx.assets),
        "formats_used": sorted({fid for fid, _ in results}),
        "failed_legs": failures,
    }


# ── Step 5 — assembly and calendar ──────────────────────────────────────────
def step_5_campaign_assembly(ctx: PipelineContext) -> dict:
    """Assemble the campaign and schedule the assets step 4 actually created."""
    if not ctx.assets:
        ctx.assets = supabase.select(
            "content_assets",
            params={
                "campaign_id": f"eq.{ctx.campaign['id']}",
                "select": "id,type,channel,format,variant,content",
            },
        )
    if not ctx.assets:
        raise StepNotImplemented("step 5: no assets to schedule")

    start = datetime.now(timezone.utc).date()
    output, cost = call_prompt(
        "step5_campaign_assembly",
        ctx,
        extra_vars={
            "asset_index": _render_asset_index(ctx.assets),
            "start_date": start.strftime("%a %-d %b %Y"),
        },
    )
    ctx.cost_gbp += cost

    # The model is told to use real ids, but a scheduled entry pointing at an
    # asset that doesn't exist would render as a dead link in the calendar UI.
    valid_ids = {a["id"] for a in ctx.assets}
    calendar = output.get("30_day_calendar", [])
    kept = [e for e in calendar if e.get("asset_id") in valid_ids]
    if len(kept) != len(calendar):
        log.warning("[pipeline] step 5 dropped %d calendar entries with unknown asset_ids",
                    len(calendar) - len(kept))
        output["30_day_calendar"] = kept

    return output


def regenerate_asset(asset: dict) -> dict:
    """Rewrite one asset in place — a single cheap call, no credit charged.

    Runs synchronously: it is a single model call, so the editor can await it rather
    than poll. Reuses the step 4 prompt so a regenerated asset is written to the
    same contract as the original.
    """
    campaign = supabase.select(
        "campaigns", params={"id": f"eq.{asset['campaign_id']}"}, single=True
    )
    if not campaign:
        raise StepNotImplemented("regenerate: campaign not found")
    business = supabase.select(
        "businesses", params={"id": f"eq.{campaign['business_id']}"}, single=True
    )

    format_id = asset.get("format")
    spec = formats.FORMATS.get(format_id)
    if not spec:
        raise StepNotImplemented(f"regenerate: unknown format {format_id!r}")

    outputs = campaign.get("pipeline_outputs") or {}
    stage = next(
        (s for s in outputs.get("3", {}).get("funnel_stages", [])
         if format_id in s.get("formats", [])),
        {"stage": "unknown", "objective": "", "channel": asset.get("channel")},
    )

    ctx = PipelineContext(campaign=campaign, business=business or {}, outputs=outputs)
    output, cost = call_prompt(
        "step4_content_generation", ctx,
        extra_vars={
            "stage": json.dumps(stage, indent=2),
            "format_id": format_id,
            "format_label": spec["label"],
            "format_aspect": spec["aspect_ratio"],
            "format_guidance": spec["brief_guidance"],
            "variant_count": 1,
        },
        model_key="strategy" if spec["category"] == "text" else "content",
    )

    assets = output.get("assets") or []
    if not assets:
        raise StepNotImplemented("regenerate: model returned no asset")
    new = assets[0]

    updated = supabase.update(
        "content_assets",
        {
            "content": {
                "hook": new["hook"], "body": new["body"],
                "cta": new["cta"], "hashtags": new.get("hashtags", []),
            },
            "image_brief": new.get("image_brief"),
            # A regenerated asset is a fresh AI draft, so any prior user edit no
            # longer applies and the approval it may have carried is void.
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


def _render_asset_index(assets: list) -> str:
    lines = []
    for a in assets:
        content = a.get("content") or {}
        hook = (content.get("hook") or "").replace("\n", " ")[:80]
        lines.append(
            f"- {a['id']} | {a.get('type')} | {a.get('channel')} | "
            f"{a.get('format')} | v{a.get('variant')} | \"{hook}\""
        )
    return "\n".join(lines)


STEPS = {
    1: step_1_business_understanding,
    2: step_2_audience_positioning,
    3: step_3_funnel_channel,
    4: step_4_content_generation,
    5: step_5_campaign_assembly,
}
