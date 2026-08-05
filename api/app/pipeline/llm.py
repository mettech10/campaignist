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
import time
from dataclasses import dataclass

from ..config import Config
from . import providers
from .providers import ProviderError

log = logging.getLogger(__name__)

LLMError = ProviderError  # single error type for callers

RETRIES = 2
MAX_TOKENS = 16000
USD_TO_GBP = 0.79


@dataclass(frozen=True)
class ModelSpec:
    provider: str            # "kimi" | "anthropic" | "openai" | "openrouter"
    input_usd: float         # per million tokens
    output_usd: float
    cached_usd: float = 0.0  # discounted rate for cache hits, where offered


# Every entry here is a model confirmed to honour strict JSON Schema output.
# Adding one without checking that breaks the no-repair-path assumption.
#
# Deliberately absent: moonshot-v1-* (predates strict schema support).
MODELS: dict[str, ModelSpec] = {
    # Kimi / Moonshot AI
    "kimi-k3":                  ModelSpec("kimi", 3.00, 15.00, 0.30),
    "kimi-k2.7-code":           ModelSpec("kimi", 1.20, 5.00, 0.19),
    "kimi-k2.7-code-highspeed": ModelSpec("kimi", 1.20, 5.00, 0.19),
    "kimi-k2.6":                ModelSpec("kimi", 0.95, 4.00, 0.16),
    "kimi-k2.5":                ModelSpec("kimi", 0.60, 3.00, 0.10),
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
    return providers.call_openai_compatible(
        api_key=api_key,
        base_url=base_url,
        model=role.model,
        temperature=Config.LLM_TEMPERATURE,
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
            if not _is_transient(e) or attempt == RETRIES:
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


def _is_transient(e: Exception) -> bool:
    text = str(e).lower()
    if "max_tokens" in text or "declined" in text or "not valid json" in text:
        return False
    return any(code in text for code in ("429", "500", "502", "503", "529", "timeout", "connection"))
