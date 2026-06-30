# broker_login_adapters/iiflxts.py
"""Daily auto-login for IIFL XTS (Symphony).

IIFL XTS is fully headless — the session token is minted from the
Interactive App Key + Secret Key (no browser, no OTP). So the daily
pre-market scheduler can re-mint it every morning with zero customer
action, unlike IIFL Capital (browser OAuth). This is the whole reason
to use XTS over Capital.

Required creds keys (mapped by broker_credentials.py):
    api_key:            Interactive App Key   -> BROKER_API_KEY
    api_secret:         Interactive Secret    -> BROKER_API_SECRET
    api_key_market:     Market Data App Key   -> BROKER_API_KEY_MARKET   (optional for trading)
    api_secret_market:  Market Data Secret    -> BROKER_API_SECRET_MARKET

No TOTP seed needed — iiflxts is exempted from the scheduler's
has_totp_seed gate (see services/auto_login_scheduler_service.py).
"""

from __future__ import annotations

from typing import Any


def _ok(access_token: str, **extra: Any) -> dict:
    return {"ok": True, "access_token": access_token, "error": None, **extra}


def _fail(error: str, **extra: Any) -> dict:
    return {"ok": False, "access_token": None, "error": error, **extra}


def login(creds: dict) -> dict:
    app_key = (creds.get("api_key") or "").strip()
    secret = (creds.get("api_secret") or "").strip()
    mkt_key = (creds.get("api_key_market") or "").strip()
    mkt_sec = (creds.get("api_secret_market") or "").strip()
    if not app_key or not secret:
        return _fail(
            "IIFL XTS Interactive App Key/Secret missing — enter them in "
            "Manage Brokers (the Interactive app, not Market Data)."
        )

    from broker.iiflxts.baseurl import resolve_urls
    from broker.iiflxts.api.auth_api import _xts_error
    from utils.httpx_client import get_httpx_client

    client = get_httpx_client()
    headers = {"Content-Type": "application/json"}
    _, interactive_url, market_data_url = resolve_urls()

    # 1) Interactive (trading) session
    try:
        r = client.post(
            f"{interactive_url}/user/session",
            json={"appKey": app_key, "secretKey": secret, "source": "WebAPI"},
            headers=headers,
        )
    except Exception as e:
        return _fail(f"IIFL XTS login error: {str(e)[:200]}")
    if r.status_code != 200:
        try:
            body = r.json()
        except Exception:
            body = r.text[:300]
        return _fail(
            f"IIFL XTS login failed: {_xts_error(body, r.status_code)}. "
            "Register the dedicated IPv4 with IIFL and confirm these are XTS Interactive keys."
        )
    result = r.json()
    if result.get("type") != "success":
        return _fail(f"IIFL XTS login failed: {_xts_error(result, 200)}")
    token = (result.get("result") or {}).get("token")
    user_id = (result.get("result") or {}).get("userID")
    if not token:
        return _fail("IIFL XTS login returned no token.")

    # 2) Market-data feed token (best-effort — trading works without it)
    feed_token = None
    if mkt_key and mkt_sec:
        try:
            fr = client.post(
                f"{market_data_url}/auth/login",
                json={"appKey": mkt_key, "secretKey": mkt_sec, "source": "WebAPI"},
                headers=headers,
            )
            if fr.status_code == 200 and fr.json().get("type") == "success":
                feed_token = (fr.json().get("result") or {}).get("token")
        except Exception:
            pass

    return _ok(access_token=token, feed_token=feed_token, user_id=user_id, expires_at=None)
