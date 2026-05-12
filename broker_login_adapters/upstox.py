# broker_login_adapters/upstox.py
"""
Daemon auto-login for Upstox — drives the daily 2FA web flow without a
human, given the customer's TOTP seed.

Lifted from alpha_live/live/broker/upstox.py::_http_login (the production
adapter that powers the daily 03:25 IST refresh cron there). Three sources
of complexity it captures:

  1. Upstox's TLS fingerprinting — vanilla `requests` is rejected at the
     TLS layer. We use curl_cffi with `impersonate="chrome131"` to look
     like a real Chrome browser.

  2. The login is IP-dependent. The first request (OAuth dialog endpoint)
     returns the login page HTML to non-whitelisted IPs (no user_id, no
     flow), but returns a 302 with user_id query param to the whitelisted
     IP. The container's source IPv6 (via utils/source_bind) is what makes
     this work in our setup.

  3. The flow has 5 steps that must execute in sequence within the same
     session (cookies carry state):
        a) GET dialog → user_id
        b) POST otp → validateOTPToken
        c) POST otp-totp/verify → cookie marks TOTP done
        d) POST 2fa SECRET_PIN → may return code= already
        e) POST oauth/authorize → final code=

  4. After the 5-step flow yields a code, we still have to POST it to
     /v2/login/authorization/token with client_id+client_secret to get
     the actual access_token JWT.

Caller responsibilities (the REST endpoint or scheduler):
  - Pass decrypted credentials in (do NOT read broker_creds_db here)
  - On success, persist access_token via database.auth_db.upsert_auth so
    OpenAlgo's normal session flow can use it
  - On failure, surface the error to the customer (broken TOTP seed?
    expired Upstox account? IP not whitelisted at Upstox?)
"""

from __future__ import annotations

import base64
import logging
import random
import string
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

# ---- Upstox internal endpoints (same as alpha_live uses) -------------------

AUTH_DIALOG_URL = "https://api.upstox.com/v2/login/authorization/dialog"
_SERVICE_URL = "https://service.upstox.com"
_OTP_GENERATE_URL = f"{_SERVICE_URL}/login/open/v4/auth/1fa/otp/generate"
_TOTP_VERIFY_URL = f"{_SERVICE_URL}/login/open/v4/auth/1fa/otp-totp/verify"
_PIN_2FA_URL = f"{_SERVICE_URL}/login/open/v6/auth/2fa"
_OAUTH_AUTHORIZE_URL = f"{_SERVICE_URL}/login/open/v6/auth/oauth/authorize"
_INTERNAL_REDIRECT_URI = "https://account.upstox.com/"  # matches what Upstox's web app posts
_TOKEN_EXCHANGE_URL = "https://api.upstox.com/v2/login/authorization/token"


def _ok(access_token: str, **extra: Any) -> dict:
    return {"ok": True, "access_token": access_token, "error": None, **extra}


def _fail(error: str, **extra: Any) -> dict:
    return {"ok": False, "access_token": None, "error": error, **extra}


def precheck(creds: dict) -> dict:
    """Lightweight validation: does the (api_key, redirect_uri) pair match
    what's registered in the customer's Upstox developer app?

    Single GET to /authorization/dialog — Upstox responds with either a
    302 redirect (config OK, proceed to actual login) or a JSON error
    (UDAPI100068 etc., config mismatch). No TOTP consumed.

    Designed to run at save-time so the user finds out about a config
    error immediately, not at auto-login time. Returns the same contract
    shape as login(): {ok, error, ...}.
    """
    api_key = (creds.get("api_key") or "").strip()
    redirect_uri = (creds.get("redirect_uri") or "").strip()
    if not api_key or not redirect_uri:
        return _ok("")  # nothing to check yet

    try:
        from curl_cffi.requests import Session as CffiSession
    except ImportError:
        # If curl_cffi isn't installed (e.g. dev env), skip the precheck
        # rather than blocking save.
        return _ok("")

    sess = CffiSession(impersonate="chrome131")
    try:
        resp = sess.get(
            AUTH_DIALOG_URL,
            params={"response_type": "code", "client_id": api_key, "redirect_uri": redirect_uri},
            allow_redirects=False,  # don't follow — we just need the response shape
            timeout=10,
        )
    except Exception as e:
        # Network errors don't mean creds are wrong — let save proceed.
        return _ok("")

    # 302 with a Location header = config OK. Anything else (200 with JSON
    # error body) = bad config.
    if resp.status_code in (302, 303):
        return _ok("")
    try:
        body = resp.json()
    except Exception:
        return _ok("")  # unrecognized response shape — don't block save
    errs = body.get("errors") if isinstance(body, dict) else None
    if errs and isinstance(errs, list):
        msg = errs[0].get("message") or errs[0].get("errorCode") or "unknown"
        return _fail(
            f"Upstox rejected this api_key + redirect_uri combination: {msg}. "
            f"In your Upstox developer app at https://account.upstox.com/developer/apps, "
            f"set 'Redirect URI' to EXACTLY: {redirect_uri}"
        )
    return _ok("")


_VERSION_VARIANTS = [
    # (appVersion, userAgent_suffix, include_x_device_details)
    # Tried in order until one succeeds or all fail. Upstox sometimes
    # rejects a particular (sourceIP × appVersion) combination with
    # error 1017072; bumping the claimed version often clears it.
    ("4.0.0", "Upstox 3.0", True),     # alpha_live's known-working values
    ("8.0.0", "Upstox 8.0", True),     # bumped, in case Upstox raised the floor
    ("", "", False),                    # last resort: drop x-device-details entirely
]


def login(creds: dict) -> dict:
    """Drive the Upstox daily 2FA flow + token exchange.

    Wraps the actual flow in a retry loop over header-version variants
    so we can recover from Upstox's intermittent error 1017072 ("This
    version is outdated") without manual intervention. See
    `_VERSION_VARIANTS` for the fallback order.
    """
    last = None
    for app_version, ua_suffix, include_xdd in _VERSION_VARIANTS:
        last = _run_login_flow(creds, app_version, ua_suffix, include_xdd)
        if last.get("ok"):
            return last
        # Only retry if the failure was specifically the version check.
        # Other failures (wrong TOTP, missing creds, redirect_uri mismatch)
        # won't be fixed by trying a different version.
        if "1017072" not in (last.get("error") or "") and "outdated" not in (last.get("error") or "").lower():
            return last
        logger.warning(
            f"Upstox: variant (appVersion={app_version!r}, ua_suffix={ua_suffix!r}, "
            f"include_xdd={include_xdd}) hit 1017072 — trying next variant"
        )
    return last or _fail("all version variants failed")


def _run_login_flow(creds: dict, app_version: str, ua_suffix: str, include_x_device_details: bool) -> dict:
    """One pass of the 5-step + token-exchange flow with the given header
    version claims. Returns the same contract as login()."""
    try:
        from curl_cffi.requests import Session as CffiSession
    except ImportError:
        return _fail("curl_cffi is required for Upstox auto-login (pip install curl_cffi)")

    try:
        import pyotp
    except ImportError:
        return _fail("pyotp is required for Upstox auto-login (pip install pyotp)")

    # Validate inputs upfront so we fail clearly without hitting the network.
    missing = [k for k in ("api_key", "api_secret", "redirect_uri", "mobile_number", "pin", "totp_secret")
               if not creds.get(k)]
    if missing:
        return _fail(f"missing required credentials: {missing}")

    api_key = creds["api_key"]
    api_secret = creds["api_secret"]
    redirect_uri = creds["redirect_uri"]
    mobile_number = creds["mobile_number"]
    pin = str(creds["pin"])
    totp_secret = creds["totp_secret"]

    request_id = "WPRO-" + "".join(random.choices(string.ascii_letters + string.digits, k=10))
    headers = {
        "accept": "*/*",
        "accept-language": "en-GB,en;q=0.9",
        "content-type": "application/json",
        "origin": "https://login.upstox.com",
        "referer": "https://login.upstox.com",
        "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        ),
        "x-request-id": request_id,
    }
    if include_x_device_details:
        headers["x-device-details"] = (
            "platform=WEB|osName=Mac OS/10.15.7|osVersion=Chrome/140.0.0.0|"
            f"appVersion={app_version}|modelName=Chrome|manufacturer=Apple|"
            f"uuid={request_id}|userAgent={ua_suffix}"
        )

    sess = CffiSession(impersonate="chrome131")
    sess.headers.update(headers)

    # Step 1: Initiate OAuth dialog. Whitelisted IPs get a 302 with user_id in
    # the redirect URL query string; non-whitelisted IPs get a generic login
    # page (no user_id) and we can't proceed.
    try:
        resp = sess.get(
            AUTH_DIALOG_URL,
            params={"response_type": "code", "client_id": api_key, "redirect_uri": redirect_uri},
            allow_redirects=True,
            timeout=15,
        )
    except Exception as e:
        return _fail(f"step1 OAuth dialog: network error: {e}")

    parsed = urlparse(str(resp.url))
    qs = parse_qs(parsed.query)
    user_id = qs.get("user_id", [None])[0]
    client_id = qs.get("client_id", [api_key])[0]
    if not user_id:
        # Surface Upstox's own error when the dialog endpoint returns JSON
        # instead of redirecting. This typically happens with UDAPI100068
        # ("Check your client_id and redirect_uri") when the registered
        # redirect URI in the Upstox app doesn't match what we send.
        upstox_msg = None
        try:
            body = resp.json()
            errs = body.get("errors") if isinstance(body, dict) else None
            if errs and isinstance(errs, list):
                upstox_msg = errs[0].get("message") or errs[0].get("errorCode")
        except Exception:
            pass
        if upstox_msg:
            return _fail(
                f"step1 OAuth dialog rejected by Upstox: {upstox_msg}. "
                f"Verify the redirect URI registered in your Upstox developer app at "
                f"https://account.upstox.com/developer/apps EXACTLY matches: {redirect_uri}"
            )
        return _fail(
            "step1 OAuth dialog: no user_id in redirect. Likely causes: "
            "(a) the redirect_uri in the Upstox app registration doesn't match "
            f"{redirect_uri!r}; (b) the source IP is not in the Upstox app's whitelist; "
            "(c) the client_id is wrong."
        )

    # Step 2: Generate OTP — Upstox sends SMS but also returns a validateOTPToken
    # we'll use for TOTP verification.
    try:
        resp = sess.post(
            _OTP_GENERATE_URL,
            json={"data": {"mobileNumber": mobile_number, "userId": user_id}},
            timeout=15,
        )
        otp_data = resp.json()
    except Exception as e:
        return _fail(f"step2 OTP generate: {e}")

    validate_token = otp_data.get("data", {}).get("validateOTPToken")
    is_totp_enabled = otp_data.get("data", {}).get("isTotpEnabled")
    if not validate_token:
        # Detect the "outdated app version" check Upstox started enforcing
        # 2026-05-13 (error code 1017072). The fix is to bump the
        # appVersion + userAgent claims in the headers block above.
        err = otp_data.get("error") if isinstance(otp_data, dict) else None
        if isinstance(err, dict) and err.get("code") == 1017072:
            return _fail(
                "Upstox is rejecting our request as an outdated app version. "
                "This is a header-version bump on Upstox's side — we need to "
                "update the appVersion strings in broker_login_adapters/upstox.py. "
                f"Upstox response: {err.get('message')}"
            )
        return _fail(f"step2 OTP generate: no validateOTPToken in response: {otp_data}")
    if is_totp_enabled is False:
        return _fail(
            "step2 OTP generate: TOTP is NOT enabled on this Upstox account — "
            "enable it at https://account.upstox.com/totp first"
        )

    # Step 3: Verify TOTP. Wait briefly if the current TOTP window is near
    # expiry so we don't race the verify call.
    remaining = 30 - (int(time.time()) % 30)
    if remaining < 5:
        time.sleep(remaining + 1)
    totp_code = pyotp.TOTP(totp_secret).now()
    try:
        resp = sess.post(
            _TOTP_VERIFY_URL,
            json={"data": {"otp": totp_code, "validateOtpToken": validate_token}},
            timeout=15,
        )
        verify_data = resp.json()
    except Exception as e:
        return _fail(f"step3 TOTP verify: {e}")

    if (not verify_data.get("success", True)) or verify_data.get("status") == "error":
        return _fail(f"step3 TOTP verify failed (wrong seed?): {verify_data}")

    # Step 4: Submit PIN (2FA). The response sometimes already contains the
    # auth code in a redirectUri — short-circuit if so.
    pin_b64 = base64.b64encode(pin.encode()).decode()
    try:
        resp = sess.post(
            _PIN_2FA_URL,
            params={"client_id": client_id, "redirect_uri": _INTERNAL_REDIRECT_URI},
            json={"data": {"twoFAMethod": "SECRET_PIN", "inputText": pin_b64}},
            allow_redirects=True,
            timeout=15,
        )
        pin_data = resp.json()
    except Exception as e:
        return _fail(f"step4 PIN submit: {e}")

    pin_success = pin_data.get("success", True)
    pin_error = pin_data.get("error", {}).get("message", "") if isinstance(pin_data.get("error"), dict) else ""
    if not pin_success or "expired" in (pin_error or "").lower():
        return _fail(f"step4 PIN verify failed: {pin_data}")

    pin_redirect = pin_data.get("data", {}).get("redirectUri", "")
    code = None
    if pin_redirect and "code=" in pin_redirect:
        code = parse_qs(urlparse(pin_redirect).query).get("code", [None])[0]

    # Step 5: If PIN didn't yield code directly, drive the OAuth-approve step.
    if not code:
        for cid, ruri in [
            (client_id, _INTERNAL_REDIRECT_URI),
            (api_key, _INTERNAL_REDIRECT_URI),
            (client_id, redirect_uri),
            (api_key, redirect_uri),
        ]:
            try:
                resp = sess.post(
                    _OAUTH_AUTHORIZE_URL,
                    params={
                        "client_id": cid,
                        "redirect_uri": ruri,
                        "requestId": request_id,
                        "response_type": "code",
                    },
                    json={"data": {"userOAuthApproval": True}},
                    allow_redirects=True,
                    timeout=15,
                )
                auth_data = resp.json()
            except Exception:
                continue
            auth_redirect = auth_data.get("data", {}).get("redirectUri", "")
            if not auth_redirect and "code=" in str(resp.url):
                auth_redirect = str(resp.url)
            if auth_redirect and "code=" in auth_redirect:
                code = parse_qs(urlparse(auth_redirect).query).get("code", [None])[0]
                if code:
                    break

    if not code:
        return _fail("step5 OAuth authorize: no auth code returned after all variant retries")

    # Step 6: Exchange code for access_token via the official documented API.
    # This part uses vanilla requests (no TLS fingerprinting needed — the
    # /v2/login/authorization/token endpoint accepts any TLS).
    import requests
    try:
        tok_resp = requests.post(
            _TOKEN_EXCHANGE_URL,
            headers={"accept": "application/json", "content-type": "application/x-www-form-urlencoded"},
            data={
                "code": code,
                "client_id": api_key,
                "client_secret": api_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=20,
        )
    except Exception as e:
        return _fail(f"step6 token exchange: {e}")

    if tok_resp.status_code != 200:
        return _fail(f"step6 token exchange: HTTP {tok_resp.status_code}: {tok_resp.text[:200]}")

    body = tok_resp.json()
    access_token = body.get("access_token")
    if not access_token:
        return _fail(f"step6 token exchange: no access_token in body: {body}")

    # Pull expiry from the JWT payload for the operator log line.
    expires_at = None
    try:
        import json
        from datetime import datetime, timedelta, timezone

        payload_b64 = access_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        expires_at = (exp + timedelta(hours=5, minutes=30)).isoformat()  # IST
    except Exception:
        pass

    return _ok(
        access_token=access_token,
        user_id=user_id,
        expires_at=expires_at,
    )
