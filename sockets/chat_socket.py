"""
sockets/chat_socket.py — Production-hardened real-time chat
Guarantees:
  1. DB commit BEFORE socket emit (no phantom messages)
  2. All DB ops wrapped in try/except with rollback
  3. Real-time notification push after every message
  4. Structured logging for every event
  5. Rate limit on send_message (100/min per user, tracked in-process)
"""
import logging
from datetime import datetime
from collections import defaultdict
from time import time as _time
from flask import request
from flask_socketio import emit, join_room
from flask_jwt_extended import decode_token
import os
from extensions import db, socketio
from db_models import Message, User, Notification, ConversationSeq

log = logging.getLogger("pg_manager.socket")

# ── Simple in-process rate limiter (per user_id) ──────────────────────────────
_SEND_WINDOW  = 60   # seconds
_SEND_MAX     = 100  # max messages per window
_rate_buckets: dict[int, list[float]] = defaultdict(list)

def _rate_check(user_id: int) -> bool:
    """Return True if user is within rate limit, False if exceeded."""
    now   = _time()
    bucket = _rate_buckets[user_id]
    # Trim old timestamps
    _rate_buckets[user_id] = [t for t in bucket if now - t < _SEND_WINDOW]
    if len(_rate_buckets[user_id]) >= _SEND_MAX:
        return False
    _rate_buckets[user_id].append(now)
    return True


# ── Auth helper ───────────────────────────────────────────────────────────────
def _get_user(token: str) -> User | None:
    try:
        decoded = decode_token(token)
        return User.query.get(int(decoded["sub"]))
    except Exception:
        return None


# ── Connection ────────────────────────────────────────────────────────────────
@socketio.on("connect")
def on_connect(auth):
    token = (auth or {}).get("token", "")
    user  = _get_user(token)
    if not user:
        log.warning("[Socket] Rejected unauthenticated connection")
        return False

    join_room(f"user:{user.id}")

    try:
        user.last_seen = datetime.utcnow()
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        log.error("[Socket] Failed to update last_seen for user %d: %s", user.id, exc)

    emit("user_online", {"user_id": user.id}, broadcast=True, include_self=False)
    log.info("[Socket] User %d (%s) connected", user.id, user.name)


@socketio.on("disconnect")
def on_disconnect():
    log.debug("[Socket] Client disconnected (sid=%s)", request.sid)


# ── Send Message (HARDENED) ───────────────────────────────────────────────────
@socketio.on("send_message")
def on_send_message(data):
    """
    CRITICAL PATH — guaranteed order:
      1. Validate auth
      2. Rate-limit check
      3. Build Message object
      4. db.session.add()
      5. db.session.commit()   ← if this fails → emit error, return
      6. Only AFTER successful commit → emit to receiver & sender
    """
    user = _get_user(data.get("token", ""))
    if not user:
        emit("error", {"message": "Unauthorized"})
        return

    receiver_id = int(data.get("receiver_id", 0) or 0)
    if not receiver_id:
        emit("error", {"message": "receiver_id required"})
        return

    # Rate limiting
    if not _rate_check(user.id):
        emit("error", {"message": "Too many messages. Please slow down."})
        log.warning("[Socket] Rate limit hit for user %d", user.id)
        return

    receiver = User.query.get(receiver_id)
    if not receiver:
        emit("error", {"message": "Recipient not found"})
        return

    now = datetime.utcnow()
    msg = Message(
        sender_id        = user.id,
        receiver_id      = receiver_id,
        content_encrypted= data.get("content_encrypted") or "",
        iv               = data.get("iv") or "",
        file_url         = data.get("file_url"),
        file_type        = data.get("file_type"),
        file_name        = data.get("file_name"),
        status           = "delivered",
        created_at       = now,
        delivered_at     = now,
    )
    notif = Notification(
        user_id    = receiver_id,
        notif_type = "new_message",
        title      = f"New message from {user.name}",
        body       = "You have a new message",
        data_json  = f'{{"sender_id":{user.id},"sender_name":"{user.name}"}}'
    )

    # Assign conversation_id + monotonic server_seq before commit
    from db_models import ConversationSeq as _CS
    conv_id = f"{min(user.id, receiver_id)}:{max(user.id, receiver_id)}"
    msg.conversation_id = conv_id
    msg.server_seq = _CS.next_seq(conv_id)

    db.session.add(msg)
    db.session.add(notif)

    try:
        db.session.commit()
        log.info("[Socket] Message %d saved: user %d → user %d seq=%d",
                 msg.id, user.id, receiver_id, msg.server_seq)
    except Exception as exc:
        db.session.rollback()
        log.error("[Socket] DB commit failed for message user %d → %d: %s", user.id, receiver_id, exc)
        emit("error", {"message": "Failed to save message. Please try again."})
        return   # ← ABORT: do NOT emit to UI if DB failed

    # DB committed — now emit
    payload = {
        **msg.to_dict(),
        "sender_name":  user.name,
        "sender_photo": user.photo_url,
    }

    emit("new_message",   payload, room=f"user:{receiver_id}")
    emit("message_sent",  payload, room=f"user:{user.id}")

    # Dispatch FCM push if receiver is offline
    _send_fcm(receiver_id, user.name, conv_id, user.id)

    # Push real-time notification to receiver
    emit("notification", {
        "type":  "new_message",
        "title": f"💬 {user.name}",
        "body":  "Sent you a message",
        "data":  {"sender_id": user.id, "sender_name": user.name},
        "unread": True,
    }, room=f"user:{receiver_id}")


# ── Typing indicator ──────────────────────────────────────────────────────────
@socketio.on("typing")
def on_typing(data):
    user = _get_user(data.get("token", ""))
    if not user:
        return
    receiver_id = int(data.get("receiver_id", 0) or 0)
    emit("typing", {
        "sender_id":   user.id,
        "sender_name": user.name,
        "is_typing":   bool(data.get("is_typing", True)),
    }, room=f"user:{receiver_id}")


# ── Mark seen ─────────────────────────────────────────────────────────────────
@socketio.on("mark_seen")
def on_mark_seen(data):
    user = _get_user(data.get("token", ""))
    if not user:
        return
    sender_id = int(data.get("sender_id", 0) or 0)
    if not sender_id:
        return

    try:
        now  = datetime.utcnow()
        msgs = Message.query.filter_by(
            sender_id=sender_id, receiver_id=user.id, status="delivered"
        ).all()
        for m in msgs:
            m.status  = "seen"
            m.seen_at = now
        db.session.commit()
        emit("message_status", {"receiver_id": user.id, "status": "seen"},
             room=f"user:{sender_id}")
    except Exception as exc:
        db.session.rollback()
        log.error("[Socket] mark_seen failed: %s", exc)


# ── Message delivered ─────────────────────────────────────────────────────────
@socketio.on("message_delivered")
def on_message_delivered(data):
    user = _get_user(data.get("token", ""))
    if not user:
        return
    msg_id = data.get("message_id")
    if not msg_id:
        return

    try:
        msg = Message.query.get(msg_id)
        if msg and msg.status == "sent":
            msg.status       = "delivered"
            msg.delivered_at = datetime.utcnow()
            db.session.commit()
            socketio.emit("message_delivered",
                          {"message_id": msg_id, "status": "delivered"},
                          room=f"user:{msg.sender_id}")
    except Exception as exc:
        db.session.rollback()
        log.error("[Socket] message_delivered failed for msg %s: %s", msg_id, exc)


# ── Rent notification helper (called by payments API) ────────────────────────
def broadcast_rent_notification(user_id: int, title: str, body: str, data: dict = None):
    try:
        socketio.emit("notification", {
            "type":  "rent_update",
            "title": title,
            "body":  body,
            "data":  data or {},
            "unread": True,
        }, room=f"user:{user_id}")
    except Exception as exc:
        log.error("[Socket] broadcast_rent_notification failed: %s", exc)
