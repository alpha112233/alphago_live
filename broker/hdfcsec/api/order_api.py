"""HDFC Securities order API — scaffolding only.

Trading endpoints stubbed with NotImplementedError; the full port
adapts ccxt-india/brokers/hdfc/hdfcsec.py (749 LOC) to OpenAlgo's
contract. See README.md for the roadmap.
"""
from __future__ import annotations


_NOT_READY = (
    "HDFC Securities trading endpoints land in follow-up PR. Scaffolding only "
    "in this release — see broker/hdfcsec/README.md."
)


def place_order_api(data, auth):
    raise NotImplementedError(_NOT_READY)


def place_smartorder_api(data, auth):
    raise NotImplementedError(_NOT_READY)


def modify_order(data, auth):
    raise NotImplementedError(_NOT_READY)


def cancel_order(orderid, auth):
    raise NotImplementedError(_NOT_READY)


def cancel_all_orders_api(data, auth):
    raise NotImplementedError(_NOT_READY)


def get_order_book(auth):
    return []


def get_trade_book(auth):
    return []


def get_positions(auth):
    return []


def get_holdings(auth):
    return []


def get_open_position(tradingsymbol, exchange, product, auth):
    return "0"


def get_order_status(orderid, auth):
    return {"status": "error", "message": _NOT_READY}
