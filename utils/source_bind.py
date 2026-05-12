# utils/source_bind.py
"""
Bind outbound HTTPS to the per-client IPv6 (alphago_live fork addition).

Reads `CLIENT_IPV6` from the environment. If set, monkeypatches urllib3's
HTTPConnection so every outbound TCP socket binds to that source address
before connect(). This makes brokers see the customer's whitelisted IPv6
rather than the host's default IPv4.

Pattern lifted from ccxt-india/common/egress_proxy.py's direct-bind
transport, minus the per-request ContextVar machinery — alphago_live runs
one container per client, so the source IP is constant for the lifetime
of the process.

IMPORTANT: must be imported VERY early in app.py — before any broker
module imports requests.Session, httpx.Client, or anything that builds
a urllib3 PoolManager. Otherwise pools created pre-patch keep using the
default source.

No-op when CLIENT_IPV6 is unset. Dev runs of alphago_live behave
identically to upstream OpenAlgo.

Routing prerequisite: the container must be able to actually reach the
internet via the assigned IPv6. On tidi that means:
  - `network_mode: host` in docker-compose (so the container shares the
    host's network namespace and can use the GRE tunnel directly), OR
  - a docker network with IPv6 enabled + the /128 bound to the container

Without either, bind(source_address) will fail at connect time because
the IPv6 isn't a local interface inside the container's namespace.
"""

from __future__ import annotations

import os
import socket
import sys


def _looks_local(host: str | None) -> bool:
    """Skip source binding for in-host destinations.

    Flask talks to ZMQ at 127.0.0.1, to its own SQLite at file:..., and
    to the docker bridge gateway for any internal lookups. None of those
    want to leave via the broker tunnel.
    """
    if not host:
        return True
    if host in ("localhost", "::1"):
        return True
    return host.startswith(("127.", "10.", "192.168.", "172.", "169.254."))


def _install() -> None:
    src = os.environ.get("CLIENT_IPV6", "").strip()
    if not src:
        return  # no-op for non-hosted deployments

    try:
        from urllib3.connection import HTTPConnection
        from urllib3.util import connection as _u3c
    except Exception as exc:
        print(f"[source_bind] urllib3 import failed: {exc}", file=sys.stderr)
        return

    is_ipv6 = ":" in src
    source_tuple = (src, 0)
    _orig_init = HTTPConnection.__init__

    def _patched_init(self, *args, **kwargs):
        # Honor explicit caller-supplied source_address. Only inject ours
        # when the caller didn't ask for anything.
        if kwargs.get("source_address") is None:
            host = kwargs.get("host")
            if host is None and args:
                host = args[0]
            if not _looks_local(host):
                kwargs["source_address"] = source_tuple
        _orig_init(self, *args, **kwargs)

    HTTPConnection.__init__ = _patched_init

    # When source is IPv6, force AAAA-only DNS resolution. Without this
    # urllib3's create_connection() may prefer an A record for dual-stack
    # broker hostnames, attempt to bind an IPv6 source on an AF_INET
    # socket, and fail with EAI_FAMILY masquerading as a DNS error.
    if is_ipv6:
        _u3c.allowed_gai_family = lambda: socket.AF_INET6

    # One-line marker in the container log so we can see at startup that
    # the bind is active. Stays visible even at LOG_LEVEL=INFO.
    print(
        f"[source_bind] outbound HTTPS bound to {src} "
        f"(family={'AF_INET6' if is_ipv6 else 'AF_INET'})",
        file=sys.stderr,
    )


_install()
