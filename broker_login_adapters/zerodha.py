# broker_login_adapters/zerodha.py
"""
Daemon auto-login for Zerodha (Kite Connect) — drives the kite.zerodha.com
web 2FA flow with a saved TOTP seed.

Zerodha intentionally does NOT expose a programmatic OAuth login API — the
official Kite Connect docs say "the user MUST visit the login page". To
automate the daily request_token rotation (which happens at ~06:00 IST),
we drive Zerodha's normal web 2FA flow the same way a browser would:

    1. POST kite.zerodha.com/api/login   (user_id + password)
        → returns request_id
    2. POST kite.zerodha.com/api/twofa   (request_id + 6-digit TOTP)
        → session cookies set
    3. GET  kite.zerodha.com/connect/login?api_key=...&v=3
        → 302 chain → final redirect to <our-app-redirect_uri>?request_token=...
    4. POST api.kite.trade/session/token (sha256(api_key+request_token+api_secret))
        → returns access_token

This is the same flow the official Kite Connect docs describe for human
users — we just script the form submissions. The TOTP seed is what the
Kite "External 2FA" QR code encodes (Settings → Account → External 2FA →
"Can't scan? Reveal secret").

Required creds keys:
    api_key      — Kite Connect app's API key
    api_secret   — Kite Connect app's API secret
    redirect_uri — registered redirect URI (must match Kite app exactly)
    mobile_number — IGNORED for Zerodha (we use user_id / kite_id instead)
    pin          — IGNORED for Zerodha
    totp_secret  — base32 TOTP seed
    extra fields used:
      user_id (string)  — Kite user ID, e.g., "ABC123"
      password (string) — Kite trading password (NOT the trading PIN)

Field mapping in our broker_metadata.py: for Zerodha,
    client_code → user_id
    extra.password → password
    totp_seed → totp_secret
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

_KITE_LOGIN_URL = "https://kite.zerodha.com/api/login"
_KITE_TWOFA_URL = "https://kite.zerodha.com/api/twofa"
_KITE_CONNECT_LOGIN_URL = "https://kite.zerodha.com/connect/login"
_KITE_SESSION_TOKEN_URL = "https://api.kite.trade/session/token"


def _ok(access_token: str, **extra: Any) -> dict:
    return {"ok": True, "access_token": access_token, "error": None, **extra}


def _fail(error: str, **extra: Any) -> dict:
    return {"ok": False, "access_token": None, "error": error, **extra}


def login(creds: dict) -> dict:
    """Drive the Zerodha web 2FA flow + token exchange.

    Returns the contract dict described in __init__.py. On success, the
    `access_token` field holds the Kite Connect access_token (valid until
    ~06:00 IST the next day).
    """
    try:
        from curl_cffi.requests import Session as CffiSession
    except ImportError:
        return _fail("curl_cffi is required for Zerodha auto-login (pip install curl_cffi)")

    try:
        import pyotp
    except ImportError:
        return _fail("pyotp is required for Zerodha auto-login (pip install pyotp)")

    required = ("api_key", "api_secret", "user_id", "password", "totp_secret")
    missing = [k for k in required if not creds.get(k)]
    if missing:
        return _fail(f"missing required credentials: {missing}")

    api_key = creds["api_key"]
    api_secret = creds["api_secret"]
    user_id = creds["user_id"]
    password = creds["password"]
    totp_secret = creds["totp_secret"]

    # curl_cffi with Chrome impersonation — Zerodha doesn't appear to enforce
    # this as strictly as Upstox, but using it avoids any TLS-fingerprint
    # surprises down the line.
    sess = CffiSession(impersonate="chrome131")
    # Bind to CLIENT_IPV6 — utils/source_bind only patches urllib3, so
    # curl_cffi otherwise egresses via the container default IP. Zerodha
    # doesn't enforce IP whitelisting per app, but binding here keeps the
    # outbound consistent with what the customer added to other brokers'
    # whitelists (and what Zerodha's audit log will show).
    from ._curl_cffi_bind import bind_to_client_ipv6
    bind_to_client_ipv6(sess)
    sess.headers.update({
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        ),
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-GB,en;q=0.9",
        "origin": "https://kite.zerodha.com",
        "referer": "https://kite.zerodha.com/",
    })

    # Step 1: POST /api/login with user_id + password → returns request_id.
    try:
        r1 = sess.post(
            _KITE_LOGIN_URL,
            data={"user_id": user_id, "password": password},
            timeout=15,
        )
        body1 = r1.json()
    except Exception as e:
        return _fail(f"step1 /api/login: network or parse error: {e}")

    if body1.get("status") != "success":
        msg = body1.get("message") or body1.get("error_type") or str(body1)[:200]
        return _fail(f"step1 /api/login rejected (wrong user_id or password?): {msg}")

    request_id = (body1.get("data") or {}).get("request_id")
    if not request_id:
        return _fail(f"step1 /api/login: response missing request_id: {body1}")

    # Step 2: POST /api/twofa with the TOTP code. Wait for next window if
    # current one is near expiry so the verify side doesn't reject a code
    # that ages out in-flight.
    remaining = 30 - (int(time.time()) % 30)
    if remaining < 5:
        time.sleep(remaining + 1)
    totp_code = pyotp.TOTP(totp_secret).now()

    try:
        r2 = sess.post(
            _KITE_TWOFA_URL,
            data={
                "user_id": user_id,
                "request_id": request_id,
                "twofa_value": totp_code,
                "twofa_type": "totp",
                "skip_session": "true",
            },
            timeout=15,
        )
        body2 = r2.json()
    except Exception as e:
        return _fail(f"step2 /api/twofa: network or parse error: {e}")

    if body2.get("status") != "success":
        msg = body2.get("message") or body2.get("error_type") or str(body2)[:200]
        return _fail(f"step2 /api/twofa rejected (wrong TOTP seed?): {msg}")

    # Step 3: GET /connect/login?api_key=... enters Kite's OAuth 302 chain,
    # which terminates at OUR app's redirect_uri with a request_token= query
    # param (chain: /connect/login -> /connect/finish -> <redirect_uri>?request_token=).
    #
    # We MUST NOT let curl_cffi auto-follow the whole chain. This adapter's
    # session carries no Flask login cookie, so following the final 302 into
    # our own /<broker>/callback makes the app answer "user not in session,
    # redirecting to login" and curl_cffi keeps following into the authenticated
    # UI, where a hop stalls for the full timeout — surfacing as
    # `curl (28) Operation timed out ... 0 bytes received` and the request_token
    # is never read (root cause of anantswain's never-succeeding Zerodha
    # auto-login, 2026-07-13). Instead we follow the chain by hand and STOP the
    # instant a hop redirects to our redirect_uri, harvesting request_token
    # straight off the Location header. Bonus: request_token is single-use, so
    # not GETting our own callback keeps it unconsumed for step 4's exchange.
    request_token = None
    status = None
    final_url = _KITE_CONNECT_LOGIN_URL
    hop_url = f"{_KITE_CONNECT_LOGIN_URL}?api_key={api_key}&v=3"
    try:
        for _ in range(10):
            r3 = sess.get(hop_url, allow_redirects=False, timeout=15)
            loc = r3.headers.get("location") or r3.headers.get("Location")
            # request_token / status=error can show up on the URL we just
            # fetched OR on the Location we're about to follow — check both,
            # preferring the Location so we stop BEFORE touching our callback.
            for candidate in (loc, hop_url):
                if not candidate:
                    continue
                cq = parse_qs(urlparse(candidate).query)
                if cq.get("request_token", [None])[0]:
                    request_token = cq["request_token"][0]
                    status = cq.get("status", [None])[0]
                    final_url = candidate
                    break
                if cq.get("status", [None])[0] == "error":
                    status = "error"
                    final_url = candidate
                    break
            if request_token or status == "error":
                break
            if not loc:
                final_url = hop_url
                break
            if loc.startswith("/"):
                p = urlparse(hop_url)
                loc = f"{p.scheme}://{p.netloc}{loc}"
            hop_url = loc
    except Exception as e:
        return _fail(f"step3 /connect/login redirect chain: {e}")

    qs = parse_qs(urlparse(final_url).query)

    if not request_token:
        # Surface what Kite returned so the customer can act on it.
        # Common failures: api_key mismatch, redirect_uri not registered
        # for this app, account temporarily locked.
        if status == "error":
            msg = qs.get("error_type", ["unknown"])[0]
            return _fail(
                f"step3 /connect/login rejected: {msg}. "
                f"Verify the redirect URI registered in your Kite Connect app at "
                f"https://developers.kite.trade/apps EXACTLY matches the one this instance uses, "
                f"and that your Kite Connect subscription is active."
            )
        return _fail(
            f"step3 /connect/login: no request_token in final URL ({final_url[:200]}). "
            f"Most common cause: the Kite app's redirect URI doesn't match our redirect URI, "
            f"or the app is missing an active Kite Connect subscription."
        )

    # Step 4: Exchange request_token for access_token via the official API.
    # This is the same SHA-256 checksum dance the existing Zerodha broker
    # plugin does (see broker/zerodha/api/auth_api.py). Plain requests is
    # fine here — TLS fingerprinting doesn't apply on api.kite.trade.
    import requests
    checksum = hashlib.sha256(f"{api_key}{request_token}{api_secret}".encode()).hexdigest()
    try:
        tok = requests.post(
            _KITE_SESSION_TOKEN_URL,
            headers={"X-Kite-Version": "3"},
            data={"api_key": api_key, "request_token": request_token, "checksum": checksum},
            timeout=20,
        )
    except Exception as e:
        return _fail(f"step4 /session/token: network error: {e}")

    if tok.status_code != 200:
        # Surface Kite's error message verbatim.
        msg = tok.text[:300]
        try:
            j = tok.json()
            msg = j.get("message") or msg
        except Exception:
            pass
        return _fail(f"step4 /session/token HTTP {tok.status_code}: {msg}")

    body = tok.json()
    data = body.get("data") or {}
    access_token = data.get("access_token")
    if not access_token:
        return _fail(f"step4 /session/token: no access_token in body: {body}")

    return _ok(
        access_token=access_token,
        user_id=data.get("user_id") or user_id,
        feed_token=None,
        expires_at=None,  # Kite tokens expire at ~06:00 IST next day; not in response
    )
