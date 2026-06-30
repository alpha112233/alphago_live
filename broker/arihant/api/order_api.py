"""Arihant TradeBridge order + portfolio API.

Ported from ccxt-india/brokers/arihant/arihant.py (the canonical Arihant
SDK used in production for the prod-alphaquark-github frontend). Adapted
to alphago_live's contract: each function takes ``auth`` (the access
token) and returns either a normalized list/dict or a (response,
parsed_data, orderid) triple for write-side calls.

Covered:
  * place_order_api / place_smartorder_api / cancel_order / modify_order
  * get_order_book / get_trade_book / get_order_status
  * get_holdings / get_positions / get_open_position

NOT covered (follow-up PRs):
  * cancel_all_orders_api (loop over order book + cancel each — implement
    after order book mapping is reviewed)
  * close_all_positions (loop over positions + place opposite-side market
    order — same)
  * GTT / OCO / basket margin / options chain / WS streaming

Arihant's response envelope shape:
  Success:  {"infoID": "INFO00", "infoMsg": "...", "data": {...}}
  Failure:  {"infoID": "ERR...", "infoMsg": "...", "_http_status": 4xx}
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from broker.arihant.baseurl import get_url
from broker.arihant.mapping.transform_data import (
    transform_data,
    transform_modify_order_data,
)
from database.token_db import get_br_symbol, get_token
from utils.httpx_client import get_httpx_client

log = logging.getLogger(__name__)

_DEFAULT_LATITUDE = "19.0760"
_DEFAULT_LONGITUDE = "72.8777"


def _headers(auth: str, *, with_geo: bool = False) -> dict:
    # 'source' must match what Arihant registered the api-key for. SDK is
    # the partner-integration value; APPCONSOLE is the dev portal value.
    # See broker/arihant/api/auth_api.py:_headers — same env override.
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "api-key": os.getenv("BROKER_API_KEY", ""),
        "source": os.getenv("ARIHANT_SOURCE", "SDK").strip() or "SDK",
        "Authorization": f"Bearer {auth}" if not auth.lower().startswith("bearer ") else auth,
    }
    if with_geo:
        h["X-latitude"] = os.getenv("ARIHANT_LATITUDE", _DEFAULT_LATITUDE)
        h["X-longitude"] = os.getenv("ARIHANT_LONGITUDE", _DEFAULT_LONGITUDE)
    return h


def _request(route: str, method: str, auth: str, body: dict | None = None,
             params: dict | None = None, with_geo: bool = False,
             _retry_count: int = 0) -> dict:
    url = get_url(route)
    headers = _headers(auth, with_geo=with_geo)
    client = get_httpx_client()
    try:
        if method == "GET":
            resp = client.get(url, headers=headers, params=params)
        elif method == "POST":
            resp = client.post(url, headers=headers, content=json.dumps(body or {}))
        elif method == "PUT":
            resp = client.put(url, headers=headers, content=json.dumps(body or {}))
        elif method == "DELETE":
            resp = client.delete(url, headers=headers, params=params)
        else:
            resp = client.request(method, url, headers=headers,
                                  content=json.dumps(body or {}))
    except Exception as e:
        log.exception(f"Arihant {method} {url} failed: {e}")
        return {"infoID": "REQUEST_FAILED", "infoMsg": str(e), "_http_status": 0}

    # 429 single retry with backoff (parity with dhan/upstox).
    if resp.status_code == 429 and _retry_count < 1:
        time.sleep(1.5)
        return _request(route, method, auth, body=body, params=params,
                        with_geo=with_geo, _retry_count=_retry_count + 1)

    # 401 / expired-access-token retry: Arihant's access_token has a much
    # shorter lifetime than OpenAlgo's auth_db cache TTL (which holds until
    # daily session expiry). When stale, we get 401 OR a 200 body with
    # infoID=EG004 'Session expired'. Re-mint via authenticate_broker
    # (which rotates the refresh_token and persists it), update auth_db
    # (clears all caches), and retry the original call once.
    parsed = None
    try:
        parsed = resp.json()
    except Exception:
        pass
    # Known Arihant "your auth needs refresh" infoIDs:
    #   EG004  — "Session expired" (mid-day stale access_token)
    #   EGN006 — "Uh-oh, your login session has expired" (different surface, same root cause)
    #   AU004  — sometimes used too
    # Also match via message text as a defensive belt+braces — Arihant has
    # added new infoIDs in the past without bumping the message.
    _SESSION_EXPIRED_CODES = {"EG004", "EGN006", "AU004"}
    _msg_lower = (parsed.get("infoMsg") or "").lower() if parsed else ""
    is_session_expired = (
        resp.status_code == 401
        or (parsed is not None and (parsed.get("infoID") or "").upper() in _SESSION_EXPIRED_CODES)
        or "session has expired" in _msg_lower
        or "session expired" in _msg_lower
    )
    if is_session_expired and _retry_count < 1:
        new_token = _refresh_and_persist_auth_token(stale_token=auth)
        if new_token:
            log.info(f"Arihant: re-minted access_token after Session-expired on {method} {url}; retrying once")
            return _request(route, method, new_token, body=body, params=params,
                            with_geo=with_geo, _retry_count=_retry_count + 1)

    # Order-adapter warmup: right after a fresh session (container restart or a
    # token re-mint) Arihant's order-routing adapter can briefly answer 200 +
    # infoMsg:"Adapter is Not Ready" before it finishes initializing — the
    # session/token is valid (reads like holdings work), only the order adapter
    # isn't up yet. The order was NOT accepted, so a plain retry after a short
    # delay is safe (no double-place) and transparently rides out the warmup.
    # Bounded to 2 retries so an overnight adapter-down state (market closed)
    # fails fast instead of looping. Don't re-mint here — that would just rotate
    # the refresh_token; the adapter only needs a moment.
    is_adapter_warming = parsed is not None and "adapter is not ready" in _msg_lower
    if is_adapter_warming and _retry_count < 2:
        time.sleep(2.0)
        log.info(f"Arihant: order adapter warming up ('Adapter is Not Ready') on {method} {url}; "
                 f"retry {_retry_count + 1}/2 after delay")
        return _request(route, method, auth, body=body, params=params,
                        with_geo=with_geo, _retry_count=_retry_count + 1)

    if resp.status_code == 401:
        log.error(f"Arihant 401 on {method} {url} — token re-mint also failed")
        return {"infoID": "AUTH_FAILED",
                "infoMsg": "Unauthorized — access token invalid or expired",
                "data": {}, "_http_status": 401}
    if parsed is None:
        return {"infoID": "PARSE_ERROR",
                "infoMsg": f"HTTP {resp.status_code}: {resp.text[:200]}",
                "_http_status": resp.status_code}
    if not (200 <= resp.status_code < 300):
        parsed["_http_status"] = resp.status_code
    return parsed


# Single-flight re-mint guard. Arihant ROTATES the refresh_token on every
# re-mint, so a burst of concurrent "Session expired" calls (multiple gunicorn
# threads/workers hitting the stale access_token at once) would each re-mint
# and rotate — invalidating each other's refresh_token and causing flaky
# empties/401s. We coalesce: the first caller mints; everyone else in the burst
# reuses the freshly-minted token instead of rotating again.
_remint_lock = threading.Lock()
_last_remint: dict = {"token": None, "ts": 0.0}
# Reuse a just-minted token for any re-mint request arriving within this many
# seconds (covers the concurrency burst at access-token expiry; well under the
# real access-token lifetime so a genuinely-expired token isn't served long).
_REMINT_COALESCE_SECONDS = 30.0


def _refresh_and_persist_auth_token(stale_token: str | None = None) -> str | None:
    """Re-mint a fresh Arihant access_token (rotates refresh_token via the
    plugin's authenticate_broker) and write it back to OpenAlgo's auth_db
    so subsequent calls hit a non-stale cache. Returns the new token or
    None on failure.

    Single-flight: serialized by `_remint_lock`. If another thread already
    minted a fresh token — either very recently, or simply one different from
    the `stale_token` this caller was using — we return that instead of
    rotating the refresh_token again."""
    with _remint_lock:
        now = time.monotonic()
        fresh = _last_remint["token"]
        if fresh and (fresh != stale_token or (now - _last_remint["ts"]) < _REMINT_COALESCE_SECONDS):
            log.info("Arihant: reusing freshly-minted access_token (single-flight coalesce)")
            return fresh
        try:
            from broker.arihant.api.auth_api import authenticate_broker
            new_token, err = authenticate_broker(None)
            if not new_token:
                log.error(f"Arihant re-mint failed: {err}")
                return None
        except Exception as e:
            log.exception(f"Arihant re-mint raised: {e}")
            return None

        # Persist into auth_db.Auth row so the cached-token paths see the
        # fresh value. Use whatever username/name exists on the active row;
        # for single-admin OpenAlgo installs there's only one. (Inside the lock
        # so the persist is part of the single-flight, not racing other mints.)
        try:
            from database.auth_db import upsert_auth, Auth
            row = Auth.query.filter_by(broker="arihant", is_revoked=False).first()
            name = row.name if row else "default"
            user_id = row.user_id if row else None
            upsert_auth(name, new_token, "arihant", user_id=user_id)
            log.info("Arihant: fresh access_token written to auth_db; cache cleared")
        except Exception as e:
            log.warning(f"Arihant: auth_db update failed (continuing — current call will still use fresh token): {e}")

        _last_remint["token"] = new_token
        _last_remint["ts"] = now
        return new_token


def _is_success(resp: dict) -> bool:
    info = (resp.get("infoID") or "").upper()
    if resp.get("_http_status") and resp["_http_status"] >= 400:
        return False
    if info.startswith("ERR"):
        return False
    return True


# ----------------------------------------------------------------------
# Read-side: order book / trade book / positions / holdings
# ----------------------------------------------------------------------

def get_order_book(auth: str) -> list[dict]:
    resp = _request("order.book", "GET", auth)
    if not _is_success(resp):
        log.error(f"Arihant order-book failed: {resp.get('infoMsg')}")
        return []
    return ((resp.get("data") or {}).get("orders") or [])


def get_trade_book(auth: str) -> list[dict]:
    resp = _request("order.trade_book", "GET", auth)
    if not _is_success(resp):
        log.error(f"Arihant trade-book failed: {resp.get('infoMsg')}")
        return []
    data = resp.get("data") or {}
    # Some Arihant tenants return trades under "trades", others under "orders".
    return data.get("trades") or data.get("orders") or []


def get_holdings(auth: str) -> list[dict] | dict:
    resp = _request("portfolio.holdings", "GET", auth)
    if not _is_success(resp):
        log.error(f"Arihant holdings failed: {resp.get('infoMsg')}")
        return {"status": "error", "message": resp.get("infoMsg", "Arihant holdings failed")}
    return ((resp.get("data") or {}).get("holdings") or [])


# Position cache + per-symbol lock for smart orders (mirrors indmoney pattern).
_position_cache: dict = {}
_position_cache_lock = threading.Lock()
_POSITION_CACHE_TTL = 1.0


def get_positions(auth: str, position_type: str = "NET") -> list[dict]:
    """``position_type`` = 'DAY' (intraday only) | 'NET' (net incl carry).
    Default NET matches the most common smart-order use case."""
    body = {"type": position_type}
    resp = _request("portfolio.positions", "POST", auth, body=body)
    if not _is_success(resp):
        log.error(f"Arihant positions failed: {resp.get('infoMsg')}")
        return []
    return ((resp.get("data") or {}).get("positions") or [])


def _get_cached_positions(auth: str) -> list[dict]:
    with _position_cache_lock:
        now = time.monotonic()
        cached = _position_cache.get(auth)
        if cached and (now - cached["timestamp"]) < _POSITION_CACHE_TTL:
            return cached["data"]
    fresh = get_positions(auth)
    with _position_cache_lock:
        _position_cache[auth] = {"data": fresh, "timestamp": time.monotonic()}
    return fresh


def _invalidate_position_cache(auth: str) -> None:
    with _position_cache_lock:
        _position_cache.pop(auth, None)


def get_open_position(tradingsymbol: str, exchange: str, product: str,
                      auth: str) -> str:
    """Return net quantity for (symbol, exchange, product), as string.
    Used by place_smartorder_api to decide buy/sell direction + qty."""
    br_symbol = get_br_symbol(tradingsymbol, exchange) or tradingsymbol
    positions = _get_cached_positions(auth) or []
    for p in positions:
        sym_obj = (p.get("symbol") or {}) if isinstance(p.get("symbol"), dict) else {}
        p_symbol = sym_obj.get("tradingSymbol") or sym_obj.get("symbol") or p.get("symbol")
        p_exch = sym_obj.get("exc") or p.get("exc") or p.get("exchange")
        p_prd = p.get("prdType") or p.get("product")
        if (p_symbol == br_symbol and (p_exch or "").upper() == (exchange or "").upper()
                and _product_matches(p_prd, product)):
            return str(p.get("netQty") or p.get("net_qty") or 0)
    return "0"


def _product_matches(broker_prd: str | None, canonical_prd: str) -> bool:
    """Loose match between Arihant's product enum and OpenAlgo's canonical."""
    if not broker_prd:
        return False
    bp = broker_prd.upper()
    cp = (canonical_prd or "").upper()
    if bp == cp:
        return True
    if bp == "DELIVERY" and cp in ("CNC", "DELIVERY"):
        return True
    if bp == "INTRADAY" and cp in ("MIS", "INTRADAY"):
        return True
    if bp == "NRML" and cp == "NRML":
        return True
    return False


def get_order_status(orderid: str, auth: str) -> dict:
    body = {"ordId": orderid, "instrument": "STK"}
    resp = _request("order.status", "POST", auth, body=body)
    if not _is_success(resp):
        return {"status": "error", "message": resp.get("infoMsg", "order-status failed")}
    return resp.get("data") or {}


# ----------------------------------------------------------------------
# Write-side: place / modify / cancel
# ----------------------------------------------------------------------

def place_order_api(data: dict, auth: str):
    """Place an order. ``data`` is OpenAlgo's canonical order dict
    (symbol, exchange, action, ordertype, quantity, price, etc.).

    Returns (response_obj, parsed_response, orderid). Matches the
    contract every other broker plugin obeys; the caller (services/
    place_order_service.py) reads orderid from the third tuple element.
    """
    BROKER_API_KEY = os.getenv("BROKER_API_KEY")
    data["apikey"] = BROKER_API_KEY
    token = get_token(data["symbol"], data["exchange"])
    body = transform_data(data, token)
    log.info(f"Arihant place_order body: {body}")
    resp = _request("order.place", "POST", auth, body=body, with_geo=True)

    if not _is_success(resp):
        return _make_resp_obj(resp), {
            "status": "error",
            "message": resp.get("infoMsg", "Arihant order failed"),
        }, None

    inner = resp.get("data") or {}
    # Arihant fills `rejReason` with a PLACEHOLDER ("--") for orders that were
    # NOT rejected (Executed / Open). Treat it as a real rejection ONLY when it
    # carries a meaningful message — otherwise a successfully-placed order gets
    # flagged failed (no order_id), the advisor re-sends, and DUPLICATE real
    # orders go in. (2026-06-30 incident: a "failed" display on adityaneo
    # produced 3× RELIANCE + 2× PNB executed orders; orderbook showed
    # status:"Executed", boOrdStatus:"complete", rejReason:"--".)
    rej_reason = (inner.get("rejReason") or "").strip()
    if rej_reason and rej_reason.strip("-–— ").strip().lower() not in ("", "na", "n/a", "null", "none"):
        return _make_resp_obj(resp), {
            "status": "error", "message": rej_reason,
        }, None

    orderid = inner.get("ordId")
    _invalidate_position_cache(auth)
    return _make_resp_obj(resp), {
        "status": "success",
        "orderid": orderid,
        "message": resp.get("infoMsg") or inner.get("ordStatus") or "ok",
    }, orderid


def place_smartorder_api(data: dict, auth: str):
    """Position-aware order. Reads current position, computes delta to
    reach the target ``position_size``, places that delta as a market or
    limit order. If already at target, no-op.

    Minimal implementation — kept simple to avoid the inventory-race
    issues that indmoney's plugin solves with per-symbol locks. Follow-up
    PR adds those locks once we've run a few real trades end-to-end.
    """
    target = int(float(data.get("position_size", 0) or 0))
    current = int(float(get_open_position(
        data["symbol"], data["exchange"], data.get("product", "MIS"), auth,
    )))
    delta = target - current
    if delta == 0:
        return None, {"status": "success", "message": "Already at target"}, None
    side = "BUY" if delta > 0 else "SELL"
    order = {**data, "action": side, "quantity": abs(delta)}
    return place_order_api(order, auth)


def cancel_order(orderid: str, auth: str):
    body = {
        "ordId": orderid,
        "remarks": "openalgo-cancel",
    }
    resp = _request("order.cancel", "POST", auth, body=body, with_geo=True)
    if not _is_success(resp):
        return _make_resp_obj(resp), {
            "status": "error",
            "message": resp.get("infoMsg", "Arihant cancel failed"),
        }, None
    _invalidate_position_cache(auth)
    return _make_resp_obj(resp), {
        "status": "success", "orderid": orderid,
        "message": resp.get("infoMsg", "cancelled"),
    }, orderid


def modify_order(data: dict, auth: str):
    """Modify a live order. ``data["orderid"]`` is the Arihant ordId."""
    orderid = data.get("orderid")
    if not orderid:
        return None, {"status": "error", "message": "orderid required"}, None
    body = transform_modify_order_data(data)
    body["ordId"] = orderid
    resp = _request("order.modify", "POST", auth, body=body, with_geo=True)
    if not _is_success(resp):
        return _make_resp_obj(resp), {
            "status": "error",
            "message": resp.get("infoMsg", "Arihant modify failed"),
        }, None
    _invalidate_position_cache(auth)
    return _make_resp_obj(resp), {
        "status": "success", "orderid": orderid,
        "message": resp.get("infoMsg", "modified"),
    }, orderid


def cancel_all_orders_api(data: dict, auth: str):
    """Cancel every open order. Iterates the order book and calls
    cancel_order on each. Returns (success_count, failure_count)."""
    open_orders = [o for o in get_order_book(auth)
                   if (o.get("status") or "").upper() in ("OPEN", "PENDING", "TRIGGER_PENDING")]
    ok, fail = 0, 0
    for o in open_orders:
        _, parsed, _ = cancel_order(o.get("ordId"), auth)
        if parsed.get("status") == "success":
            ok += 1
        else:
            fail += 1
    return ok, fail


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

class _ResponseProxy:
    """Minimal response-shape object that OpenAlgo's order service
    expects (.status attribute). Wraps a parsed dict so the caller can
    do ``response.status`` without a real httpx.Response."""
    def __init__(self, body: dict):
        self._body = body
        self.status = body.get("_http_status", 200)
        self.status_code = self.status

    def json(self):
        return self._body


def _make_resp_obj(resp: dict) -> _ResponseProxy:
    return _ResponseProxy(resp)
