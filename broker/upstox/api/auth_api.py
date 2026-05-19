import json
import os

import httpx

from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)


def _mask(s):
    """Mask a credential for log: keep first 4 + last 4 chars."""
    if not s:
        return "(empty)"
    if len(s) <= 10:
        return f"len={len(s)} (too short to safely mask)"
    return f"len={len(s)} {s[:4]}...{s[-4:]}"


def authenticate_broker(code):
    try:
        BROKER_API_KEY = os.getenv("BROKER_API_KEY")
        BROKER_API_SECRET = os.getenv("BROKER_API_SECRET")
        REDIRECT_URL = os.getenv("REDIRECT_URL")

        # Surface what auth_api sees at runtime — diagnoses "silent OAuth
        # exchange failure" cases where env vars get out of sync with the
        # active broker (broker_creds_db says Upstox, os.environ still has
        # Dhan's API_KEY) or where the customer's Upstox developer app's
        # Redirect URI doesn't EXACTLY match what alphago_live computes.
        logger.info(
            "Upstox token exchange starting | "
            f"code={_mask(code)} | "
            f"BROKER_API_KEY={_mask(BROKER_API_KEY)} | "
            f"BROKER_API_SECRET={_mask(BROKER_API_SECRET)} | "
            f"REDIRECT_URL={REDIRECT_URL!r}"
        )

        if not all([BROKER_API_KEY, BROKER_API_SECRET, REDIRECT_URL]):
            logger.error(
                "Broker API key, secret, or redirect URL is not set in environment variables. "
                f"key_set={bool(BROKER_API_KEY)} secret_set={bool(BROKER_API_SECRET)} "
                f"redirect_set={bool(REDIRECT_URL)}"
            )
            return None, "Configuration error: Missing API credentials."

        url = "https://api.upstox.com/v2/login/authorization/token"
        data = {
            "code": code,
            "client_id": BROKER_API_KEY,
            "client_secret": BROKER_API_SECRET,
            "redirect_uri": REDIRECT_URL,
            "grant_type": "authorization_code",
        }

        client = get_httpx_client()
        response = client.post(url, data=data)

        # Log status + response shape at INFO so a successful exchange leaves
        # a trail (the previous code only logged at DEBUG on success).
        try:
            body_keys = list(response.json().keys()) if response.content else []
        except Exception:
            body_keys = ["(non-json)"]
        logger.info(
            f"Upstox token exchange response: HTTP {response.status_code} | "
            f"body_keys={body_keys} | body_len={len(response.content)}"
        )

        if response.status_code == 200:
            response_data = response.json()
            access_token = response_data.get("access_token")
            if access_token:
                logger.info(
                    f"Upstox token exchange OK: access_token={_mask(access_token)} "
                    f"expires_in={response_data.get('expires_in', '(missing)')} "
                    f"user_id={response_data.get('user_id', '(missing)')}"
                )
                return access_token, None
            else:
                error_msg = "Authentication succeeded but no access token was returned."
                logger.error(f"{error_msg} Response: {response_data}")
                return None, error_msg
        else:
            error_msg = "Upstox API authentication failed."
            try:
                error_detail = response.json()
                errors = error_detail.get("errors", [])
                detailed_message = "; ".join(
                    [err.get("message", "Unknown error") for err in errors]
                )
                error_msg = f"Upstox API Error: {detailed_message}"
                logger.error(
                    f"{error_msg} | Status: {response.status_code}, Response: {response.text}"
                )
            except json.JSONDecodeError:
                logger.error(
                    f"{error_msg} | Status: {response.status_code}, Response: {response.text}"
                )
            return None, error_msg

    except httpx.RequestError as e:
        logger.exception("An HTTP request error occurred during Upstox authentication.")
        return None, f"An HTTP request error occurred: {e}"

    except Exception:
        logger.exception("An unexpected error occurred during Upstox authentication.")
        return None, "An unexpected error occurred during authentication."
