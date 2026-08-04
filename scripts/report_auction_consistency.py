#!/usr/bin/env python3
"""Generate a read-only, one-row-per-snapshot Auction consistency CSV."""

from __future__ import annotations

import argparse
from datetime import date
import logging
import os
from pathlib import Path
import sys
from typing import List, Optional, Sequence

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from logconfig import setup_logging
from services.auction_engine.consistency_reporter import generate_consistency_report

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Normal backtest defaults. Running the script with no arguments uses these.
# CLI arguments below are only convenient overrides for date, symbols/output.
# ---------------------------------------------------------------------------
DEFAULT_TRADING_DATE = "2026-08-03"
DEFAULT_SYMBOLS = ""  # Empty means every symbol present in snapshots.
DEFAULT_OUTPUT = ""  # Empty derives reports/auction_consistency_<date>.csv.

EXPERIMENT_ID = "restoration-v3-current-day"
DATASET_SPLIT = "development"
CODE_COMMIT = "restoration-trend-control-v3"
CONFIG_HASH = "backtest-default"
BATCH_SIZE = 500
OVERWRITE = True
LOG_FILE = None


def _args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read persisted snapshots chronologically and write a neutral Auction "
            "consistency input CSV. With no arguments, the hardcoded backtest "
            "defaults at the top of this script are used."
        )
    )
    parser.add_argument(
        "--date",
        default=DEFAULT_TRADING_DATE,
        help=f"Trading day YYYY-MM-DD (default: {DEFAULT_TRADING_DATE})",
    )
    parser.add_argument(
        "--symbols",
        default=DEFAULT_SYMBOLS,
        help="Optional comma-separated symbols; empty means all snapshot symbols",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="Optional output CSV path; otherwise derived from --date",
    )
    return parser.parse_args(argv)


def _symbols(raw: str) -> Optional[List[str]]:
    values = sorted({item.strip().upper() for item in raw.split(",") if item.strip()})
    return values or None


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _args(argv)
    setup_logging(log_file=LOG_FILE)

    trading_day = date.fromisoformat(args.date)
    output = Path(args.output) if args.output else Path(
        "reports"
    ) / f"auction_consistency_{trading_day:%Y%m%d}.csv"

    logger.info(
        "Generating Auction consistency input | date=%s symbols=%s output=%s",
        trading_day,
        args.symbols or "ALL",
        output,
    )
    summary = generate_consistency_report(
        trading_day=trading_day,
        output_path=output,
        symbols=_symbols(args.symbols),
        experiment_id=EXPERIMENT_ID,
        dataset_split=DATASET_SPLIT,
        code_commit=CODE_COMMIT,
        config_hash=CONFIG_HASH,
        batch_size=BATCH_SIZE,
        overwrite=OVERWRITE,
    )
    logger.info(
        "Auction consistency input complete | rows=%d errors=%d symbols=%d output=%s",
        summary.rows_written,
        summary.error_rows,
        len(summary.symbols_seen),
        summary.output_path,
    )
    return 0 if summary.error_rows == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
