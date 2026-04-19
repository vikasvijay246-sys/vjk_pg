import logging
"""
from api.utils import ok, fail, paginate, safe_route
api/social.py — Community Feed, Short Videos, Likes, Comments
Supports: text posts, image posts, short videos, notice board
Real-time likes/comments pushed via Socket.IO
"""

import os, uuid, json
from datetime import datetime
from flask import Blueprint, request, jsonify, current_app, Response
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt
from extensions import db, socketio
from db_models import Post, PostLike, Comment, User, Notification

social_bp = Blueprint("social", __name__, url_prefix="/api/social")

ALLOWED_VIDEO = {"mp4", "mov", "webm", "3gp"}
ALLOWED_IMAGE = {"jpg", "jpeg", "png", "gif", "webp"}

REACTIONS = ["❤️", "🔥", "😂", "😮", "👏", "😢"]


# ── Helpers ────────────────────────────────────────────────
def _save_media(field, allowed):
    f = request.files.get(field)
    if not f or not f.filename:
        return None
    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    if ext not in allowed:
        return None
    name = f"{uuid.uuid4().hex}.{ext}"
    f.save(os.path.join(current_app.config["UPLOAD_FOLDER"], name))
    return f"/static/uploads/{name}", os.path.getsize(
        os.path.join(current_app.config["UPLOAD_FOLDER"], name)
    )


def _push_live(event, room, data):
    """Push a Socket.IO event to a post room (non-blocking)."""
    try:
        socketio.emit(event, data, room=room)
    except Exception:
        pass


# ── Feed (paginated, filtered by type & visibility) ────────
@social_bp.route("/feed", methods=["GET"])
@jwt_required()
def feed():
    uid       = int(get_jwt_identity())
    page      = int(request.args.get("page", 1))
    per       = int(request.args.get("per_page", 10))
    feed_type = request.args.get("type", "all")   # all | video | image | text | personal

    q = Post.query.filter_by(is_active=True)

    if feed_type == "video":
        q = q.filter_by(post_type="video")
    elif feed_type == "image":
        q = q.filter_by(post_type="image")
    elif feed_type == "text":
        q = q.filter(Post.post_type.in_(["text", "notice"]))
    elif feed_type == "personal":
        q = q.filter_by(author_id=uid)

    total = q.count()
    posts = q.order_by(Post.created_at.desc()).offset((page - 1) * per).limit(per).all()

    return jsonify({
        "posts": [p.to_dict(viewer_id=uid) for p in posts],
        "total": total,
        "page": page,
        "pages": max(1, (total + per - 1) // per)
    })


# ── Create Post ────────────────────────────────────────────
@social_bp.route("/posts", methods=["POST"])
@jwt_required()
def create_post():
    uid = int(get_jwt_identity())
    is_mp = request.content_type and "multipart" in request.content_type
    data = request.form if is_mp else (request.get_json() or {})

    post_type  = str(data.get("post_type", "text")).lower()
    caption    = str(data.get("caption", "")).strip()
    visibility = str(data.get("visibility", "pg"))

    if not caption and post_type == "text":
        return jsonify({"error": "Caption required for text posts"}), 400

    media_url = thumbnail_url = None
    media_size = duration_sec = None

    if is_mp:
        if post_type == "video":
            result = _save_media("video", ALLOWED_VIDEO)
            if result:
                media_url, media_size = result
            # Optional thumbnail
            thumb = _save_media("thumbnail", ALLOWED_IMAGE)
            if thumb:
                thumbnail_url, _ = thumb
        elif post_type == "image":
            result = _save_media("image", ALLOWED_IMAGE)
            if result:
                media_url, media_size = result

    duration_sec = int(data.get("duration_sec", 0)) or None

    post = Post(
        author_id=uid, post_type=post_type, caption=caption,
        media_url=media_url, thumbnail_url=thumbnail_url,
        duration_sec=duration_sec, media_size=media_size,
        visibility=visibility, is_active=True
    )
    db.session.add(post)
    db.session.commit()

    # Push new post to feed room
    _push_live("new_post", "feed", post.to_dict(viewer_id=uid))

    return jsonify({"success": True, "post": post.to_dict(viewer_id=uid)}), 201


# ── Single Post ────────────────────────────────────────────
@social_bp.route("/posts/<int:pid>", methods=["GET"])
@jwt_required()
def get_post(pid):
    uid = int(get_jwt_identity())
    p = Post.query.filter_by(id=pid, is_active=True).first_or_404()
    # Increment view count
    p.views_count = (p.views_count or 0) + 1
    db.session.commit()
    return jsonify(p.to_dict(viewer_id=uid))


@social_bp.route("/posts/<int:pid>", methods=["DELETE"])
@jwt_required()
def delete_post(pid):
    uid    = int(get_jwt_identity())
    claims = get_jwt()
    p = Post.query.get_or_404(pid)
    if p.author_id != uid and claims.get("role") not in ("owner", "worker"):
        return jsonify({"error": "Access denied"}), 403
    p.is_active = False
    db.session.commit()
    _push_live("post_deleted", "feed", {"post_id": pid})
    return jsonify({"success": True})


# ── Video streaming with byte-range support ────────────────
@social_bp.route("/stream/<path:filename>", methods=["GET"])
@jwt_required()
def stream_video(filename):
    """HTTP Range-request video streaming for mobile."""
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], os.path.basename(filename))
    if not os.path.exists(path):
        return jsonify({"error": "Not found"}), 404

    file_size = os.path.getsize(path)
    range_header = request.headers.get("Range")

    ext = filename.rsplit(".", 1)[-1].lower()
    mime = {"mp4": "video/mp4", "webm": "video/webm",
            "mov": "video/quicktime", "3gp": "video/3gpp"}.get(ext, "video/mp4")

    if not range_header:
        with open(path, "rb") as f:
            data = f.read()
        resp = Response(data, 200, mimetype=mime)
        resp.headers["Content-Length"] = file_size
        resp.headers["Accept-Ranges"] = "bytes"
        return resp

    # Parse range
    byte_start, byte_end = 0, file_size - 1
    parts = range_header.replace("bytes=", "").split("-")
    if parts[0]:  byte_start = int(parts[0])
    if parts[1]:  byte_end   = int(parts[1])

    length = byte_end - byte_start + 1
    with open(path, "rb") as f:
        f.seek(byte_start)
        chunk = f.read(length)

    resp = Response(chunk, 206, mimetype=mime, direct_passthrough=True)
    resp.headers["Content-Range"] = f"bytes {byte_start}-{byte_end}/{file_size}"
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Content-Length"] = str(length)
    return resp


# ── Toggle Like (Live reaction) ────────────────────────────
@social_bp.route("/posts/<int:pid>/like", methods=["POST"])
@jwt_required()
def toggle_like(pid):
    uid  = int(get_jwt_identity())
    data = request.get_json() or {}
    reaction = data.get("reaction", "❤️")
    if reaction not in REACTIONS:
        reaction = "❤️"

    post = Post.query.filter_by(id=pid, is_active=True).first_or_404()
    existing = PostLike.query.filter_by(post_id=pid, user_id=uid).first()

    if existing:
        # Unlike
        db.session.delete(existing)
        post.likes_count = max(0, (post.likes_count or 0) - 1)
        liked = False
    else:
        # Like
        db.session.add(PostLike(post_id=pid, user_id=uid, reaction=reaction))
        post.likes_count = (post.likes_count or 0) + 1
        liked = True

        # Notify post author (not yourself)
        if post.author_id != uid:
            liker = User.query.get(uid)
            db.session.add(Notification(
                user_id=post.author_id,
                notif_type="like",
                title="New reaction!",
                body=f"{liker.name if liker else 'Someone'} reacted {reaction} to your post",
                data_json=json.dumps({"post_id": pid})
            ))

    db.session.commit()

    payload = {
        "post_id": pid,
        "likes_count": post.likes_count,
        "user_id": uid,
        "liked": liked,
        "reaction": reaction
    }
    # Broadcast live to everyone watching this post
    _push_live("post_liked", f"post:{pid}", payload)
    _push_live("post_liked", "feed", payload)

    return jsonify({"success": True, **payload})


# ── Comments ───────────────────────────────────────────────
@social_bp.route("/posts/<int:pid>/comments", methods=["GET"])
@jwt_required()
def get_comments(pid):
    page = int(request.args.get("page", 1))
    per  = int(request.args.get("per_page", 20))

    # Top-level comments only
    comments = (Comment.query
                .filter_by(post_id=pid, parent_id=None, is_active=True)
                .order_by(Comment.created_at.desc())
                .offset((page - 1) * per).limit(per).all())

    result = []
    for c in comments:
        d = c.to_dict()
        # Attach first 3 replies
        d["replies"] = [r.to_dict() for r in
                        c.replies.filter_by(is_active=True)
                        .order_by(Comment.created_at.asc()).limit(3).all()]
        result.append(d)

    total = Comment.query.filter_by(post_id=pid, parent_id=None, is_active=True).count()
    return jsonify({"comments": result, "total": total})


@social_bp.route("/posts/<int:pid>/comments", methods=["POST"])
@jwt_required()
def add_comment(pid):
    uid  = int(get_jwt_identity())
    data = request.get_json() or {}
    text = str(data.get("text", "")).strip()
    parent_id = data.get("parent_id")

    if not text:
        return jsonify({"error": "Comment text required"}), 400

    post = Post.query.filter_by(id=pid, is_active=True).first_or_404()

    c = Comment(post_id=pid, author_id=uid, text=text,
                parent_id=int(parent_id) if parent_id else None)
    db.session.add(c)
    post.comments_count = (post.comments_count or 0) + 1

    # Notify post author
    if post.author_id != uid:
        commenter = User.query.get(uid)
        db.session.add(Notification(
            user_id=post.author_id,
            notif_type="comment",
            title="New comment",
            body=f"{commenter.name if commenter else 'Someone'}: {text[:60]}",
            data_json=json.dumps({"post_id": pid})
        ))

    db.session.commit()

    payload = {"post_id": pid, "comment": c.to_dict(),
               "comments_count": post.comments_count}
    _push_live("new_comment", f"post:{pid}", payload)
    _push_live("new_comment", "feed", payload)

    return jsonify({"success": True, **payload}), 201


@social_bp.route("/comments/<int:cid>", methods=["DELETE"])
@jwt_required()
def delete_comment(cid):
    uid = int(get_jwt_identity())
    claims = get_jwt()
    c = Comment.query.get_or_404(cid)
    if c.author_id != uid and claims.get("role") not in ("owner", "worker"):
        return jsonify({"error": "Access denied"}), 403
    c.is_active = False
    post = Post.query.get(c.post_id)
    if post:
        post.comments_count = max(0, (post.comments_count or 0) - 1)
    db.session.commit()
    return jsonify({"success": True})
