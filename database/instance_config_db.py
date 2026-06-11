# database/instance_config_db.py
"""Per-instance OPERATIONAL config — DB-backed with env fallback.

Why this exists (2026-06-11): values that can change while the container is
running (today: the EGRESS_V4_* dedicated-IPv4 proxy config) used to live
only in /app/.env, so every allocation/heal forced a container recreate —
and the env file kept drifting from the source of truth (hostingsol's IP
pool) across re-provisions and pool IP replacements.

Tiering rule of thumb:
  - bootstrap secrets / process wiring (API_KEY_PEPPER, DATABASE_URL,
    PORT...)  → env, restart is inherent. NOT this module.
  - instance identity (HOST_SERVER, SSO_*) → env, changes only with a
    re-provision. NOT this module.
  - operational config that changes intraday → HERE.

Read path: DB row wins, else os.getenv(key), else default — so old images,
fresh provisions (env stamped by the provisioner) and FOSS self-hosters all
keep working with zero migration.

Propagation: values are cached ~30s per process (gunicorn workers each have
their own cache). Writers bump _config_version; utils/httpx_client checks
the version and rebuilds its proxy mounts — no restart, convergence ≤30s.
"""

from __future__ import annotations

import os
import time

from cachetools import TTLCache
from sqlalchemy import Column, DateTime, String, Text, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

from utils.logging import get_logger

logger = get_logger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL and "sqlite" in DATABASE_URL:
    engine = create_engine(
        DATABASE_URL, poolclass=NullPool, connect_args={"check_same_thread": False}
    )
else:
    engine = create_engine(DATABASE_URL, pool_size=50, max_overflow=100, pool_timeout=10)

db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()
Base.query = db_session.query_property()

# Short TTL: this is the worker-convergence window after a config write.
_cache = TTLCache(maxsize=64, ttl=30)

CONFIG_VERSION_KEY = "_config_version"


class InstanceConfig(Base):
    __tablename__ = "instance_config"
    key = Column(String(120), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, nullable=True)


def init_db():
    from database.db_init_helper import init_db_with_logging

    init_db_with_logging(Base, engine, "Instance Config DB", logger)


def get_config(key: str, default: str = "") -> str:
    """DB value if set, else env, else default. Never raises (a missing /
    not-yet-created table falls through to env — callers may run before
    init_db on first boot)."""
    if key in _cache:
        return _cache[key]
    value = None
    try:
        row = db_session.query(InstanceConfig).filter_by(key=key).first()
        if row is not None and row.value is not None and row.value != "":
            value = row.value
    except Exception:
        # Table missing / DB briefly unavailable — env fallback below.
        db_session.rollback()
    if value is None:
        value = (os.getenv(key) or "").strip() or default
    _cache[key] = value
    return value


def set_configs(values: dict[str, str]) -> None:
    """Upsert several keys atomically, bump the config version, and drop
    this process's cache so the writer sees its own write immediately."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        for key, value in values.items():
            row = db_session.query(InstanceConfig).filter_by(key=key).first()
            if row is None:
                db_session.add(InstanceConfig(key=key, value=str(value), updated_at=now))
            else:
                row.value = str(value)
                row.updated_at = now
        version_row = db_session.query(InstanceConfig).filter_by(key=CONFIG_VERSION_KEY).first()
        version = str(int(time.time() * 1000))
        if version_row is None:
            db_session.add(InstanceConfig(key=CONFIG_VERSION_KEY, value=version, updated_at=now))
        else:
            version_row.value = version
            version_row.updated_at = now
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise
    _cache.clear()
    logger.info(f"instance_config updated: {sorted(values.keys())} (version {version})")


def get_config_version() -> str:
    """Cheap (30s-cached) change marker for consumers that hold derived
    state, e.g. the shared httpx client's proxy mounts."""
    return get_config(CONFIG_VERSION_KEY, default="0")
