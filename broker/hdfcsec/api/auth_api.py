"""HDFC Securities (InvestRight) OAuth2 authentication.

Daily customer flow:
    1. Customer configures InvestRight creds once via the dashboard:
       - api_key    = Consumer Key from developer.hdfcsec.com
       - api_secret = Consumer Secret (used to sign the token exchange)
       - totp_seed  = optional, stored for future automated daily login
    2. Each morning the customer clicks "Connect HDFC Securities":
        a. We redirect the browser to ``HDFC_LOGIN_URL?api_key=<key>``.
        b. The customer logs into InvestRight and approves the app.
        c. InvestRight redirects back to
           ``/broker/hdfcsec/callback?request_token=<token>``.
        d. brlogin captures ``request_token`` and calls
           ``authenticate_broker(request_token)``.
        e. This function POSTs to ``/oapi/v1/access-token`` with the
           ``apiSecret`` body and ``api_key`` + ``request_token`` query
           params; HDFC returns ``accessToken`` (valid 24h).
        f. We pack ``access_token:::api_key:::api_secret`` and hand it
           back to the OpenAlgo auth layer.

Returns the same 4-tuple shape as definedge / icicidirect:
    (auth_string, feed_token, user_id, error_message)
``feed_token`` is None for HDFC (no separate WebSocket token).
"""
from __future__ import annotations

import json as _json
import logging
import os
from typing import Optional, Tuple
from urllib.parse import urlencode

import httpx

from broker.hdfcsec.baseurl import ACCESS_TOKEN_URL, HDFC_LOGIN_URL
from utils.httpx_client import get_httpx_client

log = logging.getLogger(__name__)


def get_login_url() -> Optional[str]:
    """Build the InvestRight OAuth redirect URL.

    Returns None if BROKER_API_KEY is missing — brlogin surfaces a clear
    error to the customer.
    """
    api_key = (os.getenv("BROKER_API_KEY") or "").strip()
    if not api_key:
        log.error("HDFC: BROKER_API_KEY missing — cannot build OAuth URL")
        return None
    return f"{HDFC_LOGIN_URL}?{urlencode({'api_key': api_key})}"


def authenticate_broker(
    request_token: str,
    *,
    api_key: Optional[str] = None,
    api_secret: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Exchange ``request_token`` for an access_token and pack the auth-string.

    Parameters
    ----------
    request_token : str
        The ``request_token`` query parameter handed back by InvestRight
        on OAuth callback.
    api_key, api_secret : optional
        Override the env-var sources (useful for tests).
    """
    try:
        request_token = (request_token or "").strip()
        if not request_token:
            return None, None, None, (
                "HDFC Securities: no request_token returned by InvestRight. "
                "Check the OAuth callback URL is whitelisted in your "
                "developer.hdfcsec.com app settings."
            )

        api_key = (api_key or os.getenv("BROKER_API_KEY") or "").strip()
        api_secret = (api_secret or os.getenv("BROKER_API_SECRET") or "").strip()
        if not api_key or not api_secret:
            return None, None, None, (
                "HDFC Securities: BROKER_API_KEY and BROKER_API_SECRET "
                "must both be configured before connecting. Paste them in "
                "the Manage Brokers screen and try again."
            )

        client = get_httpx_client()
        params = {"api_key": api_key, "request_token": request_token}
        body = {"apiSecret": api_secret}
        log.info(f"HDFC: exchanging request_token at {ACCESS_TOKEN_URL}")

        try:
            resp = client.post(
                ACCESS_TOKEN_URL,
                params=params,
                content=_json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "alphago-live/1.0"},
                timeout=15.0,
            )
        except httpx.HTTPError as e:
            log.exception("HDFC access-token exchange HTTP failure")
            return None, None, None, f"HDFC access-token exchange failed: {e}"

        try:
            data = resp.json()
        except Exception:
            return None, None, None, (
                f"HDFC access-token: non-JSON response "
                f"(status={resp.status_code}): {resp.text[:200]}"
            )

        if data.get("status") != "success":
            err = data.get("message") or data.get("error") or "unknown error"
            return None, None, None, f"HDFC access-token rejected: {err}"

        inner = data.get("data") or {}
        access_token = (
            inner.get("accessToken")
            or inner.get("access_token")
            or data.get("accessToken")
        )
        if not access_token:
            return None, None, None, (
                f"HDFC access-token response missing accessToken: {data!r}"
            )

        user_id = (
            inner.get("client_id")
            or inner.get("clientCode")
            or inner.get("user_id")
            or ""
        )

        auth_string = f"{access_token}:::{api_key}:::{api_secret}"
        log.info(f"HDFC Securities authenticated user_id={user_id!r}")
        return auth_string, None, str(user_id), None

    except Exception as e:
        log.exception("HDFC authenticate_broker failed")
        return None, None, None, f"HDFC Securities auth exception: {e}"
