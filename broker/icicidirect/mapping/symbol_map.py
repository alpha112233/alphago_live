"""OpenAlgo <-> ICICI Breeze symbol-format conversion.

Breeze does not use the NSE tradingsymbol. Its `stock_code` is the
ICICI-internal `ShortName` (e.g. RELIND for NSE:RELIANCE). The actual
translation lives in `database.token_db.get_br_symbol` once the master
contract has been downloaded.

For F&O the Breeze API does not accept a packed tradingsymbol — strike,
expiry and right are separate fields. Those are unpacked in
`mapping.transform_data._decode_fno_symbol`; here we just strip the
exchange-side -EQ suffix and return the OpenAlgo symbol as-is so the
caller can look it up in the master.
"""
from __future__ import annotations

from utils.logging import get_logger

logger = get_logger(__name__)


def get_br_symbol(symbol: str, exchange: str) -> str:
    """OpenAlgo symbol -> Breeze-compatible symbol (pre-lookup).

    The database has the authoritative mapping; this is the fallback for
    pure-equity symbols that may carry an -EQ suffix.
    """
    try:
        if exchange in ("NSE", "BSE") and symbol.endswith("-EQ"):
            return symbol[:-3]
        return symbol
    except Exception as e:  # pragma: no cover — defensive
        logger.error(f"Error converting OA->Breeze symbol {symbol}: {e}")
        return symbol


def get_oa_symbol(symbol: str, exchange: str) -> str:
    """Breeze stock_code -> OpenAlgo symbol (post-lookup fallback).

    For NSE equities the OpenAlgo convention is the bare tradingsymbol
    (no -EQ suffix); for derivatives we return as-is since the master
    table holds the packed OpenAlgo form.
    """
    try:
        if exchange == "NSE" and not any(t in symbol for t in ("FUT", "CE", "PE")):
            return symbol
        return symbol
    except Exception as e:  # pragma: no cover
        logger.error(f"Error converting Breeze->OA symbol {symbol}: {e}")
        return symbol
