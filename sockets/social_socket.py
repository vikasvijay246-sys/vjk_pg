"""
sockets/social_socket.py — Real-time social events
Events:
  join_feed     → join/leave the global feed room
  join_post     → join a specific post room for live reactions
  live_reaction → broadcast emoji burst to post room
"""

from extensions import socketio
from flask_jwt_extended import decode_token


def _get_uid(token):
    try:
        return int(decode_token(token)["sub"])
    except Exception:
        return None


@socketio.on("join_feed")
def on_join_feed(data):
    from flask_socketio import join_room
    uid = _get_uid((data or {}).get("token", ""))
    if not uid:
        return False
    join_room("feed")


@socketio.on("leave_feed")
def on_leave_feed(data):
    from flask_socketio import leave_room
    leave_room("feed")


@socketio.on("join_post")
def on_join_post(data):
    """Client joins a specific post room to receive live likes/comments."""
    from flask_socketio import join_room
    uid     = _get_uid((data or {}).get("token", ""))
    post_id = (data or {}).get("post_id")
    if not uid or not post_id:
        return
    join_room(f"post:{post_id}")


@socketio.on("leave_post")
def on_leave_post(data):
    from flask_socketio import leave_room
    post_id = (data or {}).get("post_id")
    if post_id:
        leave_room(f"post:{post_id}")


@socketio.on("live_reaction")
def on_live_reaction(data):
    """
    Broadcast a floating emoji reaction to all viewers of a post.
    data = { token, post_id, reaction }
    """
    uid     = _get_uid((data or {}).get("token", ""))
    post_id = (data or {}).get("post_id")
    reaction= (data or {}).get("reaction", "❤️")
    if not uid or not post_id:
        return

    socketio.emit("reaction_burst", {
        "post_id": post_id,
        "user_id": uid,
        "reaction": reaction
    }, room=f"post:{post_id}", include_self=False)
