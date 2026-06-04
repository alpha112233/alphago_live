"""
Shared httpx client module with connection pooling support for all broker APIs
with automatic protocol negotiation (HTTP/2 when available, HTTP/1.1 fallback)
"""

from typing import Optional

import httpx

from utils.logging import get_logger

# Set up logging
logger = get_logger(__name__)

# Global httpx client for connection pooling
_httpx_client = None


def get_httpx_client() -> httpx.Client:
    """
    Returns an HTTP client with automatic protocol negotiation.
    The client will use HTTP/2 when the server supports it,
    otherwise automatically falls back to HTTP/1.1.

    Returns:
        httpx.Client: A configured HTTP client with protocol auto-negotiation
    """
    global _httpx_client

    if _httpx_client is None:
        _httpx_client = _create_http_client()
        logger.info(
            "Created HTTP client with automatic protocol negotiation (HTTP/2 preferred, HTTP/1.1 fallback)"
        )
    return _httpx_client


def request(method: str, url: str, **kwargs) -> httpx.Response:
    """
    Make an HTTP request using the shared client with automatic protocol negotiation.

    Args:
        method: HTTP method (GET, POST, etc.)
        url: URL to request
        **kwargs: Additional arguments to pass to the request

    Returns:
        httpx.Response: The HTTP response

    Raises:
        httpx.HTTPError: If the request fails
    """
    import time

    from flask import g

    client = get_httpx_client()

    # Track actual broker API call time for latency monitoring
    broker_api_start = time.time()
    response = client.request(method, url, **kwargs)
    broker_api_end = time.time()

    # Store broker API time in Flask's g object for latency tracking
    if hasattr(g, "latency_tracker"):
        broker_api_time_ms = (broker_api_end - broker_api_start) * 1000
        g.broker_api_time = broker_api_time_ms
        logger.debug(f"Broker API call took {broker_api_time_ms:.2f}ms")

    # Log the actual HTTP version used (info level for visibility)
    if response.http_version:
        logger.info(f"Request used {response.http_version} - URL: {url[:50]}...")

    return response


# Shortcut methods for common HTTP methods
def get(url: str, **kwargs) -> httpx.Response:
    """
    Send a GET request.

    Args:
        url (str): The URL to send the GET request to.
        **kwargs: Additional arguments passed to the underlying request method.

    Returns:
        httpx.Response: The HTTP response from the server.
    """
    return request("GET", url, **kwargs)


def post(url: str, **kwargs) -> httpx.Response:
    """
    Send a POST request.

    Args:
        url (str): The URL to send the POST request to.
        **kwargs: Additional arguments passed to the underlying request method.

    Returns:
        httpx.Response: The HTTP response from the server.
    """
    return request("POST", url, **kwargs)


def put(url: str, **kwargs) -> httpx.Response:
    """
    Send a PUT request.

    Args:
        url (str): The URL to send the PUT request to.
        **kwargs: Additional arguments passed to the underlying request method.

    Returns:
        httpx.Response: The HTTP response from the server.
    """
    return request("PUT", url, **kwargs)


def delete(url: str, **kwargs) -> httpx.Response:
    """
    Send a DELETE request.

    Args:
        url (str): The URL to send the DELETE request to.
        **kwargs: Additional arguments passed to the underlying request method.

    Returns:
        httpx.Response: The HTTP response from the server.
    """
    return request("DELETE", url, **kwargs)


def _create_http_client() -> httpx.Client:
    """
    Create a new HTTP client with automatic protocol negotiation and latency tracking.
    Enables both HTTP/2 and HTTP/1.1, letting httpx choose the best protocol.

    Returns:
        httpx.Client: A configured HTTP client with protocol auto-negotiation and timing hooks
    """
    import os
    import time

    from flask import g

    # Event hooks for tracking broker API timing
    def log_request(request):
        """Hook called before request is sent"""
        request.extensions["start_time"] = time.time()
        logger.debug(f"Starting request to {request.url}")

    def log_response(response):
        """Hook called after response is received"""
        try:
            start_time = response.request.extensions.get("start_time")
            if start_time:
                duration_ms = (time.time() - start_time) * 1000

                # Store broker API time in Flask's g object for latency tracking
                try:
                    from flask import has_request_context

                    if has_request_context() and hasattr(g, "latency_tracker"):
                        g.broker_api_time = duration_ms
                        logger.debug(f"Broker API call took {duration_ms:.2f}ms")
                except (RuntimeError, AttributeError):
                    # Not in Flask request context or g not available
                    pass

                logger.debug(f"Request completed in {duration_ms:.2f}ms")
        except Exception as e:
            logger.exception(f"Error in response hook: {e}")

    try:
        # Detect if running in standalone mode (Docker/production) vs integrated mode (local dev)
        # In standalone mode, disable HTTP/2 to avoid protocol negotiation issues
        app_mode = os.environ.get("APP_MODE", "integrated").strip().strip("'\"")
        is_standalone = app_mode == "standalone"

        # Disable HTTP/2 in standalone/Docker environments to avoid protocol negotiation issues
        http2_enabled = not is_standalone

        _limits = httpx.Limits(
            max_keepalive_connections=40,  # Increased from 20 for multi-strategy environments
            max_connections=100,  # Increased from 50 for 10+ concurrent strategies
            keepalive_expiry=30.0,  # Reduced from 120s to recycle stale connections faster
        )

        # Per-customer egress binding. utils/source_bind.py monkeypatches
        # urllib3 to bind CLIENT_IPV6 — but httpx uses httpcore, NOT urllib3,
        # so that patch does NOT apply here. Without an explicit
        # local_address, httpx egresses via the OS default route (the shared
        # host IPv6), which breaks the per-customer /128 IP-whitelist model
        # every hostingsol broker relies on. Symptom: Dhan DH-905 "Invalid IP"
        # because the order left from the host IP, not the customer's
        # whitelisted /128. (Found via the R3 canary 2026-05-28.)
        #
        # Bind via a custom transport when CLIENT_IPV6 is set. When unset
        # (local dev / single-host operator), fall back to the default
        # transport so behaviour is unchanged.
        client_ipv6 = (os.environ.get("CLIENT_IPV6") or "").strip()

        common_kwargs = dict(
            timeout=120.0,  # Increased timeout for large historical data requests
            event_hooks={"request": [log_request], "response": [log_response]},
        )

        # Phase 7 (2026-06-05) — IPv4-only broker hosts (Arihant TradeBridge,
        # HDFC InvestRight, etc.) get routed through a per-customer Decodo
        # ISP proxy with optional failover to a secondary IP. Most brokers
        # (v6-capable) keep using the direct CLIENT_IPV6 binding.
        # The `mounts` dict picks the right transport per destination host.
        from utils.decodo_proxy import build_mounts as _decodo_mounts
        mounts = _decodo_mounts(http2=http2_enabled, limits=_limits)

        if client_ipv6:
            client = httpx.Client(
                transport=httpx.HTTPTransport(
                    local_address=client_ipv6,
                    http2=http2_enabled,
                    http1=True,
                    limits=_limits,
                    verify=True,
                    retries=0,
                ),
                mounts=mounts or None,
                **common_kwargs,
            )
            logger.info(
                f"httpx client bound to CLIENT_IPV6={client_ipv6} (per-customer egress); "
                f"v4-only broker hosts routed via Decodo: {len(mounts)//2 if mounts else 0} hosts mounted"
            )
        else:
            client = httpx.Client(
                http2=http2_enabled,
                http1=True,
                limits=_limits,
                verify=True,
                mounts=mounts or None,
                **common_kwargs,
            )

        if is_standalone:
            logger.info("Running in standalone mode - HTTP/2 disabled for compatibility")
        else:
            logger.info("Running in integrated mode - HTTP/2 enabled for optimal performance")

        return client

    except Exception as e:
        logger.exception(f"Failed to create HTTP client: {e}")
        raise


def cleanup_httpx_client() -> None:
    """
    Closes the global httpx client and releases its resources.

    Should be called when the application is shutting down to prevent
    resource leaks.

    Returns:
        None
    """
    global _httpx_client

    if _httpx_client is not None:
        _httpx_client.close()
        _httpx_client = None
        logger.info("Closed HTTP client")
