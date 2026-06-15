"""Broker-agnostic market-data fallback for SANDBOX (Analyze) mode.

Some broker plugins are order-only and ship no market-data module (Arihant,
HDFC InvestRight, Motilal — none expose REST quotes). That makes paper /
Analyze mode unusable: the sandbox needs a live price to simulate a fill,
and the quote fetch crashes (ModuleNotFound) or 401s.

This module provides a credential-free quote source (Yahoo Finance v8 chart
endpoint) so paper trading works regardless of the execution broker. It is
ONLY used as a fallback in Analyze mode — live trading never serves these
quotes. Equity (NSE/BSE) is covered; F&O/other segments return None (the
caller then surfaces the original broker error).
"""

from __future__ import annotations

from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)

# OpenAlgo exchange → Yahoo suffix. Only cash-equity is mapped; F&O symbol
# conventions don't translate cleanly to Yahoo, so those fall through.
_YAHOO_SUFFIX = {"NSE": ".NS", "BSE": ".BO"}
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def supported(exchange: str) -> bool:
    return (exchange or "").upper() in _YAHOO_SUFFIX


def get_fallback_quote(symbol: str, exchange: str) -> dict | None:
    """Credential-free quote for sandbox simulation. Returns the standard
    OpenAlgo quote dict, or None if unavailable (caller keeps the original
    broker error). Never raises."""
    suffix = _YAHOO_SUFFIX.get((exchange or "").upper())
    if not suffix or not symbol:
        return None
    ysym = f"{symbol.upper().strip()}{suffix}"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}?interval=1m&range=1d"
    try:
        resp = get_httpx_client().get(url, headers={"User-Agent": _UA}, timeout=8)
        if resp.status_code != 200:
            logger.debug(f"fallback quote {ysym}: http {resp.status_code}")
            return None
        result = (resp.json().get("chart") or {}).get("result") or []
        if not result:
            return None
        meta = result[0].get("meta") or {}
        ltp = meta.get("regularMarketPrice")
        if ltp in (None, 0):
            return None
        prev_close = meta.get("previousClose") or meta.get("chartPreviousClose") or 0
        # Day open: first non-null open in the series, else prev_close.
        day_open = 0.0
        try:
            opens = ((result[0].get("indicators") or {}).get("quote") or [{}])[0].get("open") or []
            day_open = next((float(o) for o in opens if o is not None), 0.0)
        except Exception:
            pass
        q = {
            "ask": 0.0,
            "bid": 0.0,
            "high": float(meta.get("regularMarketDayHigh") or ltp),
            "low": float(meta.get("regularMarketDayLow") or ltp),
            "ltp": float(ltp),
            "open": float(day_open or prev_close or ltp),
            "prev_close": float(prev_close),
            "volume": int(meta.get("regularMarketVolume") or 0),
            "oi": 0,
        }
        logger.info(f"sandbox fallback quote {symbol} {exchange}: ltp={q['ltp']} (Yahoo)")
        return q
    except Exception as e:
        logger.debug(f"fallback quote {ysym} failed: {e}")
        return None
