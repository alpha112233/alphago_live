# broker_login_adapters/arihant.py
"""Daemon auto-login for Arihant Capital (TradeBridge) — hands-free TOTP path.

Why this exists, given Arihant already has a refresh-token chain:
    broker/arihant/api/auth_api.authenticate_broker uses a stored
    REFRESH TOKEN (set once via the /broker/arihant/login OTP page) to
    mint a fresh access_token every day. That refresh-token path is the
    primary daily mechanism — it does NOT need this adapter.

    BUT — Arihant rotates the refresh_token itself every ~6 months. When
    that happens, the refresh-access-token call returns
    AU004/EGN006-shaped errors and authenticate_broker fails with
    "refresh failed: …". Without this adapter, the customer has to
    manually walk through the OTP page at /broker/arihant/login again.

    This adapter closes that gap: if the customer has saved their
    Arihant User ID + Trading Password + TOTP Seed, the daily auto-login
    falls back to a full login + verify-otp(TOTP) handshake and persists
    the fresh refresh_token. Hands-free 6-monthly renewal.

Inputs (creds dict, all required for hands-free path):
    api_key      — Arihant API Key (the short string from TradeBridge
                   App → API Key column, NOT the App Id UUID)
    user_id      — Arihant Client Code (e.g. "284300014") — stored as
                   broker_creds.client_code; exposed via BROKER_CLIENT_ID
    password     — Trading password — stored Fernet-encrypted in
                   broker_creds.api_key_market_enc; exposed via
                   BROKER_API_KEY_MARKET on the active worker
    totp_secret  — Base32 TOTP seed — stored Fernet-encrypted in
                   broker_creds.totp_seed_enc; the daemon passes it in
                   here via the totp_seed/totp_secret key.

Output (dict):
    {ok: bool, access_token: str|None, refresh_token: str|None,
     user_id: str|None, expires_at: str|None, error: str|None}

Validation: the canonical OTP page at brlogin.py exercises
auth_api.login_initiate + verify_otp directly, so those endpoints are
already real. This adapter simply substitutes pyotp.TOTP(seed).now()
for the human-entered OTP digit.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any

logger = logging.getLogger(__name__)


def _fail(error: str, **extra: Any) -> dict:
    return {"ok": False, "access_token": None, "refresh_token": None,
            "user_id": None, "expires_at": None, "error": error, **extra}


def login(creds: dict) -> dict:
    """Run the full Arihant login → TOTP → verify-otp handshake.

    Designed to be called by:
      (a) services/auto_login_scheduler_service at 08:00 IST daily
      (b) broker/arihant/api/auth_api.authenticate_broker when
          refresh-access-token fails with refresh-token-invalid

    Either entry-point gets the same hands-free re-mint.
    """
    api_key = (creds.get("api_key") or os.getenv("BROKER_API_KEY") or "").strip()
    user_id = (creds.get("user_id") or creds.get("client_code")
               or os.getenv("BROKER_CLIENT_ID") or "").strip()
    password = (creds.get("password") or os.getenv("BROKER_API_KEY_MARKET") or "").strip()
    totp_secret = (creds.get("totp_secret") or creds.get("totp_seed") or "").strip()

    if not api_key:
        return _fail("Arihant: api_key not set — paste your TradeBridge API Key in Manage Brokers first.")
    if not user_id:
        return _fail("Arihant: User ID (client_code) not set — fill in the optional 'Arihant User ID' field.")
    if not password:
        return _fail("Arihant: trading password not set — fill in the optional 'Arihant Trading Password' field.")
    if not totp_secret:
        return _fail("Arihant: TOTP seed not set — fill in the optional 'Arihant TOTP Seed' field and enable TOTP at TradeBridge.")

    try:
        import pyotp
    except ImportError:
        return _fail("Arihant: pyotp not installed — pip install pyotp in the openalgo venv.")

    try:
        # Normalize the seed: strip whitespace/dashes, uppercase, validate base32 length
        cleaned = totp_secret.replace(" ", "").replace("-", "").upper()
        totp = pyotp.TOTP(cleaned)
        otp_code = totp.now()
    except Exception as e:
        return _fail(f"Arihant: TOTP seed invalid base32: {e}")

    try:
        from broker.arihant.api.auth_api import login_initiate, verify_otp
    except Exception as e:
        return _fail(f"Arihant: plugin import failed: {e}")

    # Step 1: login_initiate (userId + password) → txnId
    try:
        resp1 = login_initiate(api_key=api_key, user_id=user_id, password=password)
    except Exception as e:
        logger.exception("Arihant adapter: login_initiate raised")
        return _fail(f"Arihant login_initiate failed: {e}")

    info_msg = (resp1 or {}).get("infoMsg") or ""
    data1 = (resp1 or {}).get("data") or {}
    txn_id = data1.get("txnId")
    if not txn_id:
        info_id = (resp1 or {}).get("infoID") or ""
        return _fail(
            f"Arihant login_initiate did not return a txnId. "
            f"infoID={info_id!r} infoMsg={info_msg!r}. "
            f"Common causes: wrong trading password, account locked, IP not whitelisted at Arihant."
        )

    # Step 2: verify_otp with the just-generated TOTP code
    try:
        resp2 = verify_otp(api_key=api_key, user_id=user_id, txn_id=txn_id, otp=otp_code)
    except Exception as e:
        logger.exception("Arihant adapter: verify_otp raised")
        return _fail(f"Arihant verify_otp failed: {e}")

    data2 = (resp2 or {}).get("data") or {}
    access_token = data2.get("accessToken")
    refresh_token = data2.get("refreshToken")
    if not access_token or not refresh_token:
        info_id = (resp2 or {}).get("infoID") or ""
        info_msg = (resp2 or {}).get("infoMsg") or ""
        return _fail(
            f"Arihant verify_otp did not return both tokens. "
            f"infoID={info_id!r} infoMsg={info_msg!r}. "
            f"Common causes: TOTP code drift (clock skew), TOTP seed mismatch, OTP type mismatch (Arihant still on SMS)."
        )

    # Persist {user_id}:::{refresh_token} back to broker_creds_db so
    # subsequent calls hit the refresh-token chain (cheaper) until this
    # next rotates again ~6 months out.
    try:
        _persist_refresh_token(user_id, refresh_token, api_key)
    except Exception as e:
        # Soft-fail — caller still gets the access_token for immediate use.
        logger.warning(f"Arihant adapter: refresh_token persistence failed (non-fatal): {e}")

    expires_at = data2.get("tokenExpiry")
    if not expires_at:
        # Arihant access_tokens typically last a few hours; assume 6h.
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()

    return {
        "ok": True,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "user_id": user_id,
        "expires_at": expires_at,
        "error": None,
    }


def _persist_refresh_token(user_id: str, refresh_token: str, api_key: str) -> None:
    """Write {user_id}:::{refresh_token} into broker_creds_db.api_secret_enc
    and update os.environ['BROKER_API_SECRET'] for the current worker."""
    new_secret = f"{user_id}:::{refresh_token}"
    os.environ["BROKER_API_SECRET"] = new_secret
    try:
        from database.broker_creds_db import (
            add_or_update_broker_creds, get_broker_creds,
        )
        from database.user_db import User, db_session
        admin = db_session.query(User).filter_by(is_admin=True).first()
        if admin is None:
            logger.warning("Arihant adapter: no admin user found — refresh_token NOT persisted to DB")
            return
        existing = get_broker_creds(admin.id, "arihant") or {}
        add_or_update_broker_creds(
            user_id=admin.id, broker="arihant",
            api_key=existing.get("api_key") or api_key,
            api_secret=new_secret,
        )
        logger.info("Arihant adapter: rotated refresh_token persisted to broker_creds_db")
    except Exception as e:
        logger.exception("Arihant adapter: broker_creds_db persist failed")
        raise
