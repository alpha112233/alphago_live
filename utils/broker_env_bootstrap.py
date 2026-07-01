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


def apply_xts_env(extra: dict) -> None:
    """Stamp the per-customer IIFL XTS base URLs from a broker_creds `extra`
    dict into os.environ (read by broker/iiflxts/baseurl.py), and register the
    hosts in EGRESS_V4_HOSTS so they egress via the customer's dedicated v4
    (IIFL XTS hosts are IPv4-only). Called from BOTH the startup bootstrap AND
    the save/activate env-refresh, so a base URL entered in the UI takes effect
    on the next Connect with no container restart. Interactive and Market Data
    hosts may differ (base_url vs base_url_market)."""
    extra = extra or {}
    xts_base = (extra.get("base_url") or extra.get("xts_base_url") or "").strip()
    xts_mkt = (extra.get("base_url_market") or extra.get("market_login_url") or "").strip()
    v4_hosts = {h.strip().lower() for h in (os.environ.get("EGRESS_V4_HOSTS") or "").split(",") if h.strip()}

    def _register_v4(url: str):
        try:
            from urllib.parse import urlparse
            host = urlparse(url if "://" in url else f"https://{url}").hostname
            if host:
                v4_hosts.add(host.lower())
        except Exception:
            pass

    if xts_base:
        os.environ["BROKER_XTS_BASE_URL"] = xts_base
        _register_v4(xts_base)
    if xts_mkt:
        os.environ["BROKER_XTS_MARKET_URL"] = xts_mkt
        _register_v4(xts_mkt)
    if v4_hosts:
        os.environ["EGRESS_V4_HOSTS"] = ",".join(sorted(v4_hosts))


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

        # Per-customer XTS base URLs (IIFL XTS) — same helper the save/activate
        # env-refresh uses, so a base URL entered in the UI takes effect on the
        # very next Connect without a container restart.
        apply_xts_env(creds.get("extra") or {})

        logger.info(f"broker bootstrap: activated '{broker}' from broker_creds_db at startup")
    except Exception as exc:
        logger.exception(f"broker bootstrap: failed (continuing anyway): {exc}")
