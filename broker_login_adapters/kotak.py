# broker_login_adapters/kotak.py
"""
Daemon auto-login for Kotak Securities (Neo) — TOTP-seed driven, no human.

Kotak Neo is the cleanest broker to automate in the IPv6 list: their auth
flow is a native API (no web-form scraping, no TLS fingerprint games), and
they issue a LONG-LIVED Access Token at developer-portal signup that does
NOT rotate daily. The thing that does rotate is the session itself — every
morning we re-do the 2-step TOTP + MPIN login to get a fresh trading
token, bound to a baseUrl for the day's API calls.

Pattern lifted directly from blueprints/../broker/kotak/api/auth_api.py
in this same fork (the OpenAlgo Neo plugin) — the difference is this
adapter takes all credentials as explicit kwargs, decouples from
os.environ, and generates the TOTP code from the saved seed via pyotp.

Two-step Kotak flow:
  1. POST /login/1.0/tradeApiLogin   {mobileNumber, ucc, totp}
       headers: Authorization=<access_token>, neo-fin-key=neotradeapi
       → returns view_token + view_sid
  2. POST /login/1.0/tradeApiValidate {mpin}
       headers: Authorization=<access_token>, sid=view_sid, Auth=view_token
       → returns trading_token, trading_sid, baseUrl

The persisted auth_token is the colon-joined four-tuple that subsequent
Kotak Neo API calls split apart:
    trading_token:::trading_sid:::base_url:::access_token

The broker's order/holdings/etc. modules in OpenAlgo already know how to
split this — see broker/kotak/api/order_api.py.

Required creds keys:
    api_key      — UCC (consumer code, your Kotak trading account ID)
    api_secret   — long-lived Access Token from the Neo developer portal
    mobile_number — registered mobile, accepts +91 / 91 / 10-digit forms
    pin          — 6-digit trading MPIN (we call it `pin` to match the
                   adapter-contract field used by other brokers; in Kotak
                   parlance this is MPIN)
    totp_secret  — base32 TOTP seed

Note: Kotak's `mis.kotaksecurities.com` host has IPv6 (state.json
classifies it `ipv6`). Outbound from our source_bind'd /128 → ✓ broker
sees the customer's whitelisted IPv6.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_LOGIN_URL = "https://mis.kotaksecurities.com/login/1.0/tradeApiLogin"
_VALIDATE_URL = "https://mis.kotaksecurities.com/login/1.0/tradeApiValidate"


def _ok(access_token: str, **extra: Any) -> dict:
    return {"ok": True, "access_token": access_token, "error": None, **extra}


def _fail(error: str, **extra: Any) -> dict:
    return {"ok": False, "access_token": None, "error": error, **extra}


def _normalize_mobile(raw: str) -> str:
    """Kotak's API expects +91XXXXXXXXXX. Accept loose human-typed forms."""
    s = (raw or "").strip().replace(" ", "")
    s = s.removeprefix("+91")
    if s.startswith("91") and len(s) == 12:
        s = s[2:]
    return f"+91{s}"


def login(creds: dict) -> dict:
    """Run the Kotak Neo 2-step TOTP + MPIN login and return the joined
    auth_token that the rest of OpenAlgo's Kotak module expects."""
    try:
        import pyotp
    except ImportError:
        return _fail("pyotp is required for Kotak auto-login (pip install pyotp)")

    try:
        import requests
    except ImportError:
        return _fail("requests is required (it's a base dep, should never happen)")

    missing = [
        k for k in ("api_key", "api_secret", "mobile_number", "pin", "totp_secret")
        if not creds.get(k)
    ]
    if missing:
        return _fail(f"missing required credentials: {missing}")

    ucc = creds["api_key"].strip()
    access_token = creds["api_secret"].strip()
    mobile = _normalize_mobile(creds["mobile_number"])
    mpin = str(creds["pin"]).strip()
    totp_secret = creds["totp_secret"].strip()

    base_headers = {
        "Authorization": access_token,
        "neo-fin-key": "neotradeapi",
        "Content-Type": "application/json",
    }

    # Generate fresh TOTP — wait for the next window if the current one is
    # near expiry, so the verify side doesn't reject a code that ages out
    # in flight.
    remaining = 30 - (int(time.time()) % 30)
    if remaining < 5:
        time.sleep(remaining + 1)
    totp_code = pyotp.TOTP(totp_secret).now()

    # Step 1: tradeApiLogin with TOTP.
    try:
        r1 = requests.post(
            _LOGIN_URL,
            headers=base_headers,
            data=json.dumps({"mobileNumber": mobile, "ucc": ucc, "totp": totp_code}),
            timeout=15,
        )
    except Exception as e:
        return _fail(f"step1 tradeApiLogin: network error: {e}")

    try:
        body1 = r1.json()
    except Exception:
        return _fail(f"step1 tradeApiLogin: non-JSON response (HTTP {r1.status_code}): {r1.text[:200]}")

    if "data" not in body1 or body1.get("data", {}).get("status") != "success":
        # Common failures: wrong TOTP seed (so wrong code), bad UCC, expired
        # access_token. Surface the broker's own error message so the user
        # can act on it.
        msg = body1.get("errMsg") or body1.get("message") or json.dumps(body1)[:200]
        return _fail(f"step1 tradeApiLogin failed: {msg}")

    view_token = body1["data"].get("token")
    view_sid = body1["data"].get("sid")
    if not view_token or not view_sid:
        return _fail(f"step1 tradeApiLogin: response missing token/sid: {body1}")

    # Step 2: tradeApiValidate with MPIN.
    validate_headers = {
        **base_headers,
        "sid": view_sid,
        "Auth": view_token,
    }
    try:
        r2 = requests.post(
            _VALIDATE_URL,
            headers=validate_headers,
            data=json.dumps({"mpin": mpin}),
            timeout=15,
        )
    except Exception as e:
        return _fail(f"step2 tradeApiValidate: network error: {e}")

    try:
        body2 = r2.json()
    except Exception:
        return _fail(f"step2 tradeApiValidate: non-JSON (HTTP {r2.status_code}): {r2.text[:200]}")

    if "data" not in body2 or body2.get("data", {}).get("status") != "success":
        msg = body2.get("errMsg") or body2.get("message") or json.dumps(body2)[:200]
        return _fail(f"step2 tradeApiValidate failed (wrong MPIN?): {msg}")

    trading_token = body2["data"].get("token")
    trading_sid = body2["data"].get("sid")
    base_url = body2["data"].get("baseUrl") or ""
    if not trading_token or not trading_sid:
        return _fail(f"step2 tradeApiValidate: response missing token/sid: {body2}")
    if not base_url:
        logger.warning("Kotak: baseUrl missing in validate response — Neo API calls may fail")

    # The 4-tuple is what subsequent broker calls expect. Order matches
    # broker/kotak/api/order_api.py's split logic.
    joined = f"{trading_token}:::{trading_sid}:::{base_url}:::{access_token}"

    return _ok(
        access_token=joined,
        user_id=ucc,         # Kotak's identifier for the user IS their UCC
        feed_token=None,
        expires_at=None,     # Kotak doesn't return an explicit expiry; valid until next IST midnight in practice
    )
