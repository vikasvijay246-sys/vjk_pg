import logging
"""
from api.utils import ok, fail, paginate, safe_route
api/notifications.py — Read/dismiss notifications
api/files.py        — Secure file upload/download
api/settings.py     — Language, theme, notification prefs
All in one file for brevity.
"""

import os, uuid
from datetime import datetime
from flask import Blueprint, request, jsonify, send_from_directory, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt
from extensions import db
from db_models import Notification, UploadedFile, UserSettings, User
log = logging.getLogger("pg_manager.files")

# ═══════════════════════════════════════════════════════
#  NOTIFICATIONS
# ═══════════════════════════════════════════════════════
notif_bp = Blueprint("notifications", __name__, url_prefix="/api/notifications")

@notif_bp.route("", methods=["GET"])
@jwt_required()
def list_notifications():
    uid = int(get_jwt_identity())
    page = int(request.args.get("page", 1))
    per  = int(request.args.get("per_page", 20))

    q = Notification.query.filter_by(user_id=uid).order_by(
        Notification.created_at.desc()
    )
    total   = q.count()
    unread  = q.filter_by(is_read=False).count()
    notifs  = q.offset((page - 1) * per).limit(per).all()

    import json as _json
    SOURCE_LABEL = {
        "new_message": "Chat", "rent_paid": "Payment",
        "rent_due": "Payment", "reminder": "Reminder",
        "complaint": "Issue", "system": "System",
    }
    enriched = []
    for n in notifs:
        d = n.to_dict()
        d["source_label"] = SOURCE_LABEL.get(n.notif_type, "Notification")
        try: d["data"] = _json.loads(n.data_json) if n.data_json else {}
        except: d["data"] = {}
        enriched.append(d)

    return jsonify({
        "notifications": enriched,
        "unread_count": unread,
        "total": total,
        "page": page,
    })


@notif_bp.route("/read-all", methods=["POST"])
@jwt_required()
def read_all():
    uid = int(get_jwt_identity())
    Notification.query.filter_by(user_id=uid, is_read=False).update({"is_read": True})
    db.session.commit()
    return jsonify({"success": True})


@notif_bp.route("/<int:nid>/read", methods=["POST"])
@jwt_required()
def read_one(nid):
    uid = int(get_jwt_identity())
    n = Notification.query.filter_by(id=nid, user_id=uid).first_or_404()
    n.is_read = True
    db.session.commit()
    return jsonify({"success": True})


# ═══════════════════════════════════════════════════════
#  FILES
# ═══════════════════════════════════════════════════════
files_bp = Blueprint("files", __name__, url_prefix="/api/files")

@files_bp.route("/upload", methods=["POST"])
@jwt_required()
def upload_file():
    """Hardened file upload: size check, type check, filename sanitization."""
    import re as _re
    uid  = int(get_jwt_identity())
    f    = request.files.get("file")

    if not f or not f.filename:
        return jsonify({"error": "No file provided"}), 400

    # ── Size check (before saving) ──────────────────────────────────────────
    MAX_BYTES = current_app.config.get("MAX_CONTENT_LENGTH", 20 * 1024 * 1024)
    f.seek(0, 2)           # seek to end
    file_size = f.tell()
    f.seek(0)              # reset
    if file_size > MAX_BYTES:
        mb = MAX_BYTES // (1024 * 1024)
        return jsonify({"error": f"File too large. Maximum allowed: {mb}MB"}), 413

    if file_size == 0:
        return jsonify({"error": "Empty file not allowed"}), 400

    # ── Extension / type check ───────────────────────────────────────────────
    raw_name = f.filename or ""
    ext = raw_name.rsplit(".", 1)[-1].lower() if "." in raw_name else ""
    allowed = current_app.config.get("ALLOWED_FILE_EXTENSIONS", set())
    if not ext or ext not in allowed:
        return jsonify({"error": f"File type .{ext!r} not allowed. Allowed: {sorted(allowed)}"}), 400

    # ── Sanitize filename (prevent path traversal / injection) ───────────────
    safe_orig = _re.sub(r"[^a-zA-Z0-9._-]", "_", raw_name)[:120]
    safe_orig = os.path.basename(safe_orig)   # strip any directory components
    if not safe_orig or safe_orig.startswith("."):
        safe_orig = "upload"

    # ── Generate unique stored name ──────────────────────────────────────────
    stored_name = f"{uuid.uuid4().hex}.{ext}"
    save_path   = os.path.join(current_app.config["UPLOAD_FOLDER"], stored_name)
    # Double-check path is inside UPLOAD_FOLDER (belt-and-suspenders)
    upload_dir  = os.path.realpath(current_app.config["UPLOAD_FOLDER"])
    if not os.path.realpath(save_path).startswith(upload_dir + os.sep):
        return jsonify({"error": "Invalid file path"}), 400

    # ── Save & record ────────────────────────────────────────────────────────
    try:
        f.save(save_path)
        actual_size = os.path.getsize(save_path)
    except OSError as exc:
        log.error("File save failed: %s", exc)
        return jsonify({"error": "Failed to save file. Please try again."}), 500

    access = request.form.get("access_roles", "owner")
    record = UploadedFile(
        uploaded_by   = uid,
        filename      = stored_name,
        original_name = safe_orig,
        file_type     = ext,
        file_size     = actual_size,
        access_roles  = access,
    )
    db.session.add(record)
    try:
        db.session.commit()
        log.info("File uploaded: %s by user %d (%d bytes)", stored_name, uid, actual_size)
    except Exception as exc:
        db.session.rollback()
        # Clean up orphaned file
        try: os.remove(save_path)
        except OSError: pass
        log.error("DB commit failed after file upload: %s", exc)
        return jsonify({"error": "Failed to record upload. Please try again."}), 500

    return jsonify({"success": True, "file": record.to_dict()}), 201


@files_bp.route("/download/<filename>", methods=["GET"])
@jwt_required()
def download_file(filename):
    """Access-controlled file download."""
    uid  = int(get_jwt_identity())
    role = get_jwt().get("role")
    rec  = UploadedFile.query.filter_by(filename=filename).first_or_404()

    allowed = rec.access_roles or "owner"
    if role not in allowed and rec.uploaded_by != uid:
        return jsonify({"error": "Access denied"}), 403

    return send_from_directory(current_app.config["UPLOAD_FOLDER"], filename,
                               as_attachment=True,
                               download_name=rec.original_name)


@files_bp.route("", methods=["GET"])
@jwt_required()
def list_files():
    uid  = int(get_jwt_identity())
    role = get_jwt().get("role")
    if role in ("owner", "worker"):
        files = UploadedFile.query.order_by(UploadedFile.created_at.desc()).all()
    else:
        files = UploadedFile.query.filter_by(uploaded_by=uid).all()
    return jsonify([f.to_dict() for f in files])

@files_bp.route("/<int:fid>", methods=["DELETE"])
@jwt_required()
def delete_file(fid):
    """Delete a file record and the physical file."""
    uid  = int(get_jwt_identity())
    role = get_jwt().get("role")
    rec  = UploadedFile.query.get_or_404(fid)

    if rec.uploaded_by != uid and role not in ("owner", "admin"):
        return jsonify({"error": "Access denied"}), 403

    # Remove physical file
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], rec.filename)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as exc:
        log.warning("Could not delete file %s: %s", path, exc)

    db.session.delete(rec)
    try:
        db.session.commit()
        return jsonify({"success": True})
    except Exception as exc:
        db.session.rollback()
        log.error("DB delete failed for file %d: %s", fid, exc)
        return jsonify({"error": "Failed to delete file"}), 500


@files_bp.route("/<int:fid>", methods=["GET"])
@jwt_required()
def get_file_info(fid):
    """Get metadata for a single file."""
    uid  = int(get_jwt_identity())
    role = get_jwt().get("role")
    rec  = UploadedFile.query.get_or_404(fid)
    if rec.uploaded_by != uid and role not in ("owner", "admin", "worker"):
        return jsonify({"error": "Access denied"}), 403
    return jsonify(rec.to_dict())



# ═══════════════════════════════════════════════════════
#  SETTINGS
# ═══════════════════════════════════════════════════════
settings_bp = Blueprint("settings", __name__, url_prefix="/api/settings")

@settings_bp.route("", methods=["GET"])
@jwt_required()
def get_settings():
    uid = int(get_jwt_identity())
    s = UserSettings.query.filter_by(user_id=uid).first()
    if not s:
        s = UserSettings(user_id=uid)
        db.session.add(s)
        db.session.commit()
    return jsonify(s.to_dict())


@settings_bp.route("", methods=["PUT"])
@jwt_required()
def update_settings():
    uid  = int(get_jwt_identity())
    data = request.get_json() or {}

    s = UserSettings.query.filter_by(user_id=uid).first()
    if not s:
        s = UserSettings(user_id=uid)
        db.session.add(s)

    if "language" in data and data["language"] in ("en", "te", "hi"):
        s.language = data["language"]
    if "theme" in data and data["theme"] in ("light", "dark", "high_contrast"):
        s.theme = data["theme"]
    if "notify_rent" in data:
        s.notify_rent = bool(data["notify_rent"])
    if "notify_messages" in data:
        s.notify_messages = bool(data["notify_messages"])
    if "notify_reminders" in data:
        s.notify_reminders = bool(data["notify_reminders"])
    if "rent_due_day" in data:
        day = int(data["rent_due_day"])
        if 1 <= day <= 28:
            s.rent_due_day = day
    if "fcm_token" in data:
        token = str(data["fcm_token"]).strip()[:255]
        s.fcm_token = token if token else None
        log.info("FCM token updated for user %d", uid)
    if "push_enabled" in data:
        s.push_enabled = bool(data["push_enabled"])

    s.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"success": True, "settings": s.to_dict()})
