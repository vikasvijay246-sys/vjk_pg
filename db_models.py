"""
db_models.py — All SQLAlchemy models for PG Manager Pro
Named db_models.py (NOT models.py) to avoid PyPI 'models' package conflict

Schema overview:
  User ──< Tenant ──< Payment
  User ──< Message (sender/receiver)
  User ──< Notification
  User ── UserSettings
  Room ──< Tenant
"""

from datetime import datetime
from extensions import db


# ─────────────────────────────────────────────────────────
# USER — covers Owner, Worker, Tenant roles
# ─────────────────────────────────────────────────────────
class User(db.Model):
    __tablename__ = "users"

    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(120), nullable=False)
    phone         = db.Column(db.String(20), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role          = db.Column(db.String(20), nullable=False, default="tenant")
    # role ∈ {"owner", "worker", "tenant", "admin"}

    photo_url     = db.Column(db.String(255))
    public_key    = db.Column(db.Text)     # ECDH public key for E2E chat encryption

    is_active     = db.Column(db.Boolean, default=True)
    last_seen     = db.Column(db.DateTime)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    tenant_profile = db.relationship("Tenant", backref="user", uselist=False,
                                     foreign_keys="Tenant.user_id")
    settings       = db.relationship("UserSettings", backref="user", uselist=False,
                                     cascade="all, delete-orphan")
    notifications  = db.relationship("Notification", backref="user", lazy="dynamic",
                                     cascade="all, delete-orphan")
    sent_messages  = db.relationship("Message", foreign_keys="Message.sender_id",
                                     backref="sender", lazy="dynamic")
    recv_messages  = db.relationship("Message", foreign_keys="Message.receiver_id",
                                     backref="receiver", lazy="dynamic")

    def to_dict(self, include_private=False):
        d = {
            "id": self.id,
            "name": self.name,
            "phone": self.phone,
            "role": self.role,
            "photo_url": self.photo_url,
            "is_active": self.is_active,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if include_private:
            d["public_key"] = self.public_key
        return d


# ─────────────────────────────────────────────────────────
# ROOM
# ─────────────────────────────────────────────────────────
class Room(db.Model):
    __tablename__ = "rooms"

    id          = db.Column(db.Integer, primary_key=True)
    room_number = db.Column(db.String(20), nullable=False, unique=True)
    floor       = db.Column(db.String(10))
    capacity    = db.Column(db.Integer, default=1)
    rent_price  = db.Column(db.Float, default=0.0)  # default rent for this room
    is_occupied = db.Column(db.Boolean, default=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    pg_id   = db.Column(db.Integer, db.ForeignKey("pg_properties.id"))
    tenants = db.relationship("Tenant", backref="room", lazy=True)

    def to_dict(self):
        return {
            "id": self.id,
            "room_number": self.room_number,
            "floor": self.floor,
            "capacity": self.capacity,
            "rent_price": self.rent_price,
            "is_occupied": self.is_occupied,
        }


# ─────────────────────────────────────────────────────────
# TENANT
# ─────────────────────────────────────────────────────────
class Tenant(db.Model):
    __tablename__ = "tenants"

    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    room_id      = db.Column(db.Integer, db.ForeignKey("rooms.id"))

    rent_amount  = db.Column(db.Float, nullable=False, default=0.0)
    joining_date = db.Column(db.String(10))   # YYYY-MM-DD
    vacating_date= db.Column(db.String(10))   # Set when they leave
    id_proof_url = db.Column(db.String(255))
    photo_url    = db.Column(db.String(255))
    is_active    = db.Column(db.Boolean, default=True)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

    payments = db.relationship("Payment", backref="tenant", lazy="dynamic",
                               cascade="all, delete-orphan")

    def to_dict(self, with_payment_status=False):
        d = {
            "id": self.id,
            "user_id": self.user_id,
            "name": self.user.name if self.user else "",
            "phone": self.user.phone if self.user else "",
            "photo_url": self.photo_url or (self.user.photo_url if self.user else None),
            "room_id": self.room_id,
            "room_number": self.room.room_number if self.room else "",
            "rent_amount": self.rent_amount,
            "joining_date": self.joining_date,
            "vacating_date": self.vacating_date,
            "id_proof_url": self.id_proof_url,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        return d


# ─────────────────────────────────────────────────────────
# PAYMENT
# ─────────────────────────────────────────────────────────
class Payment(db.Model):
    __tablename__ = "payments"

    id          = db.Column(db.Integer, primary_key=True)
    tenant_id   = db.Column(db.Integer, db.ForeignKey("tenants.id"), nullable=False)
    month       = db.Column(db.String(10))   # YYYY-MM-01
    amount      = db.Column(db.Float)
    is_paid     = db.Column(db.Boolean, default=False)
    paid_on     = db.Column(db.String(10))
    receipt_url = db.Column(db.String(255))  # Optional receipt image
    notes       = db.Column(db.String(255))
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "month": self.month,
            "amount": self.amount,
            "is_paid": self.is_paid,
            "paid_on": self.paid_on,
            "receipt_url": self.receipt_url,
            "notes": self.notes,
        }


# ─────────────────────────────────────────────────────────
# MESSAGE  (E2E encrypted — server stores ciphertext only)
# ─────────────────────────────────────────────────────────
class Message(db.Model):
    __tablename__ = "messages"

    id                = db.Column(db.Integer, primary_key=True)
    # ── Delta-sync identity ──────────────────────────────────────────────────
    local_id          = db.Column(db.String(64), unique=True, index=True)
    # UUID set by the sending device; lets server deduplicate retries
    server_seq        = db.Column(db.BigInteger, default=0, index=True)
    # Monotonic per-conversation counter assigned at commit time
    conversation_id   = db.Column(db.String(64), index=True)
    # "uid_a:uid_b" with lower uid first — stable across both participants
    is_deleted        = db.Column(db.Boolean, default=False, index=True)
    edited_at         = db.Column(db.DateTime)

    sender_id         = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    receiver_id       = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    # E2E: content is AES-GCM encrypted by sender's JS, stored as base64
    content_encrypted = db.Column(db.Text)
    iv                = db.Column(db.String(64))  # AES-GCM nonce (base64)

    # For file messages
    file_url          = db.Column(db.String(255))
    file_type         = db.Column(db.String(50))   # "image" | "video" | "file"
    file_name         = db.Column(db.String(255))
    s3_key            = db.Column(db.String(255))  # S3 object key for direct upload

    # Status
    status            = db.Column(db.String(20), default="sent")
    # status ∈ {"sent", "delivered", "seen"}
    delivered_at      = db.Column(db.DateTime)
    seen_at           = db.Column(db.DateTime)
    created_at        = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id":                self.id,
            "local_id":          self.local_id,
            "server_seq":        self.server_seq,
            "conversation_id":   self.conversation_id,
            "sender_id":         self.sender_id,
            "receiver_id":       self.receiver_id,
            "content_encrypted": self.content_encrypted,
            "iv":                self.iv,
            "file_url":          self.file_url,
            "file_type":         self.file_type,
            "file_name":         self.file_name,
            "status":            self.status,
            "is_deleted":        self.is_deleted,
            "edited_at":         self.edited_at.isoformat() if self.edited_at else None,
            "delivered_at":      self.delivered_at.isoformat() if self.delivered_at else None,
            "seen_at":           self.seen_at.isoformat() if self.seen_at else None,
            "created_at":        self.created_at.isoformat() if self.created_at else None,
        }


# ─────────────────────────────────────────────────────────
# NOTIFICATION
# ─────────────────────────────────────────────────────────
class Notification(db.Model):
    __tablename__ = "notifications"

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    notif_type = db.Column(db.String(30))
    # type ∈ {"new_message", "rent_paid", "rent_due", "reminder", "system"}
    title      = db.Column(db.String(120))
    body       = db.Column(db.String(500))
    is_read    = db.Column(db.Boolean, default=False)
    data_json  = db.Column(db.Text)   # JSON string for deep-link data
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "type": self.notif_type,
            "title": self.title,
            "body": self.body,
            "is_read": self.is_read,
            "data_json": self.data_json,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ─────────────────────────────────────────────────────────
# UPLOADED FILE (secure file registry)
# ─────────────────────────────────────────────────────────
class UploadedFile(db.Model):
    __tablename__ = "uploaded_files"

    id            = db.Column(db.Integer, primary_key=True)
    uploaded_by   = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    filename      = db.Column(db.String(255))      # stored name (UUID-based)
    original_name = db.Column(db.String(255))      # original filename
    file_type     = db.Column(db.String(50))
    file_size     = db.Column(db.Integer)
    access_roles  = db.Column(db.String(50), default="owner")  # who can download
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "filename": self.filename,
            "original_name": self.original_name,
            "file_type": self.file_type,
            "file_size": self.file_size,
            "url": f"/api/files/download/{self.filename}",
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ─────────────────────────────────────────────────────────
# PG PROPERTY  (multi-PG support)
# ─────────────────────────────────────────────────────────
class PGProperty(db.Model):
    __tablename__ = "pg_properties"

    id          = db.Column(db.Integer, primary_key=True)
    owner_id    = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    name        = db.Column(db.String(120), nullable=False)
    address     = db.Column(db.String(300))
    city        = db.Column(db.String(60))
    upi_id      = db.Column(db.String(80))   # e.g. owner@upi
    phone       = db.Column(db.String(20))
    is_active   = db.Column(db.Boolean, default=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    rooms       = db.relationship("Room", backref="pg_property", lazy=True,
                                  foreign_keys="Room.pg_id")

    def to_dict(self):
        return {
            "id": self.id, "owner_id": self.owner_id, "name": self.name,
            "address": self.address, "city": self.city,
            "upi_id": self.upi_id, "phone": self.phone,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }


# ─────────────────────────────────────────────────────────
# POST  (text/image/video community feed)
# ─────────────────────────────────────────────────────────
class Post(db.Model):
    __tablename__ = "posts"

    id           = db.Column(db.Integer, primary_key=True)
    author_id    = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    post_type    = db.Column(db.String(20), default="text")
    # post_type ∈ {"text", "image", "video", "notice"}

    caption      = db.Column(db.Text)
    media_url    = db.Column(db.String(255))      # image or video URL
    thumbnail_url= db.Column(db.String(255))      # video thumbnail
    duration_sec = db.Column(db.Integer)          # video duration
    media_size   = db.Column(db.Integer)          # bytes

    # Visibility
    visibility   = db.Column(db.String(20), default="pg")
    # ∈ {"pg"=all tenants in same PG, "personal"=only author, "public"=all users}
    pg_id        = db.Column(db.Integer, db.ForeignKey("pg_properties.id"))

    likes_count   = db.Column(db.Integer, default=0)
    comments_count= db.Column(db.Integer, default=0)
    views_count   = db.Column(db.Integer, default=0)
    is_active    = db.Column(db.Boolean, default=True)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

    author       = db.relationship("User", foreign_keys=[author_id])
    likes        = db.relationship("PostLike", backref="post", lazy="dynamic",
                                   cascade="all, delete-orphan")
    comments     = db.relationship("Comment", backref="post", lazy="dynamic",
                                   cascade="all, delete-orphan")

    def to_dict(self, viewer_id=None):
        d = {
            "id": self.id,
            "post_type": self.post_type,
            "caption": self.caption,
            "media_url": self.media_url,
            "thumbnail_url": self.thumbnail_url,
            "duration_sec": self.duration_sec,
            "visibility": self.visibility,
            "likes_count": self.likes_count,
            "comments_count": self.comments_count,
            "views_count": self.views_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "author": {
                "id": self.author.id,
                "name": self.author.name,
                "photo_url": self.author.photo_url,
                "role": self.author.role,
            } if self.author else {},
            "liked_by_me": False,
        }
        if viewer_id:
            d["liked_by_me"] = self.likes.filter_by(user_id=viewer_id).first() is not None
        return d


# ─────────────────────────────────────────────────────────
# POST LIKE
# ─────────────────────────────────────────────────────────
class PostLike(db.Model):
    __tablename__ = "post_likes"
    __table_args__ = (db.UniqueConstraint("post_id", "user_id"),)

    id         = db.Column(db.Integer, primary_key=True)
    post_id    = db.Column(db.Integer, db.ForeignKey("posts.id"), nullable=False)
    user_id    = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    reaction   = db.Column(db.String(10), default="❤️")
    # reaction ∈ {"❤️","🔥","😂","😮","👏","😢"}
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ─────────────────────────────────────────────────────────
# COMMENT
# ─────────────────────────────────────────────────────────
class Comment(db.Model):
    __tablename__ = "comments"

    id         = db.Column(db.Integer, primary_key=True)
    post_id    = db.Column(db.Integer, db.ForeignKey("posts.id"), nullable=False)
    author_id  = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    parent_id  = db.Column(db.Integer, db.ForeignKey("comments.id"))  # for replies
    text       = db.Column(db.Text, nullable=False)
    likes_count= db.Column(db.Integer, default=0)
    is_active  = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    author     = db.relationship("User", foreign_keys=[author_id])
    replies    = db.relationship("Comment", backref=db.backref("parent", remote_side=[id]),
                                 lazy="dynamic")

    def to_dict(self):
        return {
            "id": self.id,
            "post_id": self.post_id,
            "parent_id": self.parent_id,
            "text": self.text,
            "likes_count": self.likes_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "author": {
                "id": self.author.id,
                "name": self.author.name,
                "photo_url": self.author.photo_url,
            } if self.author else {},
        }


# ─────────────────────────────────────────────────────────
# WHATSAPP REMINDER LOG
# ─────────────────────────────────────────────────────────
class ReminderLog(db.Model):
    __tablename__ = "reminder_logs"

    id          = db.Column(db.Integer, primary_key=True)
    tenant_id   = db.Column(db.Integer, db.ForeignKey("tenants.id"))
    channel     = db.Column(db.String(20), default="whatsapp")  # whatsapp | sms | app
    message     = db.Column(db.Text)
    status      = db.Column(db.String(20), default="queued")  # queued|sent|failed
    sent_at     = db.Column(db.DateTime)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id, "tenant_id": self.tenant_id,
            "channel": self.channel, "message": self.message,
            "status": self.status,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
        }


# ─────────────────────────────────────────────────────────
# USER SETTINGS
# ─────────────────────────────────────────────────────────
class UserSettings(db.Model):
    __tablename__ = "user_settings"

    id                  = db.Column(db.Integer, primary_key=True)
    user_id             = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True)
    language            = db.Column(db.String(10), default="en")   # en | te | hi
    theme               = db.Column(db.String(20), default="light") # light | dark | high_contrast
    notify_rent         = db.Column(db.Boolean, default=True)
    notify_messages     = db.Column(db.Boolean, default=True)
    notify_reminders    = db.Column(db.Boolean, default=True)
    rent_due_day        = db.Column(db.Integer, default=1)  # day of month rent is due
    updated_at          = db.Column(db.DateTime, default=datetime.utcnow)
    # ── Push notifications ─────────────────────────────────────────────────
    fcm_token           = db.Column(db.String(255))   # Firebase Cloud Messaging device token
    push_enabled        = db.Column(db.Boolean, default=True)
    last_seq_synced     = db.Column(db.BigInteger, default=0)  # global delta-sync cursor

    def to_dict(self):
        return {
            "language": self.language,
            "theme": self.theme,
            "notify_rent": self.notify_rent,
            "notify_messages": self.notify_messages,
            "notify_reminders": self.notify_reminders,
            "rent_due_day": self.rent_due_day,
            "push_enabled": self.push_enabled,
        }


# ─────────────────────────────────────────────────────────
# OTP TABLE  (secure, DB-backed, replaces in-memory store)
# ─────────────────────────────────────────────────────────
class OTPRecord(db.Model):
    """One row per active OTP request. Cleaned up on verify or expiry."""
    __tablename__ = "otp_records"

    id           = db.Column(db.Integer, primary_key=True)
    phone        = db.Column(db.String(15), nullable=False, index=True)
    otp_hash     = db.Column(db.String(255), nullable=False)   # bcrypt hash of OTP
    expires_at   = db.Column(db.DateTime, nullable=False)
    attempts     = db.Column(db.Integer, default=0)           # wrong-OTP attempts
    send_count   = db.Column(db.Integer, default=1)           # OTPs sent this window
    window_start = db.Column(db.DateTime)                     # rate-limit window start
    is_used      = db.Column(db.Boolean, default=False)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id, "phone": self.phone,
            "expires_at": self.expires_at.isoformat(),
            "attempts": self.attempts
        }


# ─────────────────────────────────────────────────────────
# ACCOUNT LOCK TABLE
# ─────────────────────────────────────────────────────────
class AccountLock(db.Model):
    """Track failed login attempts per phone. Lock after threshold."""
    __tablename__ = "account_locks"

    id           = db.Column(db.Integer, primary_key=True)
    phone        = db.Column(db.String(15), nullable=False, unique=True, index=True)
    fail_count   = db.Column(db.Integer, default=0)
    locked_until = db.Column(db.DateTime)          # NULL = not locked
    last_attempt = db.Column(db.DateTime)
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow)


# ─────────────────────────────────────────────────────────
# ACTIVITY LOG  (audit trail)
# ─────────────────────────────────────────────────────────
class ActivityLog(db.Model):
    """Immutable audit trail for important user actions."""
    __tablename__ = "activity_logs"
    __table_args__ = (
        db.Index("ix_al_user_created", "user_id", "created_at"),
    )

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    action     = db.Column(db.String(60), nullable=False)
    # action examples: login_success, login_failed, otp_sent, password_changed,
    #                  payment_marked, tenant_added, tenant_removed, backup_downloaded
    detail     = db.Column(db.Text)          # JSON string with context
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "action": self.action,
            "detail": self.detail,
            "ip_address": self.ip_address,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ─────────────────────────────────────────────────────────
# COMPLAINT / ISSUE REPORT
# ─────────────────────────────────────────────────────────
class Complaint(db.Model):
    """Tenant-submitted complaints visible in admin panel."""
    __tablename__ = "complaints"

    id         = db.Column(db.Integer, primary_key=True)
    author_id  = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    category   = db.Column(db.String(80))   # Maintenance, Water, Electricity…
    message    = db.Column(db.Text, nullable=False)
    status     = db.Column(db.String(20), default="open")  # open | resolved | dismissed
    resolved_by= db.Column(db.Integer, db.ForeignKey("users.id"))
    resolved_at= db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    author     = db.relationship("User", foreign_keys=[author_id])

    def to_dict(self):
        return {
            "id": self.id,
            "author_id": self.author_id,
            "author_name": self.author.name if self.author else "",
            "author_phone": self.author.phone if self.author else "",
            "category": self.category,
            "message": self.message,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }


# ─────────────────────────────────────────────────────────
# DELTA SYNC — TOMBSTONE + SEQUENCE TRACKER
# ─────────────────────────────────────────────────────────

class MessageTombstone(db.Model):
    """
    Tracks deleted messages for delta sync.
    When a message is deleted, a tombstone is written here so clients
    that haven't synced yet can learn about the deletion on next pull.
    Tombstones older than 90 days can be pruned.
    """
    __tablename__ = "message_tombstones"
    id              = db.Column(db.Integer, primary_key=True)
    message_id      = db.Column(db.Integer, nullable=False, index=True)
    conversation_id = db.Column(db.String(64), nullable=False, index=True)
    deleted_seq     = db.Column(db.BigInteger, nullable=False, index=True)
    # seq at the time of deletion — clients use this to know if they need it
    deleted_at      = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "message_id":      self.message_id,
            "conversation_id": self.conversation_id,
            "deleted_seq":     self.deleted_seq,
            "deleted_at":      self.deleted_at.isoformat() if self.deleted_at else None,
        }


class ConversationSeq(db.Model):
    """
    Per-conversation monotonic sequence counter.
    Every message commit increments this atomically.
    Used as the delta-sync cursor — clients only need messages
    with server_seq > their stored last_seq.
    """
    __tablename__ = "conversation_seqs"
    conversation_id = db.Column(db.String(64), primary_key=True)
    current_seq     = db.Column(db.BigInteger, default=0, nullable=False)
    updated_at      = db.Column(db.DateTime, default=datetime.utcnow)

    @staticmethod
    def next_seq(conversation_id: str) -> int:
        """
        Atomically increment and return the next sequence number.
        Uses SELECT FOR UPDATE (PostgreSQL) or the SQLite equivalent
        to prevent two simultaneous messages getting the same seq.
        """
        from extensions import db as _db
        row = ConversationSeq.query.filter_by(
            conversation_id=conversation_id
        ).with_for_update().first()

        if row is None:
            row = ConversationSeq(conversation_id=conversation_id, current_seq=1)
            _db.session.add(row)
            return 1
        row.current_seq += 1
        row.updated_at   = datetime.utcnow()
        return row.current_seq
