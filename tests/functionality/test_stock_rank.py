#!/usr/bin/env python3
"""Manual one-cadence StockRank diagnostic.

This program may persist the selected cadence and always exports a detailed CSV.
It is intentionally outside the production service and is not collected by the
unit-test suite.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from configs.stock_rank_config import STOCK_RANK_CONFIG
from logconfig import setup_logging
from services.selection.stock_rank import StockRankService
from utils.datetime_utils import IST, business_now

DEFAULT_TRADING_DAY: Optional[str] = None
DEFAULT_AS_OF: Optional[str] = None
DEFAULT_SYMBOLS: Optional[str] = None
DEFAULT_ALL_ENABLED = False
DEFAULT_PERSIST = False
DEFAULT_REPORT_DIR = "reports"
DEFAULT_LOG_FILE = "/var/www/autotrades/tests/stock_rank_functionality.log"


def _parse_day(value: Optional[str]) -> Optional[date]:
    return date.fromisoformat(value) if value else None


def _parse_as_of(value: Optional[str], trading_day: date) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    parsed = (
        datetime.fromisoformat(text)
        if "T" in text or " " in text
        else datetime.combine(trading_day, dtime.fromisoformat(text))
    )
    return parsed.replace(tzinfo=IST) if parsed.tzinfo is None else parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one detailed StockRank diagnostic")
    parser.add_argument("--trading-day", default=DEFAULT_TRADING_DAY)
    parser.add_argument("--as-of", default=DEFAULT_AS_OF)
    parser.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    parser.add_argument(
        "--all-enabled",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_ALL_ENABLED,
    )
    parser.add_argument(
        "--persist",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_PERSIST,
    )
    parser.add_argument("--report-dir", default=DEFAULT_REPORT_DIR)
    parser.add_argument("--log-file", default=DEFAULT_LOG_FILE)
    return parser


def _export(rows, report_dir: str) -> Path:
    target = Path(report_dir)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    target.mkdir(parents=True, exist_ok=True)
    rank_time = rows[0].rank_time
    path = target / f"stock_rank_{rank_time.strftime('%Y%m%d_%H%M%S')}.csv"
    report_rows = []
    for row in rows:
        payload = row.report_row()
        payload["metrics_json"] = json.dumps(
            payload["metrics_json"],
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        report_rows.append(payload)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(report_rows[0].keys()))
        writer.writeheader()
        writer.writerows(report_rows)
    return path


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    setup_logging(log_file=args.log_file)
    logger = logging.getLogger(__name__)
    trading_day = _parse_day(args.trading_day) or business_now().date()
    through_time = _parse_as_of(args.as_of, trading_day)
    symbols = (
        [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
        if args.symbols
        else None
    )
    if args.persist and (symbols is not None or args.all_enabled):
        logger.error("Persistence is allowed only for the complete active universe")
        return 2
    logger.info(
        "STOCK_RANK_FUNCTIONALITY_START | day=%s as_of=%s universe=%s persist=%s symbols=%s",
        trading_day,
        through_time,
        "ALL_ENABLED" if args.all_enabled else "ACTIVE",
        args.persist,
        symbols or "ALL",
    )
    try:
        result = StockRankService().run(
            trading_day=trading_day,
            through_time=through_time,
            symbols=symbols,
            active_only=not args.all_enabled,
            persist=args.persist,
        )
        report = _export(result["rows"], args.report_dir)
        summary = {**result["summary"], "report": str(report)}
        logger.info("STOCK_RANK_FUNCTIONALITY_SUMMARY | %s", json.dumps(summary, default=str, sort_keys=True))
        for row in result["rows"][: STOCK_RANK_CONFIG.top_log_count]:
            logger.info(
                "STOCK_RANK_TOP | rank=%d symbol=%s score=%.2f tier=%s class=%s dir=%s",
                row.rank_position,
                row.symbol,
                row.total_score,
                row.attention_tier,
                row.classification,
                row.direction,
            )
        return 0
    except Exception:
        logger.exception("StockRank functionality run failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
