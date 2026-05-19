"""
services/auto_login_scheduler_service.py
========================================
Daily pre-market broker auto-login scheduler.

For every broker the operator has saved credentials for (and that has a
TOTP seed, except IndMoney whose token is long-lived), this scheduler
mints a fresh access_token before market open so the customer's
strategies have an authenticated broker session waiting at 09:15 IST.

Schedule (configurable via env, all read at app start):
    AUTO_LOGIN_ENABLED   default "true"    — set "false" to disable entirely
    AUTO_LOGIN_HOUR      default "8"       — 0-23 in Asia/Kolkata
    AUTO_LOGIN_MINUTE    default "0"       — 0-59
    AUTO_LOGIN_DOW       default "mon-fri" — APScheduler day_of_week spec

Why 08:00 IST by default:
    - NSE/BSE pre-market opens at 09:00, normal market at 09:15
    - Most brokers' daily auth tokens expire EOD (e.g. Upstox at 03:30 IST,
      Zerodha at 06:00 IST) — by 08:00 yesterday's token is dead everywhere
    - Leaves a 75-minute window for failures to surface and the operator
      to react via the Auto Login button before the open
    - Falls inside every broker's TOTP/OTP rate-limit-friendly hours

Holiday handling:
    Not yet — we run the cron Mon-Fri. NSE trading-holiday calendar varies
    year-to-year, so attempting to encode it here is more brittle than
    just letting the broker reject the login and logging the error. The
    cost of one extra HTTP call to a broker on a holiday is ~zero; the
    cost of NOT logging in on a working Monday because our table said it
    was a holiday is one full lost trading day.

Concurrency:
    The OpenAlgo deployment runs single-worker eventlet gunicorn, so
    APScheduler can live in-process. job_defaults={coalesce, max_instances=1}
    means a stuck job won't pile up — at worst we lose that day's run.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

_IST = pytz.timezone("Asia/Kolkata")
_scheduler: Optional[BackgroundScheduler] = None


def _is_enabled() -> bool:
    return (os.getenv("AUTO_LOGIN_ENABLED") or "true").strip().lower() in ("1", "true", "yes", "on")


def run_daily_auto_logins() -> dict:
    """Iterate every admin user's saved brokers and mint a fresh
    access_token for each one that's adapter-eligible. Returns a small
    summary so callers can log it (or surface via /auto-login-status if
    we ever wire one).

    Adapter-eligible means: a registered adapter in
    broker_login_adapters.ADAPTERS AND (a saved TOTP seed OR broker is
    IndMoney, whose adapter uses the static api_secret instead).
    """
    summary = {"users_seen": 0, "brokers_tried": 0, "ok": 0, "failed": 0, "skipped": 0}
    try:
        from database.user_db import db_session, User
        from database.broker_creds_db import list_user_brokers
        from broker_login_adapters import ADAPTERS
        from blueprints.broker_credentials import run_auto_login_for_broker

        admins = db_session.query(User).filter_by(is_admin=True).all()
        if not admins:
            logger.warning("auto-login scheduler: no admin user found, skipping run")
            return summary

        for user in admins:
            summary["users_seen"] += 1
            try:
                brokers = list_user_brokers(user.id)
            except Exception:
                logger.exception(f"auto-login: list_user_brokers failed for user={user.username}")
                continue

            for entry in brokers:
                broker = (entry.get("broker") or "").strip().lower()
                if broker not in ADAPTERS:
                    summary["skipped"] += 1
                    continue
                if broker != "indmoney" and not entry.get("has_totp_seed"):
                    summary["skipped"] += 1
                    continue

                summary["brokers_tried"] += 1
                try:
                    res = run_auto_login_for_broker(user.id, user.username, broker)
                except Exception as e:
                    logger.exception(f"auto-login crashed user={user.username} broker={broker}")
                    summary["failed"] += 1
                    continue

                if res.get("ok"):
                    summary["ok"] += 1
                    logger.info(f"auto-login OK user={user.username} broker={broker}")

                    # Pre-download the day's master contract so the first user
                    # login of the day doesn't pay the ~60s download cost on
                    # the worker thread (the actual blocking issue surfaced
                    # in the 2026-05-19 slow Manage Brokers investigation —
                    # `async_master_contract_download` runs in a daemon Thread
                    # off the login response and holds the GIL during the
                    # bulk SQL insert + cache load, blocking other requests
                    # to gunicorn --workers=1 for ~35s).
                    #
                    # We're inside an APScheduler worker thread here, and
                    # market open is ~75 min away (08:00 → 09:15 IST default),
                    # so a sync download is fine — even 2-3 brokers serially
                    # at ~60s each finishes by 08:05 IST.
                    try:
                        from utils.auth_utils import (
                            should_download_master_contract,
                            async_master_contract_download,
                        )
                        should_dl, reason = should_download_master_contract(broker)
                        if should_dl:
                            logger.info(
                                f"auto-login: pre-downloading master contract for "
                                f"{broker} (user={user.username}, reason={reason})"
                            )
                            # No app_context wrapping: the existing user-login
                            # path also calls this from a bare daemon Thread
                            # without context — the function is context-safe
                            # (db_session is scoped per-thread; socketio emits
                            # fail silently if no context, but the download
                            # + DB writes succeed).
                            async_master_contract_download(broker)
                            summary.setdefault("contracts_downloaded", 0)
                            summary["contracts_downloaded"] += 1
                        else:
                            logger.debug(
                                f"auto-login: master contract for {broker} is "
                                f"current ({reason}); skipping pre-download"
                            )
                    except Exception:
                        # Download failure is non-fatal — the user-login path
                        # will retry on demand. Log + continue.
                        logger.exception(
                            f"auto-login: master contract pre-download crashed "
                            f"for user={user.username} broker={broker}"
                        )
                else:
                    summary["failed"] += 1
                    logger.warning(
                        f"auto-login FAIL user={user.username} broker={broker} "
                        f"kind={res.get('error_kind')} err={res.get('error')}"
                    )
    finally:
        # SQLAlchemy scoped_session needs to be cleaned up after non-request
        # work, otherwise the worker accumulates idle connections.
        try:
            from database.user_db import db_session
            db_session.remove()
        except Exception:
            pass

    logger.info(f"auto-login scheduler run finished: {summary}")
    return summary


def init_auto_login_scheduler() -> None:
    """Start the daily APScheduler job. Idempotent — safe to call once at
    app startup. No-op if AUTO_LOGIN_ENABLED is false."""
    global _scheduler

    if not _is_enabled():
        logger.info("auto-login scheduler disabled (AUTO_LOGIN_ENABLED=false)")
        return
    if _scheduler is not None:
        logger.debug("auto-login scheduler already initialized")
        return

    try:
        hour = int((os.getenv("AUTO_LOGIN_HOUR") or "8").strip())
    except ValueError:
        hour = 8
    try:
        minute = int((os.getenv("AUTO_LOGIN_MINUTE") or "0").strip())
    except ValueError:
        minute = 0
    dow = (os.getenv("AUTO_LOGIN_DOW") or "mon-fri").strip().lower()

    _scheduler = BackgroundScheduler(
        timezone=_IST,
        job_defaults={
            "coalesce": True,
            "misfire_grace_time": 600,
            "max_instances": 1,
        },
    )
    _scheduler.add_job(
        run_daily_auto_logins,
        trigger=CronTrigger(day_of_week=dow, hour=hour, minute=minute, timezone=_IST),
        id="auto_login_daily",
        name="Daily pre-market broker auto-login",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info(
        f"auto-login scheduler started (dow={dow} time={hour:02d}:{minute:02d} IST)"
    )


def get_scheduler_status() -> dict:
    """Inspector for the /api/broker/credentials/auto-login-status endpoint."""
    if _scheduler is None:
        return {"enabled": _is_enabled(), "running": False, "next_run": None}
    job = _scheduler.get_job("auto_login_daily")
    next_run = job.next_run_time.isoformat() if (job and job.next_run_time) else None
    return {
        "enabled": True,
        "running": _scheduler.running,
        "next_run": next_run,
    }
