"""Model access for the campaign pipeline.

Roles, not vendors. The pipeline asks for the "strategy" model or the "content"
model; which vendor and model that resolves to is configuration. That is what
lets strategy run on one provider and the content fan-out on another, and what
makes a side-by-side bake-off an env change rather than a refactor.

The pipeline depends on schema-constrained output — that is what let us drop the
blueprint's "JSON-repair handling". Every model listed in MODELS below has been
confirmed to enforce a strict JSON Schema; `call_json` refuses anything else,
because a model that silently ignores the schema would return unvalidated JSON
and the failure would surface far downstream.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

from ..config import Config
from . import providers
from .providers import ProviderError

log = logging.getLogger(__name__)

LLMError = ProviderError  # single error type for callers

RETRIES = 2

# A timeout is weak evidence of transience: the request already ran the full
# budget, so an identical retry usually burns another full budget and fails the
# same way. One retry covers a genuine blip; more just delays the honest error.
TIMEOUT_RETRIES = 1

# Ceiling on model calls in flight across the whole process.
#
# The orchestrator nests pools: a stage runs its agents concurrently, and the
# copy agent fans out over formats inside that. Those multiply — stage 3 peaked
# at nine simultaneous calls on a 0.1-CPU instance, which is how you earn 429s
# and turn a healthy stage into a retry storm. Capping here rather than shrinking
# the pools keeps the concurrency structure intact and puts one predictable
# number on the load, wherever the calls come from.
MAX_INFLIGHT = int(os.environ.get("LLM_MAX_INFLIGHT", "4"))
_inflight = threading.Semaphore(MAX_INFLIGHT)
MAX_TOKENS = 16000
USD_TO_GBP = 0.79


@dataclass(frozen=True)
class ModelSpec:
    provider: str            # "kimi" | "anthropic" | "openai" | "openrouter"
    input_usd: float         # per million tokens
    output_usd: float
    cached_usd: float = 0.0  # discounted rate for cache hits, where offered
    # Some providers fix the sampling temperature and reject any other value
    # with a 400 — Moonshot does this across its range, not only on the
    # thinking models, which cost a live run to discover. When False the
    # parameter is omitted entirely and the provider's own default applies.
    supports_temperature: bool = True
    # Whether the model actually returns what `response_format: json_schema`
    # asks for. The pipeline has no repair path, so a model that ignores the
    # schema does not degrade — it loses the call outright.
    honours_schema: bool = True


# Every entry here must be a model *confirmed* to honour strict JSON Schema
# output, because the pipeline has no repair path. That invariant was written
# down here from the start and then not actually checked for kimi-k2.5, which
# is the whole reason honours_schema now exists as a field rather than a
# comment: an unverified claim in a comment costs nothing to write and
# everything to trust.
#
# Deliberately absent: moonshot-v1-* (predates strict schema support).
MODELS: dict[str, ModelSpec] = {
    # Kimi / Moonshot AI
    "kimi-k3":                  ModelSpec("kimi", 3.00, 15.00, 0.30, supports_temperature=False),
    "kimi-k2.6":                ModelSpec("kimi", 0.95, 4.00, 0.16, supports_temperature=False),
    # Ignores response_format and answers in Markdown prose. Measured over one
    # campaign's fan-out: 4 of 10 legs came back as JSON, the other 6 as
    # "**Variant 1**\n\n..." — well written, wrong shape, silently dropped.
    # Partial compliance is worse than none; it looks like it works.
    #
    # honours_schema is a threshold, not a guarantee. No Moonshot model observed
    # here complies every time — kimi-k2.6 missed 1 call in 14 the same way.
    # The flag separates "reliable enough that retries cover the gap" from
    # "loses most of the work"; retries are what make the former true.
    "kimi-k2.5":                ModelSpec("kimi", 0.60, 3.00, 0.10, supports_temperature=False,
                                          honours_schema=False),
    "kimi-k2.7-code":           ModelSpec("kimi", 1.20, 5.00, 0.19, supports_temperature=False),
    "kimi-k2.7-code-highspeed": ModelSpec("kimi", 1.20, 5.00, 0.19, supports_temperature=False),
    # Anthropic
    "claude-opus-5":            ModelSpec("anthropic", 5.00, 25.00, 0.50),
    "claude-sonnet-5":          ModelSpec("anthropic", 3.00, 15.00, 0.30),
    "claude-haiku-4-5":         ModelSpec("anthropic", 1.00, 5.00, 0.10),
}


@dataclass(frozen=True)
class RoleConfig:
    role: str
    model: str
    provider: str


def resolve_role(role: str) -> RoleConfig:
    """Which model backs a pipeline role, from config."""
    model = Config.model_for_role(role)
    spec = MODELS.get(model)
    if not spec:
        raise LLMError(
            f"{model!r} (role {role!r}) is not a known strict-schema model. "
            f"The pipeline has no JSON-repair path, so add it to llm.MODELS only "
            f"after confirming it enforces a strict JSON Schema. "
            f"Known: {', '.join(sorted(MODELS))}"
        )
    if not spec.honours_schema:
        raise LLMError(
            f"{model!r} is configured for role {role!r}, but it does not "
            f"reliably honour a strict JSON Schema — it answers in prose often "
            f"enough that calls are lost rather than degraded. Point "
            f"MODEL_{role.upper()} at one of: "
            f"{', '.join(sorted(m for m, s in MODELS.items() if s.honours_schema))}"
        )
    return RoleConfig(role=role, model=model, provider=spec.provider)


def cost_gbp(model: str, c: providers.Completion) -> float:
    spec = MODELS.get(model)
    if not spec:
        return 0.0
    fresh = max(c.input_tokens - c.cached_tokens, 0)
    usd = (
        fresh / 1_000_000 * spec.input_usd
        + c.cached_tokens / 1_000_000 * spec.cached_usd
        + c.output_tokens / 1_000_000 * spec.output_usd
    )
    return usd * USD_TO_GBP


def _dispatch(role: RoleConfig, **kw) -> providers.Completion:
    if role.provider == "anthropic":
        return providers.call_anthropic(
            api_key=Config.require_key("anthropic"),
            model=role.model,
            thinking=Config.ANTHROPIC_THINKING,
            **kw,
        )
    api_key, base_url = Config.openai_compatible_credentials(role.provider)
    spec = MODELS[role.model]
    return providers.call_openai_compatible(
        api_key=api_key,
        base_url=base_url,
        model=role.model,
        temperature=Config.LLM_TEMPERATURE if spec.supports_temperature else None,
        **kw,
    )


def call_json(
    *,
    role: str,
    system: str,
    user: str,
    schema: dict,
    schema_name: str = "output",
    max_tokens: int = MAX_TOKENS,
) -> tuple[dict, float, RoleConfig]:
    """One schema-constrained call for a pipeline role.

    Returns (parsed_output, cost_gbp, role_config). No repair path — the schema
    is enforced by the provider.
    """
    resolved = resolve_role(role)

    last_error: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            # Held only around the network call, so a thread waiting its turn
            # is not also holding a slot.
            with _inflight:
                completion = _dispatch(
                    resolved,
                    system=system,
                    user=user,
                    schema=schema,
                    schema_name=schema_name,
                    max_tokens=max_tokens,
                )
        except ProviderError as e:
            # Truncation, refusals and 4xx are deterministic — retrying just
            # burns money and time. Only transport-shaped failures get a retry.
            budget = TIMEOUT_RETRIES if _gets_short_budget(e) else RETRIES
            if not _is_transient(e) or attempt >= budget:
                raise
            last_error = e
            delay = 2**attempt
            log.warning("[llm] %s — retrying in %ss", e, delay)
            time.sleep(delay)
            continue
        except Exception as e:
            last_error = e
            if attempt == RETRIES:
                raise LLMError(f"{resolved.model}: {type(e).__name__}: {e}") from e
            time.sleep(2**attempt)
            continue

        parsed = providers.parse_json(completion.text, resolved.model)
        return parsed, cost_gbp(resolved.model, completion), resolved

    raise LLMError(f"{resolved.model}: failed after {RETRIES + 1} attempts: {last_error}")


def _is_timeout(e: Exception) -> bool:
    return "timed out" in str(e).lower() or "timeout" in type(e).__name__.lower()


def _gets_short_budget(e: Exception) -> bool:
    """Failures where a second identical attempt is the whole of the hope.

    A timeout qualifies: the request already ran the full window, so a third
    attempt mostly buys another full window of waiting.

    An unparseable body was in here too, on the reasoning that one more go was
    worth it and a third rarely was. That was wrong, and a later run showed why:
    schema compliance is stochastic rather than per-model. kimi-k2.6 honoured it
    on 13 of 14 calls in one campaign and dropped the fourteenth into Markdown.
    Against odds like that a third attempt is cheap and takes the loss rate from
    roughly one call in two hundred to one in three thousand, so an unparseable
    body now gets the full budget.
    """
    # Truncation joins timeouts here rather than getting the full budget: by
    # definition the attempt generated the maximum number of output tokens, so
    # each retry is the most expensive call the pipeline can make. One fresh
    # sample is worth it; three is £0.19 and twelve minutes on one agent.
    return _is_timeout(e) or "max_tokens" in str(e).lower()


def _is_transient(e: Exception) -> bool:
    """Is this worth another attempt at all?

    Truncation and refusals are properties of the request: the same prompt hits
    the same ceiling and earns the same refusal, so retrying only costs money.

    An unparseable body used to be lumped in with those, on the reasoning that a
    schema-constrained call either conforms or does not. A live run disproved
    it: three of eight fan-out legs came back unparseable on kimi-k2.5 and were
    dropped on the spot, losing a third of the creative assets to what is
    plainly a flaky generation rather than a bad request. It gets the short
    retry budget — see TIMEOUT_RETRIES — for the same reason a timeout does.
    """
    text = str(e).lower()
    if "declined" in text:
        return False
    # Truncation reads like a request property — "this prompt needs more room" —
    # and was treated as one. The measurements say otherwise: every agent's
    # normal output sits between 5% and 30% of the 16k cap (research averages
    # ~870 tokens), so hitting the ceiling is not a request that outgrew it, it
    # is a sample that ran away and repeated itself to the wall. A fresh sample
    # almost certainly does not. Raising the cap would only buy a slower, dearer
    # version of the same failure.
    #
    # If any agent's ordinary output ever approaches the cap, this reasoning
    # stops holding and truncation becomes deterministic again.
    if "max_tokens" in text:
        return True
    if "not valid json" in text:
        return True
    return any(code in text for code in ("429", "500", "502", "503", "529", "timeout", "connection"))
