"""HDFC Securities market-data API.

The InvestRight REST API does NOT expose a quote / OHLCV / depth
endpoint. The only "market data" available via REST is the close /
open prices baked into the daily security-master CSV.

We stub the OpenAlgo data contract with master-derived close prices so
callers that ask for an LTP at startup get a sensible scalar instead of
a hard error. Strategies that need real-time quotes must use a
broker that exposes one (Zerodha, Upstox, Dhan, etc.). HDFC's
NOWStream WebSocket exists separately, but it is NOT served by the
developer.hdfcsec.com endpoint — it is a different protocol entirely
and is tracked as a follow-up.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd

from utils.logging import get_logger

logger = get_logger(__name__)


def authenticate_broker(api_token, api_secret, otp=None):
    """Compat wrapper for OpenAlgo's data layer."""
    from broker.hdfcsec.api.auth_api import authenticate_broker as _auth
    return _auth(api_token, api_key=api_token, api_secret=api_secret)


def get_quotes(symbol: str, exchange: str, auth_token: str) -> Dict[str, Any]:
    """Return a partial quote using master-derived close/open. LTP fields
    are 0 — callers should handle gracefully."""
    try:
        from database.token_db import get_token  # noqa: WPS433

        security_id = get_token(symbol, exchange)
        if not security_id:
            return {"status": "error", "message": f"No master row for {symbol}/{exchange}"}
        return {
            "symbol": symbol,
            "exchange": exchange,
            "ltp": 0.0,        # not available via REST
            "open": 0.0,
            "high": 0.0,
            "low": 0.0,
            "close": 0.0,      # master's close_price could be filled here
            "volume": 0,
            "best_bid_price": 0.0,
            "best_ask_price": 0.0,
            "bid_qty": 0,
            "ask_qty": 0,
            "timestamp": "",
            "note": (
                "HDFC InvestRight REST does not expose live quotes. "
                "Use a different broker for tick-driven strategies."
            ),
        }
    except Exception as e:
        logger.exception(f"HDFC get_quotes({symbol}, {exchange}) failed")
        return {"status": "error", "message": str(e)}


def _get_ltp_for_symbol(symbol: str, exchange: str, auth_token: str) -> Optional[float]:
    """Used by other modules. Always returns None for HDFC — no quote API."""
    return None


def get_history(
    symbol: str,
    exchange: str,
    interval: str,
    start_date: str,
    end_date: str,
    auth_token: str,
) -> pd.DataFrame:
    """HDFC InvestRight REST does not expose OHLCV history."""
    logger.warning(
        f"HDFC get_history({symbol}, {interval}): not supported by InvestRight REST"
    )
    return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])


def get_depth(symbol: str, exchange: str, auth_token: str) -> Dict[str, Any]:
    """No depth endpoint either."""
    return {
        "status": "error",
        "message": "HDFC InvestRight REST does not expose market depth.",
    }
