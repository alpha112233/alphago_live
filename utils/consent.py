"""Consent gate — alphago_live fork addition.

Before placing a LIVE order, the container checks whether its AlphaQuark hosting
agreement has been signed, via a read-only, subdomain-scoped token issued by
hostingsol and injected at provision time:
    AQ_CONSENT_STATUS_URL    e.g. https://hostingsol.alphaquark.in/api/consent/status/self
    AQ_CONSENT_STATUS_TOKEN  read-only bearer scoped to THIS container's subdomain

FAIL-OPEN by design: `is_consent_blocked()` returns True (block) ONLY on a
definitive `{"signed": false}`. Any error, timeout, non-200, unparseable body, or
missing config → False (allow) — so a hostingsol outage never freezes trading.
Only NEW-ENTRY paths call this; exits (close position) are never gated.
"""

from __future__ import annotations

import logging

import requests
from cachetools import TTLCache

from utils.config import get_consent_status_token, get_consent_status_url

logger = logging.getLogger(__name__)

# Cache the signed result briefly so we don't hit hostingsol on every order.
# Once signed it stays signed (the record is immutable); a false result is
# re-checked after the TTL so the gate lifts within minutes of signing.
_cache: TTLCache = TTLCache(maxsize=1, ttl=300)

CONSENT_BLOCK_MESSAGE = (
    "Live trading is disabled until your AlphaQuark hosting agreement is signed. "
    "Please complete the agreement to place orders."
)


def _fetch_signed() -> bool | None:
    """True/False if hostingsol gives a definitive answer, else None (fail-open)."""
    url, token = get_consent_status_url(), get_consent_status_token()
    if not url or not token:
        return None
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=(2, 3))
        if r.status_code != 200:
            return None
        data = r.json()
        if isinstance(data, dict) and "signed" in data:
            return bool(data["signed"])
    except Exception as e:  # noqa: BLE001
        logger.debug("consent status check indeterminate (fail-open): %s", e)
    return None


def is_consent_blocked() -> bool:
    """Block a LIVE order ONLY when the agreement is definitively unsigned."""
    if not get_consent_status_url():
        return False  # gate not configured → disabled
    signed = _cache.get("signed")
    if signed is None:  # not cached / expired
        signed = _fetch_signed()
        if signed is not None:  # cache only definitive results
            _cache["signed"] = signed
    return signed is False
