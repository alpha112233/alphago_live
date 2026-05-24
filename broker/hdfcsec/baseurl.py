"""HDFC Securities API base URL.

Production: developer.hdfcsec.com (AAAA confirmed via AWS ALB CNAME —
IPv6 reachable from hostingsol's egress).
"""
import os

BASE_URL = os.environ.get("HDFCSEC_BASE_URL", "https://developer.hdfcsec.com").rstrip("/")


def get_url(path: str) -> str:
    if path.startswith("/"):
        return BASE_URL + path
    return BASE_URL + "/" + path
