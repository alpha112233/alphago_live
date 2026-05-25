"""ICICI Breeze response -> OpenAlgo response normalization.

Breeze wraps every payload under "Success" (on 200) or "Error" (on
non-200). Order/trade/position/holding rows live inside "Success".
"""
from __future__ import annotations

from typing import Any, Dict, List

from utils.logging import get_logger

from broker.icicidirect.mapping.transform_data import (
    map_order_status,
    reverse_map_action,
    reverse_map_exchange,
    reverse_map_price_type,
    reverse_map_product_type,
)

logger = get_logger(__name__)


def _rows(payload: Any) -> List[Dict[str, Any]]:
    """Unwrap Breeze envelope into a row-list. Returns [] on any error."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        succ = payload.get("Success")
        if isinstance(succ, list):
            return succ
        if isinstance(succ, dict):
            for k in ("trade_book", "order_book", "positions", "holdings"):
                v = succ.get(k)
                if isinstance(v, list):
                    return v
            return [succ] if succ else []
    return []


def transform_order_data(payload: Any) -> List[Dict[str, Any]]:
    """Breeze order book -> OpenAlgo order list."""
    out: List[Dict[str, Any]] = []
    for o in _rows(payload):
        try:
            exch = reverse_map_exchange(o.get("exchange_code", ""))
            out.append({
                "symbol": o.get("stock_code", ""),
                "exchange": exch,
                "action": reverse_map_action(o.get("action", "")),
                "quantity": str(o.get("quantity", "0")),
                "price": str(o.get("price", "0")),
                "trigger_price": str(o.get("stoploss", "0")),
                "pricetype": reverse_map_price_type(o.get("order_type", "")),
                "product": reverse_map_product_type(o.get("product_type", ""), exch),
                "orderid": str(o.get("order_id", "")),
                "status": map_order_status(o.get("status", "")),
                "timestamp": o.get("order_datetime", ""),
                "filled_qty": str(o.get("quantity", 0))
                              if str(o.get("status", "")).lower().startswith("execut")
                              else str(
                                  int(float(o.get("quantity") or 0))
                                  - int(float(o.get("pending_quantity") or 0))
                              ),
                "pending_qty": str(o.get("pending_quantity", "0")),
                "average_price": str(o.get("average_price", "0")),
            })
        except Exception as e:  # pragma: no cover
            logger.error(f"Failed to transform order row: {e} — row={o!r}")
    return out


def transform_trade_data(payload: Any) -> List[Dict[str, Any]]:
    """Breeze trade book -> OpenAlgo trade list."""
    out: List[Dict[str, Any]] = []
    for t in _rows(payload):
        try:
            exch = reverse_map_exchange(t.get("exchange_code", ""))
            out.append({
                "symbol": t.get("order_stock_code") or t.get("stock_code", ""),
                "exchange": exch,
                "action": reverse_map_action(t.get("order_flow") or t.get("action", "")),
                "quantity": str(t.get("order_quantity") or t.get("quantity", "0")),
                "average_price": str(
                    t.get("order_average_executed_rate") or t.get("average_price", "0")
                ),
                "orderid": str(t.get("order_reference") or t.get("order_id", "")),
                "product": reverse_map_product_type(
                    t.get("order_product") or t.get("product", ""), exch
                ),
                "timestamp": t.get("trade_datetime") or t.get("execution_time", ""),
            })
        except Exception as e:  # pragma: no cover
            logger.error(f"Failed to transform trade row: {e} — row={t!r}")
    return out


def transform_position_data(payload: Any) -> List[Dict[str, Any]]:
    """Breeze positions -> OpenAlgo positions list."""
    out: List[Dict[str, Any]] = []
    for p in _rows(payload):
        try:
            exch = reverse_map_exchange(p.get("exchange_code", ""))
            qty = int(float(p.get("quantity") or 0))
            mtf_sell = int(float(p.get("mtf_sell_quantity") or 0))
            net_qty = qty - mtf_sell  # sell side reduces net
            out.append({
                "symbol": p.get("stock_code", ""),
                "exchange": exch,
                "product": reverse_map_product_type(p.get("product_type", ""), exch),
                "quantity": str(net_qty),
                "average_price": str(p.get("price", "0")),
                "pnl": str(p.get("unrealized_profit", "0")),
                "realized_pnl": str(p.get("realized_profit", "0")),
                "last_price": str(p.get("ltp", p.get("last_price", "0"))),
            })
        except Exception as e:  # pragma: no cover
            logger.error(f"Failed to transform position row: {e} — row={p!r}")
    return out


def transform_holding_data(payload: Any) -> List[Dict[str, Any]]:
    """Breeze holdings -> OpenAlgo holdings list."""
    out: List[Dict[str, Any]] = []
    for h in _rows(payload):
        try:
            exch = reverse_map_exchange(h.get("exchange_code", "NSE"))
            qty = int(float(h.get("quantity") or 0))
            avg = float(h.get("average_price") or 0)
            ltp = float(h.get("current_market_price") or 0)
            out.append({
                "symbol": h.get("stock_code", ""),
                "exchange": exch,
                "quantity": str(qty),
                "average_price": f"{avg:.2f}",
                "isin": h.get("isin", ""),
                "product": "CNC",
                "last_price": f"{ltp:.2f}",
                "pnl": str(h.get("unrealized_profit", "0")),
            })
        except Exception as e:  # pragma: no cover
            logger.error(f"Failed to transform holding row: {e} — row={h!r}")
    return out


def extract_order_id(response_data: Dict[str, Any]) -> str:
    """Pluck the order_id out of a Breeze place-order success response.

    Breeze returns:
        { "Status": 200, "Success": { "order_id": "<digits>", "message": "..." } }
    Sometimes the order_id is "Order placed ... 12345" with the digits at
    the tail; mirror SDK's split-on-whitespace fallback (icici.py:557-579).
    """
    if not isinstance(response_data, dict):
        return ""
    succ = response_data.get("Success") or response_data.get("success") or {}
    if isinstance(succ, dict):
        oid = succ.get("order_id") or succ.get("orderid") or ""
        if isinstance(oid, str) and " " in oid:
            return oid.split()[-1]
        return str(oid)
    return ""


def extract_error_message(response_data: Dict[str, Any]) -> str:
    """Pluck the human-readable error string out of a Breeze failure."""
    if not isinstance(response_data, dict):
        return str(response_data)
    err = response_data.get("Error") or response_data.get("error") or response_data.get("message")
    if isinstance(err, dict):
        return err.get("message") or str(err)
    return str(err or response_data.get("emsg") or "Unknown Breeze error")
