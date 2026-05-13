# broker_login_adapters/groww.py
"""
Daemon auto-login for Groww — TOTP-mode access-token mint.

Groww's /v1/token/api/access endpoint has two key_type modes:

    key_type="approval"
        Customer pastes a fresh approval token from groww.in each day.
        Server computes sha256(api_secret + timestamp) as `checksum`.
        Already supported in broker/groww/api/auth_api.py for the
        manual /connect flow — it's the default OpenAlgo path.

    key_type="totp"
        Customer saves their Base32 TOTP seed ONCE (the secret shown
        under the QR code in Groww's "Generate TOTP token" dialog, NOT
        the JWT-style TOTP token displayed above it). Server computes
        pyotp.TOTP(seed).now() at exchange time. No daily customer action.
        This adapter is what makes that work — alpha live's b2b mobile
        app and ccxt-india's /groww/refresh-token use the same shape.

Request:
    POST https://api.groww.in/v1/token/api/access
    Authorization: Bearer <api_key>
    Content-Type: application/json
    {"key_type": "totp", "totp": "<6-digit>"}

Response on success:
    200 {"token": "<jwt access_token>", ...}     ~20h validity

Required creds keys (mapped by broker_credentials.py):
    api_key:      Groww API Key (from "Generate TOTP token" dialog)
    totp_secret:  Base32 TOTP seed (from same dialog, under the QR)

Optional creds keys (ignored in TOTP mode — kept for unified shape):
    api_secret, redirect_uri, user_id, password
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://api.groww.in/v1/token/api/access"
_BASE32_ALPHABET = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")


def _ok(access_token: str, **extra: Any) -> dict:
    return {"ok": True, "access_token": access_token, "error": None, **extra}


def _fail(error: str, **extra: Any) -> dict:
    return {"ok": False, "access_token": None, "error": error, **extra}


def _normalize_totp_seed(raw: str) -> tuple[str | None, str | None]:
    """Accept the many shapes a user can paste from Groww's 'Generate
    TOTP token' dialog and return a pyotp-ready Base32 seed.

    Returns (seed, None) on success, (None, error_msg) on failure.
    Mirrors ccxt-india's app_groww._normalize_totp_token so a customer
    can't get past the form by pasting the wrong field.
    """
    if not raw:
        return None, "TOTP seed is missing — copy the Base32 secret from Groww's 'Generate TOTP token' dialog."

    token = str(raw).strip()

    # otpauth://totp/Groww:user@example.com?secret=XYZ&issuer=Groww — users
    # who scan the QR with a generic scanner and paste the URL back in.
    if token.lower().startswith("otpauth://"):
        try:
            parsed = urlparse(token)
            secret_values = parse_qs(parsed.query).get("secret", [])
            if not secret_values or not secret_values[0]:
                return None, "otpauth:// URL has no 'secret' parameter."
            token = secret_values[0]
        except Exception as e:
            return None, f"Could not parse otpauth:// URL: {e}"

    token = re.sub(r"[\s_-]", "", token).upper()
    if not token:
        return None, "TOTP seed is empty after cleanup."

    bad = [c for c in token if c not in _BASE32_ALPHABET and c != "="]
    if bad:
        return None, (
            "TOTP seed has non-Base32 characters. Copy the Base32 secret shown "
            "under the QR code (NOT the JWT-style 'TOTP Token' at the top)."
        )

    stripped = token.rstrip("=")
    if len(stripped) < 16:
        return None, "TOTP seed is too short — Groww's secrets are at least 16 chars."

    remainder = len(stripped) % 8
    token = stripped + ("=" * (8 - remainder) if remainder else "")
    return token, None


def login(creds: dict) -> dict:
    """Mint a fresh Groww access_token via TOTP-mode."""
    try:
        import pyotp
    except ImportError:
        return _fail("pyotp is required for Groww auto-login (pip install pyotp)")

    try:
        import requests
    except ImportError:
        return _fail("requests is required (base dep)")

    api_key = (creds.get("api_key") or "").strip()
    totp_secret_raw = creds.get("totp_secret") or ""
    if not api_key:
        return _fail("missing api_key (Groww API Key)")
    if not totp_secret_raw:
        return _fail("missing totp_secret (Groww Base32 TOTP seed)")

    seed, seed_err = _normalize_totp_seed(totp_secret_raw)
    if seed_err is not None:
        return _fail(seed_err)

    # Avoid minting right at the 30s window edge — a code generated at t=29
    # may be invalid by the time it arrives at Groww.
    remaining = 30 - (int(time.time()) % 30)
    if remaining < 5:
        time.sleep(remaining + 1)

    try:
        totp_code = pyotp.TOTP(seed).now()
    except Exception as e:
        return _fail(f"pyotp could not read the TOTP seed: {e}")

    try:
        r = requests.post(
            _TOKEN_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={"key_type": "totp", "totp": totp_code},
            timeout=20,
        )
    except Exception as e:
        return _fail(f"Groww /v1/token/api/access request failed: {e}")

    try:
        body = r.json()
    except Exception:
        return _fail(f"Groww returned non-JSON (status {r.status_code}): {r.text[:200]}")

    if r.status_code != 200:
        # Surface Groww's own error message — it usually pinpoints what's
        # wrong (e.g. "Invalid TOTP", "IP not whitelisted", "API key revoked").
        err_payload = body.get("error") if isinstance(body, dict) else None
        msg = (
            (err_payload or {}).get("displayMessage")
            or (err_payload or {}).get("message")
            or body.get("message") if isinstance(body, dict) else None
        ) or str(body)[:200]
        return _fail(f"Groww rejected the credentials (HTTP {r.status_code}): {msg}")

    access_token = (body.get("token") if isinstance(body, dict) else None) or ""
    if not access_token:
        return _fail(f"Groww 200 OK but no token in response: {body}")

    return _ok(
        access_token=access_token,
        user_id=None,
        feed_token=None,
        expires_at=None,
    )
