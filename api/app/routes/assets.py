from flask import Blueprint, current_app, g, jsonify, request

from .. import supabase
from ..auth import require_auth
from ..pipeline import orchestrator
from ..pipeline.agents import AgentError
from ..pipeline.llm import LLMError

bp = Blueprint("assets", __name__)


def _asset_for_user(asset_id: str, user_id: str) -> dict | None:
    rows = supabase.select(
        "content_assets",
        params={
            "id": f"eq.{asset_id}",
            "select": "*,campaigns!inner(id,businesses!inner(user_id))",
            "campaigns.businesses.user_id": f"eq.{user_id}",
        },
    )
    return rows[0] if rows else None


@bp.get("/api/campaigns/<campaign_id>/assets")
@require_auth
def list_assets(campaign_id):
    if not supabase.campaign_for_user(campaign_id, g.user_id):
        return jsonify(error="not_found"), 404
    return jsonify(
        supabase.select(
            "content_assets",
            params={
                "campaign_id": f"eq.{campaign_id}",
                "order": "type.asc,channel.asc,variant.asc",
            },
        )
    )


@bp.patch("/api/assets/<asset_id>")
@require_auth
def edit_asset(asset_id):
    """Save user edits. The original generation stays in `content` untouched so
    'revert to AI version' stays possible."""
    body = request.get_json(silent=True) or {}
    if "edited_content" not in body:
        return jsonify(error="edited_content_required"), 400
    if not _asset_for_user(asset_id, g.user_id):
        return jsonify(error="not_found"), 404

    row = supabase.update(
        "content_assets",
        {"edited_content": body["edited_content"], "status": "edited"},
        params={"id": f"eq.{asset_id}"},
    )
    return jsonify(row)


@bp.post("/api/assets/<asset_id>/approve")
@require_auth
def approve_asset(asset_id):
    if not _asset_for_user(asset_id, g.user_id):
        return jsonify(error="not_found"), 404
    return jsonify(
        supabase.update(
            "content_assets", {"status": "approved"}, params={"id": f"eq.{asset_id}"}
        )
    )


@bp.post("/api/assets/<asset_id>/regenerate")
@require_auth
def regenerate_asset(asset_id):
    """One-asset regen — a single cheap call, no credit charged."""
    asset = _asset_for_user(asset_id, g.user_id)
    if not asset:
        return jsonify(error="not_found"), 404

    asset.pop("campaigns", None)  # join artefact from the ownership check
    try:
        return jsonify(orchestrator.regenerate_asset(asset))
    except AgentError as e:
        return jsonify(error="regenerate_failed", detail=str(e)), 422
    except LLMError as e:
        current_app.logger.warning("[assets] regenerate failed: %s", e)
        return jsonify(error="generation_failed", detail=str(e)), 502


@bp.get("/api/campaigns/<campaign_id>/export")
@require_auth
def export_campaign(campaign_id):
    """.zip of assets + calendar CSV + campaign summary PDF."""
    if not supabase.campaign_for_user(campaign_id, g.user_id):
        return jsonify(error="not_found"), 404
    # Week 5: build the bundle (reuse Metalyzi's @react-pdf pattern for the PDF).
    return jsonify(error="not_implemented", detail="export lands in Week 5"), 501
