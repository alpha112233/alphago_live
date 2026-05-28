# broker_login_adapters/icicidirect.py
"""Daemon auto-login for ICICI Direct (Breeze API) — UNVERIFIED SKELETON.

⚠️⚠️ THIS ADAPTER HAS NOT BEEN VALIDATED AGAINST A REAL ICICI DIRECT
ACCOUNT. It is NOT in the live ADAPTERS registry — it lives in
_UNVERIFIED_ADAPTERS so the daily scheduler will NOT call it (which
would risk account lockout from repeated failed logins). Promote to
ADAPTERS only after a capture-then-mirror validation session against a
real Breeze login (see "Validation checklist" below).

Background:
    Breeze's official Python SDK (breeze-connect) does NOT do
    programmatic login — it requires the user to open
    api.icicidirect.com/apiuser/login in a browser, complete 2FA, and
    copy the `apisession` token from the redirect URL by hand. So there
    is NO documented programmatic login path. Everything below is a
    best-effort reconstruction of the browser flow, modelled on the
    proven zerodha.py adapter, and MUST be calibrated against a real
    network capture before use.

KNOWN UNKNOWNS (each must be confirmed with a real account + dev tools):
    1. Does Breeze 2FA support TOTP (Google Authenticator), or is it
       SMS/email-OTP only?  If SMS-only, NO autologin is possible —
       delete this file and document the broker as click-only.
    2. The exact login form endpoint + field names. The login PAGE is
       api.icicidirect.com/apiuser/login?api_key=<key>, but the form
       POST target + field names (FdsetUserId / password / etc.) are
       unconfirmed.
    3. The 2FA POST endpoint + field names.
    4. How `apisession` is returned (redirect query param vs. form body).

Contract (matches broker_login_adapters/__init__.py):
    login(creds) -> {ok, access_token, feed_token, user_id, expires_at, error}
    On success `access_token` holds the Breeze `apisession` session_token —
    the same value broker/icicidirect/api/auth_api.authenticate_broker(code)
    consumes.

Required creds keys (mapped from broker_metadata.py "icicidirect"):
    api_key      — Breeze App Key
    api_secret   — Breeze Secret Key
    user_id      — ICICI Direct user id  (NOT currently a metadata field;
                   add an extra.user_id field when validating)
    password     — ICICI Direct login password (extra.password — add when validating)
    totp_secret  — base32 TOTP seed (from totp_seed)

Validation checklist (do this before promoting to ADAPTERS):
    [ ] Confirm TOTP is available for Breeze 2FA on a real account.
    [ ] Open api.icicidirect.com/apiuser/login?api_key=<key> in Chrome
        with Network tab recording "preserve log".
    [ ] Complete a manual login; capture every POST: URL, form fields,
        response shape, the redirect that carries apisession.
    [ ] Replace the ASSUMED_* constants below with the captured values.
    [ ] Test login(creds) end-to-end; confirm the returned apisession
        validates via broker/icicidirect/api/auth_api.authenticate_broker.
    [ ] Move 'icicidirect' from _UNVERIFIED_ADAPTERS to ADAPTERS.
"""
from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

# --- ASSUMED endpoints/fields — UNCONFIRMED, replace after capture --------
ASSUMED_LOGIN_PAGE = "https://api.icicidirect.com/apiuser/login"
ASSUMED_LOGIN_POST = "https://api.icicidirect.com/apiuser/base/login"   # GUESS
ASSUMED_TWOFA_POST = "https://api.icicidirect.com/apiuser/base/twofa"   # GUESS
ASSUMED_USERID_FIELD = "userid"      # GUESS
ASSUMED_PASSWORD_FIELD = "password"  # GUESS
ASSUMED_TOTP_FIELD = "otp"           # GUESS


def _ok(access_token: str, **extra: Any) -> dict:
    return {"ok": True, "access_token": access_token, "error": None, **extra}


def _fail(error: str, **extra: Any) -> dict:
    return {"ok": False, "access_token": None, "error": error, **extra}


def login(creds: dict) -> dict:
    """UNVERIFIED. Returns _fail with a clear message until calibrated.

    The structure below mirrors zerodha.py so that, once the ASSUMED_*
    constants are replaced with captured values, this becomes a working
    adapter with minimal further change. Until then it refuses to run so
    it can't lock out a real account.
    """
    # Hard guard: refuse to execute the speculative flow. Remove this block
    # (and the _UNVERIFIED gate in __init__.py) only after the validation
    # checklist in the module docstring is complete.
    return _fail(
        "ICICI Direct auto-login is UNVERIFIED — not yet calibrated against a "
        "real Breeze login flow. Use the daily Connect-button OAuth click for "
        "now. See broker_login_adapters/icicidirect.py validation checklist."
    )

    # --- Reference implementation (unreachable until guard removed) -------
    try:
        from curl_cffi.requests import Session as CffiSession  # noqa: F401
        import pyotp  # noqa: F401
    except ImportError as e:
        return _fail(f"curl_cffi + pyotp required: {e}")

    required = ("api_key", "user_id", "password", "totp_secret")
    missing = [k for k in required if not creds.get(k)]
    if missing:
        return _fail(f"missing required credentials: {missing}")

    sess = CffiSession(impersonate="chrome131")
    from ._curl_cffi_bind import bind_to_client_ipv6
    bind_to_client_ipv6(sess)

    # Step 1: warm the login page (cookies + any CSRF token).
    try:
        sess.get(ASSUMED_LOGIN_PAGE, params={"api_key": creds["api_key"]}, timeout=15)
    except Exception as e:
        return _fail(f"step1 login page: {e}")

    # Step 2: POST credentials.
    try:
        sess.post(ASSUMED_LOGIN_POST, data={
            ASSUMED_USERID_FIELD: creds["user_id"],
            ASSUMED_PASSWORD_FIELD: creds["password"],
        }, timeout=15)
    except Exception as e:
        return _fail(f"step2 credentials: {e}")

    # Step 3: TOTP.
    remaining = 30 - (int(time.time()) % 30)
    if remaining < 5:
        time.sleep(remaining + 1)
    totp_code = pyotp.TOTP(creds["totp_secret"]).now()
    try:
        r3 = sess.post(ASSUMED_TWOFA_POST, data={ASSUMED_TOTP_FIELD: totp_code},
                       allow_redirects=True, timeout=15)
    except Exception as e:
        return _fail(f"step3 twofa: {e}")

    # Step 4: extract apisession from the final redirect URL.
    qs = parse_qs(urlparse(str(r3.url)).query)
    apisession = qs.get("apisession", [None])[0]
    if not apisession:
        return _fail(f"no apisession in final URL: {str(r3.url)[:200]}")

    return _ok(access_token=apisession, user_id=creds.get("user_id"),
               feed_token=apisession, expires_at=None)
