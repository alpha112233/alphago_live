"""OpenAlgo -> HDFC InvestRight request normalization.

HDFC enum quirks (mirrors ccxt-india/brokers/hdfc/hdfcsec.py):

  * ``transaction_type`` is "Buy"/"Sell" (capitalised, NOT "BUY"/"SELL").
  * ``product`` differs per segment:
      - Equity   CNC  → "DELIVERY"
                MIS  → "INTRADAY"
                NRML → "INTRADAY"
                CO   → "INTRADAY"
                BO   → "INTRADAY"
                MTF  → "MTF"            (margin trading facility)
      - F&O      MIS  → "INTRADAY"
                NRML → "OVERNIGHT"
                CNC  → "OVERNIGHT"
  * ``validity`` is "DAY"/"IOC"/"GTD".
  * ``order_type`` is "MARKET"/"LIMIT"/"SL"/"SL-M".
  * ``instrument_segment`` is one of EQUITY, FUTSTK, OPTSTK, FUTIDX,
    OPTIDX, FUTCUR, OPTCUR, FUTCOM, OPTFUT.
  * F&O orders carry ``underlying_symbol``, ``expiry_date`` (DDMMYYYY,
    no separators), ``strike_price``, ``option_type`` (CE/PE) as
    separate fields.
  * F&O quantity is the lot-size-multiplied client-side count — the
    master contract carries the lot_size.
"""
from __future__ import annotations

import random
import re
from typing import Any, Dict, Optional, Tuple

from utils.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Enum mappings
# ---------------------------------------------------------------------------

def map_exchange(exchange: str) -> str:
    return {"NSE": "NSE", "BSE": "BSE", "NFO": "NSE", "BFO": "BSE",
            "CDS": "NSE", "MCX": "MCX"}.get(exchange, exchange)


def reverse_map_exchange(exchange: str, instrument_segment: str = "") -> str:
    seg = (instrument_segment or "").upper()
    if seg in ("FUTSTK", "OPTSTK", "FUTIDX", "OPTIDX"):
        return "NFO" if exchange == "NSE" else "BFO"
    if seg in ("FUTCUR", "OPTCUR"):
        return "CDS"
    if seg in ("FUTCOM", "OPTFUT"):
        return "MCX"
    return exchange


def map_action(action: str) -> str:
    """OpenAlgo BUY/SELL -> HDFC 'Buy'/'Sell'."""
    return {"BUY": "Buy", "SELL": "Sell", "B": "Buy", "S": "Sell"}.get(
        (action or "BUY").strip().upper(), "Buy"
    )


def reverse_map_action(action: str) -> str:
    return {"buy": "BUY", "sell": "SELL"}.get((action or "").strip().lower(), action.upper())


def map_validity(validity: str) -> str:
    v = (validity or "DAY").strip().upper()
    return {"DAY": "DAY", "IOC": "IOC", "GTD": "GTD"}.get(v, "DAY")


def map_price_type(pricetype: str) -> str:
    pt = (pricetype or "LIMIT").strip().upper()
    return {"MARKET": "MARKET", "LIMIT": "LIMIT", "SL": "SL", "SL-M": "SL-M"}.get(pt, "LIMIT")


def reverse_map_price_type(pricetype: str) -> str:
    return {"MARKET": "MARKET", "LIMIT": "LIMIT", "SL": "SL", "SL-M": "SL-M"}.get(
        (pricetype or "").strip().upper(), "LIMIT"
    )


def map_product_type(product: str, exchange: str = "NSE") -> str:
    """OpenAlgo product -> HDFC product enum (segment-aware)."""
    p = (product or "CNC").strip().upper()
    if exchange in ("NSE", "BSE"):
        return {
            "CNC": "DELIVERY", "MIS": "INTRADAY", "NRML": "INTRADAY",
            "CO": "INTRADAY", "BO": "INTRADAY", "MTF": "MTF",
        }.get(p, "DELIVERY")
    if exchange in ("NFO", "BFO", "CDS", "MCX"):
        return {"MIS": "INTRADAY", "NRML": "OVERNIGHT", "CNC": "OVERNIGHT"}.get(p, "OVERNIGHT")
    return "DELIVERY"


def reverse_map_product_type(product: str, exchange: str = "NSE") -> str:
    p = (product or "").strip().upper()
    if exchange in ("NSE", "BSE"):
        return {"DELIVERY": "CNC", "INTRADAY": "MIS", "MTF": "MTF"}.get(p, "CNC")
    return {"INTRADAY": "MIS", "OVERNIGHT": "NRML"}.get(p, "NRML")


def map_order_status(status: str) -> str:
    s = (status or "").strip().lower()
    return {
        "complete": "complete", "completed": "complete", "executed": "complete",
        "open": "open", "pending": "open", "trigger pending": "open",
        "cancelled": "cancelled", "canceled": "cancelled",
        "rejected": "rejected", "expired": "rejected",
    }.get(s, s)


# ---------------------------------------------------------------------------
# F&O symbol decoding (same convention as the icicidirect port)
# ---------------------------------------------------------------------------

_FNO_OPTION_RE = re.compile(
    r"^(?P<root>[A-Z0-9]+?)(?P<expiry>\d{2}[A-Z]{3})(?P<strike>\d+(?:\.\d+)?)(?P<right>CE|PE)$"
)
_FNO_FUTURE_RE = re.compile(
    r"^(?P<root>[A-Z0-9]+?)(?P<expiry>\d{2}[A-Z]{3})FUT$"
)

_MONTH_NUM = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _normalize_expiry_ddmmyyyy(raw: str) -> str:
    """'25JAN' -> 'DDMMYYYY' (HDFC's no-separator format).

    Year is the next future occurrence — same rule as icicidirect.
    """
    import datetime

    s = raw.upper()
    if len(s) < 5:
        return raw
    try:
        day = int(s[:2])
    except ValueError:
        return raw
    mon = _MONTH_NUM.get(s[2:5])
    if mon is None:
        return raw
    today = datetime.date.today()
    year = today.year
    try:
        candidate = datetime.date(year, mon, day)
    except ValueError:
        return f"{day:02d}{mon:02d}{year}"
    if candidate < today:
        year += 1
    return f"{day:02d}{mon:02d}{year}"


def _decode_fno_symbol(symbol: str) -> Optional[Dict[str, str]]:
    """OpenAlgo F&O packed string -> HDFC component fields."""
    s = (symbol or "").strip().upper()
    m = _FNO_OPTION_RE.match(s)
    if m:
        return {
            "root": m.group("root"),
            "expiry_date": _normalize_expiry_ddmmyyyy(m.group("expiry")),
            "strike_price": m.group("strike"),
            "option_type": m.group("right"),     # "CE" / "PE"
            "is_option": True,
        }
    m = _FNO_FUTURE_RE.match(s)
    if m:
        return {
            "root": m.group("root"),
            "expiry_date": _normalize_expiry_ddmmyyyy(m.group("expiry")),
            "strike_price": "0",
            "option_type": "",
            "is_option": False,
        }
    return None


def split_fno_components(symbol: str) -> Tuple[str, Optional[Dict[str, str]]]:
    """Convenience for non-order callers — returns (root, fno_dict|None)."""
    fno = _decode_fno_symbol(symbol)
    if fno is None:
        return symbol, None
    return fno["root"], fno


# ---------------------------------------------------------------------------
# Public order transforms
# ---------------------------------------------------------------------------

def _new_external_ref() -> int:
    """HDFC's external_reference_number — 9-digit int, idempotency key."""
    return random.randint(100_000_000, 999_999_999)


def _instrument_segment_for(exchange: str, is_option: bool, is_index: bool) -> str:
    """Derive HDFC's instrument_segment enum from OpenAlgo metadata."""
    if exchange in ("NSE", "BSE"):
        return "EQUITY"
    if exchange in ("NFO", "BFO"):
        if is_option:
            return "OPTIDX" if is_index else "OPTSTK"
        return "FUTIDX" if is_index else "FUTSTK"
    if exchange == "CDS":
        return "OPTCUR" if is_option else "FUTCUR"
    if exchange == "MCX":
        return "OPTFUT" if is_option else "FUTCOM"
    return "EQUITY"


_INDEX_ROOTS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}


def transform_data(
    data: Dict[str, Any],
    *,
    security_id: Optional[str] = None,
    lot_size: Optional[int] = None,
) -> Dict[str, Any]:
    """OpenAlgo place-order payload -> HDFC POST /orders/regular body.

    ``security_id`` is looked up from the master contract by the caller.
    ``lot_size`` is required for F&O so we can scale OpenAlgo's "lots"
    field into the absolute share count HDFC expects.
    """
    exchange = data.get("exchange", "NSE")
    symbol = data.get("symbol", "")
    fno = _decode_fno_symbol(symbol) if exchange in ("NFO", "BFO", "CDS", "MCX") else None

    if fno is not None:
        is_index = fno["root"] in _INDEX_ROOTS
        instrument_segment = _instrument_segment_for(exchange, fno["is_option"], is_index)
        qty = int(float(data.get("quantity") or 0)) * int(lot_size or 1)
        body = {
            "exchange": map_exchange(exchange),
            "security_id": str(security_id or ""),
            "instrument_segment": instrument_segment,
            "transaction_type": map_action(data.get("action", "BUY")),
            "product": map_product_type(data.get("product", "NRML"), exchange),
            "order_type": map_price_type(data.get("pricetype", "LIMIT")),
            "price": float(data.get("price") or 0),
            "trigger_price": float(data.get("trigger_price") or 0),
            "quantity": qty,
            "disclosed_quantity": int(float(data.get("disclosed_quantity") or 0)),
            "validity": map_validity(data.get("validity", "DAY")),
            "amo": bool(data.get("amo", False)),
            "external_reference_number": _new_external_ref(),
            "underlying_symbol": fno["root"],
            "expiry_date": fno["expiry_date"],
        }
        if fno["is_option"]:
            body["strike_price"] = float(fno["strike_price"])
            body["option_type"] = fno["option_type"]
        return body

    # Equity path.
    body = {
        "exchange": map_exchange(exchange),
        "security_id": str(security_id or ""),
        "instrument_segment": "EQUITY",
        "transaction_type": map_action(data.get("action", "BUY")),
        "product": map_product_type(data.get("product", "CNC"), exchange),
        "order_type": map_price_type(data.get("pricetype", "LIMIT")),
        "price": float(data.get("price") or 0),
        "trigger_price": float(data.get("trigger_price") or 0),
        "quantity": int(float(data.get("quantity") or 0)),
        "disclosed_quantity": int(float(data.get("disclosed_quantity") or 0)),
        "validity": map_validity(data.get("validity", "DAY")),
        "amo": bool(data.get("amo", False)),
        "external_reference_number": _new_external_ref(),
    }
    return body


def transform_modify_order_data(
    data: Dict[str, Any],
    *,
    lot_size: Optional[int] = None,
) -> Dict[str, Any]:
    """OpenAlgo modify-order payload -> HDFC PUT /orders/{id} body."""
    exchange = data.get("exchange", "NSE")
    qty = int(float(data.get("quantity") or 0))
    if exchange in ("NFO", "BFO", "CDS", "MCX") and lot_size:
        qty = qty * int(lot_size)
    return {
        "product": map_product_type(data.get("product", "CNC"), exchange),
        "order_type": map_price_type(data.get("pricetype", "LIMIT")),
        "price": float(data.get("price") or 0),
        "trigger_price": float(data.get("trigger_price") or 0),
        "quantity": qty,
        "disclosed_quantity": int(float(data.get("disclosed_quantity") or 0)),
        "validity": map_validity(data.get("validity", "DAY")),
        "amo": bool(data.get("amo", False)),
    }
