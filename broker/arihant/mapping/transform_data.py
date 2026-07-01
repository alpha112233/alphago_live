"""Arihant transform helpers — canonical OpenAlgo order dict → Arihant wire body.

Mirrors the enum translation tables in ccxt-india/brokers/arihant/arihant.py
so the Arihant TradeBridge backend sees the same shape it does from prod.

Symbol resolution: OpenAlgo's place_order_api passes a ``token`` (numeric
exchange-token from database/token_db.py). Arihant's API expects:
  * symbol: tradingsymbol (e.g. "SBIN-EQ")
  * exc: exchange code ("NSE"/"BSE"/"NFO"/"BFO"/"MCX")
  * excToken: numeric exchange token (same as our ``token``)
  * instrument: "STK" | "FUT" | "OPT" (derived from exchange/segment)
  * lotSize: 1 for equity; from contract master for FNO
"""
from __future__ import annotations

# Canonical action ("BUY"/"SELL") → Arihant ordAction
_TX_TYPE = {"BUY": "BUY", "SELL": "SELL"}

# Canonical ordertype → Arihant ordType (note: awkwardly cased)
_ORD_TYPE = {
    "MARKET": "Market",
    "LIMIT": "Limit",
    "SL": "SL",
    "SL_M": "SL-M",
    "SL-M": "SL-M",
    "STOP": "Stop",
    "STOP_LOSS": "Stop-loss",
}

# Canonical product → Arihant prdType
_PRD_TYPE = {
    "CNC": "DELIVERY",
    "DELIVERY": "DELIVERY",
    "MIS": "INTRADAY",
    "INTRADAY": "INTRADAY",
    "NRML": "NRML",
    "MTF": "MTF",
    "CO": "COVER_ORDER",
    "BO": "BRACKET_ORDER",
}

# Reverse — for showing positions/orders back to the user in canonical terms
_PRD_TYPE_REVERSE = {
    "DELIVERY": "CNC",
    "INTRADAY": "MIS",
    "NRML": "NRML",
    "MTF": "MTF",
    "COVER_ORDER": "CO",
    "BRACKET_ORDER": "BO",
}

# Canonical duration → Arihant ordValidity
_DURATION = {"DAY": "DAY", "IOC": "IOC", "GTC": "GTC", "GTD": "GTD"}


def map_transaction_type(action: str) -> str:
    return _TX_TYPE.get((action or "").upper(), "")


def map_order_type(ordertype: str) -> str:
    return _ORD_TYPE.get((ordertype or "").upper(), "Limit")


def map_product_type(product: str) -> str:
    return _PRD_TYPE.get((product or "").upper(), "DELIVERY")


def reverse_map_product_type(broker_prd: str) -> str:
    return _PRD_TYPE_REVERSE.get((broker_prd or "").upper(), broker_prd or "")


def map_duration(duration: str) -> str:
    return _DURATION.get((duration or "DAY").upper(), "DAY")


def _instrument_for_exchange(exchange: str) -> str:
    e = (exchange or "").upper()
    if e in ("NFO", "BFO"):
        return "OPT"  # most NFO trades are options; FUT contracts override via symbol pattern
    if e == "MCX":
        return "FUT"
    return "STK"  # NSE / BSE


def _fno_lotsize(symbol: str, exchange: str) -> int | None:
    """Contract lot size for an F&O symbol from the scrip master (SymToken).
    Returns None if not found — caller then falls back to 1. Arihant silently
    drops an F&O order placed with lotSize=1, so this must resolve for FUT/OPT."""
    if not symbol:
        return None
    try:
        from database.symbol import SymToken, db_session
        row = (
            db_session.query(SymToken)
            .filter_by(symbol=symbol, exchange=(exchange or "").upper())
            .first()
        )
        return int(row.lotsize) if row and row.lotsize else None
    except Exception:
        return None


def transform_data(data: dict, token: str | int | None) -> dict:
    """OpenAlgo canonical dict → Arihant place-order body.

    Canonical keys (per OpenAlgo schema):
      symbol, exchange, action, ordertype, product, quantity, price,
      trigger_price, disclosed_quantity, duration, after_hours, tag

    Token comes from database/token_db.py and is the exchange-token
    (numeric). Lot size: 1 for equity by default; FNO trades should
    pass lot_size via data['lot_size'] (frontend smart-form does this).
    """
    # Arihant scrip-master uses suffixed tradingsymbol ("YESBANK-EQ", not
    # "YESBANK"). OpenAlgo's canonical symbol is bare. If the symbol came
    # in bare AND exchange is NSE/BSE equity, append "-EQ". FNO / commodity
    # symbols already carry their own suffix (e.g. NIFTY24DEC25000CE) so
    # leave anything that already contains '-' or doesn't start with letters
    # untouched.
    sym = data.get("symbol") or ""
    exc_in = (data.get("exchange") or "NSE").upper()
    if exc_in in ("NSE", "BSE") and "-" not in sym and sym:
        sym = f"{sym}-EQ"
    instrument = _instrument_for_exchange(data.get("exchange"))
    # Lot size: equity = 1. For F&O (FUT/OPT) arihant needs the REAL contract
    # lot size — a lotSize of 1 makes arihant accept the request at the API
    # layer but the exchange silently drops it (success, but no ordId, order
    # never appears in the book — 2026-07-01 NIFTY option incident). Prefer the
    # caller-supplied lot_size; else look it up from the scrip master.
    lot_size = int(data.get("lot_size") or 0)
    if lot_size < 1 and instrument in ("FUT", "OPT"):
        lot_size = _fno_lotsize(data.get("symbol"), exc_in) or 1
    lot_size = max(1, lot_size)
    return {
        "symbol": sym,
        "exc": exc_in,
        "excToken": str(token) if token is not None else "",
        "instrument": instrument,
        "lotSize": lot_size,
        "ordAction": map_transaction_type(data.get("action")),
        # OpenAlgo's canonical schema names this 'pricetype' — accept all
        # three to avoid silent downgrade to 'Limit' (which Arihant rejects
        # with 'Invalid request' when limitPrice is 0).
        "ordType": map_order_type(
            data.get("pricetype") or data.get("ordertype") or data.get("order_type")
        ),
        "ordValidity": map_duration(data.get("duration")),
        "prdType": map_product_type(data.get("product")),
        "qty": int(float(data.get("quantity") or 0)),
        "disQty": int(float(data.get("disclosed_quantity") or 0)),
        "limitPrice": float(data.get("price") or 0.0),
        "triggerPrice": float(data.get("trigger_price") or 0.0),
        "amo": str(data.get("after_hours") or "N").upper() == "Y",
        "remarks": (data.get("tag") or "openalgo")[:30],
    }


def transform_modify_order_data(data: dict) -> dict:
    """Modify body — same shape minus the symbol/token bits (those don't
    change in a modify). ``ordId`` is stamped by the caller."""
    return {
        "ordAction": map_transaction_type(data.get("action")),
        # OpenAlgo's canonical schema names this 'pricetype' — accept all
        # three to avoid silent downgrade to 'Limit' (which Arihant rejects
        # with 'Invalid request' when limitPrice is 0).
        "ordType": map_order_type(
            data.get("pricetype") or data.get("ordertype") or data.get("order_type")
        ),
        "ordValidity": map_duration(data.get("duration")),
        "prdType": map_product_type(data.get("product")),
        "qty": int(float(data.get("quantity") or 0)),
        "disQty": int(float(data.get("disclosed_quantity") or 0)),
        "limitPrice": float(data.get("price") or 0.0),
        "triggerPrice": float(data.get("trigger_price") or 0.0),
        "remarks": (data.get("tag") or "openalgo-modify")[:30],
    }
