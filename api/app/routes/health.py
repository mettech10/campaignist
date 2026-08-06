from urllib.parse import urlparse

from flask import Blueprint, jsonify

from ..config import Config
from ..pipeline import llm

bp = Blueprint("health", __name__)


@bp.get("/api/health")
def health():
    """Which integrations are wired up, and what each pipeline role resolves to."""
    roles = {}
    for role in ("strategy", "content"):
        try:
            resolved = llm.resolve_role(role)
            roles[role] = {
                "model": resolved.model,
                "provider": resolved.provider,
                "key_present": bool(_provider_key(resolved.provider)),
            }
        except Exception as e:
            roles[role] = {"error": str(e)}

    return jsonify(
        status="ok",
        env=Config.ENV,
        models=roles,
        integrations={
            "supabase": bool(Config.SUPABASE_URL and Config.SUPABASE_SERVICE_KEY),
            "stripe": bool(Config.STRIPE_SECRET_KEY),
            "stripe_webhook": bool(Config.STRIPE_WEBHOOK_SECRET),
        },
        # A boolean cannot show that SUPABASE_URL points at the *wrong* project,
        # which presents as every valid token being rejected: JWKS returns a
        # different signing key, so signature verification fails. The host is
        # already public (the browser bundle carries it), so surfacing it costs
        # nothing and makes that misconfiguration self-diagnosing.
        supabase_host=urlparse(Config.SUPABASE_URL).netloc or None,
        # Which scheme incoming tokens will be verified under.
        jwt_verification="HS256 (shared secret)" if Config.SUPABASE_JWT_SECRET
        else "JWKS (asymmetric)",
    )


def _provider_key(provider: str) -> str:
    try:
        return Config.require_key(provider)
    except Exception:
        return ""
