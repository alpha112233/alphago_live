# broker_login_adapters/__init__.py
"""
Per-broker daemon auto-login adapters (alphago_live fork).

Each module exposes a single `login(creds: dict) -> dict` callable that drives
the broker's daily 2FA flow using a stored TOTP seed and returns an
`access_token` + metadata. Callers (REST endpoints, schedulers) pass the
decrypted credentials in; the adapter does NOT read storage directly.

Why these exist:
    Upstox / Zerodha / Fyers / Kotak all issue daily-rotating access_tokens.
    Their official OAuth flows require human browser interaction. But the
    web 2FA flow they expose CAN be driven programmatically with
    `curl_cffi` (Chrome TLS fingerprint) + `pyotp`, given the customer
    saved their TOTP seed. This is the same pattern alpha_live uses in
    production (see `live/broker/upstox.py::_http_login`).

Contract:
    login(creds) -> {
        "ok": bool,
        "access_token": str or None,
        "feed_token": str or None,         # broker-specific, optional
        "user_id": str or None,             # broker's user identifier
        "expires_at": ISO timestamp str or None,
        "error": str or None,
    }
"""

from .kotak import login as kotak_login
from .upstox import login as upstox_login

ADAPTERS = {
    "upstox": upstox_login,
    "kotak": kotak_login,
}


def adapter_for(broker: str):
    """Return the login() callable for a broker, or None if not supported."""
    return ADAPTERS.get((broker or "").lower())
