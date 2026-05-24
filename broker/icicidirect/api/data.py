"""ICICI Direct market-data API (Breeze quotes + historical).

Breeze's quote endpoint (`/breezeapi/api/v1/quotes`) returns a single
row per (stock_code, exchange_code) pair. F&O quotes additionally need
expiry_date, strike_price and right.

Historical OHLCV lives at `/breezeapi/api/v1/historicalcharts` — same
header set, but the response uses `success: [{...}]` (lowercase) per the
SDK; we tolerate both casings.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

from broker.icicidirect.api.breeze_http import request as breeze_request
from broker.icicidirect.baseurl import HIST_URL, QUOTES_URL
from broker.icicidirect.mapping.transform_data import (
    map_exchange,
    split_fno_components,
)
from utils.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Auth re-export for OpenAlgo's data-broker contract
# ---------------------------------------------------------------------------

def authenticate_broker(api_token, api_secret, otp=None):
    """Compat wrapper. OpenAlgo's data layer imports authenticate_broker
    from api/data.py for some brokers; we forward to api/auth_api.py.
    """
    from broker.icicidirect.api.auth_api import authenticate_broker as _auth
    return _auth(api_token, app_key=api_token, secret_key=api_secret)


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------

def _build_quote_payload(symbol: str, exchange: str) -> Dict[str, str]:
    """Build the GET-/quotes body. Equity needs (stock_code, exchange_code);
    F&O additionally needs expiry/strike/right."""
    from database.token_db import get_br_symbol

    payload = {"exchange_code": map_exchange(exchange)}

    root, fno = split_fno_components(symbol)
    if fno is not None and exchange in ("NFO", "BFO"):
        payload["stock_code"] = root
        payload["expiry_date"] = fno["expiry"]
        payload["product_type"] = fno["product_kind"]
        if fno["right"] != "others":
            payload["right"] = fno["right"]
            payload["strike_price"] = fno["strike_price"]
    else:
        try:
            payload["stock_code"] = get_br_symbol(symbol, exchange) or symbol
        except Exception:
            payload["stock_code"] = symbol

    return payload


def get_quotes(symbol: str, exchange: str, auth_token: str) -> Dict[str, Any]:
    """Return a normalized quote dict for one symbol."""
    try:
        payload = _build_quote_payload(symbol, exchange)
        raw = breeze_request("GET", QUOTES_URL, auth_token, payload=payload)
        status = raw.get("Status") if isinstance(raw, dict) else None
        if status not in (200, "200"):
            return {"status": "error", "message": (raw or {}).get("Error", "Quote fetch failed")}

        rows = (raw or {}).get("Success") or []
        if isinstance(rows, dict):
            rows = [rows]
        if not rows:
            return {"status": "error", "message": "No quote rows returned"}

        q = rows[0]
        return {
            "symbol": symbol,
            "exchange": exchange,
            "ltp": float(q.get("ltp") or q.get("last") or 0),
            "open": float(q.get("open") or 0),
            "high": float(q.get("high") or 0),
            "low": float(q.get("low") or 0),
            "close": float(q.get("previous_close") or q.get("close") or 0),
            "volume": int(float(q.get("total_quantity_traded") or q.get("volume") or 0)),
            "best_bid_price": float(q.get("best_bid_price") or 0),
            "best_ask_price": float(q.get("best_offer_price") or q.get("best_ask_price") or 0),
            "bid_qty": int(float(q.get("best_bid_quantity") or 0)),
            "ask_qty": int(float(q.get("best_offer_quantity") or 0)),
            "timestamp": q.get("ltt") or q.get("last_traded_time") or "",
        }
    except Exception as e:
        logger.exception(f"ICICI get_quotes({symbol}, {exchange}) failed")
        return {"status": "error", "message": str(e)}


def _get_ltp_for_symbol(symbol: str, exchange: str, auth_token: str) -> Optional[float]:
    """Internal helper used by order_api for MARKET->limit conversion.

    Returns None on any failure so the caller can fall back gracefully.
    """
    try:
        q = get_quotes(symbol, exchange, auth_token)
        if isinstance(q, dict) and q.get("ltp") and q["ltp"] > 0:
            return float(q["ltp"])
    except Exception as e:
        logger.warning(f"ICICI _get_ltp_for_symbol({symbol}) failed: {e}")
    return None


# ---------------------------------------------------------------------------
# Historical OHLCV
# ---------------------------------------------------------------------------

_INTERVAL_MAP = {
    "1m": "1minute", "1minute": "1minute",
    "5m": "5minute", "5minute": "5minute",
    "30m": "30minute", "30minute": "30minute",
    "1d": "1day", "day": "1day", "D": "1day", "1day": "1day",
}


def get_history(
    symbol: str,
    exchange: str,
    interval: str,
    start_date: str,
    end_date: str,
    auth_token: str,
) -> pd.DataFrame:
    """OHLCV history. Inputs accept ISO-date or YYYY-MM-DD."""
    try:
        breeze_interval = _INTERVAL_MAP.get(interval, "1day")
        payload = _build_quote_payload(symbol, exchange)
        payload.update(
            interval=breeze_interval,
            from_date=_to_iso(start_date),
            to_date=_to_iso(end_date),
        )
        raw = breeze_request("GET", HIST_URL, auth_token, payload=payload, timeout=30)
        rows = (raw or {}).get("Success") or (raw or {}).get("success") or []
        if isinstance(rows, dict):
            rows = [rows]
        if not rows:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        df = pd.DataFrame(rows)
        ren = {
            "datetime": "timestamp", "date": "timestamp", "time": "timestamp",
            "vol": "volume", "total_quantity_traded": "volume",
        }
        df = df.rename(columns={k: v for k, v in ren.items() if k in df.columns})
        for c in ("open", "high", "low", "close", "volume"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        keep = [c for c in ("timestamp", "open", "high", "low", "close", "volume") if c in df.columns]
        return df[keep].sort_values("timestamp").reset_index(drop=True)
    except Exception as e:
        logger.exception(f"ICICI get_history({symbol}, {interval}) failed: {e}")
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])


def _to_iso(d: str) -> str:
    """Normalize a date/datetime string to ISO-8601 with T00:00:00.000Z."""
    if not d:
        return d
    if "T" in d:
        return d
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%Y-%m-%dT00:00:00.000Z")
    except ValueError:
        return d


# ---------------------------------------------------------------------------
# Market-depth — Breeze has no separate depth endpoint; surface what /quotes returns
# ---------------------------------------------------------------------------

def get_depth(symbol: str, exchange: str, auth_token: str) -> Dict[str, Any]:
    """Return a single-level depth from /quotes (Breeze does not expose 5-level depth via REST)."""
    q = get_quotes(symbol, exchange, auth_token)
    if "status" in q and q["status"] == "error":
        return q
    return {
        "symbol": symbol,
        "exchange": exchange,
        "bids": [{"price": q["best_bid_price"], "qty": q["bid_qty"], "orders": 0}],
        "asks": [{"price": q["best_ask_price"], "qty": q["ask_qty"], "orders": 0}],
        "ltp": q["ltp"],
        "timestamp": q["timestamp"],
    }
