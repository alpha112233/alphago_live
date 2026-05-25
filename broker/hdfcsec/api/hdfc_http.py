"""Shared HTTP client for the HDFC InvestRight API.

HDFC's auth shape is unusual: the access_token goes into the
``Authorization`` header **without** a ``Bearer `` prefix, and every
request — even authenticated ones — must additionally pass ``api_key``
as a URL query parameter.

The auth-string returned by :func:`broker.hdfcsec.api.auth_api.authenticate_broker`
is a triple-colon packed ``access_token:::api_key:::api_secret``. We
unpack it here once per request.

Reference: ccxt-india/brokers/hdfc/hdfcsec.py — the canonical Python SDK.
"""
from __future__ import annotations

import json as _json
import time
from typing import Any, Dict, Optional, Tuple

import httpx

from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)


def parse_auth(auth_string: str) -> Tuple[str, str, str]:
    """Unpack ``access_token:::api_key:::api_secret``."""
    if not auth_string or auth_string.count(":::") < 2:
        raise ValueError(
            "HDFC auth_string malformed; expected "
            "'access_token:::api_key:::api_secret'."
        )
    access_token, api_key, api_secret = auth_string.split(":::", 2)
    return access_token, api_key, api_secret


def _build_headers(access_token: str) -> Dict[str, str]:
    return {
        "Authorization": access_token,           # NOT "Bearer <token>"
        "Content-Type": "application/json",
        # HDFC's developer portal rejects empty UA on some endpoints.
        "User-Agent": "alphago-live/1.0",
    }


def request(
    method: str,
    url_template: str,
    auth_string: str,
    payload: Optional[Dict[str, Any]] = None,
    url_args: Optional[Dict[str, str]] = None,
    extra_query: Optional[Dict[str, str]] = None,
    timeout: float = 15.0,
    max_retries: int = 3,
) -> Dict[str, Any]:
    """Make a signed HDFC call.

    ``url_template`` may contain ``{order_id}``-style placeholders that
    ``url_args`` substitutes.

    Returns parsed JSON. On HTTP / parse / retry-exhaust errors, returns
    an envelope shaped ``{"status": "error", "message": "..."}``.
    """
    try:
        access_token, api_key, _ = parse_auth(auth_string)
    except ValueError as e:
        return {"status": "error", "message": str(e)}

    url = url_template.format(**(url_args or {}))
    headers = _build_headers(access_token)
    method = method.upper()
    client = get_httpx_client()

    # Every authenticated call needs api_key in the query string.
    params = {"api_key": api_key, **(extra_query or {})}

    delays = [0.75, 1.5, 3.0]
    last_err: Optional[Exception] = None

    for attempt in range(max_retries):
        try:
            logger.debug(f"HDFC {method} {url} attempt={attempt + 1}")
            if method == "GET":
                resp = client.get(url, headers=headers, params=params, timeout=timeout)
            elif method == "POST":
                resp = client.post(
                    url, headers=headers, params=params,
                    content=_json.dumps(payload or {}).encode("utf-8"),
                    timeout=timeout,
                )
            elif method == "PUT":
                resp = client.put(
                    url, headers=headers, params=params,
                    content=_json.dumps(payload or {}).encode("utf-8"),
                    timeout=timeout,
                )
            elif method == "DELETE":
                resp = client.delete(url, headers=headers, params=params, timeout=timeout)
            else:
                return {"status": "error", "message": f"Unsupported method: {method}"}

            if resp.status_code == 429 and attempt < max_retries - 1:
                logger.warning(f"HDFC 429 throttle, sleeping {delays[attempt]}s")
                time.sleep(delays[attempt])
                continue

            try:
                return resp.json()
            except Exception:
                logger.error(
                    f"HDFC {method} {url} non-JSON response "
                    f"(status={resp.status_code}): {resp.text[:200]}"
                )
                return {
                    "status": "error",
                    "message": (resp.text or "").strip()[:500] or "Empty HDFC response",
                    "http_status": resp.status_code,
                }

        except httpx.TimeoutException as e:
            last_err = e
            if attempt < max_retries - 1:
                time.sleep(delays[attempt])
                continue
        except Exception as e:  # pragma: no cover — defensive
            logger.exception(f"HDFC {method} {url} unexpected exception")
            return {"status": "error", "message": f"Exception: {e}"}

    return {
        "status": "error",
        "message": f"HDFC {method} {url} failed after {max_retries} attempts: {last_err}",
    }
