"""ICICI Direct (Breeze API) authentication.

Daily customer flow:
    1. Customer configures Breeze creds once via the dashboard:
       - api_key    = Breeze App Key
       - api_secret = Breeze Secret Key
       - totp_seed  = optional, stored for future automated daily login
    2. Each morning the customer clicks "Connect ICICI Direct":
        a. We build the OAuth URL `BREEZE_AUTH_URL?api_key=<app_key>` and
           redirect the browser.
        b. Breeze authenticates the customer and 302s back to
           `/broker/icicidirect/callback?apisession=<session_token>`.
        c. brlogin captures `apisession` and calls
           `authenticate_broker(session_token)`.
        d. This function validates the token by hitting
           `/breezeapi/api/v1/customerdetails`, then packs
           `session_token:::app_key:::secret_key` into the auth-string
           consumed by every subsequent broker call.

Returns the same 4-tuple shape as definedge:
    (auth_string, feed_token, user_id, error_message)
where `feed_token = session_token` (used by the WS adapter).
"""
from __future__ import annotations

import logging
import os
from typing import Optional, Tuple
from urllib.parse import urlencode

from broker.icicidirect.api.breeze_http import request as breeze_request
from broker.icicidirect.baseurl import BREEZE_AUTH_URL, CUSTOMER_URL

log = logging.getLogger(__name__)


def get_login_url() -> Optional[str]:
    """Build the Breeze OAuth redirect URL.

    Returns None if BROKER_API_KEY is missing — brlogin surfaces a clear
    error to the customer in that case.
    """
    app_key = (os.getenv("BROKER_API_KEY") or "").strip()
    if not app_key:
        log.error("ICICI: BROKER_API_KEY missing — cannot build OAuth URL")
        return None
    return f"{BREEZE_AUTH_URL}?{urlencode({'api_key': app_key})}"


def authenticate_broker(
    code: str,
    *,
    app_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Validate a Breeze session_token and pack the auth-string.

    Parameters
    ----------
    code : str
        The `apisession` query parameter handed back by Breeze on OAuth
        callback. This IS the daily session_token.
    app_key, secret_key : optional
        Override the env-var sources (useful for tests). In normal
        operation these come from BROKER_API_KEY / BROKER_API_SECRET set
        by the per-customer container.
    """
    try:
        session_token = (code or "").strip()
        if not session_token:
            return None, None, None, (
                "ICICI Direct: no session_token returned by Breeze. "
                "Check the OAuth callback URL is whitelisted in your "
                "Breeze developer console."
            )

        app_key = (app_key or os.getenv("BROKER_API_KEY") or "").strip()
        secret_key = (secret_key or os.getenv("BROKER_API_SECRET") or "").strip()
        if not app_key or not secret_key:
            return None, None, None, (
                "ICICI Direct: BROKER_API_KEY and BROKER_API_SECRET must "
                "both be configured before connecting. Paste them in the "
                "Manage Brokers screen and try again."
            )

        auth_string = f"{session_token}:::{app_key}:::{secret_key}"

        # Validate the session token by calling /customerdetails. This is
        # the same call the Breeze SDK uses to confirm a fresh login.
        resp = breeze_request(
            "GET",
            CUSTOMER_URL,
            auth_string,
            payload={"SessionToken": session_token, "AppKey": app_key},
        )
        status = resp.get("Status") if isinstance(resp, dict) else None
        if status not in (200, "200"):
            err = (resp or {}).get("Error") or "Breeze customer-details call failed"
            log.error(f"ICICI customer-details failed: {err}")
            return None, None, None, f"ICICI Direct session validation failed: {err}"

        details = (resp or {}).get("Success") or {}
        if isinstance(details, list) and details:
            details = details[0]
        user_id = ""
        if isinstance(details, dict):
            user_id = (
                details.get("idirect_userid")
                or details.get("user_id")
                or details.get("userId")
                or ""
            )

        log.info(f"ICICI Direct authenticated user_id={user_id!r}")
        return auth_string, session_token, str(user_id), None

    except Exception as e:
        log.exception("ICICI authenticate_broker failed")
        return None, None, None, f"ICICI Direct auth exception: {e}"
