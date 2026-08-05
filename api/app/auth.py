"""Supabase JWT verification.

The browser holds the Supabase session (same as Metalyzi — auth lives in the
Next.js app). Flask never issues tokens; it only verifies the access token sent
as `Authorization: Bearer <jwt>` and pulls the user id out of `sub`.

Two signing schemes are supported:
  * asymmetric (ES256/RS256) — current Supabase default, keys served from JWKS
  * HS256 — legacy shared secret, used when SUPABASE_JWT_SECRET is set

Set SUPABASE_JWT_SECRET only if the project is still on the legacy scheme.
"""
import functools
import logging

import jwt
from flask import g, jsonify, request
from jwt import PyJWKClient

from .config import Config

log = logging.getLogger(__name__)

_jwks_client: PyJWKClient | None = None


def _jwks() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        # PyJWKClient caches keys in-process and refetches on unknown kid.
        _jwks_client = PyJWKClient(f"{Config.SUPABASE_URL}/auth/v1/.well-known/jwks.json")
    return _jwks_client


def verify_token(token: str) -> dict:
    """Return the decoded claims, or raise jwt.PyJWTError."""
    if Config.SUPABASE_JWT_SECRET:
        return jwt.decode(
            token,
            Config.SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated",
        )
    signing_key = _jwks().get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["ES256", "RS256"],
        audience="authenticated",
    )


def _bearer() -> str | None:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


def require_auth(fn):
    """Populate g.user_id and g.claims, or 401.

    Unlike Metalyzi's subscription check this fails *closed* — an infra problem
    must not hand out authenticated access.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        token = _bearer()
        if not token:
            return jsonify(error="missing_token"), 401
        try:
            claims = verify_token(token)
        except jwt.ExpiredSignatureError:
            return jsonify(error="token_expired"), 401
        except jwt.PyJWTError as e:
            log.warning("[auth] rejected token: %s", e)
            return jsonify(error="invalid_token"), 401
        except Exception as e:  # JWKS fetch failure, misconfiguration
            log.error("[auth] verification error: %s", e)
            return jsonify(error="auth_unavailable"), 503

        user_id = claims.get("sub")
        if not user_id:
            return jsonify(error="invalid_token"), 401

        g.user_id = user_id
        g.claims = claims
        g.email = claims.get("email", "")
        return fn(*args, **kwargs)

    return wrapper
