"""Provider adapters.

The pipeline needs exactly one capability from a provider: given a system
prompt, a user prompt, and a JSON Schema, return an object that conforms to the
schema. Everything else — SDKs, parameter names, error types — is adapter detail.

Two adapters cover the field:

* `openai_compatible` — anything speaking the OpenAI chat-completions shape.
  Kimi/Moonshot, OpenAI, OpenRouter, DeepSeek, Together. Differs only by
  base URL and key.
* `anthropic` — Claude's Messages API, which uses `output_config.format`
  rather than `response_format`.

Keeping both means a role can be pointed at either vendor, which is what makes
a side-by-side bake-off possible without touching pipeline code.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int


# ── OpenAI-compatible ───────────────────────────────────────────────────────
_openai_clients: dict[tuple[str, str], object] = {}


def _openai_client(api_key: str, base_url: str):
    import openai

    key = (api_key, base_url)
    if key not in _openai_clients:
        _openai_clients[key] = openai.OpenAI(
            api_key=api_key, base_url=base_url, timeout=120.0
        )
    return _openai_clients[key]


def call_openai_compatible(
    *, api_key: str, base_url: str, model: str, system: str, user: str,
    schema: dict, schema_name: str, max_tokens: int, temperature: float,
) -> Completion:
    import openai

    client = _openai_client(api_key, base_url)
    try:
        response = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
        )
    except openai.APIStatusError as e:
        raise ProviderError(f"{model}: {e.status_code} {e}") from e

    choice = response.choices[0]
    # A truncated response is still valid-looking JSON up to the cut, so this has
    # to be caught before parsing or it surfaces as a confusing JSONDecodeError.
    if choice.finish_reason == "length":
        raise ProviderError(f"{model}: hit max_tokens ({max_tokens}) — output truncated")
    if not choice.message.content:
        raise ProviderError(f"{model}: empty response (finish_reason={choice.finish_reason})")

    usage = response.usage
    details = getattr(usage, "prompt_tokens_details", None)
    return Completion(
        text=choice.message.content,
        input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        cached_tokens=(getattr(details, "cached_tokens", 0) or 0) if details else 0,
    )


# ── Anthropic ───────────────────────────────────────────────────────────────
_anthropic_client = None


def _anthropic(api_key: str):
    global _anthropic_client
    if _anthropic_client is None:
        try:
            import anthropic
        except ImportError as e:  # optional dependency
            raise ProviderError(
                "the anthropic package is not installed — "
                "`pip install anthropic` to use an Anthropic-backed role"
            ) from e
        _anthropic_client = anthropic.Anthropic(api_key=api_key, timeout=120.0)
    return _anthropic_client


def call_anthropic(
    *, api_key: str, model: str, system: str, user: str, schema: dict,
    schema_name: str, max_tokens: int, thinking: str,
) -> Completion:
    import anthropic

    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
    }
    # Every call here is schema-constrained extraction or drafting, so thinking
    # is off by default — it would add latency and output tokens for no gain,
    # and skew a bake-off against a provider whose default is thinking-off.
    if thinking == "disabled":
        kwargs["thinking"] = {"type": "disabled"}

    try:
        response = _anthropic(api_key).messages.create(**kwargs)
    except anthropic.APIStatusError as e:
        raise ProviderError(f"{model}: {e.status_code} {e}") from e

    # A refusal is a 200 with empty/partial content — check before indexing.
    if response.stop_reason == "refusal":
        category = getattr(getattr(response, "stop_details", None), "category", "unknown")
        raise ProviderError(f"{model} declined the request ({category})")
    if response.stop_reason == "max_tokens":
        raise ProviderError(f"{model}: hit max_tokens ({max_tokens}) — output truncated")

    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise ProviderError(f"{model}: no text block in response")

    usage = response.usage
    return Completion(
        text=text,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cached_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
    )


def parse_json(text: str, model: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ProviderError(f"{model}: response was not valid JSON: {e}") from e
