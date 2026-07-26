#!/usr/bin/env python3
import logging
import os
import sys
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import List, Optional, Tuple

# ensure project root is on PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from logconfig import setup_logging
from config import AppConfig
from configs.derivatives_config import DERIVATIVES_CONFIG
from schemas.user import UserSchema
from schemas.symbol import SymbolSchema
from schemas.derivatives import DerivativesChainSchema
from services.derivatives.derivatives_generator import DerivativesGenerator

# centralized day gate (holidays/weekends/whitelist/blackout)
from utils.run_control import allow_run_today

# ----------------------------
# CONFIG
# ----------------------------
DERIV_SERVICE = DERIVATIVES_CONFIG.service
IST: ZoneInfo = ZoneInfo("Asia/Kolkata")

START_TIME: dtime = dtime.fromisoformat(DERIV_SERVICE.window_start)
END_TIME: dtime = dtime.fromisoformat(DERIV_SERVICE.window_end)
RETRY_INTERVAL: int = DERIV_SERVICE.retry_interval_seconds
LOG_FILE: str = DERIV_SERVICE.log_file

MIN_REFRESH_SEC: int = DERIV_SERVICE.min_refresh_seconds

TICK_MINUTES: int = DERIV_SERVICE.tick_minutes
LEAD_MINUTES: int = DERIV_SERVICE.lead_minutes
MAX_WORKERS: int = DERIV_SERVICE.max_workers

logger: Optional[logging.Logger] = None

# ----------------------------
# TIME HELPERS
# ----------------------------
def in_window(now: datetime) -> bool:
    t = now.astimezone(IST).time()
    return START_TIME <= t < END_TIME


def wait_for_window() -> bool:
    """
    Block until the window opens; exit cleanly if already past END.
    Day gating handled by allow_run_today().
    """
    while True:
        now = datetime.now(IST)
        t = now.time()

        if t >= END_TIME:
            logger.info("Current time %s is past derivatives window end %s; exiting", t, END_TIME)
            return False

        if t >= START_TIME:
            logger.info("Reached derivatives window start %s; proceeding", START_TIME)
            return True

        start_dt = now.replace(
            hour=START_TIME.hour, minute=START_TIME.minute,
            second=START_TIME.second, microsecond=0
        )
        remaining = (start_dt - now).total_seconds()
        step = 30 if remaining > 120 else 5
        logger.info("Window not open yet (%s). Sleeping %d sec (remaining: %.0f)...",
                    START_TIME, step, remaining)
        time.sleep(step)


def _next_aligned_tick(now: datetime, tick_minutes: int, lead_minutes: int) -> datetime:
    """
    Next aligned tick time for a schedule that runs every `tick_minutes` on clock boundaries,
    shifted earlier by `lead_minutes`.
      tick=3, lead=1 => 09:17, 09:20, 09:23, ...
    """
    if tick_minutes <= 0:
        tick_minutes = 3
    if lead_minutes < 0:
        lead_minutes = 0

    now_ist = now.astimezone(IST)
    base = now_ist.replace(second=0, microsecond=0)

    m = base.minute
    add = tick_minutes - (m % tick_minutes)
    if add == 0 and now_ist.second == 0:
        add = tick_minutes

    next_base = base + timedelta(minutes=add)
    scheduled = next_base - timedelta(minutes=lead_minutes)

    if scheduled <= now_ist:
        scheduled = scheduled + timedelta(minutes=tick_minutes)

    return scheduled


def sleep_to_next_scheduled_tick():
    now = datetime.now(IST)
    nxt = _next_aligned_tick(now, TICK_MINUTES, LEAD_MINUTES)
    remaining = (nxt - now).total_seconds()
    if remaining > 0:
        time.sleep(remaining)


# ----------------------------
# DB THROTTLE + RESULT SHAPE
# ----------------------------
def _should_generate(equity_ref: str, now: datetime) -> bool:
    """
    Throttle chain generation if a fresh record exists for today.

    IMPORTANT: derivatives snapshot_time is stored IST-naive in DB.
    Query with IST-naive `asof` to avoid tz-aware/naive mismatch.
    """
    asof = now.astimezone(IST).replace(tzinfo=None, second=0, microsecond=0)

    try:
        chain = DerivativesChainSchema.fetch_latest_today_for_symbol_before_time(equity_ref, asof)
    except Exception:
        logger.exception("Error fetching latest derivatives chain for %s; forcing generation", equity_ref)
        return True

    if chain is None:
        return True

    chain_time = getattr(chain, "snapshot_time", None)
    if not chain_time:
        logger.warning("DerivativesChainSchema for %s has no snapshot_time; forcing generation", equity_ref)
        return True

    # interpret naive as IST for delta
    if chain_time.tzinfo is None:
        chain_time = chain_time.replace(tzinfo=IST)

    delta = (now.astimezone(IST) - chain_time.astimezone(IST)).total_seconds()
    return delta >= MIN_REFRESH_SEC


def _is_skipped_result(result: Optional[dict]) -> bool:
    """
    v2 convention:
      DerivativesGenerator.generate(...) returns {"raw": None, "derived": None} on early return/skip.
    """
    if not isinstance(result, dict):
        return True
    return result["raw"] is None and result["derived"] is None


# ----------------------------
# WORKER TASK (parallel)
# ----------------------------
def _generate_one(equity_ref: str, api_key: str, access_token: str) -> Tuple[str, bool, Optional[str]]:
    """
    Child process: generate derivatives for one equity.
    Returns: (equity_ref, persisted, note)
      persisted=True  => persisted
      persisted=False => skipped or failed (note tells why)
    Never raises.
    """
    try:
        gen = DerivativesGenerator(api_key=api_key, access_token=access_token)

        # standardize call to generate
        if hasattr(gen, "generate"):
            res = gen.generate(equity_ref)
        elif hasattr(gen, "generate_for_equity"):
            res = gen.generate_for_equity(equity_ref)
        elif hasattr(gen, "run_for_equity"):
            res = gen.run_for_equity(equity_ref)
        else:
            return (equity_ref, False, "no_generate_method")

        if _is_skipped_result(res):
            return (equity_ref, False, "skipped")

        return (equity_ref, True, None)

    except Exception as e:
        logging.exception("Derivatives error in worker for %s", equity_ref)
        return (equity_ref, False, f"error:{type(e).__name__}")


# ----------------------------
# TICK
# ----------------------------
def tick(now: datetime):
    t0 = time.perf_counter()

    # 1) credentials
    user = UserSchema.fetch_user(AppConfig.DATA_USER)
    if not user:
        logger.error("No DATA_USER %s", AppConfig.DATA_USER)
        return

    # 2) EQ universe (unique equity refs)
    syms: List[SymbolSchema] = SymbolSchema.fetch_symbols(active=1, type_filter="EQ")
    if not syms:
        logger.warning("No active EQ symbols to process")
        return

    equity_refs = sorted({s.symbol for s in syms if getattr(s, "symbol", None)})
    if not equity_refs:
        logger.warning("No equity refs resolved from active EQ symbols")
        return

    # 3) prefilter due list using throttle
    due = [eq for eq in equity_refs if _should_generate(eq, now)]

    logger.info(
        "Derivatives tick @ %s | EQ_total=%d due=%d tick_minutes=%d lead_minutes=%d max_workers=%d min_refresh=%ds",
        now.astimezone(IST).strftime("%H:%M:%S"),
        len(equity_refs),
        len(due),
        TICK_MINUTES,
        LEAD_MINUTES,
        MAX_WORKERS,
        MIN_REFRESH_SEC,
    )

    if not due:
        return

    ok = 0
    skipped = 0
    failed = 0

    # 4) parallel generation
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as exe:
        futures = {
            exe.submit(_generate_one, eq, user.apikey, user.access_token): eq
            for eq in due
        }

        for fut in as_completed(futures):
            eq = futures[fut]
            try:
                equity_ref, persisted, note = fut.result()
                if persisted:
                    ok += 1
                else:
                    if note == "skipped":
                        skipped += 1
                    else:
                        failed += 1
                        logger.error("[%s] derivatives failed (%s)", equity_ref, note)
            except Exception:
                failed += 1
                logger.exception("[%s] derivatives failed in main collector", eq)

    elapsed = time.perf_counter() - t0
    logger.info(
        "Derivatives tick done: persisted=%d skipped=%d failed=%d elapsed=%.3fs (interval_hint=%ds)",
        ok, skipped, failed, elapsed, RETRY_INTERVAL
    )

    if elapsed > (TICK_MINUTES * 60):
        logger.warning(
            "Derivatives tick exceeded %d minutes (elapsed=%.3fs). You will miss cadence.",
            TICK_MINUTES, elapsed
        )
    elif elapsed > 60.0:
        logger.warning("Derivatives tick > 60s (elapsed=%.3fs). Consider raising max_workers.", elapsed)


# ----------------------------
# MAIN
# ----------------------------
def main():
    global logger
    setup_logging(log_file=LOG_FILE)
    logger = logging.getLogger(__name__)

    # NEW: global day gate via config (whitelist overrides; else holidays/weekends/blackout skip)
    if not allow_run_today(logger, "derivatives"):
        return

    logger.info(
        "=== Derivatives Service starting (window=%s-%s; cadence=%dm; lead=%dm; max_workers=%d) ===",
        START_TIME, END_TIME, TICK_MINUTES, LEAD_MINUTES, MAX_WORKERS
    )

    if not wait_for_window():
        return

    # Align FIRST run to scheduled tick (e.g., 09:17 for tick=3, lead=1)
    sleep_to_next_scheduled_tick()

    try:
        while True:
            now = datetime.now(IST)
            if not in_window(now):
                logger.info("Reached derivatives window end at %s; exiting", END_TIME)
                break

            tick(now)
            sleep_to_next_scheduled_tick()

    except KeyboardInterrupt:
        logger.info("Interrupted; stopping")
    finally:
        logger.info("=== Derivatives Service stopped ===")


if __name__ == "__main__":
    main()