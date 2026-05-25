"""HDFC Securities order API (InvestRight).

Endpoints used:
    POST   /oapi/v1/orders/regular              place
    PUT    /oapi/v1/orders/{order_id}           modify (with body) / cancel (no body)
    GET    /oapi/v1/orders                      order book
    GET    /oapi/v1/orders/{order_id}           single order status
    GET    /oapi/v1/trades                      trade book
    GET    /oapi/v1/portfolio/holdings          holdings
    GET    /oapi/v1/portfolio/cumulative-positions positions

Notable HDFC behaviours handled here:

  * F&O orders need lot-size multiplication client-side. We look the lot
    size up from the symtoken master before sending. If the master is
    missing, we conservatively reject the order rather than send a wrong
    quantity.
  * `transaction_type` is "Buy"/"Sell" with title-case; mapping lives in
    transform_data.map_action.
  * Position rows live under `data.net[]` — mapping/order_data unwraps.
  * Order cancel uses a PUT with no body; modify uses PUT with body.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from broker.hdfcsec.api.hdfc_http import request as hdfc_request
from broker.hdfcsec.baseurl import (
    HOLDINGS_URL,
    ORDER_BOOK_URL,
    ORDER_BY_ID_URL,
    ORDER_PLACE_URL,
    POSITIONS_URL,
    TRADE_BOOK_URL,
)
from broker.hdfcsec.mapping.order_data import (
    extract_error_message,
    extract_order_id,
    transform_holding_data,
    transform_order_data,
    transform_position_data,
    transform_trade_data,
)
from broker.hdfcsec.mapping.transform_data import (
    reverse_map_product_type,
    transform_data,
    transform_modify_order_data,
)
from utils.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Per-symbol smart-order serialization
# ---------------------------------------------------------------------------

_symbol_locks: Dict[str, threading.Lock] = {}
_symbol_locks_lock = threading.Lock()
_position_cache: Dict[str, Dict[str, Any]] = {}
_position_cache_lock = threading.Lock()
_POSITION_CACHE_TTL = 1.0


def _get_symbol_lock(symbol: str, exchange: str, product: str) -> threading.Lock:
    key = f"{symbol}:{exchange}:{product}"
    with _symbol_locks_lock:
        if key not in _symbol_locks:
            _symbol_locks[key] = threading.Lock()
        return _symbol_locks[key]


def _get_cached_positions(auth: str) -> Dict[str, Any]:
    with _position_cache_lock:
        cached = _position_cache.get(auth)
        if cached and (time.monotonic() - cached["timestamp"]) < _POSITION_CACHE_TTL:
            return cached["data"]
    data = hdfc_request("GET", POSITIONS_URL, auth)
    with _position_cache_lock:
        _position_cache[auth] = {"data": data, "timestamp": time.monotonic()}
    return data


def _invalidate_position_cache(auth: str) -> None:
    with _position_cache_lock:
        _position_cache.pop(auth, None)


# ---------------------------------------------------------------------------
# Master-contract lookup (security_id + lot_size)
# ---------------------------------------------------------------------------

def _lookup_security(symbol: str, exchange: str) -> Tuple[Optional[str], Optional[int]]:
    """Return (security_id, lot_size) from the symtoken master."""
    try:
        from database.token_db import get_token  # noqa: WPS433 — late import

        security_id = get_token(symbol, exchange)
    except Exception as e:
        logger.warning(f"HDFC: security lookup for {symbol}/{exchange} failed: {e}")
        security_id = None

    lot_size: Optional[int] = None
    try:
        from database.token_db import get_lotsize  # type: ignore

        lot_size = int(get_lotsize(symbol, exchange) or 1)
    except Exception:
        lot_size = 1  # safe default; F&O paths re-validate before sending

    return (str(security_id) if security_id else None), lot_size


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------

def get_order_book(auth: str) -> List[Dict[str, Any]]:
    raw = hdfc_request("GET", ORDER_BOOK_URL, auth)
    return transform_order_data(raw)


def get_trade_book(auth: str) -> List[Dict[str, Any]]:
    raw = hdfc_request("GET", TRADE_BOOK_URL, auth)
    return transform_trade_data(raw)


def get_positions(auth: str) -> List[Dict[str, Any]]:
    raw = _get_cached_positions(auth)
    return transform_position_data(raw)


def get_holdings(auth: str) -> List[Dict[str, Any]]:
    raw = hdfc_request("GET", HOLDINGS_URL, auth)
    return transform_holding_data(raw)


def get_open_position(tradingsymbol: str, exchange: str, product: str, auth: str) -> str:
    for p in get_positions(auth):
        if (
            p.get("symbol") == tradingsymbol
            and p.get("exchange") == exchange
            and p.get("product") == reverse_map_product_type(product, exchange)
        ):
            return str(p.get("quantity") or "0")
    return "0"


def get_order_status(orderid: str, auth: str) -> Dict[str, Any]:
    raw = hdfc_request(
        "GET", ORDER_BY_ID_URL, auth, url_args={"order_id": str(orderid)}
    )
    rows = transform_order_data(raw)
    return rows[0] if rows else {
        "status": "error",
        "message": extract_error_message(raw),
    }


# ---------------------------------------------------------------------------
# Place / modify / cancel
# ---------------------------------------------------------------------------

def place_order_api(
    data: Dict[str, Any], auth: str
) -> Tuple[Any, Dict[str, Any], Optional[str]]:
    """Place a single HDFC order. Returns (shim, raw_response, order_id)."""
    try:
        logger.info(f"=== HDFC place_order: {data!r}")
        symbol = data.get("symbol", "")
        exchange = data.get("exchange", "NSE")
        security_id, lot_size = _lookup_security(symbol, exchange)
        if not security_id:
            msg = f"HDFC: no security_id for {symbol}/{exchange} — refusing to send order"
            logger.error(msg)
            shim = type("R", (), {"status": 400, "status_code": 400})()
            return shim, {"status": "error", "message": msg}, None

        payload = transform_data(data, security_id=security_id, lot_size=lot_size)
        raw = hdfc_request("POST", ORDER_PLACE_URL, auth, payload=payload)

        if raw.get("status") == "success":
            oid = extract_order_id(raw) or None
            logger.info(f"HDFC place_order OK: order_id={oid}")
            shim = type("R", (), {"status": 200, "status_code": 200})()
            return shim, raw, oid

        err = extract_error_message(raw)
        logger.error(f"HDFC place_order failed: {err} (raw={raw!r})")
        shim = type("R", (), {"status": raw.get("http_status") or 500,
                              "status_code": raw.get("http_status") or 500})()
        return shim, raw, None

    except Exception as e:
        logger.exception("HDFC place_order_api exception")
        shim = type("R", (), {"status": 500, "status_code": 500})()
        return shim, {"status": "error", "message": str(e)}, None


def place_smartorder_api(
    data: Dict[str, Any], auth: str
) -> Tuple[Any, Dict[str, Any], Optional[str]]:
    """Adjust position toward position_size."""
    res: Any = None
    resp: Dict[str, Any] = {"status": "error", "message": "No action required"}
    orderid: Optional[str] = None
    try:
        symbol = data.get("symbol")
        exchange = data.get("exchange")
        product = data.get("product")
        if not all([symbol, exchange, product]):
            return res, resp, orderid

        lock = _get_symbol_lock(symbol, exchange, product)
        with lock:
            target = int(float(data.get("position_size", "0")))
            current = int(float(get_open_position(symbol, exchange, product, auth)))
            logger.info(
                f"HDFC smartorder symbol={symbol} target={target} current={current}"
            )

            if target == 0 and current == 0:
                return res, {"status": "success", "message": "No position to square off"}, orderid
            if target == current:
                return res, {"status": "success", "message": "Already at target"}, orderid

            if target == 0:
                action = "SELL" if current > 0 else "BUY"
                qty = abs(current)
            elif current == 0:
                action = "BUY" if target > 0 else "SELL"
                qty = abs(target)
            else:
                diff = target - current
                action = "BUY" if diff > 0 else "SELL"
                qty = abs(diff)

            if qty <= 0:
                return res, {"status": "success", "message": "No action required"}, orderid

            order_data = dict(data)
            order_data["action"] = action
            order_data["quantity"] = str(qty)
            res, resp, orderid = place_order_api(order_data, auth)
            _invalidate_position_cache(auth)
            return res, resp, orderid

    except Exception as e:
        logger.exception("HDFC place_smartorder_api exception")
        return res, {"status": "error", "message": str(e)}, orderid


def modify_order(data: Dict[str, Any], auth: str) -> Tuple[Dict[str, Any], int]:
    try:
        logger.info(f"=== HDFC modify_order: {data!r}")
        orderid = data.get("orderid")
        if not orderid:
            return {"status": "error", "message": "orderid is required"}, 400

        exchange = data.get("exchange", "NSE")
        _, lot_size = _lookup_security(data.get("symbol", ""), exchange) if data.get("symbol") else (None, 1)
        payload = transform_modify_order_data(data, lot_size=lot_size)

        raw = hdfc_request(
            "PUT", ORDER_BY_ID_URL, auth,
            url_args={"order_id": str(orderid)}, payload=payload,
        )
        if raw.get("status") == "success":
            return {"status": "success", "orderid": orderid}, 200
        return {"status": "error", "message": extract_error_message(raw)}, int(
            raw.get("http_status") or 400
        )
    except Exception as e:
        logger.exception("HDFC modify_order exception")
        return {"status": "error", "message": str(e)}, 500


def cancel_order(orderid: str, auth: str) -> Tuple[Dict[str, Any], int]:
    try:
        logger.info(f"=== HDFC cancel_order: {orderid}")
        # HDFC cancel = PUT with no body. We send `{}` to be safe.
        raw = hdfc_request(
            "PUT", ORDER_BY_ID_URL, auth,
            url_args={"order_id": str(orderid)}, payload={},
        )
        if raw.get("status") == "success":
            return {"status": "success", "orderid": str(orderid)}, 200
        return {"status": "error", "message": extract_error_message(raw)}, int(
            raw.get("http_status") or 400
        )
    except Exception as e:
        logger.exception("HDFC cancel_order exception")
        return {"status": "error", "message": str(e)}, 500


def cancel_all_orders_api(data: Dict[str, Any], auth: str) -> Tuple[List[str], List[str]]:
    cancelled: List[str] = []
    failed: List[str] = []
    try:
        for o in get_order_book(auth):
            status = (o.get("status") or "").lower()
            if status not in ("open", "pending", "trigger pending", "trigger_pending"):
                continue
            oid = o.get("orderid")
            if not oid:
                continue
            _, code = cancel_order(oid, auth)
            (cancelled if code == 200 else failed).append(str(oid))
    except Exception as e:  # pragma: no cover
        logger.exception(f"HDFC cancel_all_orders_api exception: {e}")
    return cancelled, failed


def close_all_positions(current_api_key: str, auth: str) -> Tuple[Dict[str, Any], int]:
    try:
        positions = get_positions(auth)
        if not positions:
            return {"message": "No Open Positions Found", "status": "success"}, 200

        closed: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        for p in positions:
            qty = int(float(p.get("quantity") or 0))
            if qty == 0:
                continue
            action = "SELL" if qty > 0 else "BUY"
            order_data = {
                "apikey": current_api_key,
                "strategy": "Squareoff",
                "symbol": p.get("symbol"),
                "action": action,
                "exchange": p.get("exchange"),
                "pricetype": "MARKET",
                "product": p.get("product") or "MIS",
                "quantity": str(abs(qty)),
            }
            _, resp, oid = place_order_api(order_data, auth)
            if oid:
                closed.append({"symbol": p.get("symbol"), "orderid": oid})
            else:
                failed.append({"symbol": p.get("symbol"), "error": extract_error_message(resp)})

        return {
            "message": "All Open Positions SquaredOff",
            "status": "success",
            "closed": closed,
            "failed": failed,
        }, 200
    except Exception as e:
        logger.exception("HDFC close_all_positions exception")
        return {"status": "error", "message": str(e)}, 500
