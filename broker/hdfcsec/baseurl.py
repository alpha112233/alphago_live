"""HDFC Securities (InvestRight) API base URLs.

Production: developer.hdfcsec.com — AAAA via AWS ALB CNAME, confirmed
2026-05-20. IPv6 reachable from hostingsol's egress.

All endpoints live under `/oapi/v1/`. Authentication is OAuth2 redirect:
the customer is sent to `/oapi/v1/login?api_key=...` and the callback
returns a `request_token` that is exchanged at `/oapi/v1/access-token`
for a 24-hour `accessToken`.
"""
from __future__ import annotations

import os

BASE_URL = os.environ.get("HDFCSEC_BASE_URL", "https://developer.hdfcsec.com").rstrip("/")
API_V1 = f"{BASE_URL}/oapi/v1"

# Browser login endpoint — start of the daily OAuth click.
HDFC_LOGIN_URL = f"{API_V1}/login"
ACCESS_TOKEN_URL = f"{API_V1}/access-token"

# Trading endpoints.
ORDER_PLACE_URL = f"{API_V1}/orders/regular"
ORDER_BY_ID_URL = f"{API_V1}/orders/{{order_id}}"        # PUT modify/cancel, GET status
ORDER_BOOK_URL = f"{API_V1}/orders"
TRADE_BOOK_URL = f"{API_V1}/trades"
HOLDINGS_URL = f"{API_V1}/portfolio/holdings"
POSITIONS_URL = f"{API_V1}/portfolio/cumulative-positions"
FUNDS_URL = f"{API_V1}/user/margins"

# Symbol master (CSV).
SECURITY_MASTER_URL = f"{API_V1}/security-master"


def get_url(path: str) -> str:
    if path.startswith("/"):
        return BASE_URL + path
    return BASE_URL + "/" + path
