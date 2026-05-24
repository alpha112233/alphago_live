"""ICICI Direct order API — scaffolding.

Trading endpoints (place / modify / cancel / book / positions / holdings)
are stubbed with NotImplementedError. They land in the follow-up PR once
the ccxt-india/brokers/icici/icici.py port is reviewed end-to-end.

The OpenAlgo order service catches NotImplementedError and surfaces a
clear "broker integration in progress" message to the customer, so this
scaffold is safe to land — it gets the broker visible in the UI without
risking any actual trades.
"""
from __future__ import annotations


_NOT_READY = (
    "ICICI Direct trading endpoints land in follow-up PR. Scaffolding only "
    "in this release — see broker/icicidirect/README.md."
)


def _stub(*_, **__):
    raise NotImplementedError(_NOT_READY)


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
