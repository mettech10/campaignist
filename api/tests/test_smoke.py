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
    monkeypatch.setattr(Config, "MODEL_CONTENT", "kimi-k2.6")
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
    """A refusal is a property of the request: the same prompt earns the same
    refusal, so retrying only burns money.

    Truncation used to be asserted here alongside it and is not deterministic —
    see test_timeouts_get_a_shorter_retry_budget_than_other_transients. Every
    agent's normal output sits at 5-30% of the cap, so hitting it is a sample
    that ran away, and a fresh one is worth trying."""
    from app.pipeline import llm, providers

    calls = []

    def always_refuses(**kwargs):
        calls.append(1)
        raise providers.ProviderError("kimi-k3 declined the request (policy)")

    monkeypatch.setattr(providers, "call_openai_compatible", always_refuses)
    monkeypatch.setattr(
        llm.Config, "openai_compatible_credentials",
        classmethod(lambda cls, p: ("sk-test", "https://x/v1")),
    )
    monkeypatch.setattr(llm.Config, "MODEL_STRATEGY", "kimi-k3")

    with pytest.raises(llm.LLMError, match="declined"):
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
    monkeypatch.setattr(llm.Config, "MODEL_CONTENT", "kimi-k2.6")
    llm.call_json(role="content", system="s", user="u", schema={})
    assert seen["temperature"] is None, "kimi-k2.6 must not be sent a temperature"


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


def test_an_old_heartbeat_on_a_restarted_run_looks_stale():
    """Regenerate used to flip status back to generating without bumping
    progress_at. The previous run's timestamp made the first poll reap a live
    regen. This asserts the staleness signal that bug relied on still holds —
    the route fix is what clears it."""
    from datetime import datetime, timedelta, timezone

    from app.pipeline import reaper

    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    assert reaper.is_stale(
        {"id": "c1", "business_id": "b1", "status": "generating", "progress_at": old}
    )


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


def test_a_generation_that_produced_nothing_refunds_the_credit(monkeypatch):
    """Dying on the first agent leaves an empty campaign — charging for that is
    not defensible."""
    from app.pipeline import runner

    refunded = []
    monkeypatch.setattr(runner.supabase, "update",
                        lambda t, patch, **kw: {"id": "c1", "business_id": "b1",
                                                "pipeline_outputs": {}, **patch})
    monkeypatch.setattr(runner.supabase, "select", lambda t, **kw: {"user_id": "u1"})
    monkeypatch.setattr(runner.credits, "refund",
                        lambda u, reason, cid: refunded.append((u, reason, cid)))

    runner._fail("c1", "ProviderError: 400 invalid temperature")
    assert refunded == [("u1", "failed_generation", "c1")]


def test_a_partly_complete_run_keeps_the_charge(monkeypatch):
    """Real work exists on the campaign, and regenerating from a later agent
    does not re-spend."""
    from app.pipeline import runner

    refunded = []
    monkeypatch.setattr(runner.supabase, "update",
                        lambda t, patch, **kw: {"id": "c1", "business_id": "b1",
                                                "pipeline_outputs": {"research": {}}, **patch})
    monkeypatch.setattr(runner.credits, "refund", lambda *a: refunded.append(a))

    runner._fail("c1", "boom")
    assert refunded == []


def test_the_heartbeat_interval_stays_under_the_reaper_threshold():
    """These two constants live in different modules and only work as a pair.
    If the heartbeat ever ticks slower than the reaper reaps, every campaign
    that runs long enough gets killed while it is working."""
    from app.pipeline import orchestrator, reaper

    assert orchestrator.HEARTBEAT_EVERY * 2 < reaper.STALE_AFTER


def test_a_long_stage_keeps_reporting_progress(monkeypatch):
    """The bug this covers: progress_at was only written when a stage finished,
    so stage 3's four concurrent agents left it silent for the whole stage and
    the reaper reclaimed a healthy run."""
    import threading
    from datetime import timedelta

    from app.pipeline import orchestrator

    beats = []
    beat_happened = threading.Event()
    monkeypatch.setattr(orchestrator, "HEARTBEAT_EVERY", timedelta(seconds=0.01))

    def record(table, patch, **kw):
        beats.append(patch)
        beat_happened.set()

    monkeypatch.setattr(orchestrator.supabase, "update", record)

    with orchestrator._heartbeat("c1"):
        # Stand in for a stage that writes nothing while it runs. Waiting on the
        # event rather than on the clock keeps this from flaking under load.
        assert beat_happened.wait(5), "a long stage produced no heartbeat"

    assert all(set(b) == {"progress_at"} for b in beats)

    # And it must stop the moment the run is over, or a finished campaign would
    # keep claiming to be alive. _heartbeat joins its thread, so this is exact.
    settled = len(beats)
    threading.Event().wait(0.1)
    assert len(beats) == settled


def test_the_heartbeat_gives_up_so_a_wedged_worker_is_still_reaped(monkeypatch):
    """A beating heart proves the process is up, not that work is happening.
    Past the deadline it must go quiet and let the reaper take over."""
    import threading
    from datetime import timedelta

    from app.pipeline import orchestrator

    beats = []
    monkeypatch.setattr(orchestrator, "HEARTBEAT_EVERY", timedelta(seconds=0.01))
    monkeypatch.setattr(orchestrator, "HEARTBEAT_DEADLINE", timedelta(seconds=0.05))
    monkeypatch.setattr(orchestrator.supabase, "update",
                        lambda table, patch, **kw: beats.append(patch))

    with orchestrator._heartbeat("c1"):
        threading.Event().wait(0.3)

    assert 0 < len(beats) < 20, f"expected the heartbeat to stop early, got {len(beats)}"


def test_model_calls_are_capped_however_deeply_the_pools_nest(monkeypatch):
    """A stage runs its agents concurrently and the copy agent fans out inside
    that, so pool sizes multiply. The cap has to hold regardless."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from app.pipeline import llm

    peak, live, lock = 0, 0, threading.Lock()

    def fake_dispatch(resolved, **kw):
        nonlocal peak, live
        with lock:
            live += 1
            peak = max(peak, live)
        threading.Event().wait(0.02)
        with lock:
            live -= 1
        return type("C", (), {"text": "{}", "usage": None})()

    monkeypatch.setattr(llm, "_dispatch", fake_dispatch)
    monkeypatch.setattr(llm.providers, "parse_json", lambda text, model: {})
    monkeypatch.setattr(llm, "cost_gbp", lambda model, completion: 0.0)

    def call():
        return llm.call_json(role="content", system="s", user="u", schema={})

    with ThreadPoolExecutor(max_workers=16) as pool:
        for future in [pool.submit(call) for _ in range(16)]:
            future.result()

    assert peak <= llm.MAX_INFLIGHT, f"{peak} calls in flight, cap is {llm.MAX_INFLIGHT}"
    assert peak > 1, "the cap serialised everything — concurrency is gone"


@pytest.mark.parametrize(
    "error, expected_attempts",
    [
        # A request that already burned the full timeout budget is unlikely to
        # succeed unchanged, so it gets one retry, not two.
        (lambda: __import__("app.pipeline.providers", fromlist=["x"]).ProviderError(
            "kimi-k2.6: APITimeoutError: Request timed out."), 2),
        # A rate limit is genuinely worth backing off into.
        (lambda: __import__("app.pipeline.providers", fromlist=["x"]).ProviderError(
            "kimi-k2.6: 429 rate limited"), 3),
    ],
    ids=["timeout", "rate-limit"],
)
def test_timeouts_get_a_shorter_retry_budget_than_other_transients(
    monkeypatch, error, expected_attempts
):
    """Every strategy call was hitting a 120s ceiling and then being retried
    into it, turning one slow agent into six minutes of dead time."""
    from app.pipeline import llm

    attempts = []

    def fake_dispatch(resolved, **kw):
        attempts.append(1)
        raise error()

    monkeypatch.setattr(llm, "_dispatch", fake_dispatch)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    with pytest.raises(llm.LLMError):
        llm.call_json(role="strategy", system="s", user="u", schema={})

    assert len(attempts) == expected_attempts


def test_an_unparseable_body_is_retried_but_a_bad_request_is_not(monkeypatch):
    """A live run lost three of eight fan-out legs to unparseable responses that
    were classified as deterministic and dropped without a second attempt."""
    from app.pipeline import llm, providers

    cases = {
        # Schema compliance is stochastic, so this gets the full budget: a
        # third attempt is cheap next to losing the call.
        "kimi-k2.5: response was not valid JSON (Expecting value); got: '<empty>'": 3,
        # A runaway sample, not a prompt that outgrew the cap — normal outputs
        # sit at 5-30% of it. Worth one fresh sample, but each attempt costs a
        # full max_tokens generation, so only one.
        "kimi-k2.5: hit max_tokens (16000) — output truncated": 2,
        # Genuinely request-shaped: the same prompt earns the same refusal.
        "kimi-k2.5 declined the request (policy)": 1,
    }

    for message, expected in cases.items():
        attempts = []

        def fake_dispatch(resolved, **kw):
            attempts.append(1)
            raise providers.ProviderError(message)

        monkeypatch.setattr(llm, "_dispatch", fake_dispatch)
        monkeypatch.setattr(llm.time, "sleep", lambda s: None)

        with pytest.raises(llm.LLMError):
            llm.call_json(role="content", system="s", user="u", schema={})

        assert len(attempts) == expected, f"{message!r} made {len(attempts)} attempts"


def test_a_json_failure_reports_what_came_back():
    """'Expecting value: line 1 column 1' alone cannot tell an empty body from
    prose from a truncated object, and those want different fixes."""
    from app.pipeline import providers

    with pytest.raises(providers.ProviderError) as excinfo:
        providers.parse_json("I'd be happy to help with that!", "kimi-k2.5")
    assert "I'd be happy to help" in str(excinfo.value)

    with pytest.raises(providers.ProviderError) as excinfo:
        providers.parse_json("   ", "kimi-k2.5")
    assert "<empty>" in str(excinfo.value)


def test_a_role_cannot_be_pointed_at_a_model_that_ignores_the_schema(monkeypatch):
    """kimi-k2.5 sat in MODELS under a comment claiming every entry was
    confirmed to honour strict JSON Schema. It was never checked, and it
    answered 6 of 10 fan-out legs in Markdown — assets written, then dropped.
    The claim is now enforced rather than asserted in a comment."""
    from app.pipeline import llm

    # Patch llm.Config, not app.config.Config. A test above reloads the config
    # module, which mints a fresh Config class; llm still holds a reference to
    # the original, so patching the module attribute patches an object the code
    # under test never reads.
    monkeypatch.setattr(llm.Config, "MODEL_CONTENT", "kimi-k2.5")
    with pytest.raises(llm.LLMError) as excinfo:
        llm.resolve_role("content")

    message = str(excinfo.value)
    assert "MODEL_CONTENT" in message, "the error must name the var to change"
    assert "kimi-k2.6" in message, "the error must name a model that works"


def test_every_default_role_model_honours_the_schema():
    """The shipped defaults must work out of the box — an operator who sets no
    model env vars at all should not silently lose their creative assets."""
    from app.pipeline import llm

    for role in ("strategy", "content"):
        resolved = llm.resolve_role(role)
        assert llm.MODELS[resolved.model].honours_schema




def test_every_agent_is_told_to_return_json(monkeypatch):
    """No prompt in specs/ mentions JSON, a schema, or an output shape — the
    only signal was the response_format parameter, which Moonshot treats as a
    hint. Three agents came back as Markdown documents using the schema's field
    names as headings, strategy on all three attempts. The contract is appended
    centrally so a new spec cannot forget it."""
    from app.pipeline import agents

    seen = {}

    def fake_call_json(*, role, system, user, schema, schema_name, **kw):
        seen["system"] = system
        return {}, 0.0, type("R", (), {"provider": "kimi", "model": "kimi-k2.6"})()

    monkeypatch.setattr(agents, "call_json", fake_call_json)

    spec = agents.registry()["strategy"]
    agents.run(spec, {k: "" for k in _template_keys(spec.user_template)})

    assert agents.OUTPUT_CONTRACT in seen["system"]
    assert spec.system in seen["system"], "the agent's own prompt must survive"


def _template_keys(template: str) -> set:
    import string

    return {f for _, f, _, _ in string.Formatter().parse(template) if f}




# ── Video renders (fal.ai) ──────────────────────────────────────────────────
def test_only_video_assets_are_queued_and_never_twice(monkeypatch):
    """Re-rendering an asset should replace its video, not queue a second render
    beside the first and race it to the same upload path."""
    from app.render import queue

    inserted = []
    monkeypatch.setattr(queue.supabase, "insert",
                        lambda t, rows: inserted.extend(rows) or rows)

    def fake_select(table, *, params=None, **kw):
        if table == "content_assets":
            return [
                {"id": "a1", "format": "b-roll-montage", "content": {}, "image_brief": "b"},
                {"id": "a2", "format": "quote-card", "content": {}, "image_brief": "b"},
                {"id": "a3", "format": "long-form-email", "content": {}, "image_brief": ""},
                {"id": "a4", "format": "ugc-testimonial", "content": {}, "image_brief": "b"},
            ]
        return [{"asset_id": "a4"}]  # already queued

    monkeypatch.setattr(queue.supabase, "select", fake_select)

    queue.enqueue_campaign("c1")
    queued = {row["asset_id"] for row in inserted}
    assert queued == {"a1"}, f"queued {queued}: expected only the un-queued video asset"
    assert inserted[0]["aspect_ratio"] == "9:16"


def test_an_abandoned_render_goes_back_on_the_queue(monkeypatch):
    """Render redeploys mid-render exactly as the old GPU box got interrupted
    mid-render. Nothing else will ever move a row left in `claimed`."""
    from datetime import datetime, timedelta, timezone

    from app.render import queue

    now = datetime.now(timezone.utc)
    patches = []
    monkeypatch.setattr(queue.supabase, "select", lambda t, **kw: [
        {"id": "dead", "attempts": 0, "claimed_by": "w1",
         "progress_at": (now - timedelta(minutes=30)).isoformat()},
        {"id": "alive", "attempts": 0, "claimed_by": "w2",
         "progress_at": (now - timedelta(seconds=10)).isoformat()},
    ])
    monkeypatch.setattr(queue.supabase, "update",
                        lambda t, patch, **kw: patches.append((kw["params"]["id"], patch))
                        or [{"id": "x"}])

    assert queue.reap(now=now) == 1
    job_id, patch = patches[0]
    assert job_id == "eq.dead", "a render still beating lost its job"
    assert patch["status"] == "queued" and patch["claimed_by"] is None


def test_a_render_that_keeps_failing_is_eventually_called_dead(monkeypatch):
    """fal bills per render. An asset that always fails must not cycle the queue
    forever spending real money on the same failure."""
    from datetime import datetime, timedelta, timezone

    from app.render import queue

    now = datetime.now(timezone.utc)
    patches = []
    monkeypatch.setattr(queue.supabase, "select", lambda t, **kw: [{
        "id": "j1", "attempts": queue.MAX_ATTEMPTS - 1, "claimed_by": "w1",
        "progress_at": (now - timedelta(hours=1)).isoformat(),
    }])
    monkeypatch.setattr(queue.supabase, "update",
                        lambda t, patch, **kw: patches.append(patch) or [{"id": "j1"}])

    queue.reap(now=now)
    assert patches[0]["status"] == "error"


def test_losing_the_lease_mid_render_does_not_overwrite_the_new_owner(monkeypatch):
    """If a job was reaped and re-claimed while fal was still rendering, this
    thread must abandon it — not upload over whoever owns it now, and not mark
    it failed underneath them."""
    from app.render import queue

    monkeypatch.setattr(queue.fal, "submit", lambda model, payload: "req-1")
    monkeypatch.setattr(queue, "heartbeat", lambda job_id, worker_id: False)

    def fake_wait(model, request_id, *, on_progress=None):
        on_progress()          # lease is gone; this must raise
        raise AssertionError("kept rendering after losing the lease")

    monkeypatch.setattr(queue.fal, "wait", fake_wait)

    wrote = []
    monkeypatch.setattr(queue, "complete", lambda *a: wrote.append(("complete", a)))
    monkeypatch.setattr(queue, "fail", lambda *a: wrote.append(("fail", a)))

    queue.process({"id": "j1", "campaign_id": "c1", "prompt": {}, "aspect_ratio": "9:16"}, "w1")
    assert wrote == [], f"a lost lease still wrote to the job: {wrote}"


def test_a_failed_render_records_why(monkeypatch):
    """The GPU worker's failures arrived as '500 Server Error' and nothing else.
    A render that fails should say what happened, on the row, where the owner's
    status endpoint will show it."""
    from app.render import queue

    monkeypatch.setattr(queue.fal, "submit",
                        lambda model, payload: (_ for _ in ()).throw(
                            queue.fal.FalError("422 prompt rejected")))
    monkeypatch.setattr(queue, "heartbeat", lambda *a: True)

    failures = []
    monkeypatch.setattr(queue, "fail", lambda job_id, worker, reason: failures.append(reason))

    queue.process({"id": "j1", "campaign_id": "c1", "prompt": {}, "aspect_ratio": "9:16"}, "w1")
    assert failures and "422 prompt rejected" in failures[0]


def test_the_brief_reaches_fal_intact(monkeypatch):
    """The brief is written for a human with a camera, which is already close to
    what a video model wants. It should arrive as prose, not keyword soup."""
    from app.render import queue

    job = {
        "id": "j1", "campaign_id": "c1", "aspect_ratio": "9:16",
        "prompt": {"brief": 'Straight-down shot of an open sketchbook, "no people".',
                   "guidance": "Vertical, 9:16."},
    }
    payload = queue._fal_payload(job)
    assert "sketchbook" in payload["prompt"]
    assert payload["aspect_ratio"] == "9:16"
    # Spelled per model: Veo wants "6s", wan wants 6. Either is a duration.
    assert payload["duration"] in (6, "6s")


def test_a_render_can_be_limited_to_one(monkeypatch):
    """Every clip costs money, so committing to a whole campaign's worth before
    seeing one is a bad default."""
    from app.render import queue

    inserted = []
    monkeypatch.setattr(queue.supabase, "insert",
                        lambda t, rows: inserted.extend(rows) or rows)
    monkeypatch.setattr(queue.supabase, "select", lambda table, *, params=None, **kw: (
        [{"id": f"a{i}", "format": "b-roll-montage", "content": {}, "image_brief": "b"}
         for i in range(5)] if table == "content_assets" else []
    ))

    queue.enqueue_campaign("c1", limit=1)
    assert len(inserted) == 1

    inserted.clear()
    queue.enqueue_campaign("c1")
    assert len(inserted) == 5, "no limit should still queue everything"


def test_a_montage_brief_is_reduced_to_one_shot():
    """The copy agent writes for a human with a camera and an editor; fal
    renders one continuous clip. Handing it "six to eight shots, in order"
    asks for eight things at once and gets mush."""
    from app.render.queue import first_shot

    montage = ("9:16. Six to eight shots at ~2s each, in order: "
               "(1) Hands feeding sourdough starter at dawn, warm window light. "
               "(2) Close-up of local butter and free-range eggs on a steel counter. "
               "(3) A whisk folding batter.")
    out = first_shot(montage)
    assert "sourdough starter at dawn" in out
    assert "butter" not in out and "whisk" not in out, f"kept later shots: {out}"
    assert "9:16" in out, "style preamble should survive"
    assert "Six to eight shots" not in out, "sequence instruction should not"

    numbered = ("Shot 1: Hands tipping flour onto a workbench, morning light. (~2s) "
                "Shot 2: Sourdough starter bubbling in a jar. (~2s)")
    assert "tipping flour" in first_shot(numbered)
    assert "bubbling" not in first_shot(numbered)

    # Already one shot — founder-piece says so outright — must pass through.
    single = ("Medium shot of the founder carrying a finished cake in a box, "
              "paused in the bakery doorway. Static camera, no movement. "
              "One continuous shot.")
    assert first_shot(single) == single


def test_queue_endpoints_are_addressed_by_app_id_not_model_path():
    """You submit to the full model path but poll the app. Polling the full
    path returns 405, which reads like a broken request rather than a wrong
    URL — it cost three attempts and a failed job to find."""
    from app.render.fal import app_id

    assert app_id("fal-ai/wan-25-preview/text-to-video") == "fal-ai/wan-25-preview"
    assert app_id("fal-ai/flux/dev") == "fal-ai/flux"
    assert app_id("fal-ai/kling-video") == "fal-ai/kling-video"


def test_a_format_offering_two_aspect_ratios_resolves_to_one():
    """founder-piece is "9:16 or 1:1" because a human shooting it can choose.
    A renderer cannot, and the whole phrase is not a request fal can honour."""
    from app.render.queue import aspect_ratio

    assert aspect_ratio("9:16 or 1:1") == "9:16"
    assert aspect_ratio("16:9") == "16:9"
    assert aspect_ratio("") == "9:16"


def test_a_job_can_override_the_configured_model(monkeypatch):
    """Models differ by an order of magnitude in price and in how closely they
    follow direction. Comparing them on the same brief beats picking one from a
    pricing table, which is how the first one got chosen."""
    from app.render import queue

    inserted = []
    monkeypatch.setattr(queue.supabase, "insert",
                        lambda t, rows: inserted.extend(rows) or rows)
    monkeypatch.setattr(queue.supabase, "select", lambda table, *, params=None, **kw: (
        [{"id": "a1", "format": "founder-piece", "content": {}, "image_brief": "b"}]
        if table == "content_assets" else []))

    queue.enqueue_campaign("c1", model="fal-ai/veo3/fast")
    assert inserted[0]["prompt"]["model"] == "fal-ai/veo3/fast"

    submitted = []
    monkeypatch.setattr(queue.fal, "submit",
                        lambda model, payload: submitted.append(model) or "req-1")
    monkeypatch.setattr(queue, "heartbeat", lambda *a: True)
    monkeypatch.setattr(queue.fal, "wait", lambda *a, **kw: {})
    monkeypatch.setattr(queue.fal, "video_bytes", lambda r: b"x")
    monkeypatch.setattr(queue.supabase, "signed_upload_url", lambda b, p: {"url": "http://x"})
    monkeypatch.setattr(queue.requests, "put",
                        lambda *a, **kw: type("R", (), {"status_code": 200})())
    monkeypatch.setattr(queue, "complete", lambda *a: None)

    queue.process(inserted[0] | {"id": "j1", "campaign_id": "c1"}, "w1")
    assert submitted == ["fal-ai/veo3/fast"], "the job's model was ignored"


def test_clip_length_is_spelled_the_way_each_model_expects():
    """Models do not share a request schema and the differences are undocumented
    centrally — Veo rejected `duration: 5` with "should be '4s', '6s' or '8s'".
    Each case here was learned from a real 422."""
    from app.render.queue import duration_field

    # Veo: string, from a fixed set, snapped to the nearest rather than refused.
    assert duration_field("fal-ai/veo3/fast", 5) == {"duration": "4s"}
    assert duration_field("fal-ai/veo3/fast", 7) == {"duration": "6s"}
    assert duration_field("fal-ai/veo3/fast", 8) == {"duration": "8s"}

    # Anything else keeps the plain integer that wan accepts.
    assert duration_field("fal-ai/wan-25-preview/text-to-video", 5) == {"duration": 5}


def test_video_briefs_ask_for_one_clip_not_an_edit():
    """These formats are rendered by a video model now. The guidance used to
    ask for beat sheets, timecodes and 30-60 second takes — direction for a
    person with an editor — and the first real render came back as a still-life
    because the brief described a photograph."""
    from app.pipeline.formats import FORMATS

    for fid, fmt in FORMATS.items():
        if fmt["category"] != "video":
            continue
        guidance = fmt["brief_guidance"].lower()

        # Assert on what is *asked for*, not on which words appear. The first
        # version of this test failed the new guidance for containing
        # "timecode" — in the phrase "no timecodes". A bare substring cannot
        # tell a demand from a prohibition.
        for asked in ("write a shot-by-shot beat sheet",
                      "beat sheet with a cut",
                      "30-60 seconds",
                      "under 20 seconds"):
            assert asked not in guidance, f"{fid} still asks for {asked!r}"

        assert "continuous" in guidance or "stand alone" in guidance, (
            f"{fid} does not say the shot must be one continuous moment")


def test_the_copy_agent_is_told_video_is_rendered_not_filmed():
    """Nothing in this prompt knew these formats stopped being shot by a crew.
    That is where 'lifestyle photography style, soft focus background' came
    from, and a brief describing a photograph renders as one."""
    import json
    from pathlib import Path

    spec = json.loads(
        (Path(__file__).resolve().parents[1] / "app/pipeline/specs/copy_social.json").read_text()
    )
    system = spec["system"].lower()
    assert "photography style" in system, "the banned phrase must be named to be banned"
    assert "single continuous clip" in system
    assert "no editor" in system


def test_specific_assets_can_be_rendered(monkeypatch):
    """Whichever asset a campaign happens to list first is not usually the one
    you want to re-render — the campaign view already offers per-asset actions."""
    from app.render import queue

    inserted = []
    monkeypatch.setattr(queue.supabase, "insert",
                        lambda t, rows: inserted.extend(rows) or rows)
    monkeypatch.setattr(queue.supabase, "select", lambda table, *, params=None, **kw: (
        [{"id": f"a{i}", "format": "founder-piece", "content": {}, "image_brief": "b"}
         for i in range(4)] if table == "content_assets" else []))

    queue.enqueue_campaign("c1", asset_ids={"a2"})
    assert [r["asset_id"] for r in inserted] == ["a2"]


# ── URL-first onboarding ────────────────────────────────────────────────────
def test_a_user_supplied_url_cannot_reach_our_own_network():
    """The server fetches a URL the user typed. Without this it is a
    server-side request forgery hole: a link to the cloud metadata endpoint
    asks our infrastructure to read its own credentials and hand them back."""
    from app.research import site

    for hostile in (
        "http://169.254.169.254/latest/meta-data/",   # cloud metadata
        "http://127.0.0.1:8188/",                     # loopback
        "http://192.168.1.1/",                        # private range
        "http://10.0.0.5/",
        "file:///etc/passwd",                         # not http at all
        "http://localhost/",
    ):
        with pytest.raises(site.SiteError):
            site.normalise(hostile)

    # ...while ordinary addresses still work, with or without a scheme.
    assert site.normalise("example.com") == "https://example.com"
    assert site.normalise("https://www.bbc.co.uk").startswith("https://")


def test_a_redirect_to_a_private_address_is_refused(monkeypatch):
    """Checking the URL the user typed is not enough — a public host can
    redirect to a private one, and requests follows redirects by default."""
    from app.research import site

    class FakeResponse:
        status_code = 200
        url = "http://169.254.169.254/latest/meta-data/"
        headers = {"Content-Type": "text/html"}
        encoding = "utf-8"
        raw = type("R", (), {"read": staticmethod(lambda *a, **kw: b"<html></html>")})()

    monkeypatch.setattr(site.requests, "get", lambda *a, **kw: FakeResponse())
    with pytest.raises(site.SiteError):
        site._fetch("https://example.com")


def test_a_site_with_almost_no_text_falls_back_rather_than_pretending(monkeypatch):
    """An image-only or JavaScript-only site yields a few words. Feeding that to
    the pipeline and calling it research produces a confident campaign built on
    nothing; saying so sends the owner to the form instead."""
    from app.research import site

    monkeypatch.setattr(site, "_fetch", lambda url: "<html><title>Hi</title><body>Welcome</body></html>")
    with pytest.raises(site.SiteError, match="not enough text"):
        site.read("https://example.com")


def test_the_profile_draft_matches_the_onboarding_shape():
    """A drafted profile and a hand-filled one must be the same shape, or
    everything downstream has two cases to handle."""
    from app.research.profile import SCHEMA

    props = SCHEMA["properties"]
    for column in ("name", "industry", "location", "offer_description", "price_point"):
        assert column in props, f"{column} is a businesses column and must be drafted"

    voice = props["brand_voice"]["properties"]
    assert set(voice) == {"tone", "energy", "humor", "authority", "detail"}, (
        "brand_voice must match the onboarding quiz keys"
    )
    # Constrained to the same options the quiz offers, so a drafted voice can be
    # edited with the same toggles.
    assert voice["tone"]["enum"] == ["Formal", "Conversational", "Casual"]

# ── Ready-to-post packages ──────────────────────────────────────────────────

def test_ready_to_post_caption_joins_hook_body_cta_and_hashtags():
    from app.render import ready

    asset = {
        "content": {
            "hook": "Stop overpaying for wedding cakes",
            "body": "We quote the same week you enquire.",
            "cta": "Book a tasting",
            "hashtags": ["WeddingCake", "#ManchesterBaker"],
        }
    }
    caption = ready.caption_for(asset)
    assert "Stop overpaying" in caption
    assert "Book a tasting" in caption
    assert "#WeddingCake" in caption
    assert "#ManchesterBaker" in caption


def test_ready_to_post_prefers_edited_copy():
    from app.render import ready

    asset = {
        "content": {"hook": "Old", "body": "B", "cta": "C", "hashtags": []},
        "edited_content": {"hook": "New hook", "body": "B", "cta": "C", "hashtags": []},
    }
    assert ready.caption_for(asset).startswith("New hook")


def test_ready_to_post_overlay_is_short():
    from app.render import ready

    long_hook = "A" * 80
    assert len(ready.overlay_for({"content": {"hook": long_hook}})) <= ready.OVERLAY_LIMIT


def test_pack_campaign_marks_ready_only_with_a_signed_video(monkeypatch):
    from app.render import ready

    assets = [{
        "id": "a1", "format": "ugc-testimonial", "channel": "tiktok",
        "variant": 1, "status": "generated",
        "content": {"hook": "H", "body": "B", "cta": "C", "hashtags": ["x"]},
        "edited_content": None,
    }]
    jobs = [{
        "asset_id": "a1", "status": "done", "video_path": "c1/j1.mp4",
        "updated_at": "2026-09-07T12:00:00Z", "error": None,
    }]

    monkeypatch.setattr(ready.supabase, "select",
                        lambda table, **kw: assets if table == "content_assets" else jobs)
    monkeypatch.setattr(ready.supabase, "signed_download_url",
                        lambda bucket, path: f"https://signed.example/{path}")

    packed = ready.pack_campaign("c1")
    assert len(packed) == 1
    assert packed[0]["ready"] is True
    assert packed[0]["platform"] == "tiktok"
    assert packed[0]["video_url"].endswith("c1/j1.mp4")
    assert packed[0]["caption"].startswith("H")


def test_pack_campaign_without_a_render_is_not_ready(monkeypatch):
    from app.render import ready

    assets = [{
        "id": "a1", "format": "founder-piece", "channel": "instagram",
        "variant": 1, "status": "generated",
        "content": {"hook": "H", "body": "", "cta": "", "hashtags": []},
    }]
    monkeypatch.setattr(ready.supabase, "select",
                        lambda table, **kw: assets if table == "content_assets" else [])
    packed = ready.pack_campaign("c1")
    assert packed[0]["ready"] is False
    assert packed[0]["render_status"] == "not_queued"
    assert packed[0]["video_url"] is None

