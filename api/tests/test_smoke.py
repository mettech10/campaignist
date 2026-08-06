"""Week 1 verification: the app boots, health reports config, auth fails closed."""
import pytest

from app import create_app


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_health_ok(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert set(body["integrations"]) == {"supabase", "stripe", "stripe_webhook"}
    # Every pipeline role must resolve to a known model.
    for role in ("strategy", "content"):
        assert "error" not in body["models"][role], body["models"][role]
        assert body["models"][role]["provider"]


def test_protected_routes_require_a_token(client):
    for method, path in [
        ("get", "/api/business"),
        ("post", "/api/business"),
        ("get", "/api/campaigns"),
        ("post", "/api/campaigns"),
    ]:
        resp = getattr(client, method)(path, json={})
        assert resp.status_code == 401, f"{method.upper()} {path} was not protected"
        assert resp.get_json()["error"] == "missing_token"


def test_garbage_token_is_rejected(client):
    resp = client.get("/api/business", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code in (401, 503)


def test_stripe_webhook_rejects_unsigned_payload(client):
    resp = client.post("/api/stripe/webhook", json={"type": "invoice.payment_succeeded"})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_signature"


def test_stripe_signature_verification():
    import hashlib
    import hmac
    import time

    from app.routes.billing import verify_stripe_signature

    secret = "whsec_test"
    body = b'{"type":"ping"}'
    ts = str(int(time.time()))
    sig = hmac.new(
        secret.encode(), f"{ts}.{body.decode()}".encode(), hashlib.sha256
    ).hexdigest()

    assert verify_stripe_signature(body, f"t={ts},v1={sig}", secret)
    assert not verify_stripe_signature(body, f"t={ts},v1=deadbeef", secret)
    assert not verify_stripe_signature(body, "", secret)
    assert not verify_stripe_signature(b'{"type":"tampered"}', f"t={ts},v1={sig}", secret)


def test_update_refuses_unfiltered_writes():
    from app.supabase import SupabaseError, update

    with pytest.raises(SupabaseError):
        update("businesses", {"name": "x"}, params={})


# ── Week 2: prompt specs and pipeline wiring ────────────────────────────────

def test_all_prompt_specs_load_and_are_well_formed():
    from app.pipeline.steps import PROMPT_DIR, load_prompt

    specs = sorted(p.stem for p in PROMPT_DIR.glob("*.json"))
    assert specs == [
        "step1_business_understanding",
        "step2_audience_positioning",
        "step3_funnel_channel",
        "step4_content_generation",
        "step5_campaign_assembly",
    ]

    for spec_id in specs:
        spec = load_prompt(spec_id)
        for key in ("id", "model", "system", "user_template", "schema"):
            assert key in spec, f"{spec_id} missing {key}"
        assert spec["id"] == spec_id
        assert spec["model"] in ("strategy", "content")
        # Structured outputs require additionalProperties:false on every object.
        _assert_strict_schema(spec["schema"], spec_id)


def _assert_strict_schema(node, spec_id, path="root"):
    if not isinstance(node, dict):
        return
    if node.get("type") == "object":
        assert node.get("additionalProperties") is False, \
            f"{spec_id}: {path} must set additionalProperties: false"
        assert "required" in node, f"{spec_id}: {path} must list required"
        for name, child in node.get("properties", {}).items():
            _assert_strict_schema(child, spec_id, f"{path}.{name}")
    elif node.get("type") == "array":
        _assert_strict_schema(node.get("items", {}), spec_id, f"{path}[]")


def test_templates_render_with_the_context_variables():
    """Every {placeholder} in a single-call step's template must be supplied by
    _render_vars. Steps 4 and 5 take per-call extras and have their own tests."""
    from app.pipeline.steps import PipelineContext, _render_vars, load_prompt

    ctx = PipelineContext(
        campaign={"goal": "Book 25 consultations", "budget": 750, "timeframe_days": 30},
        business={"name": "Hearth & Crumb", "brand_voice": {"tone": "Warm"}},
    )
    variables = _render_vars(ctx)

    for spec_id in ("step1_business_understanding", "step2_audience_positioning",
                    "step3_funnel_channel"):
        load_prompt(spec_id)["user_template"].format(**variables)  # KeyError if unsupplied


def test_zero_budget_renders_as_a_real_value():
    from app.pipeline.steps import PipelineContext, _render_vars

    ctx = PipelineContext(campaign={"goal": "g", "budget": 0}, business={})
    assert _render_vars(ctx)["budget"] == "£0"


def test_format_catalogue_matches_the_frontend_taxonomy():
    """Backend ids must stay in sync with web/lib/content-formats.ts — the
    frontend renders labels from whatever id the backend stores."""
    import re
    from pathlib import Path

    from app.pipeline.formats import FORMATS

    ts = Path(__file__).resolve().parents[2] / "web" / "lib" / "content-formats.ts"
    if not ts.exists():
        import pytest
        pytest.skip("frontend checkout not present")

    frontend_ids = set(re.findall(r"^\s+id: '([a-z0-9-]+)',$", ts.read_text(), re.M))
    assert frontend_ids == set(FORMATS), (
        f"only in frontend: {frontend_ids - set(FORMATS)}; "
        f"only in backend: {set(FORMATS) - frontend_ids}"
    )


def test_step_3_drops_hallucinated_format_ids(monkeypatch):
    from app.pipeline import steps

    monkeypatch.setattr(steps, "run_prompt_step", lambda _id, _ctx: {
        "funnel_stages": [
            {"stage": "Awareness", "formats": ["ugc-testimonial", "tiktok-dance"]},
        ],
        "budget_split": [{"label": "Meta", "percent": 60}, {"label": "Google", "percent": 40}],
    })
    out = steps.step_3_funnel_channel(steps.PipelineContext(campaign={}, business={}))
    assert out["funnel_stages"][0]["formats"] == ["ugc-testimonial"]


def test_all_five_steps_are_wired():
    from app.pipeline.steps import STEPS

    assert sorted(STEPS) == [1, 2, 3, 4, 5]
    assert all(callable(fn) for fn in STEPS.values())


def test_step4_refuses_to_run_without_step3_output():
    from app.pipeline.steps import PipelineContext, StepNotImplemented, step_4_content_generation

    with pytest.raises(StepNotImplemented):
        step_4_content_generation(PipelineContext(campaign={"id": "c"}, business={}))


# ── Week 3: content fan-out and assembly ────────────────────────────────────

def test_week3_prompt_specs_present_and_strict():
    from app.pipeline.steps import PROMPT_DIR, load_prompt

    for spec_id in ("step4_content_generation", "step5_campaign_assembly"):
        assert (PROMPT_DIR / f"{spec_id}.json").exists()
        spec = load_prompt(spec_id)
        assert spec["id"] == spec_id
        _assert_strict_schema(spec["schema"], spec_id)


def test_step4_template_renders_with_its_extra_vars():
    from app.pipeline import formats
    from app.pipeline.steps import PipelineContext, _render_vars, load_prompt

    ctx = PipelineContext(campaign={"goal": "g", "budget": 500}, business={"name": "B"})
    spec = formats.FORMATS["ugc-testimonial"]
    variables = _render_vars(ctx) | {
        "stage": "{}", "format_id": "ugc-testimonial", "format_label": spec["label"],
        "format_aspect": spec["aspect_ratio"], "format_guidance": spec["brief_guidance"],
        "variant_count": 2,
    }
    load_prompt("step4_content_generation")["user_template"].format(**variables)


def test_step5_template_renders_with_its_extra_vars():
    from app.pipeline.steps import PipelineContext, _render_vars, load_prompt

    ctx = PipelineContext(campaign={"goal": "g", "budget": 500}, business={"name": "B"})
    variables = _render_vars(ctx) | {"asset_index": "- a | x", "start_date": "Mon 5 Aug 2026"}
    load_prompt("step5_campaign_assembly")["user_template"].format(**variables)


def test_step4_fans_out_routes_models_and_writes_assets(monkeypatch):
    """Short-copy formats go to Haiku, written formats to Sonnet, and every
    (stage x format) pair produces its own call."""
    from app.pipeline import steps

    seen = []

    def fake_call(prompt_id, ctx, *, extra_vars=None, model_key=None):
        seen.append((extra_vars["format_id"], model_key))
        return {"assets": [
            {"type": "social_post", "channel": "Instagram", "hook": "h",
             "body": "b", "cta": "c", "hashtags": [], "image_brief": "brief"}
            for _ in range(extra_vars["variant_count"])
        ]}, 0.01

    written = {}
    monkeypatch.setattr(steps, "call_prompt", fake_call)
    monkeypatch.setattr(steps.supabase, "delete", lambda t, **kw: None)
    monkeypatch.setattr(steps.supabase, "insert",
                        lambda t, rows, **kw: written.setdefault("rows", rows) or
                        [{**r, "id": f"a{i}"} for i, r in enumerate(rows)])

    ctx = steps.PipelineContext(
        campaign={"id": "camp-1", "goal": "g"}, business={},
        outputs={"3": {"funnel_stages": [
            {"stage": "Awareness", "formats": ["ugc-testimonial", "product-shot"]},
            {"stage": "Conversion", "formats": ["long-form-email"]},
        ]}},
    )
    out = steps.step_4_content_generation(ctx)

    assert dict(seen) == {
        "ugc-testimonial": "content",     # video -> Haiku
        "product-shot": "content",        # image -> Haiku
        "long-form-email": "strategy",    # text  -> Sonnet
    }
    assert out["asset_count"] == 6        # 3 formats x 2 variants
    assert ctx.cost_gbp == pytest.approx(0.03)
    assert [r["variant"] for r in written["rows"][:2]] == [1, 2]
    assert all(r["format"] in steps.formats.FORMATS for r in written["rows"])


def test_step4_survives_one_failed_leg(monkeypatch):
    from app.pipeline import steps

    def flaky(prompt_id, ctx, *, extra_vars=None, model_key=None):
        if extra_vars["format_id"] == "product-shot":
            raise RuntimeError("overloaded")
        return {"assets": [{"type": "social_post", "channel": "IG", "hook": "h",
                            "body": "b", "cta": "c", "hashtags": [],
                            "image_brief": "x"}]}, 0.01

    monkeypatch.setattr(steps, "call_prompt", flaky)
    monkeypatch.setattr(steps.supabase, "delete", lambda t, **kw: None)
    monkeypatch.setattr(steps.supabase, "insert",
                        lambda t, rows, **kw: [{**r, "id": "a"} for r in rows])

    ctx = steps.PipelineContext(
        campaign={"id": "c", "goal": "g"}, business={},
        outputs={"3": {"funnel_stages": [
            {"stage": "Awareness", "formats": ["ugc-testimonial", "product-shot"]},
        ]}},
    )
    out = steps.step_4_content_generation(ctx)
    assert out["asset_count"] == 1
    assert len(out["failed_legs"]) == 1


def test_step5_drops_calendar_entries_pointing_at_missing_assets(monkeypatch):
    from app.pipeline import steps

    monkeypatch.setattr(steps, "call_prompt", lambda *a, **kw: ({
        "campaign_name": "C", "summary": "s",
        "30_day_calendar": [
            {"day": 1, "date": "Mon", "channel": "IG", "asset_id": "real", "action": "post"},
            {"day": 2, "date": "Tue", "channel": "IG", "asset_id": "ghost", "action": "post"},
        ],
        "launch_checklist": [], "success_metrics": [],
    }, 0.02))

    ctx = steps.PipelineContext(
        campaign={"id": "c", "goal": "g"}, business={},
        assets=[{"id": "real", "type": "social_post", "channel": "IG",
                 "format": "product-shot", "variant": 1, "content": {"hook": "h"}}],
    )
    out = steps.step_5_campaign_assembly(ctx)
    assert [e["asset_id"] for e in out["30_day_calendar"]] == ["real"]


def test_supabase_delete_refuses_unfiltered():
    from app.supabase import SupabaseError, delete

    with pytest.raises(SupabaseError):
        delete("content_assets", params={})


# ── Model roles and providers ───────────────────────────────────────────────

def test_both_roles_resolve_to_known_strict_schema_models():
    """The pipeline has no JSON-repair path, so every configured model must be
    one we have confirmed enforces a strict JSON Schema."""
    from app.pipeline.llm import MODELS, resolve_role

    for role in ("strategy", "content"):
        resolved = resolve_role(role)
        assert resolved.model in MODELS
        assert resolved.provider in ("kimi", "anthropic", "openai", "openrouter")


def test_unknown_model_is_refused_with_a_useful_message(monkeypatch):
    from app.config import Config
    from app.pipeline.llm import LLMError, resolve_role

    monkeypatch.setattr(Config, "MODEL_STRATEGY", "moonshot-v1-8k")
    with pytest.raises(LLMError, match="strict JSON Schema"):
        resolve_role("strategy")


def test_unknown_role_is_refused():
    from app.config import ConfigError
    from app.pipeline.llm import resolve_role

    with pytest.raises(ConfigError, match="unknown pipeline role"):
        resolve_role("marketing-genius")


def test_roles_can_sit_on_different_providers(monkeypatch):
    """The point of the role indirection: a bake-off is an env change."""
    from app.config import Config
    from app.pipeline.llm import resolve_role

    monkeypatch.setattr(Config, "MODEL_STRATEGY", "claude-sonnet-5")
    monkeypatch.setattr(Config, "MODEL_CONTENT", "kimi-k2.5")
    assert resolve_role("strategy").provider == "anthropic"
    assert resolve_role("content").provider == "kimi"


def test_missing_provider_key_names_the_env_var(monkeypatch):
    from app.config import Config, ConfigError

    monkeypatch.setattr(Config, "ANTHROPIC_API_KEY", "")
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        Config.require_key("anthropic")


def test_call_json_sends_a_strict_json_schema_request(monkeypatch):
    from app.pipeline import llm, providers

    captured = {}

    def fake_openai(**kwargs):
        captured.update(kwargs)
        return providers.Completion(
            text='{"ok": true}', input_tokens=1000, output_tokens=500, cached_tokens=0
        )

    monkeypatch.setattr(providers, "call_openai_compatible", fake_openai)
    monkeypatch.setattr(
        llm.Config, "openai_compatible_credentials",
        classmethod(lambda cls, p: ("sk-test", "https://api.moonshot.ai/v1")),
    )
    monkeypatch.setattr(llm.Config, "MODEL_STRATEGY", "kimi-k3")

    schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    out, cost, resolved = llm.call_json(
        role="strategy", system="sys", user="usr", schema=schema, schema_name="step1"
    )

    assert out == {"ok": True}
    assert resolved.model == "kimi-k3" and resolved.provider == "kimi"
    assert captured["schema"] is schema
    assert captured["schema_name"] == "step1"
    assert captured["base_url"] == "https://api.moonshot.ai/v1"
    # kimi-k3 at $3/$15 per MTok, converted to GBP
    assert cost == pytest.approx((1000 * 3.00 + 500 * 15.00) / 1_000_000 * 0.79)


def test_deterministic_failures_are_not_retried(monkeypatch):
    """Truncation and refusals are deterministic — retrying burns money."""
    from app.pipeline import llm, providers

    calls = []

    def always_truncates(**kwargs):
        calls.append(1)
        raise providers.ProviderError("kimi-k3: hit max_tokens (16000) — output truncated")

    monkeypatch.setattr(providers, "call_openai_compatible", always_truncates)
    monkeypatch.setattr(
        llm.Config, "openai_compatible_credentials",
        classmethod(lambda cls, p: ("sk-test", "https://x/v1")),
    )
    monkeypatch.setattr(llm.Config, "MODEL_STRATEGY", "kimi-k3")

    with pytest.raises(llm.LLMError, match="max_tokens"):
        llm.call_json(role="strategy", system="s", user="u", schema={})
    assert len(calls) == 1, "a deterministic failure was retried"


def test_transient_failures_are_retried(monkeypatch):
    from app.pipeline import llm, providers

    calls = []

    def flaky(**kwargs):
        calls.append(1)
        if len(calls) < 2:
            raise providers.ProviderError("kimi-k3: 429 rate limited")
        return providers.Completion(text="{}", input_tokens=1, output_tokens=1, cached_tokens=0)

    monkeypatch.setattr(providers, "call_openai_compatible", flaky)
    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        llm.Config, "openai_compatible_credentials",
        classmethod(lambda cls, p: ("sk-test", "https://x/v1")),
    )
    monkeypatch.setattr(llm.Config, "MODEL_STRATEGY", "kimi-k3")

    out, _cost, _r = llm.call_json(role="strategy", system="s", user="u", schema={})
    assert out == {} and len(calls) == 2


def test_cost_accounts_for_cached_input_tokens():
    from app.pipeline.llm import cost_gbp
    from app.pipeline.providers import Completion

    c = Completion(text="", input_tokens=1_000_000, output_tokens=0, cached_tokens=900_000)
    # 100k fresh at $3.00/M + 900k cached at $0.30/M
    assert cost_gbp("kimi-k3", c) == pytest.approx((0.1 * 3.00 + 0.9 * 0.30) * 0.79)


# ── JWT algorithm selection ─────────────────────────────────────────────────

def test_algorithm_comes_from_the_token_not_the_config(monkeypatch):
    """A leftover SUPABASE_JWT_SECRET must not force the HS256 path and reject
    the asymmetric tokens Supabase actually issues."""
    import jwt as pyjwt
    from cryptography.hazmat.primitives.asymmetric import ec

    from app import auth
    from app.config import Config

    key = ec.generate_private_key(ec.SECP256R1())
    token = pyjwt.encode(
        {"sub": "user-1", "aud": "authenticated"}, key, algorithm="ES256"
    )

    # A secret is set, but the token is ES256 — the JWKS path must still be taken.
    monkeypatch.setattr(Config, "SUPABASE_JWT_SECRET", "a-leftover-hs256-secret")
    monkeypatch.setattr(
        auth, "_jwks",
        lambda: type("J", (), {"get_signing_key_from_jwt":
                               staticmethod(lambda _t: type("K", (), {"key": key.public_key()}))})(),
    )
    assert auth.verify_token(token)["sub"] == "user-1"


def test_hs256_token_still_verifies_with_the_secret(monkeypatch):
    import jwt as pyjwt

    from app import auth
    from app.config import Config

    monkeypatch.setattr(Config, "SUPABASE_JWT_SECRET", "shhh")
    token = pyjwt.encode({"sub": "u", "aud": "authenticated"}, "shhh", algorithm="HS256")
    assert auth.verify_token(token)["sub"] == "u"


def test_unsigned_token_is_refused(monkeypatch):
    import jwt as pyjwt

    from app import auth

    token = pyjwt.encode({"sub": "attacker", "aud": "authenticated"}, key=None, algorithm="none")
    with pytest.raises(pyjwt.InvalidTokenError, match="unsupported token algorithm"):
        auth.verify_token(token)


def test_supabase_url_is_normalised_to_its_origin():
    """Pasting the REST URL instead of the project URL made the JWKS endpoint
    404/401, which rejected every valid token as invalid."""
    from app.config import _project_origin

    want = "https://abc.supabase.co"
    for raw in (
        "https://abc.supabase.co/rest/v1/",
        "https://abc.supabase.co/rest/v1",
        "https://abc.supabase.co/auth/v1",
        "https://abc.supabase.co/",
        "https://abc.supabase.co",
        "  https://abc.supabase.co/rest/v1/  ",
        "abc.supabase.co",
    ):
        assert _project_origin(raw) == want, raw
    assert _project_origin("") == ""


def test_temperature_is_omitted_for_models_that_fix_it(monkeypatch):
    """kimi-k3 has always-on thinking and 400s on any temperature but 1."""
    from app.pipeline import llm, providers

    seen = {}

    def fake(**kwargs):
        seen.update(kwargs)
        return providers.Completion(text="{}", input_tokens=1, output_tokens=1, cached_tokens=0)

    monkeypatch.setattr(providers, "call_openai_compatible", fake)
    monkeypatch.setattr(
        llm.Config, "openai_compatible_credentials",
        classmethod(lambda cls, p: ("k", "https://x/v1")),
    )

    monkeypatch.setattr(llm.Config, "MODEL_STRATEGY", "kimi-k3")
    llm.call_json(role="strategy", system="s", user="u", schema={})
    assert seen["temperature"] is None, "kimi-k3 must not be sent a temperature"

    monkeypatch.setattr(llm.Config, "MODEL_CONTENT", "kimi-k2.5")
    monkeypatch.setattr(llm.Config, "LLM_TEMPERATURE", 0.6)
    llm.call_json(role="content", system="s", user="u", schema={})
    assert seen["temperature"] == 0.6, "kimi-k2.5 should still get a temperature"


def test_none_temperature_is_not_sent_to_the_api(monkeypatch):
    from app.pipeline import providers

    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return type("R", (), {
                "choices": [type("C", (), {
                    "finish_reason": "stop",
                    "message": type("M", (), {"content": "{}"})()})()],
                "usage": type("U", (), {"prompt_tokens": 1, "completion_tokens": 1,
                                        "prompt_tokens_details": None})(),
            })()

    monkeypatch.setattr(
        providers, "_openai_client",
        lambda k, b: type("C", (), {"chat": type("Ch", (), {"completions": FakeCompletions()})()})(),
    )
    providers.call_openai_compatible(
        api_key="k", base_url="b", model="kimi-k3", system="s", user="u",
        schema={}, schema_name="n", max_tokens=100, temperature=None,
    )
    assert "temperature" not in captured
