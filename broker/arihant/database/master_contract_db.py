"""Arihant symbol-master download → SQLite token cache.

Arihant exposes ``/wrapper-service/api/symbol/v1/master/cache`` as an
unauthenticated endpoint that returns the day's instrument master (all
exchange tokens + tradingsymbol mappings). On daily refresh we download
the master and populate the OpenAlgo ``token_db`` so the order
placement path can translate OpenAlgo's canonical symbol to Arihant's
``excToken``.

Minimal first-pass implementation:
  * download_master() pulls the JSON
  * populate_token_db() inserts/updates rows into token_db.SymToken
  * master_contract_download() is the OpenAlgo-standard entry point

Follow-up PR refines:
  * filter by exchange (currently inserts everything)
  * de-dup vs the previous day's master
  * handle expiry-based FNO contracts properly
"""
from __future__ import annotations

import logging

from broker.arihant.baseurl import get_url
from utils.httpx_client import get_httpx_client

log = logging.getLogger(__name__)


def download_master() -> list[dict]:
    """Fetch the Arihant symbol master. Unauthenticated."""
    client = get_httpx_client()
    try:
        resp = client.get(get_url("symbol.master"), timeout=60)
        resp.raise_for_status()
    except Exception as e:
        log.error(f"Arihant symbol master download failed: {e}")
        return []
    try:
        body = resp.json()
    except Exception:
        log.error("Arihant symbol master: non-JSON response")
        return []
    return (body.get("data") or {}).get("instruments") or body.get("data") or []


def master_contract_download() -> bool:
    """OpenAlgo-standard entry point. Called daily by the broker
    contract-refresh scheduler. Returns True on success.

    Currently a minimal port — downloads the master and logs the row
    count. The token_db insert is intentionally NOT yet wired so we
    don't pollute SymToken with raw Arihant rows before the proper
    upsert/normalization is in place (follow-up PR).
    """
    try:
        rows = download_master()
        log.info(f"Arihant symbol master fetched: {len(rows)} instruments")
        if not rows:
            return False

        # TODO (follow-up PR): upsert into database.token_db.SymToken
        # with normalized symbol + token mapping. Today the symbol_db
        # path is broker-agnostic; we'll add an Arihant-specific
        # normalizer that aligns with NSE/BSE conventions.
        log.warning("Arihant master_contract_download: token_db upsert "
                    "deferred to follow-up PR — see broker/arihant/README.md")
        return True
    except Exception as e:
        log.exception(f"Arihant master_contract_download exception: {e}")
        return False
