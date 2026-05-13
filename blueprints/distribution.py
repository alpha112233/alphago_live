# blueprints/distribution.py
"""
Distribution Inbox HTTP surface.

REST endpoints (Flask session auth, the inbox owner manages their own):
    GET    /api/distribution/inboxes               list inboxes
    POST   /api/distribution/inboxes               create — returns plaintext key once
    PUT    /api/distribution/inboxes/<id>          update name/broker/IPs/status
    POST   /api/distribution/inboxes/<id>/rotate   rotate API key → new plaintext
    DELETE /api/distribution/inboxes/<id>          delete inbox + its signal log
    GET    /api/distribution/inboxes/<id>/signals  recent signal log

Public webhook (no Flask session — API-key only):
    POST   /distribution/inbox/<inbox_slug>            signal receiver
    GET    /distribution/inbox/<inbox_slug>/positions  current broker positionbook

Webhook payload shape (publisher → subscriber):
    {
      "signal_id": "<unique-per-signal id, used as dedupe key>",
      "symbol":    "RELIANCE" | "NIFTY25500CE",
      "exchange":  "NSE" | "NFO" | "BSE" | ...,
      "action":    "BUY" | "SELL",
      "quantity":  100,            # FINAL share/contract count
      "product":   "MIS" | "CNC" | "NRML",
      "pricetype": "MARKET" | "LIMIT" | "SL" | "SL-M",
      "price":     0,              # required for LIMIT/SL
      "trigger_price": 0           # required for SL/SL-M
    }
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any

from flask import Blueprint, jsonify, request, session

from database.distribution_db import (
    check_api_key,
    create_inbox,
    delete_inbox,
    find_signal,
    get_inbox_by_slug,
    list_inboxes,
    list_signals,
    record_signal,
    rotate_api_key,
    update_inbox,
)

logger = logging.getLogger(__name__)

distribution_bp = Blueprint("distribution_bp", __name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _current_user_id() -> int | None:
    """Resolve the Flask-session username → user id."""
    name = session.get("user")
    if not name:
        return None
    try:
        from database.user_db import db_session, User
        u = db_session.query(User).filter_by(username=name).first()
        return u.id if u else None
    except Exception:
        logger.exception("user lookup failed")
        return None


def _real_source_ip() -> str:
    """Best-effort source IP, honouring nginx's X-Forwarded-For."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or ""


def _ip_is_allowed(src_ip: str, allowed_csv: str | None) -> bool:
    """Return True if `src_ip` matches any entry in `allowed_csv`.

    Each entry can be a plain IP or a CIDR. Empty/None allowlist = allow all
    (the inbox owner explicitly opted out of IP filtering).
    """
    if not allowed_csv or not allowed_csv.strip():
        return True
    if not src_ip:
        return False
    try:
        src = ipaddress.ip_address(src_ip)
    except ValueError:
        return False
    for raw in allowed_csv.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            if "/" in raw:
                if src in ipaddress.ip_network(raw, strict=False):
                    return True
            else:
                if src == ipaddress.ip_address(raw):
                    return True
        except ValueError:
            continue
    return False


def _extract_bearer_or_apikey_header() -> str:
    """Accept either `Authorization: Bearer <key>` or `X-API-Key: <key>`."""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("X-API-Key", "").strip()


def _resolve_routing_broker(user_id: int, broker_override: str | None) -> tuple[str | None, str | None, str | None]:
    """Pick the broker for an incoming signal + return its auth_token.

    Returns (broker, auth_token, error_message). On any failure auth_token
    is None and error_message names the problem.

    Lookup strategy:
      1. If the inbox has broker_override: require auth_db to have a valid
         row for that broker and use it. If not, fail with a clear message.
      2. Else: fall back to whatever broker the user last successfully
         authed (auth_db.filter_by(name=username) — the active broker).
    """
    from database.user_db import db_session as user_session, User
    user = user_session.query(User).filter_by(id=user_id).first()
    if user is None:
        return None, None, "user not found"

    from database.auth_db import Auth, decrypt_token
    if broker_override:
        row = Auth.query.filter_by(name=user.username, broker=broker_override).first()
        if not row or row.is_revoked:
            return broker_override, None, (
                f"Inbox is pinned to broker '{broker_override}', but no active "
                f"session for that broker. Open Manage Brokers in the dashboard "
                f"and either run Auto Login for {broker_override}, or change the "
                f"inbox's broker override."
            )
        token = decrypt_token(row.auth)
        if not token:
            return broker_override, None, f"failed to decrypt auth token for '{broker_override}'"
        return broker_override, token, None

    row = Auth.query.filter_by(name=user.username).first()
    if not row or row.is_revoked:
        return None, None, (
            "No active broker session. Open Manage Brokers in the dashboard "
            "and complete login for one broker before sending signals."
        )
    token = decrypt_token(row.auth)
    if not token:
        return row.broker, None, "failed to decrypt auth token"
    return row.broker, token, None


# ---------------------------------------------------------------------------
# REST endpoints (session-authed)
# ---------------------------------------------------------------------------


@distribution_bp.route("/api/distribution/inboxes", methods=["GET"])
def list_inboxes_endpoint():
    user_id = _current_user_id()
    if user_id is None:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401
    return jsonify({"status": "success", "data": list_inboxes(user_id)})


@distribution_bp.route("/api/distribution/inboxes", methods=["POST"])
def create_inbox_endpoint():
    user_id = _current_user_id()
    if user_id is None:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"status": "error", "message": "'name' is required"}), 400

    broker_override = (body.get("broker_override") or "").strip().lower() or None
    allowed_ips = (body.get("allowed_ips") or "").strip() or None

    try:
        row, plaintext_key = create_inbox(
            user_id=user_id,
            name=name,
            broker_override=broker_override,
            allowed_ips=allowed_ips,
        )
    except Exception as e:
        logger.exception("create_inbox failed")
        return jsonify({"status": "error", "message": str(e)}), 500

    # Plaintext key returned ONCE. We never store or return it again.
    return jsonify({
        "status": "success",
        "data": {
            "id": row.id,
            "name": row.name,
            "inbox_slug": row.inbox_slug,
            "api_key_last4": row.api_key_last4,
            "broker_override": row.broker_override,
            "allowed_ips": row.allowed_ips or "",
            "status": row.status,
            "api_key_plaintext": plaintext_key,
            "webhook_url": _build_webhook_url(row.inbox_slug),
        },
    })


@distribution_bp.route("/api/distribution/inboxes/<int:inbox_id>", methods=["PUT"])
def update_inbox_endpoint(inbox_id: int):
    user_id = _current_user_id()
    if user_id is None:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    body = request.get_json(silent=True) or {}
    kwargs: dict[str, Any] = {}
    if "name" in body:
        kwargs["name"] = (body.get("name") or "").strip()
    if "broker_override" in body:
        v = body.get("broker_override")
        kwargs["broker_override"] = (v.strip().lower() if isinstance(v, str) and v.strip() else None)
    if "allowed_ips" in body:
        v = body.get("allowed_ips")
        kwargs["allowed_ips"] = (v.strip() if isinstance(v, str) and v.strip() else None)
    if "status" in body:
        v = (body.get("status") or "").strip().lower()
        if v in ("active", "disabled"):
            kwargs["status"] = v

    row = update_inbox(user_id, inbox_id, **kwargs)
    if row is None:
        return jsonify({"status": "error", "message": "inbox not found"}), 404
    return jsonify({"status": "success"})


@distribution_bp.route("/api/distribution/inboxes/<int:inbox_id>/rotate", methods=["POST"])
def rotate_api_key_endpoint(inbox_id: int):
    user_id = _current_user_id()
    if user_id is None:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401
    result = rotate_api_key(user_id, inbox_id)
    if result is None:
        return jsonify({"status": "error", "message": "inbox not found"}), 404
    row, plaintext = result
    return jsonify({
        "status": "success",
        "data": {
            "id": row.id,
            "api_key_last4": row.api_key_last4,
            "api_key_plaintext": plaintext,
        },
    })


@distribution_bp.route("/api/distribution/inboxes/<int:inbox_id>", methods=["DELETE"])
def delete_inbox_endpoint(inbox_id: int):
    user_id = _current_user_id()
    if user_id is None:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401
    ok = delete_inbox(user_id, inbox_id)
    if not ok:
        return jsonify({"status": "error", "message": "inbox not found"}), 404
    return jsonify({"status": "success"})


@distribution_bp.route("/api/distribution/inboxes/<int:inbox_id>/signals", methods=["GET"])
def list_signals_endpoint(inbox_id: int):
    user_id = _current_user_id()
    if user_id is None:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401
    limit = int(request.args.get("limit", 50) or 50)
    return jsonify({"status": "success", "data": list_signals(user_id, inbox_id, limit)})


def _build_webhook_url(inbox_slug: str) -> str:
    """Construct the public webhook URL using HOST_SERVER as the base."""
    import os
    host = (os.getenv("HOST_SERVER") or "").rstrip("/")
    if host:
        return f"{host}/distribution/inbox/{inbox_slug}"
    # Fallback to request.host_url if HOST_SERVER isn't set (dev mode).
    return f"{request.host_url.rstrip('/')}/distribution/inbox/{inbox_slug}"


# ---------------------------------------------------------------------------
# Public webhook (no session auth — API-key only)
# ---------------------------------------------------------------------------


_REQUIRED_FIELDS = ("signal_id", "symbol", "exchange", "action", "quantity")
_VALID_ACTIONS = {"BUY", "SELL"}
_VALID_PRICETYPES = {"MARKET", "LIMIT", "SL", "SL-M"}
_DEFAULT_PRODUCT = "MIS"
_DEFAULT_PRICETYPE = "MARKET"


@distribution_bp.route("/distribution/inbox/<inbox_slug>", methods=["POST"])
def receive_signal_endpoint(inbox_slug: str):
    """Receive a trade signal from an external publisher and route it to
    the subscriber's broker. See module docstring for payload shape."""
    src_ip = _real_source_ip()

    # 1. Locate the inbox.
    inbox = get_inbox_by_slug(inbox_slug)
    if inbox is None:
        return jsonify({"status": "error", "message": "unknown inbox"}), 404
    if inbox.status != "active":
        return jsonify({"status": "error", "message": "inbox disabled"}), 403

    # 2. Validate API key (constant-time).
    presented_key = _extract_bearer_or_apikey_header()
    if not check_api_key(inbox, presented_key):
        return jsonify({
            "status": "error",
            "message": "invalid or missing API key (send as 'Authorization: Bearer <key>')",
        }), 401

    # 3. IP allowlist (only if configured).
    if not _ip_is_allowed(src_ip, inbox.allowed_ips):
        try:
            record_signal(
                inbox_id=inbox.id,
                signal_id=f"_blocked_{src_ip}_{request.headers.get('X-Request-Id', '')}",
                src_ip=src_ip,
                payload={"_note": "request blocked at IP allowlist"},
                status="ip_blocked",
                error_message=f"source {src_ip} not in allowlist",
            )
        except Exception:
            logger.exception("could not log ip_blocked signal")
        return jsonify({
            "status": "error",
            "message": f"source IP {src_ip} not in this inbox's allowlist",
        }), 403

    # 4. Parse + validate the payload.
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({
            "status": "error",
            "message": "request body must be JSON object",
        }), 400

    missing = [f for f in _REQUIRED_FIELDS if not body.get(f) and body.get(f) != 0]
    if missing:
        return jsonify({
            "status": "error",
            "message": f"missing required fields: {missing}",
        }), 400

    signal_id = str(body["signal_id"])[:160]
    action = str(body["action"]).strip().upper()
    if action not in _VALID_ACTIONS:
        return jsonify({"status": "error", "message": f"action must be one of {sorted(_VALID_ACTIONS)}"}), 400

    try:
        quantity = int(body["quantity"])
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "quantity must be an integer"}), 400
    if quantity <= 0:
        return jsonify({"status": "error", "message": "quantity must be > 0"}), 400

    pricetype = (str(body.get("pricetype") or _DEFAULT_PRICETYPE)).strip().upper()
    if pricetype not in _VALID_PRICETYPES:
        return jsonify({"status": "error", "message": f"pricetype must be one of {sorted(_VALID_PRICETYPES)}"}), 400

    # 5. Idempotency check — same (inbox_id, signal_id) returns the prior result.
    prior = find_signal(inbox.id, signal_id)
    if prior is not None:
        return jsonify({
            "status": "duplicate",
            "message": "this signal_id was already processed",
            "original_status": prior.status,
            "broker_order_id": prior.broker_order_id,
            "broker_used": prior.broker_used,
            "error": prior.error_message,
        }), 200

    # 6. Resolve the broker for this inbox.
    broker, auth_token, err = _resolve_routing_broker(inbox.user_id, inbox.broker_override)
    if err is not None:
        record_signal(
            inbox_id=inbox.id, signal_id=signal_id, src_ip=src_ip,
            payload=body, status="failed", broker_used=broker, error_message=err,
        )
        return jsonify({"status": "error", "message": err}), 503

    # 7. Place the order via OpenAlgo's existing service.
    order_data = {
        "strategy": f"distribution:{inbox.name}",
        "symbol": str(body["symbol"]).strip().upper(),
        "exchange": str(body["exchange"]).strip().upper(),
        "action": action,
        "pricetype": pricetype,
        "product": str(body.get("product") or _DEFAULT_PRODUCT).strip().upper(),
        "quantity": str(quantity),
        "price": str(body.get("price") or 0),
        "trigger_price": str(body.get("trigger_price") or 0),
        "disclosed_quantity": str(body.get("disclosed_quantity") or 0),
    }

    try:
        from services.place_order_service import place_order
        success, response_data, _http_status = place_order(
            order_data=order_data,
            auth_token=auth_token,
            broker=broker,
            emit_event=True,
        )
    except Exception as e:
        logger.exception("place_order raised")
        record_signal(
            inbox_id=inbox.id, signal_id=signal_id, src_ip=src_ip,
            payload=body, status="failed", broker_used=broker,
            error_message=f"place_order exception: {e}",
        )
        return jsonify({"status": "error", "message": str(e)}), 500

    if not success:
        err_msg = (response_data or {}).get("message", "unknown placement error")
        record_signal(
            inbox_id=inbox.id, signal_id=signal_id, src_ip=src_ip,
            payload=body, status="failed", broker_used=broker, error_message=err_msg,
        )
        return jsonify({
            "status": "error",
            "broker": broker,
            "message": err_msg,
            "response": response_data,
        }), 502

    broker_order_id = (response_data or {}).get("orderid") or (response_data or {}).get("order_id")
    record_signal(
        inbox_id=inbox.id, signal_id=signal_id, src_ip=src_ip,
        payload=body, status="placed", broker_used=broker, broker_order_id=broker_order_id,
    )
    return jsonify({
        "status": "success",
        "broker": broker,
        "broker_order_id": broker_order_id,
        "response": response_data,
    }), 200


# ---------------------------------------------------------------------------
# Positionbook readback (no session — same Bearer api_key as webhook)
# ---------------------------------------------------------------------------


@distribution_bp.route("/distribution/inbox/<inbox_slug>/positions", methods=["GET"])
def get_positions_endpoint(inbox_slug: str):
    """Return the current broker positionbook for the inbox's owner.

    Auth + IP-allowlist + broker-resolve are identical to the webhook —
    callers (the publisher, or a customer's own tooling) present
    `Authorization: Bearer <api_key>` and we route through the same broker
    the inbox would route orders through. Read-only — no side effects.
    """
    src_ip = _real_source_ip()

    inbox = get_inbox_by_slug(inbox_slug)
    if inbox is None:
        return jsonify({"status": "error", "message": "unknown inbox"}), 404
    if inbox.status != "active":
        return jsonify({"status": "error", "message": "inbox disabled"}), 403

    presented_key = _extract_bearer_or_apikey_header()
    if not check_api_key(inbox, presented_key):
        return jsonify({
            "status": "error",
            "message": "invalid or missing API key (send as 'Authorization: Bearer <key>')",
        }), 401

    if not _ip_is_allowed(src_ip, inbox.allowed_ips):
        return jsonify({
            "status": "error",
            "message": f"source IP {src_ip} not in this inbox's allowlist",
        }), 403

    broker, auth_token, err = _resolve_routing_broker(inbox.user_id, inbox.broker_override)
    if err is not None:
        return jsonify({"status": "error", "broker": broker, "message": err}), 503

    try:
        from services.positionbook_service import get_positionbook
        success, response_data, status_code = get_positionbook(
            auth_token=auth_token, broker=broker,
        )
    except Exception as e:
        logger.exception("get_positionbook raised")
        return jsonify({"status": "error", "broker": broker, "message": str(e)}), 500

    if not success:
        return jsonify({
            "status": "error",
            "broker": broker,
            "message": (response_data or {}).get("message") or "failed to fetch positions",
        }), status_code

    from datetime import datetime, timezone
    return jsonify({
        "status": "success",
        "broker": broker,
        "data": (response_data or {}).get("data") or [],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }), 200
