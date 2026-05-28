# broker_login_adapters/hdfcsec.py
"""Daemon auto-login for HDFC Securities (InvestRight) — UNVERIFIED SKELETON.

⚠️⚠️ NOT validated against a real HDFC InvestRight account. NOT in the
live ADAPTERS registry (see _UNVERIFIED_ADAPTERS). Doubly blocked:

  1. UNVERIFIED — the InvestRight web-login form mechanics are
     uncaptured (same situation as icicidirect.py).
  2. INFRA-BLOCKED — developer.hdfcsec.com is IPv4-only (AWS ALB, no
     AAAA). hostingsol customers can't reach it at all until the
     per-customer IPv4 egress project lands (hostingsol/docs/IPV4_EGRESS_GAPS.md).
     So even a correct adapter is useless on hostingsol today; only
     dual-stack operators could use it.

Background:
    HDFC InvestRight uses an OAuth2 redirect: the customer hits
    developer.hdfcsec.com/oapi/v1/login?api_key=<key>, completes login +
    2FA, and the callback returns ?request_token=... which the plugin
    exchanges for a 24h accessToken (see broker/hdfcsec/api/auth_api.py).

KNOWN UNKNOWNS (confirm with a real account + dev tools before use):
    1. Does InvestRight 2FA support TOTP, or SMS/email OTP only?
    2. The login form POST endpoint + field names.
    3. The 2FA POST endpoint + field names.
    4. Whether request_token comes back as a redirect query param.

Contract: login(creds) -> {ok, access_token, ...}. On success
`access_token` would hold the request_token (which the plugin then
exchanges) OR the final accessToken — to be decided at validation time
based on what the captured flow actually yields.

Validation checklist: same shape as icicidirect.py. Additionally,
confirm IPv4 reachability is solved (or run only on a dual-stack box)
before promoting to ADAPTERS.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _fail(error: str, **extra: Any) -> dict:
    return {"ok": False, "access_token": None, "error": error, **extra}


def login(creds: dict) -> dict:
    """UNVERIFIED + INFRA-BLOCKED. Refuses to run until both are resolved."""
    return _fail(
        "HDFC Securities auto-login is UNVERIFIED (login flow uncaptured) AND "
        "infra-blocked (developer.hdfcsec.com is IPv4-only, unreachable from "
        "hostingsol's IPv6 egress). Use the daily Connect-button OAuth click on "
        "a dual-stack network for now. See broker_login_adapters/hdfcsec.py + "
        "hostingsol/docs/IPV4_EGRESS_GAPS.md."
    )
