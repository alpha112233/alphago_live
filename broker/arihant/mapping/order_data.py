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


def _oa_symbol(brsym, exchange):
    """Reverse-map arihant's broker tradingSymbol -> OpenAlgo canonical symbol
    (e.g. NIFTY2670724100PE -> NIFTY07JUL2624100PE) so downstream consumers —
    notably the publisher's close/positions/holdings symbol matching — get the
    canonical form. Falls back to the broker symbol if the lookup misses.
    Without this, arihant read data carries the compressed F&O brsymbol which
    never matches the canonical symbol the publisher tracks (2026-07-02)."""
    if not brsym:
        return ""
    try:
        from database.token_db import get_oa_symbol
        return get_oa_symbol(brsym, (exchange or "").upper()) or brsym
    except Exception:
        return brsym


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
            "symbol": _oa_symbol(sym.get("tradingSymbol") or sym.get("symbol"), sym.get("exc")),
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


def calculate_order_statistics(order_data: list[dict]) -> dict:
    """Order-book totals (buy/sell/completed/open/rejected), computed over the
    MAPPED rows — `action` and `order_status` are already OpenAlgo-normalized
    (BUY/SELL, uppercase status). Required by services/orderbook_service.py's
    import_broker_module; without it the whole /orderbook path 404s for arihant."""
    total_buy_orders = total_sell_orders = 0
    total_completed_orders = total_open_orders = total_rejected_orders = 0
    for order in (order_data or []):
        action = str(order.get("action") or "").upper()
        if action == "BUY":
            total_buy_orders += 1
        elif action == "SELL":
            total_sell_orders += 1
        status = str(order.get("order_status") or "").upper()
        if status in ("COMPLETE", "COMPLETED", "EXECUTED", "FILLED", "TRADED"):
            total_completed_orders += 1
        elif status in ("OPEN", "PENDING", "NEW", "TRIGGER PENDING", "PLACED"):
            total_open_orders += 1
        elif status in ("REJECTED", "REJECT"):
            total_rejected_orders += 1
    return {
        "total_buy_orders": total_buy_orders,
        "total_sell_orders": total_sell_orders,
        "total_completed_orders": total_completed_orders,
        "total_open_orders": total_open_orders,
        "total_rejected_orders": total_rejected_orders,
    }


def transform_order_data(order_data: list[dict]) -> list[dict]:
    """Called on the ALREADY-MAPPED rows — orderbook_service runs
    map_order_data() first, then transform_order_data() on its output. So
    return the rows as-is. (Re-running map_order_data here double-maps: the
    mapped rows have `symbol` as a plain string + renamed keys like
    `order_status`/`filled_quantity`, so a second pass finds none of the raw
    fields and blanks EVERY field — the 2026-07-01 empty-orderbook bug.)"""
    return order_data or []


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
            "symbol": _oa_symbol(sym.get("tradingSymbol") or sym.get("symbol"), sym.get("exc")),
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
            "symbol": _oa_symbol(sym.get("tradingSymbol") or sym.get("symbol"), sym.get("exc")),
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
        # Arihant holdings field names (verified against live response
        # 2026-06-29): quantity=`qty`/`holdingQty`, pnl=`unRealizedPnl`,
        # pnl%=`pnlPerc`, avg=`avgPrice`. The earlier guesses (totalQty/netQty,
        # pnl, pnlPct) didn't exist → qty/pnl read 0 → the publisher dropped
        # every row (its qty>0 filter), so holdings looked empty.
        pnl = h.get("unRealizedPnl")
        if pnl is None:
            pnl = h.get("pnl")
        pnlpct = h.get("pnlPerc")
        if pnlpct is None:
            pnlpct = h.get("pnlPct")
        out.append({
            "symbol": _oa_symbol(sym.get("tradingSymbol") or sym.get("symbol"), sym.get("exc")),
            "exchange": (sym.get("exc") or "NSE").upper(),
            "isin": h.get("isin"),
            "quantity": h.get("qty") or h.get("holdingQty") or h.get("totalQty") or h.get("netQty") or 0,
            "average_price": h.get("avgPrice") or h.get("avgCostPrice"),
            "ltp": h.get("ltp"),
            "pnl": pnl,
            "pnl_percent": pnlpct,
            "product": "CNC",  # holdings always show as CNC
        })
    return out


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def calculate_portfolio_statistics(holdings_data: list[dict]) -> dict:
    """Portfolio totals over the MAPPED Arihant holdings (the output of
    map_portfolio_data — keys: quantity, average_price, ltp, pnl).

    Required by services/holdings_service.import_broker_module — its absence
    made the whole Arihant holdings module fail to import ("Broker-specific
    module not found", 404) for every Arihant customer. (Added 2026-06-29.)
    """
    total_current = sum(_num(h.get("ltp")) * _num(h.get("quantity")) for h in holdings_data or [])
    total_inv = sum(_num(h.get("average_price")) * _num(h.get("quantity")) for h in holdings_data or [])
    total_pnl = sum(_num(h.get("pnl")) for h in holdings_data or [])
    total_pnl_pct = (total_pnl / total_inv * 100) if total_inv else 0.0
    return {
        "totalholdingvalue": round(total_current, 2),
        "totalinvvalue": round(total_inv, 2),
        "totalprofitandloss": round(total_pnl, 2),
        "totalpnlpercentage": round(total_pnl_pct, 2),
    }


def transform_holdings_data(holdings: list[dict]) -> list[dict]:
    """Project MAPPED Arihant holdings to the OpenAlgo holdings schema.

    holdings_service calls this with the OUTPUT of map_portfolio_data (already
    normalized), so we must NOT re-run map_portfolio_data here — doing so
    double-mapped the data (treated the normalized string `symbol` as the raw
    dict) and blanked every symbol. Just project to the standard keys and
    rename pnl_percent -> pnlpercent. (Fixed 2026-06-29.)
    """
    out = []
    for h in holdings or []:
        if not isinstance(h, dict):
            continue
        pnlpct = h.get("pnl_percent")
        if pnlpct is None:
            pnlpct = h.get("pnlpercent")
        out.append({
            "symbol": h.get("symbol", ""),
            "exchange": h.get("exchange", "NSE"),
            "quantity": int(_num(h.get("quantity"))),
            "product": h.get("product", "CNC"),
            "average_price": round(_num(h.get("average_price")), 2),
            "pnl": round(_num(h.get("pnl")), 2),
            "pnlpercent": round(_num(pnlpct), 2),
        })
    return out
