#!/usr/bin/env python3
"""Generate a read-only Auction consistency report for one symbol."""

from __future__ import annotations

import argparse
from datetime import date
import logging
import os
from pathlib import Path
import sys
from typing import Optional, Sequence

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from logconfig import setup_logging
from services.auction_engine.consistency_reporter import (
    generate_consistency_report,
    summarize_symbol_report,
    upsert_symbol_summary,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Normal backtest defaults. Running the script with no arguments uses these.
# CLI arguments are only convenient overrides for the date and one symbol.
# ---------------------------------------------------------------------------
DEFAULT_TRADING_DATE = "2026-08-03"
DEFAULT_SYMBOL = "ABB"
DEFAULT_REPORT_ROOT = Path("reports")

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
            "Read persisted snapshots chronologically and write one Auction "
            "consistency CSV for one symbol, plus an accumulated summary.csv."
        )
    )
    parser.add_argument(
        "--symbol",
        default=DEFAULT_SYMBOL,
        help=f"Single symbol to report (default: {DEFAULT_SYMBOL})",
    )
    parser.add_argument(
        "--date",
        default=DEFAULT_TRADING_DATE,
        help=f"Trading day YYYY-MM-DD (default: {DEFAULT_TRADING_DATE})",
    )
    return parser.parse_args(argv)


def _clean_symbol(raw: str) -> str:
    symbol = str(raw or "").strip().upper()
    if not symbol:
        raise ValueError("--symbol must contain one non-empty symbol")
    if "," in symbol:
        raise ValueError("Run one symbol at a time; comma-separated symbols are not supported")
    return symbol


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _args(argv)
    setup_logging(log_file=LOG_FILE)

    trading_day = date.fromisoformat(args.date)
    symbol = _clean_symbol(args.symbol)
    output_dir = DEFAULT_REPORT_ROOT / f"auction_consistency_{trading_day:%Y%m%d}"
    detail_path = output_dir / f"{symbol}.csv"
    summary_path = output_dir / "summary.csv"

    logger.info(
        "Generating Auction consistency input | date=%s symbol=%s detail=%s",
        trading_day,
        symbol,
        detail_path,
    )
    result = generate_consistency_report(
        trading_day=trading_day,
        output_path=detail_path,
        symbols=[symbol],
        experiment_id=EXPERIMENT_ID,
        dataset_split=DATASET_SPLIT,
        code_commit=CODE_COMMIT,
        config_hash=CONFIG_HASH,
        batch_size=BATCH_SIZE,
        overwrite=OVERWRITE,
    )

    if result.rows_written == 0:
        logger.error(
            "No snapshots found | date=%s symbol=%s detail=%s",
            trading_day,
            symbol,
            detail_path,
        )
        return 1

    summary_row = summarize_symbol_report(
        detail_path=detail_path,
        trading_day=trading_day,
        symbol=symbol,
    )
    upsert_symbol_summary(summary_path=summary_path, summary_row=summary_row)

    logger.info(
        "Auction consistency input complete | symbol=%s rows=%d errors=%d "
        "detail=%s summary=%s",
        symbol,
        result.rows_written,
        result.error_rows,
        result.output_path,
        summary_path,
    )
    return 0 if result.error_rows == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
