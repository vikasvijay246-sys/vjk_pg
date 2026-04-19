import logging
"""
from api.utils import ok, fail, paginate, safe_route
api/tenants.py — Tenant & Room CRUD with pagination
Owner/Worker can manage; Tenant can view own profile only
"""

import os, uuid
from datetime import date
from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt
from sqlalchemy.orm import joinedload
from extensions import db, bcrypt
from db_models import User, Tenant, Room, Payment, UserSettings, Notification
from security import (
    validate_phone, validate_name, validate_amount, validate_room_number,
    sanitize, err, log_activity,
)

tenants_bp = Blueprint("tenants", __name__, url_prefix="/api/tenants")


# ── Helpers ────────────────────────────────────────────────
def _owner_or_worker():
    return get_jwt().get("role") in ("owner", "worker")

def _save_image(field):
    f = request.files.get(field)
    if not f or not f.filename:
        return None
    ext = f.filename.rsplit(".", 1)[-1].lower()
    if ext not in current_app.config["ALLOWED_IMAGE_EXTENSIONS"]:
        return None
    name = f"{uuid.uuid4().hex}.{ext}"
    f.save(os.path.join(current_app.config["UPLOAD_FOLDER"], name))
    return f"/static/uploads/{name}"

def _payment_status(tenant_id):
    today = date.today()
    ms = today.replace(day=1).isoformat()
    p = Payment.query.filter(
        Payment.tenant_id == tenant_id,
        Payment.month >= ms
    ).first()
    return p.is_paid if p else False, p.paid_on if (p and p.is_paid) else None, p.id if p else None


# ── Rooms ──────────────────────────────────────────────────
@tenants_bp.route("/rooms", methods=["GET"])
@jwt_required()
def list_rooms():
    rooms = Room.query.order_by(Room.room_number).all()
    return jsonify([r.to_dict() for r in rooms])

@tenants_bp.route("/rooms", methods=["POST"])
@jwt_required()
def add_room():
    if not _owner_or_worker():
        return jsonify({"error": "Access denied"}), 403
    data = request.get_json() or {}
    room_number = str(data.get("room_number", "")).strip()
    if not room_number:
        return jsonify({"error": "Room number required"}), 400
    if Room.query.filter_by(room_number=room_number).first():
        return jsonify({"error": "Room already exists"}), 409
    room = Room(
        room_number=room_number,
        floor=data.get("floor", ""),
        capacity=int(data.get("capacity", 1)),
        rent_price=float(data.get("rent_price", 0))
    )
    db.session.add(room)
    db.session.commit()
    return jsonify(room.to_dict()), 201


# ── Tenants ────────────────────────────────────────────────
@tenants_bp.route("", methods=["GET"])
@jwt_required()
def list_tenants():
    """Paginated tenant list with optional search and payment filter."""
    if not _owner_or_worker():
        return jsonify({"error": "Access denied"}), 403

    page     = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 20))
    search   = request.args.get("search", "").strip().lower()
    paid_filter = request.args.get("paid")  # "true" | "false" | None

    today = date.today()
    ms = today.replace(day=1).isoformat()

    # Base query — active tenants with their user
    query = (db.session.query(Tenant)
             .join(User, Tenant.user_id == User.id)
             .options(joinedload(Tenant.room), joinedload(Tenant.user))
             .filter(Tenant.is_active == True))

    if search:
        query = query.filter(
            db.or_(
                User.name.ilike(f"%{search}%"),
                User.phone.ilike(f"%{search}%"),
            )
        )

    total = query.count()

    # Get all IDs matching paid filter (done in Python to stay DB-agnostic)
    tenants_page = query.order_by(User.name).offset((page - 1) * per_page).limit(per_page).all()

    result = []
    for t in tenants_page:
        is_paid, paid_on, payment_id = _payment_status(t.id)
        if paid_filter == "true" and not is_paid:
            continue
        if paid_filter == "false" and is_paid:
            continue
        d = t.to_dict()
        d["is_paid"] = is_paid
        d["last_paid_on"] = paid_on
        d["payment_id"] = payment_id
        result.append(d)

    return jsonify({
        "tenants": result,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page
    })


@tenants_bp.route("/me", methods=["GET"])
@jwt_required()
def my_profile():
    """Tenant views own profile."""
    uid = int(get_jwt_identity())
    t = Tenant.query.filter_by(user_id=uid, is_active=True).first()
    if not t:
        return jsonify({"error": "No tenant profile"}), 404
    d = t.to_dict()
    is_paid, paid_on, pid = _payment_status(t.id)
    d["is_paid"] = is_paid
    d["last_paid_on"] = paid_on
    d["payment_id"] = pid
    return jsonify(d)


@tenants_bp.route("", methods=["POST"])
@jwt_required()
def add_tenant():
    """Add tenant. Creates a User account + Tenant profile in one step."""
    if not _owner_or_worker():
        return jsonify({"error": "Access denied"}), 403

    # Support multipart (photo upload), urlencoded, or JSON
    ct = request.content_type or ""
    is_mp = "multipart" in ct
    if is_mp or "urlencoded" in ct:
        data = request.form
    else:
        data = request.get_json() or {}

    name    = str(data.get("name", "")).strip()
    phone   = str(data.get("phone", "")).strip()
    password = str(data.get("password", phone[-4:] if len(phone) >= 4 else "1234"))
    room_id = data.get("room_id")
    rent    = data.get("rent_amount", 0)
    jdate   = data.get("joining_date", date.today().isoformat())

    # Strict validation
    try:
        name  = validate_name(name)
        phone = validate_phone(phone)
        if rent:
            rent  = validate_amount(rent, "Rent amount")
        if room_id:
            pass  # room_id validated by FK
    except ValueError as ve:
        return err(str(ve), 400)
    if not name or not phone:
        return err("Name and phone are required.", 400)

    # Check phone not already used
    existing_user = User.query.filter_by(phone=phone).first()
    if existing_user:
        # Allow reuse if they don't have an active tenant profile
        user = existing_user
    else:
        pw_hash = bcrypt.generate_password_hash(password).decode("utf-8")
        user = User(name=name, phone=phone, password_hash=pw_hash, role="tenant")
        db.session.add(user)
        db.session.flush()
        db.session.add(UserSettings(user_id=user.id))

    # Deactivate any old tenant profile for same user
    old = Tenant.query.filter_by(user_id=user.id, is_active=True).first()
    if old:
        old.is_active = False

    photo_url    = _save_image("photo") if is_mp else None
    id_proof_url = _save_image("id_proof") if is_mp else None

    tenant = Tenant(
        user_id=user.id,
        room_id=int(room_id) if room_id else None,
        rent_amount=float(rent),
        joining_date=jdate,
        photo_url=photo_url,
        id_proof_url=id_proof_url,
        is_active=True
    )
    db.session.add(tenant)
    # Mark room occupied
    if room_id:
        room = Room.query.get(int(room_id))
        if room:
            room.is_occupied = True

    db.session.commit()
    return jsonify({"success": True, "id": tenant.id,
                    "tenant": tenant.to_dict()}), 201


@tenants_bp.route("/<int:tid>", methods=["GET"])
@jwt_required()
def get_tenant(tid):
    uid   = int(get_jwt_identity())
    claims = get_jwt()
    t = (Tenant.query
         .options(joinedload(Tenant.user), joinedload(Tenant.room))
         .filter_by(id=tid).first_or_404())
    if claims.get("role") not in ("owner", "worker") and t.user_id != uid:
        return err("Access denied.", 403)
    d = t.to_dict()
    is_paid, paid_on, pid = _payment_status(t.id)
    d["is_paid"] = is_paid
    d["last_paid_on"] = paid_on
    # Payment history (last 12 months)
    history = (Payment.query.filter_by(tenant_id=tid)
               .order_by(Payment.month.desc()).limit(12).all())
    d["payment_history"] = [p.to_dict() for p in history]
    return jsonify(d)


@tenants_bp.route("/<int:tid>", methods=["PUT"])
@jwt_required()
def edit_tenant(tid):
    if not _owner_or_worker():
        return jsonify({"error": "Access denied"}), 403
    t = Tenant.query.get_or_404(tid)
    is_mp = request.content_type and "multipart" in request.content_type
    data = request.form if is_mp else (request.get_json() or {})

    if data.get("rent_amount"):
        t.rent_amount = float(data["rent_amount"])
    if data.get("room_id"):
        t.room_id = int(data["room_id"])
    if data.get("joining_date"):
        t.joining_date = data["joining_date"]

    # Update user name/phone
    if data.get("name"):
        t.user.name = data["name"]
    if data.get("phone"):
        t.user.phone = data["phone"]

    if is_mp:
        p = _save_image("photo")
        if p: t.photo_url = p
        i = _save_image("id_proof")
        if i: t.id_proof_url = i

    db.session.commit()
    return jsonify({"success": True, "tenant": t.to_dict()})


@tenants_bp.route("/<int:tid>", methods=["DELETE"])
@jwt_required()
def remove_tenant(tid):
    if not _owner_or_worker():
        return jsonify({"error": "Access denied"}), 403
    t = Tenant.query.get_or_404(tid)
    t.is_active = False
    t.vacating_date = date.today().isoformat()
    if t.room:
        t.room.is_occupied = False
    db.session.commit()
    return jsonify({"success": True})


@tenants_bp.route("/roommates", methods=["GET"])
@jwt_required()
def roommates():
    """
    Any logged-in tenant sees all other active tenants in the same PG.
    Owners/workers see all active tenants (same as list but with user info).
    """
    uid    = int(get_jwt_identity())
    claims = get_jwt()
    role   = claims.get("role")

    from sqlalchemy.orm import joinedload as _jl
    if role in ("owner", "worker", "admin"):
        tenants = (Tenant.query
                   .options(_jl(Tenant.user), _jl(Tenant.room))
                   .filter_by(is_active=True)
                   .order_by(Tenant.id).all())
    else:
        # Tenant sees all *other* active tenants
        tenants = (Tenant.query
                   .options(_jl(Tenant.user), _jl(Tenant.room))
                   .filter(Tenant.is_active == True)
                   .order_by(Tenant.id).all())

    result = []
    for t in tenants:
        if not t.user:
            continue
        result.append({
            "tenant_id": t.id,
            "user_id":   t.user_id,
            "name":      t.user.name,
            "phone":     t.user.phone if role in ("owner","worker","admin") else None,
            "room_number": t.room.room_number if t.room else "",
            "photo_url": t.user.photo_url,
            "role":      t.user.role,
            "joining_date": t.joining_date,
        })
    return jsonify(result)
