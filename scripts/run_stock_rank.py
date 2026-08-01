#!/usr/bin/env python3
"""Production six-minute StockRank service.

The runner waits for the market window, ranks one common completed snapshot
cadence across the active EQ universe, persists the rows and logs concise
summaries. It never writes CSV reports and never changes enabled/active or any
signal/trade state.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, time as dtime, timedelta
from typing import Optional

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from configs.stock_rank_config import STOCK_RANK_CONFIG
from logconfig import setup_logging
from schemas.stock_rank import StockRankSchema
from services.selection.stock_rank import StockRankService
from utils.datetime_utils import IST
from utils.run_control import allow_run_today

CONF = STOCK_RANK_CONFIG
START_TIME = dtime.fromisoformat(CONF.service_window_start)
END_TIME = dtime.fromisoformat(CONF.service_window_end)

logger: Optional[logging.Logger] = None


def _normalise_time(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(IST)
    return value.replace(tzinfo=None)


def in_window(now: datetime) -> bool:
    current = now.astimezone(IST).time()
    return START_TIME <= current < END_TIME


def wait_for_window() -> bool:
    while True:
        now = datetime.now(IST)
        current = now.time()
        if current >= END_TIME:
            logger.info("Current time %s is past StockRank window end %s; exiting", current, END_TIME)
            return False
        if current >= START_TIME:
            logger.info("Reached StockRank window start %s; proceeding", START_TIME)
            return True
        start_dt = now.replace(
            hour=START_TIME.hour,
            minute=START_TIME.minute,
            second=START_TIME.second,
            microsecond=0,
        )
        remaining = (start_dt - now).total_seconds()
        time.sleep(30 if remaining > 120 else 5)


def _log_completed(result: dict) -> None:
    summary = result["summary"]
    rows = result["rows"]
    logger.info(
        "STOCK_RANK_CYCLE | rank_time=%s requested=%d coverage=%d ranked=%d "
        "priority=%d secondary=%d suppressed=%d moving=%d developing=%d "
        "range_bound=%d missing=%d failed=%d persisted=%s",
        summary["rank_time"],
        summary["requested_symbols"],
        summary["cadence_coverage"],
        summary["ranked_symbols"],
        summary["priority_count"],
        summary["secondary_count"],
        summary["suppressed_count"],
        summary["moving_count"],
        summary["developing_count"],
        summary["range_bound_count"],
        len(summary["missing_symbols"]),
        len(summary["failed_symbols"]),
        summary["persisted"],
    )
    for row in rows[: CONF.top_log_count]:
        logger.info(
            "STOCK_RANK_TOP | rank=%d symbol=%s score=%.2f tier=%s class=%s "
            "dir=%s move15=%s move30=%s efficiency=%s range_penalty=%.2f stall_penalty=%.2f",
            row.rank_position,
            row.symbol,
            row.total_score,
            row.attention_tier,
            row.classification,
            row.direction,
            "NA" if row.move_15m_pct is None else f"{float(row.move_15m_pct):.4f}",
            "NA" if row.move_30m_pct is None else f"{float(row.move_30m_pct):.4f}",
            "NA" if row.recent_efficiency is None else f"{float(row.recent_efficiency):.4f}",
            row.range_penalty,
            row.stall_penalty,
        )
    if summary["missing_symbols"]:
        logger.warning("STOCK_RANK_MISSING | symbols=%s", ",".join(summary["missing_symbols"]))
    if summary["failed_symbols"]:
        logger.warning("STOCK_RANK_FAILED | failures=%s", summary["failed_symbols"])


def run_cycle(
    *,
    now: datetime,
    service: StockRankService,
    last_rank_time: Optional[datetime],
) -> Optional[datetime]:
    through_time = now - timedelta(seconds=CONF.snapshot_completion_lag_seconds)
    result = service.run(
        trading_day=now.astimezone(IST).date(),
        through_time=through_time,
        symbols=None,
        active_only=True,
        persist=True,
        after_rank_time=last_rank_time,
        minimum_interval_minutes=CONF.cadence_minutes,
        age_reference_time=now,
        maximum_rank_age_minutes=CONF.maximum_snapshot_age_minutes,
    )
    summary = result["summary"]
    status = summary["status"]
    if status == "NO_NEW_CADENCE":
        logger.debug("StockRank has no new six-minute cadence after %s", last_rank_time)
        return last_rank_time
    if status == "STALE_CADENCE":
        logger.warning(
            "STOCK_RANK_STALE | rank_time=%s age_minutes=%s",
            summary["rank_time"],
            summary["rank_age_minutes"],
        )
        return last_rank_time
    _log_completed(result)
    return datetime.fromisoformat(summary["rank_time"])


def main() -> None:
    global logger
    setup_logging(log_file=CONF.log_file)
    logger = logging.getLogger(__name__)

    if not allow_run_today(logger, "stock_rank"):
        return

    logger.info(
        "=== StockRank Service starting | window=%s-%s cadence=%dm poll=%ds lag=%ds universe=ACTIVE ===",
        START_TIME,
        END_TIME,
        CONF.cadence_minutes,
        CONF.poll_interval_seconds,
        CONF.snapshot_completion_lag_seconds,
    )

    if not wait_for_window():
        return

    service = StockRankService()
    today = datetime.now(IST).date()
    last_rank_time = _normalise_time(StockRankSchema.fetch_latest_rank_time(today))
    if last_rank_time is not None:
        logger.info("StockRank resuming after persisted cadence %s", last_rank_time)

    try:
        while True:
            now = datetime.now(IST)
            if not in_window(now):
                logger.info("Reached StockRank window end %s; exiting", END_TIME)
                break
            try:
                last_rank_time = run_cycle(
                    now=now,
                    service=service,
                    last_rank_time=last_rank_time,
                )
            except ValueError as exc:
                # Usually no sufficiently complete snapshot cadence yet.
                logger.warning("STOCK_RANK_WAIT | %s", exc)
            except Exception:
                logger.exception("StockRank cycle failed; continuing after backoff")
                time.sleep(CONF.error_backoff_seconds)
                continue
            time.sleep(CONF.poll_interval_seconds)
    except KeyboardInterrupt:
        logger.info("StockRank interrupted; stopping")
    finally:
        logger.info("=== StockRank Service stopped ===")


if __name__ == "__main__":
    main()
