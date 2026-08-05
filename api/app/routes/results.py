from datetime import datetime, timezone

from flask import Blueprint, g, jsonify, request

from .. import supabase
from ..auth import require_auth

bp = Blueprint("results", __name__)

METRICS = ("reach", "clicks", "leads", "sales", "revenue")


@bp.post("/api/campaigns/<campaign_id>/results")
@require_auth
def log_results(campaign_id):
    """The 60-second results form. Writing results is what triggers the
    retrospective and the memory write — the retention loop (BLUEPRINT §4)."""
    if not supabase.campaign_for_user(campaign_id, g.user_id):
        return jsonify(error="not_found"), 404

    body = request.get_json(silent=True) or {}
    row = {"campaign_id": campaign_id, "user_notes": body.get("user_notes")}
    for key in METRICS:
        if body.get(key) is not None:
            row[key] = body[key]

    saved = supabase.insert("campaign_results", row)
    # PostgREST sends values as literals, so "now()" would be parsed as a
    # timestamp string and rejected. Send a real ISO timestamp instead.
    supabase.update(
        "campaigns",
        {"status": "completed", "completed_at": datetime.now(timezone.utc).isoformat()},
        params={"id": f"eq.{campaign_id}"}, returning=False,
    )

    # Week 5: retrospective model call → embed → memories table.
    return jsonify(result=saved, retrospective=None), 201


@bp.get("/api/campaigns/<campaign_id>/results")
@require_auth
def get_results(campaign_id):
    if not supabase.campaign_for_user(campaign_id, g.user_id):
        return jsonify(error="not_found"), 404
    return jsonify(
        supabase.select(
            "campaign_results",
            params={"campaign_id": f"eq.{campaign_id}", "order": "reported_at.desc"},
        )
    )
