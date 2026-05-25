"""HDFC InvestRight master contract download + symtoken population.

Source: https://developer.hdfcsec.com/oapi/v1/security-master
The endpoint returns a CSV (or zipped CSV) with these columns
(canonical list from ccxt-india/brokers/hdfc/hdfcsec_scrip_master.py):

    exchange, security_id, instrument_segment, expiry_date, strike_price,
    option_type, lot_size, tick_size, close_price, exch_security_id,
    symbol_name, underline_symbol, open_price

Mapping to OpenAlgo's `symtoken`:

  - For NSE/BSE equity (instrument_segment == "EQUITY"):
        symbol     = symbol_name (e.g. "RELIANCE")
        brsymbol   = symbol_name
        exchange   = exchange ("NSE" / "BSE")
        token      = security_id (HDFC's internal id used in order API)
        lotsize    = 1
        instrumenttype = "EQUITY"

  - For F&O (FUTSTK / OPTSTK / FUTIDX / OPTIDX / FUTCUR / OPTCUR /
    FUTCOM / OPTFUT):
        symbol     = packed OpenAlgo string (UNDERLYING + DDMMM + strike + CE/PE
                     OR + FUT for futures)
        brsymbol   = symbol_name (HDFC's own packed form)
        exchange   = "NFO" / "BFO" / "CDS" / "MCX"
        token      = security_id
        lotsize    = lot_size
        instrumenttype = instrument_segment
        expiry     = expiry_date
        strike     = strike_price
"""
from __future__ import annotations

import io
import os
import zipfile
from datetime import datetime
from typing import Optional

import pandas as pd
from sqlalchemy import (
    Column,
    Float,
    Integer,
    Sequence,
    String,
    create_engine,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker

from broker.hdfcsec.api.hdfc_http import parse_auth
from broker.hdfcsec.baseurl import SECURITY_MASTER_URL
from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(DATABASE_URL) if DATABASE_URL else None
db_session = (
    scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
    if engine else None
)
Base = declarative_base()
if db_session is not None:
    Base.query = db_session.query_property()


class SymToken(Base):
    __tablename__ = "symtoken"
    id = Column(Integer, Sequence("symtoken_id_seq"), primary_key=True)
    symbol = Column(String, nullable=False, index=True)
    brsymbol = Column(String, nullable=False, index=True)
    name = Column(String)
    exchange = Column(String, index=True)
    brexchange = Column(String, index=True)
    token = Column(String, index=True)
    expiry = Column(String)
    strike = Column(Float)
    lotsize = Column(Integer)
    instrumenttype = Column(String)
    tick_size = Column(Float)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def init_db() -> None:
    if engine is None:
        logger.error("HDFC master: DATABASE_URL unset — nothing to init")
        return
    Base.metadata.create_all(bind=engine)


def delete_symtoken_table() -> None:
    if db_session is None:
        return
    try:
        db_session.query(SymToken).delete()
        db_session.commit()
        logger.info("symtoken table cleared")
    except Exception as e:
        logger.error(f"clear symtoken failed: {e}")
        db_session.rollback()


def copy_from_dataframe(df: pd.DataFrame) -> None:
    if engine is None or df.empty:
        return
    try:
        df.to_sql("symtoken", con=engine, if_exists="append", index=False)
        logger.info(f"Inserted {len(df)} rows into symtoken")
    except Exception as e:
        logger.error(f"insert symtoken failed: {e}")


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_security_master(auth_string: str, output_path: str) -> Optional[str]:
    """Download the security-master CSV. Returns local path to the CSV file."""
    try:
        os.makedirs(output_path, exist_ok=True)
        access_token, api_key, _ = parse_auth(auth_string)
        client = get_httpx_client()
        resp = client.get(
            SECURITY_MASTER_URL,
            headers={
                "Authorization": access_token,
                "User-Agent": "alphago-live/1.0",
            },
            params={"api_key": api_key},
            timeout=60,
        )
        resp.raise_for_status()
        content_type = (resp.headers.get("Content-Type") or "").lower()
        out_path = os.path.join(output_path, "hdfc_security_master.csv")
        # Handle gzipped CSV too — InvestRight has been known to send either.
        if "zip" in content_type or resp.content[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                inner = zf.namelist()[0]
                with zf.open(inner) as src, open(out_path, "wb") as dst:
                    dst.write(src.read())
        else:
            with open(out_path, "wb") as f:
                f.write(resp.content)
        logger.info(f"HDFC security master downloaded → {out_path}")
        return out_path
    except Exception as e:
        logger.error(f"HDFC security-master download failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Transform
# ---------------------------------------------------------------------------

_MONTH_TITLE = ["", "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


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


def _ddmmyyyy_to_oa_expiry(s: str) -> str:
    """HDFC stores expiry as DDMMYYYY or DD-MM-YYYY; we want DDMMM (e.g. 25JAN)."""
    if not s:
        return ""
    raw = s.replace("-", "").replace("/", "").strip()
    if len(raw) != 8 or not raw.isdigit():
        return s
    try:
        day = int(raw[0:2])
        month = int(raw[2:4])
        return f"{day:02d}{_MONTH_TITLE[month]}"
    except Exception:
        return s


def _pack_fno_symbol(row) -> str:
    underlying = (row.get("underline_symbol") or row.get("underlying_symbol") or "").strip().upper()
    if not underlying:
        return ""
    seg = (row.get("instrument_segment") or "").upper()
    expiry_dd = _ddmmyyyy_to_oa_expiry(str(row.get("expiry_date") or ""))
    if not expiry_dd:
        return ""
    if seg in ("FUTSTK", "FUTIDX", "FUTCUR", "FUTCOM"):
        return f"{underlying}{expiry_dd}FUT"
    if seg in ("OPTSTK", "OPTIDX", "OPTCUR", "OPTFUT"):
        opt = (row.get("option_type") or "").strip().upper()
        if opt not in ("CE", "PE"):
            return ""
        strike = _to_float(row.get("strike_price"))
        strike_str = str(int(strike)) if float(strike).is_integer() else str(strike)
        return f"{underlying}{expiry_dd}{strike_str}{opt}"
    return ""


def _exchange_for_segment(seg: str, exchange: str) -> str:
    seg = (seg or "").upper()
    if seg in ("FUTSTK", "OPTSTK", "FUTIDX", "OPTIDX"):
        return "NFO" if exchange == "NSE" else "BFO"
    if seg in ("FUTCUR", "OPTCUR"):
        return "CDS"
    if seg in ("FUTCOM", "OPTFUT"):
        return "MCX"
    return exchange


def process_master(path: str) -> pd.DataFrame:
    """Read the HDFC master CSV → OpenAlgo symtoken DataFrame."""
    try:
        df = pd.read_csv(path, dtype=str, low_memory=False)
    except Exception as e:
        logger.error(f"HDFC master CSV read failed: {e}")
        return pd.DataFrame()

    df.columns = [c.strip() for c in df.columns]
    # Normalize alternative column names found in older feeds.
    rename = {
        "InstrumentSegment": "instrument_segment",
        "SecurityId": "security_id",
        "ExchangeSecurityId": "exch_security_id",
        "LotSize": "lot_size", "TickSize": "tick_size",
        "ExpiryDate": "expiry_date", "StrikePrice": "strike_price",
        "OptionType": "option_type", "SymbolName": "symbol_name",
        "UnderlineSymbol": "underline_symbol",
        "ClosePrice": "close_price", "OpenPrice": "open_price",
        "Exchange": "exchange",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    rows = []
    for _, r in df.iterrows():
        seg = str(r.get("instrument_segment") or "").upper()
        exchange = str(r.get("exchange") or "").upper()
        if not seg or not exchange:
            continue
        oa_exchange = _exchange_for_segment(seg, exchange)
        security_id = str(r.get("security_id") or "").strip()
        if not security_id:
            continue

        if seg == "EQUITY":
            sym = str(r.get("symbol_name") or "").strip().upper()
            if not sym:
                continue
            rows.append({
                "symbol": sym,
                "brsymbol": sym,
                "name": sym,
                "exchange": oa_exchange,
                "brexchange": exchange,
                "token": security_id,
                "expiry": "",
                "strike": 0.0,
                "lotsize": _to_int(r.get("lot_size") or 1),
                "instrumenttype": "EQUITY",
                "tick_size": _to_float(r.get("tick_size") or 0.05),
            })
        else:
            packed = _pack_fno_symbol(r)
            if not packed:
                continue
            rows.append({
                "symbol": packed,
                "brsymbol": str(r.get("symbol_name") or packed),
                "name": str(r.get("underline_symbol") or ""),
                "exchange": oa_exchange,
                "brexchange": exchange,
                "token": security_id,
                "expiry": str(r.get("expiry_date") or ""),
                "strike": _to_float(r.get("strike_price")),
                "lotsize": _to_int(r.get("lot_size") or 1),
                "instrumenttype": seg,
                "tick_size": _to_float(r.get("tick_size") or 0.05),
            })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def master_contract_download(auth_string: str, output_path: Optional[str] = None) -> bool:
    """End-to-end: download CSV, parse, replace symtoken contents."""
    if output_path is None:
        output_path = f"/tmp/hdfc_master_{datetime.now().strftime('%Y%m%d')}"

    path = download_security_master(auth_string, output_path)
    if not path:
        return False

    init_db()
    delete_symtoken_table()

    df = process_master(path)
    if df.empty:
        logger.error("HDFC master: no rows parsed")
        return False

    df = df.drop_duplicates(subset=["symbol", "exchange"], keep="first")
    copy_from_dataframe(df)
    logger.info(f"HDFC master refresh OK: {len(df)} rows total")
    return True
