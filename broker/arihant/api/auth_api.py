"""Arihant TradeBridge auth flow.

Two-step auth (login + OTP verification), then daily refresh-token mints
fresh access tokens without re-prompting OTP. Designed to mirror the
prod-alphaquark-github flow:

  Routes/Broker/Arihant.js initiate-login    →  Arihant /auth/v1/login
                            verify-otp        →  /auth/v1/verify-otp
                            refresh-token     →  /auth/v1/refresh-token

In alphago_live the OpenAlgo standard auth_api.py only exposes
``authenticate_broker(code)`` — used by the OAuth callback handler.
Arihant doesn't fit the OAuth shape, so authenticate_broker delegates to
the refresh-token path: if BROKER_API_SECRET holds a stored refresh_token
(set after the user completes the OTP flow once via /broker/arihant/login),
mint a fresh access token. Otherwise return a clear "please complete OTP
login first" message.

The interactive OTP flow lives in blueprints/broker_arihant.py (added in
this PR alongside the plugin).
"""
from __future__ import annotations

import json
import logging
import os

import httpx

from broker.arihant.baseurl import get_url
from utils.httpx_client import get_httpx_client

log = logging.getLogger(__name__)


def _headers(api_key: str, access_token: str | None = None) -> dict:
    # The `source` header value is per-API-key and Arihant rejects any
    # value other than what's registered at app-creation time with
    # EG006 'Invalid source'. ccxt-india's default has been WEB for
    # years and works for the production aq_backend integrations, but
    # newer API keys (TradeBridge L2 TRADING_API type, post-2026) need
    # a different value that Arihant tells the customer at app-creation.
    # ARIHANT_SOURCE env override lets the per-customer container set
    # the right value without a code change. Probe with curl if unsure
    # — EG006 means the value is wrong; anything else (E_USR001 etc.)
    # means source is accepted and we're getting past that gate.
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "api-key": api_key,
        "source": os.getenv("ARIHANT_SOURCE", "SDK").strip() or "SDK",
    }
    if access_token:
        h["Authorization"] = f"Bearer {access_token}"
    return h


def login_initiate(*, api_key: str, user_id: str, password: str,
                   mob_no: str | None = None, email: str | None = None) -> dict:
    """Step 1: POST /login. Returns {txnId, twoFAType, ...}; caller
    surfaces OTP entry UI to the user."""
    client = get_httpx_client()
    body = {"userId": user_id, "password": password}
    if mob_no:
        body["mobNo"] = mob_no
    if email:
        body["email"] = email
    resp = client.post(get_url("auth.login"),
                       headers=_headers(api_key), content=json.dumps(body))
    return _safe_json(resp)


def verify_otp(*, api_key: str, user_id: str, txn_id: str, otp: str) -> dict:
    """Step 2: POST /verify-otp. Returns {accessToken, refreshToken,
    appId, tokenExpiry, ...}. Both tokens MUST be persisted by the caller —
    refreshToken is what daily auto-login uses."""
    client = get_httpx_client()
    body = {"userId": user_id, "txnId": txn_id, "otp": otp}
    resp = client.post(get_url("auth.verify_otp"),
                       headers=_headers(api_key), content=json.dumps(body))
    return _safe_json(resp)


def resend_otp(*, api_key: str, user_id: str, txn_id: str) -> dict:
    client = get_httpx_client()
    body = {"userId": user_id, "txnId": txn_id}
    resp = client.post(get_url("auth.resend_otp"),
                       headers=_headers(api_key), content=json.dumps(body))
    return _safe_json(resp)


def refresh_access_token(*, api_key: str, user_id: str,
                         refresh_token: str) -> dict:
    """Daily refresh — uses the saved refreshToken to mint a fresh
    accessToken. Returns the same shape as verify_otp; the new
    refreshToken (if present) supersedes the previous one."""
    client = get_httpx_client()
    body = {"userId": user_id, "refreshToken": refresh_token}
    resp = client.post(get_url("auth.refresh"),
                       headers=_headers(api_key), content=json.dumps(body))
    return _safe_json(resp)


def authenticate_broker(code):  # noqa: ARG001 — kept for OpenAlgo contract
    """OpenAlgo's standard broker-auth contract.

    Arihant doesn't have an OAuth redirect flow, so ``code`` is unused.
    Instead we read the refresh token saved at OTP-verify time
    (BROKER_API_SECRET stores ``{user_id}:{refresh_token}`` joined by ':::')
    and use the refresh-token endpoint to mint a fresh access token.

    If no refresh token is stored yet, returns (None, error message
    explaining the customer needs to complete the OTP login first).

    **Refresh-token rotation:** every successful refresh-access-token call
    consumes the previous refresh_token and returns a NEW one. We persist
    the rotated value back to broker_creds_db so the NEXT refresh call
    works. Failure to persist = the next refresh dies with 'Session expired'
    and the customer has to redo the OTP step. Verified on 2026-06-05.
    """
    try:
        api_key = os.getenv("BROKER_API_KEY", "").strip()
        secret = os.getenv("BROKER_API_SECRET", "").strip()
        if not api_key:
            return None, "BROKER_API_KEY (Arihant appId) not set"
        if not secret or ":::" not in secret:
            return None, (
                "Arihant requires one-time OTP login before daily refresh "
                "can work. Visit /broker/arihant/login to complete it."
            )
        user_id, refresh_token = secret.split(":::", 1)
        resp = refresh_access_token(
            api_key=api_key, user_id=user_id, refresh_token=refresh_token,
        )
        data = (resp or {}).get("data") or {}
        access_token = data.get("accessToken")
        if not access_token:
            return None, f"Arihant refresh failed: {resp.get('infoMsg', resp)}"

        # Capture and persist the rotated refresh token, if Arihant returned one.
        new_refresh_token = data.get("refreshToken")
        if new_refresh_token and new_refresh_token != refresh_token:
            new_secret = f"{user_id}:::{new_refresh_token}"
            os.environ["BROKER_API_SECRET"] = new_secret
            try:
                from database.broker_creds_db import (
                    add_or_update_broker_creds, get_broker_creds,
                )
                from database.user_db import User, db_session
                admin = db_session.query(User).filter_by(is_admin=True).first()
                if admin is not None:
                    existing = get_broker_creds(admin.id, "arihant") or {}
                    add_or_update_broker_creds(
                        user_id=admin.id, broker="arihant",
                        api_key=existing.get("api_key") or api_key,
                        api_secret=new_secret,
                    )
                    log.info("Arihant: rotated refresh_token persisted")
                else:
                    log.warning("Arihant: refresh-rotate seen but no admin user — NOT persisted")
            except Exception as persist_err:
                log.exception(f"Arihant: refresh-rotate persist failed: {persist_err}")
                # Don't fail the auth call — the access_token we got is still
                # valid for its lifetime; the next refresh will fail and the
                # customer will be prompted to redo OTP.
        return access_token, None
    except Exception as e:
        log.exception("Arihant authenticate_broker failed")
        return None, f"An exception occurred: {e}"


def _safe_json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except Exception:
        return {
            "infoID": "PARSE_ERROR",
            "infoMsg": f"HTTP {resp.status_code}: {resp.text[:200]}",
            "_http_status": resp.status_code,
        }
