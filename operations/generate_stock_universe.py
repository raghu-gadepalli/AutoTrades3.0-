#!/usr/bin/env python3
"""Review or apply the curated active EQ universe.

The operation reads the policy-enabled EQ universe, evaluates stable
longer-horizon tradability and movement-quality evidence, and proposes up to
``active_limit`` symbols for continuous snapshot observation.

It owns only ``symbols.active``. It never changes ``enabled``, runtime signal
flags, or StockRank rows. The default is review mode; pass ``--apply`` to write
active membership.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import logging
import math
import os
import statistics
import sys
import time
import traceback
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal
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
GEN = SCANNER_CONFIG.universe_generation

# -----------------------------------------------------------------------------
# Visible source defaults. CLI values override these defaults.
# -----------------------------------------------------------------------------
DEFAULT_AS_OF: Optional[str] = None
DEFAULT_SYMBOLS: Optional[str] = None
DEFAULT_REPORT_DIR = GEN.report_dir
DEFAULT_APPLY = False
DEFAULT_WRITE_REPORT = True
DEFAULT_ACTIVE_LIMIT = GEN.active_limit
DEFAULT_CALENDAR_LOOKBACK_DAYS = GEN.calendar_lookback_days
DEFAULT_MINIMUM_HISTORY_DAYS = GEN.minimum_history_days
DEFAULT_HYSTERESIS_SLOTS = GEN.hysteresis_slots
DEFAULT_LOG_FILE = GEN.log_file


@dataclass(frozen=True)
class UniverseSymbol:
    symbol: str
    token: Optional[str]
    exchange: Optional[str]
    price: Optional[float]
    lotsize: int
    enabled: bool
    active: bool
    derivative_available: bool
    whitelisted: bool
    blacklisted: bool


@dataclass(frozen=True)
class HistoricalMetrics:
    history_days: int
    latest_close: float
    atr_pct: float
    median_range_pct: float
    median_turnover_lakh: float
    directional_efficiency: float
    active_day_ratio: float
    price_economics: float


@dataclass(frozen=True)
class UniverseCandidate:
    symbol: str
    enabled: bool
    current_active: bool
    whitelisted: bool
    blacklisted: bool
    derivative_available: bool
    valid: bool
    error: str

    history_days: int = 0
    latest_close: Optional[float] = None
    atr_pct: Optional[float] = None
    median_range_pct: Optional[float] = None
    median_turnover_lakh: Optional[float] = None
    directional_efficiency: Optional[float] = None
    active_day_ratio: Optional[float] = None
    price_economics: Optional[float] = None

    atr_score: float = 0.0
    range_score: float = 0.0
    turnover_score: float = 0.0
    efficiency_score: float = 0.0
    active_day_score: float = 0.0
    derivative_score: float = 0.0
    economics_score: float = 0.0
    total_score: float = 0.0
    base_rank: Optional[int] = None

    proposed_active: bool = False
    membership_action: str = "INACTIVE"
    selection_reason: str = ""


@dataclass(frozen=True)
class UniverseSelectionResult:
    candidates: tuple[UniverseCandidate, ...]
    selected_symbols: tuple[str, ...]
    retained_symbols: tuple[str, ...]
    added_symbols: tuple[str, ...]
    removed_symbols: tuple[str, ...]
    cutoff_score: Optional[float]
    enabled_count: int
    valid_count: int
    coverage_pct: float
    safe_to_apply: bool
    unsafe_reasons: tuple[str, ...]


def _norm(value: object) -> str:
    return str(value or "").strip().upper()


def _safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        value = float(value)
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _parse_symbol_filter(raw: Optional[str]) -> Optional[set[str]]:
    if not raw:
        return None
    values = {_norm(item) for item in raw.split(",") if _norm(item)}
    return values or None


def _parse_as_of(raw: Optional[str]) -> datetime:
    if not raw:
        return datetime.now(IST)
    parsed = date.fromisoformat(raw)
    return datetime.combine(parsed, dtime(23, 59, 59), IST)


def _parse_bar_time(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except Exception:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def _completed_bar_date(as_of: datetime) -> date:
    market_close = dtime.fromisoformat(GEN.market_close_time)
    local = as_of.astimezone(IST)
    if local.date() == datetime.now(IST).date() and local.time() < market_close:
        return local.date() - timedelta(days=1)
    return local.date()


def _economic_score(price: float) -> float:
    floor = float(GEN.economic_price_floor)
    ceiling = float(GEN.economic_price_ceiling)
    if price <= 0 or floor <= 0 or ceiling <= floor:
        return 0.0
    if floor <= price <= ceiling:
        return 1.0
    if price < floor:
        return max(0.0, price / floor)
    return max(0.0, ceiling / price)


def _load_universe(symbol_filter: Optional[set[str]]) -> list[UniverseSymbol]:
    whitelist = {_norm(value) for value in universe_whitelist()}
    blacklist = {_norm(value) for value in universe_blacklist()}

    with get_trades_db() as db:
        derivative_refs = {
            _norm(value)
            for (value,) in db.query(SymbolORM.equity_ref)
            .filter(SymbolORM.type.in_(("FUT", "CE", "PE")), SymbolORM.equity_ref.isnot(None))
            .distinct()
            .all()
            if _norm(value)
        }

        query = db.query(SymbolORM).filter(SymbolORM.type == "EQ")
        if symbol_filter:
            query = query.filter(SymbolORM.symbol.in_(sorted(symbol_filter)))
        rows = query.order_by(SymbolORM.symbol.asc()).all()

        return [
            UniverseSymbol(
                symbol=_norm(row.symbol),
                token=str(row.token).strip() if row.token not in (None, "") else None,
                exchange=_norm(row.exchange) or None,
                price=_safe_float(row.price),
                lotsize=max(1, int(row.lotsize or 1)),
                enabled=bool(row.enabled),
                active=bool(row.active),
                derivative_available=_norm(row.symbol) in derivative_refs,
                whitelisted=_norm(row.symbol) in whitelist,
                blacklisted=_norm(row.symbol) in blacklist,
            )
            for row in rows
        ]


def _kite_client() -> KiteConnectService:
    user = UserSchema.fetch_user(AppConfig.DATA_USER)
    if not user:
        raise RuntimeError(f"DATA_USER not found: {AppConfig.DATA_USER}")
    if not user.apikey or not user.access_token:
        raise RuntimeError(f"DATA_USER missing apikey/access_token: {AppConfig.DATA_USER}")
    return KiteConnectService(api_key=user.apikey, access_token=user.access_token)


def _normalise_daily_bars(raw_bars: Iterable[Mapping[str, Any]], completed_date: date) -> list[dict[str, float]]:
    bars: list[tuple[datetime, dict[str, float]]] = []
    for raw in raw_bars or []:
        timestamp = _parse_bar_time(raw.get("date"))
        if timestamp is None or timestamp.date() > completed_date:
            continue

        open_price = _safe_float(raw.get("open"))
        high = _safe_float(raw.get("high"))
        low = _safe_float(raw.get("low"))
        close = _safe_float(raw.get("close"))
        volume = _safe_float(raw.get("volume")) or 0.0
        if not all(value is not None and value > 0 for value in (open_price, high, low, close)):
            continue
        if high < low:
            continue

        bars.append(
            (
                timestamp,
                {
                    "open": float(open_price),
                    "high": float(high),
                    "low": float(low),
                    "close": float(close),
                    "volume": max(0.0, float(volume)),
                },
            )
        )

    bars.sort(key=lambda item: item[0])
    return [row for _, row in bars]


def calculate_historical_metrics(
    bars: Sequence[Mapping[str, float]],
    minimum_history_days: int,
    atr_period: int,
) -> HistoricalMetrics:
    if len(bars) < minimum_history_days:
        raise ValueError(f"insufficient_history:{len(bars)}<{minimum_history_days}")
    if atr_period < 2 or len(bars) <= atr_period:
        raise ValueError("insufficient_history_for_atr")

    closes = [float(row["close"]) for row in bars]
    ranges_pct: list[float] = []
    turnover_lakh: list[float] = []
    true_ranges: list[float] = []
    active_days = 0

    previous_close: Optional[float] = None
    for row in bars:
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        volume = float(row.get("volume", 0.0))
        anchor = previous_close if previous_close and previous_close > 0 else close

        day_range_pct = ((high - low) / anchor) * 100.0
        ranges_pct.append(day_range_pct)
        turnover_lakh.append((volume * close) / 100000.0)
        if day_range_pct >= float(GEN.minimum_active_range_pct) and volume > 0:
            active_days += 1

        if previous_close is None:
            true_range = high - low
        else:
            true_range = max(high - low, abs(high - previous_close), abs(low - previous_close))
        true_ranges.append(true_range)
        previous_close = close

    latest_close = closes[-1]
    atr_value = statistics.fmean(true_ranges[-atr_period:])
    atr_pct = (atr_value / latest_close) * 100.0

    path = sum(abs(closes[index] - closes[index - 1]) for index in range(1, len(closes)))
    directional_efficiency = abs(closes[-1] - closes[0]) / path if path > 0 else 0.0

    return HistoricalMetrics(
        history_days=len(bars),
        latest_close=latest_close,
        atr_pct=atr_pct,
        median_range_pct=statistics.median(ranges_pct),
        median_turnover_lakh=statistics.median(turnover_lakh),
        directional_efficiency=max(0.0, min(directional_efficiency, 1.0)),
        active_day_ratio=active_days / len(bars),
        price_economics=_economic_score(latest_close),
    )


def _fetch_candidate(
    kite: KiteConnectService,
    symbol: UniverseSymbol,
    as_of: datetime,
    lookback_days: int,
    minimum_history_days: int,
    atr_period: int,
) -> UniverseCandidate:
    if not symbol.enabled:
        return UniverseCandidate(
            symbol=symbol.symbol,
            enabled=False,
            current_active=symbol.active,
            whitelisted=symbol.whitelisted,
            blacklisted=symbol.blacklisted,
            derivative_available=symbol.derivative_available,
            valid=False,
            error="disabled_by_policy",
        )
    if symbol.blacklisted and not symbol.whitelisted:
        return UniverseCandidate(
            symbol=symbol.symbol,
            enabled=True,
            current_active=symbol.active,
            whitelisted=False,
            blacklisted=True,
            derivative_available=symbol.derivative_available,
            valid=False,
            error="enabled_blacklist_policy_mismatch",
        )
    if not symbol.token:
        return UniverseCandidate(
            symbol=symbol.symbol,
            enabled=True,
            current_active=symbol.active,
            whitelisted=symbol.whitelisted,
            blacklisted=symbol.blacklisted,
            derivative_available=symbol.derivative_available,
            valid=False,
            error="missing_token",
        )

    from_date = as_of - timedelta(days=max(1, int(lookback_days)))
    raw = kite.fetch_historical_data(
        instrument_token=int(symbol.token),
        from_date=from_date,
        to_date=as_of,
        interval=GEN.historical_interval,
        oi=False,
    ) or []
    bars = _normalise_daily_bars(raw, _completed_bar_date(as_of))
    metrics = calculate_historical_metrics(bars, minimum_history_days, atr_period)

    return UniverseCandidate(
        symbol=symbol.symbol,
        enabled=True,
        current_active=symbol.active,
        whitelisted=symbol.whitelisted,
        blacklisted=symbol.blacklisted,
        derivative_available=symbol.derivative_available,
        valid=True,
        error="",
        history_days=metrics.history_days,
        latest_close=metrics.latest_close,
        atr_pct=metrics.atr_pct,
        median_range_pct=metrics.median_range_pct,
        median_turnover_lakh=metrics.median_turnover_lakh,
        directional_efficiency=metrics.directional_efficiency,
        active_day_ratio=metrics.active_day_ratio,
        price_economics=metrics.price_economics,
    )


def _percentile_map(candidates: Sequence[UniverseCandidate], field: str) -> dict[str, float]:
    values = sorted(float(getattr(row, field)) for row in candidates if getattr(row, field) is not None)
    if not values:
        return {}
    if len(values) == 1:
        return {row.symbol: 1.0 for row in candidates if getattr(row, field) is not None}

    out: dict[str, float] = {}
    denominator = len(values) - 1
    for row in candidates:
        value = getattr(row, field)
        if value is None:
            continue
        left = bisect.bisect_left(values, float(value))
        right = bisect.bisect_right(values, float(value)) - 1
        average_rank = (left + right) / 2.0
        out[row.symbol] = average_rank / denominator
    return out


def score_candidates(candidates: Sequence[UniverseCandidate]) -> list[UniverseCandidate]:
    valid = [row for row in candidates if row.valid]
    atr_scores = _percentile_map(valid, "atr_pct")
    range_scores = _percentile_map(valid, "median_range_pct")
    turnover_scores = _percentile_map(valid, "median_turnover_lakh")
    efficiency_scores = _percentile_map(valid, "directional_efficiency")
    active_day_scores = _percentile_map(valid, "active_day_ratio")

    weights = {
        "atr": float(GEN.weight_atr_pct),
        "range": float(GEN.weight_median_range_pct),
        "turnover": float(GEN.weight_turnover),
        "efficiency": float(GEN.weight_directional_efficiency),
        "active_day": float(GEN.weight_active_day_ratio),
        "derivative": float(GEN.weight_derivative_availability),
        "economics": float(GEN.weight_price_economics),
    }
    if not math.isclose(sum(weights.values()), 1.0, abs_tol=1e-9):
        raise ValueError(f"universe_generation_weights_must_sum_to_1:{sum(weights.values())}")

    scored: list[UniverseCandidate] = []
    for row in candidates:
        if not row.valid:
            scored.append(row)
            continue

        atr_score = atr_scores[row.symbol]
        range_score = range_scores[row.symbol]
        turnover_score = turnover_scores[row.symbol]
        efficiency_score = efficiency_scores[row.symbol]
        active_day_score = active_day_scores[row.symbol]
        derivative_score = 1.0 if row.derivative_available else 0.0
        economics_score = float(row.price_economics or 0.0)
        total = (
            weights["atr"] * atr_score
            + weights["range"] * range_score
            + weights["turnover"] * turnover_score
            + weights["efficiency"] * efficiency_score
            + weights["active_day"] * active_day_score
            + weights["derivative"] * derivative_score
            + weights["economics"] * economics_score
        )
        scored.append(
            replace(
                row,
                atr_score=atr_score,
                range_score=range_score,
                turnover_score=turnover_score,
                efficiency_score=efficiency_score,
                active_day_score=active_day_score,
                derivative_score=derivative_score,
                economics_score=economics_score,
                total_score=total,
            )
        )

    valid_sorted = sorted(
        (row for row in scored if row.valid),
        key=lambda row: (-row.total_score, row.symbol),
    )
    ranks = {row.symbol: rank for rank, row in enumerate(valid_sorted, start=1)}
    return [replace(row, base_rank=ranks.get(row.symbol)) for row in scored]


def select_active_universe(
    candidates: Sequence[UniverseCandidate],
    active_limit: int,
    hysteresis_slots: int,
    minimum_coverage_pct: float,
) -> UniverseSelectionResult:
    enabled = [row for row in candidates if row.enabled and not (row.blacklisted and not row.whitelisted)]
    valid = [row for row in enabled if row.valid]
    forced = sorted((row for row in enabled if row.whitelisted), key=lambda row: row.symbol)

    unsafe: list[str] = []
    if active_limit <= 0:
        unsafe.append("active_limit_must_be_positive")
    if len(forced) > active_limit:
        unsafe.append(f"whitelist_exceeds_active_limit:{len(forced)}>{active_limit}")

    coverage_pct = (len(valid) / len(enabled) * 100.0) if enabled else 0.0
    if coverage_pct < minimum_coverage_pct:
        unsafe.append(f"coverage_below_minimum:{coverage_pct:.2f}<{minimum_coverage_pct:.2f}")

    forced_symbols = {row.symbol for row in forced}
    capacity = max(0, active_limit - len(forced_symbols))
    dynamic = sorted(
        (row for row in valid if row.symbol not in forced_symbols),
        key=lambda row: (-row.total_score, row.symbol),
    )

    retention_window = dynamic[: capacity + max(0, hysteresis_slots)]
    retained_dynamic = [row for row in retention_window if row.current_active]
    retained_dynamic.sort(key=lambda row: (-row.total_score, row.symbol))
    selected_dynamic = retained_dynamic[:capacity]
    selected_dynamic_symbols = {row.symbol for row in selected_dynamic}

    for row in dynamic:
        if len(selected_dynamic) >= capacity:
            break
        if row.symbol in selected_dynamic_symbols:
            continue
        selected_dynamic.append(row)
        selected_dynamic_symbols.add(row.symbol)

    selected_symbols = forced_symbols | selected_dynamic_symbols
    current_active = {row.symbol for row in candidates if row.current_active}
    additions = sorted(selected_symbols - current_active)
    removals = sorted(current_active - selected_symbols)
    retained = sorted(selected_symbols & current_active)

    selected_non_whitelist = [row for row in selected_dynamic if row.symbol in selected_symbols]
    cutoff_score = min((row.total_score for row in selected_non_whitelist), default=None)
    retention_symbols = {row.symbol for row in retained_dynamic[:capacity]}

    annotated: list[UniverseCandidate] = []
    for row in candidates:
        proposed = row.symbol in selected_symbols
        if proposed and row.whitelisted:
            reason = "WHITELIST"
        elif proposed and row.symbol in retention_symbols:
            reason = "RETAINED_HYSTERESIS"
        elif proposed:
            reason = "SELECTED_SCORE"
        elif not row.valid:
            reason = row.error or "INVALID_HISTORY"
        elif row.blacklisted and not row.whitelisted:
            reason = "BLACKLIST"
        else:
            reason = "BELOW_CUTOFF"

        if proposed and row.current_active:
            action = "RETAIN"
        elif proposed:
            action = "ADD"
        elif row.current_active:
            action = "REMOVE"
        else:
            action = "INACTIVE"

        annotated.append(
            replace(
                row,
                proposed_active=proposed,
                membership_action=action,
                selection_reason=reason,
            )
        )

    return UniverseSelectionResult(
        candidates=tuple(sorted(annotated, key=lambda row: (not row.proposed_active, row.base_rank or 10**9, row.symbol))),
        selected_symbols=tuple(sorted(selected_symbols)),
        retained_symbols=tuple(retained),
        added_symbols=tuple(additions),
        removed_symbols=tuple(removals),
        cutoff_score=cutoff_score,
        enabled_count=len(enabled),
        valid_count=len(valid),
        coverage_pct=coverage_pct,
        safe_to_apply=not unsafe,
        unsafe_reasons=tuple(unsafe),
    )


def _apply_active_selection(selected_symbols: Sequence[str]) -> dict[str, int]:
    selected = {_norm(symbol) for symbol in selected_symbols if _norm(symbol)}
    changed = 0
    activated = 0
    deactivated = 0

    with get_trades_db() as db:
        rows = db.query(SymbolORM).filter(SymbolORM.type == "EQ").all()
        for row in rows:
            target = _norm(row.symbol) in selected
            before = bool(row.active)
            if before != target:
                row.active = target
                changed += 1
                if target:
                    activated += 1
                else:
                    deactivated += 1
        db.commit()

    return {
        "changed": changed,
        "activated": activated,
        "deactivated": deactivated,
        "selected": len(selected),
    }


def _write_report(report_dir: str, result: UniverseSelectionResult, run_time: datetime) -> Path:
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"generate_stock_universe_{run_time.strftime('%Y%m%d_%H%M%S')}.csv"
    rows = [asdict(row) for row in result.candidates]
    fieldnames = list(rows[0].keys()) if rows else ["symbol"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Review or apply the long-horizon active EQ universe."
    )
    parser.add_argument("--as-of", default=DEFAULT_AS_OF, help="Historical cutoff date YYYY-MM-DD.")
    parser.add_argument(
        "--symbols",
        default=DEFAULT_SYMBOLS,
        help="Optional comma-separated EQ symbols for focused diagnostics. Apply is blocked for focused runs.",
    )
    parser.add_argument("--active-limit", type=int, default=DEFAULT_ACTIVE_LIMIT)
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_CALENDAR_LOOKBACK_DAYS)
    parser.add_argument("--minimum-history-days", type=int, default=DEFAULT_MINIMUM_HISTORY_DAYS)
    parser.add_argument("--hysteresis-slots", type=int, default=DEFAULT_HYSTERESIS_SLOTS)
    parser.add_argument("--report-dir", default=DEFAULT_REPORT_DIR)
    parser.add_argument("--no-report", action="store_true")
    parser.add_argument(
        "--apply",
        action="store_true",
        default=DEFAULT_APPLY,
        help="Persist proposed active flags. Default is review only.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    setup_logging(log_file=DEFAULT_LOG_FILE)

    run_time = datetime.now(IST)
    as_of = _parse_as_of(args.as_of)
    symbol_filter = _parse_symbol_filter(args.symbols)

    logger.info(
        "GENERATE_STOCK_UNIVERSE_START | as_of=%s | apply=%s | active_limit=%d | lookback_days=%d | minimum_history_days=%d | hysteresis_slots=%d | focused_symbols=%s",
        as_of.isoformat(),
        bool(args.apply),
        int(args.active_limit),
        int(args.lookback_days),
        int(args.minimum_history_days),
        int(args.hysteresis_slots),
        len(symbol_filter or []),
    )

    try:
        universe = _load_universe(symbol_filter)
    except Exception:
        logger.exception("GENERATE_STOCK_UNIVERSE_DB_PREFLIGHT_FAILED")
        return 2

    if not universe:
        logger.error("GENERATE_STOCK_UNIVERSE_ABORT | reason=no_eq_symbols")
        return 2
    if args.apply and symbol_filter:
        logger.error("GENERATE_STOCK_UNIVERSE_ABORT | reason=apply_not_allowed_for_focused_symbols")
        return 2

    disabled_whitelist = sorted(row.symbol for row in universe if row.whitelisted and not row.enabled)
    enabled_blacklist = sorted(
        row.symbol for row in universe if row.enabled and row.blacklisted and not row.whitelisted
    )
    if disabled_whitelist:
        logger.error(
            "GENERATE_STOCK_UNIVERSE_POLICY_MISMATCH | disabled_whitelist=%s | run filter_stock_universe first",
            disabled_whitelist,
        )
    if enabled_blacklist:
        logger.error(
            "GENERATE_STOCK_UNIVERSE_POLICY_MISMATCH | enabled_blacklist=%s | run filter_stock_universe first",
            enabled_blacklist,
        )

    try:
        kite = _kite_client()
    except Exception:
        logger.exception("GENERATE_STOCK_UNIVERSE_BROKER_PREFLIGHT_FAILED")
        return 2

    candidates: list[UniverseCandidate] = []
    failures: list[dict[str, str]] = []
    for index, symbol in enumerate(universe, start=1):
        try:
            candidate = _fetch_candidate(
                kite,
                symbol,
                as_of,
                int(args.lookback_days),
                int(args.minimum_history_days),
                int(GEN.atr_period),
            )
        except Exception as exc:
            logger.error(
                "GENERATE_STOCK_UNIVERSE_SYMBOL_FAILED | symbol=%s | error=%s\n%s",
                symbol.symbol,
                exc,
                traceback.format_exc(),
            )
            failures.append({"symbol": symbol.symbol, "error": str(exc)})
            candidate = UniverseCandidate(
                symbol=symbol.symbol,
                enabled=symbol.enabled,
                current_active=symbol.active,
                whitelisted=symbol.whitelisted,
                blacklisted=symbol.blacklisted,
                derivative_available=symbol.derivative_available,
                valid=False,
                error=str(exc),
            )
        candidates.append(candidate)

        if index < len(universe) and float(GEN.historical_rate_sleep_sec) > 0:
            time.sleep(float(GEN.historical_rate_sleep_sec))

    try:
        scored = score_candidates(candidates)
        result = select_active_universe(
            scored,
            active_limit=int(args.active_limit),
            hysteresis_slots=int(args.hysteresis_slots),
            minimum_coverage_pct=float(GEN.minimum_coverage_pct),
        )
    except Exception:
        logger.exception("GENERATE_STOCK_UNIVERSE_SELECTION_FAILED")
        return 2

    unsafe_reasons = list(result.unsafe_reasons)
    if disabled_whitelist:
        unsafe_reasons.append("disabled_whitelist_policy_mismatch")
    if enabled_blacklist:
        unsafe_reasons.append("enabled_blacklist_policy_mismatch")
    safe_to_apply = result.safe_to_apply and not disabled_whitelist and not enabled_blacklist

    report_path: Optional[Path] = None
    if DEFAULT_WRITE_REPORT and not args.no_report:
        try:
            report_path = _write_report(args.report_dir, result, run_time)
        except Exception:
            logger.exception("GENERATE_STOCK_UNIVERSE_REPORT_FAILED")
            return 2

    apply_result: Optional[dict[str, int]] = None
    if args.apply:
        if not safe_to_apply:
            logger.error(
                "GENERATE_STOCK_UNIVERSE_APPLY_BLOCKED | reasons=%s",
                unsafe_reasons,
            )
            return 2
        try:
            apply_result = _apply_active_selection(result.selected_symbols)
        except Exception:
            logger.exception("GENERATE_STOCK_UNIVERSE_APPLY_FAILED")
            return 2

    summary = {
        "mode": "APPLY" if args.apply else "REVIEW",
        "as_of": as_of.isoformat(),
        "enabled_count": result.enabled_count,
        "valid_count": result.valid_count,
        "coverage_pct": round(result.coverage_pct, 4),
        "selected_count": len(result.selected_symbols),
        "retained_count": len(result.retained_symbols),
        "added_count": len(result.added_symbols),
        "removed_count": len(result.removed_symbols),
        "cutoff_score": result.cutoff_score,
        "safe_to_apply": safe_to_apply,
        "unsafe_reasons": unsafe_reasons,
        "symbol_failures": failures,
        "apply_result": apply_result,
        "report": str(report_path) if report_path else None,
    }
    logger.info("GENERATE_STOCK_UNIVERSE_SUMMARY | %s", json.dumps(summary, sort_keys=True))
    logger.info("GENERATE_STOCK_UNIVERSE_ADDITIONS | %s", ", ".join(result.added_symbols) or "NONE")
    logger.info("GENERATE_STOCK_UNIVERSE_REMOVALS | %s", ", ".join(result.removed_symbols) or "NONE")

    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
