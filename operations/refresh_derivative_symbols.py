#!/usr/bin/env python3
"""Rebuild AutoTrades EQ/FUT/CE/PE symbols from the broker master.

The operation is occasional and applies by default. It builds the complete
configured front/near/far plan in memory, truncates ``symbols``, and recreates
the table from the authoritative broker instrument master.

EQ rows start outside the enabled/active universe. The following operations
rebuild those policy fields:

1. ``filter_stock_universe.py`` owns ``enabled``.
2. ``generate_stock_universe.py`` owns ``active``.

Use ``--review-only`` to generate the plan and report without truncating.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from sqlalchemy import func, text

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from database.database import get_trades_db
from logconfig import setup_logging
from models.trade_models import Symbol as SymbolORM
from schemas.instrument import InstrumentSchema
from utils.datetime_utils import IST, current_fo_expiry, fo_load_expiry_count

logger = logging.getLogger(__name__)

# Visible operational defaults. Expiry scope always comes from FoConfig.
DEFAULT_REPORT_DIR = "reports"
DEFAULT_REVIEW_ONLY = False
DEFAULT_LOG_FILE = "/var/www/autotrades/operations/refresh_derivative_symbols.log"

# Broker underlying name -> application EQ display name.
UNDERLYING_TO_DISPLAY = {
    "NIFTY": "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
}

_DERIVATIVE_TYPES = {"FUT", "FUTIDX", "FUTSTK", "CE", "PE"}
_FUTURE_TYPES = {"FUT", "FUTIDX", "FUTSTK"}

@dataclass(frozen=True)
class PlannedSymbol:
    underlying: str
    expiry: Optional[date]
    kind: str
    payload: Mapping[str, Any]

    @property
    def symbol(self) -> str:
        return str(self.payload["symbol"])


@dataclass
class RefreshReportRow:
    underlying: str
    equity_symbol: str
    front_expiry: date
    selected_expiry: Optional[date]
    expiry_position: Optional[int]
    spot_found: bool
    planned_eq: int = 0
    planned_fut: int = 0
    planned_ce: int = 0
    planned_pe: int = 0
    recreated_count: int = 0
    issues: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RefreshPlan:
    records: tuple[PlannedSymbol, ...]
    report_rows: tuple[RefreshReportRow, ...]
    underlyings_discovered: int
    underlyings_planned: int
    failed_underlyings: tuple[str, ...]


def _norm(value: Any) -> str:
    return str(value or "").strip().upper()


def _as_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise ValueError(f"unsupported_expiry_type:{type(value).__name__}")


def _display_for_underlying(underlying: str) -> str:
    normalized = _norm(underlying)
    return UNDERLYING_TO_DISPLAY.get(normalized, normalized)


def _symbol_type_for_instrument(inst: InstrumentSchema) -> str:
    instrument_type = _norm(inst.instrument_type)
    if instrument_type in _FUTURE_TYPES:
        return "FUT"
    if instrument_type in {"CE", "PE"}:
        return instrument_type
    raise ValueError(f"unsupported_derivative_type:{instrument_type}")


def select_published_expiries(
    instruments: Iterable[InstrumentSchema],
    *,
    front_expiry: date,
    expiry_count: int,
) -> tuple[date, ...]:
    """Select front/near/far monthly expiries from published future contracts."""
    expiries = sorted(
        {
            expiry
            for inst in instruments
            if _norm(inst.instrument_type) in _FUTURE_TYPES
            for expiry in [_as_date(inst.expiry)]
            if expiry is not None and expiry >= front_expiry
        }
    )
    return tuple(expiries[: max(1, int(expiry_count))])


def _build_master_caches(
    instruments: Sequence[InstrumentSchema],
) -> tuple[dict[str, list[InstrumentSchema]], dict[str, InstrumentSchema]]:
    by_underlying: dict[str, list[InstrumentSchema]] = defaultdict(list)
    equity_by_symbol: dict[str, InstrumentSchema] = {}

    for inst in instruments:
        name = _norm(inst.name)
        instrument_type = _norm(inst.instrument_type)
        segment = _norm(inst.segment)
        symbol = _norm(inst.tradingsymbol)

        if name:
            by_underlying[name].append(inst)
        if symbol and (instrument_type == "EQ" or segment in {"NSE", "BSE"}):
            equity_by_symbol.setdefault(symbol, inst)

    return dict(by_underlying), equity_by_symbol


def _new_symbol_payload(
    inst: InstrumentSchema,
    *,
    symbol_type: str,
    equity_ref: str,
) -> dict[str, Any]:
    is_equity = symbol_type == "EQ"
    return {
        "symbol": _norm(inst.tradingsymbol) if not is_equity else _norm(equity_ref),
        "token": str(inst.instrument_token) if inst.instrument_token is not None else None,
        "name": _norm(equity_ref) if is_equity else inst.name,
        "type": symbol_type,
        "price": inst.last_price,
        "exchange": inst.exchange,
        "segment": inst.segment,
        "signal_profile": "MOMENTUM",
        "lotsize": int(inst.lot_size or 1),
        "expiry": _as_date(inst.expiry) if not is_equity else None,
        "strike_price": inst.strike if symbol_type in {"CE", "PE"} else None,
        "tick_size": inst.tick_size,
        "equity_ref": _norm(equity_ref),
        "last_time": None,
        "last_snapshot": None,
        "generate_candles": is_equity,
        "merge_candles": is_equity,
        "update_performance": is_equity,
        "generate_signals": is_equity,
        "processed": False,
        # EQ policy is rebuilt by the next two operations.
        "active": False if is_equity else True,
        "enabled": False if is_equity else True,
    }


def build_refresh_plan(
    instruments: Sequence[InstrumentSchema],
    *,
    front_expiry: date,
    expiry_count: int,
) -> RefreshPlan:
    by_underlying, equity_by_symbol = _build_master_caches(instruments)
    discovered = sorted(
        underlying
        for underlying, rows in by_underlying.items()
        if any(_norm(inst.instrument_type) in _DERIVATIVE_TYPES for inst in rows)
    )

    records_by_symbol: dict[str, PlannedSymbol] = {}
    report_rows: list[RefreshReportRow] = []
    failed: list[str] = []
    planned_underlyings = 0

    for underlying in discovered:
        equity_symbol = _display_for_underlying(underlying)
        try:
            rows = by_underlying.get(underlying, [])
            equity_inst = equity_by_symbol.get(equity_symbol)
            selected_expiries = select_published_expiries(
                rows,
                front_expiry=front_expiry,
                expiry_count=expiry_count,
            )

            base_report = RefreshReportRow(
                underlying=underlying,
                equity_symbol=equity_symbol,
                front_expiry=front_expiry,
                selected_expiry=None,
                expiry_position=None,
                spot_found=equity_inst is not None,
            )

            if equity_inst is None:
                base_report.issues.append("MISSING_SPOT_INSTRUMENT")
                report_rows.append(base_report)
                failed.append(underlying)
                continue
            if not selected_expiries:
                base_report.issues.append("NO_PUBLISHED_FUTURE_AT_OR_AFTER_FRONT")
                report_rows.append(base_report)
                failed.append(underlying)
                continue

            if selected_expiries[0] != front_expiry:
                base_report.issues.append(
                    f"FRONT_EXPIRY_NOT_PUBLISHED:first={selected_expiries[0].isoformat()}"
                )
            if len(selected_expiries) < expiry_count:
                base_report.issues.append(
                    f"FEWER_EXPIRIES_PUBLISHED:{len(selected_expiries)}<{expiry_count}"
                )

            eq_payload = _new_symbol_payload(
                equity_inst,
                symbol_type="EQ",
                equity_ref=equity_symbol,
            )
            records_by_symbol.setdefault(
                eq_payload["symbol"],
                PlannedSymbol(underlying, None, "EQ", eq_payload),
            )
            base_report.planned_eq = 1
            report_rows.append(base_report)

            for position, expiry in enumerate(selected_expiries, start=1):
                expiry_rows = [
                    inst for inst in rows if _as_date(inst.expiry) == expiry
                ]
                futures = [
                    inst
                    for inst in expiry_rows
                    if _norm(inst.instrument_type) in _FUTURE_TYPES
                ]
                calls = [
                    inst for inst in expiry_rows if _norm(inst.instrument_type) == "CE"
                ]
                puts = [
                    inst for inst in expiry_rows if _norm(inst.instrument_type) == "PE"
                ]

                report = RefreshReportRow(
                    underlying=underlying,
                    equity_symbol=equity_symbol,
                    front_expiry=front_expiry,
                    selected_expiry=expiry,
                    expiry_position=position,
                    spot_found=True,
                    planned_fut=len(futures),
                    planned_ce=len(calls),
                    planned_pe=len(puts),
                )
                if len(futures) != 1:
                    report.issues.append(f"FUTURE_COUNT:{len(futures)}")
                if not calls:
                    report.issues.append("NO_CALL_OPTIONS")
                if not puts:
                    report.issues.append("NO_PUT_OPTIONS")

                for inst in futures + calls + puts:
                    symbol_type = _symbol_type_for_instrument(inst)
                    payload = _new_symbol_payload(
                        inst,
                        symbol_type=symbol_type,
                        equity_ref=equity_symbol,
                    )
                    planned = PlannedSymbol(
                        underlying=underlying,
                        expiry=expiry,
                        kind=symbol_type,
                        payload=payload,
                    )
                    existing_plan = records_by_symbol.get(planned.symbol)
                    if existing_plan and dict(existing_plan.payload) != dict(planned.payload):
                        raise ValueError(f"conflicting_symbol_payload:{planned.symbol}")
                    records_by_symbol[planned.symbol] = planned

                report_rows.append(report)

            planned_underlyings += 1
        except Exception as exc:
            logger.exception(
                "REFRESH_DERIVATIVE_SYMBOLS_PLAN_FAILED | underlying=%s", underlying
            )
            report_rows.append(
                RefreshReportRow(
                    underlying=underlying,
                    equity_symbol=equity_symbol,
                    front_expiry=front_expiry,
                    selected_expiry=None,
                    expiry_position=None,
                    spot_found=False,
                    issues=[f"PLAN_ERROR:{type(exc).__name__}:{exc}"],
                )
            )
            failed.append(underlying)

    return RefreshPlan(
        records=tuple(records_by_symbol[symbol] for symbol in sorted(records_by_symbol)),
        report_rows=tuple(report_rows),
        underlyings_discovered=len(discovered),
        underlyings_planned=planned_underlyings,
        failed_underlyings=tuple(sorted(set(failed))),
    )


def _apply_plan(plan: RefreshPlan) -> tuple[int, dict[str, int]]:
    """Truncate and recreate ``symbols`` from the fully validated plan."""
    expected_by_kind: dict[str, int] = defaultdict(int)
    for planned in plan.records:
        expected_by_kind[planned.kind] += 1

    with get_trades_db() as db:
        # MySQL TRUNCATE intentionally resets the table and auto-increment state.
        # The complete plan has already been built before this destructive step.
        db.execute(text("TRUNCATE TABLE symbols"))
        db.add_all([SymbolORM(**dict(planned.payload)) for planned in plan.records])
        db.flush()

        written_total = int(db.query(func.count(SymbolORM.symbol)).scalar() or 0)
        written_by_kind = {
            str(kind): int(count)
            for kind, count in (
                db.query(SymbolORM.type, func.count(SymbolORM.symbol))
                .group_by(SymbolORM.type)
                .all()
            )
        }

        if written_total != len(plan.records):
            raise RuntimeError(
                f"symbol_rebuild_count_mismatch:{written_total}!={len(plan.records)}"
            )
        for kind, expected in expected_by_kind.items():
            actual = written_by_kind.get(kind, 0)
            if actual != expected:
                raise RuntimeError(
                    f"symbol_rebuild_kind_mismatch:{kind}:{actual}!={expected}"
                )

        db.commit()
        return written_total, written_by_kind


def _annotate_report_rebuild(
    plan: RefreshPlan,
    *,
    applied: bool,
) -> list[RefreshReportRow]:
    rows = [
        RefreshReportRow(
            underlying=row.underlying,
            equity_symbol=row.equity_symbol,
            front_expiry=row.front_expiry,
            selected_expiry=row.selected_expiry,
            expiry_position=row.expiry_position,
            spot_found=row.spot_found,
            planned_eq=row.planned_eq,
            planned_fut=row.planned_fut,
            planned_ce=row.planned_ce,
            planned_pe=row.planned_pe,
            issues=list(row.issues),
        )
        for row in plan.report_rows
    ]
    if not applied:
        return rows

    report_by_key = {(row.underlying, row.selected_expiry): row for row in rows}
    for planned in plan.records:
        report = report_by_key.get((planned.underlying, planned.expiry))
        if report is not None:
            report.recreated_count += 1
    return rows


def _write_report(
    report_dir: str,
    rows: Sequence[RefreshReportRow],
    run_time: datetime,
) -> Path:
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"refresh_derivative_symbols_{run_time.strftime('%Y%m%d_%H%M%S')}.csv"
    fieldnames = [
        "underlying",
        "equity_symbol",
        "front_expiry",
        "selected_expiry",
        "expiry_position",
        "spot_found",
        "planned_eq",
        "planned_fut",
        "planned_ce",
        "planned_pe",
        "recreated_count",
        "issues",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "underlying": row.underlying,
                    "equity_symbol": row.equity_symbol,
                    "front_expiry": row.front_expiry.isoformat(),
                    "selected_expiry": (
                        row.selected_expiry.isoformat() if row.selected_expiry else ""
                    ),
                    "expiry_position": row.expiry_position or "",
                    "spot_found": row.spot_found,
                    "planned_eq": row.planned_eq,
                    "planned_fut": row.planned_fut,
                    "planned_ce": row.planned_ce,
                    "planned_pe": row.planned_pe,
                    "recreated_count": row.recreated_count,
                    "issues": "|".join(row.issues),
                }
            )
    return path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild AutoTrades EQ and configured three-expiry derivative "
            "symbols from the authoritative broker instrument master."
        )
    )
    parser.add_argument(
        "--report-dir",
        default=DEFAULT_REPORT_DIR,
        help="Directory for the consolidated rebuild report.",
    )
    parser.add_argument(
        "--review-only",
        action="store_true",
        default=DEFAULT_REVIEW_ONLY,
        help="Build the full plan and report without truncating symbols.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    setup_logging(log_file=DEFAULT_LOG_FILE)
    started = time.monotonic()
    run_time = datetime.now(IST)

    try:
        front_expiry = current_fo_expiry()
        expiry_count = fo_load_expiry_count()
    except Exception:
        logger.exception("REFRESH_DERIVATIVE_SYMBOLS_CONFIG_FAILED")
        return 2

    if expiry_count < 1:
        logger.error("REFRESH_DERIVATIVE_SYMBOLS_ABORT | reason=expiry_count_lt_1")
        return 2

    logger.info(
        "REFRESH_DERIVATIVE_SYMBOLS_START | mode=%s | front_expiry=%s | "
        "expiry_count=%d | rebuild=TRUNCATE_AND_RECREATE",
        "REVIEW" if args.review_only else "APPLY",
        front_expiry,
        expiry_count,
    )

    try:
        instruments = InstrumentSchema.fetch_instruments() or []
    except Exception:
        logger.exception("REFRESH_DERIVATIVE_SYMBOLS_MASTER_READ_FAILED")
        return 2
    if not instruments:
        logger.error("REFRESH_DERIVATIVE_SYMBOLS_ABORT | reason=empty_instrument_master")
        return 2

    plan = build_refresh_plan(
        instruments,
        front_expiry=front_expiry,
        expiry_count=expiry_count,
    )
    if not plan.records:
        logger.error("REFRESH_DERIVATIVE_SYMBOLS_ABORT | reason=no_planned_symbols")
        return 2

    try:
        if args.review_only:
            verified_count = 0
            verified_by_kind: dict[str, int] = {}
        else:
            verified_count, verified_by_kind = _apply_plan(plan)
    except Exception:
        logger.exception("REFRESH_DERIVATIVE_SYMBOLS_REBUILD_FAILED")
        return 2

    report_rows = _annotate_report_rebuild(plan, applied=not args.review_only)
    try:
        report_path = _write_report(args.report_dir, report_rows, run_time)
    except Exception:
        logger.exception("REFRESH_DERIVATIVE_SYMBOLS_REPORT_FAILED")
        report_path = None

    kind_counts: dict[str, int] = defaultdict(int)
    for planned in plan.records:
        kind_counts[planned.kind] += 1

    warning_rows = [row for row in report_rows if row.issues]
    front_underlyings = {
        row.underlying
        for row in report_rows
        if row.selected_expiry == front_expiry and row.planned_fut > 0
    }
    summary = {
        "mode": "REVIEW" if args.review_only else "APPLY",
        "rebuild_strategy": "TRUNCATE_AND_RECREATE",
        "front_expiry": front_expiry.isoformat(),
        "expiry_count": expiry_count,
        "master_instruments": len(instruments),
        "underlyings_discovered": plan.underlyings_discovered,
        "underlyings_planned": plan.underlyings_planned,
        "front_expiry_underlyings": len(front_underlyings),
        "failed_underlyings": list(plan.failed_underlyings),
        "planned_symbols": len(plan.records),
        "planned_eq": kind_counts["EQ"],
        "planned_fut": kind_counts["FUT"],
        "planned_ce": kind_counts["CE"],
        "planned_pe": kind_counts["PE"],
        "recreated_symbols": verified_count,
        "verified_by_kind": verified_by_kind,
        "warning_rows": len(warning_rows),
        "report": str(report_path) if report_path else None,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    logger.info(
        "REFRESH_DERIVATIVE_SYMBOLS_SUMMARY | %s",
        json.dumps(summary, sort_keys=True),
    )

    for row in warning_rows:
        logger.warning(
            "REFRESH_DERIVATIVE_SYMBOLS_WARNING | underlying=%s | expiry=%s | issues=%s",
            row.underlying,
            row.selected_expiry,
            row.issues,
        )

    return 1 if plan.failed_underlyings else 0


if __name__ == "__main__":
    raise SystemExit(main())
