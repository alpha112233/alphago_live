"""Arihant TradeBridge base URL + route registry.

Production: tradebridge.arihantplus.com (currently IPv4-only — confirmed
2026-05-20 via DNS; AAAA needed before this broker can be enabled in
hostingsol's IPv6-only egress. Plugin is design-parity-ready; activation
gated on DNS).

UAT: uat-smartapi.arihantplus.com — same path layout. Switchable via
the ARIHANT_BASE_URL env var.
"""
import os

BASE_URL = os.environ.get(
    "ARIHANT_BASE_URL", "https://tradebridge.arihantplus.com"
).rstrip("/")

# Routes lifted from ccxt-india/brokers/arihant/arihant.py (which has the
# canonical Arihant SDK calls). Both /auth-services and /wrapper-service
# share the same base; routes diverge only on the path prefix.
ROUTES = {
    # Auth
    "auth.login":         "/auth-services/api/auth/v1/login",
    "auth.verify_otp":    "/auth-services/api/auth/v1/verify-otp",
    "auth.resend_otp":    "/auth-services/api/auth/v1/resend-otp",
    "auth.refresh":       "/auth-services/api/auth/v1/refresh-token",
    "auth.logout":        "/auth-services/api/auth/v1/logout",
    # Orders
    "order.place":        "/wrapper-service/api/order/v1/place-order",
    "order.modify":       "/wrapper-service/api/order/v1/modify-order",
    "order.cancel":       "/wrapper-service/api/order/v1/cancel-order",
    "order.exit":         "/wrapper-service/api/order/v1/exit-order",
    "order.book":         "/wrapper-service/api/order/v1/order-book",
    "order.trade_book":   "/wrapper-service/api/order/v1/trade-book",
    "order.status":       "/wrapper-service/api/order/v1/order-status",
    # Portfolio / funds / profile
    "portfolio.holdings":  "/wrapper-service/api/portfolio/v1/holdings",
    "portfolio.positions": "/wrapper-service/api/portfolio/v1/position-book",
    "funds.view":          "/wrapper-service/api/funds/v1/get-funds",
    "user.profile":        "/wrapper-service/api/user/v1/get-profile",
    # Symbol cache (unauthenticated)
    "symbol.master":       "/wrapper-service/api/symbol/v1/master/cache",
}


def get_url(route_or_path: str) -> str:
    """Resolve either a logical route (e.g. ``order.place``) or a raw path
    (e.g. ``/wrapper-service/api/...``) to a full URL."""
    if route_or_path in ROUTES:
        return BASE_URL + ROUTES[route_or_path]
    if route_or_path.startswith("/"):
        return BASE_URL + route_or_path
    return BASE_URL + "/" + route_or_path
