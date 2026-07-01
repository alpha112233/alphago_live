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
    GET    /distribution/inbox/<inbox_slug>/holdings   current broker holdings (demat)

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
import json
import logging
from typing import Any

from flask import Blueprint, jsonify, request, session

from database.distribution_db import (
    check_api_key,
    clear_pending_bracket,
    create_inbox,
    delete_inbox,
    derive_signing_secret,
    find_signal,
    get_inbox_by_slug,
    list_inboxes,
    list_signals,
    record_signal,
    rotate_api_key,
    set_pending_bracket,
    update_inbox,
    update_signal_status,
    verify_signed_request,
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


def _authenticate_inbox_request(inbox) -> tuple[bool, str | None]:
    """Authenticate an inbound webhook against `inbox`.

    Two accepted modes (the client picks one):
      • HMAC+timestamp (preferred): send `X-Signature` (+ `X-Timestamp`).
        The api_key never travels on the wire and replays are bounded to a
        short window. Used whenever `X-Signature` is present.
      • Bearer api_key (legacy/back-compat): `Authorization: Bearer <key>`
        or `X-API-Key: <key>`.

    Returns (ok, error_message).
    """
    if request.headers.get("X-Signature", "").strip():
        # get_data(cache=True) keeps the raw body so a later get_json() still
        # works; we MUST sign/verify over the exact bytes received.
        raw_body = request.get_data(cache=True)
        return verify_signed_request(
            inbox,
            request.headers.get("X-Timestamp", "").strip(),
            request.headers.get("X-Signature", "").strip(),
            raw_body,
        )
    if check_api_key(inbox, _extract_bearer_or_apikey_header()):
        return True, None
    return False, (
        "invalid or missing credentials — send 'Authorization: Bearer <api_key>', "
        "or sign the request with 'X-Timestamp' + 'X-Signature' (HMAC-SHA256)"
    )


def _resolve_routing_broker(user_id: int, broker_override: str | None) -> tuple[str | None, str | None, str | None]:
    """Pick the broker for an incoming signal + return its auth_token.

    Returns (broker, auth_token, error_message). On any failure auth_token
    is None and error_message names the problem.

    Lookup strategy:
      1. If the inbox has broker_override: require auth_db to have a valid
         row for that broker and use it. If not, fail with a clear message.
      2. Else: use the broker the customer has marked ACTIVE in
         broker_creds_db (their explicit intent), and fetch THAT broker's
         auth-token row. Only if broker_creds has no active broker do we
         fall back to "whatever auth row exists".

    Why (2) cross-checks broker_creds_db (fix 2026-05-27, bug #58):
      The `auth` table can hold rows for several brokers (a customer who
      tried Upstox, then switched to Dhan, leaves an Upstox auth row
      behind). `Auth.query.filter_by(name=username).first()` picked an
      arbitrary one — often NOT the broker the customer currently has
      active — so signals routed to a stale broker whose env vars no
      longer matched ("Missing mandatory field(s): apikey"). Honoring
      broker_creds_db.status='active' makes routing deterministic and
      consistent with what broker_env_bootstrap set the env vars to.
    """
    from database.user_db import db_session as user_session, User
    user = user_session.query(User).filter_by(id=user_id).first()
    if user is None:
        return None, None, "user not found"

    from database.auth_db import Auth, decrypt_token

    def _token_for(broker_name: str):
        row = Auth.query.filter_by(name=user.username, broker=broker_name).first()
        if not row or row.is_revoked:
            return None, None
        return row, decrypt_token(row.auth)

    if broker_override:
        row, token = _token_for(broker_override)
        if row is None:
            return broker_override, None, (
                f"Inbox is pinned to broker '{broker_override}', but no active "
                f"session for that broker. Open Manage Brokers in the dashboard "
                f"and either run Auto Login for {broker_override}, or change the "
                f"inbox's broker override."
            )
        if not token:
            return broker_override, None, f"failed to decrypt auth token for '{broker_override}'"
        return broker_override, token, None

    # No override: prefer the customer's explicitly-active broker.
    active_broker = None
    try:
        from database.broker_creds_db import get_active_broker
        active_broker = get_active_broker(user_id)
    except Exception:
        logger.exception("get_active_broker lookup failed; falling back to auth-table broker")

    if active_broker:
        row, token = _token_for(active_broker)
        if row is None:
            return active_broker, None, (
                f"Your active broker is '{active_broker}' but it has no valid "
                f"session. Open Manage Brokers and complete login / Auto Login "
                f"for {active_broker} before sending signals. (If you meant to "
                f"use a different broker, activate it in Manage Brokers.)"
            )
        if not token:
            return active_broker, None, f"failed to decrypt auth token for '{active_broker}'"
        return active_broker, token, None

    # Legacy fallback: broker_creds_db has no active broker (e.g. a very old
    # single-broker setup). Use whatever auth row exists.
    row = Auth.query.filter_by(name=user.username, is_revoked=False).first()
    if not row:
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
            "signing_secret": derive_signing_secret(row),
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
            "signing_secret": derive_signing_secret(row),
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


# ---- bracket / cover helpers (Phase 3, synthetic-only V1) -------------------

# Child signal_ids derive from the parent via a fixed suffix. Cancel
# cascade queries by `parent__sl` / `parent__tp` prefix. The double
# underscore is unlikely to appear in publisher-generated UUIDs.
_BRACKET_CHILD_SUFFIXES = ("__sl", "__tp")


def _opposite_action(action: str) -> str:
    return "SELL" if action.upper() == "BUY" else "BUY"


def _place_one_via_distribution(
    *, inbox, signal_id: str, src_ip: str,
    payload_for_audit: dict, order_data: dict,
    auth_token: str, broker: str,
) -> tuple[bool, str | None, dict | None, str | None]:
    """Run one parent or child order through the same placement chain the
    main receive endpoint uses. Returns (success, broker_order_id, response_data,
    error_message). Records the signal row regardless."""
    from services.place_order_service import place_order
    try:
        success, response_data, _http_status = place_order(
            order_data=order_data,
            auth_token=auth_token,
            broker=broker,
            emit_event=True,
        )
    except Exception as e:
        logger.exception("place_order raised in bracket helper")
        record_signal(
            inbox_id=inbox.id, signal_id=signal_id, src_ip=src_ip,
            payload=payload_for_audit, status="failed", broker_used=broker,
            error_message=f"place_order exception: {e}",
        )
        return False, None, None, str(e)

    if not success:
        err_msg = (response_data or {}).get("message", "unknown placement error")
        record_signal(
            inbox_id=inbox.id, signal_id=signal_id, src_ip=src_ip,
            payload=payload_for_audit, status="failed", broker_used=broker,
            error_message=err_msg,
        )
        return False, None, response_data, err_msg

    broker_order_id = (
        (response_data or {}).get("orderid") or (response_data or {}).get("order_id")
    )
    record_signal(
        inbox_id=inbox.id, signal_id=signal_id, src_ip=src_ip,
        payload=payload_for_audit, status="placed", broker_used=broker,
        broker_order_id=broker_order_id,
    )
    return True, broker_order_id, response_data, None


def _normalise_bracket_block(block) -> tuple[dict | None, str | None]:
    """Validate the 'bracket' payload field. Returns (normalised_dict, error_message).
    Either both values are None (not provided) or normalised_dict is set.

    Expected shape:
      {
        "sl_trigger_price": 1450,   # required when bracket is present
        "sl_price": 1448,           # optional — if set child SL = SL-Limit; else SL-Market
        "tp_price": 1500            # optional — if set, also place LIMIT TP child
      }
    """
    if block is None:
        return None, None
    if not isinstance(block, dict):
        return None, "bracket must be an object"
    try:
        sl_trigger = float(block.get("sl_trigger_price") or 0)
    except (TypeError, ValueError):
        return None, "bracket.sl_trigger_price must be numeric"
    if sl_trigger <= 0:
        return None, "bracket.sl_trigger_price is required (> 0)"
    try:
        sl_price = float(block.get("sl_price") or 0)
        tp_price = float(block.get("tp_price") or 0)
    except (TypeError, ValueError):
        return None, "bracket.sl_price / tp_price must be numeric"
    return {
        "sl_trigger_price": sl_trigger,
        "sl_price": sl_price,
        "tp_price": tp_price,
    }, None


def place_bracket_children_for_parent(parent_signal_row, src_ip: str = "fill-poller") -> list[dict]:
    """Resolve everything _place_bracket_children needs from a parent
    DistributionSignal row and run it. Called by services/fill_poller.py
    when a parent flips to fill_status='complete'.

    Reads the bracket spec from parent.pending_bracket_json and the original
    payload from parent.payload_json. Uses the SAME broker the parent was
    placed on (NOT necessarily the customer's currently-active broker). Best-
    effort: on placement-failure, errors land in the returned list but no
    exception is raised."""
    from database.distribution_db import db_session as _ds, DistributionInbox
    inbox = _ds.query(DistributionInbox).filter_by(id=parent_signal_row.inbox_id).first()
    if inbox is None:
        return [{"error": f"inbox {parent_signal_row.inbox_id} gone"}]
    if not parent_signal_row.pending_bracket_json:
        return [{"error": "no pending bracket spec on parent"}]
    try:
        bracket = json.loads(parent_signal_row.pending_bracket_json)
    except Exception as e:
        return [{"error": f"bracket spec parse failed: {e}"}]
    try:
        payload = json.loads(parent_signal_row.payload_json or "{}")
    except Exception:
        payload = {}

    # Reconstruct the order_data the parent went out with — same shape as
    # receive_signal_endpoint built. Children inherit symbol/exchange/qty/
    # product; action + pricetype + price/trigger are overridden per child.
    parent_order_data = {
        "apikey": "distribution-internal",
        "strategy": f"distribution:{inbox.name}",
        "symbol": str(payload.get("symbol", "")).strip().upper(),
        "exchange": str(payload.get("exchange", "")).strip().upper(),
        "action": str(payload.get("action", "")).strip().upper(),
        "pricetype": "LIMIT",   # children override; placeholder
        "product": str(payload.get("product") or _DEFAULT_PRODUCT).strip().upper(),
        # Use the FILLED quantity if available — handles partial fills
        # correctly. Fall back to original quantity if nothing was filled.
        "quantity": str(parent_signal_row.filled_quantity or int(payload.get("quantity") or 0)),
        "price": "0",
        "trigger_price": "0",
        "disclosed_quantity": str(payload.get("disclosed_quantity") or 0),
    }

    broker = parent_signal_row.broker_used
    auth_token, terr = _token_for_specific_broker(inbox.user_id, broker)
    if terr is not None:
        return [{"error": f"auth for broker={broker}: {terr}"}]

    return _place_bracket_children(
        inbox=inbox, parent_signal_id=parent_signal_row.signal_id,
        parent_action=parent_order_data["action"],
        parent_order_data=parent_order_data, bracket=bracket,
        auth_token=auth_token, broker=broker, src_ip=src_ip,
    )


def _place_bracket_children(
    *, inbox, parent_signal_id: str, parent_action: str,
    parent_order_data: dict, bracket: dict,
    auth_token: str, broker: str, src_ip: str,
) -> list[dict]:
    """Place the SL child (always) and the TP child (if tp_price > 0) for a
    bracket parent. Returns a list of {leg, signal_id, broker_order_id?,
    error?} summaries — best-effort, doesn't roll back the parent on failure.

    Called by `place_bracket_children_for_parent` after the parent's fill
    completes. The Phase-3 V1 version called this inline from the webhook;
    Phase 3.1 (2026-06-03) moved it to fill-time to prevent TP-fills-before-
    parent issues."""
    child_results: list[dict] = []
    opposite = _opposite_action(parent_action)

    # ---- SL child ----------------------------------------------------------
    sl_signal_id = parent_signal_id + "__sl"
    sl_pricetype = "SL" if (bracket.get("sl_price") or 0) > 0 else "SL-M"
    sl_data = dict(parent_order_data)
    sl_data.update({
        "action": opposite,
        "pricetype": sl_pricetype,
        "price": str(bracket.get("sl_price") or 0),
        "trigger_price": str(bracket["sl_trigger_price"]),
    })
    sl_audit = {
        "_bracket_child_of": parent_signal_id,
        "leg": "sl",
        "symbol": sl_data["symbol"], "exchange": sl_data["exchange"],
        "action": opposite, "pricetype": sl_pricetype,
        "trigger_price": bracket["sl_trigger_price"],
        "price": bracket.get("sl_price") or 0,
        "quantity": sl_data["quantity"],
    }
    ok, order_id, _resp, err = _place_one_via_distribution(
        inbox=inbox, signal_id=sl_signal_id, src_ip=src_ip,
        payload_for_audit=sl_audit, order_data=sl_data,
        auth_token=auth_token, broker=broker,
    )
    child_results.append({
        "leg": "sl", "signal_id": sl_signal_id,
        "broker_order_id": order_id, "error": err,
    })

    # ---- TP child (optional) -----------------------------------------------
    tp_price = float(bracket.get("tp_price") or 0)
    if tp_price > 0:
        tp_signal_id = parent_signal_id + "__tp"
        tp_data = dict(parent_order_data)
        tp_data.update({
            "action": opposite,
            "pricetype": "LIMIT",
            "price": str(tp_price),
            "trigger_price": "0",
        })
        tp_audit = {
            "_bracket_child_of": parent_signal_id,
            "leg": "tp",
            "symbol": tp_data["symbol"], "exchange": tp_data["exchange"],
            "action": opposite, "pricetype": "LIMIT",
            "price": tp_price, "quantity": tp_data["quantity"],
        }
        ok, order_id, _resp, err = _place_one_via_distribution(
            inbox=inbox, signal_id=tp_signal_id, src_ip=src_ip,
            payload_for_audit=tp_audit, order_data=tp_data,
            auth_token=auth_token, broker=broker,
        )
        child_results.append({
            "leg": "tp", "signal_id": tp_signal_id,
            "broker_order_id": order_id, "error": err,
        })
    return child_results


def _cascade_cancel_bracket_children(
    *, inbox, parent_signal_id: str, auth_token: str, broker: str,
) -> list[dict]:
    """When cancelling a bracket parent, also cancel its open SL/TP children
    if they exist and are still in status='placed'."""
    from services.cancel_order_service import cancel_order_with_auth

    out: list[dict] = []
    for suffix in _BRACKET_CHILD_SUFFIXES:
        child_signal_id = parent_signal_id + suffix
        child = find_signal(inbox.id, child_signal_id)
        if child is None or child.status != "placed" or not child.broker_order_id:
            continue
        try:
            success, response_data, _http = cancel_order_with_auth(
                orderid=child.broker_order_id,
                auth_token=auth_token,
                broker=broker,
                original_data={"orderid": child.broker_order_id,
                               "apikey": "distribution-internal"},
            )
        except Exception as e:
            out.append({"leg": suffix.strip("_"), "signal_id": child_signal_id,
                        "cancelled": False, "error": str(e)})
            continue
        if success:
            update_signal_status(inbox.id, child_signal_id, "cancelled", error_message=None)
            out.append({"leg": suffix.strip("_"), "signal_id": child_signal_id,
                        "cancelled": True})
        else:
            err_msg = (response_data or {}).get("message", "unknown")
            out.append({"leg": suffix.strip("_"), "signal_id": child_signal_id,
                        "cancelled": False, "error": err_msg})
    return out


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

    # 2. Authenticate — HMAC+timestamp signature, or Bearer api_key.
    _auth_ok, _auth_err = _authenticate_inbox_request(inbox)
    if not _auth_ok:
        return jsonify({"status": "error", "message": _auth_err}), 401

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

    # Bracket / cover (Phase 3, synthetic V1) — validate early so we can
    # reject malformed brackets before placing the parent.
    bracket, bracket_err = _normalise_bracket_block(body.get("bracket"))
    if bracket_err is not None:
        return jsonify({"status": "error", "message": bracket_err}), 400

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
    # NOTE: `apikey` is a REQUIRED_ORDER_FIELDS entry that validate_order_data
    # checks for *presence* only. This is an INTERNAL call — we pass
    # auth_token + broker directly to place_order(), so the OpenAlgo API key
    # isn't used for auth here and place_order pops it before hitting the
    # broker. But the validator still requires the field to exist, so we
    # inject a sentinel. (Without this every distribution signal failed with
    # "Missing mandatory field(s): apikey" — found during the R3 canary.)
    order_data = {
        "apikey": "distribution-internal",
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
    if not broker_order_id:
        # Broker accepted the call (place_order returned success=True) but the
        # response dict didn't include orderid/order_id under either standard
        # name — could be a broker plugin using a non-standard key, a partial
        # acceptance, or a returned-but-orderless rejection. We can't track
        # this order for modify/cancel/exit later, so claiming "status=success"
        # would just create a downstream phantom (publisher would increment
        # admin_positions but never be able to close cleanly). Refuse the
        # claim explicitly so the publisher's delivery row gets marked failed
        # — operator can verify on the broker portal directly.
        err_msg = (
            f"broker '{broker}' accepted the order but did not return an order_id "
            f"in the response (checked keys: 'orderid', 'order_id'). The order "
            f"may have placed at the broker — verify on broker portal — but the "
            f"container cannot track it for later modify/cancel/exit, so reporting "
            f"this as a placement failure."
        )
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

    record_signal(
        inbox_id=inbox.id, signal_id=signal_id, src_ip=src_ip,
        payload=body, status="placed", broker_used=broker, broker_order_id=broker_order_id,
    )

    # 8. (Phase 3.1) If parent was a bracket, ATTACH the spec to the parent
    # row and let the fill_poller place children only AFTER the parent's
    # fill_status flips to 'complete'. V1 placed children inline immediately
    # after parent placement — but for LIMIT parents that's wrong: the TP
    # child (opposite-side LIMIT at a profitable price) could fill on its
    # own against current market before the parent ever filled, creating an
    # unintended contra position. Confirmed via the 2026-06-03 canary —
    # see services/fill_poller.py for the placement-on-fill logic.
    bracket_state = None
    if bracket is not None:
        set_pending_bracket(inbox.id, signal_id, bracket)
        bracket_state = {
            "status": "pending_parent_fill",
            "note": (
                "SL + TP children will be placed by the fill poller once the "
                "parent's fill_status flips to 'complete'. If the parent is "
                "cancelled or rejected before then, children are never placed."
            ),
        }

    out = {
        "status": "success",
        "broker": broker,
        "broker_order_id": broker_order_id,
        "response": response_data,
    }
    if bracket_state is not None:
        out["bracket_children"] = bracket_state
    return jsonify(out), 200


# ---------------------------------------------------------------------------
# Modify / Cancel by signal_id (Phase 1)
# ---------------------------------------------------------------------------
#
# Both endpoints look up the ORIGINAL placed signal by (inbox_id, signal_id),
# extract the broker + broker_order_id, then call the broker-direct service
# (modify_order_with_auth / cancel_order_with_auth) to round-trip the change.
#
# Routing rule: modify/cancel MUST target the broker the original order was
# placed on (stored in distribution_signals.broker_used) — NOT the customer's
# currently-active broker. If the customer has since switched brokers, we
# only succeed if the old broker's auth row still has a valid token. Otherwise
# we fail with a clear message telling them to log in to that broker.


def _token_for_specific_broker(user_id: int, broker_name: str) -> tuple[str | None, str | None]:
    """Fetch the auth_token for a SPECIFIC broker (not the active one).

    Returns (auth_token, error_message). Used for modify / cancel where we
    must hit the same broker that placed the original — switching brokers
    in the meantime doesn't move the order, so we have to reach the broker
    that owns it.
    """
    from database.user_db import db_session as user_session, User
    from database.auth_db import Auth, decrypt_token

    user = user_session.query(User).filter_by(id=user_id).first()
    if user is None:
        return None, "user not found"

    row = Auth.query.filter_by(name=user.username, broker=broker_name).first()
    if not row or row.is_revoked:
        return None, (
            f"No active session for '{broker_name}' (the broker the original "
            f"order was placed on). Open Manage Brokers and log in to "
            f"{broker_name} — modify/cancel must hit the broker that owns the "
            f"order, even if your active broker has since changed."
        )
    token = decrypt_token(row.auth)
    if not token:
        return None, f"failed to decrypt auth token for '{broker_name}'"
    return token, None


def _lookup_original_for_modify_or_cancel(inbox_id: int, signal_id: str):
    """Find the original placed signal that modify/cancel target. Returns
    (signal_row, error_message, http_status). On success error_message+status
    are None; the row is guaranteed to have broker_used + broker_order_id."""
    prior = find_signal(inbox_id, signal_id)
    if prior is None:
        return None, f"no signal found with signal_id='{signal_id}' for this inbox", 404
    if prior.status != "placed":
        return None, (
            f"signal '{signal_id}' has status '{prior.status}' — only signals "
            f"in status 'placed' can be modified or cancelled"
        ), 409
    if not prior.broker_order_id or not prior.broker_used:
        return None, (
            f"signal '{signal_id}' is missing broker_order_id or broker_used — "
            f"cannot route modify/cancel (was this signal placed before "
            f"order-id tracking landed?)"
        ), 409
    return prior, None, None


@distribution_bp.route("/distribution/inbox/<inbox_slug>/modify", methods=["POST"])
def modify_signal_endpoint(inbox_slug: str):
    """Modify an existing placed order by signal_id.

    Body:
      {
        "signal_id":     "<original signal_id>",   # required — locates the order
        "quantity":      150,                        # optional, any subset
        "price":         1455.50,                    # optional
        "trigger_price": 1450.00                     # optional (SL/SL-M)
      }

    Auth, IP allowlist, inbox lookup are identical to receive_signal_endpoint.
    Returns the broker's modify response on success; the original signal row
    is left at status='placed' (the order_id stays valid post-modify) and a
    'modified' audit-status note is appended to its error_message column.
    """
    src_ip = _real_source_ip()

    inbox = get_inbox_by_slug(inbox_slug)
    if inbox is None:
        return jsonify({"status": "error", "message": "unknown inbox"}), 404
    if inbox.status != "active":
        return jsonify({"status": "error", "message": "inbox disabled"}), 403

    _auth_ok, _auth_err = _authenticate_inbox_request(inbox)
    if not _auth_ok:
        return jsonify({"status": "error", "message": _auth_err}), 401

    if not _ip_is_allowed(src_ip, inbox.allowed_ips):
        return jsonify({
            "status": "error",
            "message": f"source IP {src_ip} not in this inbox's allowlist",
        }), 403

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"status": "error", "message": "request body must be JSON object"}), 400

    signal_id = str(body.get("signal_id") or "")[:160]
    if not signal_id:
        return jsonify({"status": "error", "message": "signal_id is required"}), 400

    prior, err, code = _lookup_original_for_modify_or_cancel(inbox.id, signal_id)
    if err is not None:
        return jsonify({"status": "error", "message": err}), code

    # At least one of these MUST be present — otherwise there's nothing to modify.
    has_qty = body.get("quantity") is not None
    has_price = body.get("price") is not None
    has_trigger = body.get("trigger_price") is not None
    if not (has_qty or has_price or has_trigger):
        return jsonify({
            "status": "error",
            "message": "provide at least one of quantity, price, trigger_price",
        }), 400

    # Fetch the token for the SAME broker that placed the original — see
    # the module-level note above on why active-broker isn't right here.
    broker = prior.broker_used
    auth_token, terr = _token_for_specific_broker(inbox.user_id, broker)
    if terr is not None:
        return jsonify({"status": "error", "broker": broker, "message": terr}), 503

    # Reconstruct order_data from the original payload + apply the deltas.
    try:
        original_payload = json.loads(prior.payload_json) if prior.payload_json else {}
    except Exception:
        original_payload = {}

    order_data = {
        "apikey": "distribution-internal",
        "strategy": f"distribution:{inbox.name}",
        "orderid": prior.broker_order_id,
        "symbol": str(original_payload.get("symbol", "")).strip().upper(),
        "exchange": str(original_payload.get("exchange", "")).strip().upper(),
        "action": str(original_payload.get("action", "")).strip().upper(),
        "product": str(original_payload.get("product") or _DEFAULT_PRODUCT).strip().upper(),
        "pricetype": str(original_payload.get("pricetype") or _DEFAULT_PRICETYPE).strip().upper(),
        "quantity": str(int(body["quantity"]) if has_qty else original_payload.get("quantity") or 0),
        "price": str(body["price"] if has_price else original_payload.get("price") or 0),
        "trigger_price": str(body["trigger_price"] if has_trigger else original_payload.get("trigger_price") or 0),
        "disclosed_quantity": str(original_payload.get("disclosed_quantity") or 0),
    }

    try:
        from services.modify_order_service import modify_order_with_auth
        success, response_data, http_status = modify_order_with_auth(
            order_data=order_data,
            auth_token=auth_token,
            broker=broker,
            original_data=order_data,
        )
    except Exception as e:
        logger.exception("modify_order_with_auth raised")
        return jsonify({"status": "error", "broker": broker, "message": str(e)}), 500

    if not success:
        err_msg = (response_data or {}).get("message", "unknown modify error")
        # Don't change the original signal's status — order is still placed,
        # just the modify didn't apply. Surface the error in audit message.
        update_signal_status(
            inbox.id, signal_id, "placed",
            error_message=f"last modify failed: {err_msg}",
        )
        return jsonify({
            "status": "error",
            "broker": broker,
            "broker_order_id": prior.broker_order_id,
            "message": err_msg,
            "response": response_data,
        }), http_status or 502

    update_signal_status(
        inbox.id, signal_id, "placed",
        error_message=(
            "modified: " + ", ".join(
                f"{k}={body[k]}" for k in ("quantity", "price", "trigger_price")
                if body.get(k) is not None
            )
        ),
    )
    return jsonify({
        "status": "success",
        "broker": broker,
        "broker_order_id": prior.broker_order_id,
        "response": response_data,
    }), 200


@distribution_bp.route("/distribution/inbox/<inbox_slug>/cancel", methods=["POST"])
def cancel_signal_endpoint(inbox_slug: str):
    """Cancel an existing placed order by signal_id.

    Body: {"signal_id": "<original signal_id>"}

    Auth, IP allowlist, inbox lookup identical to receive_signal_endpoint.
    On broker-side success the original signal row flips to status='cancelled'.
    """
    src_ip = _real_source_ip()

    inbox = get_inbox_by_slug(inbox_slug)
    if inbox is None:
        return jsonify({"status": "error", "message": "unknown inbox"}), 404
    if inbox.status != "active":
        return jsonify({"status": "error", "message": "inbox disabled"}), 403

    _auth_ok, _auth_err = _authenticate_inbox_request(inbox)
    if not _auth_ok:
        return jsonify({"status": "error", "message": _auth_err}), 401

    if not _ip_is_allowed(src_ip, inbox.allowed_ips):
        return jsonify({
            "status": "error",
            "message": f"source IP {src_ip} not in this inbox's allowlist",
        }), 403

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"status": "error", "message": "request body must be JSON object"}), 400

    signal_id = str(body.get("signal_id") or "")[:160]
    if not signal_id:
        return jsonify({"status": "error", "message": "signal_id is required"}), 400

    prior, err, code = _lookup_original_for_modify_or_cancel(inbox.id, signal_id)
    if err is not None:
        return jsonify({"status": "error", "message": err}), code

    broker = prior.broker_used
    auth_token, terr = _token_for_specific_broker(inbox.user_id, broker)
    if terr is not None:
        return jsonify({"status": "error", "broker": broker, "message": terr}), 503

    try:
        from services.cancel_order_service import cancel_order_with_auth
        success, response_data, http_status = cancel_order_with_auth(
            orderid=prior.broker_order_id,
            auth_token=auth_token,
            broker=broker,
            original_data={"orderid": prior.broker_order_id, "apikey": "distribution-internal"},
        )
    except Exception as e:
        logger.exception("cancel_order_with_auth raised")
        return jsonify({"status": "error", "broker": broker, "message": str(e)}), 500

    if not success:
        err_msg = (response_data or {}).get("message", "unknown cancel error")
        # Idempotent cancel: the order may already be TERMINAL at the broker
        # (EOD square-off, a prior cancel that didn't sync, a rejection). If so
        # the desired state is already reached — sync our status to the broker
        # truth and report success, instead of leaving a phantom 'placed'
        # signal the publisher keeps showing in "Pending fills" forever.
        bstatus, _raw = _broker_status_for_order(broker, auth_token, prior.broker_order_id)
        if bstatus in ("cancelled", "rejected"):
            update_signal_status(inbox.id, signal_id, "cancelled", error_message=None)
            clear_pending_bracket(inbox.id, signal_id)
            return jsonify({
                "status": "success",
                "broker": broker,
                "broker_order_id": prior.broker_order_id,
                "action_taken": "already_cancelled",
                "message": f"order already {bstatus} at broker — marked cancelled",
            }), 200
        update_signal_status(
            inbox.id, signal_id, "placed",
            error_message=f"last cancel failed: {err_msg}",
        )
        return jsonify({
            "status": "error",
            "broker": broker,
            "broker_order_id": prior.broker_order_id,
            "message": err_msg,
            "response": response_data,
        }), http_status or 502

    update_signal_status(inbox.id, signal_id, "cancelled", error_message=None)

    # Phase 3.1 — if this was a bracket parent that was cancelled BEFORE the
    # fill_poller saw it complete, clear the pending_bracket_json so the
    # poller doesn't later place children for a cancelled parent. No-op if
    # the spec was already cleared (e.g. children were already placed and
    # the cancel happened post-fill — the cascade below handles those).
    clear_pending_bracket(inbox.id, signal_id)

    # Cascade-cancel bracket children if this was a bracket parent and
    # children already exist (i.e. parent had filled, poller placed them).
    # Probe for the conventional child signal_ids. Returns [] if there
    # were no children.
    cascade = _cascade_cancel_bracket_children(
        inbox=inbox, parent_signal_id=signal_id,
        auth_token=auth_token, broker=broker,
    )

    out = {
        "status": "success",
        "broker": broker,
        "broker_order_id": prior.broker_order_id,
        "response": response_data,
    }
    if cascade:
        out["bracket_cascade"] = cascade
    return jsonify(out), 200


# ---------------------------------------------------------------------------
# EXIT by signal_id (Phase 6)
# ---------------------------------------------------------------------------
#
# Unified "close this position OR cancel it if still pending" action. Decides
# per-broker order status:
#
#   broker status = open / trigger pending → CANCEL the order (no fill yet)
#   broker status = complete                → CLOSE the position (reverse-side
#                                              MARKET order, qty = min(our
#                                              filled_quantity, broker's
#                                              current position) — Layer-1
#                                              style; never overshoot the
#                                              actual broker position)
#   broker status = cancelled / rejected   → NO-OP (already gone)
#
# Always cascades to bracket children (cancel-or-close them too) — without
# this, an EXIT on a bracket parent would leave the SL+TP sitting at broker
# and any one of them could match the market, re-opening the position in
# the opposite direction. Bad.


def _broker_status_for_order(broker: str, auth_token: str, broker_order_id: str) -> tuple[str | None, dict | None]:
    """Pull the order's current normalised status from the broker. Returns
    (normalised_status, raw_order_dict). normalised_status is one of:
    complete | open | partial | trigger pending | cancelled | rejected
    or None if we couldn't read the orderbook."""
    try:
        from services.orderbook_service import get_orderbook_with_auth
        # original_data routes the read to the SANDBOX orderbook when
        # analyze mode is on (entry/cancel/close already pass it — the
        # status read was the one leg still hitting the LIVE broker, so
        # analyze-mode exits 503'd with "could not read order ... refusing
        # to act blindly"; found live 2026-06-11). In LIVE mode the service
        # ignores it and uses auth_token as before.
        success, resp, _http = get_orderbook_with_auth(
            auth_token=auth_token, broker=broker,
            original_data={"apikey": "distribution-internal"},
        )
    except Exception:
        logger.exception("_broker_status_for_order: orderbook fetch raised")
        return None, None
    if not success:
        return None, None
    orders = ((resp or {}).get("data") or {}).get("orders") or []
    for o in orders:
        if str(o.get("orderid") or "") == str(broker_order_id):
            raw_status = (o.get("order_status") or o.get("status") or "").strip().lower()
            # Same synonym map fill_poller uses (kept in sync intentionally).
            if raw_status in {"completed", "executed", "filled"}:
                raw_status = "complete"
            if raw_status in {"open_pending", "pending"}:
                raw_status = "open"
            return raw_status or None, o
    return None, None


def _broker_position_for(broker: str, auth_token: str, symbol: str, exchange: str, product: str) -> int | None:
    """Read the broker's current net position for (symbol, exchange, product).
    Returns the signed net qty (positive long, negative short) or None if we
    can't read the positionbook. The EXIT close path uses this to ensure we
    never overshoot the actual position (Layer-1 close discipline)."""
    try:
        from services.positionbook_service import get_positionbook_with_auth
        # Same analyze-mode routing as _broker_status_for_order above.
        success, resp, _http = get_positionbook_with_auth(
            auth_token=auth_token, broker=broker,
            original_data={"apikey": "distribution-internal"},
        )
    except Exception:
        logger.exception("_broker_position_for: positionbook fetch raised")
        return None
    if not success:
        return None
    # Response-shape difference: the live-broker service returns
    # data={"positions": [...]} while sandbox_get_positions returns data
    # as the bare list — handle both (the dict-only assumption crashed
    # analyze-mode exits with AttributeError, live 2026-06-11).
    data = (resp or {}).get("data")
    if isinstance(data, list):
        positions = data
    else:
        positions = (data or {}).get("positions") or []
    for p in positions:
        if (
            (p.get("symbol") or "").upper() == symbol.upper()
            and (p.get("exchange") or "").upper() == exchange.upper()
            and (p.get("product") or "").upper() == product.upper()
        ):
            # OpenAlgo's normalised positionbook returns 'quantity' as signed
            # net qty. (BUY-side leftover positive, SELL-side negative.)
            try:
                return int(p.get("quantity") or 0)
            except (TypeError, ValueError):
                return 0
    return None  # no row → no position


@distribution_bp.route("/distribution/inbox/<inbox_slug>/exit", methods=["POST"])
def exit_signal_endpoint(inbox_slug: str):
    """Exit a placed signal — cancel if open, close if filled, noop if
    already terminal. Always cascade-cancels any bracket children.

    Body: {"signal_id": "<original signal_id>"}

    Returns:
      {
        "status": "success",
        "action_taken": "cancelled" | "closed" | "already_closed" | "noop",
        "broker": "upstox",
        "broker_order_id": "...",      # original ENTRY order
        "close_order_id": "...",       # set when action_taken=closed
        "closed_quantity": 1,          # set when action_taken=closed
        "bracket_cascade": [...]       # if bracket parent
      }
    """
    src_ip = _real_source_ip()

    inbox = get_inbox_by_slug(inbox_slug)
    if inbox is None:
        return jsonify({"status": "error", "message": "unknown inbox"}), 404
    if inbox.status != "active":
        return jsonify({"status": "error", "message": "inbox disabled"}), 403

    _auth_ok, _auth_err = _authenticate_inbox_request(inbox)
    if not _auth_ok:
        return jsonify({"status": "error", "message": _auth_err}), 401

    if not _ip_is_allowed(src_ip, inbox.allowed_ips):
        return jsonify({
            "status": "error",
            "message": f"source IP {src_ip} not in this inbox's allowlist",
        }), 403

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"status": "error", "message": "request body must be JSON object"}), 400

    signal_id = str(body.get("signal_id") or "")[:160]
    if not signal_id:
        return jsonify({"status": "error", "message": "signal_id is required"}), 400

    # Optional marketable-LIMIT price for the reverse/cover order (the CLOSE
    # leg below). Backward-compatible: when the publisher omits `price` (or
    # sends price <= 0) the close stays a MARKET order exactly as before.
    # IIFL XTS ALGO-enabled accounts REJECT market orders ("e-orders-0002:
    # Market order Or Price 0 is not allowed for ALGO enabled orders"), so the
    # publisher forwards {"price": <float>, "pricetype": "LIMIT"} to force the
    # cover to a LIMIT @ that (caller-chosen, marketable) price. We gate on a
    # positive price: a "LIMIT" pricetype with no/zero price can't produce a
    # valid limit order (XTS also rejects Price 0), so it falls back to MARKET.
    try:
        exit_price = float(body.get("price") or 0)
    except (TypeError, ValueError):
        exit_price = 0.0
    exit_pricetype = str(body.get("pricetype") or "").strip().upper()
    close_as_limit = exit_price > 0 and exit_pricetype in ("", "LIMIT")

    prior, err, code = _lookup_original_for_modify_or_cancel(inbox.id, signal_id)
    if err is not None:
        return jsonify({"status": "error", "message": err}), code

    broker = prior.broker_used
    auth_token, terr = _token_for_specific_broker(inbox.user_id, broker)
    if terr is not None:
        return jsonify({"status": "error", "broker": broker, "message": terr}), 503

    # Reconstruct original payload — needed for both cancel and close paths.
    try:
        payload = json.loads(prior.payload_json) if prior.payload_json else {}
    except Exception:
        payload = {}
    symbol = str(payload.get("symbol", "")).strip().upper()
    exchange = str(payload.get("exchange", "")).strip().upper()
    product = str(payload.get("product") or _DEFAULT_PRODUCT).strip().upper()
    entry_action = str(payload.get("action", "")).strip().upper()
    opposite = _opposite_action(entry_action)

    # 1. Read broker-side status to decide cancel vs close vs noop.
    broker_status, _raw = _broker_status_for_order(broker, auth_token, prior.broker_order_id)
    if broker_status is None:
        return jsonify({
            "status": "error",
            "broker": broker,
            "message": f"could not read order {prior.broker_order_id} from broker orderbook — refusing to act blindly",
        }), 503

    out: dict = {
        "status": "success",
        "broker": broker,
        "broker_order_id": prior.broker_order_id,
        "broker_status_at_exit": broker_status,
    }

    action_taken = "noop"
    if broker_status in {"open", "trigger pending"}:
        # Cancel — order hasn't filled.
        try:
            from services.cancel_order_service import cancel_order_with_auth
            success, response_data, _http = cancel_order_with_auth(
                orderid=prior.broker_order_id,
                auth_token=auth_token,
                broker=broker,
                original_data={"orderid": prior.broker_order_id, "apikey": "distribution-internal"},
            )
        except Exception as e:
            logger.exception("EXIT cancel raised")
            out["status"] = "error"
            out["message"] = str(e)
            return jsonify(out), 500
        if success:
            update_signal_status(inbox.id, signal_id, "cancelled", error_message=None)
            action_taken = "cancelled"
        else:
            err_msg = (response_data or {}).get("message", "cancel failed")
            out["status"] = "error"
            out["message"] = err_msg
            return jsonify(out), 502

    elif broker_status in {"complete", "partial"}:
        # Close — order is filled. Compute the close qty as min(our
        # filled_quantity, broker's current net position) so we never
        # overshoot. The fill_poller has been writing filled_quantity to
        # this row; fall back to the original signal quantity if not set.
        our_filled = prior.filled_quantity or int(payload.get("quantity") or 0)
        if our_filled <= 0:
            # No quantity to close. Treat as already closed.
            update_signal_status(inbox.id, signal_id, "cancelled", error_message="exit: nothing to close")
            action_taken = "already_closed"
        else:
            broker_net = _broker_position_for(broker, auth_token, symbol, exchange, product)
            # broker_net is signed: positive = long, negative = short.
            # We need same-sign as the entry filled qty. If broker shows 0
            # or opposite sign, the position has already been closed
            # somehow (manual close, EOD square-off, etc).
            entry_sign = 1 if entry_action == "BUY" else -1
            if broker_net is None or broker_net * entry_sign <= 0:
                update_signal_status(
                    inbox.id, signal_id, "cancelled",
                    error_message=f"exit: broker shows no matching open position (net={broker_net})",
                )
                action_taken = "already_closed"
            else:
                close_qty = min(our_filled, abs(broker_net))
                # Back-compat default is MARKET @ price 0. When the publisher
                # supplied a positive `price` (IIFL XTS ALGO accounts), place a
                # LIMIT cover at that marketable price instead.
                close_pricetype = "LIMIT" if close_as_limit else "MARKET"
                close_price = str(exit_price) if close_as_limit else "0"
                close_order_data = {
                    "apikey": "distribution-internal",
                    "strategy": f"distribution:{inbox.name}:exit",
                    "symbol": symbol,
                    "exchange": exchange,
                    "action": opposite,
                    "pricetype": close_pricetype,
                    "product": product,
                    "quantity": str(close_qty),
                    "price": close_price,
                    "trigger_price": "0",
                    "disclosed_quantity": str(payload.get("disclosed_quantity") or 0),
                }
                try:
                    from services.place_order_service import place_order_with_auth
                    success, response_data, _http = place_order_with_auth(
                        order_data=close_order_data,
                        auth_token=auth_token,
                        broker=broker,
                        original_data=close_order_data,
                        emit_event=True,
                    )
                except Exception as e:
                    logger.exception("EXIT close placement raised")
                    out["status"] = "error"
                    out["message"] = str(e)
                    return jsonify(out), 500
                if not success:
                    err_msg = (response_data or {}).get("message", "close placement failed")
                    out["status"] = "error"
                    out["message"] = err_msg
                    return jsonify(out), 502
                close_order_id = (
                    (response_data or {}).get("orderid")
                    or (response_data or {}).get("order_id")
                )
                # Record the close as a synthesised signal row with signal_id
                # = "<entry>__exit". Lets the fill_poller track it normally
                # and avoids any cascade collision with __sl / __tp suffixes.
                record_signal(
                    inbox_id=inbox.id,
                    signal_id=signal_id + "__exit",
                    src_ip=src_ip,
                    payload={
                        "_exit_of": signal_id,
                        "symbol": symbol, "exchange": exchange,
                        "action": opposite, "quantity": close_qty,
                        "pricetype": close_pricetype, "product": product,
                        "price": close_price,
                    },
                    status="placed",
                    broker_used=broker,
                    broker_order_id=close_order_id,
                )
                # Mark the original signal as cancelled in our log — the
                # position it represented is being unwound.
                update_signal_status(
                    inbox.id, signal_id, "cancelled",
                    error_message=f"exit: closed via __exit order {close_order_id}",
                )
                action_taken = "closed"
                out["close_order_id"] = close_order_id
                out["closed_quantity"] = close_qty

    else:
        # cancelled / rejected — nothing to do at the broker.
        update_signal_status(
            inbox.id, signal_id, "cancelled",
            error_message=f"exit: order already in terminal state '{broker_status}'",
        )
        action_taken = "already_closed"

    out["action_taken"] = action_taken

    # Clear any pending bracket spec — same reason as cancel webhook.
    clear_pending_bracket(inbox.id, signal_id)

    # Cascade-cancel any bracket children. If parent EXIT closed an open
    # position, leaving SL/TP children open would risk re-opening it on a
    # spurious match. If the parent was just cancelled and never filled,
    # children don't exist yet (Phase 3.1 places them only after fill) —
    # this is a fast no-op.
    cascade = _cascade_cancel_bracket_children(
        inbox=inbox, parent_signal_id=signal_id,
        auth_token=auth_token, broker=broker,
    )
    if cascade:
        out["bracket_cascade"] = cascade

    return jsonify(out), 200


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

    _auth_ok, _auth_err = _authenticate_inbox_request(inbox)
    if not _auth_ok:
        return jsonify({"status": "error", "message": _auth_err}), 401

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


@distribution_bp.route("/distribution/inbox/<inbox_slug>/holdings", methods=["GET"])
def get_holdings_endpoint(inbox_slug: str):
    """Return the current broker holdings (demat portfolio) for the inbox's owner.

    Auth + IP-allowlist + broker-resolve are identical to /positions — the
    publisher presents `Authorization: Bearer <api_key>` and we route through
    the same broker the inbox would route orders through. Read-only.

    The publisher's Holdings tab reads this to show what each customer holds,
    and to let the advisor advise selling a holding. NOTE: a holding-sell is
    a delivery (CNC) SELL of stock the customer already owns — the publisher
    deliberately does NOT track it as a squared-off-able position. This
    endpoint is the read half of that flow.

    Response data shape: {"holdings": [...], "statistics": {...}}, where each
    holding is {symbol, exchange, quantity, product, average_price, pnl,
    pnlpercent} (OpenAlgo-normalized across all brokers).
    """
    src_ip = _real_source_ip()

    inbox = get_inbox_by_slug(inbox_slug)
    if inbox is None:
        return jsonify({"status": "error", "message": "unknown inbox"}), 404
    if inbox.status != "active":
        return jsonify({"status": "error", "message": "inbox disabled"}), 403

    _auth_ok, _auth_err = _authenticate_inbox_request(inbox)
    if not _auth_ok:
        return jsonify({"status": "error", "message": _auth_err}), 401

    if not _ip_is_allowed(src_ip, inbox.allowed_ips):
        return jsonify({
            "status": "error",
            "message": f"source IP {src_ip} not in this inbox's allowlist",
        }), 403

    broker, auth_token, err = _resolve_routing_broker(inbox.user_id, inbox.broker_override)
    if err is not None:
        return jsonify({"status": "error", "broker": broker, "message": err}), 503

    # Paper (analyze) customers: route to the sandbox portfolio via the
    # `distribution-internal` magic apikey (same substitution the fill_poller
    # and exit/cancel paths use). Live customers hit the real broker.
    try:
        from database.settings_db import get_analyze_mode
        analyze_on = bool(get_analyze_mode())
    except Exception:
        analyze_on = False

    try:
        from services.holdings_service import get_holdings, get_holdings_with_auth
        if analyze_on:
            success, response_data, status_code = get_holdings_with_auth(
                auth_token="distribution-internal", broker=broker,
                original_data={"apikey": "distribution-internal"},
            )
        else:
            success, response_data, status_code = get_holdings(
                auth_token=auth_token, broker=broker,
            )
    except Exception as e:
        logger.exception("get_holdings raised")
        return jsonify({"status": "error", "broker": broker, "message": str(e)}), 500

    if not success:
        return jsonify({
            "status": "error",
            "broker": broker,
            "message": (response_data or {}).get("message") or "failed to fetch holdings",
        }), status_code

    from datetime import datetime, timezone
    return jsonify({
        "status": "success",
        "broker": broker,
        "data": (response_data or {}).get("data") or {},
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }), 200


@distribution_bp.route("/distribution/inbox/<inbox_slug>/orderbook", methods=["GET"])
def get_orderbook_endpoint(inbox_slug: str):
    """Return the current broker ORDER BOOK for the inbox's owner. Read-only.

    Same auth / IP-allowlist / broker-resolve as /positions and /holdings. The
    publisher's close flow reads this to find SAME-DAY EXECUTED CNC/DELIVERY
    buys (BTST): those don't yet show in the intraday position book (they're
    delivery, not intraday) nor in settled holdings (they settle T+1), but the
    executed BUY order is right here in the order book, so the position is real
    and closable. Response data: {"orders": [...]} (OpenAlgo-normalized:
    orderid, symbol, exchange, action, product, order_status, quantity,
    filled_quantity, average_price, ...)."""
    src_ip = _real_source_ip()

    inbox = get_inbox_by_slug(inbox_slug)
    if inbox is None:
        return jsonify({"status": "error", "message": "unknown inbox"}), 404
    if inbox.status != "active":
        return jsonify({"status": "error", "message": "inbox disabled"}), 403

    _auth_ok, _auth_err = _authenticate_inbox_request(inbox)
    if not _auth_ok:
        return jsonify({"status": "error", "message": _auth_err}), 401

    if not _ip_is_allowed(src_ip, inbox.allowed_ips):
        return jsonify({
            "status": "error",
            "message": f"source IP {src_ip} not in this inbox's allowlist",
        }), 403

    broker, auth_token, err = _resolve_routing_broker(inbox.user_id, inbox.broker_override)
    if err is not None:
        return jsonify({"status": "error", "broker": broker, "message": err}), 503

    try:
        from database.settings_db import get_analyze_mode
        analyze_on = bool(get_analyze_mode())
    except Exception:
        analyze_on = False

    try:
        from services.orderbook_service import get_orderbook, get_orderbook_with_auth
        if analyze_on:
            success, response_data, status_code = get_orderbook_with_auth(
                "distribution-internal", broker, {"apikey": "distribution-internal"},
            )
        else:
            success, response_data, status_code = get_orderbook(
                auth_token=auth_token, broker=broker,
            )
    except Exception as e:
        logger.exception("get_orderbook raised")
        return jsonify({"status": "error", "broker": broker, "message": str(e)}), 500

    if not success:
        return jsonify({
            "status": "error",
            "broker": broker,
            "message": (response_data or {}).get("message") or "failed to fetch orderbook",
        }), status_code

    from datetime import datetime, timezone
    return jsonify({
        "status": "success",
        "broker": broker,
        "data": (response_data or {}).get("data") or {},
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }), 200


# ===========================================================================
# System endpoints — server-to-server (bearer-authed by PROVISIONER_SHARED_SECRET)
# ===========================================================================
#
# These are NOT session-authed. They're called by hostingsol's landing
# service (or the backfill ops script) to bootstrap a customer's
# Distribution Inbox at provisioning time, then link it to the publisher's
# subscriber row. The flow:
#
#   hostingsol → POST /api/distribution/system/create-inbox      → returns inbox_id + slug + plaintext_api_key
#   hostingsol → POST publisher /api/system/subscribers          → returns subscriber_id
#   hostingsol → POST /api/distribution/system/set-publisher-id  → links inbox_id ↔ subscriber_id
#
# Customer never types an api_key. Auth is the shared PROVISIONER_SHARED_SECRET
# env (one per container, set at provision time).

def _provisioner_authed() -> bool:
    """Bearer auth gate for system endpoints. Returns False if the env var
    is unset (hard-disabled) or the presented token doesn't match."""
    import hmac
    import os

    expected = (os.getenv("PROVISIONER_SHARED_SECRET") or "").strip()
    if not expected:
        return False
    presented = _extract_bearer_or_apikey_header()
    return bool(presented) and hmac.compare_digest(presented, expected)


def _resolve_admin_user_id() -> int | None:
    """alphago_live is single-admin-per-instance — find the one admin row."""
    try:
        from database.user_db import db_session as user_session, User
        admin = user_session.query(User).filter_by(is_admin=True).first()
        return admin.id if admin else None
    except Exception:
        logger.exception("admin user lookup failed in system endpoint")
        return None


@distribution_bp.route("/api/distribution/system/create-inbox", methods=["POST"])
def system_create_inbox():
    """Server-to-server: create a Distribution Inbox for the admin user.

    Idempotent: if the admin already has at least one inbox, returns the
    earliest-created one (and reports `created: false`). For freshly
    provisioned containers it creates one and returns the plaintext api_key.

    Bearer-authed by PROVISIONER_SHARED_SECRET. Returns 503 if that env
    var is unset (hard-disabled state, by design).

    Body (optional):
      {
        "name": "Distribution Inbox",      # default if omitted
        "broker_override": null,           # default: follow active broker
        "force_create": false              # if true, always create a new inbox
                                           # (ignoring any existing one). Used
                                           # by the rotate-key path; provisioner
                                           # should leave it false.
      }
    """
    import os
    if not _provisioner_authed():
        return jsonify({
            "status": "error",
            "message": "system endpoint requires PROVISIONER_SHARED_SECRET bearer token "
                       "(or env var is unset on this container)",
        }), 401 if os.getenv("PROVISIONER_SHARED_SECRET") else 503

    admin_id = _resolve_admin_user_id()
    if admin_id is None:
        return jsonify({
            "status": "error",
            "message": "no admin user exists yet on this container — finish web setup first",
        }), 503

    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "Distribution Inbox").strip() or "Distribution Inbox"
    broker_override = body.get("broker_override") or None
    force_create = bool(body.get("force_create", False))

    from database.distribution_db import get_first_inbox_for_user

    # Idempotency: if there's already an inbox, reuse it (no plaintext key
    # is returned — the customer rotates explicitly if they need a fresh one).
    if not force_create:
        existing = get_first_inbox_for_user(admin_id)
        if existing is not None:
            return jsonify({
                "status": "success",
                "created": False,
                "data": {
                    "inbox_id": existing.id,
                    "inbox_slug": existing.inbox_slug,
                    "api_key_last4": existing.api_key_last4,
                    "publisher_subscriber_id": existing.publisher_subscriber_id,
                    "api_key": None,    # plaintext NOT available post-create
                    # Derived from the inbox — recomputable even though the
                    # plaintext api_key is gone. Lets the provisioner / backfill
                    # enable HMAC signing on an existing subscriber.
                    "signing_secret": derive_signing_secret(existing),
                },
            })

    try:
        row, plaintext = create_inbox(
            user_id=admin_id,
            name=name,
            broker_override=broker_override,
        )
    except Exception as e:
        logger.exception("system_create_inbox failed")
        return jsonify({"status": "error", "message": str(e)}), 500

    logger.info(
        f"system create-inbox: admin_id={admin_id} inbox_id={row.id} slug={row.inbox_slug} "
        f"(server-to-server)"
    )
    return jsonify({
        "status": "success",
        "created": True,
        "data": {
            "inbox_id": row.id,
            "inbox_slug": row.inbox_slug,
            "api_key_last4": row.api_key_last4,
            "publisher_subscriber_id": None,
            "api_key": plaintext,    # shown ONCE — caller must register with publisher immediately
            "signing_secret": derive_signing_secret(row),
        },
    })


@distribution_bp.route("/api/distribution/system/signing-secret", methods=["POST"])
def system_signing_secret():
    """Server-to-server: return the HMAC signing secret for a specific inbox
    slug. Lets the publisher (provisioner-authed) enable HMAC signing on an
    existing subscriber — it knows the slug from the stored webhook_url. The
    secret is *derived* from the inbox, so this works for any existing inbox
    without its plaintext api_key.

    Bearer-authed by PROVISIONER_SHARED_SECRET. Body: {"inbox_slug": "..."}.
    """
    import os
    if not _provisioner_authed():
        return jsonify({
            "status": "error",
            "message": "system endpoint requires PROVISIONER_SHARED_SECRET bearer token",
        }), 401 if os.getenv("PROVISIONER_SHARED_SECRET") else 503

    body = request.get_json(silent=True) or {}
    slug = (body.get("inbox_slug") or "").strip()
    if not slug:
        return jsonify({"status": "error", "message": "inbox_slug is required"}), 400

    inbox = get_inbox_by_slug(slug)
    if inbox is None:
        return jsonify({"status": "error", "message": "unknown inbox slug"}), 404
    return jsonify({
        "status": "success",
        "data": {
            "inbox_slug": inbox.inbox_slug,
            "signing_secret": derive_signing_secret(inbox),
        },
    })


@distribution_bp.route("/api/distribution/system/set-publisher-subscriber-id", methods=["POST"])
def system_set_publisher_subscriber_id():
    """Link a local inbox to its row on publisher.alphaquark.in. Called by
    hostingsol's provisioner right after it creates the subscriber row on
    publisher and learns its id.

    Bearer-authed by PROVISIONER_SHARED_SECRET.

    Body:
      {
        "inbox_id": 1,
        "publisher_subscriber_id": 42
      }
    """
    import os
    if not _provisioner_authed():
        return jsonify({"status": "error", "message": "auth required"}), \
            (401 if os.getenv("PROVISIONER_SHARED_SECRET") else 503)

    body = request.get_json(silent=True) or {}
    try:
        inbox_id = int(body.get("inbox_id") or 0)
        sub_id = int(body.get("publisher_subscriber_id") or 0)
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "inbox_id and publisher_subscriber_id required (int)"}), 400
    if not inbox_id or not sub_id:
        return jsonify({"status": "error", "message": "inbox_id and publisher_subscriber_id required (int)"}), 400

    from database.distribution_db import set_publisher_subscriber_id as _set
    if not _set(inbox_id, sub_id):
        return jsonify({"status": "error", "message": "inbox not found"}), 404
    return jsonify({"status": "success"})


@distribution_bp.route("/api/distribution/system/inbox-info", methods=["GET"])
def system_inbox_info():
    """Returns existing inbox metadata for the admin user. Used by the
    backfill script to discover which existing customers don't yet have
    a publisher_subscriber_id set, so it can register them.
    Bearer-authed by PROVISIONER_SHARED_SECRET."""
    import os
    if not _provisioner_authed():
        return jsonify({"status": "error", "message": "auth required"}), \
            (401 if os.getenv("PROVISIONER_SHARED_SECRET") else 503)

    admin_id = _resolve_admin_user_id()
    if admin_id is None:
        return jsonify({"status": "success", "data": []})
    return jsonify({"status": "success", "data": list_inboxes(admin_id)})


@distribution_bp.route("/api/distribution/system/rotate-inbox", methods=["POST"])
def system_rotate_inbox():
    """Server-to-server: rotate an existing inbox's api_key IN PLACE (same
    slug) and return the fresh plaintext key + signing secret.

    This is the migration re-sync primitive: when a customer's container is
    migrated, hostingsol calls this to mint a fresh key, then pushes it to the
    publisher (POST /api/system/subscribers/<id>/credentials) so the
    publisher→container link can't silently drift to a 401. Unlike
    create-inbox force_create, this keeps the SAME slug/webhook_url — only the
    key changes.

    Bearer-authed by PROVISIONER_SHARED_SECRET. Body: {"inbox_slug": "..."}.
    """
    import os
    if not _provisioner_authed():
        return jsonify({
            "status": "error",
            "message": "system endpoint requires PROVISIONER_SHARED_SECRET bearer token",
        }), 401 if os.getenv("PROVISIONER_SHARED_SECRET") else 503

    body = request.get_json(silent=True) or {}
    slug = (body.get("inbox_slug") or "").strip()
    if not slug:
        return jsonify({"status": "error", "message": "inbox_slug is required"}), 400

    inbox = get_inbox_by_slug(slug)
    if inbox is None:
        return jsonify({"status": "error", "message": "unknown inbox slug"}), 404

    result = rotate_api_key(inbox.user_id, inbox.id)
    if result is None:
        return jsonify({"status": "error", "message": "rotate failed — inbox not found"}), 404
    row, plaintext = result
    logger.info(
        f"system rotate-inbox: inbox_id={row.id} slug={row.inbox_slug} "
        f"new_last4={row.api_key_last4} (server-to-server)"
    )
    return jsonify({
        "status": "success",
        "data": {
            "inbox_id": row.id,
            "inbox_slug": row.inbox_slug,
            "api_key_last4": row.api_key_last4,
            "publisher_subscriber_id": row.publisher_subscriber_id,
            "api_key": plaintext,           # shown ONCE — caller must push to publisher now
            "signing_secret": derive_signing_secret(row),
            "webhook_url": _build_webhook_url(row.inbox_slug),
        },
    })

