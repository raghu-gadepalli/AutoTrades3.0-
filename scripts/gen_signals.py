#!/usr/bin/env python3
import logging
import os
import sys
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from itertools import groupby
from typing import List, Optional

# allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from logconfig import setup_logging
from configs.signal_config import SIGNAL_CONFIG
from schemas.snapshot import SnapshotSchema
from services.signals.signal_generator import SignalGenerator

# centralized day gate (holidays/weekends/whitelist/blackout)
from utils.run_control import allow_run_today

#  CONFIG
conf = SIGNAL_CONFIG.service
IST = ZoneInfo("Asia/Kolkata")

START_TIME      = dtime.fromisoformat(conf.window_start)
END_TIME        = dtime.fromisoformat(conf.window_end)
RETRY_INTERVAL  = int(conf.retry_interval_seconds)
LOG_FILE        = conf.log_file

logger: Optional[logging.Logger] = None

#  TIME HELPERS
def in_window(now: datetime) -> bool:
    t = now.astimezone(IST).time()
    return START_TIME <= t < END_TIME

def wait_for_window() -> bool:
    """Block until window opens; exit if already past END."""
    while True:
        now = datetime.now(IST)
        t = now.time()

        if t >= END_TIME:
            logger.info("Current time %s is past signal window end %s; exiting", t, END_TIME)
            return False

        if t >= START_TIME:
            logger.info("Reached signal window start %s; proceeding", START_TIME)
            return True

        # keep this as DEBUG (like gen_snapshots)
        start_dt = now.replace(
            hour=START_TIME.hour,
            minute=START_TIME.minute,
            second=START_TIME.second,
            microsecond=0,
        )
        remaining = (start_dt - now).total_seconds()
        step = 30 if remaining > 120 else 5
        logger.debug(
            "Window not open yet (%s). Sleeping %d sec (remaining: %.0f)...",
            START_TIME, step, remaining
        )
        time.sleep(step)

#  TICK
def tick(now: datetime) -> Optional[datetime]:
    """Process unprocessed snapshots in chronological groups.

    ``now`` controls service logging/window behavior only. Auction has already
    completed setup evaluation inside each validated snapshot.
    """
    try:
        snaps: List[SnapshotSchema] = SnapshotSchema.fetch_unprocessed()
    except Exception:
        logger.exception("Failed to fetch unprocessed snapshots")
        return None

    if not snaps:
        logger.debug("No unprocessed snapshots at %s", now.astimezone(IST))
        return None

    logger.info("Processing %d unprocessed snapshots", len(snaps))
    last_snapshot_time: Optional[datetime] = None
    for snapshot_time, grouped in groupby(snaps, key=lambda snap: snap.snapshot_time):
        group = list(grouped)
        if not isinstance(snapshot_time, datetime):
            raise ValueError("Signal service encountered snapshot without snapshot_time")
        last_snapshot_time = snapshot_time
        for snap in group:
            ok = False
            try:
                action = SignalGenerator(snap).generate()
                ok = True

                if action:
                    logger.debug(
                        "Signal action for %s @ %s | action=%s",
                        snap.symbol,
                        snap.snapshot_time,
                        action,
                    )
            except Exception:
                logger.exception("Error processing snapshot %s @ %s", snap.symbol, snap.snapshot_time)

            if ok:
                try:
                    SnapshotSchema.mark_processed(snap.symbol, snap.snapshot_time)
                except Exception:
                    logger.exception("Failed to mark processed for %s @ %s", snap.symbol, snap.snapshot_time)

    return last_snapshot_time

def sleep_to_next_interval():
    time.sleep(RETRY_INTERVAL)

#  MAIN
def main():
    global logger
    setup_logging(log_file=LOG_FILE)
    logger = logging.getLogger(__name__)

    # global day gate (ALL services run OR NOTHING runs)
    if not allow_run_today(logger, "signals"):
        return

    logger.info("=== Signal Service starting (retry_interval=%ds) ===", RETRY_INTERVAL)

    if not wait_for_window():
        return

    try:
        while True:
            now = datetime.now(IST)

            # Only window check here (day policy already gated by allow_run_today)
            if not in_window(now):
                logger.info("Reached signal window end at %s; exiting", END_TIME)
                break

            tick(now)
            sleep_to_next_interval()

    except KeyboardInterrupt:
        logger.info("Interrupted; stopping")
    finally:
        logger.info("=== Signal Service stopped ===")

if __name__ == "__main__":
    main()