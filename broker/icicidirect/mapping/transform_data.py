"""OpenAlgo -> ICICI Breeze request normalization.

Breeze enums are unusual: product types are lowercased domain words
("cash" / "margin" / "futures" / "options" / "futureplus"), validity is
"day"/"ioc", action is "buy"/"sell", order_type is "limit"/"market"
(though MARKET is rejected by Breeze — see icici.py:498 in ccxt-india;
we mirror the SDK's IOC-limit conversion in order_api).

F&O orders pass the option/future contract via four separate fields
(stock_code, expiry_date, strike_price, right). Equity orders pass a
plain stock_code with no derivative fields.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

from utils.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Enum mappings
# ---------------------------------------------------------------------------

def map_exchange(exchange: str) -> str:
    """OpenAlgo exchange -> Breeze exchange_code."""
    return {
        "NSE": "NSE",
        "BSE": "BSE",
        "NFO": "NFO",
        "BFO": "BFO",
        "CDS": "NSE",   # Breeze routes CDS via NSE exchange_code; SDK quirk
        "MCX": "MCX",
    }.get(exchange, exchange)


def reverse_map_exchange(exchange: str) -> str:
    return {"NSE": "NSE", "BSE": "BSE", "NFO": "NFO", "BFO": "BFO", "MCX": "MCX"}.get(
        exchange, exchange
    )


def map_action(action: str) -> str:
    """OpenAlgo action -> Breeze action ('buy'/'sell', lowercase)."""
    return (action or "").strip().lower() or "buy"


def reverse_map_action(action: str) -> str:
    a = (action or "").strip().lower()
    return {"buy": "BUY", "sell": "SELL", "b": "BUY", "s": "SELL"}.get(a, a.upper())


def map_validity(validity: str) -> str:
    """OpenAlgo validity -> Breeze validity ('day'/'ioc')."""
    v = (validity or "DAY").strip().upper()
    return {"DAY": "day", "IOC": "ioc"}.get(v, "day")


def map_product_type(product: str, exchange: str = "NSE") -> str:
    """OpenAlgo product -> Breeze product enum.

    The mapping depends on the segment:
      - Equity (NSE/BSE):  CNC -> "cash", MIS -> "margin", NRML -> "cash"
      - F&O    (NFO/BFO):  NRML -> "futureplus" (carryforward),
                           MIS  -> "futures"   (intraday futures)
                                                or "options"        (intraday opts)
    The caller knows the segment via exchange. F&O product is auto-selected
    based on the option suffix in transform_order_payload below.
    """
    p = (product or "CNC").strip().upper()
    if exchange in ("NSE", "BSE"):
        return {"CNC": "cash", "MIS": "margin", "NRML": "cash"}.get(p, "cash")
    if exchange in ("NFO", "BFO"):
        return {"NRML": "futureplus", "MIS": "futures", "CNC": "futureplus"}.get(p, "futures")
    return "cash"


def reverse_map_product_type(product: str, exchange: str = "NSE") -> str:
    p = (product or "").strip().lower()
    if exchange in ("NSE", "BSE"):
        return {"cash": "CNC", "margin": "MIS"}.get(p, "CNC")
    return {"futureplus": "NRML", "futures": "MIS", "options": "MIS", "optionplus": "NRML"}.get(
        p, "NRML"
    )


def map_price_type(pricetype: str) -> str:
    """OpenAlgo pricetype -> Breeze order_type (lowercase).

    Breeze rejects raw MARKET (returns "kindly pass 'limit'"); place_order_api
    converts MARKET -> IOC-limit at LTP+/-buffer before sending. We still
    return "market" here so the order_api layer can detect and convert.
    """
    pt = (pricetype or "LIMIT").strip().upper()
    return {"MARKET": "market", "LIMIT": "limit", "SL": "stoploss", "SL-M": "stoploss"}.get(
        pt, "limit"
    )


def reverse_map_price_type(pricetype: str) -> str:
    pt = (pricetype or "").strip().lower()
    return {"market": "MARKET", "limit": "LIMIT", "stoploss": "SL"}.get(pt, "LIMIT")


def map_order_status(status: str) -> str:
    """Breeze status -> OpenAlgo canonical status."""
    s = (status or "").strip().lower()
    return {
        "executed": "complete",
        "completed": "complete",
        "complete": "complete",
        "ordered": "open",
        "open": "open",
        "fresh": "open",
        "partial executed": "open",
        "partially executed": "open",
        "cancelled": "cancelled",
        "rejected": "rejected",
        "expired": "rejected",
    }.get(s, s)


# ---------------------------------------------------------------------------
# F&O symbol decoding
# ---------------------------------------------------------------------------

# OpenAlgo F&O symbol formats accepted (mirrors how prod packs them):
#   - NIFTY25JAN24500CE       (index option, expiry DD+MMM, year inferred)
#   - RELIANCE25JAN3000PE     (stock option)
#   - NIFTY25JANFUT           (index future)
#   - RELIANCE25JANFUT        (stock future)
# The 25JAN portion is DD + MMM only; the year is the next occurrence of
# that month from today. Packed years are not part of the OpenAlgo
# convention, so we deliberately reject formats like 25JAN2026 to avoid
# ambiguity with the strike (e.g. 25JAN24500 — is 24 the year or the
# first two digits of strike 24500?).
_FNO_OPTION_RE = re.compile(
    r"^(?P<root>[A-Z0-9]+?)(?P<expiry>\d{2}[A-Z]{3})(?P<strike>\d+(?:\.\d+)?)(?P<right>CE|PE)$"
)
_FNO_FUTURE_RE = re.compile(
    r"^(?P<root>[A-Z0-9]+?)(?P<expiry>\d{2}[A-Z]{3})FUT$"
)


def _decode_fno_symbol(symbol: str) -> Optional[Dict[str, str]]:
    """Decode an OpenAlgo F&O symbol into Breeze components.

    Returns None for non-F&O strings; otherwise a dict with keys
    root, expiry (e.g. "25-JAN-2026"), strike_price (or "0" for FUT),
    right ("call"/"put"/"others"), product_kind ("options"/"futures").
    """
    s = (symbol or "").strip().upper()
    m = _FNO_OPTION_RE.match(s)
    if m:
        return {
            "root": m.group("root"),
            "expiry": _normalize_expiry(m.group("expiry")),
            "strike_price": m.group("strike"),
            "right": "call" if m.group("right") == "CE" else "put",
            "product_kind": "options",
        }
    m = _FNO_FUTURE_RE.match(s)
    if m:
        return {
            "root": m.group("root"),
            "expiry": _normalize_expiry(m.group("expiry")),
            "strike_price": "0",
            "right": "others",
            "product_kind": "futures",
        }
    return None


_MONTH_NUM = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_MONTH_TITLE = {n: name for name, n in zip(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
    range(1, 13),
)}


def _normalize_expiry(raw: str) -> str:
    """'25JAN' -> 'DD-Mon-YYYY' (Breeze format).

    Year is the next occurrence of that month from today: if today is
    in January 2026 and the user writes 25JAN we resolve to 25-Jan-2026;
    if 25 January has already passed we roll forward to 2027.
    """
    import datetime

    s = raw.upper()
    if len(s) < 5:
        return raw
    try:
        day = int(s[:2])
    except ValueError:
        return raw
    mon3 = s[2:5]
    month_idx = _MONTH_NUM.get(mon3)
    if month_idx is None:
        return raw
    today = datetime.date.today()
    year = today.year
    try:
        candidate = datetime.date(year, month_idx, day)
    except ValueError:
        return f"{day:02d}-{_MONTH_TITLE[month_idx]}-{year}"
    if candidate < today:
        year += 1
    return f"{day:02d}-{_MONTH_TITLE[month_idx]}-{year}"


# ---------------------------------------------------------------------------
# Public transforms
# ---------------------------------------------------------------------------

def transform_data(data: Dict[str, Any], token_id: Optional[str] = None) -> Dict[str, Any]:
    """OpenAlgo place-order payload -> Breeze POST /order body.

    Inputs (OpenAlgo): symbol, exchange, quantity, price, action, pricetype,
    product, trigger_price, disclosed_quantity, validity, strategy.
    """
    from database.token_db import get_br_symbol  # late import — circular safety

    exchange = data.get("exchange", "NSE")
    symbol = data.get("symbol", "")

    # Equity vs derivative branching
    fno = _decode_fno_symbol(symbol) if exchange in ("NFO", "BFO") else None

    if fno:
        product_explicit = (data.get("product") or "").strip().upper()
        if product_explicit == "NRML":
            product = "futureplus" if fno["product_kind"] == "futures" else "optionplus"
        else:
            product = fno["product_kind"]  # 'futures' or 'options' (intraday default)
        stock_code = fno["root"]
        body = {
            "stock_code": stock_code,
            "exchange_code": map_exchange(exchange),
            "product": product,
            "action": map_action(data.get("action", "BUY")),
            "order_type": map_price_type(data.get("pricetype", "LIMIT")),
            "validity": map_validity(data.get("validity", "DAY")),
            "quantity": str(data.get("quantity", "0")),
            "price": str(data.get("price", "0")),
            "stoploss": str(data.get("trigger_price", "0")),
            "disclosed_quantity": str(data.get("disclosed_quantity", "0")),
            "expiry_date": fno["expiry"],
            "right": fno["right"],
            "strike_price": fno["strike_price"],
        }
    else:
        # Equity path. Resolve broker-side stock_code via master if available.
        try:
            stock_code = get_br_symbol(symbol, exchange) or symbol
        except Exception:
            stock_code = symbol
        body = {
            "stock_code": stock_code,
            "exchange_code": map_exchange(exchange),
            "product": map_product_type(data.get("product", "CNC"), exchange),
            "action": map_action(data.get("action", "BUY")),
            "order_type": map_price_type(data.get("pricetype", "LIMIT")),
            "validity": map_validity(data.get("validity", "DAY")),
            "quantity": str(data.get("quantity", "0")),
            "price": str(data.get("price", "0")),
            "stoploss": str(data.get("trigger_price", "0")),
            "disclosed_quantity": str(data.get("disclosed_quantity", "0")),
        }

    return body


def transform_modify_order_data(data: Dict[str, Any], token_id: Optional[str] = None) -> Dict[str, Any]:
    """OpenAlgo modify-order payload -> Breeze PUT /order body."""
    body = {
        "order_id": data.get("orderid"),
        "exchange_code": map_exchange(data.get("exchange", "NSE")),
        "order_type": map_price_type(data.get("pricetype", "LIMIT")),
        "validity": map_validity(data.get("validity", "DAY")),
        "quantity": str(data.get("quantity", "0")),
        "price": str(data.get("price", "0")),
        "stoploss": str(data.get("trigger_price", "0")),
        "disclosed_quantity": str(data.get("disclosed_quantity", "0")),
    }
    return body


def split_fno_components(symbol: str) -> Tuple[str, Optional[Dict[str, str]]]:
    """Convenience for non-order callers (quotes, GTT): return (root, fno_dict|None)."""
    fno = _decode_fno_symbol(symbol)
    if fno is None:
        return symbol, None
    return fno["root"], fno
