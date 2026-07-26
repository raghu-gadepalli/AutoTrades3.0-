#!/usr/bin/env python3
"""
scripts/run_trade_backfill.py

OMS-backed reconciliation runner for app-managed REAL trades.

Responsibilities
----------------
- read OMS truth already ingested by broker_reconcile
- reconcile user_trades from oms_orders / oms_positions
- keep executor focused on action/polling while this service handles backfill

Notes
-----
- Writes only to user_trades
- Does not write OMS tables
- Supports dry_run via BROKER_CONFIG.trade_backfill.extras.dry_run
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

# Project root on PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from configs.service_config import SERVICE_CONFIG
from configs.broker_config import BROKER_CONFIG
from logconfig import setup_logging
from services.broker.trade_backfill import TradeBackfillService
from utils.run_control import allow_run_today

logger = logging.getLogger(__name__)

TZ = SERVICE_CONFIG.tz
IST = ZoneInfo(TZ)

CFG = BROKER_CONFIG.trade_backfill
LOG_FILE = CFG.log_file
START_TIME = dtime.fromisoformat(CFG.window_start)
END_TIME = dtime.fromisoformat(CFG.window_end)
RETRY_INTERVAL_SECONDS = int(CFG.retry_interval_seconds)

LIMIT_USERS = int(CFG.extras.limit_users)
LIMIT_TRADES_PER_USER = int(CFG.extras.limit_trades_per_user)
DRY_RUN = bool(CFG.extras.dry_run)


def _sleep_to_next_tick(seconds: int) -> None:
    try:
        time.sleep(max(int(seconds), 1))
    except Exception:
        time.sleep(1)


def _in_window(now: datetime) -> bool:
    t = now.astimezone(IST).time()
    return START_TIME <= t < END_TIME


def _wait_for_window() -> bool:
    """Block until window opens; exit if already past END."""
    while True:
        now = datetime.now(IST)
        t = now.time()

        if t >= END_TIME:
            logger.info(
                "Current time %s is past trade_backfill window end %s; exiting",
                t,
                END_TIME,
            )
            return False

        if t >= START_TIME:
            logger.info("Reached trade_backfill window start %s; proceeding", START_TIME)
            return True

        start_dt = now.replace(
            hour=START_TIME.hour,
            minute=START_TIME.minute,
            second=START_TIME.second,
            microsecond=0,
        )
        remaining = (start_dt - now).total_seconds()
        step = 30 if remaining > 120 else 5
        logger.info(
            "TradeBackfill window not open yet (%s). Sleeping %d sec (remaining: %.0f)...",
            START_TIME,
            step,
            remaining,
        )
        time.sleep(step)


def main() -> None:
    setup_logging(log_file=LOG_FILE)
    global logger
    logger = logging.getLogger(__name__)

    if not allow_run_today(logger, "trade_backfill"):
        return

    logger.info(
        "=== trade_backfill starting | window=%s-%s | tz=%s | interval=%ss | "
        "limit_users=%s | limit_trades_per_user=%s | dry_run=%s ===",
        START_TIME,
        END_TIME,
        TZ,
        RETRY_INTERVAL_SECONDS,
        LIMIT_USERS,
        LIMIT_TRADES_PER_USER,
        DRY_RUN,
    )

    if not _wait_for_window():
        return

    svc = TradeBackfillService(dry_run=DRY_RUN)

    try:
        while True:
            now = datetime.now(IST)

            if not _in_window(now):
                logger.info("Reached trade_backfill window end at %s; exiting", END_TIME)
                break

            try:
                logger.info("TradeBackfill tick @ %s", now.strftime("%H:%M:%S"))

                stats = svc.run_once(
                    limit_users=LIMIT_USERS,
                    limit_trades_per_user=LIMIT_TRADES_PER_USER,
                )

                logger.info(
                    "TradeBackfill tick done | users_found=%s users_processed=%s users_succeeded=%s "
                    "users_failed=%s trades_seen=%s trades_updated=%s errors=%s dry_run=%s",
                    stats["users_found"],
                    stats["users_processed"],
                    stats["users_succeeded"],
                    stats["users_failed"],
                    stats["trades_seen"],
                    stats["trades_updated"],
                    stats["errors"],
                    DRY_RUN,
                )

            except Exception:
                logger.exception("TradeBackfill fatal runner error")

            _sleep_to_next_tick(RETRY_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        logger.info("TradeBackfill interrupted; exiting")
    finally:
        logger.info("=== TradeBackfill Service stopped ===")


if __name__ == "__main__":
    main()
