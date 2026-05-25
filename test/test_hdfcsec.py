"""Unit tests for the hdfcsec broker port.

Pure-function tests that do not touch the InvestRight API.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Auth string handling
# ---------------------------------------------------------------------------

def test_parse_auth_round_trips():
    from broker.hdfcsec.api.hdfc_http import parse_auth

    s, k, sec = parse_auth("at:::ck:::cs")
    assert (s, k, sec) == ("at", "ck", "cs")


def test_parse_auth_rejects_malformed():
    from broker.hdfcsec.api.hdfc_http import parse_auth

    with pytest.raises(ValueError):
        parse_auth("")
    with pytest.raises(ValueError):
        parse_auth("only-one-segment")
    with pytest.raises(ValueError):
        parse_auth("two:::segments")


# ---------------------------------------------------------------------------
# Enum mappings
# ---------------------------------------------------------------------------

def test_map_action_title_case():
    from broker.hdfcsec.mapping.transform_data import map_action

    assert map_action("BUY") == "Buy"
    assert map_action("sell") == "Sell"
    assert map_action(" Sell ") == "Sell"


def test_map_product_equity():
    from broker.hdfcsec.mapping.transform_data import map_product_type

    assert map_product_type("CNC", "NSE") == "DELIVERY"
    assert map_product_type("MIS", "NSE") == "INTRADAY"
    assert map_product_type("MTF", "BSE") == "MTF"


def test_map_product_fno():
    from broker.hdfcsec.mapping.transform_data import map_product_type

    assert map_product_type("MIS", "NFO") == "INTRADAY"
    assert map_product_type("NRML", "NFO") == "OVERNIGHT"
    assert map_product_type("CNC", "BFO") == "OVERNIGHT"


def test_reverse_map_exchange_via_segment():
    from broker.hdfcsec.mapping.transform_data import reverse_map_exchange

    assert reverse_map_exchange("NSE", "EQUITY") == "NSE"
    assert reverse_map_exchange("NSE", "FUTSTK") == "NFO"
    assert reverse_map_exchange("BSE", "OPTSTK") == "BFO"
    assert reverse_map_exchange("NSE", "FUTCUR") == "CDS"
    assert reverse_map_exchange("MCX", "FUTCOM") == "MCX"


def test_map_validity_and_price_type():
    from broker.hdfcsec.mapping.transform_data import map_price_type, map_validity

    assert map_validity("DAY") == "DAY"
    assert map_validity("IOC") == "IOC"
    assert map_price_type("SL-M") == "SL-M"


def test_order_status_normalisation():
    from broker.hdfcsec.mapping.transform_data import map_order_status

    assert map_order_status("executed") == "complete"
    assert map_order_status("Trigger Pending") == "open"
    assert map_order_status("CANCELLED") == "cancelled"


# ---------------------------------------------------------------------------
# F&O decode + expiry formatting
# ---------------------------------------------------------------------------

def test_fno_option_decode():
    from broker.hdfcsec.mapping.transform_data import _decode_fno_symbol

    out = _decode_fno_symbol("NIFTY25JAN24500CE")
    assert out is not None
    assert out["root"] == "NIFTY"
    assert out["strike_price"] == "24500"
    assert out["option_type"] == "CE"
    assert out["is_option"] is True
    # expiry_date is DDMMYYYY (no separators)
    assert len(out["expiry_date"]) == 8
    assert out["expiry_date"].startswith("25")
    assert "01" in out["expiry_date"][2:4]


def test_fno_future_decode():
    from broker.hdfcsec.mapping.transform_data import _decode_fno_symbol

    out = _decode_fno_symbol("NIFTY25JANFUT")
    assert out is not None
    assert out["is_option"] is False
    assert out["strike_price"] == "0"


def test_fno_non_match():
    from broker.hdfcsec.mapping.transform_data import _decode_fno_symbol

    assert _decode_fno_symbol("RELIANCE") is None
    assert _decode_fno_symbol("") is None


# ---------------------------------------------------------------------------
# transform_data shape
# ---------------------------------------------------------------------------

def test_transform_data_equity_minimal_shape():
    from broker.hdfcsec.mapping.transform_data import transform_data

    body = transform_data({
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "action": "BUY",
        "quantity": "10",
        "price": "2500.50",
        "pricetype": "LIMIT",
        "product": "CNC",
        "validity": "DAY",
    }, security_id="1002", lot_size=1)

    assert body["instrument_segment"] == "EQUITY"
    assert body["transaction_type"] == "Buy"          # capitalised
    assert body["product"] == "DELIVERY"
    assert body["order_type"] == "LIMIT"
    assert body["security_id"] == "1002"
    assert body["quantity"] == 10
    assert "underlying_symbol" not in body
    assert "expiry_date" not in body
    assert isinstance(body["external_reference_number"], int)
    assert 100_000_000 <= body["external_reference_number"] <= 999_999_999


def test_transform_data_fno_lot_size_multiplied():
    from broker.hdfcsec.mapping.transform_data import transform_data

    body = transform_data({
        "symbol": "NIFTY25JAN24500CE",
        "exchange": "NFO",
        "action": "SELL",
        "quantity": "2",                # 2 lots
        "price": "10.5",
        "pricetype": "LIMIT",
        "product": "MIS",
        "validity": "DAY",
    }, security_id="44321", lot_size=50)

    assert body["instrument_segment"] == "OPTIDX"
    assert body["underlying_symbol"] == "NIFTY"
    assert body["option_type"] == "CE"
    assert body["strike_price"] == 24500.0
    assert body["quantity"] == 100           # 2 lots * 50 lot_size
    assert body["transaction_type"] == "Sell"
    assert body["product"] == "INTRADAY"
    assert len(body["expiry_date"]) == 8


def test_transform_data_fno_stock_option_segment():
    from broker.hdfcsec.mapping.transform_data import transform_data

    body = transform_data({
        "symbol": "RELIANCE25JAN3000PE",
        "exchange": "NFO",
        "action": "BUY",
        "quantity": "1",
        "price": "50",
        "pricetype": "LIMIT",
        "product": "NRML",
    }, security_id="9876", lot_size=250)

    assert body["instrument_segment"] == "OPTSTK"   # stock option, not index
    assert body["underlying_symbol"] == "RELIANCE"
    assert body["product"] == "OVERNIGHT"


def test_transform_modify_order_data():
    from broker.hdfcsec.mapping.transform_data import transform_modify_order_data

    body = transform_modify_order_data({
        "orderid": "abc123",
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "quantity": "5",
        "price": "2510",
        "pricetype": "LIMIT",
        "product": "CNC",
        "validity": "DAY",
    })
    assert body["quantity"] == 5
    assert body["product"] == "DELIVERY"
    assert body["order_type"] == "LIMIT"
    assert "transaction_type" not in body      # not allowed on modify


# ---------------------------------------------------------------------------
# Response normalization
# ---------------------------------------------------------------------------

def test_extract_order_id_from_data_object():
    from broker.hdfcsec.mapping.order_data import extract_order_id

    assert extract_order_id({"status": "success", "data": {"order_id": "12345"}}) == "12345"
    # data is a list of one
    assert extract_order_id({"status": "success", "data": [{"order_id": "67890"}]}) == "67890"
    assert extract_order_id({"status": "error"}) == ""


def test_extract_error_message():
    from broker.hdfcsec.mapping.order_data import extract_error_message

    assert extract_error_message({"message": "Insufficient funds"}) == "Insufficient funds"
    assert extract_error_message({"status_message": "Trigger pending"}) == "Trigger pending"
    assert "Unknown" in extract_error_message({"status": "error"})


def test_transform_order_data_envelope():
    from broker.hdfcsec.mapping.order_data import transform_order_data

    raw = {"status": "success", "data": [{
        "order_id": "ORD-001",
        "tradingsymbol": "RELIANCE",
        "exchange": "NSE",
        "transaction_type": "Buy",
        "quantity": 10,
        "filled_quantity": 10,
        "pending_quantity": 0,
        "price": 2500,
        "order_type": "LIMIT",
        "product": "DELIVERY",
        "status": "executed",
        "average_price": 2499.95,
        "instrument_type": "EQUITY",
    }]}
    out = transform_order_data(raw)
    assert len(out) == 1
    o = out[0]
    assert o["orderid"] == "ORD-001"
    assert o["status"] == "complete"
    assert o["action"] == "BUY"
    assert o["product"] == "CNC"
    assert o["filled_qty"] == "10"


def test_transform_position_data_net_unwraps():
    from broker.hdfcsec.mapping.order_data import transform_position_data

    raw = {"status": "success", "data": {"net": [{
        "tradingsymbol": "RELIANCE",
        "exchange": "NSE",
        "product": "DELIVERY",
        "instrument_type": "EQUITY",
        "t_day_buy_quantity": 10,
        "t_day_sell_qty": 3,
        "t_day_buy_value": 25000.0,
        "t_day_sell_value": 7500.0,
        "t_day_average_buy_price": 2500.0,
        "t_day_avg_sell_price": 2500.0,
        "t_day_net_qty": 7,
    }]}}
    out = transform_position_data(raw)
    assert len(out) == 1
    p = out[0]
    assert p["quantity"] == "7"
    assert p["exchange"] == "NSE"
    assert p["product"] == "CNC"


def test_transform_position_data_handles_exchange_all():
    from broker.hdfcsec.mapping.order_data import transform_position_data

    raw = {"data": {"net": [{
        "tradingsymbol": "NIFTY25JAN24500CE",
        "exchange": "ALL",
        "instrument_type": "OPTIDX",
        "product": "INTRADAY",
        "t_day_net_qty": 50,
    }]}}
    out = transform_position_data(raw)
    # ALL -> NSE -> NFO via instrument_segment
    assert out[0]["exchange"] == "NFO"
    assert out[0]["product"] == "MIS"


def test_transform_holding_data_token_prefix_extracts_exchange():
    from broker.hdfcsec.mapping.order_data import transform_holding_data

    raw = {"status": "success", "data": [{
        "tradingsymbol": "RELIANCE",
        "instrument_token": "NSE1234567",
        "quantity": 10,
        "average_price": 2500,
        "close_price": 2600,
    }]}
    out = transform_holding_data(raw)
    assert out[0]["exchange"] == "NSE"
    assert out[0]["pnl"] == "1000.00"          # (2600 - 2500) * 10
