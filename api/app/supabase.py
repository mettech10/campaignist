"""Thin PostgREST wrapper using the service-role key.

Same shape as Metalyzi's `_sb_headers()` helpers, factored into one place. The
service role bypasses RLS, so every query here MUST scope by user_id or
business_id explicitly — RLS will not save us on this path.
"""
import logging

import requests

from .config import Config

log = logging.getLogger(__name__)
TIMEOUT = 10


class SupabaseError(RuntimeError):
    pass


def _headers(extra: dict | None = None) -> dict:
    return {
        "apikey": Config.SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {Config.SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        **(extra or {}),
    }


def _url(table: str) -> str:
    if not Config.SUPABASE_URL:
        raise SupabaseError("SUPABASE_URL is not configured")
    return f"{Config.SUPABASE_URL}/rest/v1/{table}"


def _check(resp: requests.Response) -> None:
    if resp.status_code >= 400:
        raise SupabaseError(f"{resp.status_code} {resp.text[:300]}")


def select(table: str, *, params: dict | None = None, single: bool = False):
    """params use PostgREST syntax, e.g. {'id': 'eq.<uuid>', 'select': 'id,name'}"""
    resp = requests.get(_url(table), params=params or {}, headers=_headers(), timeout=TIMEOUT)
    _check(resp)
    rows = resp.json()
    if single:
        return rows[0] if rows else None
    return rows


def insert(table: str, row: dict | list[dict], *, returning: bool = True):
    prefer = "return=representation" if returning else "return=minimal"
    resp = requests.post(
        _url(table), json=row, headers=_headers({"Prefer": prefer}), timeout=TIMEOUT
    )
    _check(resp)
    if not returning:
        return None
    rows = resp.json()
    return rows[0] if isinstance(row, dict) and rows else rows


def update(table: str, patch: dict, *, params: dict, returning: bool = True):
    if not params:
        raise SupabaseError("update() requires a filter — refusing to update every row")
    prefer = "return=representation" if returning else "return=minimal"
    resp = requests.patch(
        _url(table), params=params, json=patch,
        headers=_headers({"Prefer": prefer}), timeout=TIMEOUT,
    )
    _check(resp)
    if not returning:
        return None
    rows = resp.json()
    return rows[0] if rows else None


def upsert(table: str, row: dict, *, on_conflict: str | None = None):
    params = {"on_conflict": on_conflict} if on_conflict else {}
    resp = requests.post(
        _url(table), params=params, json=row,
        headers=_headers({"Prefer": "resolution=merge-duplicates,return=representation"}),
        timeout=TIMEOUT,
    )
    _check(resp)
    rows = resp.json()
    return rows[0] if rows else None


def delete(table: str, *, params: dict) -> None:
    if not params:
        raise SupabaseError("delete() requires a filter — refusing to delete every row")
    resp = requests.delete(
        _url(table), params=params,
        headers=_headers({"Prefer": "return=minimal"}), timeout=TIMEOUT,
    )
    _check(resp)


def rpc(fn: str, args: dict | None = None):
    resp = requests.post(
        f"{Config.SUPABASE_URL}/rest/v1/rpc/{fn}",
        json=args or {}, headers=_headers(), timeout=TIMEOUT,
    )
    _check(resp)
    return resp.json() if resp.text else None


# ── Ownership helpers ───────────────────────────────────────────────────────
# The service role sees everything, so these are the real access checks.

def business_owned_by(business_id: str, user_id: str) -> bool:
    row = select(
        "businesses",
        params={"id": f"eq.{business_id}", "user_id": f"eq.{user_id}", "select": "id"},
        single=True,
    )
    return row is not None


def campaign_for_user(campaign_id: str, user_id: str) -> dict | None:
    """Return the campaign only if the caller owns the business behind it."""
    rows = select(
        "campaigns",
        params={
            "id": f"eq.{campaign_id}",
            "select": "*,businesses!inner(id,user_id)",
            "businesses.user_id": f"eq.{user_id}",
        },
    )
    return rows[0] if rows else None
