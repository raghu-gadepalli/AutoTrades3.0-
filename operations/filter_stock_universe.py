#!/usr/bin/env python3
"""Apply application whitelist/blacklist policy to ``symbols.enabled``.

This operation owns only the long-lived ``enabled`` policy flag. It does not
read broker quotes or historical bars, calculate price/ATR/beta metrics, or
change ``active`` and runtime signal flags.

The default is review mode. Pass ``--apply`` to persist the proposed enabled
state.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Sequence

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from configs.scanner_config import SCANNER_CONFIG
from database.database import get_trades_db
from logconfig import setup_logging
from models.trade_models import Symbol as SymbolORM
from utils.datetime_utils import IST
from utils.universe_policy import universe_blacklist, universe_whitelist

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Visible source defaults. CLI values override these defaults.
# -----------------------------------------------------------------------------
DEFAULT_SYMBOLS: Optional[str] = None
DEFAULT_REPORT_DIR = SCANNER_CONFIG.filter.report_dir
DEFAULT_APPLY = False
DEFAULT_WRITE_REPORT = True
DEFAULT_LOG_FILE = SCANNER_CONFIG.filter.log_file


@dataclass(frozen=True)
class PolicyDecision:
    symbol: str
    symbol_type: str
    before_enabled: bool
    proposed_enabled: bool
    changed: bool
    whitelisted: bool
    blacklisted: bool
    reason: str


def _norm(value: object) -> str:
    return str(value or "").strip().upper()


def _parse_symbol_filter(raw: Optional[str]) -> Optional[set[str]]:
    if not raw:
        return None
    values = {_norm(item) for item in raw.split(",") if _norm(item)}
    return values or None


def _load_eq_rows(symbol_filter: Optional[set[str]]) -> list[tuple[str, str, bool]]:
    with get_trades_db() as db:
        query = db.query(SymbolORM.symbol, SymbolORM.type, SymbolORM.enabled).filter(
            SymbolORM.type == "EQ"
        )
        if symbol_filter:
            query = query.filter(SymbolORM.symbol.in_(sorted(symbol_filter)))
        rows = query.order_by(SymbolORM.symbol.asc()).all()
    return [(_norm(symbol), str(symbol_type), bool(enabled)) for symbol, symbol_type, enabled in rows]


def build_policy_decisions(
    rows: Iterable[tuple[str, str, bool]],
    whitelist: set[str],
    blacklist: set[str],
) -> list[PolicyDecision]:
    decisions: list[PolicyDecision] = []
    for symbol, symbol_type, before_enabled in rows:
        whitelisted = symbol in whitelist
        blacklisted = symbol in blacklist

        if whitelisted and blacklisted:
            proposed_enabled = True
            reason = "WHITELIST_OVERRIDES_BLACKLIST"
        elif whitelisted:
            proposed_enabled = True
            reason = "WHITELIST"
        elif blacklisted:
            proposed_enabled = False
            reason = "BLACKLIST"
        else:
            proposed_enabled = True
            reason = "ELIGIBLE_EQ"

        decisions.append(
            PolicyDecision(
                symbol=symbol,
                symbol_type=symbol_type,
                before_enabled=before_enabled,
                proposed_enabled=proposed_enabled,
                changed=before_enabled != proposed_enabled,
                whitelisted=whitelisted,
                blacklisted=blacklisted,
                reason=reason,
            )
        )
    return decisions


def _apply_decisions(decisions: Sequence[PolicyDecision]) -> int:
    proposed = {row.symbol: row.proposed_enabled for row in decisions}
    if not proposed:
        return 0

    changed = 0
    with get_trades_db() as db:
        rows = (
            db.query(SymbolORM)
            .filter(SymbolORM.type == "EQ", SymbolORM.symbol.in_(sorted(proposed)))
            .all()
        )
        for row in rows:
            target = bool(proposed[_norm(row.symbol)])
            if bool(row.enabled) != target:
                row.enabled = target
                changed += 1
        db.commit()
    return changed


def _write_report(report_dir: str, decisions: Sequence[PolicyDecision], run_time: datetime) -> Path:
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"filter_stock_universe_{run_time.strftime('%Y%m%d_%H%M%S')}.csv"
    fieldnames = [
        "symbol",
        "symbol_type",
        "before_enabled",
        "proposed_enabled",
        "changed",
        "whitelisted",
        "blacklisted",
        "reason",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for decision in decisions:
            writer.writerow(asdict(decision))
    return path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Review or apply whitelist/blacklist policy to EQ symbols.enabled."
    )
    parser.add_argument(
        "--symbols",
        default=DEFAULT_SYMBOLS,
        help="Optional comma-separated EQ symbols for a focused review.",
    )
    parser.add_argument(
        "--report-dir",
        default=DEFAULT_REPORT_DIR,
        help="Directory for the review CSV.",
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="Do not write the review CSV.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=DEFAULT_APPLY,
        help="Persist proposed enabled flags. Default is review only.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    setup_logging(log_file=DEFAULT_LOG_FILE)

    run_time = datetime.now(IST)
    symbol_filter = _parse_symbol_filter(args.symbols)
    whitelist = {_norm(value) for value in universe_whitelist()}
    blacklist = {_norm(value) for value in universe_blacklist()}

    logger.info(
        "FILTER_STOCK_UNIVERSE_START | apply=%s | focused_symbols=%s | whitelist=%d | blacklist=%d",
        bool(args.apply),
        len(symbol_filter or []),
        len(whitelist),
        len(blacklist),
    )

    try:
        rows = _load_eq_rows(symbol_filter)
    except Exception:
        logger.exception("FILTER_STOCK_UNIVERSE_PREFLIGHT_FAILED")
        return 2

    if not rows:
        logger.error("FILTER_STOCK_UNIVERSE_ABORT | reason=no_eq_symbols")
        return 2

    decisions = build_policy_decisions(rows, whitelist, blacklist)
    changed = [row for row in decisions if row.changed]
    enabled_after = sum(1 for row in decisions if row.proposed_enabled)
    disabled_after = len(decisions) - enabled_after
    overlap = sorted(whitelist & blacklist)
    missing_requested = sorted((symbol_filter or set()) - {row.symbol for row in decisions})

    report_path: Optional[Path] = None
    if DEFAULT_WRITE_REPORT and not args.no_report:
        try:
            report_path = _write_report(args.report_dir, decisions, run_time)
        except Exception:
            logger.exception("FILTER_STOCK_UNIVERSE_REPORT_FAILED")
            return 2

    applied_count = 0
    if args.apply:
        try:
            applied_count = _apply_decisions(decisions)
        except Exception:
            logger.exception("FILTER_STOCK_UNIVERSE_APPLY_FAILED")
            return 2

    summary = {
        "mode": "APPLY" if args.apply else "REVIEW",
        "rows": len(decisions),
        "enabled_after": enabled_after,
        "disabled_after": disabled_after,
        "proposed_changes": len(changed),
        "applied_changes": applied_count,
        "whitelist_blacklist_overlap": overlap,
        "missing_requested_symbols": missing_requested,
        "report": str(report_path) if report_path else None,
    }
    logger.info("FILTER_STOCK_UNIVERSE_SUMMARY | %s", json.dumps(summary, sort_keys=True))

    for decision in changed:
        logger.info(
            "FILTER_STOCK_UNIVERSE_CHANGE | symbol=%s | enabled=%s->%s | reason=%s",
            decision.symbol,
            decision.before_enabled,
            decision.proposed_enabled,
            decision.reason,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
