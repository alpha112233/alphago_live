"""Arihant funds / margin view."""
from __future__ import annotations

import logging
import os

from broker.arihant.api.order_api import _is_success, _request

log = logging.getLogger(__name__)

# Arihant /funds/v1/get-funds REQUIRES a `segment` query parameter. The only
# value that returns data is "ALL" (verified live 2026-07-08 against a real
# account: EQ/EQUITY/FNO/CASH/… all answer EGN007 "invalid segment", and
# omitting it 400s with "Required parameter 'segment' is not present"). The
# missing param was the original bug — the dashboard showed 0.00 everywhere +
# a NaN Utilised because every funds fetch failed with that 400 (whose body
# carries no infoMsg, so it logged as "funds failed: None"). Overridable via
# env in case Arihant adds per-segment views later.
_FUNDS_SEGMENT = os.getenv("ARIHANT_FUNDS_SEGMENT", "ALL").strip() or "ALL"


def get_margin_data(auth: str) -> dict:
    """Return a canonical margin dict for the dashboard's Margins page.

    Arihant get-funds returns the balances nested under ``data.funds`` using
    its own compressed keys (``netCashAvail``, ``cashBal``, ``margnUsed``,
    ``collateralVal``, ``unrealMTM``, ``realizedPNL``, …). We project to the
    fields OpenAlgo's standard margin view expects:
      availablecash, collateral, m2munrealized, m2mrealized, utilizeddebits.
    Each reads the confirmed key first, then tolerant fallbacks, then 0. The
    raw ``funds`` object is logged so a schema change surfaces in the log.
    """
    resp = _request("funds.view", "GET", auth, params={"segment": _FUNDS_SEGMENT})
    if not _is_success(resp):
        # Log the FULL envelope — Arihant returns failures with infoMsg=None
        # (e.g. the segment-missing 400), and without the raw body there's
        # nothing to diagnose (2026-07-08: adityaneo dashboard 0.00 all day).
        log.error(
            f"Arihant funds failed: infoID={resp.get('infoID')} "
            f"http={resp.get('_http_status')} msg={resp.get('infoMsg')} raw={resp}"
        )
        return {"availablecash": "0.00", "collateral": "0.00",
                "m2munrealized": "0.00", "m2mrealized": "0.00",
                "utilizeddebits": "0.00",
                "status": "error",
                "message": resp.get("infoMsg", "funds-view failed")}
    data = resp.get("data") or {}
    # Real shape is data.funds; tolerate flat/equity wrappers defensively.
    funds = data.get("funds")
    if not isinstance(funds, dict):
        funds = data.get("equity") if isinstance(data.get("equity"), dict) else data
    log.info(f"Arihant funds raw keys={sorted(funds.keys())} funds={funds}")
    return {
        "availablecash": _pick(funds, "netCashAvail", "cashBal", "ttlCashBal",
                               "notnalCash"),
        "collateral": _pick(funds, "collateralVal", "t1GrossCollatrl",
                             "dirctColatrl", "adhocMargn"),
        "m2munrealized": _pick(funds, "unrealMTM", "cncUnRealMTM", "unbookPNL"),
        "m2mrealized": _pick(funds, "realizedPNL", "realMTM", "cncRealMTM"),
        "utilizeddebits": _pick(funds, "margnUsed", "spanMargn", "expMargn"),
    }


def _pick(d: dict, *keys) -> str:
    """First present-and-numeric key wins; else "0.00"."""
    for k in keys:
        if k in d and d[k] is not None:
            return _f(d[k])
    return "0.00"


def _f(v) -> str:
    """Coerce an Arihant money field to a plain 2dp string.

    🔴 2026-07-20 BUG FIX. Arihant returns money as INDIAN-COMMA-GROUPED
    STRINGS — `netCashAvail: '2,13,821.29'`. A bare `float()` raises
    ValueError on the commas, and the except-branch turned that into
    `"0.00"` — a *plausible business value*, so the dashboard showed a
    confident zero balance rather than an error. Reported as a connection
    problem ("connects fine, balance still 0") because nothing looked broken.

    The bug was INVISIBLE below ₹1,000 (no comma is emitted) and silent above
    it. adityaneo was verified on 2026-07-08 while genuinely flat at '0.00',
    which parsed fine and masked this entirely; it only surfaced once the
    account was funded to ₹2,13,821.29. Affects EVERY Arihant customer with
    any field >= 1,000 — cash, collateral, margin used, MTM alike.

    Strip separators before parsing. Keep returning "0.00" on genuinely
    unparseable input, but LOG it: silently coercing a parse failure into a
    real-looking number is what hid this for 12 days.
    """
    if v is None:
        return "0.00"
    raw = v
    if isinstance(v, str):
        # Indian grouping ('2,13,821.29'), stray spaces/NBSP, optional ₹.
        v = v.replace(",", "").replace(" ", "").replace(" ", "").strip()
        v = v.lstrip("₹").strip()
        if v in ("", "-", "--"):
            return "0.00"
    try:
        return f"{float(v or 0):.2f}"
    except (TypeError, ValueError):
        log.warning(f"Arihant funds: unparseable money value {raw!r} — using 0.00")
        return "0.00"
