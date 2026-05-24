"""Unit tests for the icicidirect broker port.

Pure-function tests that do not touch the Breeze API. Tests for things
that talked to a live server live elsewhere; here we lock the
transformation layer so a regression on enum mapping, F&O decode, or
response normalization is caught at CI time.
"""
from __future__ import annotations

import hashlib

import pytest


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------

def test_checksum_matches_breeze_sha256_formula():
    """The X-Checksum is SHA-256(timestamp + body_json + secret_key)."""
    from broker.icicidirect.api import breeze_http

    timestamp = "2026-05-25T14:32:45.123Z"
    body_json = '{"stock_code":"RELIND"}'
    secret_key = "ssssseeeeccc"
    expected = hashlib.sha256(
        (timestamp + body_json + secret_key).encode("utf-8")
    ).hexdigest()

    auth_string = f"daily-token:::app-key:::{secret_key}"
    headers, body_out = breeze_http.build_headers(
        auth_string, body={"stock_code": "RELIND"}
    )
    assert body_out == body_json
    # X-Checksum header is "token <sha>" — strip the prefix to compare.
    assert headers["X-Checksum"].startswith("token ")
    actual = headers["X-Checksum"][len("token "):]
    # The timestamp inside headers is the runtime one; recompute with it.
    runtime_ts = headers["X-Timestamp"]
    runtime_expected = hashlib.sha256(
        (runtime_ts + body_json + secret_key).encode("utf-8")
    ).hexdigest()
    assert actual == runtime_expected


def test_parse_auth_rejects_malformed_string():
    from broker.icicidirect.api.breeze_http import parse_auth

    with pytest.raises(ValueError):
        parse_auth("only-one-colon-here")
    with pytest.raises(ValueError):
        parse_auth("")
    s, a, k = parse_auth("session:::app:::secret")
    assert (s, a, k) == ("session", "app", "secret")


# ---------------------------------------------------------------------------
# Enum mapping
# ---------------------------------------------------------------------------

def test_map_product_type_equity():
    from broker.icicidirect.mapping.transform_data import map_product_type

    assert map_product_type("CNC", "NSE") == "cash"
    assert map_product_type("MIS", "NSE") == "margin"
    assert map_product_type("NRML", "BSE") == "cash"


def test_map_product_type_fno():
    from broker.icicidirect.mapping.transform_data import map_product_type

    assert map_product_type("NRML", "NFO") == "futureplus"
    assert map_product_type("MIS", "NFO") == "futures"


def test_map_validity_and_action():
    from broker.icicidirect.mapping.transform_data import (
        map_action,
        map_validity,
    )

    assert map_action("BUY") == "buy"
    assert map_action("Sell") == "sell"
    assert map_validity("DAY") == "day"
    assert map_validity("IOC") == "ioc"


def test_order_status_normalisation():
    from broker.icicidirect.mapping.transform_data import map_order_status

    assert map_order_status("Executed") == "complete"
    assert map_order_status("Cancelled") == "cancelled"
    assert map_order_status("Partial Executed") == "open"


# ---------------------------------------------------------------------------
# F&O decode
# ---------------------------------------------------------------------------

def test_fno_option_decode_index():
    from broker.icicidirect.mapping.transform_data import _decode_fno_symbol

    out = _decode_fno_symbol("NIFTY25JAN24500CE")
    assert out is not None
    assert out["root"] == "NIFTY"
    assert out["strike_price"] == "24500"
    assert out["right"] == "call"
    assert out["product_kind"] == "options"
    # Year auto-resolves to next future 25-Jan; format DD-Mon-YYYY.
    assert out["expiry"].startswith("25-Jan-")
    assert len(out["expiry"]) == len("25-Jan-2026")


def test_fno_option_decode_stock_decimal_strike():
    from broker.icicidirect.mapping.transform_data import _decode_fno_symbol

    out = _decode_fno_symbol("RELIANCE25JAN3000PE")
    assert out is not None
    assert out["root"] == "RELIANCE"
    assert out["right"] == "put"
    assert out["strike_price"] == "3000"


def test_fno_future_decode():
    from broker.icicidirect.mapping.transform_data import _decode_fno_symbol

    out = _decode_fno_symbol("NIFTY25JANFUT")
    assert out is not None
    assert out["product_kind"] == "futures"
    assert out["right"] == "others"
    assert out["strike_price"] == "0"


def test_fno_non_match_returns_none():
    from broker.icicidirect.mapping.transform_data import _decode_fno_symbol

    assert _decode_fno_symbol("RELIANCE") is None
    assert _decode_fno_symbol("") is None


# ---------------------------------------------------------------------------
# Place-order payload shape
# ---------------------------------------------------------------------------

def test_transform_data_equity(monkeypatch):
    """Equity order should produce a flat Breeze body without F&O fields."""
    # Stub out the database lookup so the test stays hermetic.
    import broker.icicidirect.mapping.transform_data as tx
    monkeypatch.setattr(
        "database.token_db.get_br_symbol",
        lambda s, e: {"RELIANCE": "RELIND"}.get(s, s),
        raising=False,
    )

    body = tx.transform_data({
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "action": "BUY",
        "quantity": "10",
        "price": "2500.50",
        "pricetype": "LIMIT",
        "product": "CNC",
        "validity": "DAY",
    })
    assert body["stock_code"] == "RELIND"
    assert body["exchange_code"] == "NSE"
    assert body["action"] == "buy"
    assert body["order_type"] == "limit"
    assert body["product"] == "cash"
    assert body["validity"] == "day"
    assert "expiry_date" not in body
    assert "strike_price" not in body


def test_transform_data_fno():
    """NFO option packs root + expiry + strike + right separately."""
    import broker.icicidirect.mapping.transform_data as tx

    body = tx.transform_data({
        "symbol": "NIFTY25JAN24500CE",
        "exchange": "NFO",
        "action": "SELL",
        "quantity": "50",
        "price": "10.5",
        "pricetype": "LIMIT",
        "product": "MIS",
        "validity": "DAY",
    })
    assert body["stock_code"] == "NIFTY"
    assert body["strike_price"] == "24500"
    assert body["right"] == "call"
    assert body["product"] == "options"
    assert body["expiry_date"].startswith("25-Jan-")


# ---------------------------------------------------------------------------
# Response normalization
# ---------------------------------------------------------------------------

def test_extract_order_id_tail_split():
    """Breeze sometimes returns 'Order placed ... 12345' — must tail-split."""
    from broker.icicidirect.mapping.order_data import extract_order_id

    assert extract_order_id({"Success": {"order_id": "12345"}}) == "12345"
    assert extract_order_id({"Success": {"order_id": "Order placed successfully 98765"}}) == "98765"
    assert extract_order_id({"Status": 500}) == ""


def test_transform_order_data_envelope():
    from broker.icicidirect.mapping.order_data import transform_order_data

    raw = {"Success": [{
        "order_id": "ORD-001",
        "stock_code": "RELIND",
        "exchange_code": "NSE",
        "action": "buy",
        "quantity": "10",
        "pending_quantity": "0",
        "price": "2500",
        "order_type": "limit",
        "product_type": "cash",
        "status": "Executed",
        "average_price": "2499.95",
    }]}
    out = transform_order_data(raw)
    assert len(out) == 1
    o = out[0]
    assert o["orderid"] == "ORD-001"
    assert o["status"] == "complete"
    assert o["action"] == "BUY"
    assert o["product"] == "CNC"


def test_transform_position_data_mtf_sell_reduces_qty():
    from broker.icicidirect.mapping.order_data import transform_position_data

    raw = {"Success": [{
        "stock_code": "RELIND",
        "exchange_code": "NSE",
        "quantity": "100",
        "mtf_sell_quantity": "30",
        "price": "2500",
        "product_type": "cash",
    }]}
    out = transform_position_data(raw)
    assert out[0]["quantity"] == "70"
    assert out[0]["product"] == "CNC"


def test_extract_error_message_breeze_shape():
    from broker.icicidirect.mapping.order_data import extract_error_message

    assert extract_error_message({"Error": "Insufficient funds"}) == "Insufficient funds"
    assert "fallback" in extract_error_message({"emsg": "fallback msg"}).lower() or \
        extract_error_message({"emsg": "fallback msg"}) == "fallback msg"
