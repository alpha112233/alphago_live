"""HDFC Securities auth — scaffolding.

HDFC Securities API uses OAuth + daily access_token rotation. The full
flow (per prod-alphaquark-github/aq_backend/Routes/Broker/Hdfc.js) is:
  1. App registration on developer.hdfcsec.com → API Key + Secret
  2. User completes OAuth redirect → returns auth_code
  3. POST /api/oauth2/token with auth_code → access_token (24h validity)
  4. access_token used as Bearer in all subsequent calls

This scaffold reads a manually-pasted access_token from
BROKER_API_SECRET — same pattern as the ICICI scaffold. Full OAuth port
in follow-up PR.

Source for the full port:
  * prod-alphaquark-github/aq_backend/Routes/Broker/Hdfc.js
  * ccxt-india/brokers/hdfc/hdfcsec.py (749 lines)
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def authenticate_broker(code):  # noqa: ARG001 — kept for OpenAlgo contract
    try:
        access_token = os.getenv("BROKER_API_SECRET", "").strip()
        if not access_token:
            return None, ("HDFC Securities: paste your daily access_token "
                          "into the BROKER_API_SECRET field. Full OAuth flow "
                          "lands in follow-up PR.")
        return access_token, None
    except Exception as e:
        log.exception("HDFC authenticate_broker failed")
        return None, f"An exception occurred: {e}"
