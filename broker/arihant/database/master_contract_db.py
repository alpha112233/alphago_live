"""Arihant symbol-master download → OpenAlgo symtoken upsert.

Arihant exposes ``/wrapper-service/api/symbol/v1/master/cache`` as an
unauthenticated endpoint that returns the day's instrument master (all
exchange tokens + tradingsymbol mappings). On daily refresh we download
the master and populate the OpenAlgo ``symtoken`` table so the order
placement path can translate OpenAlgo's canonical symbol to Arihant's
``excToken``.

Arihant master row fields (per ccxt-india/brokers/arihant/arihant.py
inspection):
  - excToken        — numeric exchange-side token (what we send as
                       `excToken` on every order)
  - exchange        — "NSE" / "BSE" / "NFO" / "BFO" / "CDS" / "MCX"
  - tradingsymbol   — Arihant's symbol form (NSE: "SBIN-EQ"; F&O packed)
  - instrument      — "STK" / "FUT" / "OPT" / "OPTSTK" / "OPTIDX" / etc.
  - lotsize         — contract lot for F&O, 1 for equity
  - ticksize        — minimum price tick
  - expiry          — F&O expiry (DD-Mon-YYYY)
  - strike          — F&O strike price (0 for futures and equity)
  - optionType      — "CE" / "PE" / "" for futures/equity

Normalization to OpenAlgo symtoken:
  - Equity (NSE/BSE):  symbol = tradingsymbol with the "-EQ" suffix
                       stripped (canonical OpenAlgo form is bare ticker)
  - F&O:              symbol = packed `UNDERLYING + DDMMM + STRIKE + CE/PE`
                       (OpenAlgo convention; matches icicidirect/hdfcsec
                       port output so cross-broker symbol lookups work)
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import List, Optional

import pandas as pd

from broker.arihant.baseurl import get_url
from utils.httpx_client import get_httpx_client

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

# The master endpoint is per-exchange and PARAMETERISED now (2026-06: it
# requires ?exch=<EXCH>, and the data-returning route is /master — not
# /master/cache, which only warms the server-side cache and returns no
# instruments). Response: {"data": {"symbols": [{tradingSymbol, excTkn,
# exc, instrument, baseSym, ...}]}}.
_MASTER_EXCHANGES = ("NSE", "BSE", "NFO", "BFO", "MCX", "CDS")


def download_master() -> list[dict]:
    """Fetch the Arihant symbol master across exchanges. Unauthenticated."""
    client = get_httpx_client()
    base = get_url("/wrapper-service/api/symbol/v1/master")
    all_rows: list[dict] = []
    for exch in _MASTER_EXCHANGES:
        try:
            resp = client.get(base, params={"exch": exch}, timeout=90)
            if resp.status_code != 200:
                log.warning(f"Arihant master {exch}: http {resp.status_code}")
                continue
            body = resp.json()
        except Exception as e:
            log.warning(f"Arihant master {exch} download failed: {e}")
            continue
        rows = (body.get("data") or {}).get("symbols") or []
        if isinstance(rows, list):
            all_rows.extend(rows)
            log.info(f"Arihant master {exch}: {len(rows)} symbols")
    return all_rows


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

_MONTH_TITLE = {
    1: "JAN", 2: "FEB", 3: "MAR", 4: "APR", 5: "MAY", 6: "JUN",
    7: "JUL", 8: "AUG", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC",
}


def _to_float(s) -> float:
    try:
        return float(s) if s not in (None, "", "nan") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _to_int(s) -> int:
    try:
        return int(float(s)) if s not in (None, "", "nan") else 0
    except (TypeError, ValueError):
        return 0


def _expiry_to_oa_token(raw: str) -> str:
    """Arihant expiry (DD-Mon-YYYY or YYYY-MM-DD) -> OpenAlgo DDMMMYY (e.g.
    25JAN26). The 2-digit year is REQUIRED — it's part of the canonical
    OpenAlgo symbol (NIFTY16JUN2623950CE), and omitting it makes every F&O
    contract unresolvable against the canonical symbol the publisher sends."""
    if not raw:
        return ""
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            dt = datetime.strptime(str(raw), fmt)
            return f"{dt.day:02d}{_MONTH_TITLE[dt.month]}{dt.year % 100:02d}"
        except ValueError:
            continue
    return str(raw)


def _strip_eq_suffix(symbol: str) -> str:
    if not symbol:
        return symbol
    return re.sub(r"-(EQ|BE|BL|SM)$", "", symbol, flags=re.IGNORECASE)


def _normalise_row(row: dict) -> Optional[dict]:
    """Arihant master row -> OpenAlgo symtoken row. Returns None to skip.

    Handles the 2026-06 master format (exc / excTkn / tradingSymbol /
    baseSym) and the older one (exchange / excToken / tradingsymbol)."""
    exchange = (row.get("exc") or row.get("exchange") or row.get("exchangeSegment") or "").upper()
    instrument = (row.get("instrument") or row.get("instrumentType") or "").upper()
    raw_tsymbol = (row.get("tradingSymbol") or row.get("tradingsymbol") or "").strip()
    token = str(row.get("excTkn") or row.get("excToken") or row.get("token") or "").strip()
    if not exchange or not raw_tsymbol or not token:
        return None

    lotsize = _to_int(row.get("lotSize") or row.get("lotsize") or 1)
    ticksize = _to_float(row.get("tickSize") or row.get("ticksize") or 0.05)
    name = (row.get("baseSym") or row.get("dispSym") or row.get("companyName") or row.get("name") or "").strip()

    if exchange in ("NSE", "BSE") and instrument in ("", "STK", "EQ", "EQUITY"):
        # Equity
        return {
            "symbol": _strip_eq_suffix(raw_tsymbol).upper(),
            "brsymbol": raw_tsymbol,
            "name": name,
            "exchange": exchange,
            "brexchange": exchange,
            "token": token,
            "expiry": "",
            "strike": 0.0,
            "lotsize": lotsize or 1,
            "instrumenttype": "EQ",
            "tick_size": ticksize,
        }

    # F&O — pack into OpenAlgo's UNDERLYING+DDMMM+STRIKE+CE/PE form.
    underlying = (row.get("baseSym") or row.get("underlying")
                  or row.get("underlyingSymbol") or row.get("name") or "").upper().strip()
    if not underlying:
        # Fallback: derive from tradingsymbol prefix (heuristic).
        m = re.match(r"^([A-Z]+)", raw_tsymbol.upper())
        if not m:
            return None
        underlying = m.group(1)

    expiry_oa = _expiry_to_oa_token(row.get("expiry") or row.get("expiryDate") or "")
    strike = _to_float(row.get("strike") or row.get("strikePrice") or 0)
    opt_type = (row.get("optionType") or row.get("right") or "").upper().strip()
    opt_type = opt_type if opt_type in ("CE", "PE") else ""

    if not expiry_oa:
        return None

    if opt_type:
        strike_str = str(int(strike)) if float(strike).is_integer() else str(strike)
        packed = f"{underlying}{expiry_oa}{strike_str}{opt_type}"
    elif instrument in ("FUT", "FUTSTK", "FUTIDX", "FUTCUR", "FUTCOM"):
        packed = f"{underlying}{expiry_oa}FUT"
    else:
        return None  # unknown F&O shape — skip

    # Map exchange to OpenAlgo convention. Arihant gives the raw segment as the
    # derivative exchange itself (NFO/BFO/...), NOT the cash exchange — so the
    # NSE-vs-everything-else test below was sending all NSE F&O to BFO. Treat
    # NSE/NFO as NFO and BSE/BFO as BFO.
    oa_exch = exchange
    if instrument in ("OPTSTK", "OPTIDX", "FUTSTK", "FUTIDX"):
        oa_exch = "NFO" if exchange in ("NSE", "NFO") else "BFO"
    elif instrument in ("OPTCUR", "FUTCUR"):
        oa_exch = "CDS"
    elif instrument in ("OPTCOM", "FUTCOM"):
        oa_exch = "MCX"

    return {
        "symbol": packed,
        "brsymbol": raw_tsymbol,
        "name": underlying,
        "exchange": oa_exch,
        "brexchange": exchange,
        "token": token,
        "expiry": str(row.get("expiry") or row.get("expiryDate") or ""),
        "strike": strike,
        "lotsize": lotsize or 1,
        "instrumenttype": instrument or "FUT",
        "tick_size": ticksize,
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def populate_token_db(rows: List[dict]) -> int:
    """Insert normalised rows into the OpenAlgo symtoken table.
    Returns the count of rows inserted. 0 on any failure."""
    if not rows:
        return 0

    try:
        from sqlalchemy import create_engine

        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            log.error("Arihant master: DATABASE_URL unset — cannot upsert symtoken")
            return 0

        engine = create_engine(database_url)
        df = pd.DataFrame(rows)
        df = df.drop_duplicates(subset=["symbol", "exchange"], keep="first")

        # Clear+insert per the established convention used by other
        # broker masters (definedge, icicidirect, hdfcsec).
        with engine.begin() as conn:
            conn.exec_driver_sql("DELETE FROM symtoken")
        df.to_sql("symtoken", con=engine, if_exists="append", index=False)
        log.info(f"Arihant symtoken refresh OK: {len(df)} rows")
        return len(df)
    except Exception as e:
        log.exception(f"Arihant populate_token_db failed: {e}")
        return 0


def master_contract_download() -> bool:
    """OpenAlgo-standard entry point. Called daily by the broker
    contract-refresh scheduler. Returns True on success.
    """
    try:
        raw_rows = download_master()
        if not raw_rows:
            log.warning("Arihant master: empty download")
            return False
        log.info(f"Arihant symbol master fetched: {len(raw_rows)} instruments")

        normalised = [n for r in raw_rows if (n := _normalise_row(r)) is not None]
        if not normalised:
            log.error("Arihant master: 0 rows after normalisation — schema drift?")
            return False

        inserted = populate_token_db(normalised)
        if inserted == 0:
            return False

        log.info(f"Arihant master_contract_download OK: {inserted} rows persisted")
        return True
    except Exception as e:
        log.exception(f"Arihant master_contract_download exception: {e}")
        return False
