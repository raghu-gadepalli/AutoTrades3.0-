#!/usr/bin/env python3
"""Historical single-day StockRank replay.

The replay uses exact same-time snapshot cadences and causal history only. It
produces one consolidated detail report, one cadence summary and one per-symbol
summary for the requested trading day.

The normal operating mode is intentionally simple:

* one hard-coded replay day;
* persistence enabled;
* one database commit and one visible progress line after every cadence;
* detail and cadence CSV files flushed after every cadence.

Command-line arguments are overrides for exceptional runs, not required inputs.
Persistence is idempotent because StockRank is uniquely keyed by symbol and
rank_time.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from configs.stock_rank_config import STOCK_RANK_CONFIG
from logconfig import setup_logging
from schemas.snapshot import SnapshotSchema
from services.selection.stock_rank import StockRankService

# Normal workflow: change this one value and run the program without arguments.
DEFAULT_REPLAY_DATE = date(2026, 8, 5)
DEFAULT_SYMBOLS: Optional[str] = None
DEFAULT_PERSIST = True
DEFAULT_REPORT_DIR = "reports"
DEFAULT_LOG_FILE = "/var/www/autotrades/tests/replay_stock_rank.log"

CADENCE_REPORT_FIELDS = [
    "status",
    "run_id",
    "trading_day",
    "rank_time",
    "requested_symbols",
    "cadence_coverage",
    "ranked_symbols",
    "missing_symbols",
    "failed_symbols",
    "priority_count",
    "secondary_count",
    "suppressed_count",
    "range_bound_count",
    "moving_count",
    "developing_count",
    "persisted",
]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay StockRank for one historical trading day")
    parser.add_argument(
        "--date",
        dest="replay_date",
        default=None,
        help=f"Override the default replay day ({DEFAULT_REPLAY_DATE.isoformat()})",
    )
    parser.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    parser.add_argument(
        "--persist",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_PERSIST,
        help="Persist each completed cadence immediately; use --no-persist for report-only runs",
    )
    parser.add_argument("--report-dir", default=DEFAULT_REPORT_DIR)
    parser.add_argument("--log-file", default=DEFAULT_LOG_FILE)
    return parser


def select_cadences(times: Sequence[datetime], cadence_minutes: int) -> list[datetime]:
    selected: list[datetime] = []
    minimum_seconds = cadence_minutes * 60
    for value in sorted(set(times)):
        if not selected or (value - selected[-1]).total_seconds() >= minimum_seconds:
            selected.append(value)
    return selected


def _report_dir(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def _flatten_cadence_summary(summary: dict) -> dict:
    row = {field: summary.get(field) for field in CADENCE_REPORT_FIELDS}
    row["missing_symbols"] = json.dumps(summary.get("missing_symbols", []), sort_keys=True)
    row["failed_symbols"] = json.dumps(summary.get("failed_symbols", {}), sort_keys=True)
    return row


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    setup_logging(log_file=args.log_file)
    logger = logging.getLogger(__name__)

    try:
        replay_day = (
            date.fromisoformat(args.replay_date)
            if args.replay_date
            else DEFAULT_REPLAY_DATE
        )
    except ValueError:
        logger.error("Invalid --date value: %s; expected YYYY-MM-DD", args.replay_date)
        return 2

    requested_symbols = (
        sorted({item.strip().upper() for item in args.symbols.split(",") if item.strip()})
        if args.symbols
        else None
    )
    if args.persist and requested_symbols is not None:
        logger.error("Focused StockRank replay cannot persist a partial cross-sectional run")
        return 2

    target = _report_dir(args.report_dir)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"replay_stock_rank_{replay_day.isoformat()}_{stamp}"
    detail_path = target / f"{stem}_rows.csv"
    cadence_path = target / f"{stem}_cadences.csv"
    symbol_path = target / f"{stem}_symbols.csv"

    symbol_stats = defaultdict(lambda: {
        "observations": 0,
        "rank_sum": 0.0,
        "score_sum": 0.0,
        "priority": 0,
        "secondary": 0,
        "suppressed": 0,
        "moving": 0,
        "developing": 0,
        "range_bound": 0,
        "neutral": 0,
        "top25": 0,
        "top50": 0,
    })
    failures: list[dict] = []
    total_cadences = 0
    total_rows = 0
    service = StockRankService()
    start_clock = dtime.fromisoformat(STOCK_RANK_CONFIG.service_window_start)
    end_clock = dtime.fromisoformat(STOCK_RANK_CONFIG.service_window_end)

    symbols = requested_symbols or SnapshotSchema.fetch_symbols_for_day(replay_day)
    if not symbols:
        logger.error("STOCK_RANK_REPLAY_ABORT | day=%s reason=NO_SNAPSHOTS", replay_day)
        return 2

    rankable = SnapshotSchema.fetch_rankable_times(
        trading_day=replay_day,
        symbols=symbols,
        minimum_coverage_ratio=STOCK_RANK_CONFIG.minimum_snapshot_coverage_ratio,
        start_time=datetime.combine(replay_day, start_clock),
        end_time=datetime.combine(replay_day, end_clock),
    )
    cadences = select_cadences(
        [row[0] for row in rankable],
        STOCK_RANK_CONFIG.cadence_minutes,
    )
    if not cadences:
        logger.error("STOCK_RANK_REPLAY_ABORT | day=%s reason=NO_RANKABLE_CADENCES", replay_day)
        return 2

    logger.info(
        "STOCK_RANK_REPLAY_START | day=%s cadence=%dm persist=%s symbols=%d rankable=%d selected=%d",
        replay_day,
        STOCK_RANK_CONFIG.cadence_minutes,
        args.persist,
        len(symbols),
        len(rankable),
        len(cadences),
    )

    detail_handle = detail_path.open("w", newline="", encoding="utf-8")
    cadence_handle = cadence_path.open("w", newline="", encoding="utf-8")
    detail_writer = None
    cadence_writer = csv.DictWriter(cadence_handle, fieldnames=CADENCE_REPORT_FIELDS)
    cadence_writer.writeheader()
    cadence_handle.flush()

    try:
        for cadence_index, rank_time in enumerate(cadences, start=1):
            try:
                result = service.run(
                    trading_day=replay_day,
                    rank_time=rank_time,
                    symbols=symbols,
                    active_only=False,
                    persist=args.persist,
                )
                summary = result["summary"]
                rows = result["rows"]

                cadence_writer.writerow(_flatten_cadence_summary(summary))
                cadence_handle.flush()

                for row in rows:
                    payload = row.report_row()
                    payload.pop("metrics_json", None)
                    if detail_writer is None:
                        detail_writer = csv.DictWriter(detail_handle, fieldnames=list(payload.keys()))
                        detail_writer.writeheader()
                    detail_writer.writerow(payload)
                    total_rows += 1

                    stats = symbol_stats[row.symbol]
                    stats["observations"] += 1
                    stats["rank_sum"] += row.rank_position
                    stats["score_sum"] += float(row.total_score)
                    stats[row.attention_tier.lower()] += 1
                    if row.classification.startswith("MOVING_"):
                        stats["moving"] += 1
                    elif row.classification.startswith("DEVELOPING_"):
                        stats["developing"] += 1
                    elif row.classification in {"RANGE_BOUND", "STALLED_GAP_RANGE"}:
                        stats["range_bound"] += 1
                    else:
                        stats["neutral"] += 1
                    stats["top25"] += int(row.rank_position <= 25)
                    stats["top50"] += int(row.rank_position <= 50)

                detail_handle.flush()
                total_cadences += 1

                logger.info(
                    "STOCK_RANK_REPLAY_CADENCE | %d/%d | rank_time=%s | ranked=%d | "
                    "priority=%d | secondary=%d | suppressed=%d | missing=%d | failed=%d | persisted=%s",
                    cadence_index,
                    len(cadences),
                    summary.get("rank_time"),
                    int(summary.get("ranked_symbols", 0)),
                    int(summary.get("priority_count", 0)),
                    int(summary.get("secondary_count", 0)),
                    int(summary.get("suppressed_count", 0)),
                    len(summary.get("missing_symbols", [])),
                    len(summary.get("failed_symbols", {})),
                    bool(summary.get("persisted", False)),
                )
            except Exception as exc:
                logger.exception(
                    "StockRank replay cadence failed | day=%s rank_time=%s",
                    replay_day,
                    rank_time,
                )
                failures.append({
                    "trading_day": replay_day.isoformat(),
                    "rank_time": rank_time.isoformat(sep=" "),
                    "error": f"{type(exc).__name__}: {exc}",
                })
                logger.error(
                    "STOCK_RANK_REPLAY_CADENCE_FAILED | %d/%d | rank_time=%s | error=%s: %s",
                    cadence_index,
                    len(cadences),
                    rank_time,
                    type(exc).__name__,
                    exc,
                )
    finally:
        detail_handle.close()
        cadence_handle.close()

    symbol_rows = []
    for symbol, stats in sorted(symbol_stats.items()):
        count = stats["observations"]
        symbol_rows.append({
            "symbol": symbol,
            "observations": count,
            "average_rank": round(stats["rank_sum"] / count, 4),
            "average_score": round(stats["score_sum"] / count, 4),
            "priority_pct": round(stats["priority"] / count * 100.0, 4),
            "secondary_pct": round(stats["secondary"] / count * 100.0, 4),
            "suppressed_pct": round(stats["suppressed"] / count * 100.0, 4),
            "moving_pct": round(stats["moving"] / count * 100.0, 4),
            "developing_pct": round(stats["developing"] / count * 100.0, 4),
            "range_bound_pct": round(stats["range_bound"] / count * 100.0, 4),
            "neutral_pct": round(stats["neutral"] / count * 100.0, 4),
            "top25_pct": round(stats["top25"] / count * 100.0, 4),
            "top50_pct": round(stats["top50"] / count * 100.0, 4),
        })
    if symbol_rows:
        with symbol_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(symbol_rows[0].keys()))
            writer.writeheader()
            writer.writerows(symbol_rows)

    summary = {
        "replay_date": replay_day.isoformat(),
        "cadences": total_cadences,
        "expected_cadences": len(cadences),
        "rows": total_rows,
        "symbols": len(symbol_rows),
        "failures": failures,
        "persisted": args.persist,
        "detail_report": str(detail_path),
        "cadence_report": str(cadence_path),
        "symbol_report": str(symbol_path),
    }
    logger.info("STOCK_RANK_REPLAY_SUMMARY | %s", json.dumps(summary, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
