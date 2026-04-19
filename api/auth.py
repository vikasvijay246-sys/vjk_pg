import logging
"""
from api.utils import ok, fail, paginate, safe_route
api/auth.py — Production Authentication
  • Phone + OTP (DB-backed, hashed, rate-limited)
  • Phone + Password (bcrypt)
  • JWT + account locking + audit logs
"""

import os, re, random, uuid, json
from datetime import datetime
from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import (
    create_access_token, create_refresh_token,
    jwt_required, get_jwt_identity, get_jwt,
)
from extensions import db, bcrypt, limiter
from db_models import User, UserSettings
from security import (
    validate_phone, validate_name, validate_password, validate_otp,
    sanitize, err,
    check_account_locked, get_lock_remaining,
    record_failed_attempt, clear_failed_attempts,
    create_otp, verify_otp as db_verify_otp,
    log_activity,
)

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")


# ── Helpers ────────────────────────────────────────────────────────────────
def _save_photo(field):
    f = request.files.get(field)
    if not f or not f.filename: return None
    ext = f.filename.rsplit(".", 1)[-1].lower()
    if ext not in current_app.config["ALLOWED_IMAGE_EXTENSIONS"]: return None
    name = f"{uuid.uuid4().hex}.{ext}"
    f.save(os.path.join(current_app.config["UPLOAD_FOLDER"], name))
    return f"/static/uploads/{name}"

def _tokens(user):
    a = create_access_token(identity=str(user.id), additional_claims={"role": user.role})
    r = create_refresh_token(identity=str(user.id))
    return a, r

def _gen_otp():
    return f"{random.SystemRandom().randint(0,999999):06d}"

def _send_sms(phone, otp):
    provider = os.environ.get("SMS_PROVIDER", "console")
    if provider == "twilio":
        try:
            from twilio.rest import Client
            Client(os.environ["TWILIO_SID"], os.environ["TWILIO_AUTH"]).messages.create(
                body=f"Your PG Manager OTP is {otp}. Valid 5 min. Do not share.",
                from_=os.environ.get("TWILIO_FROM",""), to=f"+91{phone}")
            return True
        except Exception as e:
            current_app.logger.error(f"Twilio error: {e}"); return False
    elif provider == "fast2sms":
        try:
            import requests as _r
            resp = _r.post("https://www.fast2sms.com/dev/bulkV2",
                headers={"authorization": os.environ["FAST2SMS_KEY"]},
                data={"route":"otp","variables_values":otp,"numbers":phone,"flash":0},
                timeout=10)
            return resp.json().get("return") is True
        except Exception as e:
            current_app.logger.error(f"Fast2SMS error: {e}"); return False
    else:
        current_app.logger.info(f"\n🔑 OTP for +91{phone}: {otp}\n")
        return True


# ── Send OTP ───────────────────────────────────────────────────────────────
@auth_bp.route("/otp/send", methods=["POST"])
@limiter.limit("10 per minute;30 per hour")
def send_otp():
    data = request.get_json() or {}
    try:
        phone = validate_phone(str(data.get("phone","")))
        role  = str(data.get("role","")).lower().strip()
    except ValueError as e:
        return err(str(e), 400)

    if role and role not in ("owner","tenant","worker"):
        return err("Invalid role. Choose 'owner' or 'tenant'.", 400)

    if check_account_locked(phone):
        mins = get_lock_remaining(phone)//60+1
        return err(f"Account locked. Try again in ~{mins} minute(s).", 429)

    user = User.query.filter_by(phone=phone, is_active=True).first()
    if user and role and user.role != role:
        return err(f"This number is registered as '{user.role}', not '{role}'.", 403)

    otp = _gen_otp()
    try:
        create_otp(phone, otp)
    except ValueError as e:
        return err(str(e), 429)

    sent = _send_sms(phone, otp)
    log_activity(user.id if user else None, "otp_sent", {"phone":phone,"sent":sent})

    resp = {"success":True,"is_new_user":user is None,
            "user_name":user.name if user else None,
            "message":f"OTP sent to +91-{phone[:5]}XXXXX"}
    if os.environ.get("FLASK_ENV","development") != "production":
        resp["dev_otp"] = otp
    return jsonify(resp)


# ── Verify OTP ─────────────────────────────────────────────────────────────
@auth_bp.route("/otp/verify", methods=["POST"])
@limiter.limit("20 per minute")
def verify_otp_route():
    data = request.get_json() or {}
    try:
        phone = validate_phone(str(data.get("phone","")))
        otp   = validate_otp(str(data.get("otp","")))
        role  = str(data.get("role","tenant")).lower().strip()
        name_raw = str(data.get("name","")).strip()
    except ValueError as e:
        return err(str(e), 400)

    if check_account_locked(phone):
        mins = get_lock_remaining(phone)//60+1
        return err(f"Account locked. Try again in ~{mins} minute(s).", 429)

    ok, emsg = db_verify_otp(phone, otp)
    if not ok:
        fails = record_failed_attempt(phone)
        log_activity(None,"otp_failed",{"phone":phone,"reason":emsg})
        if max(0,5-fails) == 0:
            return err("Account locked after too many failed attempts. Wait 30 minutes.", 429)
        return err(emsg, 401)

    clear_failed_attempts(phone)
    user = User.query.filter_by(phone=phone, is_active=True).first()

    if not user:
        try:
            name = validate_name(name_raw) if name_raw else None
        except ValueError as e:
            return err(str(e), 400, "name")
        if not name:
            return err("Please enter your name to complete registration.", 400, "name")
        role = role if role in ("owner","tenant","worker") else "tenant"
        tmp_pw = bcrypt.generate_password_hash(phone[-4:]).decode()
        user = User(name=sanitize(name), phone=phone, password_hash=tmp_pw,
                    role=role, is_active=True, created_at=datetime.utcnow())
        db.session.add(user); db.session.flush()
        db.session.add(UserSettings(user_id=user.id))
        db.session.commit()
        is_new = True
    else:
        user.last_seen = datetime.utcnow(); db.session.commit(); is_new = False

    a, r = _tokens(user)
    log_activity(user.id,"login_success",{"method":"otp","is_new":is_new})
    return jsonify({"access_token":a,"refresh_token":r,"user":user.to_dict(),"is_new":is_new})


# ── Password login ─────────────────────────────────────────────────────────
@auth_bp.route("/login", methods=["POST"])
@limiter.limit("15 per minute;60 per hour")
def login():
    data = request.get_json() or {}
    try:
        phone = validate_phone(str(data.get("phone","")))
        pw    = str(data.get("password","")).strip()
    except ValueError as e:
        return err(str(e), 400)
    if not pw:
        return err("Password is required.", 400)

    if check_account_locked(phone):
        mins = get_lock_remaining(phone)//60+1
        return err(f"Account locked. Try again in ~{mins} minute(s).", 429)

    user = User.query.filter_by(phone=phone, is_active=True).first()
    if not user or not bcrypt.check_password_hash(user.password_hash, pw):
        fails = record_failed_attempt(phone)
        log_activity(user.id if user else None,"login_failed",
                     {"phone":phone,"method":"password","fails":fails})
        remaining = max(0,5-fails)
        if remaining == 0:
            return err("Account locked after 5 failed attempts. Wait 30 minutes or use OTP.", 429)
        return err(f"Wrong phone number or password. {remaining} attempt(s) remaining.", 401)

    clear_failed_attempts(phone)
    user.last_seen = datetime.utcnow(); db.session.commit()
    a, r = _tokens(user)
    log_activity(user.id,"login_success",{"method":"password"})
    return jsonify({"access_token":a,"refresh_token":r,"user":user.to_dict()})


# ── Register (Owner / Worker) ──────────────────────────────────────────────
@auth_bp.route("/register", methods=["POST"])
def register():
    ct = request.content_type or ""
    data = request.form if "multipart" in ct else (request.get_json() or {})
    try:
        name = validate_name(str(data.get("name","")))
        phone = validate_phone(str(data.get("phone","")))
        pw    = validate_password(str(data.get("password","")))
        role  = str(data.get("role","tenant")).lower().strip()
    except ValueError as e:
        return err(str(e), 400)
    if role not in ("owner","worker","tenant"):
        return err("Invalid role.", 400)
    if User.query.filter_by(phone=phone).first():
        return err("This phone number is already registered.", 409)
    ph = bcrypt.generate_password_hash(pw).decode()
    pk = sanitize(str(data.get("public_key","") or ""))[:2000]
    photo = _save_photo("photo") if request.files else None
    user  = User(name=sanitize(name), phone=phone, password_hash=ph, role=role,
                 photo_url=photo, public_key=pk or None,
                 is_active=True, created_at=datetime.utcnow())
    db.session.add(user); db.session.flush()
    db.session.add(UserSettings(user_id=user.id)); db.session.commit()
    a, r = _tokens(user)
    log_activity(user.id,"registered",{"role":role})
    return jsonify({"access_token":a,"refresh_token":r,"user":user.to_dict()}), 201


# ── Change password ────────────────────────────────────────────────────────
@auth_bp.route("/change-password", methods=["POST"])
@jwt_required()
def change_password():
    uid  = int(get_jwt_identity())
    user = User.query.get_or_404(uid)
    data = request.get_json() or {}
    try:
        new_pw = validate_password(str(data.get("new_password","")))
    except ValueError as e:
        return err(str(e), 400)
    old_pw = str(data.get("old_password","")).strip()
    if old_pw and not bcrypt.check_password_hash(user.password_hash, old_pw):
        return err("Current password is incorrect.", 401)
    user.password_hash = bcrypt.generate_password_hash(new_pw).decode()
    db.session.commit()
    log_activity(uid,"password_changed")
    return jsonify({"success":True,"message":"Password updated successfully."})


# ── Token endpoints ────────────────────────────────────────────────────────
@auth_bp.route("/refresh", methods=["POST"])
@jwt_required(refresh=True)
def refresh():
    uid  = int(get_jwt_identity())
    user = User.query.filter_by(id=uid, is_active=True).first()
    if not user: return err("User not found.", 404)
    a = create_access_token(identity=str(uid), additional_claims={"role":user.role})
    return jsonify({"access_token":a})

@auth_bp.route("/refresh-silent", methods=["POST"])
@jwt_required(refresh=True)
def refresh_silent():
    uid  = int(get_jwt_identity())
    user = User.query.filter_by(id=uid, is_active=True).first()
    if not user: return err("User not found.", 404)
    user.last_seen = datetime.utcnow(); db.session.commit()
    a = create_access_token(identity=str(uid), additional_claims={"role":user.role})
    return jsonify({"access_token":a,"user":user.to_dict()})

@auth_bp.route("/validate", methods=["GET"])
@jwt_required()
def validate_token():
    uid  = int(get_jwt_identity())
    user = User.query.filter_by(id=uid, is_active=True).first()
    if not user: return err("User not found.", 401)
    user.last_seen = datetime.utcnow(); db.session.commit()
    d = user.to_dict(include_private=True)
    if user.settings: d["settings"] = user.settings.to_dict()
    return jsonify({"valid":True,"user":d})

@auth_bp.route("/logout", methods=["POST"])
@jwt_required()
def logout():
    log_activity(int(get_jwt_identity()),"logout")
    return jsonify({"success":True,"message":"Logged out successfully."})

@auth_bp.route("/me", methods=["GET"])
@jwt_required()
def me():
    uid  = int(get_jwt_identity())
    user = User.query.get_or_404(uid)
    d    = user.to_dict(include_private=True)
    if user.settings: d["settings"] = user.settings.to_dict()
    return jsonify(d)

@auth_bp.route("/update-key", methods=["POST"])
@jwt_required()
def update_public_key():
    uid  = int(get_jwt_identity())
    user = User.query.get_or_404(uid)
    data = request.get_json() or {}
    user.public_key = sanitize(str(data.get("public_key","") or ""))[:2000] or None
    db.session.commit()
    return jsonify({"success":True})

@auth_bp.route("/users", methods=["GET"])
@jwt_required()
def list_users():
    if get_jwt().get("role") not in ("owner","worker"):
        return err("Access denied.", 403)
    return jsonify([u.to_dict(include_private=True)
                    for u in User.query.filter_by(is_active=True).all()])

@auth_bp.route("/activity", methods=["GET"])
@jwt_required()
def activity_log():
    uid    = int(get_jwt_identity())
    claims = get_jwt()
    page   = int(request.args.get("page",1))
    per    = min(int(request.args.get("per_page",20)),100)
    from db_models import ActivityLog
    q = ActivityLog.query if claims.get("role")=="owner" else \
        ActivityLog.query.filter_by(user_id=uid)
    q = q.order_by(ActivityLog.created_at.desc())
    total = q.count()
    logs  = q.offset((page-1)*per).limit(per).all()
    return jsonify({"logs":[l.to_dict() for l in logs],"total":total,"page":page})
