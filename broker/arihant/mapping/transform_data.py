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
    # Arihant REPORTS an NRML (F&O carry) order's product as "CARRYFORWARD" in
    # the order/position book even though it ACCEPTS "NRML" on placement. Without
    # this, read-side product = "CARRYFORWARD" (unmapped) → the publisher's
    # product-matched close/positions logic never matches → F&O NRML positions
    # show "broker reports flat" and can't be closed (2026-07-02).
    "CARRYFORWARD": "NRML",
    "CF": "NRML",
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


def _br_symbol(symbol: str, exchange: str) -> str | None:
    """Arihant broker symbol (brsymbol) from the scrip master. Equity =
    'RELIANCE-EQ', F&O = 'NIFTY2670724000CE' (compressed) — arihant rejects the
    canonical OpenAlgo symbol for F&O with EG001 'Invalid request'. None on miss
    (caller falls back to the -EQ heuristic)."""
    if not symbol:
        return None
    try:
        from database.token_db import get_br_symbol
        return get_br_symbol(symbol, (exchange or "").upper()) or None
    except Exception:
        return None


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


def _fno_instrument(symbol: str, exchange: str) -> str | None:
    """Arihant instrument type from the scrip master for F&O: OPTIDX / OPTSTK /
    FUTIDX / FUTSTK. arihant's order.place rejects the GENERIC 'OPT'/'FUT' with
    EG001 'Invalid request' — it wants the specific type its own master uses.
    None on miss (caller falls back to the generic mapping)."""
    if not symbol:
        return None
    try:
        from database.symbol import SymToken, db_session
        row = (
            db_session.query(SymToken)
            .filter_by(symbol=symbol, exchange=(exchange or "").upper())
            .first()
        )
        it = (row.instrumenttype or "").strip() if row else ""
        return it or None
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
    exc_in = (data.get("exchange") or "NSE").upper()
    # Arihant expects its OWN broker symbol (brsymbol), which differs from
    # OpenAlgo's canonical symbol: equity "RELIANCE" -> "RELIANCE-EQ", but F&O
    # "NIFTY07JUL2624000CE" -> "NIFTY2670724000CE" (compressed). Sending the
    # canonical symbol for F&O makes arihant reject with EG001 "Invalid request"
    # (2026-07-01). Resolve brsymbol from the scrip master; fall back to the
    # legacy -EQ heuristic only if that lookup fails.
    sym = _br_symbol(data.get("symbol"), exc_in)
    if not sym:
        sym = data.get("symbol") or ""
        if exc_in in ("NSE", "BSE") and "-" not in sym and sym:
            sym = f"{sym}-EQ"
    instrument = _instrument_for_exchange(data.get("exchange"))
    # For F&O, arihant wants the SPECIFIC instrument type from its master
    # (OPTIDX/OPTSTK/FUTIDX/FUTSTK) — the generic 'OPT'/'FUT' is rejected EG001
    # 'Invalid request' (2026-07-02, even with the correct brsymbol). Resolve it
    # from the scrip master; keep the generic mapping only as a fallback.
    if exc_in in ("NFO", "BFO"):
        _it = _fno_instrument(data.get("symbol"), exc_in)
        if _it:
            instrument = _it
    # Lot size: equity = 1. For F&O (FUT/OPT) arihant needs the REAL contract
    # lot size — a lotSize of 1 makes arihant accept the request at the API
    # layer but the exchange silently drops it (success, but no ordId, order
    # never appears in the book — 2026-07-01 NIFTY option incident). Prefer the
    # caller-supplied lot_size; else look it up from the scrip master.
    lot_size = int(data.get("lot_size") or 0)
    # F&O = any exchange-derived option/future instrument. Match on the exchange
    # (not the instrument string) so the specific master types OPTIDX/OPTSTK/
    # FUTIDX/FUTSTK all qualify — checking `instrument in ("FUT","OPT")` here
    # broke once instrument became the specific type (2026-07-02).
    if lot_size < 1 and exc_in in ("NFO", "BFO", "MCX", "CDS", "NCDEX", "BCD"):
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
