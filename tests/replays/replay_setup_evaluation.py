#!/usr/bin/env python3
"""Read-only setup evaluation over Auction snapshots.

The default scope is one symbol (ABB) and one trading day.  The runner reads
persisted snapshots or an exported snapshots CSV, evaluates explicit Auction
creation events, and writes compact detail and summary reports.  It does not
write opportunities, signals, trades or snapshot processed flags.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import date, datetime
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from schemas.snapshot import SnapshotSchema
from services.auction_engine.setup_evaluator import (
    SetupEvaluator,
)

logger = logging.getLogger(__name__)

DEFAULT_TRADING_DAY = "2026-08-03"
DEFAULT_SYMBOL = "ABB"
DEFAULT_REPORT_DIR = "reports"
DEFAULT_CSV: Optional[str] = None
DEFAULT_BATCH_SIZE = 500


def _args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate Auction setup events without downstream writes."
    )
    parser.add_argument("--day", "--date", dest="date", default=DEFAULT_TRADING_DAY)
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument(
        "--csv",
        default=DEFAULT_CSV,
        help="Optional exported snapshots CSV. When omitted, read from the configured DB.",
    )
    parser.add_argument("--report-dir", default=DEFAULT_REPORT_DIR)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    return parser.parse_args(argv)


def _load_csv(path: Path, *, symbol: str, trading_day: date) -> List[SnapshotSchema]:
    snapshots: List[SnapshotSchema] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row_number, row in enumerate(reader, start=2):
            row_symbol = str(row.get("symbol") or "").strip().upper()
            if row_symbol != symbol:
                continue
            raw = row.get("data")
            if not raw:
                logger.error("CSV row %s has no data payload", row_number)
                continue
            try:
                payload = json.loads(raw)
                snapshot = SnapshotSchema.from_db_dict(payload)
            except Exception:
                logger.exception("CSV row %s failed snapshot validation", row_number)
                continue
            if snapshot.snapshot_time.date() != trading_day:
                continue
            snapshots.append(snapshot)
    return sorted(snapshots, key=lambda item: item.snapshot_time)


def _load_db(
    *,
    symbol: str,
    trading_day: date,
    batch_size: int,
) -> List[SnapshotSchema]:
    output: List[SnapshotSchema] = []
    after_time: Optional[datetime] = None
    after_symbol = ""
    while True:
        batch = SnapshotSchema.fetch_day_replay_batch(
            trading_day=trading_day,
            after_time=after_time,
            after_symbol=after_symbol,
            symbols=[symbol],
            limit=max(1, batch_size),
        )
        if not batch:
            break
        output.extend(batch)
        last = batch[-1]
        after_time = last.snapshot_time.replace(tzinfo=None)
        after_symbol = last.symbol
        if len(batch) < max(1, batch_size):
            break
    return output


def _write_csv(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    data = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in data for key in row}) if data else ["status"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(data)


def _evaluation_row(snapshot: SnapshotSchema, result: Any) -> Dict[str, Any]:
    return {
        "symbol": result.symbol,
        "snapshot_time": result.snapshot_time.isoformat(),
        "close": float(snapshot.close),
        "fresh_direction": snapshot.auction.evidence.side.value,
        "active_direction": snapshot.auction.directional.direction.value,
        "directional_transition": snapshot.auction.directional.transition.value,
        "balance_state": snapshot.auction.balance.current_state.value,
        "source_event_id": result.source_event_id,
        "source_event_type": result.source_event_type.value,
        "source_episode_id": result.source_episode_id,
        "setup_family": result.setup_family.value,
        "side": result.side.value,
        "evaluation_status": result.status.value,
        "structural_result": (
            result.structural_result.value if result.structural_result is not None else ""
        ),
        "selected": result.selected,
        "candidate_id": result.candidate_id or "",
        "entry_price": result.entry_price,
        "stop_price": result.stop_price,
        "target_price": result.target_price,
        "reference_price": result.reference_price,
        "blockers": "|".join(result.blockers),
        "reason_codes": "|".join(result.reason_codes),
        "manager_reason_codes": "|".join(result.manager_reason_codes),
        "permission_source": result.permission_source,
        "stored_permission_match": result.stored_permission_match,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s:%(lineno)d] %(message)s",
    )
    symbol = str(args.symbol or "").strip().upper()
    if not symbol:
        raise ValueError("--symbol is required")
    trading_day = date.fromisoformat(args.date)
    if args.csv:
        snapshots = _load_csv(Path(args.csv), symbol=symbol, trading_day=trading_day)
        source = str(Path(args.csv))
    else:
        snapshots = _load_db(
            symbol=symbol,
            trading_day=trading_day,
            batch_size=args.batch_size,
        )
        source = "configured database"
    if not snapshots:
        raise ValueError(f"No snapshots found for {symbol} on {trading_day}")

    evaluator = SetupEvaluator()
    rows: List[Dict[str, Any]] = []
    failures = 0
    for snapshot in snapshots:
        try:
            evaluations = evaluator.evaluate(snapshot)
        except Exception:
            failures += 1
            logger.exception(
                "[%s] evaluation failed at %s",
                symbol,
                snapshot.snapshot_time,
            )
            continue
        rows.extend(_evaluation_row(snapshot, item) for item in evaluations)

    report_root = Path(args.report_dir) / f"setup_evaluation_{trading_day:%Y%m%d}"
    detail_path = report_root / f"{symbol}.csv"
    summary_path = report_root / f"{symbol}_summary.csv"
    _write_csv(detail_path, rows)

    counts = Counter((row["setup_family"], row["evaluation_status"]) for row in rows)
    permission_mismatch_count = sum(
        1 for row in rows if not bool(row.get("stored_permission_match", True))
    )
    selected_counts = Counter(
        (row["setup_family"], row["evaluation_status"])
        for row in rows
        if bool(row.get("selected"))
    )
    summary_rows = [
        {
            "symbol": symbol,
            "trading_day": trading_day.isoformat(),
            "snapshot_count": len(snapshots),
            "evaluation_count": len(rows),
            "setup_family": family,
            "evaluation_status": status,
            "count": count,
            "selected_count": selected_counts.get((family, status), 0),
            "record_failures": failures,
            "permission_mismatch_count": permission_mismatch_count,
            "source": source,
        }
        for (family, status), count in sorted(counts.items())
    ]
    if not summary_rows:
        summary_rows = [{
            "symbol": symbol,
            "trading_day": trading_day.isoformat(),
            "snapshot_count": len(snapshots),
            "evaluation_count": 0,
            "setup_family": "",
            "evaluation_status": "",
            "count": 0,
            "selected_count": 0,
            "record_failures": failures,
            "permission_mismatch_count": permission_mismatch_count,
            "source": source,
        }]
    _write_csv(summary_path, summary_rows)

    logger.info(
        "Completed %s %s: snapshots=%s evaluations=%s failures=%s detail=%s summary=%s",
        symbol,
        trading_day,
        len(snapshots),
        len(rows),
        failures,
        detail_path,
        summary_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
