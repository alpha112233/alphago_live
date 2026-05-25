"""HDFC Securities funds/margin API.

GET /oapi/v1/user/margins returns:
    {
      "status": "success",
      "data": {
        "equity": {
          "totalAvailableLimitDetails": {
            "cash": ..., "equity_intraday": ..., "equity_margin": ...
          },
          "total_limit": ...,
          "total_utilised_limit": ...
        }
      }
    }
"""
from __future__ import annotations

from typing import Any, Dict

from broker.hdfcsec.api.hdfc_http import request as hdfc_request
from broker.hdfcsec.baseurl import FUNDS_URL
from utils.logging import get_logger

logger = get_logger(__name__)


_DEFAULT_MARGIN = {
    "availablecash": "0.00",
    "collateral": "0.00",
    "m2munrealized": "0.00",
    "m2mrealized": "0.00",
    "utiliseddebits": "0.00",
}


def _fmt(v: Any) -> str:
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "0.00"


def get_margin_data(auth_token: str) -> Dict[str, Any]:
    try:
        raw = hdfc_request("GET", FUNDS_URL, auth_token)
        if raw.get("status") != "success":
            logger.error(f"HDFC funds fetch failed: {raw!r}")
            return dict(_DEFAULT_MARGIN)

        eq = (raw.get("data") or {}).get("equity") or {}
        avail = eq.get("totalAvailableLimitDetails") or {}

        return {
            "availablecash": _fmt(avail.get("cash") or 0),
            "collateral": _fmt(avail.get("equity_margin") or 0),
            "m2munrealized": _fmt(eq.get("currentUnrealizedMTOM") or 0),
            "m2mrealized": _fmt(eq.get("currentRealizedPNL") or 0),
            "utiliseddebits": _fmt(eq.get("total_utilised_limit") or 0),
        }

    except Exception as e:  # pragma: no cover
        logger.exception(f"HDFC get_margin_data exception: {e}")
        return dict(_DEFAULT_MARGIN)
