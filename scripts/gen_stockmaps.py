#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, time as dtime, timedelta
from typing import List, Optional, Set
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import AppConfig
from configs.stockmap_config import STOCKMAP_CONFIG
from logconfig import setup_logging
from schemas.symbol import SymbolSchema
from schemas.user import UserSchema
from services.stockmap.stockmap_generator import StockMapGenerator
from utils.run_control import allow_run_today

# ---------------------------------------------------------------------------
# Hard-coded operational defaults. CLI values override these values.
# ---------------------------------------------------------------------------
DEFAULT_SYMBOLS: List[str] = ["ALL"]
DEFAULT_MAX_WORKERS = int(STOCKMAP_CONFIG.service.max_workers)
DEFAULT_ONCE = False

IST = ZoneInfo("Asia/Kolkata")
SERVICE = STOCKMAP_CONFIG.service
START_TIME = dtime.fromisoformat(SERVICE.window_start)
END_TIME = dtime.fromisoformat(SERVICE.window_end)
TICK_MINUTES = int(SERVICE.tick_minutes)
LOG_FILE = SERVICE.log_file

logger: Optional[logging.Logger] = None


def _normalise_symbols(values: List[str]) -> Optional[Set[str]]:
    out = {str(value or "").strip().upper() for value in values if str(value or "").strip()}
    if not out or out == {"ALL"}:
        return None
    if "ALL" in out:
        raise ValueError("ALL cannot be combined with named symbols")
    return out


def _safe_token(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _generate_one(token, symbol, api_key, access_token, now: datetime):
    try:
        token_int = _safe_token(token)
        if token_int is None:
            raise ValueError(f"Invalid token for {symbol}: {token!r}")
        return StockMapGenerator(
            token=token_int,
            symbol=symbol,
            api_key=api_key,
            access_token=access_token,
        ).generate_stockmap(end_date=now, persist_stockmap=True)
    except Exception:
        logging.exception("StockMap generation failed for %s", symbol)
        return None


def _selected_symbols(only_symbols: Optional[Set[str]]):
    rows = SymbolSchema.fetch_symbols(active=1, type_filter="EQ") or []
    rows = [
        row
        for row in rows
        if str(getattr(row, "type", "") or "").strip().upper() == "EQ"
    ]
    if only_symbols is not None:
        rows = [
            row
            for row in rows
            if str(getattr(row, "symbol", "") or "").strip().upper() in only_symbols
        ]
    return rows


def tick(now: datetime, *, only_symbols: Optional[Set[str]], max_workers: int) -> None:
    started = time.perf_counter()
    user = UserSchema.fetch_user(AppConfig.DATA_USER)
    if user is None:
        logger.error("No DATA_USER %s", AppConfig.DATA_USER)
        return

    selected = _selected_symbols(only_symbols)
    due = []
    for row in selected:
        token = _safe_token(getattr(row, "token", None))
        if token is None:
            logger.warning("Skipping %s: invalid token %r", row.symbol, row.token)
            continue
        due.append(row)

    logger.info(
        "StockMap tick @ %s | selected=%d due=%d cadence=%dm max_workers=%d",
        now.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S"),
        len(selected),
        len(due),
        TICK_MINUTES,
        max_workers,
    )
    if not due:
        return

    ok = 0
    fail = 0
    with ProcessPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(
                _generate_one,
                row.token,
                row.symbol,
                user.apikey,
                user.access_token,
                now,
            ): row.symbol
            for row in due
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                if future.result() is None:
                    fail += 1
                else:
                    ok += 1
            except Exception:
                fail += 1
                logger.exception("StockMap worker failed for %s", symbol)

    elapsed = time.perf_counter() - started
    logger.info("StockMap tick complete: ok=%d fail=%d elapsed=%.3fs", ok, fail, elapsed)
    if elapsed > TICK_MINUTES * 60:
        logger.warning("StockMap tick exceeded the %d-minute cadence", TICK_MINUTES)


def _sleep_to_next_tick() -> None:
    now = datetime.now(IST)
    base = now.replace(second=0, microsecond=0)
    add = TICK_MINUTES - (base.minute % TICK_MINUTES)
    if add == 0 and now.second == 0:
        add = TICK_MINUTES
    target = base + timedelta(minutes=add, seconds=int(SERVICE.cadence_delay_seconds))
    delay = (target - now).total_seconds()
    if delay > 0:
        time.sleep(delay)


def _parse_args():
    parser = argparse.ArgumentParser(description="Generate and persist 15-minute StockMaps")
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help="Override DEFAULT_SYMBOLS, for example --symbols ABB BSE or --symbols ALL",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help="Override DEFAULT_MAX_WORKERS",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        default=None,
        help="Run one tick immediately",
    )
    return parser.parse_args()


def main() -> None:
    global logger
    args = _parse_args()
    symbols = args.symbols if args.symbols is not None else DEFAULT_SYMBOLS
    only_symbols = _normalise_symbols(symbols)
    max_workers = int(args.max_workers or DEFAULT_MAX_WORKERS)
    once = DEFAULT_ONCE if args.once is None else bool(args.once)

    setup_logging(log_file=LOG_FILE)
    logger = logging.getLogger(__name__)

    if not allow_run_today(logger, "stockmap"):
        return

    logger.info(
        "=== StockMap service starting | window=%s-%s cadence=%dm symbols=%s workers=%d once=%s ===",
        START_TIME,
        END_TIME,
        "ALL" if only_symbols is None else sorted(only_symbols),
        max_workers,
        once,
    )

    if once:
        tick(datetime.now(IST), only_symbols=only_symbols, max_workers=max_workers)
        return

    while True:
        now = datetime.now(IST)
        if now.time() >= END_TIME:
            logger.info("StockMap window closed; exiting")
            return
        if now.time() < START_TIME:
            time.sleep(min(30, max(1, int((datetime.combine(now.date(), START_TIME, tzinfo=IST) - now).total_seconds()))))
            continue
        tick(now, only_symbols=only_symbols, max_workers=max_workers)
        _sleep_to_next_tick()


if __name__ == "__main__":
    main()
