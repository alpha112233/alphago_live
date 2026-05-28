# broker_login_adapters/arihant.py
"""Daemon auto-login for Arihant Capital (TradeBridge) — UNVERIFIED SKELETON.

⚠️⚠️ NOT validated against a real Arihant account. NOT in the live
ADAPTERS registry (see _UNVERIFIED_ADAPTERS).

IMPORTANT NUANCE — Arihant already has a daily-refresh path that does
NOT need this adapter:
    broker/arihant/api/auth_api.authenticate_broker(code) uses a stored
    REFRESH TOKEN (set once via the /broker/arihant/login OTP flow) to
    mint a fresh access token every day. That refresh-token path is the
    intended hands-free mechanism — NOT a TOTP web-login.

So this adapter is only relevant IF:
    (a) Arihant's refresh token expires (they rotate ~6 months), AND
    (b) the customer enabled TOTP-based 2FA (vs the default SMS OTP), AND
    (c) we want to re-mint the refresh token without a human completing
        the OTP page again.

If Arihant 2FA is SMS-only (likely the default), there is NO autologin —
the customer must redo the one-time OTP login at /broker/arihant/login
when the refresh token expires. In that case, delete this file.

KNOWN UNKNOWNS (confirm with a real account before use):
    1. Does Arihant TradeBridge support TOTP 2FA at all? (Default is
       SMS/email OTP — see broker/arihant/api/auth_api.login_initiate.)
    2. If TOTP: the login + verify-otp endpoints already exist in
       auth_api.py (login_initiate / verify_otp) — this adapter would
       just supply the TOTP code instead of a human-entered OTP.

Contract: login(creds) -> {ok, access_token, ...}.

Validation checklist: confirm TOTP support first. If yes, this adapter
can reuse broker/arihant/api/auth_api.login_initiate + verify_otp
directly (pass pyotp.TOTP(seed).now() as the otp), which is MUCH less
speculative than icicidirect/hdfcsec since those auth functions are
already real. Promote to ADAPTERS after one real-account round-trip.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _fail(error: str, **extra: Any) -> dict:
    return {"ok": False, "access_token": None, "error": error, **extra}


def login(creds: dict) -> dict:
    """UNVERIFIED. Arihant's primary hands-free path is the stored
    refresh-token (authenticate_broker), not TOTP web-login. This adapter
    only matters if TOTP 2FA is confirmed available + the refresh token
    has expired. Refuses to run until validated."""
    return _fail(
        "Arihant auto-login via TOTP is UNVERIFIED. The intended hands-free "
        "path is the stored refresh token (broker/arihant/api/auth_api). If "
        "that expires, redo the one-time OTP login at /broker/arihant/login. "
        "A TOTP-driven re-mint is only possible if Arihant 2FA supports TOTP "
        "(default is SMS OTP). See broker_login_adapters/arihant.py."
    )
