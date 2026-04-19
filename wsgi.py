"""
wsgi.py — Production entry point.

Priority order for async mode (most stable first):
  1. SOCKETIO_ASYNC_MODE env var (explicit)
  2. gevent  (if installed — best for Render)
  3. threading (always available — safe fallback)

gevent.monkey.patch_all() MUST run before ANY other imports.
"""
import os
import sys

# ── Resolve async mode ─────────────────────────────────────────────────────
_mode = os.environ.get("SOCKETIO_ASYNC_MODE", "auto").lower()

if _mode in ("gevent", "auto"):
    try:
        from gevent import monkey
        monkey.patch_all()
        _mode = "gevent"
        os.environ["SOCKETIO_ASYNC_MODE"] = "gevent"
    except ImportError:
        _mode = "threading"
        os.environ["SOCKETIO_ASYNC_MODE"] = "threading"
        print("[wsgi] gevent not found, using threading mode", file=sys.stderr)

elif _mode == "eventlet":
    try:
        import eventlet
        eventlet.monkey_patch()
        os.environ["SOCKETIO_ASYNC_MODE"] = "eventlet"
    except ImportError:
        _mode = "threading"
        os.environ["SOCKETIO_ASYNC_MODE"] = "threading"
        print("[wsgi] eventlet not found, using threading mode", file=sys.stderr)

# ── Import app AFTER monkey-patching ──────────────────────────────────────
from app import app  # noqa — triggers create_app()

__all__ = ["app"]
