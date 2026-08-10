"""Environment configuration. Fails loudly at boot on missing required vars."""
import logging
import os
from urllib.parse import urlparse


class ConfigError(RuntimeError):
    pass


def _req(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _opt(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _project_origin(raw: str) -> str:
    """Reduce a Supabase URL to its origin.

    Supabase's dashboard shows several URLs — the project URL, the REST URL
    (`.../rest/v1/`), the GraphQL one — and pasting the wrong one is easy. The
    consequence is severe and unobvious: the JWKS URL becomes
    `<project>/rest/v1/auth/v1/.well-known/jwks.json`, which returns 401, so key
    fetch fails and *every* valid token is rejected as invalid. Normalising here
    means the app works whichever URL was pasted.
    """
    raw = raw.strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlparse(raw if "//" in raw else f"https://{raw}")
    if not parsed.netloc:
        return raw
    if parsed.path:
        logging.getLogger(__name__).warning(
            "[config] SUPABASE_URL had a path (%r) — using the origin instead. "
            "Set it to https://%s", parsed.path, parsed.netloc,
        )
    return f"{parsed.scheme or 'https'}://{parsed.netloc}"


class Config:
    # ── Supabase ────────────────────────────────────────────────────────────
    SUPABASE_URL = _project_origin(_opt("SUPABASE_URL"))
    # Service role: bypasses RLS. Never expose this to the browser.
    SUPABASE_SERVICE_KEY = _opt("SUPABASE_SERVICE_KEY")
    # Legacy HS256 verification. Unset when the project uses asymmetric JWTs,
    # in which case auth.py falls back to the JWKS endpoint.
    SUPABASE_JWT_SECRET = _opt("SUPABASE_JWT_SECRET")

    # ── Models ──────────────────────────────────────────────────────────────
    # The pipeline asks for a *role*; config decides the model, and the model
    # decides the provider (see llm.MODELS). The two roles can sit on different
    # providers — that is what makes a bake-off an env change.
    #
    # The blueprint's split: a strong model for strategy, a cheap one for the
    # content fan-out.
    MODEL_STRATEGY = _opt("MODEL_STRATEGY", "kimi-k2.6")
    # Was kimi-k2.5, which is cheaper and ignores the JSON schema often enough
    # to lose most of a campaign's creative assets. See llm.MODELS.
    MODEL_CONTENT = _opt("MODEL_CONTENT", "kimi-k2.6")

    # Temperature applies to OpenAI-compatible providers. Anthropic's current
    # models reject sampling parameters, so it is not sent there.
    LLM_TEMPERATURE = float(_opt("LLM_TEMPERATURE", "0.6"))
    # Every call is schema-constrained extraction or drafting, so thinking earns
    # nothing here and would skew a bake-off on latency and output tokens.
    ANTHROPIC_THINKING = _opt("ANTHROPIC_THINKING", "disabled")

    # ── Provider credentials ────────────────────────────────────────────────
    # MOONSHOT_API_KEY is accepted as an alias for KIMI_API_KEY because that is
    # the name Moonshot's own docs use.
    KIMI_API_KEY = _opt("KIMI_API_KEY") or _opt("MOONSHOT_API_KEY")
    KIMI_BASE_URL = _opt("KIMI_BASE_URL", "https://api.moonshot.ai/v1")
    ANTHROPIC_API_KEY = _opt("ANTHROPIC_API_KEY")
    OPENAI_API_KEY = _opt("OPENAI_API_KEY")
    OPENAI_BASE_URL = _opt("OPENAI_BASE_URL", "https://api.openai.com/v1")
    OPENROUTER_API_KEY = _opt("OPENROUTER_API_KEY")
    OPENROUTER_BASE_URL = _opt("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

    _ROLE_MODELS = {"strategy": "MODEL_STRATEGY", "content": "MODEL_CONTENT"}

    @classmethod
    def model_for_role(cls, role: str) -> str:
        try:
            return getattr(cls, cls._ROLE_MODELS[role])
        except KeyError:
            raise ConfigError(
                f"unknown pipeline role {role!r}; expected one of "
                f"{', '.join(sorted(cls._ROLE_MODELS))}"
            ) from None

    @classmethod
    def require_key(cls, provider: str) -> str:
        env_name = {
            "kimi": "KIMI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
        }.get(provider)
        if not env_name:
            raise ConfigError(f"unknown provider {provider!r}")
        key = getattr(cls, env_name, "")
        if not key:
            raise ConfigError(
                f"{env_name} is not set, but a pipeline role is configured to "
                f"use {provider}"
            )
        return key

    @classmethod
    def openai_compatible_credentials(cls, provider: str) -> tuple[str, str]:
        base = {
            "kimi": cls.KIMI_BASE_URL,
            "openai": cls.OPENAI_BASE_URL,
            "openrouter": cls.OPENROUTER_BASE_URL,
        }.get(provider)
        if not base:
            raise ConfigError(f"{provider!r} is not an OpenAI-compatible provider")
        return cls.require_key(provider), base

    # ── Video rendering ─────────────────────────────────────────────────────
    # fal.ai renders the video. This replaced a rented GPU box: £54/month
    # whether or not anything rendered, against roughly $0.15 a clip and
    # nothing at all when idle. Break-even was somewhere near 450 clips a
    # month, which is a long way from where this product is.
    FAL_KEY = _opt("FAL_KEY")
    # Veo, on evidence rather than a pricing table. Side by side on the same brief
    # it followed direction better and rendered in 48s against wan's 213s — the
    # premium model is the faster one, so cost is the only trade.
    FAL_VIDEO_MODEL = _opt("FAL_VIDEO_MODEL", "fal-ai/veo3/fast")
    # Veo allows 4, 6 or 8; other models take any integer. See render.queue.
    FAL_VIDEO_SECONDS = int(_opt("FAL_VIDEO_SECONDS", "6"))

    # ── Stripe ──────────────────────────────────────────────────────────────
    STRIPE_SECRET_KEY = _opt("STRIPE_SECRET_KEY")
    STRIPE_WEBHOOK_SECRET = _opt("STRIPE_WEBHOOK_SECRET")

    # ── App ─────────────────────────────────────────────────────────────────
    # Browsers send the Origin header without a trailing slash, so an entry
    # written as "https://example.com/" matches nothing and every cross-origin
    # request is blocked — with no server-side trace, because the browser never
    # sends it. Pasting a URL from the address bar produces exactly that, so
    # normalise rather than expect people to notice.
    CORS_ORIGINS = [
        o.strip().rstrip("/")
        for o in _opt("CORS_ORIGINS", "http://localhost:3000").split(",")
        if o.strip().rstrip("/")
    ]
    ENV = _opt("FLASK_ENV", "development")

    @classmethod
    def validate(cls) -> list[str]:
        """Return a list of warnings for anything unset. Boot proceeds anyway so
        the skeleton is runnable before every key exists (Week 1)."""
        warnings = []
        for name in (
            "SUPABASE_URL",
            "SUPABASE_SERVICE_KEY",
            "KIMI_API_KEY",
            "STRIPE_SECRET_KEY",
            "STRIPE_WEBHOOK_SECRET",
        ):
            if not getattr(cls, name):
                warnings.append(name)
        if not cls.SUPABASE_JWT_SECRET:
            warnings.append("SUPABASE_JWT_SECRET (will use JWKS instead)")
        return warnings
