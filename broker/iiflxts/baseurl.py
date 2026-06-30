"""IIFL XTS (Symphony) broker base URLs configuration.

The base URL is PER-CUSTOMER: IIFL issues XTS API access on different hosts
(commonly ttblaze.iifl.com, but some dealers get a different host). The
customer can set their own via the optional "XTS API Base URL" field in Manage
Brokers — it's stored in broker_creds extra.base_url and surfaced here as the
`BROKER_XTS_BASE_URL` env var by utils/broker_env_bootstrap. Blank → default.
"""

import os

_DEFAULT_BASE_URL = "https://ttblaze.iifl.com"


def _base() -> str:
    return (os.getenv("BROKER_XTS_BASE_URL") or "").strip().rstrip("/") or _DEFAULT_BASE_URL


def resolve_urls() -> tuple[str, str, str]:
    """(base, interactive, market-data) read FRESH from env each call — so a
    per-customer base URL set after this module is imported still takes effect
    on the login path without a container restart."""
    b = _base()
    return b, f"{b}/interactive", f"{b}/apimarketdata"


# Import-time constants (default, or env at startup) — used by order_api / data /
# streaming. For a custom host these are correct once the container starts with
# BROKER_XTS_BASE_URL set (bootstrap stamps it from extra.base_url). Changing the
# host on an already-running container needs a restart for these paths; the login
# path (auth_api) re-resolves dynamically so it works immediately.
BASE_URL = _base()
MARKET_DATA_URL = f"{BASE_URL}/apimarketdata"
INTERACTIVE_URL = f"{BASE_URL}/interactive"
