"""ICICI Direct (Breeze API) auth.

ICICI Direct uses a custom token-based auth: the user logs in via the
Breeze browser flow at ``https://api.icicidirect.com/apiuser/login``, then
generates a ``session_token`` from their developer console. The
``session_token`` is exchanged for an ``access_token`` via the SDK's
``generate_session`` call (which is what BROKER_API_KEY + BROKER_API_SECRET
+ session_token combine into).

This module currently delegates to a static access_token in
BROKER_API_SECRET — the same shape IndMoney uses — to get the plugin
visible in the UI. The full programmatic auth flow (sha256 of
api_secret+session_token+timestamp → POST /customer-details, etc.) ports
in the follow-up PR.

Source for the full flow:
  * prod-alphaquark-github/aq_backend/Routes/Broker/icici.js (Breeze
    session generation + customer-details call)
  * ccxt-india/brokers/icici/icici.py (canonical SDK, 1375 lines)
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def authenticate_broker(code):  # noqa: ARG001 — kept for OpenAlgo contract
    """Minimal scaffolding: read access_token from BROKER_API_SECRET.

    Customer flow today:
      1. Sign up at ICICI's API portal (https://api.icicidirect.com)
      2. Generate session_token via the Breeze login UI
      3. Compute access_token using the Breeze SDK (one-time, daily)
      4. Paste the access_token as BROKER_API_SECRET in the dashboard
      5. The plugin uses it as-is until the daily refresh

    Follow-up PR adds the programmatic generate_session flow so step 3
    happens automatically.
    """
    try:
        access_token = os.getenv("BROKER_API_SECRET", "").strip()
        if not access_token:
            return None, ("ICICI Direct: paste your daily Breeze access_token "
                          "into the BROKER_API_SECRET field. Full programmatic "
                          "auth ports in follow-up PR.")
        return access_token, None
    except Exception as e:
        log.exception("ICICI authenticate_broker failed")
        return None, f"An exception occurred: {e}"
