"""HDFC InvestRight response -> OpenAlgo response normalization.

Most HDFC envelopes look like:
    { "status": "success", "data": [ {...}, ... ] }
Positions wrap differently:
    { "status": "success", "data": { "net": [ {...}, ... ] } }
Single-order details return a single-element list:
    { "status": "success", "data": [ {...} ] }
"""
from __future__ import annotations

from typing import Any, Dict, List

from utils.logging import get_logger

from broker.hdfcsec.mapping.transform_data import (
    map_order_status,
    reverse_map_action,
    reverse_map_exchange,
    reverse_map_price_type,
    reverse_map_product_type,
)

logger = get_logger(__name__)


def _rows(payload: Any, *, positions: bool = False) -> List[Dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if positions:
            net = data.get("net") or data.get("Net") or []
            if isinstance(net, list):
                return net
        # Generic single-object payload
        return [data]
    return []


def transform_order_data(payload: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for o in _rows(payload):
        try:
            exch = reverse_map_exchange(
                o.get("exchange", ""), o.get("instrument_type") or o.get("instrument_segment", "")
            )
            qty = int(float(o.get("quantity") or 0))
            filled = int(float(o.get("filled_quantity") or 0))
            pending = int(float(o.get("pending_quantity") or max(qty - filled, 0)))
            out.append({
                "symbol": o.get("tradingsymbol") or o.get("underlying_symbol", ""),
                "exchange": exch,
                "action": reverse_map_action(o.get("transaction_type", "")),
                "quantity": str(qty),
                "price": str(o.get("price") or 0),
                "trigger_price": str(o.get("trigger_price") or 0),
                "pricetype": reverse_map_price_type(o.get("order_type", "")),
                "product": reverse_map_product_type(o.get("product", ""), exch),
                "orderid": str(o.get("order_id", "")),
                "status": map_order_status(o.get("status") or o.get("status_message", "")),
                "timestamp": o.get("order_timestamp", ""),
                "filled_qty": str(filled),
                "pending_qty": str(pending),
                "average_price": str(o.get("average_price") or 0),
            })
        except Exception as e:  # pragma: no cover
            logger.error(f"Failed to transform HDFC order row: {e} — {o!r}")
    return out


def transform_trade_data(payload: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for t in _rows(payload):
        try:
            exch = reverse_map_exchange(
                t.get("exchange", ""), t.get("instrument_type") or t.get("instrument_segment", "")
            )
            out.append({
                "symbol": t.get("tradingsymbol") or t.get("underlying_symbol", ""),
                "exchange": exch,
                "action": reverse_map_action(t.get("transaction_type", "")),
                "quantity": str(t.get("filled_quantity") or t.get("quantity", 0)),
                "average_price": str(t.get("average_price", "0")),
                "orderid": str(t.get("order_id", "")),
                "product": reverse_map_product_type(t.get("product", ""), exch),
                "timestamp": t.get("trade_timestamp") or t.get("exchange_timestamp", ""),
            })
        except Exception as e:  # pragma: no cover
            logger.error(f"Failed to transform HDFC trade row: {e} — {t!r}")
    return out


def transform_position_data(payload: Any) -> List[Dict[str, Any]]:
    """Position rows come from data.net[]."""
    out: List[Dict[str, Any]] = []
    for p in _rows(payload, positions=True):
        try:
            seg = p.get("instrument_type") or p.get("instrument_segment", "")
            raw_exch = p.get("exchange", "")
            # HDFC sometimes returns "ALL"; treat as NSE for OpenAlgo round-tripping.
            if raw_exch.upper() == "ALL":
                raw_exch = "NSE"
            exch = reverse_map_exchange(raw_exch, seg)
            net = int(float(p.get("t_day_net_qty") or 0))
            buy_qty = int(float(p.get("t_day_buy_quantity") or 0))
            sell_qty = int(float(p.get("t_day_sell_qty") or 0))
            buy_val = float(p.get("t_day_buy_value") or 0)
            sell_val = float(p.get("t_day_sell_value") or 0)
            avg_buy = float(p.get("t_day_average_buy_price") or 0)
            avg_sell = float(p.get("t_day_avg_sell_price") or 0)
            out.append({
                "symbol": p.get("tradingsymbol") or p.get("underlying_symbol", ""),
                "exchange": exch,
                "product": reverse_map_product_type(p.get("product", ""), exch),
                "quantity": str(net),
                "buy_quantity": str(buy_qty),
                "sell_quantity": str(sell_qty),
                "buy_value": f"{buy_val:.2f}",
                "sell_value": f"{sell_val:.2f}",
                "average_price": f"{avg_buy if net >= 0 else avg_sell:.2f}",
                "pnl": f"{sell_val - buy_val:.2f}",
                "realized_pnl": "0",
                "last_price": str(p.get("ltp") or p.get("last_price") or 0),
            })
        except Exception as e:  # pragma: no cover
            logger.error(f"Failed to transform HDFC position row: {e} — {p!r}")
    return out


def transform_holding_data(payload: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for h in _rows(payload):
        try:
            # HDFC holdings carry instrument_token of form "NSE..." / "BSE..."
            tok = (h.get("instrument_token") or "")[:3]
            exch = tok if tok in ("NSE", "BSE") else "NSE"
            qty = int(float(h.get("quantity") or 0))
            avg = float(h.get("average_price") or 0)
            ltp = float(h.get("close_price") or h.get("ltp") or 0)
            out.append({
                "symbol": h.get("tradingsymbol") or h.get("company_name", ""),
                "exchange": exch,
                "quantity": str(qty),
                "average_price": f"{avg:.2f}",
                "isin": h.get("isin", ""),
                "product": "CNC",
                "last_price": f"{ltp:.2f}",
                "pnl": f"{(ltp - avg) * qty:.2f}",
            })
        except Exception as e:  # pragma: no cover
            logger.error(f"Failed to transform HDFC holding row: {e} — {h!r}")
    return out


def extract_order_id(response_data: Dict[str, Any]) -> str:
    if not isinstance(response_data, dict):
        return ""
    data = response_data.get("data") or {}
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        return str(data.get("order_id") or data.get("orderId") or "")
    return ""


def extract_error_message(response_data: Dict[str, Any]) -> str:
    if not isinstance(response_data, dict):
        return str(response_data)
    msg = (
        response_data.get("message")
        or response_data.get("status_message")
        or response_data.get("error")
        or response_data.get("errorMessage")
    )
    if msg:
        return str(msg)
    # If `data` is an error object
    data = response_data.get("data")
    if isinstance(data, dict):
        return str(data.get("message") or data.get("error") or data)
    return "Unknown HDFC error"
