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

from .aliceblue import login as aliceblue_login
from .arihant import login as arihant_login
from .dhan import login as dhan_login
from .flattrade import login as flattrade_login
from .fyers import login as fyers_login
from .groww import login as groww_login
from .indmoney import login as indmoney_login
from .kotak import login as kotak_login
from .upstox import login as upstox_login, precheck as upstox_precheck
from .zerodha import login as zerodha_login

ADAPTERS = {
    "upstox": upstox_login,
    "kotak": kotak_login,
    "zerodha": zerodha_login,
    "dhan": dhan_login,
    "fyers": fyers_login,
    "aliceblue": aliceblue_login,
    "groww": groww_login,
    "flattrade": flattrade_login,
    "indmoney": indmoney_login,
    # arihant: refuses with a clear message if any of the 3 hands-free
    # fields aren't set, so it's safe to register in the live registry.
    # The refresh-token chain (cheaper) is tried first by the scheduler;
    # this adapter only fires when the refresh-token has expired and the
    # customer has filled in user_id + password + totp_seed.
    "arihant": arihant_login,
}

# Adapters that exist in skeleton form but are NOT yet validated against a
# real broker login flow. DELIBERATELY kept out of ADAPTERS so the daily
# auto-login scheduler never calls them — a speculative/wrong login flow
# fired on a schedule risks locking out a real customer account.
#
# Each of these has a hard guard in its login() that returns _fail with a
# clear message. Promote a broker from here into ADAPTERS only after its
# in-module "Validation checklist" is complete (capture the real flow,
# replace ASSUMED_* constants, confirm a real round-trip). See:
#   broker_login_adapters/{icicidirect,hdfcsec,arihant}.py
from .hdfcsec import login as hdfcsec_login
from .icicidirect import login as icicidirect_login

_UNVERIFIED_ADAPTERS = {
    "icicidirect": icicidirect_login,
    "hdfcsec": hdfcsec_login,
}

# Optional cheap pre-save validators. Each returns {ok, error}. Used by
# /api/broker/credentials/save to catch obvious config errors at save time
# instead of letting the user discover them only at auto-login time.
# Only brokers where a real-network check is cheap AND doesn't consume
# state (TOTP windows, OTP SMS, rate-limit quota) belong here.
PRECHECKS = {
    "upstox": upstox_precheck,
}


def adapter_for(broker: str):
    """Return the login() callable for a broker, or None if not supported."""
    return ADAPTERS.get((broker or "").lower())


def precheck_for(broker: str):
    """Return the precheck() callable for a broker, or None if no cheap
    pre-save validation is available."""
    return PRECHECKS.get((broker or "").lower())
