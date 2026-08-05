from flask import Blueprint, g, jsonify, request

from .. import supabase
from ..auth import require_auth

bp = Blueprint("business", __name__)

EDITABLE = (
    "name", "industry", "offer_description", "price_point",
    "location", "brand_voice", "website",
)


@bp.get("/api/profile")
@require_auth
def get_profile():
    """The signed-in user's plan and credit balance, for the sidebar and
    settings. Created by the signup trigger, so it always exists."""
    row = supabase.select(
        "profiles",
        params={"id": f"eq.{g.user_id}", "select": "id,full_name,plan,credits"},
        single=True,
    )
    if not row:
        return jsonify(error="profile_missing"), 404
    return jsonify({**row, "email": g.email})


@bp.patch("/api/profile")
@require_auth
def update_profile():
    """Name only. plan and credits are service-role writes — the database
    enforces that with a column-level grant, not just this handler."""
    body = request.get_json(silent=True) or {}
    if "full_name" not in body:
        return jsonify(error="full_name_required"), 400
    row = supabase.update(
        "profiles", {"full_name": body["full_name"]}, params={"id": f"eq.{g.user_id}"}
    )
    return jsonify(row)


@bp.post("/api/business")
@require_auth
def create_business():
    body = request.get_json(silent=True) or {}
    if not (body.get("name") or "").strip():
        return jsonify(error="name_required"), 400

    row = {k: body[k] for k in EDITABLE if k in body}
    row["user_id"] = g.user_id
    return jsonify(supabase.insert("businesses", row)), 201


@bp.get("/api/business")
@require_auth
def list_businesses():
    return jsonify(
        supabase.select(
            "businesses",
            params={"user_id": f"eq.{g.user_id}", "order": "created_at.desc"},
        )
    )


@bp.get("/api/business/<business_id>")
@require_auth
def get_business(business_id):
    row = supabase.select(
        "businesses",
        params={"id": f"eq.{business_id}", "user_id": f"eq.{g.user_id}"},
        single=True,
    )
    if not row:
        return jsonify(error="not_found"), 404
    return jsonify(row)


@bp.patch("/api/business/<business_id>")
@require_auth
def update_business(business_id):
    body = request.get_json(silent=True) or {}
    patch = {k: body[k] for k in EDITABLE if k in body}
    if not patch:
        return jsonify(error="nothing_to_update"), 400

    # Filter on user_id as well as id so a wrong id cannot touch someone
    # else's row — the service role has no RLS to fall back on.
    row = supabase.update(
        "businesses", patch,
        params={"id": f"eq.{business_id}", "user_id": f"eq.{g.user_id}"},
    )
    if not row:
        return jsonify(error="not_found"), 404
    return jsonify(row)


@bp.get("/api/business/<business_id>/memories")
@require_auth
def list_memories(business_id):
    """'What the AI has learned about your business' — dashboard trust signal."""
    if not supabase.business_owned_by(business_id, g.user_id):
        return jsonify(error="not_found"), 404
    return jsonify(
        supabase.select(
            "memories",
            params={
                "business_id": f"eq.{business_id}",
                "select": "id,kind,content,source_campaign_id,created_at",
                "order": "created_at.desc",
            },
        )
    )
