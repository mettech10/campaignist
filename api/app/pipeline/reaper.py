"""Recovering campaigns whose worker died mid-generation.

A generation runs in a background thread and the request returns 202
immediately. If the worker restarts — a deploy, an OOM, a host replacement —
that thread dies silently and the campaign sits in `generating` forever with the
user's credit already spent. Nothing marks it failed, and the frontend polls a
status that will never change.

There is no scheduler here on purpose. The check runs when someone reads a
campaign, which is exactly when it matters: the polling UI is the thing that
would otherwise hang. A cron would add infrastructure to solve a problem the
read path already sees.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .. import credits, supabase

log = logging.getLogger(__name__)

# How long a campaign may sit without a new agent output before we call it dead.
# Generous: a single agent call has been measured at ~4 minutes on a reasoning
# model, and a stage runs several concurrently. Too tight and we would kill live
# runs, which is worse than a late reap.
STALE_AFTER = timedelta(minutes=12)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_stale(campaign: dict, now: datetime | None = None) -> bool:
    if campaign.get("status") != "generating":
        return False
    now = now or datetime.now(timezone.utc)
    marker = _parse(campaign.get("progress_at")) or _parse(campaign.get("created_at"))
    if marker is None:
        return False
    if marker.tzinfo is None:
        marker = marker.replace(tzinfo=timezone.utc)
    return now - marker > STALE_AFTER


def reap(campaign: dict) -> dict:
    """Mark a stalled campaign failed and refund the credit. Returns the
    campaign, updated in place if it was reaped."""
    if not is_stale(campaign):
        return campaign

    campaign_id = campaign["id"]
    log.warning("[reaper] campaign %s stalled — marking error and refunding", campaign_id)

    updated = supabase.update(
        "campaigns",
        {
            "status": "error",
            "error": (
                "Generation stopped unexpectedly — the server restarted partway "
                "through. Your credit has been returned."
            ),
        },
        # Only reap a row still in `generating`, so a run that finished between
        # the staleness check and this write is not clobbered.
        params={"id": f"eq.{campaign_id}", "status": "eq.generating"},
    )
    if not updated:
        return campaign  # someone else got there first, or it completed

    business = supabase.select(
        "businesses",
        params={"id": f"eq.{campaign['business_id']}", "select": "user_id"},
        single=True,
    )
    if business:
        try:
            credits.refund(business["user_id"], "stalled_generation", campaign_id)
        except Exception as e:
            # A campaign correctly marked failed is better than one left hanging,
            # so a refund failure is logged rather than raised.
            log.error("[reaper] campaign %s refund failed: %s", campaign_id, e)
    return updated
