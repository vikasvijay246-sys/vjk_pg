"""
security.py — Production-grade security utilities
  • Input validation (phone, UPI, name, amount)
  • Input sanitization (XSS prevention)
  • Audit logging
  • Account lock management
  • Structured error responses
"""

import re
import json
import bleach
from datetime import datetime, timedelta
from flask import request, jsonify

# ── Shared imports (lazy to avoid circular) ────────────────────────────────
def _get_db():
    from extensions import db
    return db

def _get_bcrypt():
    from extensions import bcrypt
    return bcrypt


# ══════════════════════════════════════════════════════════════════════════════
#  VALIDATION RULES
# ══════════════════════════════════════════════════════════════════════════════

# Indian phone: 10 digits starting with 6-9 (no +91 prefix required)
PHONE_RE    = re.compile(r"^[6-9]\d{9}$")
# Lenient name: letters, spaces, dots, hyphens, 2–80 chars
NAME_RE     = re.compile(r"^[A-Za-z\u0900-\u097F\u0C00-\u0C7F .'\-]{2,80}$")
# UPI ID: user@bank or user.name@bank
UPI_RE      = re.compile(r"^[\w.\-_]{2,64}@[\w]{2,20}$")
# Room number: alphanumeric + basic punctuation, 1–10 chars
ROOM_RE     = re.compile(r"^[A-Za-z0-9\-/]{1,10}$")


def validate_phone(value: str) -> str:
    """Return cleaned 10-digit phone or raise ValueError."""
    raw = str(value or "").strip()
    # Remove leading + sign
    if raw.startswith("+"): raw = raw[1:]
    # Strip country code prefixes: +91 or 0091
    for prefix in ("0091", "91"):
        if raw.startswith(prefix) and len(raw) > len(prefix):
            candidate = raw[len(prefix):]
            if len(candidate) == 10:
                raw = candidate
                break
    v = re.sub(r"\D", "", raw)
    if not v:
        raise ValueError("Phone number is required")
    if len(v) != 10:
        raise ValueError("Phone number must be exactly 10 digits")
    if not PHONE_RE.match(v):
        raise ValueError("Please enter a valid Indian mobile number (starts with 6-9)")
    return v


def validate_name(value: str) -> str:
    """Return cleaned name or raise ValueError."""
    v = str(value or "").strip()
    if not v:
        raise ValueError("Name is required")
    if len(v) < 2:
        raise ValueError("Name must be at least 2 characters")
    if len(v) > 80:
        raise ValueError("Name must be less than 80 characters")
    if not NAME_RE.match(v):
        raise ValueError("Name must contain only letters and spaces")
    return v


def validate_amount(value, field="Amount") -> float:
    """Return positive float or raise ValueError."""
    try:
        v = float(str(value or "").strip())
    except (ValueError, TypeError):
        raise ValueError(f"{field} must be a number")
    if v <= 0:
        raise ValueError(f"{field} must be greater than zero")
    if v > 10_000_000:
        raise ValueError(f"{field} is unreasonably large")
    return round(v, 2)


def validate_upi(value: str) -> str:
    """Return cleaned UPI ID or raise ValueError."""
    v = str(value or "").strip().lower()
    if not v:
        raise ValueError("UPI ID is required")
    if not UPI_RE.match(v):
        raise ValueError(
            "Please enter a valid UPI ID (e.g. name@upi, name@okicici)"
        )
    return v


def validate_room_number(value: str) -> str:
    v = str(value or "").strip().upper()
    if not v:
        raise ValueError("Room number is required")
    if not ROOM_RE.match(v):
        raise ValueError("Room number must be 1-10 alphanumeric characters")
    return v


def validate_otp(value: str) -> str:
    v = re.sub(r"\D", "", str(value or "").strip())
    if len(v) != 6:
        raise ValueError("OTP must be exactly 6 digits")
    return v


def validate_password(value: str) -> str:
    v = str(value or "").strip()
    if len(v) < 6:
        raise ValueError("Password must be at least 6 characters")
    if len(v) > 128:
        raise ValueError("Password is too long (max 128 chars)")
    return v


# ══════════════════════════════════════════════════════════════════════════════
#  SANITIZATION  (XSS / HTML injection prevention)
# ══════════════════════════════════════════════════════════════════════════════

def sanitize(value: str, max_len: int = 500) -> str:
    """Strip all HTML tags and limit length."""
    if not value:
        return ""
    cleaned = bleach.clean(str(value), tags=[], strip=True)
    return cleaned[:max_len].strip()


def sanitize_text(value: str, max_len: int = 2000) -> str:
    """Allow only plain text; remove all HTML."""
    return sanitize(value, max_len)


# ══════════════════════════════════════════════════════════════════════════════
#  STRUCTURED ERROR RESPONSES
# ══════════════════════════════════════════════════════════════════════════════

def err(message: str, code: int = 400, field: str = None):
    """Return a structured JSON error response."""
    body = {"error": True, "message": message}
    if field:
        body["field"] = field
    return jsonify(body), code


def ok_response(data: dict = None, message: str = None, code: int = 200):
    body = {"error": False}
    if message:
        body["message"] = message
    if data:
        body.update(data)
    return jsonify(body), code


# ══════════════════════════════════════════════════════════════════════════════
#  ACCOUNT LOCK  (brute-force protection)
# ══════════════════════════════════════════════════════════════════════════════

MAX_FAIL_ATTEMPTS = 5
LOCK_DURATION_MIN = 30   # minutes to lock account


def check_account_locked(phone: str) -> bool:
    """Return True if account is currently locked."""
    from db_models import AccountLock
    db = _get_db()
    record = AccountLock.query.filter_by(phone=phone).first()
    if not record:
        return False
    if record.locked_until and record.locked_until > datetime.utcnow():
        return True
    return False


def get_lock_remaining(phone: str) -> int:
    """Return seconds remaining on lock, or 0."""
    from db_models import AccountLock
    record = AccountLock.query.filter_by(phone=phone).first()
    if not record or not record.locked_until:
        return 0
    remaining = (record.locked_until - datetime.utcnow()).total_seconds()
    return max(0, int(remaining))


def record_failed_attempt(phone: str) -> int:
    """Increment fail count, lock if threshold reached. Returns fail count."""
    from db_models import AccountLock
    db = _get_db()
    record = AccountLock.query.filter_by(phone=phone).first()
    if not record:
        record = AccountLock(phone=phone, fail_count=0)
        db.session.add(record)

    record.fail_count = (record.fail_count or 0) + 1
    record.last_attempt = datetime.utcnow()
    record.updated_at = datetime.utcnow()

    if record.fail_count >= MAX_FAIL_ATTEMPTS:
        record.locked_until = datetime.utcnow() + timedelta(minutes=LOCK_DURATION_MIN)

    db.session.commit()
    return record.fail_count


def clear_failed_attempts(phone: str):
    """Reset after successful login."""
    from db_models import AccountLock
    db = _get_db()
    record = AccountLock.query.filter_by(phone=phone).first()
    if record:
        record.fail_count = 0
        record.locked_until = None
        record.updated_at = datetime.utcnow()
        db.session.commit()


# ══════════════════════════════════════════════════════════════════════════════
#  ACTIVITY / AUDIT LOG
# ══════════════════════════════════════════════════════════════════════════════

def log_activity(user_id, action: str, detail: dict = None):
    """Write one audit record. Silently swallows exceptions to not break flows."""
    try:
        from db_models import ActivityLog
        db = _get_db()
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
        if ip and "," in ip:
            ip = ip.split(",")[0].strip()
        ua = request.headers.get("User-Agent", "")[:255]

        entry = ActivityLog(
            user_id=user_id,
            action=action,
            detail=json.dumps(detail or {}, ensure_ascii=False)[:1000],
            ip_address=ip[:45],
            user_agent=ua,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception as exc:
        # Never let audit logging crash a business flow
        try:
            _get_db().session.rollback()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  OTP MANAGEMENT  (DB-backed, hashed)
# ══════════════════════════════════════════════════════════════════════════════

OTP_TTL_SECONDS   = 300   # 5 minutes
OTP_MAX_ATTEMPTS  = 5     # wrong-OTP attempts before invalidation
OTP_RATE_WINDOW   = 300   # 5-minute window
OTP_MAX_PER_WINDOW = 3    # max OTPs allowed per window


def _hash_otp(otp: str) -> str:
    return _get_bcrypt().generate_password_hash(otp).decode("utf-8")


def _check_otp(otp: str, hashed: str) -> bool:
    return _get_bcrypt().check_password_hash(hashed, otp)


def create_otp(phone: str, otp: str):
    """
    Store a new OTP for phone. Enforces rate-limit: max 3 OTPs per 5 minutes.
    Raises ValueError if rate limit exceeded.
    Returns the OTPRecord.
    """
    from db_models import OTPRecord
    db = _get_db()
    now = datetime.utcnow()
    window_start = now - timedelta(seconds=OTP_RATE_WINDOW)

    # Count ALL OTPs sent in this window (used or not) to enforce rate limit
    recent = OTPRecord.query.filter(
        OTPRecord.phone == phone,
        OTPRecord.created_at >= window_start
    ).count()

    if recent >= OTP_MAX_PER_WINDOW:
        raise ValueError(
            f"Too many OTP requests. Please wait {OTP_RATE_WINDOW // 60} minutes before trying again."
        )

    # Invalidate any old unused OTPs for this phone
    OTPRecord.query.filter_by(phone=phone, is_used=False).update({"is_used": True})

    record = OTPRecord(
        phone=phone,
        otp_hash=_hash_otp(otp),
        expires_at=now + timedelta(seconds=OTP_TTL_SECONDS),
        attempts=0,
        send_count=recent + 1,
        window_start=now,
    )
    db.session.add(record)
    db.session.commit()
    return record


def verify_otp(phone: str, otp: str):
    """
    Verify OTP for phone.
    Returns (True, None) on success.
    Returns (False, error_message) on failure.
    Invalidates the record on success or too many attempts.
    """
    from db_models import OTPRecord
    db = _get_db()
    now = datetime.utcnow()

    record = OTPRecord.query.filter_by(
        phone=phone, is_used=False
    ).order_by(OTPRecord.created_at.desc()).first()

    if not record:
        return False, "No OTP was requested for this number. Please request a new one."

    if now > record.expires_at:
        record.is_used = True
        db.session.commit()
        return False, "Your OTP has expired. Please request a new one."

    record.attempts += 1

    if record.attempts > OTP_MAX_ATTEMPTS:
        record.is_used = True
        db.session.commit()
        return False, "Too many incorrect attempts. Please request a new OTP."

    if not _check_otp(otp, record.otp_hash):
        remaining = OTP_MAX_ATTEMPTS - record.attempts
        db.session.commit()
        return False, f"Incorrect OTP. {remaining} attempt(s) remaining."

    # ✅ Correct
    record.is_used = True
    db.session.commit()
    return True, None
