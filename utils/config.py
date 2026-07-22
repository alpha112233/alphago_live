# utils/config.py

import os

from dotenv import load_dotenv

# Load environment variables from .env file with override=True to ensure values are updated
load_dotenv(override=True)


def get_broker_api_key() -> str | None:
    """
    Retrieve the configured broker API key.

    Returns:
        str | None: The broker API key from environment variables, or None if not set.
    """
    return os.getenv("BROKER_API_KEY")


def get_broker_api_secret() -> str | None:
    """
    Retrieve the configured broker API secret.

    Returns:
        str | None: The broker API secret from environment variables, or None if not set.
    """
    return os.getenv("BROKER_API_SECRET")


def get_login_rate_limit_min() -> str:
    """
    Retrieve the rate limit for logins per minute.

    Returns:
        str: The rate limit string (e.g., '5 per minute').
    """
    return os.getenv("LOGIN_RATE_LIMIT_MIN", "5 per minute")


def get_login_rate_limit_hour() -> str:
    """
    Retrieve the rate limit for logins per hour.

    Returns:
        str: The rate limit string (e.g., '25 per hour').
    """
    return os.getenv("LOGIN_RATE_LIMIT_HOUR", "25 per hour")


def get_host_server() -> str:
    """
    Retrieve the host server URL.

    Returns:
        str: The host server URL string.
    """
    return os.getenv("HOST_SERVER", "http://127.0.0.1:5000")


# alphago_live fork additions — product branding overrides
def get_brand_name() -> str:
    """Product name shown in UI. Defaults to 'Alpha Live Trading'."""
    return os.getenv("BRAND_NAME", "Alpha Live Trading")


def get_brand_tagline() -> str:
    """Tagline shown under the brand name. Defaults to 'Your own trading platform'."""
    return os.getenv("BRAND_TAGLINE", "A terminal for serious traders")


# alphago_live fork additions — hosting-agreement consent gate (Phase 3b).
# Injected at provision time by hostingsol; unset => gate disabled (fail-open).
def get_consent_status_url() -> str:
    """hostingsol endpoint the container calls to check its OWN signed status."""
    return os.getenv("AQ_CONSENT_STATUS_URL", "").strip()


def get_consent_status_token() -> str:
    """Read-only, subdomain-scoped bearer for the consent status check."""
    return os.getenv("AQ_CONSENT_STATUS_TOKEN", "").strip()
