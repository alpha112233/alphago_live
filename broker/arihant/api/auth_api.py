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
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "api-key": api_key,
        "source": "WEB",
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
