# broker_login_adapters/aliceblue.py
"""
Daemon auto-login for AliceBlue (ANT v2) — drives the customer-portal SSO
endpoints with a saved TOTP seed and exchanges the resulting authCode for
the long-lived 24h userSession via the vendor checksum.

AliceBlue exposes ONE login form (https://ant.aliceblueonline.com) for
their web app AND for OAuth vendor authorization. The browser flow is:
  client_id → password → 2FA (TOTP or year-of-birth) → vendor consent →
  redirect to <redirect_uri>?authCode=<...>

The same flow is reachable as a sequence of JSON POSTs hitting
`/rest/AliceBlueAPIService/`. No browser, no Playwright. The 2FA step
still uses the `answer1` field whether the customer's 2FA is TOTP or YOB
— AliceBlue did not split the endpoints when they added TOTP support
(see krishnavelu/alice_blue#334).

5-step flow:
  1. POST /api/customer/getEncryptionKey   {userId}                     → encKey
  2. POST /api/customer/webLogin           {userId, userData=AES(pw,encKey)} → session cookie
  3. POST /api/sso/validAnswer             {answer1=<TOTP>, sCount=1, sIndex=1,
                                           userId, vendor=<app_code>}  → redirectUrl
  4. parse authCode from redirectUrl
  5. POST /open-api/od/v1/vendor/getUserDetails {checkSum=sha256(uid+code+secret)}
                                                                       → userSession

Required creds keys (mapped by broker_credentials.py from db_creds+extra):
    api_key:       AliceBlue App Code (the "appcode" from the vendor portal)
    api_secret:    AliceBlue API Secret
    user_id:       client ID (e.g. "AB123456") — saved as client_code
    password:      trading password — saved in extra.password
    totp_secret:   base32 TOTP seed
    redirect_uri:  must match the vendor app's registered URL (we send it
                   for completeness; AliceBlue cross-checks server-side
                   against the app_code-bound URL)
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import _cryptojs_aes

logger = logging.getLogger(__name__)

_BASE = "https://ant.aliceblueonline.com"
_ENC_KEY_URL = f"{_BASE}/rest/AliceBlueAPIService/api/customer/getEncryptionKey"
_WEB_LOGIN_URL = f"{_BASE}/rest/AliceBlueAPIService/api/customer/webLogin"
_VALID_ANSWER_URL = f"{_BASE}/rest/AliceBlueAPIService/api/sso/validAnswer"
_AUTHORIZE_VENDOR_URL = f"{_BASE}/rest/AliceBlueAPIService/api/sso/authorizeVendor"
_GET_USER_DETAILS_URL = f"{_BASE}/open-api/od/v1/vendor/getUserDetails"


def _ok(access_token: str, **extra: Any) -> dict:
    return {"ok": True, "access_token": access_token, "error": None, **extra}


def _fail(error: str, **extra: Any) -> dict:
    return {"ok": False, "access_token": None, "error": error, **extra}


def login(creds: dict) -> dict:
    """Drive the AliceBlue ANT v2 vendor-login flow end-to-end."""
    try:
        import pyotp
    except ImportError:
        return _fail("pyotp is required for AliceBlue auto-login (pip install pyotp)")

    try:
        import requests
    except ImportError:
        return _fail("requests is required (base dep)")

    required = ("api_key", "api_secret", "user_id", "password", "totp_secret")
    missing = [k for k in required if not creds.get(k)]
    if missing:
        return _fail(f"missing required credentials: {missing}")

    app_code = creds["api_key"].strip()
    api_secret = creds["api_secret"].strip()
    user_id = creds["user_id"].strip().upper()
    password = str(creds["password"])
    totp_secret = creds["totp_secret"].strip()

    sess = requests.Session()
    sess.headers.update({
        "accept": "application/json",
        "content-type": "application/json",
        "origin": _BASE,
        "referer": f"{_BASE}/",
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        ),
    })

    # Step 1: fetch the per-session encryption key.
    try:
        r1 = sess.post(_ENC_KEY_URL, json={"userId": user_id}, timeout=15)
        body1 = r1.json()
    except Exception as e:
        return _fail(f"step1 getEncryptionKey: {e}")

    enc_key = body1.get("encKey")
    if not enc_key:
        msg = body1.get("emsg") or body1.get("message") or str(body1)[:200]
        return _fail(f"step1 getEncryptionKey rejected: {msg}")

    # Step 2: encrypt the password with the AES/CryptoJS-compat helper and submit.
    user_data = _cryptojs_aes.encrypt(password, enc_key)
    try:
        r2 = sess.post(_WEB_LOGIN_URL, json={"userId": user_id, "userData": user_data}, timeout=15)
        body2 = r2.json()
    except Exception as e:
        return _fail(f"step2 webLogin: {e}")

    # AliceBlue returns stat:"Not_ok" with emsg on a bad password; on success
    # it sets a session cookie and either advances to 2FA or returns a hint.
    if body2.get("stat") == "Not_ok":
        return _fail(f"step2 webLogin rejected (wrong password?): {body2.get('emsg') or body2}")

    # Step 3: 2FA. Compute fresh TOTP; if we're near the window edge, sleep
    # a tick so the answer is valid through the round-trip.
    remaining = 30 - (int(time.time()) % 30)
    if remaining < 5:
        time.sleep(remaining + 1)
    totp_code = pyotp.TOTP(totp_secret).now()

    valid_answer_payload = {
        "userId": user_id,
        "answer1": totp_code,
        "sCount": "1",
        "sIndex": "1",
        "vendor": app_code,
    }
    try:
        r3 = sess.post(_VALID_ANSWER_URL, json=valid_answer_payload, timeout=15)
        body3 = r3.json()
    except Exception as e:
        return _fail(f"step3 validAnswer: {e}")

    if body3.get("stat") == "Not_ok":
        return _fail(f"step3 validAnswer rejected (wrong TOTP or YOB?): {body3.get('emsg') or body3}")

    # Step 4: extract authCode. The response either contains a `redirectUrl`
    # (with authCode= in the querystring) or, if the customer has not yet
    # authorized this vendor app for their account, asks us to call
    # /sso/authorizeVendor first.
    redirect_url = body3.get("redirectUrl") or body3.get("url") or ""
    is_authorized = body3.get("isAuthorized")

    if not redirect_url and is_authorized is False:
        try:
            r3b = sess.post(
                _AUTHORIZE_VENDOR_URL,
                json={"userId": user_id, "vendor": app_code},
                timeout=15,
            )
            body3b = r3b.json()
        except Exception as e:
            return _fail(f"step3b authorizeVendor: {e}")
        redirect_url = body3b.get("redirectUrl") or body3b.get("url") or ""
        if not redirect_url:
            return _fail(f"step3b authorizeVendor: no redirectUrl: {body3b}")

    if not redirect_url:
        return _fail(f"step3 validAnswer: no redirectUrl in response: {body3}")

    auth_code = parse_qs(urlparse(redirect_url).query).get("authCode", [None])[0]
    if not auth_code:
        return _fail(f"step3 validAnswer: no authCode in redirectUrl: {redirect_url}")

    # Step 5: exchange (userId + authCode + apiSecret) checksum for userSession.
    checksum = hashlib.sha256(f"{user_id}{auth_code}{api_secret}".encode("utf-8")).hexdigest()
    try:
        r5 = requests.post(
            _GET_USER_DETAILS_URL,
            json={"checkSum": checksum},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=20,
        )
        body5 = r5.json()
    except Exception as e:
        return _fail(f"step5 getUserDetails: {e}")

    if body5.get("stat") != "Ok" or not body5.get("userSession"):
        msg = body5.get("emsg") or body5.get("message") or str(body5)[:200]
        return _fail(f"step5 getUserDetails rejected: {msg}")

    return _ok(
        access_token=body5["userSession"],
        user_id=body5.get("clientId") or user_id,
        feed_token=None,
        expires_at=None,
    )
