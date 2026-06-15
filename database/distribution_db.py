# database/distribution_db.py
"""
Distribution Inbox — webhook receivers for external signal publishers.

Each inbox is a one-way trade-signal pipe: an admin / strategy publisher
POSTs `{symbol, action, quantity, ...}` to the inbox's webhook URL with
its API key, and the subscriber's container places the order on the
chosen broker.

Why a separate table instead of reusing OpenAlgo's `strategy.webhook_id`:
    The existing strategy webhook is overloaded — it enforces trading-hour
    windows, smart-order detection, symbol-mapping tables, intraday vs
    positional gating. None of that fits the "raw signal, place exactly
    this qty" semantics admin needs for F&O fan-out across subscribers.
    A dedicated inbox keeps the receive-side dumb: validate key, dedupe,
    place order, log.

Idempotency:
    Each signal carries a `signal_id` set by the publisher. We persist
    (inbox_id, signal_id) with a UNIQUE constraint, so a publisher
    retrying after a transient network failure doesn't double-order.
    The duplicate POST returns the result of the original signal.

Per-inbox broker pin:
    Each inbox can pin a specific broker (`broker_override`). If null,
    the inbox follows the user's active broker. Pin is per-inbox so
    a subscriber can route "Admin A's signals → Dhan" and "Admin B's
    signals → Upstox" without flipping their active broker every time.

Inbound IP allowlist:
    Optional comma-separated list. When set, the webhook rejects POSTs
    from any source IP not in the list. Defense in depth against a
    leaked API key.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import string
import time
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Column,
    DateTime,
    Float,
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


# ---- schema -----------------------------------------------------------------


class DistributionInbox(Base):
    __tablename__ = "distribution_inboxes"

    id = Column(Integer, primary_key=True)
    # NOTE: not a FK across module-local Base instances (same pattern as
    # broker_creds_db). user/inbox relationship is enforced in code.
    user_id = Column(Integer, nullable=False, index=True)
    # Display label the subscriber chose (e.g. "Mukul's Strategy Admin").
    name = Column(String(120), nullable=False)
    # Short URL-safe slug used in the public webhook URL.
    inbox_slug = Column(String(40), unique=True, nullable=False, index=True)
    # SHA-256(API_KEY_PEPPER + plaintext) — we never store the plaintext.
    api_key_hash = Column(String(128), nullable=False)
    # Display-only tail of the plaintext key, so the UI can show the
    # right inbox in a list ("...kE3a").
    api_key_last4 = Column(String(8), nullable=False)
    # Null = follow user's active broker. Set = always route to this broker.
    broker_override = Column(String(40), nullable=True)
    # Optional: comma-separated list of IPs / CIDRs allowed to POST here.
    allowed_ips = Column(String(500), nullable=True)
    # "active" or "disabled". Disabled inboxes return 403 on POST.
    status = Column(String(20), nullable=False, default="active")
    # Last observed activity — surfaced in the UI list.
    last_signal_at = Column(DateTime, nullable=True)
    last_signal_status = Column(String(40), nullable=True)
    last_signal_summary = Column(String(500), nullable=True)
    signal_count_total = Column(Integer, default=0, nullable=False)
    # When this inbox was auto-registered with the upstream publisher
    # (publisher.alphaquark.in), this is the publisher's subscriber row id.
    # Used by the customer's "Choose your Strategy Provider" picker — we
    # need to know which publisher-side row to ask reassign on. Nullable
    # because legacy inboxes were created without auto-registration; the
    # backfill ops script populates this for them lazily.
    publisher_subscriber_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class DistributionSignal(Base):
    __tablename__ = "distribution_signals"

    id = Column(Integer, primary_key=True)
    inbox_id = Column(Integer, nullable=False, index=True)
    # Publisher-provided dedupe key. Same key in two POSTs → second is a
    # no-op and returns the first's result.
    signal_id = Column(String(160), nullable=False)
    received_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    src_ip = Column(String(64), nullable=True)
    payload_json = Column(Text, nullable=False)
    # "placed" | "duplicate" | "failed" | "invalid" | "ip_blocked" | "disabled"
    # | "cancelled" (set by /cancel webhook)
    status = Column(String(40), nullable=False)
    broker_used = Column(String(40), nullable=True)
    broker_order_id = Column(String(120), nullable=True)
    error_message = Column(String(500), nullable=True)
    # Post-placement fill visibility. Updated by the orderbook poller
    # (services/fill_poller.py) every POLL_INTERVAL_SECONDS during market
    # hours. Normalised values: complete | open | cancelled | rejected |
    # trigger pending | partial. NULL = poller hasn't seen this signal yet.
    fill_status = Column(String(30), nullable=True)
    filled_quantity = Column(Integer, nullable=True)
    average_price = Column(Float, nullable=True)
    last_polled_at = Column(DateTime, nullable=True)
    # Phase 3.1 — pending bracket spec. JSON of {sl_trigger_price, sl_price?,
    # tp_price?} attached to a parent signal at receive time when the payload
    # carried a `bracket` block. The fill_poller places the SL + TP children
    # only AFTER the parent's fill_status flips to 'complete' — preventing
    # the V1 issue where a TP child could fill on its own before the parent.
    # Cleared after children are placed (or on cancel).
    pending_bracket_json = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("inbox_id", "signal_id", name="uix_inbox_signal"),
    )


def init_db():
    """Create distribution tables. Idempotent. Called from app.py at startup."""
    Base.metadata.create_all(bind=engine)
    _migrate_add_columns_if_missing()
    logger.info("distribution tables initialized")


def _migrate_add_columns_if_missing() -> None:
    """ADD COLUMN IF NOT EXISTS for columns appended after first deploy.
    create_all only creates NEW tables — pre-existing tables need explicit
    ALTERs. Idempotent; safe to run on every start."""
    from sqlalchemy import inspect as sqla_inspect, text
    insp = sqla_inspect(engine)
    if "distribution_signals" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("distribution_signals")}
    with engine.begin() as conn:
        if "fill_status" not in cols:
            conn.execute(text(
                "ALTER TABLE distribution_signals ADD COLUMN fill_status VARCHAR(30) NULL"
            ))
            logger.info("migration: added distribution_signals.fill_status")
        if "filled_quantity" not in cols:
            conn.execute(text(
                "ALTER TABLE distribution_signals ADD COLUMN filled_quantity INTEGER NULL"
            ))
            logger.info("migration: added distribution_signals.filled_quantity")
        if "average_price" not in cols:
            conn.execute(text(
                "ALTER TABLE distribution_signals ADD COLUMN average_price FLOAT NULL"
            ))
            logger.info("migration: added distribution_signals.average_price")
        if "last_polled_at" not in cols:
            conn.execute(text(
                "ALTER TABLE distribution_signals ADD COLUMN last_polled_at DATETIME NULL"
            ))
            logger.info("migration: added distribution_signals.last_polled_at")
        if "pending_bracket_json" not in cols:
            conn.execute(text(
                "ALTER TABLE distribution_signals ADD COLUMN pending_bracket_json TEXT NULL"
            ))
            logger.info("migration: added distribution_signals.pending_bracket_json")


# ---- key generation + hashing ----------------------------------------------


_ALPHABET = string.ascii_letters + string.digits


def _gen_slug(length: int = 16) -> str:
    """URL-safe slug for the inbox webhook. Looks like 'aB3xY9zQ4mN1pK2r'."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def _gen_api_key(length: int = 40) -> str:
    """Plaintext API key shown to the customer once on create / rotate."""
    return "ali_" + "".join(secrets.choice(_ALPHABET) for _ in range(length))


def _hash_api_key(plaintext: str) -> str:
    """SHA-256 with the existing API_KEY_PEPPER as salt. Constant-time
    comparable via `secrets.compare_digest(stored, computed)`."""
    pepper = (os.getenv("API_KEY_PEPPER") or "").encode("utf-8")
    return hashlib.sha256(pepper + plaintext.encode("utf-8")).hexdigest()


# ---- public helpers --------------------------------------------------------


def create_inbox(
    user_id: int,
    name: str,
    broker_override: str | None = None,
    allowed_ips: str | None = None,
) -> tuple[DistributionInbox, str]:
    """Create a new inbox. Returns (row, plaintext_api_key).

    The plaintext key is the ONLY time the caller sees it — we hash and
    discard it on this side. Surface it once to the customer, never store.
    """
    plaintext = _gen_api_key()
    # Retry on the (astronomically unlikely) slug collision.
    for _ in range(5):
        slug = _gen_slug()
        if db_session.query(DistributionInbox).filter_by(inbox_slug=slug).first() is None:
            break
    else:
        raise RuntimeError("could not allocate a unique inbox slug after 5 tries")

    row = DistributionInbox(
        user_id=user_id,
        name=(name or "Untitled").strip()[:120],
        inbox_slug=slug,
        api_key_hash=_hash_api_key(plaintext),
        api_key_last4=plaintext[-4:],
        broker_override=(broker_override or None),
        allowed_ips=(allowed_ips or None),
        status="active",
    )
    db_session.add(row)
    try:
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise

    logger.info(
        f"inbox created: user_id={user_id} slug={slug} broker_override={broker_override}"
    )
    return row, plaintext


def update_inbox(
    user_id: int,
    inbox_id: int,
    *,
    name: str | None = None,
    broker_override: Optional[str] = ...,  # ... = leave alone, None = clear
    allowed_ips: Optional[str] = ...,
    status: str | None = None,
) -> DistributionInbox | None:
    row = (
        db_session.query(DistributionInbox)
        .filter_by(id=inbox_id, user_id=user_id)
        .first()
    )
    if row is None:
        return None

    if name is not None and name.strip():
        row.name = name.strip()[:120]
    if broker_override is not ...:
        row.broker_override = (broker_override or None)
    if allowed_ips is not ...:
        row.allowed_ips = (allowed_ips or None)
    if status in ("active", "disabled"):
        row.status = status

    try:
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise
    return row


def rotate_api_key(user_id: int, inbox_id: int) -> tuple[DistributionInbox, str] | None:
    row = (
        db_session.query(DistributionInbox)
        .filter_by(id=inbox_id, user_id=user_id)
        .first()
    )
    if row is None:
        return None
    plaintext = _gen_api_key()
    row.api_key_hash = _hash_api_key(plaintext)
    row.api_key_last4 = plaintext[-4:]
    try:
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise
    logger.info(f"inbox API key rotated: user_id={user_id} inbox_id={inbox_id}")
    return row, plaintext


def delete_inbox(user_id: int, inbox_id: int) -> bool:
    row = (
        db_session.query(DistributionInbox)
        .filter_by(id=inbox_id, user_id=user_id)
        .first()
    )
    if row is None:
        return False
    # Cascade-clear signals too — small table, just delete by inbox_id.
    db_session.query(DistributionSignal).filter_by(inbox_id=inbox_id).delete()
    db_session.delete(row)
    try:
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise
    logger.info(f"inbox deleted: user_id={user_id} inbox_id={inbox_id}")
    return True


def list_inboxes(user_id: int) -> list[dict]:
    rows = (
        db_session.query(DistributionInbox)
        .filter_by(user_id=user_id)
        .order_by(DistributionInbox.created_at.desc())
        .all()
    )
    return [_to_public_dict(r) for r in rows]


def _to_public_dict(r: DistributionInbox) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "inbox_slug": r.inbox_slug,
        "api_key_last4": r.api_key_last4,
        "broker_override": r.broker_override,
        "allowed_ips": r.allowed_ips or "",
        "status": r.status,
        "last_signal_at": r.last_signal_at.isoformat() if r.last_signal_at else None,
        "last_signal_status": r.last_signal_status,
        "last_signal_summary": r.last_signal_summary,
        "signal_count_total": r.signal_count_total,
        "publisher_subscriber_id": getattr(r, "publisher_subscriber_id", None),
        "created_at": r.created_at.isoformat(),
        "updated_at": r.updated_at.isoformat(),
    }


def get_first_inbox_for_user(user_id: int) -> DistributionInbox | None:
    """Return the user's earliest-created inbox, or None."""
    return (
        db_session.query(DistributionInbox)
        .filter_by(user_id=user_id)
        .order_by(DistributionInbox.created_at.asc())
        .first()
    )


def set_publisher_subscriber_id(inbox_id: int, publisher_subscriber_id: int) -> bool:
    """Link this local inbox to its row on the upstream publisher. Idempotent.

    Returns True if the row was found and updated (or already had the same
    value), False if the inbox doesn't exist.
    """
    row = db_session.query(DistributionInbox).filter_by(id=inbox_id).first()
    if row is None:
        return False
    if row.publisher_subscriber_id == publisher_subscriber_id:
        return True
    row.publisher_subscriber_id = int(publisher_subscriber_id)
    try:
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise
    logger.info(
        f"inbox publisher link set: inbox_id={inbox_id} "
        f"publisher_subscriber_id={publisher_subscriber_id}"
    )
    return True


def get_inbox_plaintext_api_key_via_db(inbox_id: int) -> str | None:
    """We never store the plaintext key — by design. This function exists
    so the "pick admin" flow can fail loudly when called: the customer's
    own dashboard knows the plaintext key only at create/rotate time. The
    picker UI receives the plaintext key as a URL fragment / one-time
    session item; backend never persists it. Keeping this as an explicit
    "intentionally returns None" so future readers don't try to wire
    something here."""
    return None


def get_inbox_by_slug(slug: str) -> DistributionInbox | None:
    return (
        db_session.query(DistributionInbox)
        .filter_by(inbox_slug=slug)
        .first()
    )


def check_api_key(inbox: DistributionInbox, plaintext: str) -> bool:
    """Constant-time API-key comparison."""
    import secrets as _secrets
    if not plaintext:
        return False
    return _secrets.compare_digest(inbox.api_key_hash, _hash_api_key(plaintext))


# ---- HMAC + timestamp request signing -------------------------------------
#
# An opt-in alternative to the static Bearer api_key. The client signs each
# request with a per-inbox *signing secret* and a unix timestamp, so the
# secret itself NEVER travels on the wire and replays are bounded to a short
# window. The bearer api_key keeps working unchanged for existing clients.
#
# Canonical string the client signs (ASCII):  f"{timestamp}.{raw_request_body}"
#   signature = HMAC_SHA256(signing_secret, canonical)  -> lowercase hex
# Request headers:  X-Timestamp: <unix seconds>   X-Signature: <hex>  (a
# leading "sha256=" on X-Signature is accepted and stripped).
#
# The signing secret is DERIVED from the per-instance API_KEY_PEPPER + the
# inbox id + its api_key_hash, so: (a) the server can always recompute it
# without storing anything new (no schema change); (b) only a holder of the
# server-side pepper can forge it; (c) rotating the api_key rotates it too.

SIGNATURE_WINDOW_SECONDS = 30


def derive_signing_secret(inbox: DistributionInbox) -> str:
    """Per-inbox HMAC signing secret. Stable until the api_key is rotated."""
    pepper = (os.getenv("API_KEY_PEPPER") or "").encode("utf-8")
    msg = f"sigkey:v1:{inbox.id}:{inbox.api_key_hash}".encode("utf-8")
    return "alis_" + hmac.new(pepper, msg, hashlib.sha256).hexdigest()


def verify_signed_request(
    inbox: DistributionInbox,
    timestamp: str,
    signature: str,
    raw_body: bytes,
    window: int = SIGNATURE_WINDOW_SECONDS,
) -> tuple[bool, Optional[str]]:
    """Verify an HMAC+timestamp signed request. Returns (ok, error_message)."""
    if not timestamp or not signature:
        return False, "missing X-Timestamp or X-Signature"
    try:
        ts = int(float(timestamp))
    except (TypeError, ValueError):
        return False, "X-Timestamp must be a unix timestamp in seconds"
    skew = abs(int(time.time()) - ts)
    if skew > window:
        return False, (
            f"timestamp outside the +/-{window}s window "
            f"(skew {skew}s — check the clock, or this is a replay)"
        )
    secret = derive_signing_secret(inbox).encode("utf-8")
    canonical = str(timestamp).strip().encode("utf-8") + b"." + (raw_body or b"")
    expected = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
    provided = signature.strip()
    if provided.startswith("sha256="):
        provided = provided[len("sha256="):].strip()
    if not secrets.compare_digest(expected, provided):
        return False, "bad signature"
    return True, None


# ---- signal log ------------------------------------------------------------


def find_signal(inbox_id: int, signal_id: str) -> DistributionSignal | None:
    return (
        db_session.query(DistributionSignal)
        .filter_by(inbox_id=inbox_id, signal_id=signal_id)
        .first()
    )


def set_pending_bracket(inbox_id: int, signal_id: str, bracket: dict) -> bool:
    """Attach a bracket spec to a parent signal — the fill_poller picks this
    up and places SL+TP children once the parent flips to fill_status='complete'.
    Returns True if a row was updated."""
    row = (
        db_session.query(DistributionSignal)
        .filter_by(inbox_id=inbox_id, signal_id=signal_id)
        .first()
    )
    if row is None:
        return False
    row.pending_bracket_json = json.dumps(bracket)
    try:
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise
    return True


def clear_pending_bracket(inbox_id: int, signal_id: str) -> bool:
    """Clear the bracket spec on a parent signal. Called after children are
    placed, or when the parent is cancelled before fill (to prevent the
    poller from later placing children for a parent that's already gone)."""
    row = (
        db_session.query(DistributionSignal)
        .filter_by(inbox_id=inbox_id, signal_id=signal_id)
        .first()
    )
    if row is None or row.pending_bracket_json is None:
        return False
    row.pending_bracket_json = None
    try:
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise
    return True


def update_signal_status(
    inbox_id: int,
    signal_id: str,
    new_status: str,
    error_message: str | None = None,
) -> bool:
    """Update an existing signal row's status — used by modify / cancel
    flows to mark a previously-placed signal as `modified` / `cancelled`
    after a successful broker round-trip. Returns True if a row was
    updated, False if no signal existed for this (inbox, signal_id)."""
    row = (
        db_session.query(DistributionSignal)
        .filter_by(inbox_id=inbox_id, signal_id=signal_id)
        .first()
    )
    if row is None:
        return False
    row.status = new_status
    if error_message is not None:
        row.error_message = error_message[:500] or None
    try:
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise
    return True


def record_signal(
    inbox_id: int,
    signal_id: str,
    src_ip: str | None,
    payload: dict,
    status: str,
    broker_used: str | None = None,
    broker_order_id: str | None = None,
    error_message: str | None = None,
) -> DistributionSignal:
    row = DistributionSignal(
        inbox_id=inbox_id,
        signal_id=signal_id,
        src_ip=src_ip,
        payload_json=json.dumps(payload, default=str),
        status=status,
        broker_used=broker_used,
        broker_order_id=broker_order_id,
        error_message=(error_message or "")[:500] or None,
    )
    db_session.add(row)

    # Mirror the latest signal's headline state onto the inbox row so the
    # UI list view doesn't need to query the signal log for every inbox.
    inbox = db_session.query(DistributionInbox).filter_by(id=inbox_id).first()
    if inbox is not None:
        inbox.last_signal_at = datetime.utcnow()
        inbox.last_signal_status = status
        inbox.signal_count_total = (inbox.signal_count_total or 0) + 1
        summary_bits = [payload.get("action") or "", payload.get("symbol") or "",
                        f"qty={payload.get('quantity')}"]
        inbox.last_signal_summary = " ".join(str(b) for b in summary_bits if b)[:500]

    try:
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise
    return row


def list_signals(user_id: int, inbox_id: int, limit: int = 50) -> list[dict]:
    # Verify ownership before returning anything.
    inbox = (
        db_session.query(DistributionInbox)
        .filter_by(id=inbox_id, user_id=user_id)
        .first()
    )
    if inbox is None:
        return []
    rows = (
        db_session.query(DistributionSignal)
        .filter_by(inbox_id=inbox_id)
        .order_by(DistributionSignal.received_at.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )
    out = []
    for r in rows:
        try:
            payload = json.loads(r.payload_json)
        except Exception:
            payload = {"_raw": r.payload_json[:200]}
        out.append({
            "id": r.id,
            "signal_id": r.signal_id,
            "received_at": r.received_at.isoformat(),
            "src_ip": r.src_ip,
            "payload": payload,
            "status": r.status,
            "broker_used": r.broker_used,
            "broker_order_id": r.broker_order_id,
            "error_message": r.error_message,
        })
    return out
