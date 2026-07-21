# blueprints/broker_credentials.py
"""
Broker credentials management API.
Handles reading and updating broker credentials in the .env file.
"""

import os

from database.instance_config_db import get_config
import re

from flask import Blueprint, jsonify, request

from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

broker_credentials_bp = Blueprint("broker_credentials_bp", __name__, url_prefix="/api/broker")


def get_env_path():
    """Get the absolute path to the .env file."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(base_dir, "..", ".env"))


def read_env_file():
    """Read and parse the .env file into a dictionary of lines."""
    env_path = get_env_path()
    if not os.path.exists(env_path):
        return None, "Environment file not found"

    try:
        # Use UTF-8 encoding for cross-platform compatibility
        with open(env_path, encoding="utf-8") as f:
            return f.read(), None
    except Exception as e:
        logger.exception(f"Error reading .env file: {e}")
        return None, str(e)


def update_env_value(content: str, key: str, value: str) -> str:
    """Update a specific key's value in the .env content.

    Uses single quotes for values. This is compatible with python-dotenv
    and most .env parsers across platforms.
    """
    # Pattern to match the key with various formats
    # Handles: KEY = 'value', KEY = "value", KEY = value, KEY='value', etc.
    pattern = rf"^({re.escape(key)}\s*=\s*).*$"

    # Always wrap in single quotes for consistency
    # Single quotes in .env files don't require escaping in most parsers
    # If value contains single quotes, use double quotes instead
    if "'" in value:
        # Use double quotes, escape any existing double quotes and backslashes
        escaped_value = value.replace("\\", "\\\\").replace('"', '\\"')
        new_value = f'"{escaped_value}"'
    else:
        # Use single quotes (no escaping needed)
        new_value = f"'{value}'"

    replacement = rf"\g<1>{new_value}"

    # Try to replace existing key
    new_content, count = re.subn(pattern, replacement, content, flags=re.MULTILINE)

    if count == 0:
        # Key doesn't exist, append it
        if not new_content.endswith("\n"):
            new_content += "\n"
        new_content += f"{key} = {new_value}\n"

    return new_content


def get_env_value(key: str) -> str:
    """Get a value from the .env file."""
    return os.getenv(key, "")


def mask_secret(value: str, show_chars: int = 4) -> str:
    """Mask a secret value, showing only the first few characters.

    Returns a FIXED-length output (``prefix + '*' * 8``) regardless of the
    original secret's length. This intentionally hides the secret's true
    length so an over-the-shoulder viewer (or a screenshot) cannot infer
    "this is a 64-char Zerodha API secret" vs "this is a 32-char Fyers
    secret" from the asterisk count.

    The fixed-length mask also keeps the rendered value bounded so a long
    secret (some brokers issue 80+ char tokens) cannot overflow the
    Profile UI's column layout — the bug originally reported in the
    Current Configuration card where the asterisks ran past the right
    edge of the card.

    For empty values, returns "" so the frontend can detect "not set" and
    show its placeholder copy.
    """
    if not value:
        return ""
    if len(value) <= show_chars:
        # Edge case: secret shorter than the prefix budget. Show only the
        # mask suffix to avoid revealing the entire short value.
        return "*" * 8
    return value[:show_chars] + "*" * 8


def get_broker_from_redirect_url(redirect_url: str) -> str:
    """Extract broker name from redirect URL."""
    try:
        match = re.search(r"/([^/]+)/callback$", redirect_url)
        if match:
            return match.group(1).lower()
    except Exception:
        pass
    return ""


@broker_credentials_bp.route("/credentials", methods=["GET"])
@check_session_validity
def get_credentials():
    """Get current broker credentials (masked)."""
    try:
        # Get current values from environment
        broker_api_key = get_env_value("BROKER_API_KEY")
        broker_api_secret = get_env_value("BROKER_API_SECRET")
        broker_api_key_market = get_env_value("BROKER_API_KEY_MARKET")
        broker_api_secret_market = get_env_value("BROKER_API_SECRET_MARKET")
        redirect_url = get_env_value("REDIRECT_URL")
        valid_brokers = get_env_value("VALID_BROKERS")
        ngrok_allow = get_env_value("NGROK_ALLOW")
        host_server = get_env_value("HOST_SERVER")
        websocket_url = get_env_value("WEBSOCKET_URL")

        # Get port configuration
        flask_host = get_env_value("FLASK_HOST_IP") or "127.0.0.1"
        flask_port = get_env_value("FLASK_PORT") or "5000"
        websocket_host = get_env_value("WEBSOCKET_HOST") or "127.0.0.1"
        websocket_port = get_env_value("WEBSOCKET_PORT") or "8765"
        zmq_host = get_env_value("ZMQ_HOST") or "127.0.0.1"
        zmq_port = get_env_value("ZMQ_PORT") or "5555"

        # Get current broker from redirect URL
        current_broker = get_broker_from_redirect_url(redirect_url)

        # Parse valid brokers list
        brokers_list = [b.strip() for b in valid_brokers.split(",") if b.strip()]

        return jsonify(
            {
                "status": "success",
                "data": {
                    "broker_api_key": mask_secret(broker_api_key, 6),
                    "broker_api_key_raw_length": len(broker_api_key),
                    "broker_api_secret": mask_secret(broker_api_secret, 4),
                    "broker_api_secret_raw_length": len(broker_api_secret),
                    "broker_api_key_market": mask_secret(broker_api_key_market, 6),
                    "broker_api_key_market_raw_length": len(broker_api_key_market),
                    "broker_api_secret_market": mask_secret(broker_api_secret_market, 4),
                    "broker_api_secret_market_raw_length": len(broker_api_secret_market),
                    "redirect_url": redirect_url,
                    "current_broker": current_broker,
                    "valid_brokers": brokers_list,
                    "ngrok_allow": ngrok_allow.upper() == "TRUE",
                    "host_server": host_server,
                    "websocket_url": websocket_url,
                    # Server status info
                    "server_status": {
                        "flask": {"host": flask_host, "port": flask_port},
                        "websocket": {"host": websocket_host, "port": websocket_port},
                        "zmq": {"host": zmq_host, "port": zmq_port},
                    },
                },
            }
        )
    except Exception as e:
        logger.exception(f"Error getting broker credentials: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@broker_credentials_bp.route("/credentials", methods=["POST"])
@check_session_validity
def update_credentials():
    """Update broker credentials in .env file."""
    try:
        # Support both JSON and form data
        if request.is_json:
            data = request.get_json() or {}
            broker_api_key = data.get("broker_api_key", "").strip()
            broker_api_secret = data.get("broker_api_secret", "").strip()
            broker_api_key_market = data.get("broker_api_key_market", "").strip()
            broker_api_secret_market = data.get("broker_api_secret_market", "").strip()
            redirect_url = data.get("redirect_url", "").strip()
            ngrok_allow = data.get("ngrok_allow", "")
            host_server = data.get("host_server", "").strip()
            websocket_url = data.get("websocket_url", "").strip()
            has_ngrok_key = "ngrok_allow" in data
        else:
            # Form data
            broker_api_key = request.form.get("broker_api_key", "").strip()
            broker_api_secret = request.form.get("broker_api_secret", "").strip()
            broker_api_key_market = request.form.get("broker_api_key_market", "").strip()
            broker_api_secret_market = request.form.get("broker_api_secret_market", "").strip()
            redirect_url = request.form.get("redirect_url", "").strip()
            ngrok_allow = request.form.get("ngrok_allow", "").strip()
            host_server = request.form.get("host_server", "").strip()
            websocket_url = request.form.get("websocket_url", "").strip()
            has_ngrok_key = "ngrok_allow" in request.form

        # Validate redirect URL format
        if redirect_url:
            if not re.match(r"^https?://.+/[^/]+/callback$", redirect_url):
                return jsonify(
                    {
                        "status": "error",
                        "message": "Invalid redirect URL format. Must end with /<broker>/callback",
                    }
                ), 400

            # Validate broker name
            broker_name = get_broker_from_redirect_url(redirect_url)
            valid_brokers_str = get_env_value("VALID_BROKERS")
            valid_brokers = set(
                b.strip().lower() for b in valid_brokers_str.split(",") if b.strip()
            )

            if broker_name and broker_name not in valid_brokers:
                return jsonify(
                    {
                        "status": "error",
                        "message": f"Invalid broker '{broker_name}'. Valid brokers: {', '.join(sorted(valid_brokers))}",
                    }
                ), 400

            # Validate broker-specific API key formats
            if broker_name == "fivepaisa" and broker_api_key:
                if ":::" not in broker_api_key or broker_api_key.count(":::") != 2:
                    return jsonify(
                        {
                            "status": "error",
                            "message": "5paisa API key must be in format: 'User_Key:::User_ID:::client_id'",
                        }
                    ), 400

            elif broker_name == "flattrade" and broker_api_key:
                if ":::" not in broker_api_key or broker_api_key.count(":::") != 1:
                    return jsonify(
                        {
                            "status": "error",
                            "message": "Flattrade API key must be in format: 'client_id:::api_key'",
                        }
                    ), 400

            elif broker_name == "dhan" and broker_api_key:
                if ":::" not in broker_api_key or broker_api_key.count(":::") != 1:
                    return jsonify(
                        {
                            "status": "error",
                            "message": "Dhan API key must be in format: 'client_id:::api_key'",
                        }
                    ), 400

        # Read current .env content
        content, error = read_env_file()
        if error:
            return jsonify(
                {"status": "error", "message": f"Failed to read .env file: {error}"}
            ), 500

        # Track what was updated
        updated_fields = []

        # Update values (only if provided - empty string means keep existing)
        if broker_api_key:
            content = update_env_value(content, "BROKER_API_KEY", broker_api_key)
            updated_fields.append("BROKER_API_KEY")

        if broker_api_secret:
            content = update_env_value(content, "BROKER_API_SECRET", broker_api_secret)
            updated_fields.append("BROKER_API_SECRET")

        if broker_api_key_market:
            content = update_env_value(content, "BROKER_API_KEY_MARKET", broker_api_key_market)
            updated_fields.append("BROKER_API_KEY_MARKET")

        if broker_api_secret_market:
            content = update_env_value(
                content, "BROKER_API_SECRET_MARKET", broker_api_secret_market
            )
            updated_fields.append("BROKER_API_SECRET_MARKET")

        if redirect_url:
            content = update_env_value(content, "REDIRECT_URL", redirect_url)
            updated_fields.append("REDIRECT_URL")

        # Check for ngrok_allow by key presence, not value truthiness
        # This allows setting it to FALSE (disabling ngrok)
        if has_ngrok_key:
            ngrok_allow_str = str(ngrok_allow).strip().upper()
            ngrok_value = "TRUE" if ngrok_allow_str == "TRUE" else "FALSE"
            content = update_env_value(content, "NGROK_ALLOW", ngrok_value)
            updated_fields.append("NGROK_ALLOW")

        if host_server:
            # Validate host_server URL format
            if not re.match(r"^https?://.+", host_server):
                return jsonify(
                    {
                        "status": "error",
                        "message": "Invalid HOST_SERVER format. Must start with http:// or https://",
                    }
                ), 400
            content = update_env_value(content, "HOST_SERVER", host_server)
            updated_fields.append("HOST_SERVER")

        if websocket_url:
            # Validate websocket_url format
            if not re.match(r"^wss?://.+", websocket_url):
                return jsonify(
                    {
                        "status": "error",
                        "message": "Invalid WEBSOCKET_URL format. Must start with ws:// or wss://",
                    }
                ), 400
            content = update_env_value(content, "WEBSOCKET_URL", websocket_url)
            updated_fields.append("WEBSOCKET_URL")

        if not updated_fields:
            return jsonify({"status": "error", "message": "No credentials provided to update"}), 400

        # Write updated content back to .env
        env_path = get_env_path()
        try:
            # Use UTF-8 encoding for cross-platform compatibility
            with open(env_path, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info(f"Updated broker credentials: {', '.join(updated_fields)}")
        except Exception as e:
            logger.exception(f"Error writing .env file: {e}")
            return jsonify({"status": "error", "message": f"Failed to write .env file: {e}"}), 500

        return jsonify(
            {
                "status": "success",
                "message": f"Credentials updated successfully. Updated: {', '.join(updated_fields)}",
                "updated_fields": updated_fields,
                "restart_required": True,
            }
        )

    except Exception as e:
        logger.exception(f"Error updating broker credentials: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@broker_credentials_bp.route("/capabilities", methods=["GET"])
@check_session_validity
def get_capabilities():
    """Return broker capabilities (supported exchanges, type, features) from cached plugin.json."""
    from flask import session

    from utils.plugin_loader import get_broker_capabilities

    broker = session.get("broker")
    if not broker:
        return jsonify({"status": "error", "message": "No broker in session"}), 400

    capabilities = get_broker_capabilities(broker)
    if not capabilities:
        # Fallback for brokers without plugin.json capabilities
        return jsonify(
            {
                "status": "success",
                "data": {
                    "broker_name": broker,
                    "broker_type": "IN_stock",
                    "supported_exchanges": [],
                    "leverage_config": False,
                },
            }
        )

    return jsonify({"status": "success", "data": capabilities})


# =============================================================================
# Multi-broker endpoints (alphago_live fork addition)
#
# Upstream OpenAlgo treats `.env` as the source of truth for the ONE broker
# this instance can use. The endpoints below add a per-user `broker_creds`
# table that stores credentials for many brokers and tracks which one is
# "active". Activating a broker syncs its creds back to `.env` so the rest
# of OpenAlgo (brlogin.py, auth.py — they still read os.getenv) keeps
# working unchanged. The full decoupling of those modules from .env lands in
# follow-up patches (auth.py and brlogin.py refactors).
# =============================================================================


def _current_user_id() -> int | None:
    """Resolve the logged-in user to a database id. Returns None if not signed in.

    OpenAlgo is single-admin-per-instance, but using user_id keeps the DB
    schema clean and future-proof for multi-user (if anyone ever wants that).
    """
    from flask import session
    from database.user_db import User, db_session

    username = session.get("user")
    if not username:
        return None
    user = db_session.query(User).filter_by(username=username).first()
    return user.id if user else None


def _build_redirect_url(broker: str) -> str:
    """Build the broker OAuth callback URL using HOST_SERVER from .env."""
    host = (get_env_value("HOST_SERVER") or "").rstrip("/")
    if not host:
        return ""
    return f"{host}/{broker}/callback"


def _sync_active_broker_to_env(creds: dict, broker: str) -> tuple[bool, str | None]:
    """Write the active broker's credentials to .env AND to os.environ.

    Source of truth is broker_creds_db (Fernet-encrypted at rest). This
    function ONLY updates the in-process `os.environ` cache so the existing
    auth.py / brlogin.py read paths (`os.getenv('BROKER_API_KEY')`) see the
    just-activated broker without restart.

    Does NOT write to /app/.env on disk — that would defeat the at-rest
    encryption (the .env mount is plaintext) and the file is mounted as a
    single file, not a directory, so atomic-rename fails with EACCES anyway.
    Restart-persistence comes from app.py's bootstrap hook that re-populates
    os.environ from broker_creds_db at startup.

    NOTE: single-worker eventlet gunicorn is what OpenAlgo runs, so a single
    os.environ update is visible to every request. If we ever switch to
    multi-worker gunicorn we need to move to a shared in-memory store.

    Returns (ok, err_message).
    """
    redirect_url = _build_redirect_url(broker)
    os.environ["BROKER_API_KEY"] = creds.get("api_key", "") or ""
    os.environ["BROKER_API_SECRET"] = creds.get("api_secret", "") or ""
    os.environ["BROKER_API_KEY_MARKET"] = creds.get("api_key_market", "") or ""
    os.environ["BROKER_API_SECRET_MARKET"] = creds.get("api_secret_market", "") or ""
    if redirect_url:
        os.environ["REDIRECT_URL"] = redirect_url
    # IIFL XTS per-customer base URLs — take effect on the very next Connect
    # (no restart) when the customer enters them in the UI and saves. Rebuild the
    # httpx client so the new host's v4-proxy mount is active for the login call.
    try:
        from utils.broker_env_bootstrap import apply_xts_env
        apply_xts_env(creds.get("extra") or {})
        if (creds.get("extra") or {}).get("base_url") or (creds.get("extra") or {}).get("base_url_market"):
            from utils.httpx_client import reset_httpx_client
            reset_httpx_client()
    except Exception:
        logger.exception("apply_xts_env failed on save/activate")
    return True, None


@broker_credentials_bp.route("/credentials/list", methods=["GET"])
def list_credentials_endpoint():
    """List all brokers saved by the current user. No secrets in the response."""
    from database.broker_creds_db import list_user_brokers

    user_id = _current_user_id()
    if user_id is None:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    return jsonify({"status": "success", "data": list_user_brokers(user_id)})


@broker_credentials_bp.route("/credentials/save", methods=["POST"])
def save_credentials_endpoint():
    """Save (or update) credentials for a broker. Optionally activate it.

    Request JSON shape:
        {
          "broker": "zerodha",
          "api_key": "...",
          "api_secret": "...",
          "api_key_market": "",            # optional
          "api_secret_market": "",         # optional
          "client_code": "",               # optional
          "totp_seed": "",                 # optional (for auto-login)
          "extra": { "mpin": "1234" },     # optional broker-specific
          "notes": "",                     # optional user label
          "activate": false                # if true, make this the active broker
        }
    """
    from database.broker_creds_db import (
        add_or_update_broker_creds,
        activate_broker,
        get_broker_creds,
    )

    user_id = _current_user_id()
    if user_id is None:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    broker = (data.get("broker") or "").strip().lower()
    if not broker:
        return jsonify({"status": "error", "message": "'broker' is required"}), 400

    # api_key handling: required on first save, optional on Edit (blank
    # means "keep what's saved"). Look up the existing row to decide.
    existing = get_broker_creds(user_id, broker)
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        if existing and existing.get("api_key"):
            api_key = existing["api_key"]   # preserve
        else:
            return jsonify({"status": "error", "message": "'api_key' is required"}), 400

    # Merge "extra" the same way: callers on Edit send only the keys
    # they're changing, blank values for the rest. Preserve existing
    # sub-keys when the incoming value is empty.
    incoming_extra = data.get("extra") if isinstance(data.get("extra"), dict) else {}
    existing_extra = (existing or {}).get("extra") or {}
    merged_extra: dict = dict(existing_extra)
    for k, v in (incoming_extra or {}).items():
        if v is None or (isinstance(v, str) and not v.strip()):
            continue   # blank → preserve existing
        merged_extra[k] = v
    extra_to_save = merged_extra if merged_extra else None

    # Optional pre-save validation — cheap config-only check per broker, to
    # surface obvious mismatches (Upstox redirect_uri, etc.) at save time
    # instead of letting the user discover them only at auto-login time.
    from broker_login_adapters import precheck_for
    precheck = precheck_for(broker)
    if precheck is not None:
        precheck_result = precheck({
            "api_key": api_key,
            "redirect_uri": _build_redirect_url(broker),
        })
        if not precheck_result.get("ok"):
            return jsonify({
                "status": "error",
                "message": precheck_result.get("error") or "Pre-save validation failed",
            }), 400

    # Validate TOTP seed at save time — catches paste mistakes (JWT instead
    # of base32, copied with formatting characters, etc.) before they show
    # up as opaque "Invalid TOTP" rejections at auto-login. Empty stays as
    # None so the "leave blank to keep existing" UX (commit 8a26a363) works.
    raw_totp_seed = (data.get("totp_seed") or "").strip()
    totp_seed_to_save: str | None = None
    if raw_totp_seed:
        normalized = raw_totp_seed.replace(" ", "").replace("-", "").upper()
        import base64
        try:
            pad = "=" * (-len(normalized) % 8)
            base64.b32decode(normalized + pad, casefold=False)
        except Exception:
            return jsonify({
                "status": "error",
                "message": (
                    "TOTP seed is not valid base32. Common mistakes: pasted "
                    "the JWT-style 'TOTP Token' instead of the base32 seed "
                    "shown below the QR code; copied trailing whitespace; "
                    "or used the wrong field. Re-open your broker's 2FA "
                    "setup screen and copy only the secret string (looks "
                    "like 'JBSWY3DPEHPK3PXP')."
                ),
            }), 400
        totp_seed_to_save = normalized

    try:
        add_or_update_broker_creds(
            user_id=user_id,
            broker=broker,
            api_key=api_key,
            api_secret=(data.get("api_secret") or "").strip() or None,
            api_key_market=(data.get("api_key_market") or "").strip() or None,
            api_secret_market=(data.get("api_secret_market") or "").strip() or None,
            client_code=(data.get("client_code") or "").strip() or None,
            totp_seed=totp_seed_to_save,
            extra=extra_to_save,
            notes=(data.get("notes") or "").strip() or None,
        )
    except Exception as e:
        logger.exception("Failed to save broker creds")
        return jsonify({"status": "error", "message": str(e)}), 500

    if data.get("activate"):
        activate_broker(user_id, broker)
        creds = get_broker_creds(user_id, broker) or {}
        ok, err = _sync_active_broker_to_env(creds, broker)
        if not ok:
            return jsonify({"status": "error", "message": f"saved but env sync failed: {err}"}), 500

    return jsonify({"status": "success", "broker": broker, "activated": bool(data.get("activate"))})


@broker_credentials_bp.route("/credentials/<broker>/activate", methods=["PUT"])
def activate_credentials_endpoint(broker: str):
    """Make `broker` the active broker for this user. Syncs creds to .env.

    Phase 7.6 gate: if the broker is v4-only AND this container has no
    dedicated v4 IP yet, refuse with 409 + needs_v4_ip flag so the
    frontend renders a 'Request dedicated IPv4 IP' button instead of a
    raw error."""
    from database.broker_creds_db import activate_broker, get_broker_creds
    from utils.decodo_proxy import broker_needs_v4

    user_id = _current_user_id()
    if user_id is None:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    broker = (broker or "").strip().lower()

    if broker_needs_v4(broker) and not get_config("EGRESS_V4_PRIMARY_IP"):
        return jsonify({
            "status": "error",
            "needs_v4_ip": True,
            "message": (
                f"{broker} requires a dedicated IPv4 IP for broker API access. "
                "Click 'Request dedicated IPv4 IP', whitelist the assigned IP in "
                f"your {broker} developer console, then activate again."
            ),
        }), 409

    previous_broker = os.getenv("BROKER") or ""

    if not activate_broker(user_id, broker):
        return jsonify({"status": "error", "message": f"broker '{broker}' is not saved"}), 404

    creds = get_broker_creds(user_id, broker) or {}
    ok, err = _sync_active_broker_to_env(creds, broker)

    # Audit: customer-initiated broker switch is high-signal for compliance.
    try:
        from utils.audit import audit_log
        audit_log(
            actor="customer", action="broker.activate", resource=broker,
            before={"active_broker": previous_broker or None},
            after={"active_broker": broker},
            src_ip=(request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip(),
            status="ok" if ok else "failed",
            note=err or None,
        )
    except Exception:
        pass

    if not ok:
        return jsonify({"status": "error", "message": f"activated but env sync failed: {err}"}), 500

    return jsonify({"status": "success", "broker": broker})


@broker_credentials_bp.route("/credentials/request-v4-ip", methods=["POST"])
def request_v4_ip_endpoint():
    """Phase 7.6 — customer-facing button that asks hostingsol to allocate a
    Decodo v4 IP for this container. Returns the allocated IP for the
    dashboard to display + tells the customer the container will restart in
    a few seconds (so they whitelist the IP at the broker before retrying
    activation).

    Body (optional): {"broker": "arihant"} — for the Telegram alert text on
    exhaustion. Auth: customer session (must be logged in)."""
    from flask import session
    import requests as _rq

    if not session.get("user"):
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    body = request.get_json(silent=True) or {}
    broker = (body.get("broker") or "").strip().lower()

    secret = (os.getenv("PROVISIONER_SHARED_SECRET") or "").strip()
    hsol_base = (os.getenv("HOSTINGSOL_API_BASE") or "").rstrip("/")
    subdomain = (os.getenv("HSOL_SUBDOMAIN") or "").strip()
    if not secret or not hsol_base or not subdomain:
        return jsonify({
            "status": "error",
            "message": "v4 allocator not configured on this container "
                       "(PROVISIONER_SHARED_SECRET / HOSTINGSOL_API_BASE / HSOL_SUBDOMAIN)"
        }), 503

    try:
        # apply=self: WE write the returned proxy config into instance_config
        # (DB) and refresh the httpx mounts in-process — the admin service
        # must NOT rewrite our env + recreate the container (which killed
        # the customer's session mid-click). Old admin services ignore the
        # flag and fall back to the legacy env-write+recreate behaviour.
        r = _rq.post(
            f"{hsol_base}/api/clients/{subdomain}/allocate-v4",
            headers={"Authorization": f"Bearer {secret}"},
            json={"broker": broker, "apply": "self"},
            timeout=120,
        )
        # 503 with status=exhausted is a clean signal — propagate.
        if r.status_code == 503:
            return jsonify(r.json() if r.headers.get("content-type", "").startswith("application/json") else {
                "status": "exhausted",
                "message": "v4 IP pool exhausted",
            }), 503
        r.raise_for_status()
        result = r.json()

        # New-style response carries the full config — apply it ourselves:
        # DB write + httpx mount refresh, zero downtime. Absent proxy_url
        # means an old admin service handled it the legacy way (env write +
        # container recreate) — keep the old messaging for that.
        applied_live = False
        if result.get("proxy_url"):
            from database.instance_config_db import set_configs
            from utils.httpx_client import reset_httpx_client

            set_configs({
                "EGRESS_V4_PROXY_PRIMARY": result["proxy_url"],
                "EGRESS_V4_PRIMARY_IP": result.get("ip") or "",
                "EGRESS_V4_POOL_IPS": result.get("pool_ips") or "",
            })
            reset_httpx_client()
            applied_live = True

        try:
            from utils.audit import audit_log
            src = (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip()
            audit_log(
                actor="admin", action="instance.allocate_v4_ip",
                resource=result.get("ip"),
                after={
                    "ip": result.get("ip"),
                    "port": result.get("port"),
                    "broker_requested": broker,
                    "applied_live": applied_live,
                    "container_restart_triggered": not applied_live,
                },
                src_ip=src, status="ok",
                note=f"hostingsol allocate-v4 allocated {result.get('ip')} for {broker}"
                     + ("; applied live (no restart)" if applied_live else "; container restarting"),
            )
        except Exception:
            pass
        if applied_live:
            message = (
                f"Allocated IP {result.get('ip')} — active immediately, no restart "
                f"needed. Whitelist {result.get('ip')} in your {broker} developer "
                f"console, then activate the broker."
            )
        else:
            message = (
                f"Allocated IP {result.get('ip')}. Your container is restarting now "
                f"(takes ~30 seconds). After it's back, whitelist {result.get('ip')} "
                f"in your {broker} developer console, then activate the broker again."
            )
        return jsonify({"status": "success", "ip": result.get("ip"), "message": message})
    except _rq.HTTPError as e:
        return jsonify({"status": "error", "message": f"allocator http error: {e}"}), 502
    except Exception as e:
        return jsonify({"status": "error", "message": f"allocator call failed: {e}"}), 502


@broker_credentials_bp.route("/credentials/<broker>", methods=["DELETE"])
def delete_credentials_endpoint(broker: str):
    """Remove a saved broker. Does NOT clear .env if this was the active one —
    the user must activate a different broker explicitly to switch over."""
    from database.broker_creds_db import delete_broker_creds

    user_id = _current_user_id()
    if user_id is None:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    broker = (broker or "").strip().lower()
    if not delete_broker_creds(user_id, broker):
        return jsonify({"status": "error", "message": f"broker '{broker}' not saved"}), 404

    return jsonify({"status": "success", "broker": broker})


@broker_credentials_bp.route("/credentials/<broker>/instructions", methods=["GET"])
def broker_instructions_endpoint(broker: str):
    """Return rendered markdown instructions + form field metadata for `broker`."""
    from flask import session
    from blueprints.broker_metadata import get_fields, get_instructions

    if not session.get("user"):
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    from utils.decodo_proxy import broker_needs_v4
    broker = (broker or "").strip().lower()
    redirect_url = _build_redirect_url(broker)
    return jsonify({
        "status": "success",
        "broker": broker,
        "fields": get_fields(broker),
        "instructions_md": get_instructions(broker, redirect_url),
        "redirect_url": redirect_url,
        # True when this broker's API endpoint is IPv4-only and the customer
        # must whitelist their dedicated IPv4 (not IPv6). Drives which IP the
        # frontend renders as the focused "Whitelist this IP" callout.
        "v4_required": broker_needs_v4(broker),
        # The /128 IPv6 the customer must whitelist in their broker's
        # developer console. Pulled from os.environ so it stays in sync
        # with what the source-bind patch will actually use at runtime.
        "client_ipv6": os.getenv("CLIENT_IPV6", ""),
        # Phase 7 — per-customer Decodo IPv4 for IPv4-only broker hosts.
        # Primary is assigned at provision time. Secondary is set by
        # super-admin for failover and is optional. Both must be
        # whitelisted at IPv4-only brokers (Arihant etc.) to enable
        # transparent failover when the primary is unreachable.
        "client_ipv4_primary": get_config("EGRESS_V4_PRIMARY_IP"),
        "client_ipv4_secondary": get_config("EGRESS_V4_SECONDARY_IP"),
        # V1 ship 2026-06-05: shared-pool routing — every customer's
        # traffic load-balances across all IPs in EGRESS_V4_POOL_IPS. The
        # primary is the customer's "preferred" IP but ANY pool IP can
        # show up at the broker. Customer must whitelist ALL of these.
        "client_ipv4_pool": [
            ip.strip() for ip in get_config("EGRESS_V4_POOL_IPS").split(",") if ip.strip()
        ],
        # Legacy shared host v4 — kept for back-compat; dashboard shows
        # it only as a fallback when no per-customer Decodo IP is set.
        "shared_host_ipv4": os.getenv("SHARED_HOST_IPV4", ""),
    })


@broker_credentials_bp.route("/credentials/host-info", methods=["GET"])
def host_info_endpoint():
    """Return the per-instance metadata the frontend needs to show the
    customer (their assigned IPv6, host server URL, default redirect-URL
    pattern). Used by /manage-brokers to render a top-level "what to
    whitelist" banner that's the same for every broker."""
    from flask import session

    if not session.get("user"):
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    host = (os.getenv("HOST_SERVER") or "").rstrip("/")
    return jsonify({
        "status": "success",
        "data": {
            "client_ipv6": os.getenv("CLIENT_IPV6", ""),
        # Phase 7 — per-customer Decodo IPv4 for IPv4-only broker hosts.
        # Primary is assigned at provision time. Secondary is set by
        # super-admin for failover and is optional. Both must be
        # whitelisted at IPv4-only brokers (Arihant etc.) to enable
        # transparent failover when the primary is unreachable.
        "client_ipv4_primary": get_config("EGRESS_V4_PRIMARY_IP"),
        "client_ipv4_secondary": get_config("EGRESS_V4_SECONDARY_IP"),
        # V1 ship 2026-06-05: shared-pool routing — see /credentials/<broker>.
        "client_ipv4_pool": [
            ip.strip() for ip in get_config("EGRESS_V4_POOL_IPS").split(",") if ip.strip()
        ],
        # Legacy shared host v4 — kept for back-compat; dashboard shows
        # it only as a fallback when no per-customer Decodo IP is set.
        "shared_host_ipv4": os.getenv("SHARED_HOST_IPV4", ""),
            "host_server": host,
            "redirect_url_pattern": f"{host}/<broker>/callback" if host else "",
        },
    })


@broker_credentials_bp.route("/supported", methods=["GET"])
def supported_brokers_endpoint():
    """List all brokers OpenAlgo supports + which ones have detailed instructions."""
    from flask import session
    from blueprints.broker_metadata import BROKER_FIELDS, BROKER_INSTRUCTIONS

    if not session.get("user"):
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    valid = [b.strip().lower() for b in (get_env_value("VALID_BROKERS") or "").split(",") if b.strip()]
    out = []
    for b in valid:
        out.append({
            "broker": b,
            "has_fields_meta": b in BROKER_FIELDS,
            "has_instructions": b in BROKER_INSTRUCTIONS,
        })
    return jsonify({"status": "success", "data": out})


@broker_credentials_bp.route("/credentials/auto-login-status", methods=["GET"])
def auto_login_status_endpoint():
    """Surface the daily auto-login scheduler's state (enabled + next-run)
    so the React UI can show "next run at 08:00 IST tomorrow" alongside
    the per-broker last_auth_at fields the list endpoint already returns."""
    from flask import session

    if not session.get("user"):
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    try:
        from services.auto_login_scheduler_service import get_scheduler_status
        sched = get_scheduler_status()
    except Exception as e:
        logger.exception("auto-login-status read failed")
        sched = {"enabled": None, "running": False, "next_run": None, "error": str(e)}

    return jsonify({"status": "success", "data": sched})


# Headless brokers whose auto-login mints a session from stored keys alone,
# with NO TOTP seed. MUST stay in sync with the scheduler's _NO_TOTP_AUTO_LOGIN
# (services/auto_login_scheduler_service.py):
#   • indmoney: long-lived access token pasted by the customer.
#   • iiflxts:  XTS Interactive appKey/secretKey -> daily session token, fully
#     headless (no OTP/browser — see broker_login_adapters/iiflxts.py).
_HEADLESS_NO_TOTP_BROKERS = {"indmoney", "iiflxts"}


def run_auto_login_for_broker(user_id: int, username: str, broker: str) -> dict:
    """Core auto-login logic, shared between the HTTP route and the daily
    scheduler. Decrypts creds → runs adapter → persists access_token →
    marks success/error in broker_creds_db. Does NOT touch flask.session.

    Returns:
        {"ok": True,  "access_token": "...", "broker": ..., "user_id": ...,
         "expires_at": ..., "feed_token": ...}
        {"ok": False, "error": "...", "error_kind": "no_adapter|no_creds|no_totp|adapter_fail|persist_fail"}
    """
    from database.broker_creds_db import (
        get_broker_creds,
        get_active_broker,
        mark_auth_error,
        mark_auth_success,
    )
    from broker_login_adapters import adapter_for

    broker = (broker or "").strip().lower()
    adapter = adapter_for(broker)
    if adapter is None:
        return {
            "ok": False,
            "error_kind": "no_adapter",
            "error": f"Auto-login adapter for '{broker}' is not implemented yet. "
                     f"Click 'Connect' in the dashboard to do the broker login manually.",
        }

    db_creds = get_broker_creds(user_id, broker)
    if db_creds is None:
        return {
            "ok": False,
            "error_kind": "no_creds",
            "error": f"No saved credentials for '{broker}'",
        }

    # Most brokers' auto-login needs a TOTP seed. Headless brokers are exempt
    # (see _HEADLESS_NO_TOTP_BROKERS above). Bug fix 2026-07-01: iiflxts was NOT
    # exempt here (only indmoney was), so this returned no_totp for IIFL XTS —
    # breaking BOTH the daily 08:00 scheduler AND on-startup auto-login, which
    # left XTS customers (e.g. rohit) with no broker session after any restart
    # and every order rejected as "Failed to place order".
    if broker not in _HEADLESS_NO_TOTP_BROKERS and not db_creds.get("totp_seed"):
        return {
            "ok": False,
            "error_kind": "no_totp",
            "error": "Auto-login needs a saved TOTP seed. Edit the broker in Manage Brokers "
                     "and add your TOTP seed first.",
        }

    # Adapter contract: pass the SHAPE each broker's login() expects.
    # Different brokers store the same kind of thing under different
    # field names (Kotak MPIN, Upstox password, Zerodha kite password).
    # We unify into a stable dict the adapter can self-select from.
    extra = db_creds.get("extra") or {}
    client_code = db_creds.get("client_code") or ""
    password_field = extra.get("password") or ""
    pin_field = extra.get("mpin") or extra.get("pin") or password_field
    adapter_creds = {
        "api_key": db_creds["api_key"],
        "api_secret": db_creds["api_secret"],
        "redirect_uri": _build_redirect_url(broker),
        # client_code holds the broker-specific user identifier:
        #   Zerodha → kite user_id (e.g. "ABC123")
        #   Upstox/Kotak → mobile number (with country code)
        #   Fyers → Fyers client_id
        # Each adapter picks the alias it needs.
        "mobile_number": client_code,
        "user_id": client_code,
        # `pin` / `password` resolve to whichever broker-specific secret
        # is saved under extra.* — adapters use the name that fits.
        "pin": pin_field,
        "password": password_field,
        "totp_secret": db_creds["totp_seed"],
        # Market-data (feed) app creds — REQUIRED for the IIFL XTS adapter to
        # mint the feed token. Without these the adapter silently skips the feed
        # login, so quotes/market-data (and the orderbook/positions UI that
        # enriches with LTP) break with "Invalid Token" even though trading
        # works (2026-07-10 incident: rohit's UI showed empty order/trade/
        # position pages). broker_credentials.py stores them; they just weren't
        # being forwarded to the adapter here.
        "api_key_market": db_creds.get("api_key_market") or "",
        "api_secret_market": db_creds.get("api_secret_market") or "",
    }

    result = adapter(adapter_creds)
    if not result.get("ok"):
        mark_auth_error(user_id, broker, result.get("error") or "unknown")
        return {
            "ok": False,
            "error_kind": "adapter_fail",
            "error": result.get("error") or "Auto-login failed",
        }

    # Persist the access_token via OpenAlgo's normal auth path so subsequent
    # broker API calls (orders, holdings, etc.) find it.
    #
    # 🔴 2026-07-21 BUG FIX. Kite Connect requires the stored auth token to be
    # `api_key:access_token` (the Authorization header is `token
    # api_key:access_token`). The MANUAL browser login assembles that prefix
    # (brlogin.py:1200), but THIS daemon/scheduler path stored the RAW
    # access_token — so every day the 08:00 auto-login re-minted, it wrote a
    # MALFORMED token and every Zerodha call (funds/positions/orders) failed
    # with "authorization value should atleast be `api_key`:`access_token`"
    # → the customer saw all values as NaN and orders ENTRY-failed, until a
    # manual browser re-login overwrote it with the correct format (anantswain,
    # 2026-07-21). Mirror the browser path here. Use db_creds['api_key'] (the
    # stored key, in scope from line ~970) — NOT os.getenv('BROKER_API_KEY'),
    # which is EMPTY in the scheduler process (that env is only populated on an
    # interactive login/bootstrap, which is also why the manual path happened
    # to work while this one did not).
    persist_token = result["access_token"]
    if broker == "zerodha" and persist_token and ":" not in persist_token:
        _ak = (db_creds.get("api_key") or "").strip()
        if _ak:
            persist_token = f"{_ak}:{persist_token}"
        else:
            logger.error("zerodha auto-login: api_key missing from creds — stored "
                         "token will be malformed (orders will fail). Check broker_creds.")
    try:
        from database.auth_db import upsert_auth
        upsert_auth(
            name=username,
            auth_token=persist_token,
            broker=broker,
            feed_token=result.get("feed_token"),
            user_id=result.get("user_id"),
            revoke=False,
        )
    except Exception as e:
        logger.exception("Auto-login succeeded but auth_db persist failed")
        return {
            "ok": False,
            "error_kind": "persist_fail",
            "error": f"Auto-login succeeded but session persist failed: {e}",
        }

    # If this broker is the user's active one, refresh os.environ so the
    # rest of OpenAlgo (auth.py, brlogin.py, the broker plugin's order
    # placement path) reads the just-minted credentials without a restart.
    try:
        if get_active_broker(user_id) == broker:
            _sync_active_broker_to_env(db_creds, broker)
    except Exception:
        logger.exception("os.environ sync skipped (non-fatal)")

    # Ensure the user has an OpenAlgo API key. Webhook-only customers (who
    # trade via the publisher distribution inbox and never open the "API Key"
    # page) otherwise have NO key — which silently blanks the orderbook /
    # positions / tradebook SPA pages even though trading works fine
    # (2026-07-10: rohit held a 111-share position his UI couldn't show).
    # Auto-provision one on successful login so the customer UI always works.
    try:
        from database.auth_db import get_api_key_for_tradingview, upsert_api_key
        if not get_api_key_for_tradingview(username):
            import secrets as _secrets
            upsert_api_key(username, _secrets.token_hex(32))
            logger.info(f"auto-provisioned OpenAlgo API key for {username} (had none)")
    except Exception:
        logger.exception("API key auto-provision skipped (non-fatal)")

    mark_auth_success(user_id, broker)
    return {
        "ok": True,
        "access_token": result["access_token"],
        "broker": broker,
        "user_id": result.get("user_id"),
        "expires_at": result.get("expires_at"),
        "feed_token": result.get("feed_token"),
    }


@broker_credentials_bp.route("/credentials/<broker>/auto-login", methods=["POST"])
def auto_login_endpoint(broker: str):
    """HTTP wrapper around run_auto_login_for_broker — adds session auth +
    flask-session bookkeeping. Returns masked metadata only (never the
    raw token). The daily scheduler calls the same helper without going
    through this endpoint."""
    from flask import session
    from datetime import datetime, timezone

    if not session.get("user"):
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    user_id = _current_user_id()
    if user_id is None:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    username = session["user"]
    result = run_auto_login_for_broker(user_id, username, (broker or "").strip().lower())

    if not result.get("ok"):
        # Map error_kind to a sensible HTTP code so the React UI's branching
        # on .response.status stays meaningful.
        code = {
            "no_adapter":   501,
            "no_creds":     404,
            "no_totp":      400,
            "persist_fail": 500,
        }.get(result.get("error_kind"), 502)
        return jsonify({"status": "error", "message": result.get("error") or "Auto-login failed"}), code

    # Update the in-Flask session too so the user lands on the dashboard
    # in a fully-authed state if they're driving this from the UI.
    session["broker"] = result["broker"]
    session["logged_in"] = True
    session["login_time"] = datetime.now(timezone.utc).isoformat()

    # Don't leak the token to the frontend — surface metadata only.
    token = result["access_token"]
    masked = f"{token[:6]}...{token[-6:]}" if len(token) > 16 else "***"
    return jsonify({
        "status": "success",
        "broker": result["broker"],
        "access_token_masked": masked,
        "user_id": result.get("user_id"),
        "expires_at": result.get("expires_at"),
    })
