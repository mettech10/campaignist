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


# ── Agent registry and orchestration ────────────────────────────────────────

def test_every_agent_spec_is_well_formed_and_strict():
    from app.pipeline import agents

    registry = agents.registry()
    assert set(registry) == {
        "research", "strategy", "copy_social", "email", "seo", "geo", "assembly",
    }
    for spec in registry.values():
        assert spec.role in ("strategy", "content")
        assert spec.title and spec.description
        # Structured outputs require additionalProperties:false on every object.
        _assert_strict_schema(spec.schema, spec.id)


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


def test_the_four_channel_agents_share_one_stage():
    """Copy/Social, Email, SEO and GEO are independent, so they must run
    concurrently rather than queueing behind each other."""
    from app.pipeline import agents

    stages = agents.stages()
    assert [a.id for a in stages[0]] == ["research"]
    assert [a.id for a in stages[1]] == ["strategy"]
    assert set(a.id for a in stages[2]) == {"copy_social", "email", "seo", "geo"}
    assert [a.id for a in stages[3]] == ["assembly"]


def test_dependencies_always_run_in_an_earlier_stage():
    from app.pipeline import agents

    registry = agents.registry()
    for spec in registry.values():
        for dep in spec.depends_on:
            assert registry[dep].stage < spec.stage, f"{spec.id} -> {dep}"


def test_every_template_renders_from_the_orchestrator_context():
    from app.pipeline import agents, orchestrator

    ctx = orchestrator.CampaignContext(
        campaign={"id": "c", "goal": "Book 25 consultations", "budget": 750, "timeframe_days": 30},
        business={"name": "Hearth & Crumb", "brand_voice": {"tone": "Warm"}},
    )
    variables = orchestrator.base_variables(ctx)
    # copy_social is the one fan-out agent and takes per-format extras.
    fanout_extras = {
        "stage": "{}", "format_id": "x", "format_label": "X",
        "format_aspect": "1:1", "format_guidance": "g", "variant_count": 2,
    }
    for spec in agents.registry().values():
        spec.user_template.format(**(variables | fanout_extras))


def test_zero_budget_renders_as_a_real_value():
    from app.pipeline import orchestrator

    ctx = orchestrator.CampaignContext(campaign={"goal": "g", "budget": 0}, business={})
    assert orchestrator.base_variables(ctx)["budget"] == "£0"


def test_format_catalogue_matches_the_frontend_taxonomy():
    import re
    from pathlib import Path

    from app.pipeline.formats import FORMATS

    ts = Path(__file__).resolve().parents[2] / "web" / "lib" / "content-formats.ts"
    if not ts.exists():
        pytest.skip("frontend checkout not present")
    frontend_ids = set(re.findall(r"^\s+id: '([a-z0-9-]+)',$", ts.read_text(), re.M))
    assert frontend_ids == set(FORMATS)


def test_frontend_step_list_matches_the_agent_registry():
    """The generation view renders pipelineSteps; if it drifts from the agents
    the orchestrator actually runs, progress labels lie."""
    import re
    from pathlib import Path

    from app.pipeline import agents

    ts = Path(__file__).resolve().parents[2] / "web" / "lib" / "ui-constants.ts"
    if not ts.exists():
        pytest.skip("frontend checkout not present")
    block = ts.read_text().split("export const pipelineSteps")[1]
    ids = re.findall(r"id: '([a-z_]+)'", block)
    assert ids == [a.id for a in agents.ordered()]


def test_email_output_becomes_content_assets():
    from app.pipeline import agents, orchestrator

    spec = agents.registry()["email"]
    ctx = orchestrator.CampaignContext(campaign={"id": "camp-1"}, business={})
    rows = orchestrator._assets_from(spec, {"trigger": "t", "emails": [
        {"position": 1, "subject": "s", "subject_variant": "sv", "preview_text": "p",
         "body": "b", "cta": "c", "send_offset_days": 0, "objection_addressed": "o"},
    ]}, ctx)
    assert len(rows) == 1
    assert rows[0]["type"] == "email" and rows[0]["format"] == "long-form-email"
    assert rows[0]["content"]["hook"] == "s"
    assert "sv" in rows[0]["image_brief"]


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
    """Moonshot 400s on any temperature but 1, across its whole range."""
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

    # Every Moonshot model fixes it, so no Kimi role may be sent one.
    monkeypatch.setattr(llm.Config, "MODEL_CONTENT", "kimi-k2.5")
    llm.call_json(role="content", system="s", user="u", schema={})
    assert seen["temperature"] is None, "kimi-k2.5 must not be sent a temperature"


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


# ── Stalled-generation reaper ───────────────────────────────────────────────

def _campaign(status="generating", minutes_ago=0):
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    return {"id": "c1", "business_id": "b1", "status": status, "progress_at": ts}


def test_a_live_generation_is_not_reaped():
    from app.pipeline import reaper

    assert not reaper.is_stale(_campaign(minutes_ago=0))
    assert not reaper.is_stale(_campaign(minutes_ago=11))


def test_a_silent_generation_is_stale():
    from app.pipeline import reaper

    assert reaper.is_stale(_campaign(minutes_ago=13))


def test_settled_campaigns_are_never_reaped():
    from app.pipeline import reaper

    for status in ("ready", "live", "completed", "error", "draft"):
        assert not reaper.is_stale(_campaign(status=status, minutes_ago=999))


def test_reaping_marks_error_and_refunds_the_credit(monkeypatch):
    from app.pipeline import reaper

    calls = {}

    def fake_update(table, patch, **kw):
        calls["update"] = (patch, kw)
        return {**_campaign(status="error"), **patch}

    def fake_refund(user_id, reason, campaign_id):
        calls["refund"] = (user_id, reason, campaign_id)
        return 1

    monkeypatch.setattr(reaper.supabase, "update", fake_update)
    monkeypatch.setattr(reaper.supabase, "select", lambda t, **kw: {"user_id": "u1"})
    monkeypatch.setattr(reaper.credits, "refund", fake_refund)

    out = reaper.reap(_campaign(minutes_ago=30))
    patch, kw = calls["update"]
    assert patch["status"] == "error"
    # Only reap a row still generating, so a run that finished in the meantime
    # is not clobbered.
    assert kw["params"]["status"] == "eq.generating"
    assert calls["refund"] == ("u1", "stalled_generation", "c1")
    assert out["status"] == "error"


def test_a_lost_race_does_not_refund(monkeypatch):
    """If the campaign completed between the check and the write, the update
    matches nothing — and no credit should be handed back."""
    from app.pipeline import reaper

    refunded = []
    monkeypatch.setattr(reaper.supabase, "update", lambda t, patch, **kw: None)
    monkeypatch.setattr(reaper.credits, "refund",
                        lambda *a: refunded.append(a))

    campaign = _campaign(minutes_ago=30)
    assert reaper.reap(campaign) is campaign
    assert refunded == []


def test_a_failed_refund_still_leaves_the_campaign_marked(monkeypatch):
    from app.pipeline import reaper

    monkeypatch.setattr(reaper.supabase, "update",
                        lambda t, patch, **kw: {**_campaign(status="error"), **patch})
    monkeypatch.setattr(reaper.supabase, "select", lambda t, **kw: {"user_id": "u1"})
    monkeypatch.setattr(reaper.credits, "refund",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("supabase down")))

    out = reaper.reap(_campaign(minutes_ago=30))
    assert out["status"] == "error"


def test_cors_origins_tolerate_a_trailing_slash(monkeypatch):
    """A browser sends Origin without a trailing slash, so an entry copied from
    the address bar would otherwise match nothing and block every request."""
    import importlib

    monkeypatch.setenv(
        "CORS_ORIGINS",
        " https://example.vercel.app/ ,http://localhost:3000, ",
    )
    import app.config as config
    importlib.reload(config)
    assert config.Config.CORS_ORIGINS == [
        "https://example.vercel.app", "http://localhost:3000",
    ]
    monkeypatch.delenv("CORS_ORIGINS")
    importlib.reload(config)


def test_no_kimi_model_is_sent_a_temperature():
    """Moonshot rejects any temperature but 1 on every model we use — sending
    one fails the run at the first agent."""
    from app.pipeline.llm import MODELS

    for name, spec in MODELS.items():
        if spec.provider == "kimi":
            assert not spec.supports_temperature, f"{name} would be sent a temperature"
