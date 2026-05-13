# broker_login_adapters/indmoney.py
"""
Daemon "auto-login" for IndMoney — really just a token surfacer.

IndMoney does NOT expose an interactive OAuth login flow for their
developer API. Instead, the customer goes to their IndMoney developer
console and generates a long-lived access token (typically valid for
the entire fiscal year, or until they explicitly revoke it). Our
existing broker/indmoney/api/auth_api.py already treats
BROKER_API_SECRET as the access_token directly — no exchange needed.

For the auto-login adapter framework, all we have to do is honour the
same contract every other broker uses (`login(creds) -> {ok, access_token, ...}`)
and hand back the saved api_secret. This means:

  • The Auto Login button in the UI works for IndMoney — no error
    about "no adapter implemented", no manual /connect detour.
  • If the customer ever rotates the token in IndMoney's console, they
    just re-save it in our broker form and the next Auto Login uses
    the new value.
  • No TOTP seed needed (IndMoney has no MFA on the API).

Required creds keys (mapped by broker_credentials.py):
    api_secret:  the static access token from IndMoney's developer console
"""

from __future__ import annotations

from typing import Any


def _ok(access_token: str, **extra: Any) -> dict:
    return {"ok": True, "access_token": access_token, "error": None, **extra}


def _fail(error: str, **extra: Any) -> dict:
    return {"ok": False, "access_token": None, "error": error, **extra}


def login(creds: dict) -> dict:
    token = (creds.get("api_secret") or "").strip()
    if not token:
        return _fail(
            "IndMoney access token is missing. Open the IndMoney developer "
            "console, generate an access token, and paste it into the "
            "'Long-lived Access Token' field in Manage Brokers."
        )
    return _ok(access_token=token, user_id=None, feed_token=None, expires_at=None)
