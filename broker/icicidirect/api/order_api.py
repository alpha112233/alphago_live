"""ICICI Direct order API (Breeze).

Endpoints used:
    POST   /breezeapi/api/v1/order              place
    PUT    /breezeapi/api/v1/order              modify
    DELETE /breezeapi/api/v1/order              cancel
    GET    /breezeapi/api/v1/order              order book / status
    GET    /breezeapi/api/v1/trades             trade book
    GET    /breezeapi/api/v1/portfoliopositions positions
    GET    /breezeapi/api/v1/portfolioholdings  holdings

Notable Breeze quirks handled here:

  * Breeze rejects ``order_type: "market"`` with the message
    "kindly pass 'limit'". When the OpenAlgo caller asks for MARKET we
    convert to an IOC-limit (NSE) or DAY-limit (BSE) with a tiered LTP
    buffer, matching ccxt-india/brokers/icici/icici.py:498-530.

  * Successful place-order responses sometimes return the order_id
    inside a sentence ("Order placed successfully. Order id is 12345");
    we always tail-split on whitespace via
    :func:`broker.icicidirect.mapping.order_data.extract_order_id`.

  * 429s are retried with backoff inside :func:`breeze_http.request`.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from broker.icicidirect.api.breeze_http import request as breeze_request
from broker.icicidirect.api.data import _get_ltp_for_symbol
from broker.icicidirect.baseurl import (
    HOLDINGS_URL,
    ORDER_URL,
    POSITIONS_URL,
    TRADES_URL,
)
from broker.icicidirect.mapping.order_data import (
    extract_error_message,
    extract_order_id,
    transform_holding_data,
    transform_order_data,
    transform_position_data,
    transform_trade_data,
)
from broker.icicidirect.mapping.transform_data import (
    reverse_map_product_type,
    transform_data,
    transform_modify_order_data,
)
from utils.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Per-symbol smart-order serialization (mirrors definedge implementation)
# ---------------------------------------------------------------------------

_symbol_locks: Dict[str, threading.Lock] = {}
_symbol_locks_lock = threading.Lock()
_position_cache: Dict[str, Dict[str, Any]] = {}
_position_cache_lock = threading.Lock()
_POSITION_CACHE_TTL = 1.0  # seconds


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
    data = breeze_request("GET", POSITIONS_URL, auth, payload={})
    with _position_cache_lock:
        _position_cache[auth] = {"data": data, "timestamp": time.monotonic()}
    return data


def _invalidate_position_cache(auth: str) -> None:
    with _position_cache_lock:
        _position_cache.pop(auth, None)


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------

def get_order_book(auth: str) -> List[Dict[str, Any]]:
    raw = breeze_request("GET", ORDER_URL, auth, payload={"exchange_code": "NSE"})
    return transform_order_data(raw)


def get_trade_book(auth: str) -> List[Dict[str, Any]]:
    raw = breeze_request("GET", TRADES_URL, auth, payload={"exchange_code": "NSE"})
    return transform_trade_data(raw)


def get_positions(auth: str) -> List[Dict[str, Any]]:
    raw = _get_cached_positions(auth)
    return transform_position_data(raw)


def get_holdings(auth: str) -> List[Dict[str, Any]]:
    raw = breeze_request("GET", HOLDINGS_URL, auth, payload={"exchange_code": "NSE"})
    return transform_holding_data(raw)


def get_open_position(tradingsymbol: str, exchange: str, product: str, auth: str) -> str:
    """Return signed net qty (as string) for an OpenAlgo (symbol, exch, product) triple."""
    positions = get_positions(auth)
    for p in positions:
        if (
            p.get("symbol") == tradingsymbol
            and p.get("exchange") == exchange
            and p.get("product") == reverse_map_product_type(product, exchange)
        ):
            return str(p.get("quantity") or "0")
    return "0"


def get_order_status(orderid: str, auth: str) -> Dict[str, Any]:
    payload = {"order_id": str(orderid), "exchange_code": "NSE"}
    raw = breeze_request("GET", ORDER_URL, auth, payload=payload)
    rows = transform_order_data(raw)
    return rows[0] if rows else {"status": "error", "message": extract_error_message(raw)}


# ---------------------------------------------------------------------------
# Place / modify / cancel
# ---------------------------------------------------------------------------

def _ioc_limit_price_with_buffer(ltp: float, action: str) -> float:
    """Tiered LTP-buffer for MARKET->IOC-limit conversion.

    Mirrors ccxt-india/brokers/icici/icici.py:236-249. Buffer scales with
    price: cheap stocks need more headroom in absolute terms.
    """
    if ltp <= 100:
        buf = 0.50
    elif ltp <= 1000:
        buf = 1.00
    elif ltp <= 5000:
        buf = 5.00
    else:
        buf = 10.00
    if action.lower() == "buy":
        return round(ltp + buf, 2)
    return max(round(ltp - buf, 2), 0.05)


def _maybe_convert_market_to_limit(
    payload: Dict[str, Any], data: Dict[str, Any], auth: str
) -> Dict[str, Any]:
    """If caller wants MARKET, convert to IOC/DAY-limit at LTP±buffer."""
    if payload.get("order_type") != "market":
        return payload
    exchange = data.get("exchange", "NSE")
    action = data.get("action", "BUY")
    symbol = data.get("symbol", "")
    ltp = 0.0
    try:
        ltp = float(_get_ltp_for_symbol(symbol, exchange, auth) or 0)
    except Exception as e:
        logger.warning(f"ICICI: failed to fetch LTP for MARKET->limit conversion: {e}")
    if ltp <= 0:
        fallback = float(data.get("price") or 0)
        if fallback <= 0:
            logger.error(
                "ICICI MARKET order: no LTP and no price supplied; sending Breeze a "
                "limit order_type with quote=0 will reject. Caller should provide "
                "a price floor for safety."
            )
            return payload
        price = fallback
    else:
        price = _ioc_limit_price_with_buffer(ltp, action)

    payload = dict(payload)
    payload["order_type"] = "limit"
    payload["price"] = str(price)
    payload["validity"] = "ioc" if exchange == "NSE" else "day"
    logger.info(
        f"ICICI: MARKET->{payload['validity'].upper()}-LIMIT @ {price} "
        f"(ltp={ltp}, action={action}, exchange={exchange})"
    )
    return payload


def place_order_api(
    data: Dict[str, Any], auth: str
) -> Tuple[Any, Dict[str, Any], Optional[str]]:
    """Place a single order. Returns (response_shim, response_data, order_id).

    The response_shim is a simple object with `.status` + `.status_code`
    attributes to match the calling convention used by the PlaceOrder API
    endpoint (mirrors definedge).
    """
    try:
        logger.info(f"=== ICICI place_order: {data!r}")
        payload = transform_data(data)
        payload = _maybe_convert_market_to_limit(payload, data, auth)

        raw = breeze_request("POST", ORDER_URL, auth, payload=payload)
        status = raw.get("Status") if isinstance(raw, dict) else None

        order_id: Optional[str] = None
        if status in (200, "200"):
            order_id = extract_order_id(raw) or None
            logger.info(f"ICICI place_order OK: order_id={order_id}")
        else:
            logger.error(
                f"ICICI place_order failed: {extract_error_message(raw)} (raw={raw!r})"
            )

        shim = type("R", (), {"status": status or 500, "status_code": status or 500})()
        return shim, raw, order_id

    except Exception as e:
        logger.exception("ICICI place_order_api exception")
        shim = type("R", (), {"status": 500, "status_code": 500})()
        return shim, {"Status": 500, "Error": str(e)}, None


def place_smartorder_api(
    data: Dict[str, Any], auth: str
) -> Tuple[Any, Dict[str, Any], Optional[str]]:
    """Adjust position toward `position_size`. Returns same triple as place_order_api."""
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
                f"ICICI smartorder symbol={symbol} target={target} current={current}"
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
        logger.exception("ICICI place_smartorder_api exception")
        return res, {"status": "error", "message": str(e)}, orderid


def modify_order(data: Dict[str, Any], auth: str) -> Tuple[Dict[str, Any], int]:
    try:
        logger.info(f"=== ICICI modify_order: {data!r}")
        payload = transform_modify_order_data(data)
        raw = breeze_request("PUT", ORDER_URL, auth, payload=payload)
        status = raw.get("Status") if isinstance(raw, dict) else None
        if status in (200, "200"):
            return {"status": "success", "orderid": data.get("orderid")}, 200
        return {"status": "error", "message": extract_error_message(raw)}, int(status or 400)
    except Exception as e:
        logger.exception("ICICI modify_order exception")
        return {"status": "error", "message": str(e)}, 500


def cancel_order(orderid: str, auth: str) -> Tuple[Dict[str, Any], int]:
    try:
        logger.info(f"=== ICICI cancel_order: {orderid}")
        payload = {"order_id": str(orderid), "exchange_code": "NSE"}
        raw = breeze_request("DELETE", ORDER_URL, auth, payload=payload)
        status = raw.get("Status") if isinstance(raw, dict) else None
        if status in (200, "200"):
            return {"status": "success", "orderid": str(orderid)}, 200
        return {"status": "error", "message": extract_error_message(raw)}, int(status or 400)
    except Exception as e:
        logger.exception("ICICI cancel_order exception")
        return {"status": "error", "message": str(e)}, 500


def cancel_all_orders_api(data: Dict[str, Any], auth: str) -> Tuple[List[str], List[str]]:
    """Cancel every open / trigger-pending order. Returns (cancelled_ids, failed_ids)."""
    cancelled: List[str] = []
    failed: List[str] = []
    try:
        for o in get_order_book(auth):
            status = (o.get("status") or "").lower()
            if status not in ("open", "trigger pending", "pending", "trigger_pending"):
                continue
            oid = o.get("orderid")
            if not oid:
                continue
            _, code = cancel_order(oid, auth)
            (cancelled if code == 200 else failed).append(str(oid))
    except Exception as e:  # pragma: no cover
        logger.exception(f"ICICI cancel_all_orders_api exception: {e}")
    return cancelled, failed


def close_all_positions(current_api_key: str, auth: str) -> Tuple[Dict[str, Any], int]:
    """Square off every non-zero position with a MARKET counter-order."""
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
        logger.exception("ICICI close_all_positions exception")
        return {"status": "error", "message": str(e)}, 500
