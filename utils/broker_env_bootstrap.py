# utils/broker_env_bootstrap.py
"""
At-startup bootstrap of broker credentials from broker_creds_db → os.environ.

Reason this exists:
    Source of truth for broker credentials is broker_creds_db (Fernet-
    encrypted at rest, keyed off per-instance API_KEY_PEPPER). The rest of
    OpenAlgo's existing code (auth.py, brlogin.py, every broker module)
    still reads `os.getenv('BROKER_API_KEY')` etc. at request time.

    On container restart os.environ is reset to what the .env file holds
    (which is intentionally empty/placeholder — we don't store decrypted
    credentials on disk). This module re-populates os.environ from the DB
    so the user doesn't have to manually re-activate their broker every
    time the container restarts.

Looks up the admin user (single-user-per-instance design), finds whichever
broker is marked status='active' for them in broker_creds_db, decrypts the
creds, and writes them to os.environ. Idempotent — safe to call multiple
times.
"""

from __future__ import annotations

import os

from utils.logging import get_logger

logger = get_logger(__name__)


def bootstrap_active_broker() -> None:
    """Re-populate os.environ from the DB's active broker, if any.

    Failures are logged and swallowed — a startup without an active broker
    is the normal state for a fresh container, not an error.
    """
    try:
        # Lazy imports — at the very top of app.py the database modules
        # haven't been initialized yet.
        from database.user_db import User, db_session
        from database.broker_creds_db import get_active_broker_creds
    except Exception as exc:
        logger.debug(f"broker bootstrap: imports not ready yet: {exc}")
        return

    try:
        # OpenAlgo is single-admin-per-instance — find the one admin user.
        admin = db_session.query(User).filter_by(is_admin=True).first()
        if admin is None:
            logger.info("broker bootstrap: no admin user yet, skipping")
            return

        creds = get_active_broker_creds(admin.id)
        if creds is None:
            logger.info("broker bootstrap: no active broker for admin, skipping")
            return

        broker = creds.get("broker") or ""
        if not broker:
            return

        host_server = os.getenv("HOST_SERVER", "").rstrip("/")
        redirect_url = f"{host_server}/{broker}/callback" if host_server else ""

        api_key = creds.get("api_key", "") or ""
        # Broker-specific packing of BROKER_API_KEY.
        #
        # Dhan's /dhan/initiate-oauth handler expects BROKER_API_KEY in the
        # form `client_id:::api_key` (it splits on `:::` to pull the client
        # ID). The dashboard form collects client_code and api_key as
        # separate fields, so the bootstrap has to do the join here. Other
        # brokers that use the same `<id>:::<key>` convention — Flattrade —
        # already store the joined string in the api_key field itself
        # (the customer pastes it pre-joined), so they don't need this
        # branch.
        client_code = creds.get("client_code", "") or ""
        if broker == "dhan" and client_code and ":::" not in api_key:
            api_key = f"{client_code}:::{api_key}"

        os.environ["BROKER_API_KEY"] = api_key
        os.environ["BROKER_API_SECRET"] = creds.get("api_secret", "") or ""
        os.environ["BROKER_API_KEY_MARKET"] = creds.get("api_key_market", "") or ""
        os.environ["BROKER_API_SECRET_MARKET"] = creds.get("api_secret_market", "") or ""
        if client_code:
            os.environ["BROKER_CLIENT_ID"] = client_code
        if redirect_url:
            os.environ["REDIRECT_URL"] = redirect_url

        # Per-customer XTS base URL (IIFL XTS issues different hosts per dealer).
        # Stored in broker_creds extra.base_url; read by broker/iiflxts/baseurl.py.
        extra = creds.get("extra") or {}
        xts_base = (extra.get("base_url") or extra.get("xts_base_url") or "").strip()
        if xts_base:
            os.environ["BROKER_XTS_BASE_URL"] = xts_base
            # A custom XTS host is almost certainly IPv4-only (like ttblaze) and
            # NOT in DEFAULT_V4_HOSTS — register it in EGRESS_V4_HOSTS so the
            # httpx client routes it via the customer's dedicated v4 proxy
            # (otherwise it egresses from the v6 default and IIFL rejects it).
            try:
                from urllib.parse import urlparse
                host = urlparse(xts_base if "://" in xts_base else f"https://{xts_base}").hostname
                if host:
                    hosts = {h.strip().lower() for h in (os.environ.get("EGRESS_V4_HOSTS") or "").split(",") if h.strip()}
                    hosts.add(host.lower())
                    os.environ["EGRESS_V4_HOSTS"] = ",".join(sorted(hosts))
            except Exception:
                pass

        logger.info(f"broker bootstrap: activated '{broker}' from broker_creds_db at startup")
    except Exception as exc:
        logger.exception(f"broker bootstrap: failed (continuing anyway): {exc}")
