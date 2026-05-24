"""Arihant response transforms — broker wire format → OpenAlgo canonical.

Used by the order/trade/position views in the OpenAlgo dashboard to
render Arihant's responses in the same shape every other broker uses
(so the frontend doesn't have to special-case Arihant).

Field mapping derived from ccxt-india/brokers/arihant/arihant.py response
parsers (which production has been running against Arihant since the
prod-alphaquark-github launch).
"""
from __future__ import annotations

from broker.arihant.mapping.transform_data import reverse_map_product_type


def map_order_data(order_data: list[dict]) -> list[dict]:
    """Normalize order book rows. Returns a list of dicts with the
    canonical fields the OpenAlgo /orderbook page expects."""
    if not order_data:
        return []
    out = []
    for o in order_data:
        if not isinstance(o, dict):
            continue
        sym = o.get("symbol") or {}
        if not isinstance(sym, dict):
            sym = {}
        out.append({
            "orderid": o.get("ordId"),
            "exchange": (sym.get("exc") or o.get("exc") or "").upper(),
            "symbol": sym.get("tradingSymbol") or sym.get("symbol") or "",
            "action": (o.get("ordAction") or "").upper(),
            "quantity": o.get("qty"),
            "price": o.get("price") or o.get("limitPrice"),
            "trigger_price": o.get("triggerPrice"),
            "order_type": o.get("ordType"),
            "product": reverse_map_product_type(o.get("prdType")),
            "order_status": (o.get("status") or o.get("orderStatus") or "").upper(),
            "filled_quantity": o.get("tradedQty"),
            "pending_quantity": o.get("remainQty"),
            "average_price": o.get("avgPrice"),
            "rejection_reason": o.get("rejReason"),
            "order_time": o.get("orderUpdatedAt") or o.get("excOrdTime"),
            "tag": o.get("remarks"),
        })
    return out


def transform_order_data(order_data: list[dict]) -> list[dict]:
    """Alias used by some OpenAlgo views — same shape as map_order_data."""
    return map_order_data(order_data)


def map_trade_data(trade_data: list[dict]) -> list[dict]:
    if not trade_data:
        return []
    out = []
    for t in trade_data:
        if not isinstance(t, dict):
            continue
        sym = t.get("symbol") or {}
        if not isinstance(sym, dict):
            sym = {}
        out.append({
            "orderid": t.get("ordId"),
            "exchange": (sym.get("exc") or "").upper(),
            "symbol": sym.get("tradingSymbol") or sym.get("symbol") or "",
            "action": (t.get("ordAction") or "").upper(),
            "quantity": t.get("qty"),
            "fill_price": t.get("avgPrice") or t.get("price"),
            "filled_quantity": t.get("tradedQty") or t.get("qty"),
            "product": reverse_map_product_type(t.get("prdType")),
            "trade_time": t.get("tradeTime") or t.get("excTime"),
        })
    return out


def transform_tradebook_data(trade_data: list[dict]) -> list[dict]:
    return map_trade_data(trade_data)


def map_position_data(position_data: list[dict]) -> list[dict]:
    """Normalize Arihant position-book rows."""
    if not position_data:
        return []
    out = []
    for p in position_data:
        if not isinstance(p, dict):
            continue
        sym = p.get("symbol") or {}
        if not isinstance(sym, dict):
            sym = {}
        out.append({
            "symbol": sym.get("tradingSymbol") or sym.get("symbol") or "",
            "exchange": (sym.get("exc") or "").upper(),
            "product": reverse_map_product_type(p.get("prdType")),
            "quantity": p.get("netQty") or p.get("net_qty") or 0,
            "average_price": p.get("avgBuyPrice") or p.get("avgPrice"),
            "ltp": p.get("ltp"),
            "pnl": p.get("pnl") or p.get("realizedPnl"),
            "day_pnl": p.get("dayPnl"),
        })
    return out


def transform_positions_data(position_data: list[dict]) -> list[dict]:
    return map_position_data(position_data)


def map_portfolio_data(holdings: list[dict]) -> list[dict]:
    """Normalize Arihant holdings."""
    if not holdings:
        return []
    out = []
    for h in holdings:
        if not isinstance(h, dict):
            continue
        sym = h.get("symbol") or {}
        if not isinstance(sym, dict):
            sym = {}
        out.append({
            "symbol": sym.get("tradingSymbol") or sym.get("symbol") or "",
            "exchange": (sym.get("exc") or "NSE").upper(),
            "isin": h.get("isin"),
            "quantity": h.get("totalQty") or h.get("netQty") or 0,
            "average_price": h.get("avgPrice") or h.get("avgCostPrice"),
            "ltp": h.get("ltp"),
            "pnl": h.get("pnl"),
            "pnl_percent": h.get("pnlPct"),
            "product": "CNC",  # holdings always show as CNC
        })
    return out


def transform_holdings_data(holdings: list[dict]) -> list[dict]:
    return map_portfolio_data(holdings)
