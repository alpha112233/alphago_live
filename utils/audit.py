"""Per-container audit log — one row per significant action on this
customer's dedicated instance.

Goal: every operator AND customer action that touches money, credentials,
or container state lands in a SQLite table only this customer can read,
queryable from the dashboard and exportable as CSV. Compliance trail
without depending on shared infrastructure.

Storage: /app/db/audit.db — a dedicated SQLite file so it can be
exported / archived independently from the operational dbs.

Usage:
    from utils.audit import audit_log
    audit_log(
        actor="customer", action="broker.activate",
        resource="arihant",
        before={"active_broker": "upstox"},
        after={"active_broker": "arihant"},
        note="manual switch via /manage-brokers",
    )

Schema is created lazily on first call so importing this module is free
during container boot.

Failures to write the audit row are logged but NEVER raise — audit
logging must not break a place-order flow.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Per-container DB (host bind-mounted at /app/db inside the container).
_AUDIT_DB_PATH = os.getenv("AUDIT_DB_PATH", "/app/db/audit.db")

# Single connection, shared across the (single eventlet-worker) gunicorn
# process. SQLite handles concurrent reads + serialized writes; we'll
# wrap writes in a lock to avoid 'database is locked' under eventlet.
_conn: sqlite3.Connection | None = None
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,          -- ISO-8601 UTC
    actor       TEXT NOT NULL,          -- 'customer' | 'admin' | 'system' | 'broker'
    action      TEXT NOT NULL,          -- dotted-snake (e.g. 'order.place', 'broker.activate')
    resource    TEXT,                   -- target identifier (e.g. broker name, order id, dispatch id)
    before_json TEXT,                   -- JSON before state (may be null)
    after_json  TEXT,                   -- JSON after state (may be null)
    src_ip      TEXT,                   -- request originator IP if available
    status      TEXT,                   -- 'ok' | 'failed' | 'rejected' | None
    note        TEXT,                   -- free-text context (kept short)
    -- Hash-chain fields. row_hash = sha256(prev_hash + canonical_row_payload).
    -- Anyone tampering with a past row (or deleting one) breaks the chain
    -- at the next row, and every row after that, which `verify_chain()`
    -- detects. genesis row has prev_hash = '0'*64.
    prev_hash   TEXT,
    row_hash    TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log (action);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log (actor);
"""
# Idempotent ADD COLUMNs for instances that pre-date the hash-chain. Run
# after CREATE TABLE because CREATE TABLE only fires for fresh DBs.
_MIGRATIONS = [
    "ALTER TABLE audit_log ADD COLUMN prev_hash TEXT",
    "ALTER TABLE audit_log ADD COLUMN row_hash  TEXT",
]
_GENESIS_PREV_HASH = "0" * 64


def _get_conn() -> sqlite3.Connection | None:
    """Open the audit DB once, lazily. Returns None on persistent failure
    (we'll log + skip writes rather than crash the request)."""
    global _conn
    if _conn is not None:
        return _conn
    try:
        os.makedirs(os.path.dirname(_AUDIT_DB_PATH), exist_ok=True)
        conn = sqlite3.connect(
            _AUDIT_DB_PATH, check_same_thread=False, isolation_level=None
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        # Run forward migrations idempotently — silently ignore "duplicate
        # column" errors which mean the column is already there.
        for sql in _MIGRATIONS:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass
        _conn = conn
        return conn
    except Exception as e:
        logger.error(f"audit_log: failed to open {_AUDIT_DB_PATH}: {e}")
        return None


def audit_log(
    *,
    action: str,
    actor: str = "system",
    resource: str | None = None,
    before: Any = None,
    after: Any = None,
    src_ip: str | None = None,
    status: str | None = None,
    note: str | None = None,
) -> None:
    """Append a single audit row. Never raises.

    Each row's row_hash chains to the previous row's row_hash, making
    tampering detectable: any edit/delete of a past row breaks the chain
    at the NEXT row, which verify_chain() reports. The chain is read
    under the same lock as the insert to keep prev_hash linearizable
    under eventlet's single-worker model.
    """
    try:
        conn = _get_conn()
        if conn is None:
            return
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        before_json = _safe_json(before)
        after_json = _safe_json(after)
        note_clamped = (note or "")[:500]
        with _lock:
            # Read the latest row's hash to chain ours from.
            cur = conn.execute(
                "SELECT row_hash FROM audit_log "
                "ORDER BY id DESC LIMIT 1"
            )
            prev_row = cur.fetchone()
            prev_hash = (prev_row[0] if (prev_row and prev_row[0]) else _GENESIS_PREV_HASH)
            row_hash = _compute_row_hash(
                prev_hash, ts, actor, action, resource,
                before_json, after_json, src_ip, status, note_clamped,
            )
            conn.execute(
                "INSERT INTO audit_log (ts, actor, action, resource, before_json, "
                "after_json, src_ip, status, note, prev_hash, row_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, actor, action, resource, before_json, after_json,
                 src_ip, status, note_clamped, prev_hash, row_hash),
            )
    except Exception as e:
        logger.warning(f"audit_log write failed (non-fatal): {e}")


def query_recent(
    limit: int = 200,
    actor: str | None = None,
    action_prefix: str | None = None,
    since_iso: str | None = None,
) -> list[dict]:
    """Read the most recent rows, optionally filtered. Used by the
    dashboard page + CSV export."""
    conn = _get_conn()
    if conn is None:
        return []
    where = []
    params: list[Any] = []
    if actor:
        where.append("actor = ?")
        params.append(actor)
    if action_prefix:
        where.append("action LIKE ?")
        params.append(f"{action_prefix}%")
    if since_iso:
        where.append("ts >= ?")
        params.append(since_iso)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = (
        f"SELECT id, ts, actor, action, resource, before_json, after_json, "
        f"src_ip, status, note FROM audit_log {where_sql} "
        f"ORDER BY id DESC LIMIT ?"
    )
    params.append(int(min(limit, 5000)))
    try:
        rows = conn.execute(sql, params).fetchall()
    except Exception as e:
        logger.warning(f"audit_log read failed: {e}")
        return []
    cols = ["id", "ts", "actor", "action", "resource", "before_json",
            "after_json", "src_ip", "status", "note"]
    out: list[dict] = []
    for r in rows:
        d = dict(zip(cols, r))
        # Pre-parse the JSON columns so the frontend doesn't have to
        for k in ("before_json", "after_json"):
            if d.get(k):
                try:
                    d[k.replace("_json", "")] = json.loads(d[k])
                except Exception:
                    d[k.replace("_json", "")] = d[k]
                d.pop(k)
            else:
                d[k.replace("_json", "")] = None
                d.pop(k)
        out.append(d)
    return out


def prune(retention_days: int | None = None) -> int:
    """Delete rows older than retention_days (default 365 unless overridden
    by AUDIT_LOG_RETENTION_DAYS env). Returns deleted count."""
    if retention_days is None:
        try:
            retention_days = int(os.getenv("AUDIT_LOG_RETENTION_DAYS", "365"))
        except Exception:
            retention_days = 365
    if retention_days <= 0:
        return 0  # 0 means "never prune"
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    conn = _get_conn()
    if conn is None:
        return 0
    try:
        with _lock:
            c = conn.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff,))
        return c.rowcount or 0
    except Exception as e:
        logger.warning(f"audit_log prune failed: {e}")
        return 0


def _safe_json(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return json.dumps(value)
    try:
        return json.dumps(value, default=str)[:4000]
    except Exception:
        return json.dumps(str(value)[:4000])


# ---------------------------------------------------------------------------
# Hash-chain helpers
# ---------------------------------------------------------------------------

import hashlib  # noqa: E402 — kept near use site


def _compute_row_hash(
    prev_hash: str, ts: str, actor: str, action: str, resource: str | None,
    before_json: str | None, after_json: str | None, src_ip: str | None,
    status: str | None, note: str | None,
) -> str:
    """SHA-256 over a canonical concatenation. Order + separators are
    fixed so a verifier always reproduces the same hash. None → empty."""
    parts = [
        prev_hash, ts, actor, action,
        resource or "", before_json or "", after_json or "",
        src_ip or "", status or "", note or "",
    ]
    payload = "".join(parts)  # ASCII unit-separator, never appears in JSON / ISO timestamps
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def head_hash() -> dict:
    """Return the most recent row's hash + id. Customers can save this
    externally; if a future verify shows the same head hash, no row in
    between has been tampered with."""
    conn = _get_conn()
    if conn is None:
        return {"head_hash": None, "head_id": None, "count": 0}
    try:
        cur = conn.execute(
            "SELECT id, row_hash FROM audit_log ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        cur2 = conn.execute("SELECT COUNT(*) FROM audit_log")
        count = cur2.fetchone()[0]
        if row is None:
            return {"head_hash": None, "head_id": None, "count": 0}
        return {"head_hash": row[1], "head_id": row[0], "count": int(count)}
    except Exception as e:
        logger.warning(f"audit head_hash failed: {e}")
        return {"head_hash": None, "head_id": None, "count": 0}


def verify_chain(limit_rows: int | None = None) -> dict:
    """Walk the audit log from the genesis row forward, recomputing each
    row's hash. Returns:
      {
        "ok": bool,
        "total_rows": int,
        "verified_rows": int,
        "first_break_at_id": int | None,
        "first_break_reason": str | None,
        "head_hash": str | None,
        "legacy_unhashed_rows": int  # rows that pre-date the hash-chain
      }
    `limit_rows` limits how many rows are checked (most recent N). None =
    all rows.
    """
    conn = _get_conn()
    if conn is None:
        return {"ok": False, "total_rows": 0, "verified_rows": 0,
                "first_break_at_id": None, "first_break_reason": "audit DB unavailable",
                "head_hash": None, "legacy_unhashed_rows": 0}

    try:
        sql = (
            "SELECT id, ts, actor, action, resource, before_json, after_json, "
            "src_ip, status, note, prev_hash, row_hash FROM audit_log "
            "ORDER BY id ASC"
        )
        rows = conn.execute(sql).fetchall()
    except Exception as e:
        return {"ok": False, "total_rows": 0, "verified_rows": 0,
                "first_break_at_id": None, "first_break_reason": f"read failed: {e}",
                "head_hash": None, "legacy_unhashed_rows": 0}

    total = len(rows)
    legacy = sum(1 for r in rows if r[11] is None)  # row_hash IS NULL
    chained = [r for r in rows if r[11] is not None]
    if not chained:
        return {"ok": True, "total_rows": total, "verified_rows": 0,
                "first_break_at_id": None, "first_break_reason": None,
                "head_hash": None, "legacy_unhashed_rows": legacy}

    expected_prev = _GENESIS_PREV_HASH
    verified = 0
    first_break_id: int | None = None
    first_break_reason: str | None = None
    for r in chained:
        (rid, ts, actor, action, resource, before_json, after_json,
         src_ip, status, note, prev_hash, row_hash) = r
        if first_break_id is None and prev_hash != expected_prev:
            first_break_id = rid
            first_break_reason = (
                f"prev_hash mismatch at id={rid}: "
                f"chain expected {expected_prev[:12]}..., row claims {prev_hash[:12]}..."
            )
        recomputed = _compute_row_hash(
            prev_hash, ts, actor, action, resource,
            before_json, after_json, src_ip, status, note,
        )
        if recomputed != row_hash:
            if first_break_id is None:
                first_break_id = rid
                first_break_reason = (
                    f"row_hash mismatch at id={rid}: "
                    f"stored {row_hash[:12]}..., recomputed {recomputed[:12]}..."
                )
        else:
            if first_break_id is None:
                verified += 1
        expected_prev = row_hash
        if limit_rows is not None and verified >= limit_rows:
            break

    head_hash_value = chained[-1][11] if chained else None
    return {
        "ok": first_break_id is None,
        "total_rows": total,
        "verified_rows": verified,
        "first_break_at_id": first_break_id,
        "first_break_reason": first_break_reason,
        "head_hash": head_hash_value,
        "legacy_unhashed_rows": legacy,
    }
