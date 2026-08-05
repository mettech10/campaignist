"""Credit system — ported from Metalyzi.

profiles.credits is the balance; credit_ledger is the append-only audit trail.
The spend is done inside a Postgres function so the decrement and the ledger
row cannot drift apart under concurrent requests.
"""
import logging

from . import supabase
from .supabase import SupabaseError

log = logging.getLogger(__name__)

# Plan → monthly campaign credits. Mirrors BLUEPRINT.md §9.
PLAN_CREDITS = {
    "trial": 1,
    "starter": 5,
    "growth": 20,
    "pro": 200,  # 10 businesses; effectively unmetered at MVP
}


class InsufficientCredits(Exception):
    pass


def balance(user_id: str) -> int:
    row = supabase.select(
        "profiles", params={"id": f"eq.{user_id}", "select": "credits"}, single=True
    )
    return int(row["credits"]) if row else 0


def spend(user_id: str, reason: str, campaign_id: str | None = None) -> int:
    """Consume one credit. Returns the new balance."""
    try:
        return int(
            supabase.rpc(
                "spend_credit",
                {"p_user_id": user_id, "p_reason": reason, "p_campaign_id": campaign_id},
            )
        )
    except SupabaseError as e:
        if "insufficient_credits" in str(e):
            raise InsufficientCredits() from e
        raise


def grant(user_id: str, delta: int, reason: str) -> int:
    """Add credits (renewal, top-up, manual). Returns the new balance.

    Read-then-write: acceptable here because grants come from Stripe webhooks
    and admin actions, which are not concurrent with themselves. If that stops
    being true, move this into a Postgres function like spend_credit.
    """
    current = balance(user_id)
    row = supabase.update(
        "profiles", {"credits": current + delta}, params={"id": f"eq.{user_id}"}
    )
    supabase.insert(
        "credit_ledger",
        {"user_id": user_id, "delta": delta, "reason": reason},
        returning=False,
    )
    return int(row["credits"])


def set_plan(user_id: str, plan: str, reason: str) -> None:
    """Move a user onto a plan and reset their monthly credit allowance."""
    if plan not in PLAN_CREDITS:
        raise ValueError(f"unknown plan: {plan}")
    supabase.update(
        "profiles",
        {"plan": plan, "credits": PLAN_CREDITS[plan]},
        params={"id": f"eq.{user_id}"},
        returning=False,
    )
    supabase.insert(
        "credit_ledger",
        {"user_id": user_id, "delta": PLAN_CREDITS[plan], "reason": reason},
        returning=False,
    )
