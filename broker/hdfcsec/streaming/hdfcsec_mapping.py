"""HDFC Securities WS exchange / mode mapping."""
from __future__ import annotations

from typing import Dict


class HdfcsecExchangeMapper:
    EXCHANGE_MAP: Dict[str, str] = {
        "NSE": "NSE", "BSE": "BSE",
        "NFO": "NFO", "BFO": "BFO",
        "CDS": "CDS", "MCX": "MCX",
        "NSE_INDEX": "NSE", "BSE_INDEX": "BSE",
    }

    @classmethod
    def map(cls, exchange: str) -> str:
        return cls.EXCHANGE_MAP.get(exchange, exchange)


class HdfcsecCapabilityRegistry:
    """Modes that the HDFC stream feed exposes.

    1 = LTP only, 2 = Quote, 3 = Depth-5.
    """

    SUPPORTED_MODES = {1, 2}

    @classmethod
    def supports(cls, mode: int) -> bool:
        return mode in cls.SUPPORTED_MODES
