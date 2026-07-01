"""IIFL XTS (Symphony) broker base URLs configuration.

The base URLs are PER-CUSTOMER: IIFL issues XTS API access on different hosts
(ttblaze.iifl.com, blazemum.indiainfoline.com, dealer-specific hosts…). TWO
optional URLs are supported because the Interactive (trading) and Market Data
hosts can differ:
  * "XTS API Base URL"        → broker_creds extra.base_url        → BROKER_XTS_BASE_URL
  * "XTS Market Data Base URL" → broker_creds extra.base_url_market → BROKER_XTS_MARKET_URL
Both are stamped into env by utils/broker_env_bootstrap. Market defaults to the
interactive base when unset. Blank → default host.
"""

import os

_DEFAULT_BASE_URL = "https://ttblaze.iifl.com"


def _base() -> str:
    """Interactive (trading) base URL."""
    return (os.getenv("BROKER_XTS_BASE_URL") or "").strip().rstrip("/") or _DEFAULT_BASE_URL


def _market_base() -> str:
    """Market-data base URL — separate optional host; defaults to interactive."""
    return (os.getenv("BROKER_XTS_MARKET_URL") or "").strip().rstrip("/") or _base()


def resolve_urls() -> tuple[str, str, str]:
    """(interactive_base, interactive_url, market-data_url) read FRESH from env
    each call — so per-customer base URLs set after this module is imported still
    take effect on the login path without a container restart."""
    ib = _base()
    return ib, f"{ib}/interactive", f"{_market_base()}/apimarketdata"


# Import-time constants (default, or env at startup) — used by order_api / data /
# streaming. For a custom host these are correct once the container starts with
# BROKER_XTS_BASE_URL / BROKER_XTS_MARKET_URL set (bootstrap stamps them from
# extra.base_url / extra.base_url_market). Changing the host on an already-running
# container needs a restart for these paths; the login path (auth_api) re-resolves
# dynamically so it works immediately.
BASE_URL = _base()
INTERACTIVE_URL = f"{BASE_URL}/interactive"
MARKET_DATA_URL = f"{_market_base()}/apimarketdata"
