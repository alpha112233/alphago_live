"""Decodo / IPv4 egress routing for IPv4-only broker APIs.

THE PROBLEM
-----------
A handful of broker APIs (Arihant TradeBridge today; HDFC InvestRight,
m.stock, Motilal, etc. potentially later) don't publish AAAA records —
their hosts are IPv4-only. The per-customer IPv6 (`CLIENT_IPV6`) we
bind for every other broker is unreachable to those, and falling back
to the host's shared v4 means every customer egresses from one address
(see hostingsol/docs/IPV4_EGRESS_GAPS.md, 2026-05-25 architecture note).

THE FIX (Phase 7, 2026-06-05)
-----------------------------
We route IPv4-only broker calls through a per-customer Decodo ISP proxy
(Vodafone Idea ASN, Mumbai-located). Each customer gets a static
dedicated /32 from our Decodo pool. Brokers see a clean residential-grade
Indian IP that the customer has whitelisted in the broker portal.

For resilience: super-admins can assign a SECONDARY Decodo (or other)
IP per customer. When the primary fails (connection error, timeout),
outbound transparently fails over to the secondary. Customer dashboards
show BOTH IPs so they whitelist both at the broker — failover is invisible
to them.

ENV VARS
--------
  EGRESS_V4_PROXY_PRIMARY    Full proxy URL with creds:
                             http://USER:PASS@isp.decodo.com:10222
                             Each Decodo IP has its own port (Direct IP
                             mode in the Decodo dashboard).
  EGRESS_V4_PROXY_SECONDARY  Optional second proxy URL with creds.
                             When set, failover engages on primary errors.
  EGRESS_V4_PRIMARY_IP       Just the IP (no creds). Used for dashboard
                             display + broker-whitelist guidance. Not
                             used for routing.
  EGRESS_V4_SECONDARY_IP     Same, for the secondary.
  EGRESS_V4_HOSTS            Comma-separated list of hostnames that
                             should route via Decodo. Defaults to the
                             v4-only broker hosts in IPV4_EGRESS_GAPS.md.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# Brokers whose API hosts are IPv4-only (no AAAA records). These get
# routed via Decodo. Keep in sync with hostingsol/docs/IPV4_EGRESS_GAPS.md.
# Lowercase for case-insensitive matching.
DEFAULT_V4_HOSTS = {
    "tradebridge.arihantplus.com",       # Arihant TradeBridge
    "developer.hdfcsec.com",             # HDFC InvestRight
    "api.mstock.trade",                  # m.stock
    "openapi.motilaloswal.com",          # Motilal Oswal
    "ttblaze.compositedge.com",          # Compositedge
    "api.shoonya.com",                   # Shoonya / Finvasia
    "openapi.firstock.in",               # Firstock
    "api.tradejini.com",                 # Tradejini
    "trade.pocketful.in",                # Pocketful
    "wcapital.in",                       # Wisdom
    "go.mynt.in",                        # Zebu
    "api.stocknote.com",                 # Samco
}


# Broker-name → does this broker need a v4 IP allocated. Used by
# broker_credentials.py to decide whether to gate activation on a
# dedicated v4 IP being assigned (Phase 7.6 allocate-on-demand).
# Keep the lowercase names aligned with broker_creds_db.broker values.
V4_ONLY_BROKERS = {
    "arihant",
    "hdfcsec",
    "mstock",
    "motilaloswal",
    "compositedge",
    "shoonya",
    "firstock",
    "tradejini",
    "pocketful",
    "wisdom",
    "zebu",
    "samco",
}


def broker_needs_v4(broker: str) -> bool:
    return (broker or "").strip().lower() in V4_ONLY_BROKERS


def _hosts_set() -> set[str]:
    extra = (os.getenv("EGRESS_V4_HOSTS") or "").strip()
    if not extra:
        return DEFAULT_V4_HOSTS
    return DEFAULT_V4_HOSTS | {h.strip().lower() for h in extra.split(",") if h.strip()}


def needs_v4_proxy(url_or_host: str) -> bool:
    """Return True if the URL or hostname is one of the IPv4-only broker
    hosts we route via Decodo."""
    if not url_or_host:
        return False
    host = url_or_host.lower()
    if "://" in host:
        # crude URL → host extraction (avoids importing urlparse for hot path)
        host = host.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]
    return host in _hosts_set()


def primary_proxy_url() -> Optional[str]:
    """Full proxy URL (with auth) for the customer's primary Decodo IP.
    None if not configured — caller should fall back to direct egress."""
    return (os.getenv("EGRESS_V4_PROXY_PRIMARY") or "").strip() or None


def secondary_proxy_url() -> Optional[str]:
    """Full proxy URL (with auth) for the customer's optional secondary
    Decodo IP. None if not configured — failover is disabled."""
    return (os.getenv("EGRESS_V4_PROXY_SECONDARY") or "").strip() or None


def primary_ip() -> str:
    """Just the IP — for dashboard display. Empty string if not configured."""
    return (os.getenv("EGRESS_V4_PRIMARY_IP") or "").strip()


def secondary_ip() -> str:
    return (os.getenv("EGRESS_V4_SECONDARY_IP") or "").strip()


class FailoverProxyTransport(httpx.BaseTransport):
    """httpx Transport that tries `primary` first and falls back to
    `secondary` on connection errors / timeouts. If only `primary` is
    set, behaves as a single-transport proxy.

    Used by `httpx_client._create_http_client()` for outbound broker
    calls to v4-only hosts."""

    _RETRYABLE = (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.PoolTimeout,
        httpx.RemoteProtocolError,
    )

    def __init__(self, primary: httpx.BaseTransport, secondary: Optional[httpx.BaseTransport] = None):
        self._primary = primary
        self._secondary = secondary

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            return self._primary.handle_request(request)
        except self._RETRYABLE as e:
            if self._secondary is None:
                raise
            logger.warning(
                f"decodo_proxy: primary failed ({type(e).__name__}: {e}) — "
                f"failing over to secondary for {request.method} {request.url}"
            )
            return self._secondary.handle_request(request)

    def close(self) -> None:
        try:
            self._primary.close()
        finally:
            if self._secondary is not None:
                self._secondary.close()


def build_v4_transport(http2: bool, limits: httpx.Limits) -> Optional[httpx.BaseTransport]:
    """Build the httpx Transport that routes through Decodo with optional
    failover. Returns None if no primary is configured (caller falls back
    to direct egress)."""
    primary = primary_proxy_url()
    if not primary:
        return None
    secondary = secondary_proxy_url()

    primary_tr = httpx.HTTPTransport(
        proxy=primary,
        http2=http2, http1=True,
        limits=limits, verify=True, retries=0,
    )
    if not secondary:
        logger.info("decodo_proxy: primary-only mode (no secondary configured)")
        return primary_tr

    secondary_tr = httpx.HTTPTransport(
        proxy=secondary,
        http2=http2, http1=True,
        limits=limits, verify=True, retries=0,
    )
    logger.info("decodo_proxy: failover mode (primary + secondary)")
    return FailoverProxyTransport(primary_tr, secondary_tr)


def build_mounts(http2: bool, limits: httpx.Limits) -> dict[str, httpx.BaseTransport]:
    """Build the `mounts` dict for httpx.Client — one entry per IPv4-only
    broker host, all pointing to the failover transport. Empty dict when
    no proxy is configured."""
    tr = build_v4_transport(http2=http2, limits=limits)
    if tr is None:
        return {}
    mounts: dict[str, httpx.BaseTransport] = {}
    for host in _hosts_set():
        mounts[f"http://{host}"] = tr
        mounts[f"https://{host}"] = tr
    return mounts
