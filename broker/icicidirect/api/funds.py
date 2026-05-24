"""ICICI Direct funds/limits API (Breeze).

GET /breezeapi/api/v1/funds returns a single Success object with:
    total_bank_balance, allocated_equity, allocated_fno,
    block_by_trade_equity, block_by_trade_fno, block_by_trade_balance,
    unallocated_balance.

We normalize into OpenAlgo's margin shape:
    availablecash, collateral, m2munrealized, m2mrealized, utiliseddebits
"""
from __future__ import annotations

from typing import Any, Dict

from broker.icicidirect.api.breeze_http import request as breeze_request
from broker.icicidirect.baseurl import FUNDS_URL
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
    """Fetch + normalize margin data. Returns _DEFAULT_MARGIN shape on any error."""
    try:
        raw = breeze_request("GET", FUNDS_URL, auth_token, payload={})
        status = raw.get("Status") if isinstance(raw, dict) else None
        if status not in (200, "200"):
            logger.error(f"ICICI funds fetch failed: {raw!r}")
            return dict(_DEFAULT_MARGIN)

        s = raw.get("Success") or {}
        if isinstance(s, list) and s:
            s = s[0]
        if not isinstance(s, dict):
            return dict(_DEFAULT_MARGIN)

        unallocated = float(s.get("unallocated_balance") or 0)
        total = float(s.get("total_bank_balance") or 0)
        allocated_eq = float(s.get("allocated_equity") or 0)
        allocated_fno = float(s.get("allocated_fno") or 0)
        block_eq = float(s.get("block_by_trade_equity") or 0)
        block_fno = float(s.get("block_by_trade_fno") or 0)
        block_bal = float(s.get("block_by_trade_balance") or 0)

        # Breeze does not surface a P&L field on /funds — leave m2m fields
        # at 0.00 and let the positions endpoint provide the live MTM.
        return {
            "availablecash": _fmt(unallocated or total),
            "collateral": _fmt(s.get("collateral_amount") or 0),
            "m2munrealized": "0.00",
            "m2mrealized": "0.00",
            "utiliseddebits": _fmt(allocated_eq + allocated_fno + block_eq + block_fno + block_bal),
        }

    except Exception as e:  # pragma: no cover
        logger.exception(f"ICICI get_margin_data exception: {e}")
        return dict(_DEFAULT_MARGIN)
