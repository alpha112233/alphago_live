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


# ===========================================================================
# Customer-facing picker endpoints (Flask session auth)
# ===========================================================================
#
# The customer's dashboard uses these to fetch the list of strategy authors
# (admins on publisher.alphaquark.in) and self-pick which one they want
# their signals dispatched from.

@distribution_bp.route("/api/distribution/admins/list", methods=["GET"])
def list_strategy_admins():
    """Proxy to publisher's /api/system/admins/list-public.

    The bearer secret + publisher URL are container-side env vars set at
    provisioning time:
      PUBLISHER_BASE_URL          (e.g. https://publisher.alphaquark.in)
      PUBLISHER_SHARED_SECRET     (the same value as HOSTINGSOL_SHARED_SECRET
                                    on the publisher side)
    """
    import os
    if _current_user_id() is None:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    publisher_base = (os.getenv("PUBLISHER_BASE_URL") or "").rstrip("/")
    secret = (os.getenv("PUBLISHER_SHARED_SECRET") or "").strip()
    if not publisher_base or not secret:
        # Soft-fail: empty list. Lets the UI render without crashing on
        # containers that aren't part of the hostingsol fleet.
        return jsonify({
            "status": "success",
            "data": [],
            "_note": "PUBLISHER_BASE_URL / PUBLISHER_SHARED_SECRET not configured",
        })

    try:
        import requests
        resp = requests.get(
            f"{publisher_base}/api/system/admins/list-public",
            headers={"Authorization": f"Bearer {secret}"},
            timeout=10,
        )
        body = resp.json()
        if resp.status_code != 200 or body.get("status") != "success":
            logger.warning(f"publisher list-public returned {resp.status_code}: {body}")
            return jsonify({"status": "error", "message": body.get("message") or "publisher error"}), 502
        return jsonify({"status": "success", "data": body.get("data") or []})
    except Exception as e:
        logger.exception("list_strategy_admins failed")
        return jsonify({"status": "error", "message": f"publisher unreachable: {e}"}), 502


@distribution_bp.route("/api/distribution/inboxes/<int:inbox_id>/pick-admin", methods=["POST"])
def pick_strategy_admin(inbox_id: int):
    """Customer picks a Strategy Provider for this inbox. We forward the
    pick to publisher's /api/system/subscribers/<sub_id>/reassign-by-customer
    using the inbox's API key as the proof of ownership.

    Body:
      {
        "target_admin_id": 17,
        "api_key": "<the plaintext bearer key the customer kept from inbox
                     creation — surfaced once at inbox-create time>"
      }
    """
    import os
    user_id = _current_user_id()
    if user_id is None:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    body = request.get_json(silent=True) or {}
    try:
        target_admin_id = int(body.get("target_admin_id") or 0)
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "target_admin_id must be int"}), 400
    if not target_admin_id:
        return jsonify({"status": "error", "message": "target_admin_id required"}), 400

    api_key = (body.get("api_key") or "").strip()
    if not api_key:
        return jsonify({
            "status": "error",
            "message": "api_key required — the plaintext bearer key shown when the inbox was created. "
                       "If you've lost it, rotate the key (Distribution Inbox → Rotate) and try again.",
        }), 400

    # Verify the customer actually owns this inbox + key.
    from database.distribution_db import check_api_key
    from database.distribution_db import db_session as dist_session, DistributionInbox
    inbox = (dist_session.query(DistributionInbox)
             .filter_by(id=inbox_id, user_id=user_id).first())
    if inbox is None:
        return jsonify({"status": "error", "message": "inbox not found"}), 404
    if not check_api_key(inbox, api_key):
        return jsonify({"status": "error", "message": "api_key does not match this inbox"}), 401
    if inbox.publisher_subscriber_id is None:
        return jsonify({
            "status": "error",
            "message": "this inbox is not registered with the upstream publisher yet — "
                       "ask the operator to run the backfill script or re-trigger provisioning.",
        }), 409

    publisher_base = (os.getenv("PUBLISHER_BASE_URL") or "").rstrip("/")
    if not publisher_base:
        return jsonify({
            "status": "error",
            "message": "PUBLISHER_BASE_URL not configured on this container",
        }), 503

    try:
        import requests
        resp = requests.post(
            f"{publisher_base}/api/system/subscribers/{inbox.publisher_subscriber_id}/reassign-by-customer",
            json={"api_key": api_key, "target_admin_id": target_admin_id},
            timeout=10,
        )
        out = resp.json()
        if resp.status_code != 200 or out.get("status") != "success":
            logger.warning(f"publisher reassign returned {resp.status_code}: {out}")
            return jsonify({
                "status": "error",
                "message": out.get("message") or "publisher rejected the pick",
            }), resp.status_code if resp.status_code >= 400 else 502
        logger.info(
            f"customer picked admin: inbox_id={inbox_id} publisher_sub_id={inbox.publisher_subscriber_id} "
            f"target_admin_id={target_admin_id}"
        )
        return jsonify({"status": "success", "data": out.get("data")})
    except Exception as e:
        logger.exception("pick_strategy_admin failed")
        return jsonify({"status": "error", "message": f"publisher unreachable: {e}"}), 502
