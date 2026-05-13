# broker_login_adapters/flattrade.py
"""
Daemon auto-login for Flattrade — drives the customer-portal /ftauth web
endpoint with a saved TOTP seed and exchanges the resulting request_code
for the access_token via the same checksum step OpenAlgo already uses.

Flattrade is built on Finvasia/Noren rails but exposes a Flattrade-only
"/ftauth" OAuth-style shim on auth.flattrade.in. That shim accepts the
(UserName, SHA256(Password), 6-digit TOTP) form fields directly and
returns a RedirectURL with `?code=<request_code>` in the JSON body — no
actual 302 to follow. We then run the standard hash exchange to get the
final token.

3-step flow:
  1. POST https://authapi.flattrade.in/auth/session       → text body == sid
  2. POST https://authapi.flattrade.in/ftauth             → {RedirectURL: "?code=..."}
       UserName = USER_ID
       Password = sha256(password).hexdigest()            ← single SHA-256
       PAN_DOB  = pyotp.TOTP(seed).now()                  ← misleadingly named field, carries TOTP
       APIKey   = api_key
       Sid      = sid
       (Override="Y" retry when first response is emsg=="DUPLICATE")
  3. POST https://authapi.flattrade.in/trade/apitoken
       api_secret = sha256(api_key + request_code + api_secret).hexdigest()
                                                          → {token: <access_token>}

Convention: the api_key field in OpenAlgo's broker form stores
"<user_id>:::<api_key>" joined by three colons (same as Flattrade's
official multi-broker docs). We split that here so customers only fill
one field.

Required creds keys (mapped by broker_credentials.py):
    api_key:      "<user_id>:::<api_key>" joined
    api_secret:   Flattrade API Secret
    password:     trading password (saved in extra.password)
    totp_secret:  base32 TOTP seed (the QR-code key, not a 6-digit code)
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

_HOST = "https://auth.flattrade.in"
_API_HOST = "https://authapi.flattrade.in"
_SESSION_URL = f"{_API_HOST}/auth/session"
_FTAUTH_URL = f"{_API_HOST}/ftauth"
_TOKEN_URL = f"{_API_HOST}/trade/apitoken"


def _ok(access_token: str, **extra: Any) -> dict:
    return {"ok": True, "access_token": access_token, "error": None, **extra}


def _fail(error: str, **extra: Any) -> dict:
    return {"ok": False, "access_token": None, "error": error, **extra}


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def login(creds: dict) -> dict:
    try:
        import pyotp
    except ImportError:
        return _fail("pyotp is required for Flattrade auto-login (pip install pyotp)")

    try:
        import requests
    except ImportError:
        return _fail("requests is required (base dep)")

    joined_key = (creds.get("api_key") or "").strip()
    api_secret = (creds.get("api_secret") or "").strip()
    password = creds.get("password") or ""
    totp_secret = (creds.get("totp_secret") or "").strip()

    if ":::" not in joined_key:
        return _fail(
            "Flattrade api_key must be '<user_id>:::<api_key>' (joined by three colons). "
            "Fix the API Key field in Manage Brokers."
        )
    user_id, _, api_key = joined_key.partition(":::")
    user_id = user_id.strip()
    api_key = api_key.strip()

    missing = []
    if not user_id: missing.append("user_id (left of ':::' in API Key field)")
    if not api_key: missing.append("api_key (right of ':::' in API Key field)")
    if not api_secret: missing.append("api_secret")
    if not password: missing.append("password (extra.password)")
    if not totp_secret: missing.append("totp_secret")
    if missing:
        return _fail(f"missing required credentials: {missing}")

    sess = requests.Session()
    sess.headers.update({
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": _HOST,
        "Referer": f"{_HOST}/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        ),
    })

    # Step 1: open a login session. Body is empty, response is plain text == sid.
    try:
        r1 = sess.post(_SESSION_URL, timeout=15)
    except Exception as e:
        return _fail(f"step1 /auth/session: {e}")
    if r1.status_code != 200:
        return _fail(f"step1 /auth/session HTTP {r1.status_code}: {r1.text[:200]}")
    sid = (r1.text or "").strip().strip('"')
    if not sid:
        return _fail(f"step1 /auth/session: empty sid in response: {r1.text[:200]}")

    # Step 2: drive the login form. Sleep a tick if we're at the 30s edge so
    # the TOTP we compute is still valid by the time it lands at Flattrade.
    remaining = 30 - (int(time.time()) % 30)
    if remaining < 5:
        time.sleep(remaining + 1)
    totp_code = pyotp.TOTP(totp_secret).now()
    password_sha = _sha256(password)

    payload = {
        "UserName": user_id,
        "Password": password_sha,
        "PAN_DOB": totp_code,         # field name is "PAN_DOB" but it carries the TOTP
        "App": "",
        "ClientID": "",
        "Key": "",
        "APIKey": api_key,
        "Sid": sid,
        "Override": "",
    }

    def _post_ftauth(p):
        return sess.post(_FTAUTH_URL, json=p, timeout=15)

    try:
        r2 = _post_ftauth(payload)
    except Exception as e:
        return _fail(f"step2 /ftauth: {e}")
    try:
        body2 = r2.json()
    except Exception:
        return _fail(f"step2 /ftauth: non-JSON (status {r2.status_code}): {r2.text[:200]}")

    # Retry once on DUPLICATE — Flattrade flags this when a previous session
    # is still active for the same user. The retry with Override=Y bumps it.
    emsg = (body2.get("emsg") or "").upper()
    if body2.get("stat") != "Ok" and "DUPLICATE" in emsg:
        payload["Override"] = "Y"
        try:
            r2 = _post_ftauth(payload)
            body2 = r2.json()
        except Exception as e:
            return _fail(f"step2 /ftauth retry with Override=Y: {e}")

    if body2.get("stat") != "Ok":
        return _fail(
            f"step2 /ftauth rejected (wrong password/TOTP?): "
            f"{body2.get('emsg') or str(body2)[:200]}"
        )

    redirect_url = body2.get("RedirectURL") or body2.get("redirectURL") or ""
    if not redirect_url:
        return _fail(f"step2 /ftauth: no RedirectURL in response: {body2}")

    request_code = parse_qs(urlparse(redirect_url).query).get("code", [None])[0]
    if not request_code:
        return _fail(f"step2 /ftauth: no 'code' in RedirectURL: {redirect_url}")

    # Step 3: exchange (api_key + request_code + api_secret) checksum for token.
    security_hash = _sha256(f"{api_key}{request_code}{api_secret}")
    try:
        r3 = requests.post(
            _TOKEN_URL,
            json={"api_key": api_key, "request_code": request_code, "api_secret": security_hash},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=20,
        )
        body3 = r3.json()
    except Exception as e:
        return _fail(f"step3 /trade/apitoken: {e}")

    if body3.get("stat") != "Ok" or not body3.get("token"):
        return _fail(
            f"step3 /trade/apitoken rejected: {body3.get('emsg') or str(body3)[:200]}"
        )

    return _ok(
        access_token=body3["token"],
        user_id=user_id,
        feed_token=None,
        expires_at=None,
    )
