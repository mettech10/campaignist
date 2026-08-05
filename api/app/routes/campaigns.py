from flask import Blueprint, g, jsonify, request

from .. import credits, supabase
from ..auth import require_auth
from ..pipeline import runner

bp = Blueprint("campaigns", __name__)


@bp.post("/api/campaigns")
@require_auth
def create_campaign():
    """Spend a credit, create the row, kick off generation, return immediately.

    The frontend then polls GET /api/campaigns/<id> and reveals each pipeline
    step as it lands in pipeline_outputs (BLUEPRINT §6, §7 screen 4).
    """
    body = request.get_json(silent=True) or {}
    business_id = body.get("business_id")
    goal = (body.get("goal") or "").strip()

    if not business_id or not goal:
        return jsonify(error="business_id_and_goal_required"), 400
    if not supabase.business_owned_by(business_id, g.user_id):
        return jsonify(error="not_found"), 404

    campaign = supabase.insert(
        "campaigns",
        {
            "business_id": business_id,
            "goal": goal,
            "budget": body.get("budget"),
            "timeframe_days": body.get("timeframe_days", 30),
            "status": "generating",
        },
    )

    # Spend after the row exists so the ledger can point at the campaign, and
    # refund if the user has run out.
    try:
        remaining = credits.spend(g.user_id, "campaign_generation", campaign["id"])
    except credits.InsufficientCredits:
        supabase.update(
            "campaigns", {"status": "error", "error": "insufficient_credits"},
            params={"id": f"eq.{campaign['id']}"}, returning=False,
        )
        return jsonify(error="insufficient_credits"), 402

    runner.start(campaign["id"])
    return jsonify(campaign_id=campaign["id"], status="generating", credits=remaining), 202


@bp.get("/api/campaigns")
@require_auth
def list_campaigns():
    business_id = request.args.get("business_id")
    if business_id:
        if not supabase.business_owned_by(business_id, g.user_id):
            return jsonify(error="not_found"), 404
        params = {"business_id": f"eq.{business_id}"}
    else:
        owned = supabase.select(
            "businesses", params={"user_id": f"eq.{g.user_id}", "select": "id"}
        )
        if not owned:
            return jsonify([])
        ids = ",".join(b["id"] for b in owned)
        params = {"business_id": f"in.({ids})"}

    params |= {
        "select": "id,business_id,goal,status,budget,timeframe_days,created_at,completed_at",
        "order": "created_at.desc",
    }
    return jsonify(supabase.select("campaigns", params=params))


@bp.get("/api/campaigns/<campaign_id>")
@require_auth
def get_campaign(campaign_id):
    campaign = supabase.campaign_for_user(campaign_id, g.user_id)
    if not campaign:
        return jsonify(error="not_found"), 404
    campaign.pop("businesses", None)  # join artefact from the ownership check
    return jsonify(campaign)


@bp.post("/api/campaigns/<campaign_id>/regenerate")
@require_auth
def regenerate_step(campaign_id):
    """Re-run one pipeline step and everything downstream of it."""
    body = request.get_json(silent=True) or {}
    step = body.get("step")
    if step not in (1, 2, 3, 4, 5):
        return jsonify(error="step_must_be_1_to_5"), 400
    if not supabase.campaign_for_user(campaign_id, g.user_id):
        return jsonify(error="not_found"), 404

    supabase.update(
        "campaigns", {"status": "generating", "error": None},
        params={"id": f"eq.{campaign_id}"}, returning=False,
    )
    runner.start(campaign_id, from_step=step)
    return jsonify(campaign_id=campaign_id, status="generating", from_step=step), 202


@bp.post("/api/campaigns/<campaign_id>/approve")
@require_auth
def approve_campaign(campaign_id):
    if not supabase.campaign_for_user(campaign_id, g.user_id):
        return jsonify(error="not_found"), 404
    row = supabase.update(
        "campaigns", {"status": "live"}, params={"id": f"eq.{campaign_id}"}
    )
    return jsonify(row)
