"""Shared HTTP signing layer for the Breeze API.

Every Breeze call carries four headers:
    X-Checksum    "token " + SHA-256(timestamp + payload + secret_key)
    X-Timestamp   ISO-8601 UTC with milliseconds (e.g. 2026-05-25T14:32:45.123Z)
    X-AppKey      Breeze app_key
    X-SessionToken Daily session token from OAuth callback

The auth_string returned by `authenticate_broker` is a triple-colon
packed `session_token:::app_key:::secret_key`. This helper parses it and
returns ready-to-use headers + the JSON-serialised body.

Refer to ccxt-india/brokers/icici/icici.py:62-83 — the canonical SDK
implementation. The signature here is byte-for-byte equivalent.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json as _json
from typing import Any, Dict, Optional, Tuple

import httpx

from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)


def parse_auth(auth_string: str) -> Tuple[str, str, str]:
    """Unpack `session_token:::app_key:::secret_key`."""
    if not auth_string or auth_string.count(":::") < 2:
        raise ValueError(
            "ICICI auth_string malformed; expected "
            "'session_token:::app_key:::secret_key'."
        )
    session_token, app_key, secret_key = auth_string.split(":::", 2)
    return session_token, app_key, secret_key


def _utc_iso() -> str:
    """ISO-8601 UTC with milliseconds + trailing 'Z'."""
    now = _dt.datetime.now(_dt.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _checksum(timestamp: str, body_json: str, secret_key: str) -> str:
    return hashlib.sha256((timestamp + body_json + secret_key).encode("utf-8")).hexdigest()


def build_headers(
    auth_string: str,
    body: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, str], str]:
    """Return (headers, body_json) ready for httpx.

    Body is JSON-serialised with no whitespace (matches Breeze's signature
    expectations — any extra space breaks the checksum).
    """
    session_token, app_key, secret_key = parse_auth(auth_string)
    body_json = _json.dumps(body or {}, separators=(",", ":"))
    timestamp = _utc_iso()
    headers = {
        "Content-Type": "application/json",
        "X-Checksum": "token " + _checksum(timestamp, body_json, secret_key),
        "X-Timestamp": timestamp,
        "X-AppKey": app_key,
        "X-SessionToken": session_token,
    }
    return headers, body_json


def request(
    method: str,
    url: str,
    auth_string: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: float = 15.0,
    max_retries: int = 3,
) -> Dict[str, Any]:
    """Make a signed Breeze call. Returns parsed JSON (or an error envelope).

    Retries idempotent GETs and 429s with exponential backoff
    (0.75s, 1.5s, 3.0s) per ccxt-india/brokers/icici/icici.py:95-157.
    """
    method = method.upper()
    client = get_httpx_client()
    delays = [0.75, 1.5, 3.0]

    last_err: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            headers, body_json = build_headers(auth_string, payload)
            content = body_json.encode("utf-8") if payload is not None else None
            logger.debug(f"Breeze {method} {url} attempt={attempt + 1}")
            resp = client.request(method, url, headers=headers, content=content, timeout=timeout)

            if resp.status_code == 429 and attempt < max_retries - 1:
                logger.warning(f"Breeze 429 throttle, sleeping {delays[attempt]}s")
                import time
                time.sleep(delays[attempt])
                continue

            try:
                data = resp.json()
            except Exception:
                logger.error(
                    f"Breeze {method} {url} returned non-JSON "
                    f"(status={resp.status_code}): {resp.text[:200]}"
                )
                return {
                    "Status": resp.status_code,
                    "Error": (resp.text or "").strip()[:500] or "Empty Breeze response",
                }
            return data

        except httpx.TimeoutException as e:
            last_err = e
            if attempt < max_retries - 1:
                import time
                time.sleep(delays[attempt])
                continue
        except Exception as e:  # pragma: no cover — defensive
            logger.exception(f"Breeze {method} {url} unexpected exception")
            return {"Status": 500, "Error": f"Exception: {e}"}

    return {"Status": 500, "Error": f"Breeze {method} {url} failed after {max_retries} attempts: {last_err}"}
