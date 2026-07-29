#!/usr/bin/env python3
"""Simple stockscan test runner.

Edit the TEST SETTINGS below and run:

    python tests/test_stockscan.py

By default this is a dry run and does not update symbols.active. Set
APPLY_UPDATES = True only when you intentionally want to apply the selected
daily active basket to the database.
"""

from __future__ import annotations

import csv
import logging
import os
import sys
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Dict, List, Optional

# allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from configs.scanner_config import SCANNER_CONFIG
from logconfig import setup_logging
from services.selection.stockscan import StockScanner
from utils.datetime_utils import IST, business_now


# =============================================================================
# TEST SETTINGS
# =============================================================================
# None = today in IST. Or set explicitly, for example: "2026-07-10".
TEST_DATE: Optional[str] = None

# Stockscan live default is SCANNER_CONFIG.scan.run_time, usually 09:16:00.
# If Kite returns the first candle a little late, use "09:16:10".
TEST_TIME: str = SCANNER_CONFIG.scan.run_time

# False = dry run only. True = write selected symbols to symbols.active.
APPLY_UPDATES: bool = False

# Number of top ranked candidates to print to console/log.
PRINT_TOP_N: int = 100

# Empty string disables CSV output. Example: "/tmp/stockscan_candidates.csv".
CSV_PATH: str = "stockscan_candidates.csv"

LOG_FILE: str = "test_stockscan.log"


# =============================================================================
# Helpers
# =============================================================================
def _test_as_of() -> datetime:
    scan_date = date.fromisoformat(TEST_DATE) if TEST_DATE else business_now().date()
    scan_time = dtime.fromisoformat(TEST_TIME)
    return datetime.combine(scan_date, scan_time, IST)


def _write_csv(path: str, rows: List[Dict]) -> None:
    if not path:
        return

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "rank",
        "selected",
        "is_whitelisted",
        "selection_reason",
        "symbol",
        "score",
        "direction",
        "candle_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "prev_close",
        "gap_pct",
        "day_move_pct",
        "candle_move_pct",
        "candle_range_pct",
        "turnover_lakh",
        "gap_score",
        "day_move_score",
        "candle_move_score",
        "candle_range_score",
        "turnover_score",
    ]

    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _print_summary(result: Dict, candidates: List[Dict]) -> None:
    stats = result.get("stats") or {}

    print("\nStockscan test summary")
    print("-" * 80)
    print(f"as_of              : {result.get('as_of')}")
    print(f"apply_updates      : {result.get('apply_updates')}")
    print(f"active_limit       : {result.get('daily_active_limit')}")
    print(f"universe           : {stats.get('universe', 0)}")
    print(f"candidates         : {stats.get('candidates', 0)}")
    print(f"missing_candle     : {stats.get('missing_candle', 0)}")
    print(f"invalid_candle     : {stats.get('invalid_candle', 0)}")
    print(f"selected_total     : {len(result.get('selected') or [])}")
    print(f"selected_whitelist    : {len(result.get('selected_whitelist') or [])}")
    print(f"selected_dynamic   : {len(result.get('selected_dynamic') or [])}")
    print(f"csv_path           : {CSV_PATH or '(disabled)'}")

    selected_set = {str(x).strip().upper() for x in (result.get("selected") or [])}
    rows_marked_selected = sum(1 for r in candidates if str(r.get("symbol") or "").strip().upper() in selected_set)
    print(f"candidate_rows_selected: {rows_marked_selected}")

    print("\nSelected symbols")
    print("-" * 80)
    print(", ".join(result.get("selected") or []))

    print(f"\nTop {min(PRINT_TOP_N, len(candidates))} candidates")
    print("-" * 80)
    for row in candidates[: max(0, int(PRINT_TOP_N))]:
        marker = "*" if row.get("selected") else " "
        whitelist = "W" if row.get("is_whitelisted") else " "
        reason = str(row.get("selection_reason") or "")
        print(
            f"{marker}{whitelist} "
            f"{str(row.get('symbol') or ''):14s} "
            f"score={float(row.get('score') or 0):.3f} "
            f"dir={str(row.get('direction') or ''):4s} "
            f"gap={float(row.get('gap_pct') or 0):7.3f}% "
            f"day={float(row.get('day_move_pct') or 0):7.3f}% "
            f"bar={float(row.get('candle_move_pct') or 0):7.3f}% "
            f"rng={float(row.get('candle_range_pct') or 0):7.3f}% "
            f"turnover_lakh={float(row.get('turnover_lakh') or 0):8.1f} "
            f"reason={reason}"
        )

    print("\nLegend: *=selected, W=whitelist")


# =============================================================================
# Main
# =============================================================================
def main() -> None:
    setup_logging(log_file=LOG_FILE)
    logger = logging.getLogger(__name__)

    as_of = _test_as_of()
    logger.info(
        "Starting test_stockscan | as_of=%s | apply_updates=%s | active_limit=%d",
        as_of.isoformat(),
        APPLY_UPDATES,
        SCANNER_CONFIG.scan.daily_active_limit,
    )

    scanner = StockScanner()
    result = scanner.generate_scan(as_of, apply_updates=APPLY_UPDATES)
    candidates = result.get("candidates") or []

    _write_csv(CSV_PATH, candidates)
    _print_summary(result, candidates)

    logger.info(
        "Stockscan test complete | selected=%d | candidates=%d | csv=%s | apply_updates=%s",
        len(result.get("selected") or []),
        len(candidates),
        CSV_PATH or "disabled",
        APPLY_UPDATES,
    )


if __name__ == "__main__":
    main()
