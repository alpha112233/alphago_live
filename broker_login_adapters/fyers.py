# broker_login_adapters/fyers.py
"""
Daemon auto-login for Fyers — drives the v2 'vagator' web auth endpoints
with a saved TOTP seed + trading PIN.

The OFFICIAL Fyers documented v3 OAuth flow requires a human browser
session (user clicks "Authorize" at api-t1.fyers.in). But Fyers' own web
app uses an INTERNAL /vagator/v2 endpoint set that accepts (fy_id, TOTP,
PIN) directly and returns the auth_code we'd otherwise harvest from the
OAuth redirect. We drive those endpoints from this adapter, then hand
the auth_code to the documented v3 /validate-authcode for the final
access_token. End-to-end no human interaction.

5-step flow:
  1. POST /vagator/v2/send_login_otp_v2 {fy_id_b64, app_id="2"}
       → request_key (a UUID identifying the login session)
  2. POST /vagator/v2/verify_otp {request_key, otp=<pyotp.TOTP(seed).now()>}
       → new request_key (now "OTP_VERIFIED")
  3. POST /vagator/v2/verify_pin {request_key, identifier=fy_id_b64,
                                  pin=<base64(pin)>}
       → access_token cookie + an auth-flow handle
  4. GET  /api/v3/token?client_id=...&redirect_uri=...&response_type=code
                       &state=...&nonce=...&scope=  with the cookie set
       → 302 redirect to redirect_uri?auth_code=...&...
  5. POST /api/v3/validate-authcode {grant_type=authorization_code,
                                    appIdHash=sha256(api_key:api_secret),
                                    code=auth_code}
       → final access_token

This pattern is the same one community libs (fyers-apiv2-totp, etc.)
have used since 2024. Fyers has occasionally changed payload-encoding
details so we surface their JSON errors verbatim to make iteration
easy when something drifts.

Required creds keys (the endpoint mapping in broker_credentials.py
populates these from db_creds + extra.*):
    api_key:       Fyers app_id (string)
    api_secret:    Fyers app secret
    redirect_uri:  must EXACTLY match what's registered in the Fyers
                   developer app at myapi.fyers.in
    mobile_number: ALIASED — for Fyers this carries the Fyers Client ID
                   (e.g. "XK12345"). Our endpoint maps client_code → this.
    pin:           4-digit trading PIN
    totp_secret:   base32 TOTP seed
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
import uuid
from typing import Any
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

_SEND_OTP_URL = "https://api-t2.fyers.in/vagator/v2/send_login_otp_v2"
_VERIFY_OTP_URL = "https://api-t2.fyers.in/vagator/v2/verify_otp"
_VERIFY_PIN_URL = "https://api-t2.fyers.in/vagator/v2/verify_pin"
_TOKEN_URL = "https://api-t1.fyers.in/api/v3/token"
_VALIDATE_AUTHCODE_URL = "https://api-t1.fyers.in/api/v3/validate-authcode"


def _ok(access_token: str, **extra: Any) -> dict:
    return {"ok": True, "access_token": access_token, "error": None, **extra}


def _fail(error: str, **extra: Any) -> dict:
    return {"ok": False, "access_token": None, "error": error, **extra}


def _b64(s: str) -> str:
    """Fyers expects fy_id and pin base64-encoded in the vagator payloads."""
    return base64.b64encode(s.encode("utf-8")).decode("utf-8")


def login(creds: dict) -> dict:
    """Drive the Fyers vagator-v2 + validate-authcode flow."""
    try:
        import pyotp
    except ImportError:
        return _fail("pyotp is required for Fyers auto-login (pip install pyotp)")

    try:
        import requests
    except ImportError:
        return _fail("requests is required (base dep)")

    required = ("api_key", "api_secret", "redirect_uri", "mobile_number", "pin", "totp_secret")
    missing = [k for k in required if not creds.get(k)]
    if missing:
        return _fail(f"missing required credentials: {missing}")

    api_key = creds["api_key"].strip()
    api_secret = creds["api_secret"].strip()
    redirect_uri = creds["redirect_uri"].strip()
    # In our credential schema, client_code → mobile_number → fy_id for Fyers.
    fy_id = creds["mobile_number"].strip().upper()
    pin = str(creds["pin"]).strip()
    totp_secret = creds["totp_secret"].strip()

    fy_id_b64 = _b64(fy_id)
    pin_b64 = _b64(pin)

    sess = requests.Session()
    sess.headers.update({
        "accept": "application/json",
        "content-type": "application/json",
        "origin": "https://login.fyers.in",
        "referer": "https://login.fyers.in/",
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        ),
    })

    # Step 1: send_login_otp_v2 — opens a login session, returns request_key.
    try:
        r1 = sess.post(
            _SEND_OTP_URL,
            data=json.dumps({"fy_id": fy_id_b64, "app_id": "2"}),
            timeout=15,
        )
        body1 = r1.json()
    except Exception as e:
        return _fail(f"step1 send_login_otp: {e}")

    if body1.get("s") != "ok" and "request_key" not in body1:
        msg = body1.get("message") or body1.get("error") or str(body1)[:200]
        return _fail(f"step1 send_login_otp rejected: {msg}")

    request_key = body1.get("request_key")
    if not request_key:
        return _fail(f"step1 send_login_otp: no request_key in response: {body1}")

    # Step 2: verify_otp — generate fresh TOTP, send it.
    remaining = 30 - (int(time.time()) % 30)
    if remaining < 5:
        time.sleep(remaining + 1)
    totp_code = pyotp.TOTP(totp_secret).now()

    try:
        r2 = sess.post(
            _VERIFY_OTP_URL,
            data=json.dumps({"request_key": request_key, "otp": totp_code}),
            timeout=15,
        )
        body2 = r2.json()
    except Exception as e:
        return _fail(f"step2 verify_otp: {e}")

    if body2.get("s") != "ok" and "request_key" not in body2:
        msg = body2.get("message") or str(body2)[:200]
        return _fail(f"step2 verify_otp rejected (wrong TOTP seed?): {msg}")

    request_key2 = body2.get("request_key") or request_key

    # Step 3: verify_pin — base64-encoded PIN, identifier is fy_id.
    try:
        r3 = sess.post(
            _VERIFY_PIN_URL,
            data=json.dumps({
                "request_key": request_key2,
                "identifier": pin_b64,
                "identity_type": "pin",
            }),
            timeout=15,
        )
        body3 = r3.json()
    except Exception as e:
        return _fail(f"step3 verify_pin: {e}")

    if body3.get("s") != "ok":
        msg = body3.get("message") or str(body3)[:200]
        return _fail(f"step3 verify_pin rejected (wrong PIN?): {msg}")

    fyers_token = (body3.get("data") or {}).get("access_token")
    if not fyers_token:
        return _fail(f"step3 verify_pin: no access_token in response: {body3}")

    # Step 4: token endpoint — pass the bearer + standard OAuth params, get
    # back a 302 redirect to our app's redirect_uri with auth_code= in the
    # query string.
    state = uuid.uuid4().hex
    try:
        r4 = sess.post(
            _TOKEN_URL,
            data=json.dumps({
                "fyers_id": fy_id,
                "app_id": api_key.split("-")[0],  # Fyers app_id is the bit before any dash
                "redirect_uri": redirect_uri,
                "appType": "100",                  # web app
                "code_challenge": "",
                "state": state,
                "scope": "",
                "nonce": "",
                "response_type": "code",
                "create_cookie": True,
            }),
            headers={"authorization": f"Bearer {fyers_token}"},
            timeout=15,
        )
        body4 = r4.json()
    except Exception as e:
        return _fail(f"step4 token request: {e}")

    redirect_url_with_code = (body4.get("Url") or body4.get("data") or {})
    if isinstance(redirect_url_with_code, dict):
        redirect_url_with_code = redirect_url_with_code.get("Url") or ""
    if not redirect_url_with_code:
        # Sometimes Fyers returns the redirect URL at the top-level. Surface
        # everything so we can iterate when their schema drifts.
        return _fail(f"step4 token: no redirect URL in response: {body4}")

    auth_code = parse_qs(urlparse(str(redirect_url_with_code)).query).get("auth_code", [None])[0]
    if not auth_code:
        return _fail(f"step4 token: no auth_code in redirect URL: {redirect_url_with_code}")

    # Step 5: exchange auth_code for the actual access_token. This step
    # matches OpenAlgo's existing broker/fyers/api/auth_api.py exactly.
    app_id_hash = hashlib.sha256(f"{api_key}:{api_secret}".encode("utf-8")).hexdigest()
    try:
        r5 = requests.post(
            _VALIDATE_AUTHCODE_URL,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            json={
                "grant_type": "authorization_code",
                "appIdHash": app_id_hash,
                "code": auth_code,
            },
            timeout=20,
        )
        body5 = r5.json()
    except Exception as e:
        return _fail(f"step5 validate-authcode: {e}")

    if body5.get("s") != "ok":
        msg = body5.get("message") or str(body5)[:200]
        return _fail(f"step5 validate-authcode rejected: {msg}")

    access_token = body5.get("access_token")
    if not access_token:
        return _fail(f"step5 validate-authcode: no access_token: {body5}")

    return _ok(
        access_token=access_token,
        user_id=fy_id,
        feed_token=None,
        expires_at=None,
    )
