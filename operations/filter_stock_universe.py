#!/usr/bin/env python3
"""Review or apply the enabled EQ universe policy.

This operation owns ``symbols.enabled``.  The configured whitelist and
blacklist come from ``SCANNER_CONFIG.universe`` through ``utils.universe_policy``.
For all other EQ symbols, a live broker quote enforces the configured minimum
price (₹200 by default).  ATR, beta, first-candle movement, active-universe
selection and StockRank are intentionally outside this program.

The default is review mode.  Pass ``--apply`` to persist enabled decisions and
refresh the stored EQ price.  ``symbols.active`` and runtime signal flags are
never changed.
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
from typing import Any, Iterable, Mapping, Optional, Sequence

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config import AppConfig
from configs.scanner_config import SCANNER_CONFIG
from database.database import get_trades_db
from logconfig import setup_logging
from models.trade_models import Symbol as SymbolORM
from schemas.user import UserSchema
from services.zerodha.kiteconnect_service import KiteConnectService
from utils.datetime_utils import IST
from utils.universe_policy import universe_blacklist, universe_whitelist

logger = logging.getLogger(__name__)

# Visible source defaults. CLI values override these defaults.
DEFAULT_SYMBOLS: Optional[str] = None
DEFAULT_REPORT_DIR = SCANNER_CONFIG.filter.report_dir
DEFAULT_MINIMUM_PRICE = float(SCANNER_CONFIG.filter.minimum_price)
DEFAULT_QUOTE_BATCH_SIZE = int(SCANNER_CONFIG.filter.quote_batch_size)
DEFAULT_MINIMUM_QUOTE_COVERAGE_PCT = float(
    SCANNER_CONFIG.filter.minimum_quote_coverage_pct
)
DEFAULT_APPLY = False
DEFAULT_WRITE_REPORT = True
DEFAULT_LOG_FILE = SCANNER_CONFIG.filter.log_file


@dataclass(frozen=True)
class EqPolicyRow:
    symbol: str
    symbol_type: str
    exchange: str
    before_enabled: bool
    price_before: Optional[float]


@dataclass(frozen=True)
class PolicyDecision:
    symbol: str
    symbol_type: str
    exchange: str
    before_enabled: bool
    proposed_enabled: bool
    changed: bool
    price_before: Optional[float]
    quote_price: Optional[float]
    quote_available: bool
    minimum_price: float
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


def _load_eq_rows(symbol_filter: Optional[set[str]]) -> list[EqPolicyRow]:
    with get_trades_db() as db:
        query = db.query(
            SymbolORM.symbol,
            SymbolORM.type,
            SymbolORM.exchange,
            SymbolORM.enabled,
            SymbolORM.price,
        ).filter(SymbolORM.type == "EQ")
        if symbol_filter:
            query = query.filter(SymbolORM.symbol.in_(sorted(symbol_filter)))
        rows = query.order_by(SymbolORM.symbol.asc()).all()

    return [
        EqPolicyRow(
            symbol=_norm(symbol),
            symbol_type=str(symbol_type),
            exchange=_norm(exchange) or "NSE",
            before_enabled=bool(enabled),
            price_before=float(price) if price is not None else None,
        )
        for symbol, symbol_type, exchange, enabled, price in rows
    ]


def _data_client() -> KiteConnectService:
    user = UserSchema.fetch_user(AppConfig.DATA_USER)
    if not user:
        raise RuntimeError(f"DATA_USER not found: {AppConfig.DATA_USER}")
    if not user.apikey or not user.access_token:
        raise RuntimeError(f"DATA_USER missing apikey/access_token: {AppConfig.DATA_USER}")
    return KiteConnectService(api_key=user.apikey, access_token=user.access_token)


def _fetch_quote_prices(
    rows: Sequence[EqPolicyRow],
    *,
    batch_size: int,
) -> dict[str, float]:
    kite = _data_client()
    key_to_symbol = {
        f"{row.exchange or 'NSE'}:{row.symbol}": row.symbol for row in rows
    }
    keys = list(key_to_symbol)
    prices: dict[str, float] = {}

    for start in range(0, len(keys), max(1, batch_size)):
        batch = keys[start : start + max(1, batch_size)]
        response = kite.fetch_quote(batch)
        if not isinstance(response, Mapping):
            response = {}
        for key in batch:
            record = response.get(key)
            if not isinstance(record, Mapping):
                continue
            value = record.get("last_price")
            try:
                price = float(value)
            except (TypeError, ValueError):
                continue
            if price > 0:
                prices[key_to_symbol[key]] = price
    return prices


def build_policy_decisions(
    rows: Iterable[EqPolicyRow],
    whitelist: set[str],
    blacklist: set[str],
    quote_prices: Mapping[str, float],
    minimum_price: float,
) -> list[PolicyDecision]:
    """Build deterministic enabled decisions from shared policy and quotes."""
    decisions: list[PolicyDecision] = []
    for row in rows:
        symbol = _norm(row.symbol)
        whitelisted = symbol in whitelist
        blacklisted = symbol in blacklist
        quote_price = quote_prices.get(symbol)
        quote_available = quote_price is not None and float(quote_price) > 0

        if whitelisted and blacklisted:
            proposed_enabled = True
            reason = "WHITELIST_OVERRIDES_BLACKLIST"
        elif whitelisted:
            proposed_enabled = True
            reason = "WHITELIST"
        elif blacklisted:
            proposed_enabled = False
            reason = "BLACKLIST"
        elif not quote_available:
            # A missing quote is unresolved, not evidence to enable or disable.
            proposed_enabled = row.before_enabled
            reason = "QUOTE_UNAVAILABLE"
        elif float(quote_price) < float(minimum_price):
            proposed_enabled = False
            reason = "BELOW_MIN_PRICE"
        else:
            proposed_enabled = True
            reason = "ELIGIBLE_EQ"

        decisions.append(
            PolicyDecision(
                symbol=symbol,
                symbol_type=row.symbol_type,
                exchange=row.exchange,
                before_enabled=row.before_enabled,
                proposed_enabled=proposed_enabled,
                changed=row.before_enabled != proposed_enabled,
                price_before=row.price_before,
                quote_price=float(quote_price) if quote_available else None,
                quote_available=quote_available,
                minimum_price=float(minimum_price),
                whitelisted=whitelisted,
                blacklisted=blacklisted,
                reason=reason,
            )
        )
    return decisions


def quote_coverage_pct(decisions: Sequence[PolicyDecision]) -> float:
    price_dependent = [
        row for row in decisions if not row.whitelisted and not row.blacklisted
    ]
    if not price_dependent:
        return 100.0
    available = sum(1 for row in price_dependent if row.quote_available)
    return round((available / len(price_dependent)) * 100.0, 2)


def _apply_decisions(decisions: Sequence[PolicyDecision]) -> tuple[int, int]:
    proposed = {row.symbol: row for row in decisions}
    if not proposed:
        return 0, 0

    enabled_changes = 0
    price_updates = 0
    with get_trades_db() as db:
        rows = (
            db.query(SymbolORM)
            .filter(SymbolORM.type == "EQ", SymbolORM.symbol.in_(sorted(proposed)))
            .all()
        )
        for row in rows:
            decision = proposed[_norm(row.symbol)]
            target = bool(decision.proposed_enabled)
            if bool(row.enabled) != target:
                row.enabled = target
                enabled_changes += 1
            if decision.quote_price is not None:
                existing_price = float(row.price) if row.price is not None else None
                if existing_price != decision.quote_price:
                    row.price = decision.quote_price
                    price_updates += 1
        db.commit()
    return enabled_changes, price_updates


def _write_report(
    report_dir: str,
    decisions: Sequence[PolicyDecision],
    run_time: datetime,
) -> Path:
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"filter_stock_universe_{run_time.strftime('%Y%m%d_%H%M%S')}.csv"
    fieldnames = [
        "symbol",
        "symbol_type",
        "exchange",
        "before_enabled",
        "proposed_enabled",
        "changed",
        "price_before",
        "quote_price",
        "quote_available",
        "minimum_price",
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
        description=(
            "Review or apply enabled EQ policy from whitelist, blacklist and "
            "minimum live price."
        )
    )
    parser.add_argument(
        "--symbols",
        default=DEFAULT_SYMBOLS,
        help="Optional comma-separated EQ symbols for a focused review.",
    )
    parser.add_argument(
        "--minimum-price",
        type=float,
        default=DEFAULT_MINIMUM_PRICE,
        help="Minimum live EQ price for non-whitelist symbols.",
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
        help="Persist proposed enabled flags and quote prices. Default is review only.",
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
        "FILTER_STOCK_UNIVERSE_START | apply=%s | focused_symbols=%s | "
        "minimum_price=%.2f | whitelist=%d | blacklist=%d",
        bool(args.apply),
        len(symbol_filter or []),
        args.minimum_price,
        len(whitelist),
        len(blacklist),
    )

    try:
        rows = _load_eq_rows(symbol_filter)
    except Exception:
        logger.exception("FILTER_STOCK_UNIVERSE_DB_PREFLIGHT_FAILED")
        return 2
    if not rows:
        logger.error("FILTER_STOCK_UNIVERSE_ABORT | reason=no_eq_symbols")
        return 2

    try:
        quote_prices = _fetch_quote_prices(rows, batch_size=DEFAULT_QUOTE_BATCH_SIZE)
    except Exception:
        logger.exception("FILTER_STOCK_UNIVERSE_QUOTE_PREFLIGHT_FAILED")
        quote_prices = {}

    decisions = build_policy_decisions(
        rows,
        whitelist,
        blacklist,
        quote_prices,
        args.minimum_price,
    )
    changed = [row for row in decisions if row.changed]
    enabled_after = sum(1 for row in decisions if row.proposed_enabled)
    disabled_after = len(decisions) - enabled_after
    unresolved = [row.symbol for row in decisions if row.reason == "QUOTE_UNAVAILABLE"]
    coverage = quote_coverage_pct(decisions)
    safe_to_apply = coverage >= DEFAULT_MINIMUM_QUOTE_COVERAGE_PCT
    overlap = sorted(whitelist & blacklist)
    missing_requested = sorted((symbol_filter or set()) - {row.symbol for row in decisions})

    report_path: Optional[Path] = None
    if DEFAULT_WRITE_REPORT and not args.no_report:
        try:
            report_path = _write_report(args.report_dir, decisions, run_time)
        except Exception:
            logger.exception("FILTER_STOCK_UNIVERSE_REPORT_FAILED")
            return 2

    enabled_changes = 0
    price_updates = 0
    if args.apply:
        if not safe_to_apply:
            logger.error(
                "FILTER_STOCK_UNIVERSE_APPLY_BLOCKED | quote_coverage_pct=%.2f | required=%.2f",
                coverage,
                DEFAULT_MINIMUM_QUOTE_COVERAGE_PCT,
            )
        else:
            try:
                enabled_changes, price_updates = _apply_decisions(decisions)
            except Exception:
                logger.exception("FILTER_STOCK_UNIVERSE_APPLY_FAILED")
                return 2

    summary = {
        "mode": "APPLY" if args.apply else "REVIEW",
        "rows": len(decisions),
        "minimum_price": args.minimum_price,
        "enabled_after": enabled_after,
        "disabled_after": disabled_after,
        "proposed_enabled_changes": len(changed),
        "applied_enabled_changes": enabled_changes,
        "applied_price_updates": price_updates,
        "quote_coverage_pct": coverage,
        "minimum_quote_coverage_pct": DEFAULT_MINIMUM_QUOTE_COVERAGE_PCT,
        "quote_unavailable_count": len(unresolved),
        "quote_unavailable_symbols": unresolved,
        "safe_to_apply": safe_to_apply,
        "whitelist_blacklist_overlap": overlap,
        "missing_requested_symbols": missing_requested,
        "report": str(report_path) if report_path else None,
    }
    logger.info("FILTER_STOCK_UNIVERSE_SUMMARY | %s", json.dumps(summary, sort_keys=True))

    for decision in changed:
        logger.info(
            "FILTER_STOCK_UNIVERSE_CHANGE | symbol=%s | enabled=%s->%s | "
            "quote_price=%s | reason=%s",
            decision.symbol,
            decision.before_enabled,
            decision.proposed_enabled,
            decision.quote_price,
            decision.reason,
        )

    if args.apply and not safe_to_apply:
        return 2
    return 1 if not safe_to_apply else 0


if __name__ == "__main__":
    raise SystemExit(main())
