import hashlib
import os

import httpx
import requests

from broker.iiflxts.baseurl import INTERACTIVE_URL, MARKET_DATA_URL
from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)


def _xts_error(detail, status=None):
    """Pull the human-readable error out of an XTS / Symphony error body.
    IIFL XTS returns errors as {"type":"error","code":"e-...","description":"..."}
    — the real reason is in `description` (+ `code`), NOT `message` (which the
    5paisa template read, so IIFL errors showed as a generic fallback)."""
    if not isinstance(detail, dict):
        text = str(detail)[:200]
        return f"HTTP {status}: {text}" if status else text
    desc = detail.get("description") or detail.get("message") or detail.get("result")
    code = detail.get("code")
    parts = [str(p) for p in (code, desc) if p]
    msg = " — ".join(parts) if parts else "request rejected"
    return f"{msg}" + (f" (HTTP {status})" if status else "")


def authenticate_broker(request_token):
    try:
        # Get the shared httpx client
        client = get_httpx_client()
        # Fetching the necessary credentials from environment variables
        BROKER_API_KEY = os.getenv("BROKER_API_KEY")
        BROKER_API_SECRET = os.getenv("BROKER_API_SECRET")

        # Make POST request to get the final token
        payload = {"appKey": BROKER_API_KEY, "secretKey": BROKER_API_SECRET, "source": "WebAPI"}

        headers = {"Content-Type": "application/json"}

        session_url = f"{INTERACTIVE_URL}/user/session"
        response = client.post(session_url, json=payload, headers=headers)

        if response.status_code == 200:
            result = response.json()
            if result.get("type") == "success":
                token = result["result"]["token"]
                logger.info(f"Auth Token: {token}")

                # Call get_feed_token() after successful authentication
                feed_token, user_id, feed_error = get_feed_token()
                if feed_error:
                    return token, None, None, f"Feed token error: {feed_error}"

                return token, feed_token, user_id, None

            else:
                # 200 but type != success — surface the body so the real
                # reason (e.g. "Data Not found") is visible, not a generic msg.
                return None, None, None, f"IIFL XTS login failed: {_xts_error(result, 200)}"
        else:
            # Non-200 — IIFL puts the reason in `description` (+ code).
            try:
                error_detail = response.json()
            except Exception:
                error_detail = response.text[:300]
            return None, None, None, (
                f"IIFL XTS login failed: {_xts_error(error_detail, response.status_code)}. "
                "If this is 'Data Not found' / an auth/IP error, register the dedicated "
                "IPv4 shown on this broker's panel with IIFL and confirm these are XTS "
                "Interactive keys."
            )

    except Exception as e:
        return None, None, None, f"Error during authentication: {str(e)}"


def get_feed_token():
    try:
        # Fetch credentials for feed token
        BROKER_API_KEY_MARKET = os.getenv("BROKER_API_KEY_MARKET")
        BROKER_API_SECRET_MARKET = os.getenv("BROKER_API_SECRET_MARKET")

        # Construct payload for feed token request
        feed_payload = {
            "secretKey": BROKER_API_SECRET_MARKET,
            "appKey": BROKER_API_KEY_MARKET,
            "source": "WebAPI",
        }

        feed_headers = {"Content-Type": "application/json"}

        # Get feed token
        feed_url = f"{MARKET_DATA_URL}/auth/login"
        client = get_httpx_client()
        feed_response = client.post(feed_url, json=feed_payload, headers=feed_headers)

        feed_token = None
        user_id = None
        if feed_response.status_code == 200:
            feed_result = feed_response.json()
            if feed_result.get("type") == "success":
                feed_token = feed_result["result"].get("token")
                user_id = feed_result["result"].get("userID")
                logger.info(f"Feed Token: {feed_token}")
            else:
                return None, None, f"Market-data login failed: {_xts_error(feed_result, 200)}"
        else:
            try:
                feed_error_detail = feed_response.json()
            except Exception:
                feed_error_detail = feed_response.text[:300]
            return None, None, f"Market-data login failed: {_xts_error(feed_error_detail, feed_response.status_code)}"

        return feed_token, user_id, None
    except Exception as e:
        return None, None, f"An exception occurred: {str(e)}"
