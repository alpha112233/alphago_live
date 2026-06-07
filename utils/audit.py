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
    note        TEXT                    -- free-text context (kept short)
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log (action);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log (actor);
"""


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
    """Append a single audit row. Never raises."""
    try:
        conn = _get_conn()
        if conn is None:
            return
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        before_json = _safe_json(before)
        after_json = _safe_json(after)
        with _lock:
            conn.execute(
                "INSERT INTO audit_log (ts, actor, action, resource, before_json, "
                "after_json, src_ip, status, note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, actor, action, resource, before_json, after_json,
                 src_ip, status, (note or "")[:500]),
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
