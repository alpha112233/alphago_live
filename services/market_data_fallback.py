"""Broker-agnostic market-data fallback for SANDBOX (Analyze) mode.

Some broker plugins are order-only and ship no market-data module (Arihant,
HDFC InvestRight, Motilal — none expose REST quotes). That makes paper /
Analyze mode unusable: the sandbox needs a live price to simulate a fill, and
the quote fetch crashes (ModuleNotFound) or 401s.

This module provides a credential-free quote source so paper trading works
regardless of the execution broker. It is ONLY used as a fallback in Analyze
mode — live trading never serves these quotes.

Coverage:
  • Cash equity (NSE/BSE)  → Yahoo Finance v8 chart (real LTP).
  • F&O (NFO/BFO) options  → theoretical price: underlying spot from Yahoo +
                             Black-Scholes (default IV by class). Approximate,
                             but lets a sandbox MARKET order simulate instead
                             of failing "unable to fetch current price".
  • F&O futures            → underlying spot (≈ futures for a sandbox sim).

The contract details (underlying / strike / expiry, and CE/PE/FUT) come from
the container's own symtoken master; pricing depends only on Yahoo for the
underlying spot (no broker key, no fragile option-chain scraping).
"""

from __future__ import annotations

import math
from datetime import date, datetime

from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)

# OpenAlgo exchange → Yahoo suffix (cash equity only).
_YAHOO_SUFFIX = {"NSE": ".NS", "BSE": ".BO"}
_FNO_EXCHANGES = {"NFO", "BFO"}

# Underlying → Yahoo spot ticker for index F&O. Stock F&O falls back to "<name>.NS".
_UNDERLYING_YAHOO = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "MIDCPNIFTY": "^NSEMDCP50",
    "NIFTYNXT50": "^NSMIDCP",
    "SENSEX": "^BSESN",
    "BANKEX": "BSE-BANK.BO",
}
_INDEX_UNDERLYINGS = set(_UNDERLYING_YAHOO)

_RISK_FREE = 0.065
_DEFAULT_IV_INDEX = 0.13
_DEFAULT_IV_STOCK = 0.30

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def supported(exchange: str) -> bool:
    e = (exchange or "").upper()
    return e in _YAHOO_SUFFIX or e in _FNO_EXCHANGES


def get_fallback_quote(symbol: str, exchange: str) -> dict | None:
    """Credential-free quote for sandbox simulation. Returns the standard
    OpenAlgo quote dict, or None if unavailable. Never raises."""
    e = (exchange or "").upper()
    if not symbol:
        return None
    try:
        if e in _YAHOO_SUFFIX:
            return _equity_quote(symbol, e)
        if e in _FNO_EXCHANGES:
            return _fno_quote(symbol, e)
    except Exception as ex:  # pragma: no cover — fallback must never raise
        logger.debug(f"fallback quote {symbol} {exchange} failed: {ex}")
    return None


# ---- equity (Yahoo) --------------------------------------------------------

def _equity_quote(symbol: str, exchange: str) -> dict | None:
    ysym = f"{symbol.upper().strip()}{_YAHOO_SUFFIX[exchange]}"
    q = _yahoo_quote(ysym)
    if q:
        logger.info(f"sandbox fallback quote {symbol} {exchange}: ltp={q['ltp']} (Yahoo)")
    return q


def _yahoo_quote(yticker: str) -> dict | None:
    """Full quote dict for a Yahoo ticker, or None."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yticker}?interval=1m&range=1d"
    resp = get_httpx_client().get(url, headers={"User-Agent": _UA}, timeout=8)
    if resp.status_code != 200:
        logger.debug(f"yahoo {yticker}: http {resp.status_code}")
        return None
    result = (resp.json().get("chart") or {}).get("result") or []
    if not result:
        return None
    meta = result[0].get("meta") or {}
    ltp = meta.get("regularMarketPrice")
    if ltp in (None, 0):
        return None
    prev_close = meta.get("previousClose") or meta.get("chartPreviousClose") or 0
    day_open = 0.0
    try:
        opens = ((result[0].get("indicators") or {}).get("quote") or [{}])[0].get("open") or []
        day_open = next((float(o) for o in opens if o is not None), 0.0)
    except Exception:
        pass
    return {
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


def _underlying_spot(underlying: str) -> float | None:
    yt = _UNDERLYING_YAHOO.get(underlying) or f"{underlying}.NS"
    q = _yahoo_quote(yt)
    return float(q["ltp"]) if q else None


# ---- F&O (theoretical) -----------------------------------------------------

def _lookup_contract(symbol: str, exchange: str):
    """(underlying, expiry_raw, strike) from the container's symtoken, or None."""
    from database.symbol import SymToken, db_session
    row = (
        db_session.query(SymToken)
        .filter_by(symbol=symbol.upper().strip(), exchange=exchange.upper())
        .first()
    )
    if row is None:
        return None
    return (row.name or "").upper(), row.expiry, row.strike


def _years_to_expiry(expiry_raw) -> float:
    if not expiry_raw:
        return 7 / 365.0
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d/%m/%Y", "%d-%m-%Y", "%d%b%y"):
        try:
            d = datetime.strptime(str(expiry_raw), fmt).date()
            days = max(1, (d - date.today()).days)
            return days / 365.0
        except ValueError:
            continue
    return 7 / 365.0


def _bs_price(S: float, K: float, T: float, r: float, sigma: float, is_call: bool) -> float:
    """Black-Scholes European option price."""
    if S <= 0 or K <= 0:
        return 0.0
    if T <= 0 or sigma <= 0:
        return max(0.0, (S - K) if is_call else (K - S))  # intrinsic
    from scipy.stats import norm
    d1 = (math.log(S / K) + (r + sigma * sigma / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if is_call:
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def _quote_dict(px: float) -> dict:
    px = round(max(float(px), 0.05), 2)
    return {"ask": 0.0, "bid": 0.0, "high": px, "low": px, "ltp": px,
            "open": px, "prev_close": px, "volume": 0, "oi": 0}


def _fno_quote(symbol: str, exchange: str) -> dict | None:
    s = symbol.upper().strip()
    contract = _lookup_contract(s, exchange)
    if contract is None:
        logger.debug(f"fno fallback: {s} {exchange} not in symtoken")
        return None
    underlying, expiry_raw, strike = contract
    if not underlying:
        return None
    spot = _underlying_spot(underlying)
    if not spot:
        logger.debug(f"fno fallback: no underlying spot for {underlying}")
        return None

    if s.endswith("FUT"):
        # Futures track spot closely enough for a sandbox simulation.
        logger.info(f"sandbox fallback FUT {s}: ~spot {spot} ({underlying})")
        return _quote_dict(spot)

    if s.endswith("CE") or s.endswith("PE"):
        is_call = s.endswith("CE")
        T = _years_to_expiry(expiry_raw)
        iv = _DEFAULT_IV_INDEX if underlying in _INDEX_UNDERLYINGS else _DEFAULT_IV_STOCK
        px = _bs_price(spot, float(strike or 0), T, _RISK_FREE, iv, is_call)
        logger.info(
            f"sandbox fallback OPT {s}: BS px={px:.2f} (spot={spot}, K={strike}, "
            f"T={T:.3f}, iv={iv}) — theoretical"
        )
        return _quote_dict(px)

    return None
