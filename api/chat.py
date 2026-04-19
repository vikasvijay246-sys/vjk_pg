"""
api/chat.py — Chat REST API
Full CRUD: create, read, edit, delete messages + file upload
Only the message author can edit/delete their own messages.
"""
import logging
import os
import re
import uuid
from datetime import datetime

from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from extensions import db
from db_models import Message, User, Notification, MessageTombstone, ConversationSeq
from api.utils import ok, fail, paginate, safe_route

log = logging.getLogger("pg_manager.chat")

def _conv_id(uid_a: int, uid_b: int) -> str:
    """Stable conversation ID regardless of who is sender/receiver."""
    a, b = min(uid_a, uid_b), max(uid_a, uid_b)
    return f"{a}:{b}"


def _assign_seq(msg: Message) -> int:
    """Assign server_seq and conversation_id atomically on first commit."""
    if not msg.conversation_id:
        msg.conversation_id = _conv_id(msg.sender_id, msg.receiver_id)
    seq = ConversationSeq.next_seq(msg.conversation_id)
    msg.server_seq = seq
    return seq

chat_bp = Blueprint("chat", __name__, url_prefix="/api/chat")

ALLOWED_CHAT_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "mp4", "mov", "webm", "3gp", "pdf"}
MAX_CHAT_FILE_MB  = 20


# ── Helpers ───────────────────────────────────────────────────────────────────
def _save_chat_file(field: str):
    """Save an uploaded file; return (url, type, original_name) or (None,None,None)."""
    f = request.files.get(field)
    if not f or not f.filename:
        return None, None, None

    # Size check before saving
    f.seek(0, 2); size = f.tell(); f.seek(0)
    if size > MAX_CHAT_FILE_MB * 1024 * 1024:
        raise ValueError(f"File exceeds {MAX_CHAT_FILE_MB}MB limit")

    raw_name = f.filename or ""
    ext = raw_name.rsplit(".", 1)[-1].lower() if "." in raw_name else ""
    if ext not in ALLOWED_CHAT_EXTS:
        raise ValueError(f"File type .{ext} not allowed in chat")

    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", raw_name)[:120]
    stored    = f"{uuid.uuid4().hex}.{ext}"
    path      = os.path.join(current_app.config["UPLOAD_FOLDER"], stored)

    upload_dir = os.path.realpath(current_app.config["UPLOAD_FOLDER"])
    if not os.path.realpath(path).startswith(upload_dir + os.sep):
        raise ValueError("Invalid path")

    f.save(path)
    ftype = "image" if ext in {"jpg","jpeg","png","gif","webp"} else \
            "video" if ext in {"mp4","mov","webm","3gp"} else "file"
    return f"/static/uploads/{stored}", ftype, safe_name


def _notify(receiver_id: int, sender_name: str, sender_id: int):
    db.session.add(Notification(
        user_id    = receiver_id,
        notif_type = "new_message",
        title      = f"Message from {sender_name}",
        body       = "New message received",
        data_json  = f'{{"sender_id":{sender_id},"sender_name":"{sender_name}"}}',
    ))


# ── Conversations list ────────────────────────────────────────────────────────
@chat_bp.route("/conversations", methods=["GET"])
@jwt_required()
@safe_route
def conversations():
    uid = int(get_jwt_identity())

    sent_to   = db.session.query(Message.receiver_id).filter_by(sender_id=uid).distinct()
    recv_from = db.session.query(Message.sender_id).filter_by(receiver_id=uid).distinct()

    peer_ids = {r[0] for r in sent_to} | {r[0] for r in recv_from}

    result = []
    for pid in peer_ids:
        peer = User.query.get(pid)
        if not peer:
            continue
        last = (Message.query.filter(
            db.or_(
                db.and_(Message.sender_id == uid,  Message.receiver_id == pid),
                db.and_(Message.sender_id == pid,  Message.receiver_id == uid),
            )
        ).order_by(Message.created_at.desc()).first())

        unread = Message.query.filter_by(
            sender_id=pid, receiver_id=uid, status="delivered"
        ).count()

        result.append({
            "peer":         peer.to_dict(),
            "last_message": last.to_dict() if last else None,
            "unread_count": unread,
        })

    result.sort(
        key=lambda x: x["last_message"]["created_at"] if x["last_message"] else "",
        reverse=True,
    )
    return jsonify(result)


# ── Message history ───────────────────────────────────────────────────────────
@chat_bp.route("/history/<int:peer_id>", methods=["GET"])
@jwt_required()
@safe_route
def history(peer_id):
    uid  = int(get_jwt_identity())
    page = max(1, int(request.args.get("page", 1)))
    per  = min(50, int(request.args.get("per_page", 30)))

    q = Message.query.filter(
        db.or_(
            db.and_(Message.sender_id == uid,     Message.receiver_id == peer_id),
            db.and_(Message.sender_id == peer_id, Message.receiver_id == uid),
        )
    ).order_by(Message.created_at.desc())

    total = q.count()
    msgs  = q.offset((page - 1) * per).limit(per).all()

    # Mark delivered → seen
    try:
        now    = datetime.utcnow()
        unread = Message.query.filter_by(
            sender_id=peer_id, receiver_id=uid, status="delivered"
        ).all()
        for m in unread:
            m.status  = "seen"
            m.seen_at = now
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        log.error("mark-seen failed: %s", exc)

    return jsonify({
        "messages": [m.to_dict() for m in reversed(msgs)],
        "total":    total,
        "page":     page,
        "pages":    max(1, (total + per - 1) // per),
    })


# ── CREATE message ─────────────────────────────────────────────────────────────
@chat_bp.route("/send", methods=["POST"])
@jwt_required()
@safe_route
def send_message():
    uid   = int(get_jwt_identity())
    is_mp = request.content_type and "multipart" in request.content_type
    data  = request.form if is_mp else (request.get_json() or {})

    receiver_id = int(data.get("receiver_id", 0) or 0)
    if not receiver_id:
        return fail("receiver_id required")

    # Ensure receiver exists
    receiver = User.query.get(receiver_id)
    if not receiver:
        return fail("Recipient not found", 404)

    User.query.get_or_404(receiver_id)

    file_url = file_type = file_name = None
    if is_mp:
        try:
            file_url, file_type, file_name = _save_chat_file("file")
        except ValueError as e:
            return fail(str(e))

    now = datetime.utcnow()
    msg = Message(
        sender_id         = uid,
        receiver_id       = receiver_id,
        content_encrypted = data.get("content_encrypted", ""),
        iv                = data.get("iv", ""),
        file_url          = file_url,
        file_type         = file_type,
        file_name         = file_name,
        status            = "delivered",
        created_at        = now,
        delivered_at      = now,
    )
    sender = User.query.get(uid)
    msg.conversation_id = _conv_id(uid, receiver_id)

    db.session.add(msg)
    _notify(receiver_id, sender.name if sender else "Someone", uid)

    # Assign monotonic seq inside the same transaction
    seq = ConversationSeq.next_seq(msg.conversation_id)
    msg.server_seq = seq

    try:
        db.session.commit()
        log.info("Message %d: user %d → user %d", msg.id, uid, receiver_id)
    except Exception as exc:
        db.session.rollback()
        log.error("send_message DB commit failed: %s", exc)
        return fail("Failed to save message. Please retry.", 500)

    payload = msg.to_dict()
    payload["sender_name"]  = sender.name  if sender else ""
    payload["sender_photo"] = sender.photo_url if sender else None
    return jsonify({"success": True, "message": payload}), 201


# ── UPDATE message (edit text) ────────────────────────────────────────────────
@chat_bp.route("/<int:msg_id>", methods=["PUT"])
@jwt_required()
@safe_route
def edit_message(msg_id):
    """Only the original sender may edit. Only text content can be edited."""
    uid  = int(get_jwt_identity())
    msg  = Message.query.get_or_404(msg_id)

    if msg.sender_id != uid:
        return fail("You can only edit your own messages.", 403)

    data = request.get_json() or {}
    new_enc = data.get("content_encrypted")
    new_iv  = data.get("iv")

    if not new_enc:
        return fail("content_encrypted required")

    msg.content_encrypted = new_enc
    if new_iv:
        msg.iv = new_iv

    try:
        db.session.commit()
        log.info("Message %d edited by user %d", msg_id, uid)
    except Exception as exc:
        db.session.rollback()
        log.error("edit_message DB commit failed: %s", exc)
        return fail("Failed to update message.", 500)

    return jsonify({"success": True, "message": msg.to_dict()})


# ── DELETE message ─────────────────────────────────────────────────────────────
@chat_bp.route("/<int:msg_id>", methods=["DELETE"])
@jwt_required()
@safe_route
def delete_message(msg_id):
    """Only the original sender may delete their message."""
    uid = int(get_jwt_identity())
    msg = Message.query.get_or_404(msg_id)

    if msg.sender_id != uid:
        return fail("You can only delete your own messages.", 403)

    # Remove physical file if attached
    if msg.file_url:
        fname = msg.file_url.rsplit("/", 1)[-1]
        fpath = os.path.join(current_app.config["UPLOAD_FOLDER"], fname)
        try:
            if os.path.isfile(fpath):
                os.remove(fpath)
        except OSError as exc:
            log.warning("Could not delete file %s: %s", fpath, exc)

    try:
        db.session.delete(msg)
        db.session.commit()
        log.info("Message %d deleted by user %d", msg_id, uid)
    except Exception as exc:
        db.session.rollback()
        log.error("delete_message DB commit failed: %s", exc)
        return fail("Failed to delete message.", 500)

    return jsonify({"success": True})


# ── Public key ────────────────────────────────────────────────────────────────
@chat_bp.route("/pubkey/<int:peer_id>", methods=["GET"])
@jwt_required()
@safe_route
def get_pubkey(peer_id):
    peer = User.query.get_or_404(peer_id)
    return jsonify({"user_id": peer.id, "public_key": peer.public_key})


# ── DELTA SYNC ─────────────────────────────────────────────────────────────────
@chat_bp.route("/delta/<path:conversation_id>", methods=["GET"])
@jwt_required()
@safe_route
def delta_sync(conversation_id):
    """
    Pull-based delta sync: returns only messages with server_seq > since_seq.

    Query params:
        since_seq  (int, default 0) — last seq the client has
        page       (int, default 1)
        per_page   (int, default 50, max 100)

    Returns:
        messages   — list of new/updated messages
        deleted_ids — message IDs deleted since since_seq (tombstones)
        max_seq    — highest seq in this batch (save as new cursor)
        has_more   — whether another page exists
    """
    uid       = int(get_jwt_identity())
    since_seq = int(request.args.get("since_seq", 0))
    page      = max(1, int(request.args.get("page", 1)))
    per       = min(100, int(request.args.get("per_page", 50)))

    # Security: caller must be a participant in this conversation
    uid_a, uid_b = (int(x) for x in conversation_id.split(":"))
    if uid not in (uid_a, uid_b):
        return fail("Not a participant in this conversation.", 403)

    q = (Message.query
         .filter(
             Message.conversation_id == conversation_id,
             Message.server_seq > since_seq,
         )
         .order_by(Message.server_seq.asc()))

    total = q.count()
    msgs  = q.offset((page - 1) * per).limit(per).all()

    # Tombstones: deleted messages since last sync
    tombstones = (db.session.query(MessageTombstone)
                  .filter(
                      MessageTombstone.conversation_id == conversation_id,
                      MessageTombstone.deleted_seq > since_seq,
                  ).all())

    max_seq = msgs[-1].server_seq if msgs else since_seq

    log.info("Delta sync conv=%s since=%d → %d messages, %d tombstones",
             conversation_id, since_seq, len(msgs), len(tombstones))

    return jsonify({
        "messages":    [m.to_dict() for m in msgs],
        "deleted_ids": [t.message_id for t in tombstones],
        "max_seq":     max_seq,
        "has_more":    (page * per) < total,
        "total":       total,
    })


@chat_bp.route("/delta/all", methods=["GET"])
@jwt_required()
@safe_route
def delta_sync_all():
    """
    Global delta: returns all conversations + their latest seq.
    Used on app launch to know which conversations need syncing.
    """
    uid      = int(get_jwt_identity())
    since_seq = int(request.args.get("since_seq", 0))

    # All conversations this user participates in that have new activity
    convs = (db.session.query(
                 Message.conversation_id,
                 db.func.max(Message.server_seq).label("max_seq"),
                 db.func.count(Message.id).label("new_count"),
             )
             .filter(
                 db.or_(Message.sender_id == uid, Message.receiver_id == uid),
                 Message.server_seq > since_seq,
             )
             .group_by(Message.conversation_id)
             .all())

    return jsonify({
        "conversations": [
            {"conversation_id": c.conversation_id,
             "max_seq": c.max_seq,
             "new_count": c.new_count}
            for c in convs
        ],
        "since_seq": since_seq,
    })


# ── PATCH delete to write tombstone ─────────────────────────────────────────
# Override the existing delete route to also create a tombstone
@chat_bp.route("/tombstone/<int:msg_id>", methods=["DELETE"])
@jwt_required()
@safe_route
def delete_with_tombstone(msg_id):
    """
    Same as DELETE /api/chat/<id> but also writes a MessageTombstone
    so delta-sync clients learn about the deletion.
    """
    uid = int(get_jwt_identity())
    msg = Message.query.get_or_404(msg_id)

    if msg.sender_id != uid:
        return fail("You can only delete your own messages.", 403)

    conv_id = msg.conversation_id or _conv_id(msg.sender_id, msg.receiver_id)

    # Remove physical file
    if msg.file_url:
        fname = msg.file_url.rsplit("/", 1)[-1]
        fpath = os.path.join(current_app.config["UPLOAD_FOLDER"], fname)
        try:
            if os.path.isfile(fpath):
                os.remove(fpath)
        except OSError as exc:
            log.warning("File delete failed %s: %s", fpath, exc)

    # Assign a deletion seq so clients can order tombstones
    del_seq = ConversationSeq.next_seq(conv_id) if conv_id else 0

    tombstone = MessageTombstone(
        message_id      = msg_id,
        conversation_id = conv_id,
        deleted_seq     = del_seq,
    )
    db.session.add(tombstone)

    try:
        db.session.delete(msg)
        db.session.commit()
        log.info("Message %d deleted+tombstone seq=%d by user %d", msg_id, del_seq, uid)
    except Exception as exc:
        db.session.rollback()
        log.error("delete_with_tombstone failed: %s", exc)
        return fail("Failed to delete message.", 500)

    return jsonify({"success": True, "tombstone_seq": del_seq})


# ── MEDIA UPLOAD (pre-signed URL flow) ────────────────────────────────────────
import uuid as _uuid

@chat_bp.route("/upload-url", methods=["POST"])
@jwt_required()
@safe_route
def request_upload_url():
    """
    Generate a pre-signed S3 URL for direct client upload.

    If AWS is not configured, falls back to local storage and returns
    a direct upload URL to /api/chat/upload-local instead.

    Body JSON:
        filename   (str)
        mime_type  (str)
        size_bytes (int)
    """
    import re as _re
    uid  = int(get_jwt_identity())
    data = request.get_json() or {}

    raw_name  = data.get("filename", "file")
    mime_type = data.get("mime_type", "application/octet-stream")
    size      = int(data.get("size_bytes", 0))

    if size > 50 * 1024 * 1024:
        return fail("File too large. Maximum 50MB.")

    safe_name = _re.sub(r"[^a-zA-Z0-9._-]", "_", raw_name)[:80]
    key       = f"chat/{uid}/{_uuid.uuid4().hex}/{safe_name}"

    # Try S3 first
    s3_bucket = current_app.config.get("S3_BUCKET")
    aws_key   = current_app.config.get("AWS_ACCESS_KEY_ID")

    if s3_bucket and aws_key:
        try:
            import boto3
            s3 = boto3.client(
                "s3",
                aws_access_key_id     = aws_key,
                aws_secret_access_key = current_app.config.get("AWS_SECRET_ACCESS_KEY"),
                region_name           = current_app.config.get("AWS_REGION", "us-east-1"),
            )
            presigned = s3.generate_presigned_url(
                "put_object",
                Params   = {"Bucket": s3_bucket, "Key": key, "ContentType": mime_type},
                ExpiresIn = 300,
            )
            public_url = f"https://{s3_bucket}.s3.amazonaws.com/{key}"
            return jsonify({
                "mode":       "s3",
                "upload_url": presigned,
                "key":        key,
                "public_url": public_url,
            })
        except Exception as exc:
            log.warning("S3 presign failed, falling back to local: %s", exc)

    # Fallback: local upload token
    import secrets
    token = secrets.token_urlsafe(32)
    # Store token temporarily so upload-local can validate it
    from extensions import db as _db
    from db_models import UserSettings
    settings = UserSettings.query.filter_by(user_id=uid).first()
    # We reuse a simple cache — in production use Redis
    current_app.config.setdefault("_upload_tokens", {})[token] = {
        "uid": uid, "key": key, "mime": mime_type
    }
    return jsonify({
        "mode":       "local",
        "upload_url": f"/api/chat/upload-local",
        "key":        key,
        "token":      token,
    })


@chat_bp.route("/upload-local", methods=["POST"])
@jwt_required()
@safe_route
def upload_local():
    """
    Fallback: direct file upload to local storage (when S3 not configured).
    Returns the public URL that goes into Message.file_url.
    """
    import re as _re
    uid  = int(get_jwt_identity())
    f    = request.files.get("file")
    if not f:
        return fail("No file provided")

    raw   = f.filename or "file"
    ext   = raw.rsplit(".", 1)[-1].lower() if "." in raw else ""
    if ext not in {"jpg","jpeg","png","gif","webp","mp4","mov","webm","3gp","pdf"}:
        return fail(f"File type .{ext} not allowed")

    f.seek(0, 2); size = f.tell(); f.seek(0)
    if size > 50 * 1024 * 1024:
        return fail("File too large. Maximum 50MB.")

    stored = f"{_uuid.uuid4().hex}.{ext}"
    path   = os.path.join(current_app.config["UPLOAD_FOLDER"], stored)
    f.save(path)
    ftype  = "image" if ext in {"jpg","jpeg","png","gif","webp"} else              "video" if ext in {"mp4","mov","webm","3gp"} else "file"
    url    = f"/static/uploads/{stored}"
    log.info("Local upload: %s by user %d (%d bytes)", stored, uid, size)
    return jsonify({"success": True, "url": url, "file_type": ftype, "key": stored}), 201


@chat_bp.route("/confirm-upload", methods=["POST"])
@jwt_required()
@safe_route
def confirm_upload():
    """
    Call after S3 PUT succeeds. Links the S3 key to the message.
    Body: { local_id, key, public_url }
    """
    uid  = int(get_jwt_identity())
    data = request.get_json() or {}
    local_id   = data.get("local_id", "")
    s3_key     = data.get("key", "")
    public_url = data.get("public_url", "")

    msg = Message.query.filter_by(local_id=local_id, sender_id=uid).first()
    if not msg:
        return fail("Message not found", 404)

    msg.file_url  = public_url
    msg.s3_key    = s3_key

    try:
        db.session.commit()
        return jsonify({"success": True, "url": public_url})
    except Exception as exc:
        db.session.rollback()
        log.error("confirm_upload failed: %s", exc)
        return fail("Failed to update message.", 500)
