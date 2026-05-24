"""Arihant funds / margin view."""
from __future__ import annotations

import logging

from broker.arihant.api.order_api import _is_success, _request

log = logging.getLogger(__name__)


def get_margin_data(auth: str) -> dict:
    """Return a canonical margin dict for the dashboard's Margins page.

    Arihant /funds/get-funds returns a nested object. We project it to
    the fields OpenAlgo's standard margin view expects:
      availablecash, collateral, m2munrealized, m2mrealized, utilizeddebits.
    Anything missing falls back to 0.
    """
    resp = _request("funds.view", "GET", auth)
    if not _is_success(resp):
        log.error(f"Arihant funds failed: {resp.get('infoMsg')}")
        return {"availablecash": "0.00", "collateral": "0.00",
                "m2munrealized": "0.00", "m2mrealized": "0.00",
                "utilizeddebits": "0.00",
                "status": "error",
                "message": resp.get("infoMsg", "funds-view failed")}
    data = resp.get("data") or {}
    eq = data.get("equity") or data
    return {
        "availablecash": _f(eq.get("availableBalance") or eq.get("available")),
        "collateral": _f(eq.get("collateral") or eq.get("collateralValue")),
        "m2munrealized": _f(eq.get("unrealizedMtm") or eq.get("unrealized")),
        "m2mrealized": _f(eq.get("realizedMtm") or eq.get("realized")),
        "utilizeddebits": _f(eq.get("usedMargin") or eq.get("utilized")),
    }


def _f(v) -> str:
    try:
        return f"{float(v or 0):.2f}"
    except (TypeError, ValueError):
        return "0.00"
