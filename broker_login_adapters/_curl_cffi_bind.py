# broker_login_adapters/_curl_cffi_bind.py
"""
Helper: bind a curl_cffi Session's outbound socket to the customer's
dedicated IPv6.

Why this exists:
    `utils/source_bind.py` monkeypatches `urllib3.connection.HTTPConnection`
    so that any urllib3-based HTTPS call (requests, httpx in sync mode)
    egresses from the customer's CLIENT_IPV6. But curl_cffi wraps libcurl
    directly — it does NOT go through urllib3. So Upstox / Zerodha / Fyers,
    which all need curl_cffi's Chrome TLS impersonation to get past the
    broker's TLS fingerprinting, would silently egress from the container's
    default IP (typically the unrouted Hostinger /48), bypassing the
    per-customer source bind.

    The visible effect is that brokers which whitelist API access by IP
    (Upstox, Dhan, Fyers) reject the login because we're not coming from
    the IP the customer added to their whitelist. Upstox surfaces this as
    error 1017072 "This version is outdated" — a misleading message that
    cost a few hours of debugging.

What this does:
    CURLOPT_INTERFACE accepts an IP literal as the bind source. Passing
    CLIENT_IPV6 makes libcurl bind() the outbound socket to that source
    address, so the broker's edge sees the right IP.

Usage:
    sess = Session(impersonate="chrome131")
    bind_to_client_ipv6(sess)   # no-op if CLIENT_IPV6 isn't set
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def get_client_ipv6() -> str:
    """Return the customer's dedicated IPv6, or '' if not configured."""
    return (os.getenv("CLIENT_IPV6") or "").strip()


def bind_to_client_ipv6(sess: Any) -> str:
    """Bind a curl_cffi Session's outbound socket to CLIENT_IPV6.

    Returns the IPv6 that was bound (empty string if no binding done).
    Failures are logged but non-fatal — caller still gets a working
    Session, just with default egress IP.
    """
    ipv6 = get_client_ipv6()
    if not ipv6:
        return ""

    try:
        from curl_cffi import CurlOpt
        # CURLOPT_INTERFACE accepts a bytes literal of an IP address;
        # libcurl resolves it via getifaddrs() and bind()s the socket.
        sess.curl.setopt(CurlOpt.INTERFACE, ipv6.encode("ascii"))
    except Exception as e:
        logger.warning(
            f"could not bind curl_cffi session to CLIENT_IPV6={ipv6!r} ({e}); "
            f"this request will egress from the container's default IP, which "
            f"is likely NOT on the broker's whitelist."
        )
        return ""
    return ipv6
