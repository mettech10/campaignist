"""Stripe webhook — signature verification ported verbatim from Metalyzi.

Maps Stripe price IDs to plans, then resets the user's monthly credits. The
webhook is the only writer of profiles.plan; the browser cannot set it (see the
narrow profiles_update RLS policy).
"""
import hashlib
import hmac
import logging
import os

import requests
from flask import Blueprint, jsonify, request

from .. import credits, supabase
from ..config import Config

log = logging.getLogger(__name__)
bp = Blueprint("billing", __name__)

# Set these to the live Stripe price IDs once the products exist.
PRICE_TO_PLAN = {
    os.environ.get("STRIPE_PRICE_STARTER", "price_starter"): "starter",
    os.environ.get("STRIPE_PRICE_GROWTH", "price_growth"): "growth",
    os.environ.get("STRIPE_PRICE_PRO", "price_pro"): "pro",
}


def verify_stripe_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    """Header format: t=TIMESTAMP,v1=HMAC_SHA256_HEX,...
    Signed string:  {timestamp}.{raw_body}
    """
    if not signature_header or not secret:
        return False
    try:
        parts: dict[str, list] = {}
        for item in signature_header.split(","):
            key, _, value = item.partition("=")
            parts.setdefault(key.strip(), []).append(value.strip())
        ts = parts.get("t", [""])[0]
        signatures = parts.get("v1", [])
        if not ts or not signatures:
            return False
        signed = f'{ts}.{raw_body.decode("utf-8")}'
        expected = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
        return any(hmac.compare_digest(expected, sig) for sig in signatures)
    except Exception:
        return False


def _stripe_customer_email(customer_id: str) -> str:
    if not Config.STRIPE_SECRET_KEY or not customer_id:
        return ""
    try:
        resp = requests.get(
            f"https://api.stripe.com/v1/customers/{customer_id}",
            auth=(Config.STRIPE_SECRET_KEY, ""),
            timeout=6,
        )
        return resp.json().get("email", "") if resp.ok else ""
    except Exception as e:
        log.warning("[Stripe] customer lookup failed: %s", e)
        return ""


def _user_id_for(customer_id: str, email: str) -> str | None:
    """Find the profile by stripe_customer_id, falling back to email via
    auth.users. The fallback matters for the first event of a new subscriber,
    before stripe_customer_id has been stored."""
    row = supabase.select(
        "profiles",
        params={"stripe_customer_id": f"eq.{customer_id}", "select": "id"},
        single=True,
    )
    if row:
        return row["id"]

    if not email:
        return None
    # GoTrue's admin `filter` is a substring match, not an exact one, so the
    # returned list still has to be checked against the address we want.
    resp = requests.get(
        f"{Config.SUPABASE_URL}/auth/v1/admin/users",
        params={"filter": email, "per_page": 50},
        headers={
            "apikey": Config.SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {Config.SUPABASE_SERVICE_KEY}",
        },
        timeout=8,
    )
    if not resp.ok:
        return None
    wanted = email.strip().lower()
    match = next(
        (u for u in resp.json().get("users", [])
         if (u.get("email") or "").lower() == wanted),
        None,
    )
    if not match:
        return None

    user_id = match["id"]
    supabase.update(
        "profiles", {"stripe_customer_id": customer_id},
        params={"id": f"eq.{user_id}"}, returning=False,
    )
    return user_id


@bp.post("/api/stripe/webhook")
def stripe_webhook():
    raw_body = request.get_data()
    if not verify_stripe_signature(
        raw_body, request.headers.get("Stripe-Signature", ""), Config.STRIPE_WEBHOOK_SECRET
    ):
        log.warning("[Stripe] invalid webhook signature")
        return jsonify(error="invalid_signature"), 400

    event = request.get_json(silent=True) or {}
    event_type = event.get("type", "")
    obj = event.get("data", {}).get("object", {})
    log.info("[Stripe] event: %s", event_type)

    customer_id = obj.get("customer", "")
    email = obj.get("customer_email") or obj.get("metadata", {}).get("email") \
        or _stripe_customer_email(customer_id)

    user_id = _user_id_for(customer_id, email)
    if not user_id:
        # 200 so Stripe stops retrying; the event is logged for manual repair.
        log.warning("[Stripe] no profile for customer=%s email=%s", customer_id, email)
        return jsonify(received=True, matched=False), 200

    if event_type in ("customer.subscription.created", "customer.subscription.updated",
                      "invoice.payment_succeeded"):
        price_id = ""
        items = obj.get("items", {}).get("data", [])
        if items:
            price_id = items[0].get("price", {}).get("id", "")
        elif obj.get("lines"):
            price_id = obj["lines"]["data"][0].get("price", {}).get("id", "")

        plan = PRICE_TO_PLAN.get(price_id)
        if plan:
            credits.set_plan(user_id, plan, f"stripe:{event_type}")
            log.info("[Stripe] user %s → %s", user_id, plan)
        else:
            log.warning("[Stripe] unmapped price id: %s", price_id)

    elif event_type in ("customer.subscription.deleted", "invoice.payment_failed"):
        credits.set_plan(user_id, "trial", f"stripe:{event_type}")
        log.info("[Stripe] user %s downgraded to trial", user_id)

    return jsonify(received=True, matched=True), 200
