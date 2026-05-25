"""ICICI Breeze master contract download + symtoken population.

Source: https://directlink.icicidirect.com/NewSecurityMaster/SecurityMaster.zip
Files inside (we use four of five):
    NSEScripMaster.txt   — NSE cash equity
    BSEScripMaster.txt   — BSE cash equity
    FONSEScripMaster.txt — NSE F&O
    FOBSEScripMaster.txt — BSE F&O
    CDNSEScripMaster.txt — Currency derivatives (NSE side)

For each row we populate one entry in the OpenAlgo `symtoken` table with:
    symbol        — OpenAlgo-style tradingsymbol (NSE: ExchangeCode;
                    F&O: packed root+expiry+strike+CE/PE)
    brsymbol      — Breeze's ShortName (the value Breeze expects as
                    `stock_code` in every API call)
    token         — Breeze internal Token
    exchange      — OpenAlgo exchange (NSE/BSE/NFO/BFO/CDS)
    brexchange    — Breeze exchange_code (NSE/BSE/NFO/BFO)
    name          — CompanyName from the master
    expiry, strike, lotsize, instrumenttype, tick_size — Breeze fields
"""
from __future__ import annotations

import io
import os
import re
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

from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(DATABASE_URL) if DATABASE_URL else None
db_session = (
    scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
    if engine
    else None
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


MASTER_URL = (
    "https://directlink.icicidirect.com/NewSecurityMaster/SecurityMaster.zip"
)

_FILES = [
    "NSEScripMaster.txt",
    "BSEScripMaster.txt",
    "FONSEScripMaster.txt",
    "FOBSEScripMaster.txt",
    "CDNSEScripMaster.txt",
]


# ---------------------------------------------------------------------------
# Setup / teardown
# ---------------------------------------------------------------------------

def init_db() -> None:
    if engine is None:
        logger.error("ICICI master: DATABASE_URL unset — nothing to init")
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
# Download + extract
# ---------------------------------------------------------------------------

def download_icici_master_files(output_path: str) -> bool:
    """Pull SecurityMaster.zip and extract the five master TXTs.

    Output: each TXT file at `os.path.join(output_path, filename)`.
    Returns True on success.
    """
    try:
        os.makedirs(output_path, exist_ok=True)
        client = get_httpx_client()
        logger.info(f"Downloading ICICI master from {MASTER_URL}")
        resp = client.get(MASTER_URL, timeout=60)
        resp.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            for name in _FILES:
                try:
                    zf.extract(name, output_path)
                    logger.info(f"  extracted {name}")
                except KeyError:
                    logger.warning(f"  missing in archive: {name}")
        return True
    except Exception as e:
        logger.error(f"ICICI master download failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Per-segment transforms
# ---------------------------------------------------------------------------

def _read_master(path: str) -> Optional[pd.DataFrame]:
    """Read a CSV/TXT master file, normalizing dtypes to str."""
    try:
        df = pd.read_csv(path, dtype=str, low_memory=False)
        df.columns = [c.strip() for c in df.columns]
        return df
    except FileNotFoundError:
        logger.warning(f"master file not found: {path}")
        return None
    except Exception as e:
        logger.error(f"failed to read master {path}: {e}")
        return None


def _to_float(s: Optional[str]) -> float:
    try:
        return float(s) if s is not None and s != "" else 0.0
    except (TypeError, ValueError):
        return 0.0


def _to_int(s: Optional[str]) -> int:
    try:
        return int(float(s)) if s is not None and s != "" else 0
    except (TypeError, ValueError):
        return 0


def process_nse_csv(path: str) -> pd.DataFrame:
    """NSE cash master: ExchangeCode -> symbol, ShortName -> brsymbol."""
    df = _read_master(path)
    if df is None or df.empty:
        return pd.DataFrame()

    out = pd.DataFrame()
    out["symbol"] = df.get("ExchangeCode", df.get("Symbol", "")).fillna("").astype(str)
    out["brsymbol"] = df.get("ShortName", "").fillna("").astype(str)
    out["name"] = df.get("CompanyName", "").fillna("").astype(str)
    out["exchange"] = "NSE"
    out["brexchange"] = "NSE"
    out["token"] = df.get("Token", "").fillna("").astype(str)
    out["expiry"] = ""
    out["strike"] = 0.0
    out["lotsize"] = df.get("Lotsize", df.get("BoardLotQty", "0")).fillna("0").map(_to_int)
    out["instrumenttype"] = df.get("InstrumentType", df.get("Series", "EQ")).fillna("EQ").astype(str)
    out["tick_size"] = df.get("ticksize", df.get("Ticksize", "0.05")).fillna("0.05").map(_to_float)

    out = out[(out["symbol"] != "") & (out["brsymbol"] != "")]
    return out


def process_bse_csv(path: str) -> pd.DataFrame:
    df = _read_master(path)
    if df is None or df.empty:
        return pd.DataFrame()

    out = pd.DataFrame()
    out["symbol"] = df.get("ScripID", df.get("ExchangeCode", "")).fillna("").astype(str)
    out["brsymbol"] = df.get("ShortName", "").fillna("").astype(str)
    out["name"] = df.get("ScripName", df.get("CompanyName", "")).fillna("").astype(str)
    out["exchange"] = "BSE"
    out["brexchange"] = "BSE"
    out["token"] = df.get("Token", df.get("ScripCode", "")).fillna("").astype(str)
    out["expiry"] = ""
    out["strike"] = 0.0
    out["lotsize"] = df.get("Lotsize", df.get("MarketLot", "1")).fillna("1").map(_to_int)
    out["instrumenttype"] = df.get("Series", "C").fillna("C").astype(str)
    out["tick_size"] = df.get("Ticksize", "0.01").fillna("0.01").map(_to_float)

    out = out[(out["symbol"] != "") & (out["brsymbol"] != "")]
    return out


_MONTH3 = {
    1: "JAN", 2: "FEB", 3: "MAR", 4: "APR", 5: "MAY", 6: "JUN",
    7: "JUL", 8: "AUG", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC",
}


def _pack_fno_symbol(root: str, expiry: str, strike: float, right: str) -> str:
    """Build OpenAlgo-style F&O symbol: NIFTY25JAN24500CE / NIFTY25JANFUT.

    Expiry is the master's "ExpiryDate" string (YYYY-MM-DD or DD-Mon-YYYY).
    """
    try:
        dt = None
        for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y"):
            try:
                dt = datetime.strptime(expiry, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return ""
        ddmon = f"{dt.day:02d}{_MONTH3[dt.month]}"
        if right in ("CE", "PE"):
            strike_int = int(strike) if float(strike).is_integer() else strike
            return f"{root}{ddmon}{strike_int}{right}"
        return f"{root}{ddmon}FUT"
    except Exception:
        return ""


def process_fo_csv(path: str, exchange: str) -> pd.DataFrame:
    """F&O (NFO/BFO) master. exchange = 'NFO' or 'BFO'."""
    df = _read_master(path)
    if df is None or df.empty:
        return pd.DataFrame()

    rights = df.get("OptionType", "").fillna("").str.upper()
    series = df.get("Series", "").fillna("").str.upper()
    root = df.get("ShortName", "").fillna("").astype(str)
    expiry_raw = df.get("ExpiryDate", "").fillna("").astype(str)
    strike = df.get("StrikePrice", "0").fillna("0").map(_to_float)
    lot = df.get("LotSize", df.get("Lotsize", "0")).fillna("0").map(_to_int)
    name = df.get("CompanyName", "").fillna("").astype(str)
    tokens = df.get("Token", "").fillna("").astype(str)
    tick = df.get("TickSize", df.get("Ticksize", "0.05")).fillna("0.05").map(_to_float)
    exch_code = df.get("ExchangeCode", "").fillna("").astype(str)

    # Synthesise OpenAlgo packed symbols. Use ExchangeCode if it already
    # carries an OpenAlgo-style string (RELIANCE25JAN3000CE); otherwise
    # build from components.
    packed = []
    for i in range(len(df)):
        ec = exch_code.iloc[i] if i < len(exch_code) else ""
        if ec and re.search(r"\d{2}[A-Z]{3}", ec):
            packed.append(ec)
            continue
        is_option = rights.iloc[i] in ("CE", "PE") and series.iloc[i] in ("OPTION", "OPTSTK", "OPTIDX")
        right = rights.iloc[i] if is_option else "FUT"
        packed.append(_pack_fno_symbol(root.iloc[i], expiry_raw.iloc[i], strike.iloc[i], right))

    out = pd.DataFrame({
        "symbol": packed,
        "brsymbol": root,
        "name": name,
        "exchange": exchange,
        "brexchange": exchange,
        "token": tokens,
        "expiry": expiry_raw,
        "strike": strike,
        "lotsize": lot,
        "instrumenttype": series,
        "tick_size": tick,
    })
    out = out[(out["symbol"] != "") & (out["brsymbol"] != "")]
    return out


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def master_contract_download(output_path: Optional[str] = None) -> bool:
    """End-to-end: download, parse, replace symtoken contents.

    Output directory defaults to `/tmp/icici_master_<date>`.
    """
    if output_path is None:
        output_path = f"/tmp/icici_master_{datetime.now().strftime('%Y%m%d')}"

    if not download_icici_master_files(output_path):
        return False

    init_db()
    delete_symtoken_table()

    frames = []
    nse = process_nse_csv(os.path.join(output_path, "NSEScripMaster.txt"))
    if not nse.empty:
        frames.append(nse)
    bse = process_bse_csv(os.path.join(output_path, "BSEScripMaster.txt"))
    if not bse.empty:
        frames.append(bse)
    nfo = process_fo_csv(os.path.join(output_path, "FONSEScripMaster.txt"), "NFO")
    if not nfo.empty:
        frames.append(nfo)
    bfo = process_fo_csv(os.path.join(output_path, "FOBSEScripMaster.txt"), "BFO")
    if not bfo.empty:
        frames.append(bfo)
    cds = process_fo_csv(os.path.join(output_path, "CDNSEScripMaster.txt"), "CDS")
    if not cds.empty:
        frames.append(cds)

    if not frames:
        logger.error("ICICI master: no rows parsed from any segment")
        return False

    merged = pd.concat(frames, ignore_index=True)
    merged = merged.drop_duplicates(subset=["symbol", "exchange"], keep="first")
    copy_from_dataframe(merged)
    logger.info(f"ICICI master refresh OK: {len(merged)} rows total")
    return True
