"""HDFC Securities live WebSocket adapter (NOWStream).

The InvestRight REST surface does not expose a quote endpoint; HDFC's
real-time stream is the separate **NOWStream** product, which uses a
different host, authentication path, and binary tick protocol.

This adapter scaffolds the OpenAlgo ``BaseBrokerWebSocketAdapter``
contract so the WS dispatch layer loads cleanly on app boot. Calling
:meth:`connect` returns a clear "not implemented" message until the
NOWStream binary protocol is ported (tracked as a separate follow-up —
the REST trading port is the unlock for hostingsol allowlist; live
ticks can come later via a polling daemon backed by a different
broker's data feed).
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, Optional

from utils.logging import get_logger

sys.path.append(os.path.join(os.path.dirname(__file__), "../../../"))

from websocket_proxy.base_adapter import BaseBrokerWebSocketAdapter  # noqa: E402

from .hdfcsec_mapping import HdfcsecCapabilityRegistry, HdfcsecExchangeMapper

logger = get_logger(__name__)


_NOT_IMPLEMENTED = (
    "HDFC Securities live ticks (NOWStream) is a separate follow-up. The "
    "REST trading surface — orders, positions, holdings, funds, master — "
    "is fully ported; strategies that need real-time quotes should "
    "subscribe via a different broker's WS feed."
)


class HdfcsecWebSocketAdapter(BaseBrokerWebSocketAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.broker_name = "hdfcsec"
        self.connected = False
        self._subscriptions: Dict[str, Dict[str, Any]] = {}

    def initialize(
        self, broker_name: str, user_id: str, auth_data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return {"status": "success", "broker": broker_name, "user_id": user_id}

    def connect(self) -> Dict[str, Any]:
        logger.warning(_NOT_IMPLEMENTED)
        return {"status": "error", "message": _NOT_IMPLEMENTED}

    def disconnect(self) -> Dict[str, Any]:
        self.connected = False
        return {"status": "success"}

    def is_connected(self) -> bool:
        return self.connected

    def subscribe(
        self,
        symbol: str,
        exchange: str,
        mode: int = 2,
        depth_level: int = 5,
    ) -> Dict[str, Any]:
        if not HdfcsecCapabilityRegistry.supports(mode):
            return {"status": "error", "message": f"Unsupported mode: {mode}"}
        # Park the subscription so a future NOWStream port can pick it up.
        self._subscriptions[f"{symbol}:{exchange}:{mode}"] = {
            "symbol": symbol,
            "exchange": HdfcsecExchangeMapper.map(exchange),
            "mode": mode,
        }
        return {"status": "error", "message": _NOT_IMPLEMENTED}

    def unsubscribe(self, symbol: str, exchange: str, mode: int = 2) -> Dict[str, Any]:
        self._subscriptions.pop(f"{symbol}:{exchange}:{mode}", None)
        return {"status": "success"}

    def get_subscriptions(self) -> Dict[str, Any]:
        return {"status": "success", "subscriptions": list(self._subscriptions.values())}
