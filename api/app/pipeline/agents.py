"""One reusable agent runtime.

CLAUDE.md §4: "Build one reusable agent runtime, not seven bespoke
implementations. An agent is { system_prompt, tool_set, memory_scope,
output_contract } run through the shared Kimi client. The difference between the
Email Agent and the SEO Agent is configuration, not separate codebases."

So an agent here is a JSON file in `specs/`, and this module is the only code
that runs one. Adding the SEO agent added a file, not a function.

The registry also owns the *shape* of the run: `depends_on` says what an agent
needs, `stage` says what can run alongside it. The orchestrator derives the
order from that rather than hardcoding a sequence, which is what lets the four
channel agents fan out concurrently instead of queueing behind each other.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .llm import call_json

log = logging.getLogger(__name__)

SPEC_DIR = Path(__file__).parent / "specs"


class AgentError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentSpec:
    id: str
    title: str                      # shown in the generation view
    description: str                # shown in the generation view
    role: str                       # model role: "strategy" | "content"
    stage: int                      # agents sharing a stage run concurrently
    system: str
    user_template: str
    schema: dict
    depends_on: tuple[str, ...] = ()
    # Agents that write content_assets rows rather than only pipeline_outputs.
    produces_assets: bool = False
    # "per_format" fans out one call per (funnel stage x creative format).
    # The only execution mode beyond a single call; see orchestrator.
    fan_out: str | None = None
    # Reserved for §5 agent chat: which tools this agent may call. Empty for now
    # — every agent is still generate-only.
    tool_set: tuple[str, ...] = field(default=())


@lru_cache(maxsize=1)
def registry() -> dict[str, AgentSpec]:
    specs: dict[str, AgentSpec] = {}
    for path in sorted(SPEC_DIR.glob("*.json")):
        raw = json.loads(path.read_text())
        missing = {"id", "title", "description", "role", "stage", "system",
                   "user_template", "schema"} - set(raw)
        if missing:
            raise AgentError(f"{path.name}: missing {sorted(missing)}")
        if raw["id"] != path.stem:
            raise AgentError(f"{path.name}: id {raw['id']!r} does not match filename")
        specs[raw["id"]] = AgentSpec(
            id=raw["id"],
            title=raw["title"],
            description=raw["description"],
            role=raw["role"],
            stage=raw["stage"],
            system=raw["system"],
            user_template=raw["user_template"],
            schema=raw["schema"],
            depends_on=tuple(raw.get("depends_on", ())),
            produces_assets=raw.get("produces_assets", False),
            fan_out=raw.get("fan_out"),
            tool_set=tuple(raw.get("tool_set", ())),
        )

    for spec in specs.values():
        for dep in spec.depends_on:
            if dep not in specs:
                raise AgentError(f"{spec.id}: depends on unknown agent {dep!r}")
            if specs[dep].stage >= spec.stage:
                raise AgentError(
                    f"{spec.id} (stage {spec.stage}) depends on {dep} "
                    f"(stage {specs[dep].stage}) — a dependency must run earlier"
                )
    return specs


def stages() -> list[list[AgentSpec]]:
    """Agents grouped into execution stages, in order.

    Everything in one stage is independent of everything else in it, so the
    orchestrator can run a stage concurrently.
    """
    by_stage: dict[int, list[AgentSpec]] = {}
    for spec in registry().values():
        by_stage.setdefault(spec.stage, []).append(spec)
    return [sorted(by_stage[s], key=lambda a: a.id) for s in sorted(by_stage)]


def ordered() -> list[AgentSpec]:
    """Flat run order — what the generation view renders as its step list."""
    return [spec for stage in stages() for spec in stage]


# Appended to every agent's system prompt.
#
# Moonshot treats response_format as a strong hint, not a hard constraint. Every
# prompt in specs/ is written in Markdown, and not one of them mentions JSON, a
# schema, or an output shape — the only signal was the API parameter. When the
# words and the parameter disagree, the words win: the model returns a nicely
# formatted document using the schema's field names as **bold** headings.
# Observed on copy_social, email and strategy, on both k2.5 and k2.6, and
# strategy once missed on all three attempts, so retries alone do not cover it.
#
# This lives here rather than in each spec so a new agent cannot forget it.
OUTPUT_CONTRACT = (
    "Return one JSON object conforming to the supplied schema, and nothing "
    "else. No Markdown, no headings, no bold, no code fences, and no "
    "commentary before or after it. The names in the schema are keys, not "
    "section titles."
)


def run(spec: AgentSpec, variables: dict, *, max_tokens: int | None = None) -> tuple[dict, float]:
    """Render an agent's prompt and make its one schema-constrained call.

    Returns (output, cost_gbp). Does not mutate shared state — the orchestrator
    runs a whole stage concurrently, so accumulating here would race.
    """
    try:
        user = spec.user_template.format(**variables)
    except KeyError as e:
        raise AgentError(f"{spec.id}: template references unknown variable {e}") from e

    kwargs = {"max_tokens": max_tokens} if max_tokens else {}
    output, cost, resolved = call_json(
        role=spec.role,
        system=f"{spec.system}\n\n{OUTPUT_CONTRACT}",
        user=user,
        schema=spec.schema,
        schema_name=spec.id,
        **kwargs,
    )
    log.info("[agent] %s on %s/%s — £%.4f", spec.id, resolved.provider, resolved.model, cost)
    return output, cost
