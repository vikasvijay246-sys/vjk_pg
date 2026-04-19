"""
api/utils.py — Shared helpers for every API blueprint.

Provides:
  ok(data, message, code)    → {"error": False, "message": ..., "data": ...}
  fail(message, code, field) → {"error": True,  "message": ..., "field": ...}
  paginate(query, page, per) → (items, total, pages)
"""
from __future__ import annotations
import logging
from functools import wraps
from flask import jsonify
from extensions import db

log = logging.getLogger("pg_manager.api")


# ── Response builders ──────────────────────────────────────────────────────────

def ok(data=None, message: str = "Success", code: int = 200):
    """Return a well-formed success response."""
    body = {"error": False, "message": message}
    if data is not None:
        if isinstance(data, dict):
            body.update(data)
        else:
            body["data"] = data
    return jsonify(body), code


def fail(message: str, code: int = 400, field: str | None = None):
    """Return a well-formed error response."""
    body = {"error": True, "message": message}
    if field:
        body["field"] = field
    return jsonify(body), code


# ── Pagination ─────────────────────────────────────────────────────────────────

def paginate(query, page: int, per: int):
    """
    Returns (items, total, pages) from a SQLAlchemy query.
    Clamps `per` to 1-100.
    """
    per   = max(1, min(per, 100))
    page  = max(1, page)
    total = query.count()
    pages = max(1, (total + per - 1) // per)
    items = query.offset((page - 1) * per).limit(per).all()
    return items, total, pages


# ── Safe route decorator ───────────────────────────────────────────────────────

def safe_route(fn):
    """
    Decorator that wraps a route in try/except.
    Rolls back the DB session on error and returns a structured JSON 500.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            db.session.rollback()
            log.exception("Unhandled error in %s: %s", fn.__name__, exc)
            return fail("An unexpected error occurred. Please try again.", 500)
    return wrapper
