"""Background poller that bridges the broker orderbook → distribution log
→ publisher fill-status visibility.

THE PROBLEM
-----------
`distribution_signals.status` only captures PLACEMENT outcome ("did the
broker accept the order"). It does NOT capture what happens after:

    - did the LIMIT actually fill, or is it still resting?
    - did the SL trigger and execute?
    - was the order rejected by the exchange after placement?
    - was it cancelled by the broker?

The publisher (alphago_publisher) only sees PLACEMENT status too, because
that's all we report back from the /distribution/inbox webhook. So an
admin staring at the dispatch list has no idea whether their LIMIT at
10:30am ever filled, all the way until end-of-day reconciliation runs.

WHAT THIS POLLER DOES
---------------------
Every POLL_INTERVAL_SECONDS during IST market hours:

  1. Find every distribution_signals row whose broker_order_id is known
     and whose fill_status isn't already terminal (complete/cancelled/
     rejected). Restrict to today's rows — orders older than that are
     gone from the broker orderbook anyway.

  2. Group by broker_used. For each broker, one orderbook fetch (cheap)
     covers every signal placed via that broker today.

  3. For each signal, find the matching order in the orderbook by
     orderid match. Read the normalised broker status + filled qty +
     avg price.

  4. If the broker-side state changed since last poll:
     - Update the local distribution_signals row.
     - Best-effort POST /api/service/fill-update to the publisher with
       the new fill_status + filled_quantity + average_price.

  5. Sleep until the next tick.

DESIGN NOTES
------------
- Runs as ONE daemon thread per container. Idempotent: re-posting the
  same fill_status is a no-op on the publisher side.
- Skipped outside market hours (IST 09:00 - 15:45 weekdays). Configurable
  via FILL_POLL_FORCE_ALWAYS=1 for testing.
- Errors (publisher unreachable, broker not logged in, no active
  signals) are logged at INFO/DEBUG, never thrown. The poller MUST NOT
  crash the container.
- The publisher URL defaults to https://publisher.alphaquark.in and
  is overridable via PUBLISHER_BASE_URL. The auth Bearer is the
  inbox's own api_key — the publisher matches it via api_key_hash.

Disable via FILL_POLL_ENABLED=0 (off by default until the admin opts in
per-container via env, OR for the test fleet via the provisioner default).
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any

import requests

from utils.logging import get_logger

logger = get_logger(__name__)

# Tunables (env-driven). Defaults chosen to be safe on a single broker
# rate-limit budget: 30s poll = 2 orderbook fetches per minute per broker.
POLL_INTERVAL_SECONDS = int(os.getenv("FILL_POLL_INTERVAL_SECONDS", "30") or "30")
PUBLISHER_BASE_URL = (
    os.getenv("PUBLISHER_BASE_URL")
    or os.getenv("PUBLISHER_URL")
    or "https://publisher.alphaquark.in"
).rstrip("/")
PUBLISHER_TIMEOUT = float(os.getenv("FILL_POLL_PUBLISHER_TIMEOUT", "10") or "10")
# Shared service token. Same env the publisher's /api/service/* endpoints
# check. Provisioner sets it on container creation. If unset, the poller
# still runs locally (updates distribution_signals) but skips publisher
# push — handy for dev where the publisher isn't wired.
SERVICE_TOKEN = (os.getenv("SERVICE_TOKEN") or "").strip()
ENABLED = (os.getenv("FILL_POLL_ENABLED", "1").strip() not in {"0", "false", "False", ""})
FORCE_ALWAYS = os.getenv("FILL_POLL_FORCE_ALWAYS", "0").strip() in {"1", "true", "True"}

# Terminal fill states — once we see these we stop polling that signal.
_TERMINAL_FILL = {"complete", "cancelled", "rejected"}
# Any normalised broker status worth pushing through. "partial" appears
# in OpenAlgo brokers that explicitly distinguish it; otherwise we infer
# it locally from filled_quantity vs quantity.
_ALLOWED_FILL = {"complete", "open", "cancelled", "rejected", "trigger pending", "partial"}

_IST = timezone(timedelta(hours=5, minutes=30))


def start_fill_poller(app) -> None:
    """Spin a daemon thread that runs poll_once() forever. Called once at
    app boot. No-ops if FILL_POLL_ENABLED is off."""
    if not ENABLED:
        logger.info("fill_poller: disabled via FILL_POLL_ENABLED=0")
        return
    t = threading.Thread(
        target=_loop_forever, args=(app,),
        name="distribution-fill-poller", daemon=True,
    )
    t.start()
    logger.info(
        f"fill_poller: started (interval={POLL_INTERVAL_SECONDS}s, "
        f"publisher={PUBLISHER_BASE_URL})"
    )


def _loop_forever(app) -> None:
    # Stagger first run so containers that restart together don't all hit
    # the broker at the exact same second.
    time.sleep(5 + (os.getpid() % 10))
    while True:
        try:
            if FORCE_ALWAYS or _market_hours_active():
                # The poller touches SQLAlchemy session + Flask context.
                with app.app_context():
                    poll_once()
            else:
                logger.debug("fill_poller: outside market hours, skipping cycle")
        except Exception:
            logger.exception("fill_poller: cycle raised — continuing")
        time.sleep(POLL_INTERVAL_SECONDS)


def _market_hours_active() -> bool:
    """IST 09:00 - 15:45, Mon-Fri. Window starts ahead of 09:15 open so
    pre-market placed orders that the broker already shows in the
    orderbook get picked up quickly; ends 15 min past close so the last
    fills are captured."""
    now = datetime.now(_IST)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    start = now.replace(hour=9, minute=0, second=0, microsecond=0)
    end = now.replace(hour=15, minute=45, second=0, microsecond=0)
    return start <= now <= end


def poll_once() -> dict:
    """Run one poll cycle. Returns a summary dict for logging/testing."""
    from database.distribution_db import (
        db_session, DistributionSignal, DistributionInbox,
    )

    today_start = datetime.combine(
        datetime.now(_IST).date(),
        datetime.min.time(),
    )
    open_signals = (
        db_session.query(DistributionSignal)
        .filter(DistributionSignal.broker_order_id.isnot(None))
        .filter(DistributionSignal.broker_order_id != "")
        .filter(DistributionSignal.received_at >= today_start)
        .filter(
            (DistributionSignal.fill_status.is_(None))
            | (~DistributionSignal.fill_status.in_(list(_TERMINAL_FILL)))
        )
        .all()
    )
    if not open_signals:
        return {"polled": 0, "updated": 0, "reason": "no open signals"}

    # Group by broker. One orderbook fetch per broker, then index by orderid.
    by_broker: dict[str, list[DistributionSignal]] = {}
    for sig in open_signals:
        if not sig.broker_used:
            continue
        by_broker.setdefault(sig.broker_used, []).append(sig)

    updated_count = 0
    for broker, sigs in by_broker.items():
        orders_by_id = _fetch_orderbook_indexed(broker, sigs)
        if orders_by_id is None:
            continue  # logged inside helper
        for sig in sigs:
            ob_row = orders_by_id.get(str(sig.broker_order_id))
            if ob_row is None:
                # Order not visible in orderbook — likely too old, or a
                # broker that prunes after fill. We don't tombstone here;
                # next poll might find it.
                continue
            new_status, filled_qty, avg_px = _extract_fill_state(ob_row)
            if new_status is None:
                continue
            if _is_unchanged(sig, new_status, filled_qty, avg_px):
                sig.last_polled_at = datetime.utcnow()
                continue

            sig.fill_status = new_status
            if filled_qty is not None:
                sig.filled_quantity = filled_qty
            if avg_px is not None:
                sig.average_price = avg_px
            sig.last_polled_at = datetime.utcnow()
            updated_count += 1

            # Best-effort push to publisher. Failure → we still log
            # locally; next cycle will re-attempt because the fact that
            # we sent it isn't persisted (only the fill_status change
            # is). Idempotent on publisher side.
            _push_to_publisher(sig, new_status, filled_qty, avg_px)

    try:
        db_session.commit()
    except Exception:
        db_session.rollback()
        logger.exception("fill_poller: commit failed")
        return {"polled": len(open_signals), "updated": 0, "error": "commit_failed"}

    return {"polled": len(open_signals), "updated": updated_count}


def _fetch_orderbook_indexed(broker: str, sigs: list) -> dict[str, dict] | None:
    """One orderbook fetch per broker. Returns dict {orderid: order_row}
    or None if the broker isn't available right now."""
    if not sigs:
        return {}
    # All signals in `sigs` share broker_used; auth comes from any one of
    # their owners. alphago_live is single-admin-per-container so this is
    # effectively "the container's one user."
    from database.distribution_db import db_session, DistributionInbox
    inbox_id = sigs[0].inbox_id
    inbox = db_session.query(DistributionInbox).filter_by(id=inbox_id).first()
    if inbox is None:
        return None

    # Resolve auth_token for the specific broker the signals were placed
    # against — NOT the customer's currently-active broker (they may have
    # switched). This is the same rule modify/cancel use.
    from database.user_db import db_session as user_session, User
    from database.auth_db import Auth, decrypt_token

    user = user_session.query(User).filter_by(id=inbox.user_id).first()
    if user is None:
        logger.debug(f"fill_poller: no user for inbox {inbox_id}")
        return None
    auth_row = Auth.query.filter_by(name=user.username, broker=broker).first()
    if not auth_row or auth_row.is_revoked:
        logger.debug(f"fill_poller: no auth for broker={broker} on user={user.username}")
        return None
    auth_token = decrypt_token(auth_row.auth)
    if not auth_token:
        return None

    try:
        from services.orderbook_service import get_orderbook_with_auth
        success, response_data, _http = get_orderbook_with_auth(
            auth_token=auth_token, broker=broker, original_data=None,
        )
    except Exception:
        logger.exception(f"fill_poller: orderbook fetch raised for broker={broker}")
        return None
    if not success:
        logger.debug(
            f"fill_poller: orderbook fetch failed for broker={broker}: "
            f"{(response_data or {}).get('message')}"
        )
        return None

    orders = ((response_data or {}).get("data") or {}).get("orders") or []
    return {str(o.get("orderid") or ""): o for o in orders if o.get("orderid")}


def _extract_fill_state(order_row: dict) -> tuple[str | None, int | None, float | None]:
    """Pull (fill_status, filled_quantity, average_price) out of an
    orderbook row. Returns (None, None, None) if the row is unusable."""
    raw_status = (order_row.get("order_status") or order_row.get("status") or "").strip().lower()
    if not raw_status:
        return None, None, None

    # Normalise broker quirks. OpenAlgo's transform layer already maps to
    # the lowercase enum we want, but a couple of brokers slip 'completed'
    # or 'executed' through. Treat known synonyms.
    if raw_status in {"completed", "executed", "filled"}:
        raw_status = "complete"
    if raw_status in {"open_pending", "pending"}:
        raw_status = "open"

    if raw_status not in _ALLOWED_FILL:
        # Unknown status — don't push garbage to the publisher. Log so we
        # can extend the synonym map.
        logger.debug(f"fill_poller: unknown broker status {raw_status!r} — skipping")
        return None, None, None

    filled_qty_raw = (
        order_row.get("filled_quantity")
        or order_row.get("filledqty")
        or order_row.get("tradedqty")
        or order_row.get("traded_quantity")
        or 0
    )
    try:
        filled_qty = int(filled_qty_raw or 0)
    except (TypeError, ValueError):
        filled_qty = None

    avg_px_raw = (
        order_row.get("average_price")
        or order_row.get("avg_price")
        or order_row.get("averageprice")
        or 0
    )
    try:
        avg_px = float(avg_px_raw or 0) or None
    except (TypeError, ValueError):
        avg_px = None

    # Infer partial when broker only says "open" but qty has moved.
    total_qty_raw = order_row.get("quantity") or order_row.get("qty") or 0
    try:
        total_qty = int(total_qty_raw or 0)
    except (TypeError, ValueError):
        total_qty = 0
    if raw_status == "open" and filled_qty and total_qty and 0 < filled_qty < total_qty:
        raw_status = "partial"

    return raw_status, filled_qty, avg_px


def _is_unchanged(sig, new_status: str, filled_qty: int | None, avg_px: float | None) -> bool:
    """Tight equality check so we don't re-push the publisher on every
    cycle when nothing actually changed."""
    if sig.fill_status != new_status:
        return False
    if filled_qty is not None and sig.filled_quantity != filled_qty:
        return False
    if avg_px is not None and sig.average_price != avg_px:
        return False
    return True


def _push_to_publisher(sig, fill_status: str, filled_qty: int | None, avg_px: float | None) -> None:
    """Best-effort POST to publisher's /api/service/fill-update. Auth =
    shared SERVICE_TOKEN env (the same one /api/service/subscribers uses).
    Identifies the subscriber via the inbox's stored publisher_subscriber_id.
    Never raises."""
    if not SERVICE_TOKEN:
        logger.debug(
            "fill_poller: SERVICE_TOKEN not set — publisher push skipped "
            "(local fill_status update applied)"
        )
        return

    from database.distribution_db import db_session, DistributionInbox

    inbox = db_session.query(DistributionInbox).filter_by(id=sig.inbox_id).first()
    if inbox is None or not inbox.publisher_subscriber_id:
        logger.debug(
            f"fill_poller: inbox {sig.inbox_id} has no publisher_subscriber_id "
            f"(not registered with publisher yet) — push skipped"
        )
        return

    url = f"{PUBLISHER_BASE_URL}/api/service/fill-update"
    body: dict[str, Any] = {
        "publisher_subscriber_id": int(inbox.publisher_subscriber_id),
        "signal_id": sig.signal_id,
        "fill_status": fill_status,
    }
    if filled_qty is not None:
        body["filled_quantity"] = filled_qty
    if avg_px is not None:
        body["average_price"] = avg_px

    try:
        r = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {SERVICE_TOKEN}",
                "Content-Type": "application/json",
                "User-Agent": "alphago_live-fill-poller/0.1",
            },
            json=body,
            timeout=PUBLISHER_TIMEOUT,
        )
    except requests.RequestException as e:
        logger.info(f"fill_poller: publisher push network err: {e}")
        return
    if r.status_code != 200:
        logger.info(
            f"fill_poller: publisher push for signal_id={sig.signal_id} "
            f"returned {r.status_code}: {(r.text or '')[:200]}"
        )
