#!/usr/bin/env python3
"""
scripts/run_broker_reconcile.py

Broker Reconcile runner (V0: funds sync)

Behavior:
- Uses BROKER_CONFIG.reconcile
- 1 thread per user (bounded by max_workers)
- Waits for all user tasks to finish BEFORE sleeping
- No align-to-tick
- Service owns refresh; routes remain DB readers
"""

import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dtime
from typing import Optional, Tuple

# allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from logconfig import setup_logging
from configs.broker_config import BROKER_CONFIG
from services.broker.broker_reconcile import BrokerReconcileService
from services.broker.reconcile_helper import get_reconcile_users
from utils.run_control import allow_run_today
from utils.datetime_utils import IST

# ----------------------------
# CONFIG
# ----------------------------
conf = BROKER_CONFIG.reconcile

START_TIME = dtime.fromisoformat(conf.window_start)
END_TIME = dtime.fromisoformat(conf.window_end)
RETRY_INTERVAL = int(conf.retry_interval_seconds)
LOG_FILE = conf.log_file

MAX_WORKERS = int(conf.extras.max_workers)
LIMIT_USERS = int(conf.extras.limit_users)

logger: Optional[logging.Logger] = None


# ----------------------------
# TIME HELPERS
# ----------------------------
def in_window(now: datetime) -> bool:
    t = now.astimezone(IST).time()
    return START_TIME <= t < END_TIME


def wait_for_window() -> bool:
    """Block until window opens; exit if already past END."""
    while True:
        now = datetime.now(IST)
        t = now.time()

        if t >= END_TIME:
            logger.info("Current time %s is past broker_reconcile window end %s; exiting", t, END_TIME)
            return False

        if t >= START_TIME:
            logger.info("Reached broker_reconcile window start %s; proceeding", START_TIME)
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
            "Broker reconcile window not open yet (%s). Sleeping %d sec (remaining: %.0f)...",
            START_TIME, step, remaining
        )
        time.sleep(step)


# ----------------------------
# PER-USER TASK
# ----------------------------
def _run_user(user) -> Tuple[str, bool]:
    """
    Returns:
      (userid, synced_ok)
    """
    svc = BrokerReconcileService()
    ok = svc.reconcile_user_once(user=user)
    return str(getattr(user, "userid", "") or ""), bool(ok)


# ----------------------------
# TICK (multi-thread)
# ----------------------------
def tick(now: datetime):
    t0 = time.perf_counter()

    users = get_reconcile_users() or []
    if LIMIT_USERS > 0:
        users = users[:LIMIT_USERS]

    if not users:
        logger.info(
            "Broker reconcile tick @ %s | no eligible users",
            now.astimezone(IST).strftime("%H:%M:%S"),
        )
        return

    logger.info(
        "Broker reconcile tick @ %s | users=%d max_workers=%d limit_users=%d",
        now.astimezone(IST).strftime("%H:%M:%S"),
        len(users),
        MAX_WORKERS,
        LIMIT_USERS,
    )

    synced = 0
    failed = 0

    maxw = min(MAX_WORKERS, max(1, len(users)))

    # One thread per user (bounded). Wait for all users before sleeping.
    with ThreadPoolExecutor(max_workers=maxw) as pool:
        futs = {pool.submit(_run_user, u): str(getattr(u, "userid", "") or "") for u in users}

        for fut in as_completed(futs):
            userid = futs[fut]
            try:
                uid, ok = fut.result()
                if ok:
                    synced += 1
                    logger.debug("Broker reconcile user done userid=%s synced=%s", uid, ok)
                else:
                    failed += 1
                    logger.warning("Broker reconcile user failed userid=%s", uid)
            except Exception:
                failed += 1
                logger.exception("Broker reconcile user tick failed userid=%s", userid)

    elapsed = time.perf_counter() - t0
    logger.info(
        "Broker reconcile tick done: synced=%d failed_users=%d elapsed=%.3fs",
        synced, failed, elapsed
    )


# ----------------------------
# MAIN
# ----------------------------
def main():
    global logger
    setup_logging(log_file=LOG_FILE)
    logger = logging.getLogger(__name__)

    if not allow_run_today(logger, "broker_reconcile"):
        return

    logger.info(
        "=== Broker Reconcile Service starting "
        "(window=%s-%s; interval=%ss; max_workers=%d; limit_users=%d) ===",
        START_TIME, END_TIME, RETRY_INTERVAL, MAX_WORKERS, LIMIT_USERS
    )

    if not wait_for_window():
        return

    try:
        while True:
            now = datetime.now(IST)

            if not in_window(now):
                logger.info("Reached broker_reconcile window end at %s; exiting", END_TIME)
                break

            tick(now)

            # Sleep only AFTER all user threads finish
            time.sleep(RETRY_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Interrupted; stopping")
    finally:
        logger.info("=== Broker Reconcile Service stopped ===")


if __name__ == "__main__":
    main()