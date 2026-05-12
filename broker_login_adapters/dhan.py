# broker_login_adapters/dhan.py
"""
Daemon auto-login for Dhan — single-call TOTP login. The simplest of all
the broker adapters: one POST returns a 24h JWT.

Dhan exposes a first-class programmable auth endpoint at
    POST https://auth.dhan.co/app/generateAccessToken
        ?dhanClientId=...&pin=...&totp=...

  - dhanClientId: customer's Dhan Client ID (separate from the API Key)
  - pin:          4-digit web/app PIN (not the API secret)
  - totp:         6-digit code from pyotp.TOTP(seed).now()

Response on success:
    {
      "dhanClientId":   "1100000123",
      "dhanClientName": "...",
      "dhanClientUcc":  "...",
      "accessToken":    "<24h JWT>",
      "expiryTime":     "2026-05-14T...+05:30"
    }

The JWT is valid 24h. Calling generateAccessToken again invalidates the
previous token, so the daemon should run this once a day (~05:00 IST is
a safe slot; markets open ~09:15 IST). Use api.dhan.co/v2/RenewToken for
a mid-session extension if needed (not implemented here — daemon runs
once daily and the JWT lasts the whole trading day).

Prerequisite (customer-side, one-time):
  Enable TOTP-for-API on web.dhan.co → My Profile → 2FA Settings → save
  the base32 seed shown during setup. This is what we generate codes
  from. The PIN is the existing trading PIN (Dhan calls it the web/app
  PIN — not the API secret).

Required creds keys (the endpoint maps these from db_creds):
    api_key:     stored as "<dhanClientId>:::<apiKey>" — we only need the
                 client_id prefix; the apiKey suffix is used for actual
                 trading API calls, not the auth endpoint.
    pin:         4-digit trading PIN (passed via the endpoint mapping
                 logic from extra.pin / extra.password)
    totp_secret: base32 TOTP seed

api_secret is NOT used by /generateAccessToken (it's used by the partner
OAuth-consent flow in OpenAlgo's existing broker module, but not here).
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_GENERATE_ACCESS_TOKEN_URL = "https://auth.dhan.co/app/generateAccessToken"


def _ok(access_token: str, **extra: Any) -> dict:
    return {"ok": True, "access_token": access_token, "error": None, **extra}


def _fail(error: str, **extra: Any) -> dict:
    return {"ok": False, "access_token": None, "error": error, **extra}


def login(creds: dict) -> dict:
    """Run Dhan's single-call TOTP login and return the 24h JWT."""
    try:
        import pyotp
    except ImportError:
        return _fail("pyotp is required for Dhan auto-login (pip install pyotp)")

    try:
        import requests
    except ImportError:
        return _fail("requests is required (base dep)")

    # Parse api_key — stored as "<dhanClientId>:::<apiKey>" per
    # broker_metadata.py. The client_id prefix is what we need; the
    # apiKey suffix is for downstream trading API calls.
    api_key_raw = (creds.get("api_key") or "").strip()
    if ":::" in api_key_raw:
        dhan_client_id, _api_key_suffix = api_key_raw.split(":::", 1)
        dhan_client_id = dhan_client_id.strip()
    else:
        # If the customer saved it as just the client_id (no ::: suffix),
        # use the whole field as the client_id.
        dhan_client_id = api_key_raw

    pin = str(creds.get("pin") or "").strip()
    totp_secret = (creds.get("totp_secret") or "").strip()

    missing = []
    if not dhan_client_id:
        missing.append("dhan_client_id (api_key prefix before ':::')")
    if not pin:
        missing.append("pin (4-digit web/app PIN)")
    if not totp_secret:
        missing.append("totp_secret")
    if missing:
        return _fail(f"missing required Dhan credentials: {missing}")

    # Generate the TOTP, waiting briefly if the current 30s window has
    # less than 5s left so the code doesn't age out in flight.
    remaining = 30 - (int(time.time()) % 30)
    if remaining < 5:
        time.sleep(remaining + 1)
    totp_code = pyotp.TOTP(totp_secret).now()

    try:
        r = requests.post(
            _GENERATE_ACCESS_TOKEN_URL,
            params={
                "dhanClientId": dhan_client_id,
                "pin": pin,
                "totp": totp_code,
            },
            timeout=15,
        )
    except Exception as e:
        return _fail(f"generateAccessToken: network error: {e}")

    # Surface Dhan's exact error if non-200 — they return JSON with a
    # readable message field, so don't bury it.
    try:
        body = r.json()
    except Exception:
        return _fail(f"generateAccessToken: HTTP {r.status_code} non-JSON: {r.text[:200]}")

    if r.status_code != 200:
        msg = body.get("message") or body.get("errorMessage") or str(body)[:200]
        return _fail(f"generateAccessToken HTTP {r.status_code}: {msg}")

    access_token = body.get("accessToken")
    if not access_token:
        return _fail(f"generateAccessToken: no accessToken in body: {body}")

    return _ok(
        access_token=access_token,
        user_id=body.get("dhanClientId") or dhan_client_id,
        feed_token=None,
        expires_at=body.get("expiryTime"),
    )
