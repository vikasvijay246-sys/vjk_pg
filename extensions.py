"""
extensions.py — Shared Flask extension singletons.

All extensions are initialised here and wired to the app in create_app()
so nothing can create circular-import problems.
"""
import logging
from flask_sqlalchemy import SQLAlchemy
from flask_migrate    import Migrate
from flask_jwt_extended import JWTManager
from flask_socketio  import SocketIO
from flask_bcrypt    import Bcrypt
from flask_cors      import CORS
from flask_limiter   import Limiter
from flask_limiter.util import get_remote_address

db       = SQLAlchemy()
migrate  = Migrate()          # ← Flask-Migrate (version-controlled schema)
jwt      = JWTManager()
socketio = SocketIO()
bcrypt   = Bcrypt()
cors     = CORS()
limiter  = Limiter(key_func=get_remote_address, default_limits=[])


# ── Structured application logger ─────────────────────────────────────────────
def configure_logging(debug: bool = False) -> logging.Logger:
    """
    Configure the root logger with a clean format.
    Returns the 'pg_manager' logger for use throughout the app.
    """
    level = logging.DEBUG if debug else logging.INFO
    fmt   = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    logging.basicConfig(level=level, format=fmt, datefmt=datefmt)

    # Quieten noisy third-party loggers in production
    if not debug:
        for noisy in ("werkzeug", "socketio", "engineio", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    logger = logging.getLogger("pg_manager")
    logger.setLevel(level)
    return logger


logger = logging.getLogger("pg_manager")
