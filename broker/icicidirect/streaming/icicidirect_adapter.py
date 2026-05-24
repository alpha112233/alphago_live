"""ICICI Direct WebSocket adapter (Breeze live feed).

Implements the OpenAlgo ``BaseBrokerWebSocketAdapter`` contract using the
official ``breeze-connect`` Python SDK. If ``breeze-connect`` is not
installed, the adapter still imports cleanly (so the WS dispatch layer
doesn't crash on app boot) and surfaces a clear error message at
``connect`` time.

Why use breeze-connect rather than reimplement the protocol:
  * Breeze uses a Socket.IO transport with proprietary subscription
    payloads ("STK_FUT_OPT", token-list formatting). The official SDK
    has all of this baked in.
  * Tick parsing differs across modes (LTP vs quote vs depth-5). The
    SDK normalizes the message envelopes so we only need a thin
    output-shape converter.

Mode mapping mirrors the rest of the OpenAlgo adapters:
    mode=1  → LTP-only ticks
    mode=2  → quote/OHLCV ticks
    mode=3  → 5-level depth
"""
from __future__ import annotations

import os
import sys
import threading
from typing import Any, Dict, Optional

from utils.logging import get_logger

# Allow `websocket_proxy.*` imports from this depth.
sys.path.append(os.path.join(os.path.dirname(__file__), "../../../"))

from websocket_proxy.base_adapter import BaseBrokerWebSocketAdapter  # noqa: E402

from .icicidirect_mapping import IciciDirectCapabilityRegistry, IciciDirectExchangeMapper

logger = get_logger(__name__)


try:
    from breeze_connect import BreezeConnect  # type: ignore
    _BREEZE_AVAILABLE = True
except Exception:  # pragma: no cover — optional dep
    BreezeConnect = None  # type: ignore
    _BREEZE_AVAILABLE = False


class IcicidirectWebSocketAdapter(BaseBrokerWebSocketAdapter):
    """Live tick adapter backed by the Breeze SDK."""

    def __init__(self) -> None:
        super().__init__()
        self.broker_name = "icicidirect"
        self.lock = threading.Lock()
        self.connected = False
        self._breeze: Optional[Any] = None
        self._user_id: Optional[str] = None
        self._subscriptions: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(
        self, broker_name: str, user_id: str, auth_data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        self._user_id = user_id
        return {"status": "success", "broker": broker_name, "user_id": user_id}

    def connect(self) -> Dict[str, Any]:
        if not _BREEZE_AVAILABLE:
            msg = (
                "breeze-connect SDK is not installed. Add `breeze-connect` to "
                "the alphago_live image to enable ICICI live ticks. REST trading "
                "works without it."
            )
            logger.error(msg)
            return {"status": "error", "message": msg}

        try:
            from database.auth_db import get_auth_token

            auth_string = get_auth_token(self._user_id, "icicidirect")
            if not auth_string or auth_string.count(":::") < 2:
                return {"status": "error", "message": "ICICI auth_string missing/malformed"}
            session_token, app_key, secret_key = auth_string.split(":::", 2)

            self._breeze = BreezeConnect(api_key=app_key)
            self._breeze.generate_session(
                api_secret=secret_key, session_token=session_token
            )
            self._breeze.ws_connect()
            self._breeze.on_ticks = self._on_tick
            self.connected = True
            logger.info("ICICI Breeze WS connected")
            return {"status": "success"}
        except Exception as e:
            logger.exception("ICICI Breeze WS connect failed")
            return {"status": "error", "message": str(e)}

    def disconnect(self) -> Dict[str, Any]:
        try:
            if self._breeze is not None:
                self._breeze.ws_disconnect()
            self.connected = False
            return {"status": "success"}
        except Exception as e:  # pragma: no cover
            logger.exception("ICICI Breeze WS disconnect failed")
            return {"status": "error", "message": str(e)}

    def is_connected(self) -> bool:
        return self.connected

    # ------------------------------------------------------------------
    # Subscribe / unsubscribe
    # ------------------------------------------------------------------

    def subscribe(
        self,
        symbol: str,
        exchange: str,
        mode: int = 2,
        depth_level: int = 5,
    ) -> Dict[str, Any]:
        if not self.connected:
            return {"status": "error", "message": "ICICI WS not connected"}
        if not IciciDirectCapabilityRegistry.supports(mode):
            return {"status": "error", "message": f"Unsupported mode: {mode}"}

        try:
            br_exch = IciciDirectExchangeMapper.map(exchange)
            self._breeze.subscribe_feeds(
                exchange_code=br_exch,
                stock_code=symbol,
                product_type="cash" if exchange in ("NSE", "BSE") else "futures",
                get_market_depth=(mode == 3),
                get_exchange_quotes=(mode in (2, 3)),
            )
            self._subscriptions[f"{symbol}:{exchange}:{mode}"] = {
                "symbol": symbol,
                "exchange": exchange,
                "mode": mode,
            }
            return {"status": "success"}
        except Exception as e:  # pragma: no cover
            logger.exception(f"ICICI WS subscribe({symbol}, {exchange}) failed")
            return {"status": "error", "message": str(e)}

    def unsubscribe(self, symbol: str, exchange: str, mode: int = 2) -> Dict[str, Any]:
        if not self.connected:
            return {"status": "error", "message": "ICICI WS not connected"}
        try:
            br_exch = IciciDirectExchangeMapper.map(exchange)
            self._breeze.unsubscribe_feeds(exchange_code=br_exch, stock_code=symbol)
            self._subscriptions.pop(f"{symbol}:{exchange}:{mode}", None)
            return {"status": "success"}
        except Exception as e:  # pragma: no cover
            logger.exception(f"ICICI WS unsubscribe({symbol}, {exchange}) failed")
            return {"status": "error", "message": str(e)}

    def get_subscriptions(self) -> Dict[str, Any]:
        return {"status": "success", "subscriptions": list(self._subscriptions.values())}

    # ------------------------------------------------------------------
    # Tick handling
    # ------------------------------------------------------------------

    def _on_tick(self, tick: Dict[str, Any]) -> None:
        """Breeze tick -> OpenAlgo tick + publish via base adapter."""
        try:
            normalized = self._normalize_tick(tick)
            if normalized is None:
                return
            topic = (
                f"{normalized['exchange']}_{normalized['symbol']}_{normalized.get('mode_str', 'QUOTE')}"
            )
            self.publish_tick(topic, normalized)
        except Exception as e:  # pragma: no cover
            logger.exception(f"ICICI _on_tick failed: {e}")

    @staticmethod
    def _normalize_tick(tick: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(tick, dict):
            return None
        symbol = tick.get("symbol") or tick.get("stock_code")
        exchange = tick.get("exchange_code") or tick.get("exchange") or "NSE"
        if not symbol:
            return None
        return {
            "symbol": symbol,
            "exchange": exchange,
            "ltp": float(tick.get("last") or tick.get("close") or 0),
            "bid": float(tick.get("best_bid_price") or 0),
            "ask": float(tick.get("best_offer_price") or 0),
            "volume": int(float(tick.get("ltq") or tick.get("volume") or 0)),
            "high": float(tick.get("high") or 0),
            "low": float(tick.get("low") or 0),
            "open": float(tick.get("open") or 0),
            "close": float(tick.get("close") or 0),
            "timestamp": tick.get("ltt") or tick.get("datetime", ""),
            "mode_str": "DEPTH" if tick.get("depth") else "QUOTE",
        }
