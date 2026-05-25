"""OpenAlgo <-> HDFC symbol/format conversion.

HDFC uses its own ``security_id`` (numeric) for orders rather than the
NSE tradingsymbol; the lookup is via ``database.token_db.get_token``
after the master contract has been loaded.

Equity: OpenAlgo "RELIANCE" maps to HDFC's stored "RELIANCE-EQ" form;
HDFC accepts either, but for symbol uniqueness we keep an explicit
``-EQ`` strip on the OpenAlgo side.
"""
from __future__ import annotations

from utils.logging import get_logger

logger = get_logger(__name__)


def get_br_symbol(symbol: str, exchange: str) -> str:
    """OpenAlgo symbol -> HDFC tradingsymbol (pre-master-lookup)."""
    try:
        if exchange in ("NSE", "BSE") and symbol.endswith("-EQ"):
            return symbol[:-3]
        return symbol
    except Exception as e:  # pragma: no cover
        logger.error(f"Error converting OA->HDFC symbol {symbol}: {e}")
        return symbol


def get_oa_symbol(symbol: str, exchange: str) -> str:
    """HDFC tradingsymbol -> OpenAlgo symbol (post-master-lookup fallback)."""
    try:
        return symbol
    except Exception as e:  # pragma: no cover
        logger.error(f"Error converting HDFC->OA symbol {symbol}: {e}")
        return symbol
