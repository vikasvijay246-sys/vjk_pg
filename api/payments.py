import logging
"""
from api.utils import ok, fail, paginate, safe_route
api/payments.py — Payment tracking, monthly reports, dashboard stats
"""

from datetime import date, datetime
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt
from sqlalchemy.orm import joinedload
from extensions import db
from db_models import Tenant, Payment, User, Notification
from security import err, log_activity

payments_bp = Blueprint("payments", __name__, url_prefix="/api/payments")


def _notify_owner(tenant, is_paid):
    """Push notification to all owners when rent status changes."""
    owners = User.query.filter_by(role="owner", is_active=True).all()
    for o in owners:
        msg = f"✅ {tenant.user.name} paid ₹{tenant.rent_amount}" if is_paid \
              else f"🔴 {tenant.user.name} marked unpaid"
        db.session.add(Notification(
            user_id=o.id,
            notif_type="rent_paid" if is_paid else "rent_due",
            title="Rent Update",
            body=msg,
            data_json=f'{{"tenant_id":{tenant.id}}}'
        ))


# ── Dashboard ──────────────────────────────────────────────
@payments_bp.route("/dashboard", methods=["GET"])
@jwt_required()
def dashboard():
    role = get_jwt().get("role")
    uid  = int(get_jwt_identity())

    today = date.today()
    ms = today.replace(day=1).isoformat()

    if role in ("owner", "worker"):
        total = Tenant.query.filter_by(is_active=True).count()

        paid_ids = [r[0] for r in db.session.query(Payment.tenant_id).filter(
            Payment.month >= ms, Payment.is_paid == True).distinct()]

        unpaid = Tenant.query.filter(
            Tenant.is_active == True,
            ~Tenant.id.in_(paid_ids)
        ).count()

        # Total rent collected this month
        collected = db.session.query(db.func.sum(Payment.amount)).filter(
            Payment.month >= ms, Payment.is_paid == True
        ).scalar() or 0

        # Total rent expected
        expected = db.session.query(db.func.sum(Tenant.rent_amount)).filter(
            Tenant.is_active == True
        ).scalar() or 0

        # Today's collections
        collected_today = db.session.query(db.func.sum(Payment.amount)).filter(
            Payment.paid_on == today.isoformat(),
            Payment.is_paid == True
        ).scalar() or 0

        # Overdue: unpaid past due day
        due_day = 5
        overdue_count = Tenant.query.filter(
            Tenant.is_active == True,
            ~Tenant.id.in_(paid_ids)
        ).count() if today.day > due_day else 0

        return jsonify({
            "total_tenants": total,
            "paid_tenants": total - unpaid,
            "unpaid_tenants": unpaid,
            "overdue_count": overdue_count,
            "collected_this_month": round(collected, 2),
            "expected_this_month": round(expected, 2),
            "pending_this_month": round(float(expected) - float(collected), 2),
            "collected_today": round(collected_today, 2),
            "month": today.strftime("%B %Y"),
            "due_day": due_day,
            "today_day": today.day,
        })
    else:
        # Tenant sees own payment status
        t = Tenant.query.filter_by(user_id=uid, is_active=True).first()
        if not t:
            return jsonify({"error": "No tenant profile"}), 404
        p = Payment.query.filter(
            Payment.tenant_id == t.id, Payment.month >= ms
        ).first()
        return jsonify({
            "rent_amount": t.rent_amount,
            "is_paid": p.is_paid if p else False,
            "paid_on": p.paid_on if (p and p.is_paid) else None,
            "month": today.strftime("%B %Y")
        })


# ── Mark Paid / Unpaid ─────────────────────────────────────
@payments_bp.route("/<int:tenant_id>/mark", methods=["POST"])
@jwt_required()
def mark_payment(tenant_id):
    if get_jwt().get("role") not in ("owner", "worker"):
        return jsonify({"error": "Access denied"}), 403

    data = request.get_json() or {}
    is_paid = bool(data.get("is_paid", True))
    month_override = data.get("month")  # Allow marking past months
    notes = data.get("notes", "")

    tenant = Tenant.query.get_or_404(tenant_id)
    today = date.today()
    ms = month_override or today.replace(day=1).isoformat()

    payment = Payment.query.filter(
        Payment.tenant_id == tenant_id,
        Payment.month == ms
    ).first()

    if payment:
        payment.is_paid = is_paid
        payment.paid_on = today.isoformat() if is_paid else None
        payment.notes = notes
    else:
        payment = Payment(
            tenant_id=tenant_id,
            month=ms,
            amount=tenant.rent_amount,
            is_paid=is_paid,
            paid_on=today.isoformat() if is_paid else None,
            notes=notes
        )
        db.session.add(payment)

    _notify_owner(tenant, is_paid)
    try:
        db.session.commit()
        log_activity(
            user_id=None,
            action="payment_marked",
            detail={"tenant_id": tenant_id, "is_paid": is_paid, "month": ms}
        )
    except Exception as exc:
        db.session.rollback()
        import logging; logging.getLogger("pg_manager").error("Payment commit failed: %s", exc)
        return jsonify({"error": "Failed to save payment. Please try again."}), 500
    return jsonify({"success": True, "is_paid": is_paid, "payment": payment.to_dict()})


# ── Monthly Report ─────────────────────────────────────────
@payments_bp.route("/report", methods=["GET"])
@jwt_required()
def monthly_report():
    if get_jwt().get("role") not in ("owner", "worker"):
        return jsonify({"error": "Access denied"}), 403

    month = request.args.get("month", date.today().replace(day=1).isoformat())

    tenants = Tenant.query.filter_by(is_active=True).limit(500).all()
    report = []
    total_collected = 0
    total_due = 0

    # Bulk-load all payments for this month (avoids N+1)
    payment_map = {
        p.tenant_id: p
        for p in Payment.query.filter(
            Payment.tenant_id.in_([t.id for t in tenants]),
            Payment.month == month
        ).all()
    }
    for t in tenants:
        p = payment_map.get(t.id)
        is_paid = p.is_paid if p else False
        amount = t.rent_amount

        if is_paid:
            total_collected += amount
        else:
            total_due += amount

        report.append({
            "tenant_id": t.id,
            "name": t.user.name if t.user else "",
            "phone": t.user.phone if t.user else "",
            "room_number": t.room.room_number if t.room else "",
            "rent_amount": amount,
            "is_paid": is_paid,
            "paid_on": (p.paid_on if p and p.is_paid else None),
        })

    return jsonify({
        "month": month,
        "report": report,
        "summary": {
            "total_tenants": len(tenants),
            "paid": sum(1 for r in report if r["is_paid"]),
            "unpaid": sum(1 for r in report if not r["is_paid"]),
            "total_collected": round(total_collected, 2),
            "total_due": round(total_due, 2),
        }
    })


# ── History for one tenant ─────────────────────────────────
@payments_bp.route("/<int:tenant_id>/history", methods=["GET"])
@jwt_required()
def payment_history(tenant_id):
    uid = int(get_jwt_identity())
    t = Tenant.query.get_or_404(tenant_id)
    if get_jwt().get("role") not in ("owner", "worker") and t.user_id != uid:
        return jsonify({"error": "Access denied"}), 403

    history = (Payment.query
               .filter_by(tenant_id=tenant_id)
               .order_by(Payment.month.desc())
               .limit(24).all())
    return jsonify([p.to_dict() for p in history])
