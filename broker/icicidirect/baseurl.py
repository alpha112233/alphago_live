"""ICICI Direct Breeze API base URLs.

Production: api.icicidirect.com — AAAA records confirmed
(`2001:df3:140:1::b`, 2026-05-20), so the host is reachable from
hostingsol's IPv6-only egress.
"""
from __future__ import annotations

import os

BASE_URL = os.environ.get("ICICI_BASE_URL", "https://api.icicidirect.com").rstrip("/")
API_V1 = f"{BASE_URL}/breezeapi/api/v1"

# Browser login endpoint — used to build the OAuth redirect.
BREEZE_AUTH_URL = f"{BASE_URL}/apiuser/login"

# Per-resource endpoints. Mirrors the surface in
# ccxt-india/brokers/icici/icici.py.
ORDER_URL = f"{API_V1}/order"
TRADES_URL = f"{API_V1}/trades"
POSITIONS_URL = f"{API_V1}/portfoliopositions"
HOLDINGS_URL = f"{API_V1}/portfolioholdings"
FUNDS_URL = f"{API_V1}/funds"
CUSTOMER_URL = f"{API_V1}/customerdetails"
QUOTES_URL = f"{API_V1}/quotes"
GTT_URL = f"{API_V1}/gttorder"
GTT_BOOK_URL = f"{API_V1}/gttorderbook"
GTT_THREE_LEG_URL = f"{API_V1}/gttthreelegorder"
HIST_URL = f"{API_V1}/historicalcharts"


def get_url(path: str) -> str:
    if path.startswith("/"):
        return BASE_URL + path
    return BASE_URL + "/" + path
