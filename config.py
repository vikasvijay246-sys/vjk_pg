"""
config.py — Environment-based configuration.

Usage:
    FLASK_ENV=production flask run
    FLASK_ENV=development flask run   (default)
"""
import os
from datetime import timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class Config:
    # ── Security ──────────────────────────────────────────────────────────────
    SECRET_KEY     = os.environ.get("SECRET_KEY",     "pg-manager-dev-secret-CHANGE-IN-PROD")
    JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "pg-jwt-secret-CHANGE-IN-PROD")
    JWT_ACCESS_TOKEN_EXPIRES  = timedelta(days=7)
    JWT_REFRESH_TOKEN_EXPIRES = timedelta(days=30)

    # ── Files ─────────────────────────────────────────────────────────────────
    # Render: set RENDER_DISK_PATH env var to a persistent disk mount (e.g. /var/data)
    # Without it, uploads go to /tmp (ephemeral but safe — app won't crash)
    _disk = os.environ.get("RENDER_DISK_PATH", "")
    UPLOAD_FOLDER = (
        os.path.join(_disk, "uploads")        if _disk else
        os.path.join("/tmp", "pg_uploads")    if os.environ.get("RENDER") else
        os.path.join(BASE_DIR, "static", "uploads")
    )
    MAX_CONTENT_LENGTH  = 20 * 1024 * 1024   # 20 MB hard limit
    ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
    ALLOWED_FILE_EXTENSIONS  = {
        "pdf", "png", "jpg", "jpeg", "webp", "gif",
        "doc", "docx", "mp4", "mov", "webm", "3gp",
    }

    # ── Database ──────────────────────────────────────────────────────────────
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,   # Detect dead connections before use
        "pool_recycle":  300,    # Recycle connections every 5 min
        "pool_timeout":  20,
        "max_overflow":  10,
    }

    # ── SMS provider (OTP) ────────────────────────────────────────────────────
    # Set SMS_PROVIDER=twilio and TWILIO_SID/TWILIO_AUTH/TWILIO_FROM in env
    SMS_PROVIDER = os.environ.get("SMS_PROVIDER", "console")

    # ── AWS S3 (optional — chat media) ─────────────────────────────────────
    # Leave blank to use local storage fallback
    S3_BUCKET              = os.environ.get("S3_BUCKET", "")
    AWS_ACCESS_KEY_ID      = os.environ.get("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY  = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    AWS_REGION             = os.environ.get("AWS_REGION", "ap-south-1")
    # CloudFront CDN URL prefix (optional, for faster media delivery)
    CDN_BASE_URL           = os.environ.get("CDN_BASE_URL", "")

    # ── Firebase / FCM (optional — push notifications) ─────────────────────
    # Path to your Firebase service-account JSON file
    FIREBASE_CREDENTIALS_PATH = os.environ.get("FIREBASE_CREDENTIALS_PATH", "")


class DevelopmentConfig(Config):
    DEBUG = True
    TESTING = False
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        f"sqlite:///{os.path.join(BASE_DIR, 'pg_manager.db')}"
    )
    # Verbose SQLAlchemy queries in development
    SQLALCHEMY_ECHO = False


class ProductionConfig(Config):
    DEBUG   = False
    TESTING = False

    _db_url = os.environ.get(
        "DATABASE_URL",
        f"sqlite:///{os.path.join(BASE_DIR, 'pg_manager.db')}"
    )
    # Render provides postgres:// — SQLAlchemy requires postgresql://
    if _db_url and _db_url.startswith("postgres://"):
        _db_url = _db_url.replace("postgres://", "postgresql://", 1)
    SQLALCHEMY_DATABASE_URI = _db_url

    SQLALCHEMY_ECHO = False

    # Stricter secrets in production
    @classmethod
    def validate(cls):
        import logging as _log
        if cls.SECRET_KEY == "pg-manager-dev-secret-CHANGE-IN-PROD":
            _log.getLogger("pg_manager").warning(
                "⚠️  SECRET_KEY is the default dev value — set it in Render env vars!")
        if cls.JWT_SECRET_KEY == "pg-jwt-secret-CHANGE-IN-PROD":
            _log.getLogger("pg_manager").warning(
                "⚠️  JWT_SECRET_KEY is the default dev value — set it in Render env vars!")


class TestingConfig(Config):
    TESTING = True
    DEBUG   = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(minutes=5)
    WTF_CSRF_ENABLED = False


_CONFIG_MAP = {
    "development": DevelopmentConfig,
    "production":  ProductionConfig,
    "testing":     TestingConfig,
}


def get_config():
    env = os.environ.get("FLASK_ENV", "development").lower()
    cfg = _CONFIG_MAP.get(env, DevelopmentConfig)
    if env == "production" and hasattr(cfg, "validate"):
        cfg.validate()
    return cfg
