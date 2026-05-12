# blueprints/broker_credentials.py
"""
Broker credentials management API.
Handles reading and updating broker credentials in the .env file.
"""

import os
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

    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return jsonify({"status": "error", "message": "'api_key' is required"}), 400

    try:
        add_or_update_broker_creds(
            user_id=user_id,
            broker=broker,
            api_key=api_key,
            api_secret=(data.get("api_secret") or "").strip() or None,
            api_key_market=(data.get("api_key_market") or "").strip() or None,
            api_secret_market=(data.get("api_secret_market") or "").strip() or None,
            client_code=(data.get("client_code") or "").strip() or None,
            totp_seed=(data.get("totp_seed") or "").strip() or None,
            extra=data.get("extra") if isinstance(data.get("extra"), dict) else None,
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
    """Make `broker` the active broker for this user. Syncs creds to .env."""
    from database.broker_creds_db import activate_broker, get_broker_creds

    user_id = _current_user_id()
    if user_id is None:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    broker = (broker or "").strip().lower()
    if not activate_broker(user_id, broker):
        return jsonify({"status": "error", "message": f"broker '{broker}' is not saved"}), 404

    creds = get_broker_creds(user_id, broker) or {}
    ok, err = _sync_active_broker_to_env(creds, broker)
    if not ok:
        return jsonify({"status": "error", "message": f"activated but env sync failed: {err}"}), 500

    return jsonify({"status": "success", "broker": broker})


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

    broker = (broker or "").strip().lower()
    redirect_url = _build_redirect_url(broker)
    return jsonify({
        "status": "success",
        "broker": broker,
        "fields": get_fields(broker),
        "instructions_md": get_instructions(broker, redirect_url),
        "redirect_url": redirect_url,
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


@broker_credentials_bp.route("/credentials/<broker>/auto-login", methods=["POST"])
def auto_login_endpoint(broker: str):
    """Run the broker-specific daemon auto-login flow using the user's saved
    TOTP seed. On success the broker session is established as if the user
    had completed the manual OAuth/2FA flow.

    Per-broker behaviour (only `upstox` is implemented in this first cut):
      - Decrypts broker_creds_db row for current user + broker
      - Runs broker_login_adapters.<broker>.login(creds)
      - On success, persists access_token via OpenAlgo's normal auth flow
        (database.auth_db.upsert_auth) so subsequent broker API calls work
      - Returns {ok, access_token (masked), error, ...}

    Auth: requires the session 'user' to be set (same level as the other
    multi-broker endpoints — usable in the password-authenticated state
    before any broker session exists).
    """
    from flask import session
    from datetime import datetime, timezone
    from database.broker_creds_db import (
        get_broker_creds,
        mark_auth_error,
        mark_auth_success,
    )
    from broker_login_adapters import adapter_for

    if not session.get("user"):
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    user_id = _current_user_id()
    if user_id is None:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    broker = (broker or "").strip().lower()
    adapter = adapter_for(broker)
    if adapter is None:
        return jsonify({
            "status": "error",
            "message": f"Auto-login adapter for '{broker}' is not implemented yet. "
                       f"Click 'Connect' in the dashboard to do the broker login manually.",
        }), 501

    db_creds = get_broker_creds(user_id, broker)
    if db_creds is None:
        return jsonify({"status": "error", "message": f"No saved credentials for '{broker}'"}), 404

    if not db_creds.get("totp_seed"):
        return jsonify({
            "status": "error",
            "message": "Auto-login needs a saved TOTP seed. Edit the broker in Manage Brokers "
                       "and add your TOTP seed first.",
        }), 400

    # Adapter contract: pass the SHAPE upstream alpha_live expects.
    extra = db_creds.get("extra") or {}
    adapter_creds = {
        "api_key": db_creds["api_key"],
        "api_secret": db_creds["api_secret"],
        "redirect_uri": _build_redirect_url(broker),
        # client_code stores mobile/userid for these brokers — see broker_metadata.py
        "mobile_number": db_creds.get("client_code") or "",
        "pin": extra.get("password") or extra.get("pin") or "",
        "totp_secret": db_creds["totp_seed"],
    }

    result = adapter(adapter_creds)

    if not result.get("ok"):
        mark_auth_error(user_id, broker, result.get("error") or "unknown")
        return jsonify({
            "status": "error",
            "message": result.get("error") or "Auto-login failed",
        }), 502

    # Persist the access_token via OpenAlgo's normal auth path so subsequent
    # broker API calls (orders, holdings, etc.) find it.
    try:
        from database.auth_db import upsert_auth
        username = session.get("user")
        upsert_auth(
            name=username,
            auth_token=result["access_token"],
            broker=broker,
            feed_token=result.get("feed_token"),
            user_id=result.get("user_id"),
            revoke=False,
        )
    except Exception as e:
        logger.exception("Auto-login succeeded but auth_db persist failed")
        return jsonify({
            "status": "error",
            "message": f"Auto-login succeeded but session persist failed: {e}",
        }), 500

    # Update the in-Flask session too so the user lands on the dashboard
    # in a fully-authed state if they're driving this from the UI.
    session["broker"] = broker
    session["logged_in"] = True
    session["login_time"] = datetime.now(timezone.utc).isoformat()

    mark_auth_success(user_id, broker)

    # Don't leak the token to the frontend — surface metadata only.
    token = result["access_token"]
    masked = f"{token[:6]}...{token[-6:]}" if len(token) > 16 else "***"
    return jsonify({
        "status": "success",
        "broker": broker,
        "access_token_masked": masked,
        "user_id": result.get("user_id"),
        "expires_at": result.get("expires_at"),
    })
