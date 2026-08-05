#!/usr/bin/env python
"""Run the golden set through the pipeline and report cost, latency and rule hits.

This is the blueprint's §11 mitigation ("golden-set of 10 test businesses scored
weekly") and the instrument for deciding which provider to use. It runs the real
prompts against the real API but touches no database — step 4 generates content
without persisting, and step 5 gets a synthetic asset index.

    # whatever api/.env is configured with
    ./.venv/bin/python evals/run_eval.py

    # a specific pairing, for a bake-off
    MODEL_STRATEGY=kimi-k3 MODEL_CONTENT=kimi-k2.5 \\
        ./.venv/bin/python evals/run_eval.py --label kimi

    MODEL_STRATEGY=claude-sonnet-5 MODEL_CONTENT=claude-haiku-4-5 \\
        ./.venv/bin/python evals/run_eval.py --label claude

    # one business, for a fast loop while editing a prompt
    ./.venv/bin/python evals/run_eval.py --only bakery --steps 3

Outputs land in evals/runs/<label>/<business-id>.json so two runs can be diffed
or read side by side. Automated checks catch broken references and fabricated
claims; judging whether the copy is any *good* is still yours.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import checks  # noqa: E402
from app.pipeline import formats, steps  # noqa: E402
from app.pipeline.llm import resolve_role  # noqa: E402

HERE = Path(__file__).resolve().parent
GOLDEN = json.loads((HERE / "golden_set.json").read_text())


def run_business(case: dict, max_step: int) -> dict:
    """Steps 1..max_step for one business. No database involved."""
    ctx = steps.PipelineContext(
        campaign={"id": f"eval-{case['id']}", **case["campaign"]},
        business=case["business"],
    )
    result = {
        "id": case["id"],
        "note": case.get("note", ""),
        "goal": case["campaign"]["goal"],
        "budget": case["campaign"]["budget"],
        "timeframe_days": case["campaign"]["timeframe_days"],
        "steps": {},
        "timings": {},
        "error": None,
    }

    try:
        for step_no, fn in ((1, steps.step_1_business_understanding),
                            (2, steps.step_2_audience_positioning),
                            (3, steps.step_3_funnel_channel)):
            if step_no > max_step:
                break
            t0 = time.monotonic()
            ctx.outputs[str(step_no)] = fn(ctx)
            result["timings"][f"step{step_no}"] = round(time.monotonic() - t0, 1)

        if max_step >= 4:
            t0 = time.monotonic()
            assets = _generate_assets_without_db(ctx)
            result["timings"]["step4"] = round(time.monotonic() - t0, 1)
            result["assets"] = assets
            ctx.assets = assets
            ctx.outputs["4"] = {"asset_count": len(assets)}

        if max_step >= 5:
            t0 = time.monotonic()
            ctx.outputs["5"] = steps.step_5_campaign_assembly(ctx)
            result["timings"]["step5"] = round(time.monotonic() - t0, 1)

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    result["steps"] = ctx.outputs
    result["cost_gbp"] = round(ctx.cost_gbp, 4)
    result["total_seconds"] = round(sum(result["timings"].values()), 1)
    result["report"] = _score(case, ctx, result)
    return result


def _generate_assets_without_db(ctx: steps.PipelineContext) -> list[dict]:
    """Step 4's fan-out, minus the Supabase write, with synthetic asset ids."""
    jobs = [
        (stage, fid, formats.FORMATS[fid])
        for stage in ctx.outputs["3"].get("funnel_stages", [])
        for fid in stage.get("formats", [])
        if fid in formats.FORMATS
    ]

    def run(job):
        stage, fid, spec = job
        output, cost = steps.call_prompt(
            "step4_content_generation", ctx,
            extra_vars={
                "stage": json.dumps(stage, indent=2),
                "format_id": fid,
                "format_label": spec["label"],
                "format_aspect": spec["aspect_ratio"],
                "format_guidance": spec["brief_guidance"],
                "variant_count": steps.VARIANTS_PER_FORMAT,
            },
            model_key="strategy" if spec["category"] == "text" else "content",
        )
        return fid, output.get("assets", []), cost

    assets, total_cost = [], 0.0
    with ThreadPoolExecutor(max_workers=steps.MAX_FANOUT_WORKERS) as pool:
        for future in as_completed([pool.submit(run, j) for j in jobs]):
            try:
                fid, produced, cost = future.result()
            except Exception as e:
                print(f"    ! content leg failed: {e}", file=sys.stderr)
                continue
            total_cost += cost
            for i, a in enumerate(produced, start=1):
                assets.append({
                    "id": f"{fid}-v{i}",
                    "format": fid,
                    "type": a.get("type"),
                    "channel": a.get("channel"),
                    "variant": i,
                    "content": {
                        "hook": a.get("hook"), "body": a.get("body"),
                        "cta": a.get("cta"), "hashtags": a.get("hashtags", []),
                    },
                    "image_brief": a.get("image_brief"),
                })
    ctx.cost_gbp += total_cost
    return assets


def _score(case: dict, ctx: steps.PipelineContext, result: dict) -> dict:
    report = checks.Report()
    outputs = ctx.outputs

    checks.check_text_hygiene(outputs, report)
    if "3" in outputs:
        checks.check_step3(outputs["3"], formats.FORMATS, report)
        checks.check_budget_honesty(outputs["3"], case["campaign"]["budget"], report)
        checks.check_voice_gating(outputs["3"], case["business"].get("brand_voice", {}), report)
    if result.get("assets"):
        checks.check_assets(result["assets"], report)
        checks.check_text_hygiene({"assets": result["assets"]}, report)
    if "5" in outputs:
        checks.check_step5(
            outputs["5"], case["campaign"]["timeframe_days"],
            {a["id"] for a in result.get("assets", [])}, report,
        )

    return {
        "failures": [f"{f.rule}: {f.detail}" for f in report.failures],
        "warnings": [f"{f.rule}: {f.detail}" for f in report.warnings],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default=None, help="output directory name (default: the models)")
    parser.add_argument("--only", default=None, help="run a single business by id")
    parser.add_argument("--steps", type=int, default=5, help="highest step to run (1-5)")
    parser.add_argument("--workers", type=int, default=3, help="businesses in parallel")
    args = parser.parse_args()

    try:
        strategy, content = resolve_role("strategy"), resolve_role("content")
    except Exception as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    label = args.label or f"{strategy.model}__{content.model}".replace("/", "-")
    out_dir = HERE / "runs" / label
    out_dir.mkdir(parents=True, exist_ok=True)

    cases = GOLDEN["businesses"]
    if args.only:
        cases = [c for c in cases if c["id"] == args.only]
        if not cases:
            print(f"no business with id {args.only!r}", file=sys.stderr)
            return 2

    print(f"strategy: {strategy.provider}/{strategy.model}")
    print(f"content:  {content.provider}/{content.model}")
    print(f"running {len(cases)} business(es) through steps 1-{args.steps} -> {out_dir}\n")

    started = time.monotonic()
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_business, c, args.steps): c for c in cases}
        for future in as_completed(futures):
            case = futures[future]
            try:
                result = future.result()
            except Exception as e:
                print(f"  {case['id']:24} CRASHED  {type(e).__name__}: {e}")
                continue
            results.append(result)
            (out_dir / f"{case['id']}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))

            fails = len(result["report"]["failures"])
            warns = len(result["report"]["warnings"])
            status = "ERROR" if result["error"] else ("FAIL" if fails else "ok")
            print(
                f"  {case['id']:24} {status:6} "
                f"{result['total_seconds']:6.1f}s  £{result['cost_gbp']:.4f}  "
                f"{fails} fail / {warns} warn"
            )
            if result["error"]:
                print(f"      {result['error']}")
            for f in result["report"]["failures"][:3]:
                print(f"      FAIL {f}")

    if not results:
        return 1

    ok = [r for r in results if not r["error"]]
    total_cost = sum(r["cost_gbp"] for r in results)
    summary = {
        "label": label,
        "strategy_model": f"{strategy.provider}/{strategy.model}",
        "content_model": f"{content.provider}/{content.model}",
        "businesses": len(results),
        "errored": len(results) - len(ok),
        "total_cost_gbp": round(total_cost, 4),
        "mean_cost_gbp": round(total_cost / len(results), 4),
        "mean_seconds": round(sum(r["total_seconds"] for r in ok) / len(ok), 1) if ok else None,
        "max_seconds": max((r["total_seconds"] for r in ok), default=None),
        "total_failures": sum(len(r["report"]["failures"]) for r in results),
        "total_warnings": sum(len(r["report"]["warnings"]) for r in results),
        "wall_clock_seconds": round(time.monotonic() - started, 1),
    }
    (out_dir / "_summary.json").write_text(json.dumps(summary, indent=2))

    print(f"\n{'':24} {'mean':>8} {'max':>8}")
    print(f"  {'seconds':22} {summary['mean_seconds'] or 0:8.1f} {summary['max_seconds'] or 0:8.1f}")
    print(f"  {'cost (GBP)':22} {summary['mean_cost_gbp']:8.4f} {'':>8}")
    print(f"\n  total £{summary['total_cost_gbp']:.4f} across {summary['businesses']} businesses")
    print(f"  {summary['total_failures']} failures, {summary['total_warnings']} warnings")

    # The blueprint's Definition of Done: <£0.40 and <90s per campaign.
    if args.steps == 5 and ok:
        over_cost = [r["id"] for r in ok if r["cost_gbp"] > 0.40]
        over_time = [r["id"] for r in ok if r["total_seconds"] > 90]
        print(f"\n  over £0.40: {over_cost or 'none'}")
        print(f"  over 90s:   {over_time or 'none'}")

    print(f"\n  written to {out_dir}")
    return 1 if summary["total_failures"] or summary["errored"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
