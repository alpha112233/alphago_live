#!/usr/bin/env python3
"""Add publisher_subscriber_id column to distribution_inboxes.

Idempotent — safe to run multiple times. Runs automatically at boot via
upgrade/run_all_migrations.py.

Why: with the auto-provisioning flow (POST /api/distribution/system/create-inbox),
each new inbox is auto-registered with publisher.alphaquark.in and the
publisher's subscriber row id is stored back here. The customer's
"Choose your Strategy Provider" picker uses this id to call publisher's
reassign-by-customer endpoint.
"""
from __future__ import annotations

import os
import sys

# Add parent directory to path for sibling imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text

env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(env_path)

from utils.logging import get_logger

logger = get_logger(__name__)


def migrate():
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///db/openalgo.db")
    if DATABASE_URL.startswith("sqlite:///") and not DATABASE_URL.startswith("sqlite:////"):
        db_path = DATABASE_URL.replace("sqlite:///", "")
        parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        full_db_path = os.path.join(parent_dir, db_path)
        DATABASE_URL = f"sqlite:///{full_db_path}"
        logger.info(f"Using database: {full_db_path}")

    engine = create_engine(DATABASE_URL)
    inspector = inspect(engine)

    if "distribution_inboxes" not in inspector.get_table_names():
        logger.info("distribution_inboxes table doesn't exist yet — will be created on first run")
        return True

    existing_cols = [c["name"] for c in inspector.get_columns("distribution_inboxes")]
    if "publisher_subscriber_id" in existing_cols:
        logger.info("Column publisher_subscriber_id already exists; nothing to do")
        return True

    logger.info("Adding distribution_inboxes.publisher_subscriber_id column")
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE distribution_inboxes "
            "ADD COLUMN publisher_subscriber_id INTEGER NULL"
        ))
    logger.info("Migration complete")
    return True


if __name__ == "__main__":
    ok = migrate()
    sys.exit(0 if ok else 1)
