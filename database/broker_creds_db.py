# database/broker_creds_db.py
"""
Per-user, per-broker credential storage — supports multiple brokers per user.

This is the alphago_live fork's addition over upstream OpenAlgo. Upstream
keeps a single broker's credentials in `.env` (see blueprints/broker_credentials.py).
That model breaks for the Alpha Live Trading hosting product, where one user
needs to save credentials for multiple brokers and switch between them.

Encryption-at-rest reuses auth_db's Fernet helpers (PBKDF2 over API_KEY_PEPPER).
This keeps the key-management story identical to how TOTP secrets and session
auth_tokens are already protected.
"""

import json
import os
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.ext.declarative import declarative_base

from utils.logging import get_logger

logger = get_logger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")

engine = create_engine(
    DATABASE_URL, echo=False, pool_size=10, max_overflow=20, pool_timeout=10
)
db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()
Base.query = db_session.query_property()


class BrokerCreds(Base):
    __tablename__ = "broker_creds"

    id = Column(Integer, primary_key=True)
    # NOTE: not a SQLAlchemy ForeignKey — cross-module declarative_base() can't
    # see user_db's `users` table. The user/broker_creds relationship is
    # enforced by application code (broker_credentials.py routes always look
    # up the current user via session before reading/writing this table).
    # SQLite also ignores FKs by default unless PRAGMA foreign_keys=ON.
    user_id = Column(Integer, nullable=False, index=True)
    broker = Column(String(50), nullable=False)

    # All `*_enc` columns hold Fernet ciphertext (auth_db.encrypt_token).
    # Length 512 fits a Fernet-wrapped 256-byte secret with overhead headroom.
    api_key_enc = Column(String(512), nullable=False)
    api_secret_enc = Column(String(512), nullable=True)
    api_key_market_enc = Column(String(512), nullable=True)
    api_secret_market_enc = Column(String(512), nullable=True)

    # Broker-specific identifiers — not all brokers need these.
    client_code = Column(String(120), nullable=True)
    totp_seed_enc = Column(String(512), nullable=True)

    # Broker-specific extras (MPIN, server_id, baseUrl, etc.) as JSON blob.
    # Each key inside is either plaintext (non-sensitive) or '<enc>:<ciphertext>'.
    extra_json = Column(Text, nullable=True)

    # status: 'saved' (creds stored, not the active broker for this user)
    #         'active' (THE one broker this user is currently using — at most one per user)
    #         'expired' (token rotation needed; credentials retained)
    #         'error' (last login attempt failed)
    status = Column(String(20), default="saved", nullable=False)

    last_activated_at = Column(DateTime, nullable=True)
    last_auth_at = Column(DateTime, nullable=True)
    last_error = Column(String(500), nullable=True)
    notes = Column(String(200), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "broker", name="uix_broker_creds_user_broker"),
    )


def init_db():
    """Create the broker_creds table. Called from app.py at startup, idempotent."""
    Base.metadata.create_all(bind=engine)
    logger.info("broker_creds table initialized")


# ---- encryption helpers (delegate to auth_db) -------------------------------

def _encrypt(plaintext: str | None) -> str | None:
    """Encrypt a string with Fernet. Returns None for None/empty inputs."""
    if not plaintext:
        return None
    from database.auth_db import encrypt_token
    return encrypt_token(plaintext)


def _decrypt(ciphertext: str | None) -> str | None:
    """Decrypt a Fernet ciphertext. Returns None for None/empty inputs.

    Uses safe_decrypt_token so legacy plaintext rows (if any) are handled
    gracefully — matches the User.get_totp_secret() pattern.
    """
    if not ciphertext:
        return None
    from database.auth_db import safe_decrypt_token
    return safe_decrypt_token(ciphertext) or ciphertext


# ---- CRUD --------------------------------------------------------------------

def add_or_update_broker_creds(
    user_id: int,
    broker: str,
    api_key: str,
    api_secret: str | None = None,
    api_key_market: str | None = None,
    api_secret_market: str | None = None,
    client_code: str | None = None,
    totp_seed: str | None = None,
    extra: dict | None = None,
    notes: str | None = None,
) -> int:
    """Upsert credentials for (user_id, broker). Returns the row's id.

    Status defaults to 'saved' on first insert; on update, status is preserved
    so the caller doesn't accidentally demote an 'active' broker by re-saving.
    Use activate_broker() to change status.
    """
    if not broker or not api_key:
        raise ValueError("broker and api_key are required")

    row = db_session.query(BrokerCreds).filter_by(user_id=user_id, broker=broker).first()
    extra_json = json.dumps(extra) if extra else None

    if row is None:
        row = BrokerCreds(
            user_id=user_id,
            broker=broker,
            api_key_enc=_encrypt(api_key),
            api_secret_enc=_encrypt(api_secret),
            api_key_market_enc=_encrypt(api_key_market),
            api_secret_market_enc=_encrypt(api_secret_market),
            client_code=client_code,
            totp_seed_enc=_encrypt(totp_seed),
            extra_json=extra_json,
            notes=notes,
            status="saved",
        )
        db_session.add(row)
    else:
        # Don't overwrite a field with None — caller passing None means "leave alone".
        if api_key:
            row.api_key_enc = _encrypt(api_key)
        if api_secret is not None:
            row.api_secret_enc = _encrypt(api_secret) if api_secret else None
        if api_key_market is not None:
            row.api_key_market_enc = _encrypt(api_key_market) if api_key_market else None
        if api_secret_market is not None:
            row.api_secret_market_enc = _encrypt(api_secret_market) if api_secret_market else None
        if client_code is not None:
            row.client_code = client_code or None
        if totp_seed is not None:
            row.totp_seed_enc = _encrypt(totp_seed) if totp_seed else None
        if extra is not None:
            row.extra_json = extra_json
        if notes is not None:
            row.notes = notes

    try:
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise

    logger.info(f"broker_creds upserted: user_id={user_id} broker={broker} id={row.id}")
    return row.id


def list_user_brokers(user_id: int) -> list[dict]:
    """Return public metadata for all brokers this user has saved.

    Never returns decrypted secrets — only flags ('has_api_key' etc.) and
    timestamps. Safe to expose to the React frontend's broker list view.
    """
    rows = db_session.query(BrokerCreds).filter_by(user_id=user_id).order_by(BrokerCreds.created_at).all()
    return [
        {
            "broker": r.broker,
            "status": r.status,
            "has_api_key": bool(r.api_key_enc),
            "has_api_secret": bool(r.api_secret_enc),
            "has_totp_seed": bool(r.totp_seed_enc),
            "client_code": r.client_code or "",
            "last_activated_at": r.last_activated_at.isoformat() if r.last_activated_at else None,
            "last_auth_at": r.last_auth_at.isoformat() if r.last_auth_at else None,
            "last_error": r.last_error,
            "notes": r.notes or "",
            "created_at": r.created_at.isoformat(),
            "updated_at": r.updated_at.isoformat(),
        }
        for r in rows
    ]


def get_broker_creds(user_id: int, broker: str) -> dict | None:
    """Return decrypted credentials for one (user_id, broker). None if not saved.

    Callers MUST treat the returned secrets as sensitive — never log them,
    never write them anywhere persistent that isn't itself encrypted.
    """
    row = db_session.query(BrokerCreds).filter_by(user_id=user_id, broker=broker).first()
    if row is None:
        return None
    return {
        "broker": row.broker,
        "api_key": _decrypt(row.api_key_enc) or "",
        "api_secret": _decrypt(row.api_secret_enc) or "",
        "api_key_market": _decrypt(row.api_key_market_enc) or "",
        "api_secret_market": _decrypt(row.api_secret_market_enc) or "",
        "client_code": row.client_code or "",
        "totp_seed": _decrypt(row.totp_seed_enc) or "",
        "extra": json.loads(row.extra_json) if row.extra_json else {},
        "status": row.status,
    }


def delete_broker_creds(user_id: int, broker: str) -> bool:
    """Remove a saved broker. Returns True if a row was deleted."""
    row = db_session.query(BrokerCreds).filter_by(user_id=user_id, broker=broker).first()
    if row is None:
        return False
    db_session.delete(row)
    try:
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise
    logger.info(f"broker_creds deleted: user_id={user_id} broker={broker}")
    return True


def activate_broker(user_id: int, broker: str) -> bool:
    """Mark `broker` as active for `user_id`; any previously-active broker
    becomes 'saved'. Returns True on success, False if the broker isn't saved
    for this user.

    Note: this only updates DB state. The caller is responsible for any
    side-effects needed by OpenAlgo's runtime (e.g., clearing cached
    BROKER_API_KEY at module level — see blueprints/brlogin.py refactor).
    """
    target = db_session.query(BrokerCreds).filter_by(user_id=user_id, broker=broker).first()
    if target is None:
        return False

    # Demote any current active for this user.
    db_session.query(BrokerCreds).filter_by(user_id=user_id, status="active").update({"status": "saved"})

    target.status = "active"
    target.last_activated_at = datetime.utcnow()
    try:
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise
    logger.info(f"broker activated: user_id={user_id} broker={broker}")
    return True


def get_active_broker(user_id: int) -> str | None:
    """Return the name of the broker currently marked active for this user,
    or None if no broker is active. Single-broker fallback for legacy auth
    paths is handled by the caller, not here.
    """
    row = db_session.query(BrokerCreds.broker).filter_by(user_id=user_id, status="active").first()
    return row.broker if row else None


def get_active_broker_creds(user_id: int) -> dict | None:
    """Convenience: decrypted creds for the user's active broker, or None."""
    broker = get_active_broker(user_id)
    return get_broker_creds(user_id, broker) if broker else None


def mark_auth_success(user_id: int, broker: str) -> None:
    """Record a successful broker authentication (called by brlogin callbacks)."""
    row = db_session.query(BrokerCreds).filter_by(user_id=user_id, broker=broker).first()
    if row is None:
        return
    row.last_auth_at = datetime.utcnow()
    row.last_error = None
    if row.status != "active":
        # First successful auth implicitly activates this broker.
        row.status = "active"
    try:
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise


def mark_auth_error(user_id: int, broker: str, error: str) -> None:
    """Record a failed broker authentication. Truncates `error` to fit column."""
    row = db_session.query(BrokerCreds).filter_by(user_id=user_id, broker=broker).first()
    if row is None:
        return
    row.last_error = (error or "")[:500]
    row.status = "error"
    try:
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise
