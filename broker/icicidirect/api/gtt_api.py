"""ICICI Direct GTT (Good-Till-Triggered) order API.

Two flavours, mirroring the Breeze SDK:

  * Single-leg GTT — one trigger price, one execution leg.
    Endpoint: ``/breezeapi/api/v1/gttorder``
              POST place, PUT modify, DELETE cancel.

  * Three-leg GTT — entry + target + stoploss (OCO).
    Endpoint: ``/breezeapi/api/v1/gttthreelegorder``
              Body carries an ``order_details`` array with three legs.

Reference: ccxt-india/brokers/icici/icici.py:1026-1376.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from broker.icicidirect.api.breeze_http import request as breeze_request
from broker.icicidirect.baseurl import (
    GTT_BOOK_URL,
    GTT_THREE_LEG_URL,
    GTT_URL,
)
from broker.icicidirect.mapping.order_data import (
    extract_error_message,
    extract_order_id,
)
from broker.icicidirect.mapping.transform_data import (
    map_action,
    map_exchange,
    map_product_type,
    map_validity,
)
from utils.logging import get_logger

logger = get_logger(__name__)


def _build_single_leg_body(data: Dict[str, Any]) -> Dict[str, Any]:
    """OpenAlgo GTT input -> Breeze single-leg body."""
    exchange = data.get("exchange", "NSE")
    return {
        "stock_code": data.get("symbol"),
        "exchange_code": map_exchange(exchange),
        "product": map_product_type(data.get("product", "CNC"), exchange),
        "action": map_action(data.get("action", "BUY")),
        "order_type": "limit",
        "quantity": str(data.get("quantity", "0")),
        "price": str(data.get("price", "0")),
        "trigger_price": str(data.get("trigger_price", "0")),
        "validity": map_validity(data.get("validity", "DAY")),
        "stoploss": str(data.get("stoploss", "0")),
        "disclosed_quantity": str(data.get("disclosed_quantity", "0")),
    }


def _build_three_leg_body(data: Dict[str, Any]) -> Dict[str, Any]:
    """OpenAlgo OCO GTT -> Breeze three-leg body.

    Required keys in `data`: symbol, exchange, action, quantity, product,
    entry_price, entry_trigger, target_price, target_trigger,
    stoploss_price, stoploss_trigger.
    """
    exchange = data.get("exchange", "NSE")
    action = data.get("action", "BUY")
    qty = str(data.get("quantity", "0"))

    def leg(kind: str, price: Any, trigger: Any, leg_action: str) -> Dict[str, Any]:
        return {
            "gtt_type": kind,            # "entry" | "target" | "stoploss"
            "action": map_action(leg_action),
            "order_type": "limit",
            "price": str(price),
            "trigger_price": str(trigger),
            "quantity": qty,
            "validity": "day",
        }

    counter = "SELL" if action.upper() == "BUY" else "BUY"

    return {
        "stock_code": data.get("symbol"),
        "exchange_code": map_exchange(exchange),
        "product": map_product_type(data.get("product", "CNC"), exchange),
        "order_details": [
            leg("entry", data.get("entry_price"), data.get("entry_trigger"), action),
            leg("target", data.get("target_price"), data.get("target_trigger"), counter),
            leg("stoploss", data.get("stoploss_price"), data.get("stoploss_trigger"), counter),
        ],
    }


def place_gtt_order(data: Dict[str, Any], auth: str) -> Tuple[Dict[str, Any], int]:
    """Place a single-leg GTT. Returns (response, status_code)."""
    try:
        body = _build_single_leg_body(data)
        raw = breeze_request("POST", GTT_URL, auth, payload=body)
        status = raw.get("Status") if isinstance(raw, dict) else None
        if status in (200, "200"):
            return {"status": "success", "gtt_id": extract_order_id(raw)}, 200
        return {"status": "error", "message": extract_error_message(raw)}, int(status or 400)
    except Exception as e:
        logger.exception("ICICI place_gtt_order exception")
        return {"status": "error", "message": str(e)}, 500


def place_oco_gtt_order(data: Dict[str, Any], auth: str) -> Tuple[Dict[str, Any], int]:
    """Place a three-leg (entry + target + stoploss) OCO GTT."""
    try:
        body = _build_three_leg_body(data)
        raw = breeze_request("POST", GTT_THREE_LEG_URL, auth, payload=body)
        status = raw.get("Status") if isinstance(raw, dict) else None
        if status in (200, "200"):
            return {"status": "success", "gtt_id": extract_order_id(raw)}, 200
        return {"status": "error", "message": extract_error_message(raw)}, int(status or 400)
    except Exception as e:
        logger.exception("ICICI place_oco_gtt_order exception")
        return {"status": "error", "message": str(e)}, 500


def modify_gtt_order(data: Dict[str, Any], auth: str) -> Tuple[Dict[str, Any], int]:
    try:
        body = _build_single_leg_body(data)
        body["gtt_order_id"] = data.get("gtt_id") or data.get("orderid")
        raw = breeze_request("PUT", GTT_URL, auth, payload=body)
        status = raw.get("Status") if isinstance(raw, dict) else None
        if status in (200, "200"):
            return {"status": "success", "gtt_id": body["gtt_order_id"]}, 200
        return {"status": "error", "message": extract_error_message(raw)}, int(status or 400)
    except Exception as e:
        logger.exception("ICICI modify_gtt_order exception")
        return {"status": "error", "message": str(e)}, 500


def cancel_gtt_order(gtt_id: str, auth: str) -> Tuple[Dict[str, Any], int]:
    try:
        payload = {"gtt_order_id": str(gtt_id)}
        raw = breeze_request("DELETE", GTT_URL, auth, payload=payload)
        status = raw.get("Status") if isinstance(raw, dict) else None
        if status in (200, "200"):
            return {"status": "success", "gtt_id": str(gtt_id)}, 200
        return {"status": "error", "message": extract_error_message(raw)}, int(status or 400)
    except Exception as e:
        logger.exception("ICICI cancel_gtt_order exception")
        return {"status": "error", "message": str(e)}, 500


def get_gtt_orderbook(auth: str) -> List[Dict[str, Any]]:
    try:
        raw = breeze_request("GET", GTT_BOOK_URL, auth, payload={})
        rows = (raw or {}).get("Success") or []
        if isinstance(rows, dict):
            rows = [rows]
        return rows
    except Exception as e:
        logger.exception(f"ICICI get_gtt_orderbook exception: {e}")
        return []
