"""ICICI Direct WS exchange / mode mapping.

Mirrors the small helper structure used by the definedge / zerodha
streaming modules so the WS dispatch layer can introspect supported
exchanges + tick modes.
"""
from __future__ import annotations

from typing import Dict


class IciciDirectExchangeMapper:
    """OpenAlgo exchange -> Breeze WS exchange code."""

    EXCHANGE_MAP: Dict[str, str] = {
        "NSE": "NSE",
        "BSE": "BSE",
        "NFO": "NFO",
        "BFO": "BFO",
        "CDS": "NSE",
        "MCX": "MCX",
        "NSE_INDEX": "NSE",
        "BSE_INDEX": "BSE",
    }

    @classmethod
    def map(cls, exchange: str) -> str:
        return cls.EXCHANGE_MAP.get(exchange, exchange)


class IciciDirectCapabilityRegistry:
    """Modes supported by the ICICI Breeze WebSocket feed.

    1 = LTP only
    2 = Quote (OHLCV + LTP)
    3 = Market depth (top 5)
    """

    SUPPORTED_MODES = {1, 2, 3}

    @classmethod
    def supports(cls, mode: int) -> bool:
        return mode in cls.SUPPORTED_MODES
