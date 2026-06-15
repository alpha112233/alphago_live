"""Arihant TradeBridge market-data plugin.

Arihant exposes NO simple "get quote/LTP" REST endpoint — live ticks are
WebSocket-only (/marketdata, /market-stream). The REST surface for prices
is the chart/candle API:

    POST /wrapper-service/api/chart/v1/intraday-candle-data
    GET  /wrapper-service/api/chart/v1/historical-candle-data

So get_quotes() derives the LTP from the chart API: it pulls today's
candles and reads the latest close (= last traded price), with the day's
open/high/low/volume from the same series. Bid/ask/OI aren't available over
REST → returned as 0 (sandbox simulated-fill pricing only needs LTP; this
unblocks Analyze mode for Arihant, which previously crashed on the missing
`broker.arihant.api.data` module).

Request/response shapes confirmed from the TradeBridge docs bundle
(IntradayCandleDataRequest / OhlcData = [Open, High, Low, Close, Volume,
Interval]).

NOTE (2026-06-15 live probe vs adityaneo's session): endpoint, method and
request body are confirmed from the TradeBridge docs and pass the source
gate (source="SDK"). BUT a trading-only Arihant API key is rejected by the
chart endpoint with AU015 "Invalid API key" even though it works for
login/orders — Arihant gates market data behind a SEPARATE market-data API
entitlement. So this plugin needs a market-data-enabled key to actually
return quotes; the response-array parsing below is docs-derived (OhlcData =
[O,H,L,C,V,Interval]) and should be re-verified against a real 200 once such
a key exists.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from broker.arihant.api.order_api import _headers
from broker.arihant.baseurl import get_url
from broker.arihant.mapping.transform_data import _instrument_for_exchange
from database.token_db import get_br_symbol, get_token
from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)

_IST = timedelta(hours=5, minutes=30)
# Resolutions accepted by the chart API (string form, per docs example
# "1day"). Minute granularity gives a live LTP; "1day" is the fallback.
_INTRADAY_RES = "1"


def _now_ist() -> datetime:
    return datetime.utcnow() + _IST


def _iso(dt: datetime) -> str:
    # Arihant wants e.g. "2024-09-02T15:00:00.000" (no tz suffix, IST wall clock).
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000")


class BrokerData:
    def __init__(self, auth_token: str):
        self.auth = auth_token

    # -- internal: pull today's candle series for a symbol -----------------
    def _intraday_candles(self, symbol: str, exchange: str, resolution: str) -> list[list]:
        br_symbol = get_br_symbol(symbol, exchange) or symbol
        body = {
            "symbol": br_symbol,
            "resolution": resolution,
            "exc": (exchange or "NSE").upper(),
            "instrument": _instrument_for_exchange(exchange),
            "startTime": _iso(_now_ist().replace(hour=9, minute=0, second=0, microsecond=0)),
            "endTime": _iso(_now_ist()),
        }
        url = get_url("/wrapper-service/api/chart/v1/intraday-candle-data")
        client = get_httpx_client()
        resp = client.post(url, headers=_headers(self.auth), content=json.dumps(body), timeout=12)
        try:
            data = resp.json()
        except Exception:
            logger.error(f"Arihant candle: non-JSON ({resp.status_code}) {resp.text[:160]}")
            return []
        if str(data.get("infoID")) not in ("0", "200", "None"):
            # Surface auth/session errors to the caller (quotes_service logs it).
            logger.warning(f"Arihant candle infoID={data.get('infoID')} msg={data.get('infoMsg')}")
        # OhlcData.array = [[Open, High, Low, Close, Volume, Interval], ...].
        # The series sits under data{} — be defensive about the exact key.
        d = data.get("data") or {}
        candles = (
            d.get("ohlc") or d.get("candles") or d.get("array")
            or d.get("OhlcData") or (d if isinstance(d, list) else [])
        )
        if isinstance(candles, dict):
            candles = candles.get("array") or candles.get("ohlc") or []
        return candles if isinstance(candles, list) else []

    # -- public interface ---------------------------------------------------
    def get_quotes(self, symbol: str, exchange: str) -> dict:
        """LTP + day OHLCV derived from the chart API. bid/ask/oi = 0 (REST
        has no depth/OI). Raises on failure so quotes_service reports it."""
        candles = self._intraday_candles(symbol, exchange, _INTRADAY_RES)
        if not candles:
            # Fall back to the daily candle (today) for a close = current price.
            candles = self._intraday_candles(symbol, exchange, "1day")
        if not candles:
            raise Exception(f"Arihant: no candle data for {symbol} {exchange}")

        # Each candle: [Open, High, Low, Close, Volume, Interval]
        last = candles[-1]
        ltp = float(last[3])
        day_open = float(candles[0][0])
        day_high = max(float(c[1]) for c in candles)
        day_low = min(float(c[2]) for c in candles)
        day_vol = int(sum(float(c[4] or 0) for c in candles))
        # prev_close: the previous day's daily close (best-effort, one extra
        # call). Skip if it fails — sandbox doesn't require it.
        prev_close = 0.0
        try:
            prev_close = self._prev_close(symbol, exchange)
        except Exception:
            pass
        return {
            "ask": 0.0,
            "bid": 0.0,
            "high": day_high,
            "low": day_low,
            "ltp": ltp,
            "open": day_open,
            "prev_close": prev_close,
            "volume": day_vol,
            "oi": 0,
        }

    def _prev_close(self, symbol: str, exchange: str) -> float:
        br_symbol = get_br_symbol(symbol, exchange) or symbol
        end = _now_ist().replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=7)
        params = {
            "symbol": br_symbol,
            "resolution": "1day",
            "from": _iso(start),
            "to": _iso(end),
            "exc": (exchange or "NSE").upper(),
            "streamSym": f"{get_token(symbol, exchange)}_{(exchange or 'NSE').upper()}",
            "instrument": _instrument_for_exchange(exchange),
        }
        url = get_url("/wrapper-service/api/chart/v1/historical-candle-data")
        resp = get_httpx_client().get(url, headers=_headers(self.auth), params=params, timeout=12)
        d = (resp.json().get("data") or {})
        candles = d.get("ohlc") or d.get("candles") or d.get("array") or []
        if isinstance(candles, dict):
            candles = candles.get("array") or []
        return float(candles[-1][3]) if candles else 0.0

    def get_depth(self, symbol: str, exchange: str) -> dict:
        """Arihant has no REST depth (WebSocket-only). Return an LTP-only
        book so callers that expect the shape don't crash."""
        q = self.get_quotes(symbol, exchange)
        empty5 = [{"price": 0, "quantity": 0} for _ in range(5)]
        return {
            "asks": empty5, "bids": empty5,
            "high": q["high"], "low": q["low"], "ltp": q["ltp"],
            "open": q["open"], "prev_close": q["prev_close"],
            "volume": q["volume"], "oi": 0, "totalbuyqty": 0, "totalsellqty": 0,
        }

    def get_history(self, symbol: str, exchange: str, interval: str,
                    start_date: str, end_date: str):
        """Historical candles via the chart API. interval e.g. '1m','D'."""
        import pandas as pd
        res = "1day" if interval.upper() in ("D", "1D", "1DAY", "DAY") else interval.rstrip("m") or "1"
        br_symbol = get_br_symbol(symbol, exchange) or symbol
        params = {
            "symbol": br_symbol, "resolution": res,
            "from": f"{start_date}T00:00:00.000", "to": f"{end_date}T23:59:59.000",
            "exc": (exchange or "NSE").upper(),
            "streamSym": f"{get_token(symbol, exchange)}_{(exchange or 'NSE').upper()}",
            "instrument": _instrument_for_exchange(exchange),
        }
        url = get_url("/wrapper-service/api/chart/v1/historical-candle-data")
        resp = get_httpx_client().get(url, headers=_headers(self.auth), params=params, timeout=20)
        d = (resp.json().get("data") or {})
        candles = d.get("ohlc") or d.get("candles") or d.get("array") or []
        if isinstance(candles, dict):
            candles = candles.get("array") or []
        rows = [{"open": float(c[0]), "high": float(c[1]), "low": float(c[2]),
                 "close": float(c[3]), "volume": int(float(c[4] or 0)),
                 "timestamp": c[5] if len(c) > 5 else None} for c in candles]
        return pd.DataFrame(rows)
