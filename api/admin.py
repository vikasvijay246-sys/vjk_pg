import logging
"""
from api.utils import ok, fail, paginate, safe_route
api/admin.py — Admin Panel API
All routes require role=admin JWT.
Endpoints:
  /api/admin/stats          GET  Dashboard summary
  /api/admin/users          GET  Paginated users + search
  /api/admin/users/<id>     PUT  Block/unblock, role change
  /api/admin/users/<id>     DELETE Soft-delete user
  /api/admin/tenants        GET  All tenants + rent status
  /api/admin/tenants/<id>   PUT  Update tenant
  /api/admin/tenants/<id>   DELETE Remove tenant
  /api/admin/payments       GET  Paginated payments
  /api/admin/payments/<id>  PUT  Mark paid / delete
  /api/admin/complaints     GET  All complaints
  /api/admin/complaints/<id> PUT  Resolve / dismiss
  /api/admin/complaints/<id> DELETE
"""

from datetime import datetime, date
from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt
from sqlalchemy.orm import joinedload
from extensions import db
from db_models import User, Tenant, Room, Payment, Complaint, ActivityLog
from security import sanitize, err, log_activity

admin_bp = Blueprint("admin", __name__, url_prefix="/api/admin")


# ── Guard decorator ─────────────────────────────────────────────────────────
def admin_only(fn):
    """Decorator: allow only role=admin. Must be used after @jwt_required()."""
    from functools import wraps
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if get_jwt().get("role") != "admin":
            return err("Admin access required.", 403)
        return fn(*args, **kwargs)
    return wrapper


# ── Helper ───────────────────────────────────────────────────────────────────
def _paginate(query, page, per):
    total = query.count()
    items = query.offset((page - 1) * per).limit(per).all()
    return items, total, (total + per - 1) // per


# ── Dashboard stats ──────────────────────────────────────────────────────────
@admin_bp.route("/stats", methods=["GET"])
@jwt_required()
@admin_only
def stats():
    today = date.today()
    ms    = today.replace(day=1).isoformat()

    total_users    = User.query.filter_by(is_active=True).count()
    total_owners   = User.query.filter_by(role="owner", is_active=True).count()
    total_tenants_u= User.query.filter_by(role="tenant", is_active=True).count()
    total_tenants_t= Tenant.query.filter_by(is_active=True).count()
    blocked_users  = User.query.filter_by(is_active=False).count()

    paid_ids = [r[0] for r in db.session.query(Payment.tenant_id).filter(
        Payment.month >= ms, Payment.is_paid == True).distinct()]
    paid_this_month = len(paid_ids)
    unpaid_this_month = Tenant.query.filter(
        Tenant.is_active == True, ~Tenant.id.in_(paid_ids)
    ).count()

    collected = db.session.query(db.func.sum(Payment.amount)).filter(
        Payment.month >= ms, Payment.is_paid == True
    ).scalar() or 0
    expected = db.session.query(db.func.sum(Tenant.rent_amount)).filter(
        Tenant.is_active == True
    ).scalar() or 0

    open_complaints = Complaint.query.filter_by(status="open").count()

    return jsonify({
        "total_users": total_users,
        "total_owners": total_owners,
        "total_tenants_accounts": total_tenants_u,
        "total_tenants_active": total_tenants_t,
        "blocked_users": blocked_users,
        "paid_this_month": paid_this_month,
        "unpaid_this_month": unpaid_this_month,
        "collected_this_month": round(float(collected), 2),
        "expected_this_month": round(float(expected), 2),
        "open_complaints": open_complaints,
        "month": today.strftime("%B %Y"),
    })


# ── Users ────────────────────────────────────────────────────────────────────
@admin_bp.route("/users", methods=["GET"])
@jwt_required()
@admin_only
def list_users():
    page    = int(request.args.get("page", 1))
    per     = min(int(request.args.get("per_page", 20)), 100)
    search  = request.args.get("search", "").strip()
    role_f  = request.args.get("role", "").strip()
    status_f= request.args.get("status", "").strip()  # active | blocked

    q = User.query
    if search:
        q = q.filter(db.or_(
            User.name.ilike(f"%{search}%"),
            User.phone.ilike(f"%{search}%")
        ))
    if role_f:
        q = q.filter_by(role=role_f)
    if status_f == "active":
        q = q.filter_by(is_active=True)
    elif status_f == "blocked":
        q = q.filter_by(is_active=False)

    q = q.order_by(User.created_at.desc())
    items, total, pages = _paginate(q, page, per)

    return jsonify({
        "users": [{
            "id": u.id, "name": u.name, "phone": u.phone,
            "role": u.role, "is_active": u.is_active,
            "last_seen": u.last_seen.isoformat() if u.last_seen else None,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        } for u in items],
        "total": total, "page": page, "pages": pages
    })


@admin_bp.route("/users/<int:uid>", methods=["PUT"])
@jwt_required()
@admin_only
def update_user(uid):
    user = User.query.get_or_404(uid)
    data = request.get_json() or {}
    admin_id = int(get_jwt_identity())

    # Prevent admin from modifying themselves
    if uid == admin_id:
        return err("Cannot modify your own account.", 400)

    if "is_active" in data:
        user.is_active = bool(data["is_active"])
        action = "user_unblocked" if user.is_active else "user_blocked"
        log_activity(admin_id, action, {"target_user_id": uid, "name": user.name})

    if "role" in data:
        new_role = str(data["role"]).lower().strip()
        if new_role not in ("owner", "worker", "tenant", "admin"):
            return err("Invalid role.", 400)
        user.role = new_role
        log_activity(admin_id, "role_changed",
                     {"target_user_id": uid, "new_role": new_role})

    db.session.commit()
    return jsonify({"success": True, "is_active": user.is_active, "role": user.role})


@admin_bp.route("/users/<int:uid>", methods=["DELETE"])
@jwt_required()
@admin_only
def delete_user(uid):
    admin_id = int(get_jwt_identity())
    if uid == admin_id:
        return err("Cannot delete your own account.", 400)
    user = User.query.get_or_404(uid)
    user.is_active = False
    # Deactivate tenant profile too
    if user.tenant_profile:
        user.tenant_profile.is_active = False
    db.session.commit()
    log_activity(admin_id, "user_deleted", {"target_user_id": uid, "name": user.name})
    return jsonify({"success": True})


# ── Tenants ──────────────────────────────────────────────────────────────────
@admin_bp.route("/tenants", methods=["GET"])
@jwt_required()
@admin_only
def list_tenants():
    page   = int(request.args.get("page", 1))
    per    = min(int(request.args.get("per_page", 20)), 100)
    search = request.args.get("search", "").strip()
    paid_f = request.args.get("paid", "").strip()

    today = date.today()
    ms    = today.replace(day=1).isoformat()

    q = (db.session.query(Tenant)
         .join(User, Tenant.user_id == User.id)
         .options(joinedload(Tenant.user), joinedload(Tenant.room))
         .filter(Tenant.is_active == True))

    if search:
        q = q.filter(db.or_(
            User.name.ilike(f"%{search}%"),
            User.phone.ilike(f"%{search}%"),
        ))

    total = q.count()
    tenants = q.order_by(User.name).offset((page-1)*per).limit(per).all()

    # Bulk-load payments
    payment_map = {p.tenant_id: p for p in Payment.query.filter(
        Payment.tenant_id.in_([t.id for t in tenants]),
        Payment.month >= ms
    ).all()}

    rows = []
    for t in tenants:
        p = payment_map.get(t.id)
        is_paid = p.is_paid if p else False
        if paid_f == "paid" and not is_paid:   continue
        if paid_f == "unpaid" and is_paid:     continue
        rows.append({
            "id": t.id,
            "user_id": t.user_id,
            "name": t.user.name if t.user else "",
            "phone": t.user.phone if t.user else "",
            "room_number": t.room.room_number if t.room else "",
            "rent_amount": t.rent_amount,
            "joining_date": t.joining_date,
            "is_paid": is_paid,
            "paid_on": p.paid_on if (p and p.is_paid) else None,
        })

    return jsonify({
        "tenants": rows,
        "total": total,
        "page": page,
        "pages": (total + per - 1) // per
    })


@admin_bp.route("/tenants/<int:tid>", methods=["PUT"])
@jwt_required()
@admin_only
def update_tenant(tid):
    t    = Tenant.query.get_or_404(tid)
    data = request.get_json() or {}
    if "rent_amount" in data:
        try: t.rent_amount = float(data["rent_amount"])
        except ValueError: return err("Invalid rent amount.", 400)
    if "room_id" in data:
        t.room_id = int(data["room_id"])
    db.session.commit()
    log_activity(int(get_jwt_identity()), "admin_tenant_updated", {"tenant_id": tid})
    return jsonify({"success": True})


@admin_bp.route("/tenants/<int:tid>", methods=["DELETE"])
@jwt_required()
@admin_only
def delete_tenant(tid):
    t = Tenant.query.get_or_404(tid)
    t.is_active = False
    if t.room: t.room.is_occupied = False
    db.session.commit()
    log_activity(int(get_jwt_identity()), "admin_tenant_removed", {"tenant_id": tid})
    return jsonify({"success": True})


# ── Payments ─────────────────────────────────────────────────────────────────
@admin_bp.route("/payments", methods=["GET"])
@jwt_required()
@admin_only
def list_payments():
    page   = int(request.args.get("page", 1))
    per    = min(int(request.args.get("per_page", 20)), 100)
    month  = request.args.get("month", date.today().replace(day=1).isoformat())
    paid_f = request.args.get("paid", "").strip()

    q = (db.session.query(Payment)
         .join(Tenant, Payment.tenant_id == Tenant.id)
         .join(User, Tenant.user_id == User.id)
         .options(joinedload(Payment.tenant).joinedload(Tenant.user),
                  joinedload(Payment.tenant).joinedload(Tenant.room))
         .filter(Payment.month >= month))

    if paid_f == "paid":   q = q.filter(Payment.is_paid == True)
    if paid_f == "unpaid": q = q.filter(Payment.is_paid == False)

    q = q.order_by(Payment.created_at.desc())
    items, total, pages = _paginate(q, page, per)

    return jsonify({
        "payments": [{
            "id": p.id,
            "tenant_id": p.tenant_id,
            "tenant_name": p.tenant.user.name if p.tenant and p.tenant.user else "",
            "room_number": p.tenant.room.room_number if p.tenant and p.tenant.room else "",
            "amount": p.amount,
            "month": p.month,
            "is_paid": p.is_paid,
            "paid_on": p.paid_on,
        } for p in items],
        "total": total, "page": page, "pages": pages
    })


@admin_bp.route("/payments/<int:pid>", methods=["PUT"])
@jwt_required()
@admin_only
def update_payment(pid):
    p    = Payment.query.get_or_404(pid)
    data = request.get_json() or {}
    if "is_paid" in data:
        p.is_paid = bool(data["is_paid"])
        p.paid_on = date.today().isoformat() if p.is_paid else None
    db.session.commit()
    log_activity(int(get_jwt_identity()), "admin_payment_updated",
                 {"payment_id": pid, "is_paid": p.is_paid})
    return jsonify({"success": True, "is_paid": p.is_paid})


@admin_bp.route("/payments/<int:pid>", methods=["DELETE"])
@jwt_required()
@admin_only
def delete_payment(pid):
    p = Payment.query.get_or_404(pid)
    db.session.delete(p)
    db.session.commit()
    log_activity(int(get_jwt_identity()), "admin_payment_deleted", {"payment_id": pid})
    return jsonify({"success": True})


# ── Complaints ───────────────────────────────────────────────────────────────
@admin_bp.route("/complaints", methods=["GET"])
@jwt_required()
@admin_only
def list_complaints():
    page    = int(request.args.get("page", 1))
    per     = min(int(request.args.get("per_page", 20)), 100)
    status_f= request.args.get("status", "").strip()  # open | resolved | dismissed

    q = Complaint.query.options(joinedload(Complaint.author))
    if status_f:
        q = q.filter_by(status=status_f)
    q = q.order_by(Complaint.created_at.desc())
    items, total, pages = _paginate(q, page, per)

    return jsonify({
        "complaints": [c.to_dict() for c in items],
        "total": total, "page": page, "pages": pages
    })


@admin_bp.route("/complaints/<int:cid>", methods=["PUT"])
@jwt_required()
@admin_only
def update_complaint(cid):
    c    = Complaint.query.get_or_404(cid)
    data = request.get_json() or {}
    admin_id = int(get_jwt_identity())
    new_status = str(data.get("status", c.status)).lower()
    if new_status not in ("open", "resolved", "dismissed"):
        return err("Invalid status.", 400)
    c.status = new_status
    if new_status in ("resolved", "dismissed"):
        c.resolved_by = admin_id
        c.resolved_at = datetime.utcnow()
    db.session.commit()
    log_activity(admin_id, "complaint_updated",
                 {"complaint_id": cid, "status": new_status})
    return jsonify({"success": True, "status": c.status})


@admin_bp.route("/complaints/<int:cid>", methods=["DELETE"])
@jwt_required()
@admin_only
def delete_complaint(cid):
    c = Complaint.query.get_or_404(cid)
    db.session.delete(c)
    db.session.commit()
    log_activity(int(get_jwt_identity()), "complaint_deleted", {"complaint_id": cid})
    return jsonify({"success": True})


# ── Submit complaint (for tenants) ────────────────────────────────────────────
@admin_bp.route("/complaints", methods=["POST"])
@jwt_required()
def submit_complaint():
    uid  = int(get_jwt_identity())
    data = request.get_json() or {}
    msg  = sanitize(str(data.get("message", "")).strip(), 1000)
    cat  = sanitize(str(data.get("category", "General")).strip(), 80)
    if not msg or len(msg) < 5:
        return err("Please write at least 5 characters.", 400)
    c = Complaint(author_id=uid, category=cat, message=msg)
    db.session.add(c)
    db.session.commit()
    return jsonify({"success": True, "id": c.id}), 201


# ── Activity log ──────────────────────────────────────────────────────────────
@admin_bp.route("/activity", methods=["GET"])
@jwt_required()
@admin_only
def activity_log():
    page = int(request.args.get("page", 1))
    per  = min(int(request.args.get("per_page", 30)), 100)
    q    = ActivityLog.query.order_by(ActivityLog.created_at.desc())
    items, total, pages = _paginate(q, page, per)
    return jsonify({
        "logs": [l.to_dict() for l in items],
        "total": total, "page": page, "pages": pages
    })


# ── Messages (admin view) ─────────────────────────────────────────────────────
@admin_bp.route("/messages", methods=["GET"])
@jwt_required()
@admin_only
def list_messages():
    """View all messages (admin sees metadata, not decrypted content)."""
    page   = int(request.args.get("page", 1))
    per    = min(int(request.args.get("per_page", 20)), 100)
    search = request.args.get("search", "").strip()

    from db_models import Message
    from sqlalchemy.orm import aliased

    SenderUser   = db.aliased(User)
    ReceiverUser = db.aliased(User)

    q = (db.session.query(Message, SenderUser, ReceiverUser)
         .join(SenderUser,   Message.sender_id   == SenderUser.id)
         .join(ReceiverUser, Message.receiver_id == ReceiverUser.id))

    if search:
        q = q.filter(
            db.or_(
                SenderUser.name.ilike(f"%{search}%"),
                SenderUser.phone.ilike(f"%{search}%"),
                ReceiverUser.name.ilike(f"%{search}%"),
            )
        )

    q = q.order_by(Message.created_at.desc())
    total = q.count()
    rows  = q.offset((page-1)*per).limit(per).all()

    result = []
    for msg, sender, receiver in rows:
        result.append({
            "id":            msg.id,
            "sender_name":   sender.name,
            "sender_phone":  sender.phone,
            "receiver_name": receiver.name,
            "file_type":     msg.file_type,
            "status":        msg.status,
            "has_text":      bool(msg.content_encrypted),
            "has_file":      bool(msg.file_url),
            "created_at":    msg.created_at.isoformat() if msg.created_at else None,
        })

    return jsonify({"messages": result, "total": total, "page": page,
                    "pages": (total + per - 1) // per})


@admin_bp.route("/messages/<int:mid>", methods=["DELETE"])
@jwt_required()
@admin_only
def delete_message(mid):
    from db_models import Message
    msg = Message.query.get_or_404(mid)
    db.session.delete(msg)
    db.session.commit()
    log_activity(int(get_jwt_identity()), "admin_message_deleted", {"message_id": mid})
    return jsonify({"success": True})
