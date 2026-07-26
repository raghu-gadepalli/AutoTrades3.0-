#!/usr/bin/env python3
import logging
import os
import sys
import time
import time as _time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from typing import Optional

# ensure project root is on PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from logconfig import setup_logging
from configs.service_config import SERVICE_CONFIG
from schemas.event import EventSchema  # EventStatus not used here
from services.event_router import dispatch_event

# NEW: centralized day gate (holidays/weekends/whitelist/blackout)
from utils.run_control import allow_run_today

#  CONFIG
conf = SERVICE_CONFIG.event
IST            = ZoneInfo(SERVICE_CONFIG.tz)
START_TIME     = dtime.fromisoformat(conf.window_start)
END_TIME       = dtime.fromisoformat(conf.window_end)
POLL_INTERVAL  = int(conf.retry_interval_seconds)
ERROR_BACKOFF  = int(conf.extras.error_backoff_seconds)
LOG_FILE       = conf.log_file

logger: Optional[logging.Logger] = None

# TIME HELPERS
def in_window(now: datetime) -> bool:
    # compare in IST
    t = now.astimezone(IST).time()
    return START_TIME <= t < END_TIME

def wait_for_window() -> bool:
    """
    Block until the window opens; exit cleanly if already past END.

    NOTE: day gating (weekends/holidays/whitelist/blackout) is handled by allow_run_today().
    """
    while True:
        now = datetime.now(IST)
        t = now.time()

        if t >= END_TIME:
            logger.info("Current time %s is past event window end %s; exiting", t, END_TIME)
            return False

        if t >= START_TIME:
            logger.info("Reached event window start %s; proceeding", START_TIME)
            return True

        # Sleep in small chunks so systemd stop/interrupts are responsive
        start_dt = now.replace(
            hour=START_TIME.hour,
            minute=START_TIME.minute,
            second=START_TIME.second,
            microsecond=0,
        )
        remaining = (start_dt - now).total_seconds()
        step = 30 if remaining > 120 else 5
        logger.info(
            "Window not open yet (%s). Sleeping %d sec (remaining: %.0f)...",
            START_TIME, step, remaining
        )
        time.sleep(step)

#  WORK
def process_tick() -> bool:
    """
    Try to fetch and process one due event.
    Returns True if an event was successfully processed; False otherwise.
    """
    event = EventSchema.fetch_due_event()
    if not event:
        logger.debug("No due event; sleeping")
        return False

    logger.info(
        "Picked event id=%s type=%s aggregate=%s corr=%s attempts=%s",
        event.id, event.event_type, event.aggregate_key, event.correlation_id, event.attempts,
    )

    try:
        dispatch_event(event)
        event.mark_succeeded()
        logger.info(
            "Event succeeded id=%s type=%s aggregate=%s status=%s",
            event.id, event.event_type, event.aggregate_key, getattr(event, "status", "SUCCEEDED"),
        )
        return True
    except Exception as e:
        logger.exception(
            "Handler error for event id=%s type=%s aggregate=%s: %s",
            event.id, event.event_type, event.aggregate_key, e,
        )
        try:
            event.mark_failed(str(e))
        except Exception:
            logger.exception("Failed to mark event as failed id=%s", event.id)
        return False

#  MAIN
def main():
    global logger
    setup_logging(log_file=LOG_FILE)
    logger = logging.getLogger(__name__)

    # NEW: global day gate (ALL services run OR NOTHING runs)
    if not allow_run_today(logger, "event"):
        return

    logger.info("=== Event handler starting ===")

    # Config-driven timing
    if not wait_for_window():
        return

    processed = 0
    ticks = 0
    last_loop_error = None

    try:
        # Loop only within the window (day policy already gated at startup)
        while True:
            now = datetime.now(IST)
            if not in_window(now):
                logger.info("Reached end of event window at %s; exiting", END_TIME)
                break

            try:
                did_work = process_tick()
                if did_work:
                    processed += 1
                ticks += 1
            except Exception as loop_err:
                last_loop_error = loop_err
                logger.exception(
                    "Unexpected error in event handler loop; backing off %ss",
                    ERROR_BACKOFF
                )
                time.sleep(ERROR_BACKOFF)
                continue

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Interrupted; stopping")

    logger.info(
        "=== Event handler stopped === ticks=%d processed=%d last_loop_error=%s ===",
        ticks, processed, repr(last_loop_error),
    )

if __name__ == "__main__":
    main()
