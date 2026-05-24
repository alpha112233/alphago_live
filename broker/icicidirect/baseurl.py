"""ICICI Direct Breeze API base URL.

Production: api.icicidirect.com (AAAA confirmed — IPv6 reachable from
hostingsol's egress).
"""
import os

BASE_URL = os.environ.get("ICICI_BASE_URL", "https://api.icicidirect.com").rstrip("/")
BREEZE_AUTH_URL = "https://api.icicidirect.com/apiuser/login"


def get_url(path: str) -> str:
    if path.startswith("/"):
        return BASE_URL + path
    return BASE_URL + "/" + path
