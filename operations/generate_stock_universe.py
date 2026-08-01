#!/usr/bin/env python3
"""Review or apply the curated active EQ universe.

The operation has one responsibility: reduce the persisted ``enabled=True`` EQ
universe to a configurable, stable active observation universe.  It never
re-evaluates filter policy and never writes ``symbols.enabled``.

Selection uses the most recent completed trading-day window and favours stocks
that move usefully and repeatedly rather than stocks dominated by a few event
spikes.  The same run also reports how often the proposed universe captured the
daily top movers over that historical window.  Two supplemental report sections
review policy-disabled frequent movers and top movers whose useful move was
mostly completed in the first 45 minutes; neither section changes selection.

Operational membership is always::

    symbols.enabled = True AND symbols.active = True

The default is review mode.  Pass ``--apply`` to update ``symbols.active`` for
enabled EQ symbols only.
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
from utils.datetime_utils import IST, current_fo_expiry
from utils.universe_policy import universe_whitelist

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
DEFAULT_AUDIT_TRADING_DAYS = GEN.audit_trading_days
DEFAULT_TOP_MOVERS_PER_DAY = GEN.top_movers_per_day
DEFAULT_CALENDAR_LOOKBACK_DAYS = GEN.calendar_lookback_days
DEFAULT_MINIMUM_HISTORY_DAYS = GEN.minimum_history_days
DEFAULT_HYSTERESIS_SCORE_GAP = GEN.hysteresis_score_gap
DEFAULT_LOG_FILE = GEN.log_file


@dataclass(frozen=True)
class UniverseSymbol:
    symbol: str
    token: Optional[str]
    exchange: Optional[str]
    enabled: bool
    active: bool
    derivative_available: bool
    whitelisted: bool

    @property
    def operationally_active(self) -> bool:
        return bool(self.enabled and self.active)


@dataclass(frozen=True)
class HistoricalMetrics:
    history_days: int
    latest_bar_date: date
    latest_close: float
    atr_pct: float
    median_excursion_pct: float
    p90_excursion_pct: float
    median_turnover_lakh: float
    directional_efficiency: float
    movement_consistency: float


@dataclass(frozen=True)
class MoverStats:
    top20_days: int = 0
    top20_weeks: int = 0
    top20_best_rank: Optional[int] = None
    top20_average_rank: Optional[float] = None
    top20_last_seen_date: Optional[date] = None


@dataclass(frozen=True)
class PolicyMoverBehavior:
    symbol: str
    top20_days_60d: int
    top20_weeks_60d: int
    top20_best_rank_60d: Optional[int]
    top20_average_rank_60d: Optional[float]
    top20_last_seen_date: Optional[date]
    daily_history_days: int
    median_excursion_pct: float
    directional_efficiency: float
    movement_consistency: float
    intraday_days_available: int
    median_close_retention_ratio: float
    median_peak_retracement_ratio: float
    reversal_prone_days: int
    reversal_prone_pct: float
    full_reversal_days: int
    full_reversal_pct: float
    median_two_sided_ratio: float
    two_sided_days: int
    two_sided_pct: float
    gap_driven_days: int
    gap_driven_pct: float
    maximum_week_share_pct: float
    behavior_classification: str
    error: str = ""


@dataclass(frozen=True)
class EarlyMoveStats:
    symbol: str
    top20_days_60d: int
    intraday_days_available: int
    meaningful_intraday_days: int
    early_move_only_days: int
    early_move_only_pct: float
    median_early_move_share_pct: float
    median_post_range_extension_pct: float
    median_post_contained_pct: float
    late_opportunity_days: int
    late_opportunity_pct: float
    first_intraday_date: Optional[date]
    last_intraday_date: Optional[date]
    early_move_classification: str
    error: str = ""


@dataclass(frozen=True)
class IntradayDayProfile:
    bar_date: date
    total_excursion_pct: float
    dominant_direction: str
    early_excursion_pct: float
    early_move_share_pct: float
    post_range_extension_pct: float
    post_contained_pct: float
    close_retention_ratio: float
    peak_retracement_ratio: float
    full_reversal: bool
    two_sided_ratio: float


@dataclass(frozen=True)
class UniverseCandidate:
    symbol: str
    enabled: bool
    active_flag_before: bool
    operationally_active_before: bool
    whitelisted: bool
    derivative_available: bool
    selection_eligible: bool
    valid_history: bool
    error: str

    history_days: int = 0
    latest_bar_date: Optional[date] = None
    history_staleness_days: Optional[int] = None
    latest_close: Optional[float] = None
    atr_pct: Optional[float] = None
    median_excursion_pct: Optional[float] = None
    p90_excursion_pct: Optional[float] = None
    median_turnover_lakh: Optional[float] = None
    directional_efficiency: Optional[float] = None
    movement_consistency: Optional[float] = None

    top20_days_60d: int = 0
    top20_frequency_pct_60d: float = 0.0
    top20_weeks_60d: int = 0
    top20_week_frequency_pct_60d: float = 0.0
    top20_best_rank_60d: Optional[int] = None
    top20_average_rank_60d: Optional[float] = None
    top20_last_seen_date: Optional[date] = None

    excursion_score: float = 0.0
    turnover_score: float = 0.0
    atr_score: float = 0.0
    efficiency_score: float = 0.0
    top20_days_score: float = 0.0
    top20_weeks_score: float = 0.0
    total_score: float = 0.0
    base_rank: Optional[int] = None

    proposed_active: bool = False
    active_action: str = "REMAIN_INACTIVE"
    selection_reason: str = ""
    missed_mover_reason: str = ""


@dataclass(frozen=True)
class UniverseSelectionResult:
    candidates: tuple[UniverseCandidate, ...]
    selected_symbols: tuple[str, ...]
    retained_symbols: tuple[str, ...]
    added_symbols: tuple[str, ...]
    removed_symbols: tuple[str, ...]
    cutoff_score: Optional[float]
    target_count: int
    total_eq_count: int
    enabled_count: int
    current_operational_active_count: int
    derivative_eligible_count: int
    valid_candidate_count: int
    history_coverage_pct: float
    derivative_coverage_pct: float
    critical_invalid_symbols: tuple[str, ...]
    safe_to_apply: bool
    unsafe_reasons: tuple[str, ...]


@dataclass(frozen=True)
class MoverAuditSummary:
    audit_dates: tuple[date, ...]
    audit_weeks: int
    total_top_mover_appearances: int
    captured_appearances: int
    policy_disabled_appearances: int
    enabled_not_selected_appearances: int
    missing_derivative_appearances: int
    insufficient_history_appearances: int
    capture_pct: float


@dataclass(frozen=True)
class SymbolHistory:
    symbol: UniverseSymbol
    bars: tuple[dict[str, Any], ...]
    error: str = ""


@dataclass(frozen=True)
class IntradayHistory:
    symbol: UniverseSymbol
    bars: tuple[dict[str, Any], ...]
    error: str = ""


@dataclass(frozen=True)
class WhitelistPreflight:
    missing_symbols: tuple[str, ...]
    disabled_symbols: tuple[str, ...]
    missing_derivative_symbols: tuple[str, ...]

    @property
    def safe_to_apply(self) -> bool:
        return not (
            self.missing_symbols
            or self.disabled_symbols
            or self.missing_derivative_symbols
        )


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


def _completed_bar_date(as_of: datetime, *, now: Optional[datetime] = None) -> date:
    market_close = dtime.fromisoformat(GEN.market_close_time)
    local_as_of = as_of.astimezone(IST)
    local_now = (now or datetime.now(IST)).astimezone(IST)

    if local_as_of.date() > local_now.date():
        raise ValueError(f"as_of_in_future:{local_as_of.date().isoformat()}")
    if local_as_of.date() == local_now.date() and local_now.time() < market_close:
        return local_as_of.date() - timedelta(days=1)
    return local_as_of.date()


def _history_staleness_days(
    latest_bar_date: Optional[date],
    completed_date: date,
    maximum_history_staleness_days: int,
) -> int:
    if latest_bar_date is None:
        raise ValueError("missing_latest_bar_date")
    staleness_days = (completed_date - latest_bar_date).days
    if staleness_days < 0:
        raise ValueError(f"history_after_completed_date:{latest_bar_date}")
    if staleness_days > maximum_history_staleness_days:
        raise ValueError(
            "stale_history:"
            f"latest={latest_bar_date.isoformat()}:"
            f"completed={completed_date.isoformat()}:"
            f"days={staleness_days}>{maximum_history_staleness_days}"
        )
    return staleness_days


def _load_universe(symbol_filter: Optional[set[str]]) -> list[UniverseSymbol]:
    whitelist = {_norm(value) for value in universe_whitelist()}

    with get_trades_db() as db:
        derivative_refs = {
            _norm(value)
            for (value,) in db.query(SymbolORM.equity_ref)
            .filter(
                SymbolORM.enabled == True,
                SymbolORM.type.in_(("FUT", "CE", "PE")),
                SymbolORM.equity_ref.isnot(None),
                SymbolORM.expiry == current_fo_expiry(),
            )
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
                enabled=bool(row.enabled),
                active=bool(row.active),
                derivative_available=_norm(row.symbol) in derivative_refs,
                whitelisted=_norm(row.symbol) in whitelist,
            )
            for row in rows
        ]


def inspect_whitelist_preflight(universe: Sequence[UniverseSymbol]) -> WhitelistPreflight:
    whitelist = {_norm(value) for value in universe_whitelist()}
    by_symbol = {row.symbol: row for row in universe}
    missing = sorted(whitelist - set(by_symbol))
    disabled = sorted(
        symbol for symbol in whitelist if symbol in by_symbol and not by_symbol[symbol].enabled
    )
    missing_derivative = sorted(
        symbol
        for symbol in whitelist
        if symbol in by_symbol and not by_symbol[symbol].derivative_available
    )
    return WhitelistPreflight(
        missing_symbols=tuple(missing),
        disabled_symbols=tuple(disabled),
        missing_derivative_symbols=tuple(missing_derivative),
    )


def _kite_client() -> KiteConnectService:
    user = UserSchema.fetch_user(AppConfig.DATA_USER)
    if not user:
        raise RuntimeError(f"DATA_USER not found: {AppConfig.DATA_USER}")
    if not user.apikey or not user.access_token:
        raise RuntimeError(f"DATA_USER missing apikey/access_token: {AppConfig.DATA_USER}")
    return KiteConnectService(api_key=user.apikey, access_token=user.access_token)


def _normalise_daily_bars(
    raw_bars: Iterable[Mapping[str, Any]], completed_date: date
) -> list[dict[str, Any]]:
    bars: list[tuple[datetime, dict[str, Any]]] = []
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
        if high < low or high < open_price or high < close or low > open_price or low > close:
            continue

        upward_excursion_pct = ((high - open_price) / open_price) * 100.0
        downward_excursion_pct = ((open_price - low) / open_price) * 100.0
        excursion_pct = max(upward_excursion_pct, downward_excursion_pct)
        follow_through_pct = abs(close - open_price) / open_price * 100.0

        bars.append(
            (
                timestamp,
                {
                    "bar_date": timestamp.date(),
                    "open": float(open_price),
                    "high": float(high),
                    "low": float(low),
                    "close": float(close),
                    "volume": max(0.0, float(volume)),
                    "excursion_pct": float(excursion_pct),
                    "follow_through_pct": float(follow_through_pct),
                },
            )
        )

    bars.sort(key=lambda item: item[0])
    deduplicated: dict[date, dict[str, Any]] = {}
    for _, row in bars:
        deduplicated[row["bar_date"]] = row
    return [deduplicated[key] for key in sorted(deduplicated)]


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        raise ValueError("empty_values")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def calculate_historical_metrics(
    bars: Sequence[Mapping[str, Any]],
    minimum_history_days: int,
    atr_period: int,
) -> HistoricalMetrics:
    if len(bars) < minimum_history_days:
        raise ValueError(f"insufficient_history:{len(bars)}<{minimum_history_days}")
    if atr_period < 2 or len(bars) <= atr_period:
        raise ValueError("insufficient_history_for_atr")

    closes = [float(row["close"]) for row in bars]
    excursions = [float(row["excursion_pct"]) for row in bars]
    turnover_lakh = [
        (float(row.get("volume", 0.0)) * float(row["close"])) / 100000.0
        for row in bars
    ]
    true_ranges: list[float] = []
    previous_close: Optional[float] = None

    for row in bars:
        high = float(row["high"])
        low = float(row["low"])
        if previous_close is None:
            true_range = high - low
        else:
            true_range = max(high - low, abs(high - previous_close), abs(low - previous_close))
        true_ranges.append(true_range)
        previous_close = float(row["close"])

    latest_close = closes[-1]
    atr_value = statistics.fmean(true_ranges[-atr_period:])
    atr_pct = (atr_value / latest_close) * 100.0

    close_path = sum(
        abs(closes[index] - closes[index - 1]) for index in range(1, len(closes))
    )
    directional_efficiency = abs(closes[-1] - closes[0]) / close_path if close_path > 0 else 0.0

    median_excursion = statistics.median(excursions)
    p90_excursion = _percentile(excursions, 0.90)
    movement_consistency = median_excursion / p90_excursion if p90_excursion > 0 else 0.0

    latest_bar_date_value = bars[-1].get("bar_date")
    if not isinstance(latest_bar_date_value, date):
        raise ValueError("missing_latest_bar_date")

    return HistoricalMetrics(
        history_days=len(bars),
        latest_bar_date=latest_bar_date_value,
        latest_close=latest_close,
        atr_pct=atr_pct,
        median_excursion_pct=median_excursion,
        p90_excursion_pct=p90_excursion,
        median_turnover_lakh=statistics.median(turnover_lakh),
        directional_efficiency=max(0.0, min(directional_efficiency, 1.0)),
        movement_consistency=max(0.0, min(movement_consistency, 1.0)),
    )


def _fetch_symbol_history(
    kite: KiteConnectService,
    symbol: UniverseSymbol,
    as_of: datetime,
    completed_date: date,
    lookback_days: int,
) -> SymbolHistory:
    if not symbol.token:
        return SymbolHistory(symbol=symbol, bars=(), error="missing_token")

    from_date = as_of - timedelta(days=max(1, int(lookback_days)))
    raw = kite.fetch_historical_data(
        instrument_token=int(symbol.token),
        from_date=from_date,
        to_date=as_of,
        interval=GEN.historical_interval,
        oi=False,
    ) or []
    bars = _normalise_daily_bars(raw, completed_date)
    if not bars:
        return SymbolHistory(symbol=symbol, bars=(), error="missing_history")
    return SymbolHistory(symbol=symbol, bars=tuple(bars), error="")


def _normalise_intraday_bars(
    raw_bars: Iterable[Mapping[str, Any]],
    audit_dates: Sequence[date],
) -> list[dict[str, Any]]:
    allowed_dates = set(audit_dates)
    market_open = dtime.fromisoformat(GEN.market_open_time)
    market_close = dtime.fromisoformat(GEN.market_close_time)
    rows: list[tuple[datetime, dict[str, Any]]] = []

    for raw in raw_bars or []:
        timestamp = _parse_bar_time(raw.get("date"))
        if timestamp is None or timestamp.date() not in allowed_dates:
            continue
        if timestamp.time() < market_open or timestamp.time() >= market_close:
            continue

        open_price = _safe_float(raw.get("open"))
        high = _safe_float(raw.get("high"))
        low = _safe_float(raw.get("low"))
        close = _safe_float(raw.get("close"))
        volume = _safe_float(raw.get("volume")) or 0.0
        if not all(value is not None and value > 0 for value in (open_price, high, low, close)):
            continue
        if high < low or high < open_price or high < close or low > open_price or low > close:
            continue
        rows.append(
            (
                timestamp,
                {
                    "timestamp": timestamp,
                    "bar_date": timestamp.date(),
                    "open": float(open_price),
                    "high": float(high),
                    "low": float(low),
                    "close": float(close),
                    "volume": max(0.0, float(volume)),
                },
            )
        )

    rows.sort(key=lambda item: item[0])
    return [row for _, row in rows]


def _fetch_intraday_history(
    kite: KiteConnectService,
    symbol: UniverseSymbol,
    as_of: datetime,
    audit_dates: Sequence[date],
    lookback_days: int,
) -> IntradayHistory:
    if not symbol.token:
        return IntradayHistory(symbol=symbol, bars=(), error="missing_token")

    from_date = as_of - timedelta(days=max(1, int(lookback_days)))
    raw = kite.fetch_historical_data(
        instrument_token=int(symbol.token),
        from_date=from_date,
        to_date=as_of,
        interval=GEN.intraday_interval,
        oi=False,
    ) or []
    bars = _normalise_intraday_bars(raw, audit_dates)
    if not bars:
        return IntradayHistory(symbol=symbol, bars=(), error="missing_intraday_history")
    return IntradayHistory(symbol=symbol, bars=tuple(bars), error="")


def _audit_dates(
    histories: Sequence[SymbolHistory], audit_trading_days: int
) -> tuple[date, ...]:
    dates = sorted(
        {
            row["bar_date"]
            for history in histories
            for row in history.bars
            if isinstance(row.get("bar_date"), date)
        }
    )
    if len(dates) < audit_trading_days:
        raise ValueError(f"insufficient_common_calendar:{len(dates)}<{audit_trading_days}")
    return tuple(dates[-audit_trading_days:])


def build_top_mover_stats(
    histories: Sequence[SymbolHistory],
    audit_dates: Sequence[date],
    top_movers_per_day: int,
) -> tuple[dict[str, MoverStats], dict[date, tuple[tuple[str, int, float], ...]]]:
    if top_movers_per_day <= 0:
        raise ValueError("top_movers_per_day_must_be_positive")

    allowed_dates = set(audit_dates)
    by_date: dict[date, list[tuple[str, float]]] = {day: [] for day in audit_dates}
    for history in histories:
        for bar in history.bars:
            bar_date = bar.get("bar_date")
            if bar_date not in allowed_dates:
                continue
            excursion = _safe_float(bar.get("excursion_pct"))
            if excursion is None:
                continue
            by_date[bar_date].append((history.symbol.symbol, excursion))

    daily_ranked: dict[date, tuple[tuple[str, int, float], ...]] = {}
    appearances: dict[str, list[tuple[date, int]]] = {}
    for day in audit_dates:
        ranked = sorted(by_date.get(day, []), key=lambda item: (-item[1], item[0]))
        selected = ranked[: min(top_movers_per_day, len(ranked))]
        rows = tuple((symbol, rank, excursion) for rank, (symbol, excursion) in enumerate(selected, start=1))
        daily_ranked[day] = rows
        for symbol, rank, _ in rows:
            appearances.setdefault(symbol, []).append((day, rank))

    stats: dict[str, MoverStats] = {}
    for history in histories:
        rows = appearances.get(history.symbol.symbol, [])
        if not rows:
            stats[history.symbol.symbol] = MoverStats()
            continue
        dates = [day for day, _ in rows]
        ranks = [rank for _, rank in rows]
        weeks = {(day.isocalendar().year, day.isocalendar().week) for day in dates}
        stats[history.symbol.symbol] = MoverStats(
            top20_days=len(rows),
            top20_weeks=len(weeks),
            top20_best_rank=min(ranks),
            top20_average_rank=statistics.fmean(ranks),
            top20_last_seen_date=max(dates),
        )
    return stats, daily_ranked


def _top_mover_dates_by_symbol(
    daily_ranked: Mapping[date, Sequence[tuple[str, int, float]]],
) -> dict[str, tuple[date, ...]]:
    result: dict[str, list[date]] = {}
    for day, rows in daily_ranked.items():
        for symbol, _, _ in rows:
            result.setdefault(symbol, []).append(day)
    return {symbol: tuple(sorted(days)) for symbol, days in result.items()}


def calculate_intraday_day_profile(
    bars: Sequence[Mapping[str, Any]],
    *,
    market_open_time: dtime,
    early_window_minutes: int,
    containment_tolerance_pct: float,
) -> IntradayDayProfile:
    ordered = sorted((dict(row) for row in bars), key=lambda row: row["timestamp"])
    if len(ordered) < 2:
        raise ValueError("insufficient_intraday_bars")

    session_open = float(ordered[0]["open"])
    if session_open <= 0:
        raise ValueError("invalid_session_open")

    cutoff_minutes = (
        market_open_time.hour * 60
        + market_open_time.minute
        + int(early_window_minutes)
    )
    early = [
        row
        for row in ordered
        if row["timestamp"].hour * 60 + row["timestamp"].minute < cutoff_minutes
    ]
    post = [row for row in ordered if row not in early]
    if not early or not post:
        raise ValueError("missing_early_or_post_window")

    session_high = max(float(row["high"]) for row in ordered)
    session_low = min(float(row["low"]) for row in ordered)
    session_close = float(ordered[-1]["close"])
    upward = max(0.0, (session_high - session_open) / session_open * 100.0)
    downward = max(0.0, (session_open - session_low) / session_open * 100.0)
    dominant_up = upward >= downward
    total_excursion = max(upward, downward)
    dominant_direction = "UP" if dominant_up else "DOWN"

    early_high = max(float(row["high"]) for row in early)
    early_low = min(float(row["low"]) for row in early)
    if dominant_up:
        early_excursion = max(0.0, (early_high - session_open) / session_open * 100.0)
    else:
        early_excursion = max(0.0, (session_open - early_low) / session_open * 100.0)
    early_share = early_excursion / total_excursion * 100.0 if total_excursion > 0 else 0.0

    post_high = max(float(row["high"]) for row in post)
    post_low = min(float(row["low"]) for row in post)
    post_up_extension = max(0.0, (post_high - early_high) / session_open * 100.0)
    post_down_extension = max(0.0, (early_low - post_low) / session_open * 100.0)
    post_range_extension = max(post_up_extension, post_down_extension)

    tolerance_value = session_open * max(0.0, containment_tolerance_pct) / 100.0
    contained_bars = sum(
        1
        for row in post
        if float(row["high"]) <= early_high + tolerance_value
        and float(row["low"]) >= early_low - tolerance_value
    )
    post_contained_pct = contained_bars / len(post) * 100.0

    if dominant_up:
        peak_index = next(
            index
            for index, row in enumerate(ordered)
            if math.isclose(float(row["high"]), session_high, rel_tol=0.0, abs_tol=1e-12)
        )
        subsequent = ordered[peak_index + 1 :]
        subsequent_extreme = (
            min(float(row["low"]) for row in subsequent)
            if subsequent
            else session_high
        )
        denominator = max(session_high - session_open, 1e-12)
        peak_retracement = max(0.0, (session_high - subsequent_extreme) / denominator)
        full_reversal = bool(subsequent and subsequent_extreme <= session_open)
        close_retention = (session_close - session_open) / denominator
    else:
        peak_index = next(
            index
            for index, row in enumerate(ordered)
            if math.isclose(float(row["low"]), session_low, rel_tol=0.0, abs_tol=1e-12)
        )
        subsequent = ordered[peak_index + 1 :]
        subsequent_extreme = (
            max(float(row["high"]) for row in subsequent)
            if subsequent
            else session_low
        )
        denominator = max(session_open - session_low, 1e-12)
        peak_retracement = max(0.0, (subsequent_extreme - session_low) / denominator)
        full_reversal = bool(subsequent and subsequent_extreme >= session_open)
        close_retention = (session_open - session_close) / denominator

    close_retention = max(0.0, min(float(close_retention), 1.0))
    two_sided_ratio = (
        min(upward, downward) / total_excursion if total_excursion > 0 else 0.0
    )

    bar_date = ordered[0].get("bar_date")
    if not isinstance(bar_date, date):
        raise ValueError("missing_intraday_bar_date")

    return IntradayDayProfile(
        bar_date=bar_date,
        total_excursion_pct=total_excursion,
        dominant_direction=dominant_direction,
        early_excursion_pct=early_excursion,
        early_move_share_pct=early_share,
        post_range_extension_pct=post_range_extension,
        post_contained_pct=post_contained_pct,
        close_retention_ratio=close_retention,
        peak_retracement_ratio=peak_retracement,
        full_reversal=full_reversal,
        two_sided_ratio=two_sided_ratio,
    )


def _intraday_profiles(
    history: IntradayHistory,
    top20_dates: Sequence[date],
    *,
    market_open_time: dtime,
    early_window_minutes: int,
    containment_tolerance_pct: float,
    minimum_bars_per_day: int,
) -> dict[date, IntradayDayProfile]:
    selected_dates = set(top20_dates)
    by_date: dict[date, list[dict[str, Any]]] = {}
    for bar in history.bars:
        bar_date = bar.get("bar_date")
        if bar_date in selected_dates:
            by_date.setdefault(bar_date, []).append(dict(bar))

    profiles: dict[date, IntradayDayProfile] = {}
    for day in sorted(selected_dates):
        bars = by_date.get(day, [])
        if len(bars) < minimum_bars_per_day:
            continue
        try:
            profiles[day] = calculate_intraday_day_profile(
                bars,
                market_open_time=market_open_time,
                early_window_minutes=early_window_minutes,
                containment_tolerance_pct=containment_tolerance_pct,
            )
        except (KeyError, TypeError, ValueError):
            continue
    return profiles


def calculate_policy_mover_behavior(
    daily_history: SymbolHistory,
    intraday_history: IntradayHistory,
    top20_dates: Sequence[date],
    *,
    minimum_excursion_pct: float,
    reversal_retracement_ratio: float,
    full_reversal_flag_rate_pct: float,
    low_close_retention_ratio: float,
    two_sided_ratio_threshold: float,
    gap_driven_share_threshold: float,
    event_cluster_max_week_share: float,
    behavior_flag_rate_pct: float,
    market_open_time: dtime,
    early_window_minutes: int,
    containment_tolerance_pct: float,
    minimum_bars_per_day: int,
) -> PolicyMoverBehavior:
    selected_dates = tuple(sorted(set(top20_dates)))
    daily_bars = list(daily_history.bars)[-int(GEN.audit_trading_days) :]
    daily_metrics: Optional[HistoricalMetrics] = None
    daily_error = ""
    try:
        daily_metrics = calculate_historical_metrics(
            daily_bars,
            minimum_history_days=min(int(GEN.minimum_history_days), len(daily_bars)),
            atr_period=int(GEN.atr_period),
        )
    except Exception as exc:
        daily_error = str(exc)

    profiles = _intraday_profiles(
        intraday_history,
        selected_dates,
        market_open_time=market_open_time,
        early_window_minutes=early_window_minutes,
        containment_tolerance_pct=containment_tolerance_pct,
        minimum_bars_per_day=minimum_bars_per_day,
    )
    valid_dates = sorted(profiles)

    def _empty(error: str) -> PolicyMoverBehavior:
        return PolicyMoverBehavior(
            symbol=daily_history.symbol.symbol,
            top20_days_60d=len(selected_dates),
            top20_weeks_60d=len(
                {(day.isocalendar().year, day.isocalendar().week) for day in selected_dates}
            ),
            top20_best_rank_60d=None,
            top20_average_rank_60d=None,
            top20_last_seen_date=max(selected_dates) if selected_dates else None,
            daily_history_days=daily_metrics.history_days if daily_metrics else 0,
            median_excursion_pct=daily_metrics.median_excursion_pct if daily_metrics else 0.0,
            directional_efficiency=(
                daily_metrics.directional_efficiency if daily_metrics else 0.0
            ),
            movement_consistency=(daily_metrics.movement_consistency if daily_metrics else 0.0),
            intraday_days_available=0,
            median_close_retention_ratio=0.0,
            median_peak_retracement_ratio=0.0,
            reversal_prone_days=0,
            reversal_prone_pct=0.0,
            full_reversal_days=0,
            full_reversal_pct=0.0,
            median_two_sided_ratio=0.0,
            two_sided_days=0,
            two_sided_pct=0.0,
            gap_driven_days=0,
            gap_driven_pct=0.0,
            maximum_week_share_pct=0.0,
            behavior_classification="INTRADAY_DATA_UNAVAILABLE",
            error=error,
        )

    if intraday_history.error:
        return _empty(intraday_history.error)
    if not valid_dates:
        return _empty("missing_intraday_top_mover_days")

    close_retention_values = [profiles[day].close_retention_ratio for day in valid_dates]
    retracement_values = [profiles[day].peak_retracement_ratio for day in valid_dates]
    two_sided_values = [profiles[day].two_sided_ratio for day in valid_dates]
    reversal_days = sum(
        1
        for day in valid_dates
        if profiles[day].total_excursion_pct >= minimum_excursion_pct
        and profiles[day].peak_retracement_ratio >= reversal_retracement_ratio
    )
    full_reversal_days = sum(1 for day in valid_dates if profiles[day].full_reversal)
    two_sided_days = sum(
        1 for day in valid_dates if profiles[day].two_sided_ratio >= two_sided_ratio_threshold
    )

    daily_by_date = {
        row.get("bar_date"): (index, row)
        for index, row in enumerate(daily_history.bars)
        if isinstance(row.get("bar_date"), date)
    }
    gap_driven_days = 0
    for day in valid_dates:
        indexed = daily_by_date.get(day)
        if indexed is None:
            continue
        index, row = indexed
        if index <= 0:
            continue
        previous_close = float(daily_history.bars[index - 1]["close"])
        open_price = float(row["open"])
        gap_pct = abs(open_price - previous_close) / previous_close * 100.0
        excursion = profiles[day].total_excursion_pct
        gap_share = gap_pct / (gap_pct + excursion) if (gap_pct + excursion) > 0 else 0.0
        if gap_share >= gap_driven_share_threshold:
            gap_driven_days += 1

    count = len(valid_dates)
    week_counts: dict[tuple[int, int], int] = {}
    for day in valid_dates:
        key = (day.isocalendar().year, day.isocalendar().week)
        week_counts[key] = week_counts.get(key, 0) + 1
    maximum_week_share = max(week_counts.values()) / count
    reversal_pct = reversal_days / count * 100.0
    full_reversal_pct = full_reversal_days / count * 100.0
    two_sided_pct = two_sided_days / count * 100.0
    gap_driven_pct = gap_driven_days / count * 100.0
    median_close_retention = statistics.median(close_retention_values)

    flags: list[str] = []
    if maximum_week_share >= event_cluster_max_week_share:
        flags.append("EVENT_CLUSTERED")
    if reversal_pct >= behavior_flag_rate_pct:
        flags.append("REVERSAL_PRONE")
    if full_reversal_pct >= full_reversal_flag_rate_pct:
        flags.append("FULL_REVERSAL_PRONE")
    if two_sided_pct >= behavior_flag_rate_pct:
        flags.append("TWO_SIDED")
    if gap_driven_pct >= behavior_flag_rate_pct:
        flags.append("GAP_DRIVEN")
    if median_close_retention <= low_close_retention_ratio:
        flags.append("LOW_CLOSE_RETENTION")
    classification = "|".join(flags) if flags else "NO_ADVERSE_PATTERN_DETECTED"

    error_parts = [part for part in (daily_error,) if part]
    if count < len(selected_dates):
        error_parts.append(f"intraday_coverage:{count}/{len(selected_dates)}")

    return PolicyMoverBehavior(
        symbol=daily_history.symbol.symbol,
        top20_days_60d=len(selected_dates),
        top20_weeks_60d=len(
            {(day.isocalendar().year, day.isocalendar().week) for day in selected_dates}
        ),
        top20_best_rank_60d=None,
        top20_average_rank_60d=None,
        top20_last_seen_date=max(selected_dates) if selected_dates else None,
        daily_history_days=daily_metrics.history_days if daily_metrics else 0,
        median_excursion_pct=daily_metrics.median_excursion_pct if daily_metrics else 0.0,
        directional_efficiency=daily_metrics.directional_efficiency if daily_metrics else 0.0,
        movement_consistency=daily_metrics.movement_consistency if daily_metrics else 0.0,
        intraday_days_available=count,
        median_close_retention_ratio=median_close_retention,
        median_peak_retracement_ratio=statistics.median(retracement_values),
        reversal_prone_days=reversal_days,
        reversal_prone_pct=reversal_pct,
        full_reversal_days=full_reversal_days,
        full_reversal_pct=full_reversal_pct,
        median_two_sided_ratio=statistics.median(two_sided_values),
        two_sided_days=two_sided_days,
        two_sided_pct=two_sided_pct,
        gap_driven_days=gap_driven_days,
        gap_driven_pct=gap_driven_pct,
        maximum_week_share_pct=maximum_week_share * 100.0,
        behavior_classification=classification,
        error=";".join(error_parts),
    )


def build_policy_mover_behaviors(
    daily_histories: Sequence[SymbolHistory],
    intraday_histories: Sequence[IntradayHistory],
    daily_ranked: Mapping[date, Sequence[tuple[str, int, float]]],
    mover_stats: Mapping[str, MoverStats],
) -> list[PolicyMoverBehavior]:
    top_dates = _top_mover_dates_by_symbol(daily_ranked)
    intraday_by_symbol = {row.symbol.symbol: row for row in intraday_histories}
    results: list[PolicyMoverBehavior] = []
    for history in daily_histories:
        stats = mover_stats.get(history.symbol.symbol, MoverStats())
        if history.symbol.enabled or stats.top20_days < int(GEN.policy_mover_min_top20_days):
            continue
        intraday = intraday_by_symbol.get(
            history.symbol.symbol,
            IntradayHistory(
                symbol=history.symbol,
                bars=(),
                error="intraday_history_not_requested",
            ),
        )
        behavior = calculate_policy_mover_behavior(
            history,
            intraday,
            top_dates.get(history.symbol.symbol, ()),
            minimum_excursion_pct=float(GEN.policy_reversal_min_excursion_pct),
            reversal_retracement_ratio=float(GEN.policy_reversal_retracement_ratio),
            full_reversal_flag_rate_pct=float(GEN.policy_full_reversal_flag_rate_pct),
            low_close_retention_ratio=float(GEN.policy_low_close_retention_ratio),
            two_sided_ratio_threshold=float(GEN.policy_two_sided_ratio_threshold),
            gap_driven_share_threshold=float(GEN.policy_gap_driven_share_threshold),
            event_cluster_max_week_share=float(GEN.policy_event_cluster_max_week_share),
            behavior_flag_rate_pct=float(GEN.policy_behavior_flag_rate_pct),
            market_open_time=dtime.fromisoformat(GEN.market_open_time),
            early_window_minutes=int(GEN.early_window_minutes),
            containment_tolerance_pct=float(GEN.early_move_containment_tolerance_pct),
            minimum_bars_per_day=int(GEN.intraday_min_bars_per_day),
        )
        results.append(
            replace(
                behavior,
                top20_days_60d=stats.top20_days,
                top20_weeks_60d=stats.top20_weeks,
                top20_best_rank_60d=stats.top20_best_rank,
                top20_average_rank_60d=stats.top20_average_rank,
                top20_last_seen_date=stats.top20_last_seen_date,
            )
        )
    return sorted(
        results,
        key=lambda row: (-row.top20_days_60d, -row.top20_weeks_60d, row.symbol),
    )


def calculate_early_move_stats(
    history: IntradayHistory,
    top20_dates: Sequence[date],
    *,
    market_open_time: dtime,
    early_window_minutes: int,
    minimum_session_excursion_pct: float,
    minimum_early_share_pct: float,
    maximum_post_range_extension_pct: float,
    minimum_post_contained_pct: float,
    containment_tolerance_pct: float,
    late_opportunity_extension_pct: float,
    minimum_bars_per_day: int,
    minimum_classification_days: int,
    high_rate_pct: float,
    moderate_rate_pct: float,
) -> EarlyMoveStats:
    if history.error:
        return EarlyMoveStats(
            symbol=history.symbol.symbol,
            top20_days_60d=len(top20_dates),
            intraday_days_available=0,
            meaningful_intraday_days=0,
            early_move_only_days=0,
            early_move_only_pct=0.0,
            median_early_move_share_pct=0.0,
            median_post_range_extension_pct=0.0,
            median_post_contained_pct=0.0,
            late_opportunity_days=0,
            late_opportunity_pct=0.0,
            first_intraday_date=None,
            last_intraday_date=None,
            early_move_classification="INTRADAY_DATA_UNAVAILABLE",
            error=history.error,
        )

    profiles = _intraday_profiles(
        history,
        top20_dates,
        market_open_time=market_open_time,
        early_window_minutes=early_window_minutes,
        containment_tolerance_pct=containment_tolerance_pct,
        minimum_bars_per_day=minimum_bars_per_day,
    )
    valid_dates = sorted(profiles)
    meaningful = [
        profiles[day]
        for day in valid_dates
        if profiles[day].total_excursion_pct >= minimum_session_excursion_pct
    ]
    early_only = [
        profile
        for profile in meaningful
        if profile.early_move_share_pct >= minimum_early_share_pct
        and profile.post_range_extension_pct <= maximum_post_range_extension_pct
        and profile.post_contained_pct >= minimum_post_contained_pct
    ]
    late_opportunity = [
        profile
        for profile in meaningful
        if profile.post_range_extension_pct >= late_opportunity_extension_pct
    ]

    available = len(valid_dates)
    meaningful_count = len(meaningful)
    early_pct = len(early_only) / meaningful_count * 100.0 if meaningful_count else 0.0
    late_pct = (
        len(late_opportunity) / meaningful_count * 100.0 if meaningful_count else 0.0
    )
    if available == 0:
        classification = "INTRADAY_DATA_UNAVAILABLE"
    elif meaningful_count < minimum_classification_days:
        classification = "LIMITED_SAMPLE"
    elif early_pct >= high_rate_pct:
        classification = "MOSTLY_EARLY_MOVE_ONLY"
    elif early_pct >= moderate_rate_pct:
        classification = "OFTEN_EARLY_MOVE_ONLY"
    else:
        classification = "MIXED_OR_LATE_OPPORTUNITY"

    error = ""
    if available == 0:
        error = "missing_intraday_days"
    elif available < len(set(top20_dates)):
        error = f"intraday_coverage:{available}/{len(set(top20_dates))}"

    return EarlyMoveStats(
        symbol=history.symbol.symbol,
        top20_days_60d=len(top20_dates),
        intraday_days_available=available,
        meaningful_intraday_days=meaningful_count,
        early_move_only_days=len(early_only),
        early_move_only_pct=early_pct,
        median_early_move_share_pct=(
            statistics.median(profile.early_move_share_pct for profile in meaningful)
            if meaningful
            else 0.0
        ),
        median_post_range_extension_pct=(
            statistics.median(profile.post_range_extension_pct for profile in meaningful)
            if meaningful
            else 0.0
        ),
        median_post_contained_pct=(
            statistics.median(profile.post_contained_pct for profile in meaningful)
            if meaningful
            else 0.0
        ),
        late_opportunity_days=len(late_opportunity),
        late_opportunity_pct=late_pct,
        first_intraday_date=min(valid_dates) if valid_dates else None,
        last_intraday_date=max(valid_dates) if valid_dates else None,
        early_move_classification=classification,
        error=error,
    )


def build_early_move_stats(
    histories: Sequence[IntradayHistory],
    daily_ranked: Mapping[date, Sequence[tuple[str, int, float]]],
) -> list[EarlyMoveStats]:
    top_dates = _top_mover_dates_by_symbol(daily_ranked)
    rows = [
        calculate_early_move_stats(
            history,
            top_dates.get(history.symbol.symbol, ()),
            market_open_time=dtime.fromisoformat(GEN.market_open_time),
            early_window_minutes=int(GEN.early_window_minutes),
            minimum_session_excursion_pct=float(GEN.early_move_min_session_excursion_pct),
            minimum_early_share_pct=float(GEN.early_move_min_share_pct),
            maximum_post_range_extension_pct=float(
                GEN.early_move_max_post_range_extension_pct
            ),
            minimum_post_contained_pct=float(GEN.early_move_min_post_contained_pct),
            containment_tolerance_pct=float(GEN.early_move_containment_tolerance_pct),
            late_opportunity_extension_pct=float(
                GEN.early_move_late_opportunity_extension_pct
            ),
            minimum_bars_per_day=int(GEN.intraday_min_bars_per_day),
            minimum_classification_days=int(
                GEN.early_move_min_classification_days
            ),
            high_rate_pct=float(GEN.early_move_high_rate_pct),
            moderate_rate_pct=float(GEN.early_move_moderate_rate_pct),
        )
        for history in histories
        if top_dates.get(history.symbol.symbol)
    ]
    return sorted(
        rows,
        key=lambda row: (
            -row.early_move_only_pct,
            -row.early_move_only_days,
            -row.top20_days_60d,
            row.symbol,
        ),
    )


def _candidate_from_history(
    history: SymbolHistory,
    audit_dates: Sequence[date],
    mover_stats: MoverStats,
    minimum_history_days: int,
    atr_period: int,
    completed_date: date,
    maximum_history_staleness_days: int,
) -> UniverseCandidate:
    symbol = history.symbol
    selection_eligible = bool(symbol.enabled and symbol.derivative_available)
    common = {
        "symbol": symbol.symbol,
        "enabled": symbol.enabled,
        "active_flag_before": symbol.active,
        "operationally_active_before": symbol.operationally_active,
        "whitelisted": symbol.whitelisted,
        "derivative_available": symbol.derivative_available,
        "selection_eligible": selection_eligible,
        "top20_days_60d": mover_stats.top20_days,
        "top20_frequency_pct_60d": (
            mover_stats.top20_days / len(audit_dates) * 100.0 if audit_dates else 0.0
        ),
        "top20_weeks_60d": mover_stats.top20_weeks,
        "top20_week_frequency_pct_60d": (
            mover_stats.top20_weeks
            / len({(day.isocalendar().year, day.isocalendar().week) for day in audit_dates})
            * 100.0
            if audit_dates
            else 0.0
        ),
        "top20_best_rank_60d": mover_stats.top20_best_rank,
        "top20_average_rank_60d": mover_stats.top20_average_rank,
        "top20_last_seen_date": mover_stats.top20_last_seen_date,
    }

    if not symbol.enabled:
        return UniverseCandidate(**common, valid_history=False, error="disabled_by_filter")
    if not symbol.derivative_available:
        return UniverseCandidate(**common, valid_history=False, error="missing_derivative_instrument")
    if history.error:
        return UniverseCandidate(**common, valid_history=False, error=history.error)

    allowed_dates = set(audit_dates)
    bars = [row for row in history.bars if row.get("bar_date") in allowed_dates]
    try:
        metrics = calculate_historical_metrics(bars, minimum_history_days, atr_period)
        staleness = _history_staleness_days(
            metrics.latest_bar_date,
            completed_date,
            maximum_history_staleness_days,
        )
    except Exception as exc:
        return UniverseCandidate(**common, valid_history=False, error=str(exc))

    return UniverseCandidate(
        **common,
        valid_history=True,
        error="",
        history_days=metrics.history_days,
        latest_bar_date=metrics.latest_bar_date,
        history_staleness_days=staleness,
        latest_close=metrics.latest_close,
        atr_pct=metrics.atr_pct,
        median_excursion_pct=metrics.median_excursion_pct,
        p90_excursion_pct=metrics.p90_excursion_pct,
        median_turnover_lakh=metrics.median_turnover_lakh,
        directional_efficiency=metrics.directional_efficiency,
        movement_consistency=metrics.movement_consistency,
    )


def _percentile_map(candidates: Sequence[UniverseCandidate], field: str) -> dict[str, float]:
    values = sorted(
        float(getattr(row, field))
        for row in candidates
        if getattr(row, field) is not None
    )
    if not values:
        return {}
    if len(values) == 1:
        return {row.symbol: 1.0 for row in candidates if getattr(row, field) is not None}

    denominator = len(values) - 1
    result: dict[str, float] = {}
    for row in candidates:
        value = getattr(row, field)
        if value is None:
            continue
        left = bisect.bisect_left(values, float(value))
        right = bisect.bisect_right(values, float(value)) - 1
        result[row.symbol] = ((left + right) / 2.0) / denominator
    return result


def score_candidates(candidates: Sequence[UniverseCandidate]) -> list[UniverseCandidate]:
    valid = [
        row
        for row in candidates
        if row.selection_eligible and row.valid_history
    ]
    component_fields = {
        "excursion": "median_excursion_pct",
        "turnover": "median_turnover_lakh",
        "atr": "atr_pct",
        "efficiency": "directional_efficiency",
        "top20_days": "top20_days_60d",
        "top20_weeks": "top20_weeks_60d",
    }
    component_scores = {
        name: _percentile_map(valid, field)
        for name, field in component_fields.items()
    }
    weights = {
        "excursion": float(GEN.weight_median_excursion_pct),
        "turnover": float(GEN.weight_turnover),
        "atr": float(GEN.weight_atr_pct),
        "efficiency": float(GEN.weight_directional_efficiency),
        "top20_days": float(GEN.weight_top20_days),
        "top20_weeks": float(GEN.weight_top20_weeks),
    }
    if not math.isclose(sum(weights.values()), 1.0, abs_tol=1e-9):
        raise ValueError(
            f"universe_generation_weights_must_sum_to_1:{sum(weights.values())}"
        )

    scored: list[UniverseCandidate] = []
    for row in candidates:
        if row not in valid:
            scored.append(row)
            continue
        values = {name: component_scores[name][row.symbol] for name in component_fields}
        total = sum(weights[name] * values[name] for name in weights)
        scored.append(
            replace(
                row,
                excursion_score=values["excursion"],
                turnover_score=values["turnover"],
                atr_score=values["atr"],
                efficiency_score=values["efficiency"],
                top20_days_score=values["top20_days"],
                top20_weeks_score=values["top20_weeks"],
                total_score=total,
            )
        )

    valid_sorted = sorted(
        (row for row in scored if row.selection_eligible and row.valid_history),
        key=lambda row: (-row.total_score, row.symbol),
    )
    ranks = {row.symbol: rank for rank, row in enumerate(valid_sorted, start=1)}
    return [replace(row, base_rank=ranks.get(row.symbol)) for row in scored]


def select_active_universe(
    candidates: Sequence[UniverseCandidate],
    active_limit: int,
    hysteresis_score_gap: float,
    minimum_history_coverage_pct: float,
    minimum_derivative_coverage_pct: float,
) -> UniverseSelectionResult:
    enabled = [row for row in candidates if row.enabled]
    derivative_eligible = [row for row in enabled if row.derivative_available]
    valid = [row for row in derivative_eligible if row.valid_history]
    forced = sorted((row for row in valid if row.whitelisted), key=lambda row: row.symbol)

    unsafe: list[str] = []
    if active_limit <= 0:
        unsafe.append("active_limit_must_be_positive")
    if hysteresis_score_gap < 0:
        unsafe.append("hysteresis_score_gap_must_be_non_negative")
    if not 0 <= minimum_history_coverage_pct <= 100:
        unsafe.append("minimum_history_coverage_pct_out_of_range")
    if not 0 <= minimum_derivative_coverage_pct <= 100:
        unsafe.append("minimum_derivative_coverage_pct_out_of_range")
    if len(forced) > active_limit:
        unsafe.append(f"whitelist_exceeds_active_limit:{len(forced)}>{active_limit}")

    derivative_coverage_pct = (
        len(derivative_eligible) / len(enabled) * 100.0 if enabled else 0.0
    )
    history_coverage_pct = (
        len(valid) / len(derivative_eligible) * 100.0
        if derivative_eligible
        else 0.0
    )
    if derivative_coverage_pct < minimum_derivative_coverage_pct:
        unsafe.append(
            "derivative_coverage_below_minimum:"
            f"{derivative_coverage_pct:.2f}<{minimum_derivative_coverage_pct:.2f}"
        )
    if history_coverage_pct < minimum_history_coverage_pct:
        unsafe.append(
            "history_coverage_below_minimum:"
            f"{history_coverage_pct:.2f}<{minimum_history_coverage_pct:.2f}"
        )

    critical_invalid = sorted(
        row.symbol
        for row in derivative_eligible
        if not row.valid_history and (row.operationally_active_before or row.whitelisted)
    )
    if critical_invalid:
        unsafe.append(f"critical_history_failures:{len(critical_invalid)}")

    forced_symbols = {row.symbol for row in forced}
    capacity = max(0, active_limit - len(forced_symbols))
    dynamic = sorted(
        (row for row in valid if row.symbol not in forced_symbols),
        key=lambda row: (-row.total_score, row.symbol),
    )
    selected_dynamic = list(dynamic[:capacity])
    hysteresis_retained: set[str] = set()

    if capacity > 0 and hysteresis_score_gap > 0:
        incumbents = sorted(
            (
                row
                for row in dynamic[capacity:]
                if row.operationally_active_before
            ),
            key=lambda row: (-row.total_score, row.symbol),
        )
        for incumbent in incumbents:
            newcomers = [
                row for row in selected_dynamic if not row.operationally_active_before
            ]
            if not newcomers:
                break
            weakest_newcomer = min(
                newcomers, key=lambda row: (row.total_score, row.symbol)
            )
            score_gap = weakest_newcomer.total_score - incumbent.total_score
            if score_gap < 0 or score_gap > hysteresis_score_gap:
                continue
            selected_dynamic.remove(weakest_newcomer)
            selected_dynamic.append(incumbent)
            hysteresis_retained.add(incumbent.symbol)

    selected_symbols = forced_symbols | {row.symbol for row in selected_dynamic}
    if len(selected_symbols) < active_limit:
        unsafe.append(f"selection_underfilled:{len(selected_symbols)}<{active_limit}")

    current_operational = {
        row.symbol for row in candidates if row.operationally_active_before
    }
    additions = sorted(selected_symbols - current_operational)
    removals = sorted(current_operational - selected_symbols)
    retained = sorted(selected_symbols & current_operational)
    cutoff_score = min(
        (row.total_score for row in selected_dynamic), default=None
    )

    annotated: list[UniverseCandidate] = []
    for row in candidates:
        proposed = row.symbol in selected_symbols
        if proposed and row.operationally_active_before:
            action = "RETAIN"
        elif proposed:
            action = "ACTIVATE"
        elif row.operationally_active_before:
            action = "DEACTIVATE"
        else:
            action = "REMAIN_INACTIVE"

        if proposed and row.whitelisted:
            reason = "WHITELIST"
        elif proposed and row.symbol in hysteresis_retained:
            reason = "RETAINED_HYSTERESIS"
        elif proposed:
            reason = "SELECTED_SCORE"
        elif not row.enabled:
            reason = "DISABLED_BY_FILTER"
        elif not row.derivative_available:
            reason = "MISSING_DERIVATIVE"
        elif not row.valid_history:
            reason = "INVALID_HISTORY"
        else:
            reason = "BELOW_CUTOFF"

        if row.top20_days_60d <= 0 or proposed:
            miss_reason = ""
        elif not row.enabled:
            miss_reason = "POLICY_DISABLED"
        elif not row.derivative_available:
            miss_reason = "MISSING_DERIVATIVE"
        elif not row.valid_history:
            miss_reason = "INSUFFICIENT_HISTORY"
        else:
            miss_reason = "ENABLED_NOT_SELECTED"

        annotated.append(
            replace(
                row,
                proposed_active=proposed,
                active_action=action,
                selection_reason=reason,
                missed_mover_reason=miss_reason,
            )
        )

    return UniverseSelectionResult(
        candidates=tuple(
            sorted(
                annotated,
                key=lambda row: (
                    not row.proposed_active,
                    row.base_rank or 10**9,
                    row.symbol,
                ),
            )
        ),
        selected_symbols=tuple(sorted(selected_symbols)),
        retained_symbols=tuple(retained),
        added_symbols=tuple(additions),
        removed_symbols=tuple(removals),
        cutoff_score=cutoff_score,
        target_count=active_limit,
        total_eq_count=len(candidates),
        enabled_count=len(enabled),
        current_operational_active_count=len(current_operational),
        derivative_eligible_count=len(derivative_eligible),
        valid_candidate_count=len(valid),
        history_coverage_pct=history_coverage_pct,
        derivative_coverage_pct=derivative_coverage_pct,
        critical_invalid_symbols=tuple(critical_invalid),
        safe_to_apply=not unsafe,
        unsafe_reasons=tuple(unsafe),
    )


def summarize_mover_capture(
    result: UniverseSelectionResult,
    audit_dates: Sequence[date],
) -> MoverAuditSummary:
    total = captured = policy_disabled = enabled_not_selected = 0
    missing_derivative = insufficient_history = 0

    for row in result.candidates:
        appearances = int(row.top20_days_60d)
        total += appearances
        if row.proposed_active:
            captured += appearances
        elif row.missed_mover_reason == "POLICY_DISABLED":
            policy_disabled += appearances
        elif row.missed_mover_reason == "MISSING_DERIVATIVE":
            missing_derivative += appearances
        elif row.missed_mover_reason == "INSUFFICIENT_HISTORY":
            insufficient_history += appearances
        elif row.missed_mover_reason == "ENABLED_NOT_SELECTED":
            enabled_not_selected += appearances

    weeks = len({(day.isocalendar().year, day.isocalendar().week) for day in audit_dates})
    return MoverAuditSummary(
        audit_dates=tuple(audit_dates),
        audit_weeks=weeks,
        total_top_mover_appearances=total,
        captured_appearances=captured,
        policy_disabled_appearances=policy_disabled,
        enabled_not_selected_appearances=enabled_not_selected,
        missing_derivative_appearances=missing_derivative,
        insufficient_history_appearances=insufficient_history,
        capture_pct=(captured / total * 100.0 if total else 0.0),
    )


def _apply_active_selection(selected_symbols: Sequence[str]) -> dict[str, int]:
    selected = {_norm(symbol) for symbol in selected_symbols if _norm(symbol)}
    changed = activated = deactivated = 0

    with get_trades_db() as db:
        rows = (
            db.query(SymbolORM)
            .filter(SymbolORM.type == "EQ", SymbolORM.enabled.is_(True))
            .all()
        )
        for row in rows:
            target = _norm(row.symbol) in selected
            before = bool(row.active)
            if before == target:
                continue
            row.active = target
            changed += 1
            if target:
                activated += 1
            else:
                deactivated += 1
        db.commit()

    with get_trades_db() as db:
        verified = (
            db.query(SymbolORM)
            .filter(
                SymbolORM.type == "EQ",
                SymbolORM.enabled.is_(True),
                SymbolORM.active.is_(True),
            )
            .count()
        )
    if verified != len(selected):
        raise RuntimeError(f"active_verification_failed:{verified}!={len(selected)}")

    return {
        "changed": changed,
        "activated": activated,
        "deactivated": deactivated,
        "verified_operational_active": verified,
    }


def _write_reports(
    report_dir: str,
    result: UniverseSelectionResult,
    policy_behaviors: Sequence[PolicyMoverBehavior],
    early_move_rows: Sequence[EarlyMoveStats],
    run_time: datetime,
) -> tuple[Path, Path, Path, Path]:
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = run_time.strftime("%Y%m%d_%H%M%S")
    main_path = directory / f"generate_stock_universe_{stamp}.csv"
    missed_path = directory / f"generate_stock_universe_missed_movers_{stamp}.csv"
    policy_path = directory / f"generate_stock_universe_policy_movers_{stamp}.csv"
    early_path = directory / f"generate_stock_universe_early_move_only_{stamp}.csv"

    rows = [asdict(row) for row in result.candidates]
    fieldnames = list(rows[0].keys()) if rows else ["symbol"]
    with main_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    missed = sorted(
        (
            row
            for row in result.candidates
            if row.top20_days_60d > 0 and not row.proposed_active
        ),
        key=lambda row: (
            -row.top20_days_60d,
            -row.top20_weeks_60d,
            row.symbol,
        ),
    )
    missed_fields = [
        "symbol",
        "enabled",
        "active_flag_before",
        "operationally_active_before",
        "derivative_available",
        "top20_days_60d",
        "top20_frequency_pct_60d",
        "top20_weeks_60d",
        "top20_week_frequency_pct_60d",
        "top20_best_rank_60d",
        "top20_average_rank_60d",
        "top20_last_seen_date",
        "base_rank",
        "total_score",
        "missed_mover_reason",
        "selection_reason",
    ]
    with missed_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=missed_fields)
        writer.writeheader()
        for row in missed:
            data = asdict(row)
            writer.writerow({field: data.get(field) for field in missed_fields})

    by_symbol = {row.symbol: row for row in result.candidates}
    policy_fields = [
        "symbol",
        "enabled",
        "active_flag_before",
        "operationally_active_before",
        "derivative_available",
        "proposed_active",
        "selection_reason",
        "top20_days_60d",
        "top20_weeks_60d",
        "top20_best_rank_60d",
        "top20_average_rank_60d",
        "top20_last_seen_date",
        "daily_history_days",
        "median_excursion_pct",
        "directional_efficiency",
        "movement_consistency",
        "intraday_days_available",
        "median_close_retention_ratio",
        "median_peak_retracement_ratio",
        "reversal_prone_days",
        "reversal_prone_pct",
        "full_reversal_days",
        "full_reversal_pct",
        "median_two_sided_ratio",
        "two_sided_days",
        "two_sided_pct",
        "gap_driven_days",
        "gap_driven_pct",
        "maximum_week_share_pct",
        "behavior_classification",
        "error",
    ]
    with policy_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=policy_fields)
        writer.writeheader()
        for behavior in policy_behaviors:
            candidate = by_symbol[behavior.symbol]
            data = {
                **{field: getattr(candidate, field, None) for field in policy_fields},
                **asdict(behavior),
            }
            writer.writerow({field: data.get(field) for field in policy_fields})

    early_fields = [
        "symbol",
        "enabled",
        "active_flag_before",
        "operationally_active_before",
        "derivative_available",
        "proposed_active",
        "selection_reason",
        "base_rank",
        "total_score",
        "top20_days_60d",
        "top20_weeks_60d",
        "intraday_days_available",
        "meaningful_intraday_days",
        "early_move_only_days",
        "early_move_only_pct",
        "median_early_move_share_pct",
        "median_post_range_extension_pct",
        "median_post_contained_pct",
        "late_opportunity_days",
        "late_opportunity_pct",
        "first_intraday_date",
        "last_intraday_date",
        "early_move_classification",
        "error",
    ]
    with early_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=early_fields)
        writer.writeheader()
        for early in early_move_rows:
            candidate = by_symbol[early.symbol]
            data = {
                **{field: getattr(candidate, field, None) for field in early_fields},
                **asdict(early),
                "top20_weeks_60d": candidate.top20_weeks_60d,
            }
            writer.writerow({field: data.get(field) for field in early_fields})

    return main_path, missed_path, policy_path, early_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Review or apply the stable active universe from enabled EQ symbols."
    )
    parser.add_argument("--as-of", default=DEFAULT_AS_OF, help="Historical cutoff date YYYY-MM-DD.")
    parser.add_argument(
        "--symbols",
        default=DEFAULT_SYMBOLS,
        help="Optional comma-separated EQ symbols for focused review. Apply is blocked.",
    )
    parser.add_argument("--active-limit", type=int, default=DEFAULT_ACTIVE_LIMIT)
    parser.add_argument("--audit-days", type=int, default=DEFAULT_AUDIT_TRADING_DAYS)
    parser.add_argument("--top-movers", type=int, default=DEFAULT_TOP_MOVERS_PER_DAY)
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_CALENDAR_LOOKBACK_DAYS)
    parser.add_argument("--minimum-history-days", type=int, default=DEFAULT_MINIMUM_HISTORY_DAYS)
    parser.add_argument(
        "--hysteresis-score-gap",
        type=float,
        default=DEFAULT_HYSTERESIS_SCORE_GAP,
    )
    parser.add_argument(
        "--minimum-history-coverage-pct",
        type=float,
        default=float(GEN.minimum_history_coverage_pct),
    )
    parser.add_argument(
        "--minimum-derivative-coverage-pct",
        type=float,
        default=float(GEN.minimum_derivative_coverage_pct),
    )
    parser.add_argument(
        "--maximum-history-staleness-days",
        type=int,
        default=int(GEN.maximum_history_staleness_days),
    )
    parser.add_argument("--report-dir", default=DEFAULT_REPORT_DIR)
    parser.add_argument("--no-report", action="store_true")
    parser.add_argument(
        "--apply",
        action="store_true",
        default=DEFAULT_APPLY,
        help="Persist active flags for enabled EQ symbols. Default is review only.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    setup_logging(log_file=DEFAULT_LOG_FILE)
    run_time = datetime.now(IST)

    try:
        as_of = _parse_as_of(args.as_of)
        completed_date = _completed_bar_date(as_of, now=run_time)
        if args.active_limit <= 0:
            raise ValueError("active_limit_must_be_positive")
        if args.audit_days <= 0:
            raise ValueError("audit_days_must_be_positive")
        if args.top_movers <= 0:
            raise ValueError("top_movers_must_be_positive")
        if args.minimum_history_days > args.audit_days:
            raise ValueError("minimum_history_days_cannot_exceed_audit_days")
        if args.minimum_history_days <= int(GEN.atr_period):
            raise ValueError("minimum_history_days_must_exceed_atr_period")
        if args.hysteresis_score_gap < 0:
            raise ValueError("hysteresis_score_gap_must_be_non_negative")
        if args.maximum_history_staleness_days < 0:
            raise ValueError("maximum_history_staleness_days_must_be_non_negative")
    except Exception as exc:
        logger.error("GENERATE_STOCK_UNIVERSE_INVALID_OPTIONS | error=%s", exc)
        return 2

    symbol_filter = _parse_symbol_filter(args.symbols)
    logger.info(
        "GENERATE_STOCK_UNIVERSE_START | as_of=%s | completed_date=%s | apply=%s | active_limit=%d | audit_days=%d | top_movers=%d | focused_symbols=%d",
        as_of.isoformat(),
        completed_date.isoformat(),
        bool(args.apply),
        int(args.active_limit),
        int(args.audit_days),
        int(args.top_movers),
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

    whitelist_preflight = inspect_whitelist_preflight(universe)
    try:
        kite = _kite_client()
    except Exception:
        logger.exception("GENERATE_STOCK_UNIVERSE_BROKER_PREFLIGHT_FAILED")
        return 2

    histories: list[SymbolHistory] = []
    fetch_failures: list[dict[str, str]] = []
    for index, symbol in enumerate(universe, start=1):
        try:
            history = _fetch_symbol_history(
                kite,
                symbol,
                as_of,
                completed_date,
                int(args.lookback_days),
            )
        except Exception as exc:
            logger.error(
                "GENERATE_STOCK_UNIVERSE_SYMBOL_FAILED | symbol=%s | error=%s\n%s",
                symbol.symbol,
                exc,
                traceback.format_exc(),
            )
            history = SymbolHistory(symbol=symbol, bars=(), error=str(exc))
            fetch_failures.append({"symbol": symbol.symbol, "error": str(exc)})
        histories.append(history)
        if index < len(universe) and float(GEN.historical_rate_sleep_sec) > 0:
            time.sleep(float(GEN.historical_rate_sleep_sec))

    try:
        audit_dates = _audit_dates(histories, int(args.audit_days))
        mover_stats, daily_ranked = build_top_mover_stats(
            histories,
            audit_dates,
            int(args.top_movers),
        )
        candidates = [
            _candidate_from_history(
                history,
                audit_dates,
                mover_stats.get(history.symbol.symbol, MoverStats()),
                int(args.minimum_history_days),
                int(GEN.atr_period),
                completed_date,
                int(args.maximum_history_staleness_days),
            )
            for history in histories
        ]
        scored = score_candidates(candidates)
        result = select_active_universe(
            scored,
            active_limit=int(args.active_limit),
            hysteresis_score_gap=float(args.hysteresis_score_gap),
            minimum_history_coverage_pct=float(args.minimum_history_coverage_pct),
            minimum_derivative_coverage_pct=float(args.minimum_derivative_coverage_pct),
        )
        mover_summary = summarize_mover_capture(result, audit_dates)
    except Exception:
        logger.exception("GENERATE_STOCK_UNIVERSE_SELECTION_FAILED")
        return 2

    unsafe_reasons = list(result.unsafe_reasons)
    invalid_whitelist_history = sorted(
        row.symbol
        for row in result.candidates
        if row.whitelisted and not row.valid_history
    )
    if whitelist_preflight.missing_symbols:
        unsafe_reasons.append(
            f"missing_whitelist:{len(whitelist_preflight.missing_symbols)}"
        )
    if whitelist_preflight.disabled_symbols:
        unsafe_reasons.append(
            f"disabled_whitelist:{len(whitelist_preflight.disabled_symbols)}"
        )
    if whitelist_preflight.missing_derivative_symbols:
        unsafe_reasons.append(
            "whitelist_missing_derivative:"
            f"{len(whitelist_preflight.missing_derivative_symbols)}"
        )
    if invalid_whitelist_history:
        unsafe_reasons.append(
            f"whitelist_invalid_history:{len(invalid_whitelist_history)}"
        )
    safe_to_apply = result.safe_to_apply and not unsafe_reasons

    policy_behaviors: list[PolicyMoverBehavior] = []
    early_move_rows: list[EarlyMoveStats] = []
    intraday_failures: list[dict[str, str]] = []

    main_report: Optional[Path] = None
    missed_report: Optional[Path] = None
    policy_report: Optional[Path] = None
    early_move_report: Optional[Path] = None
    if DEFAULT_WRITE_REPORT and not args.no_report:
        top_dates = _top_mover_dates_by_symbol(daily_ranked)
        by_symbol = {row.symbol: row for row in universe}
        intraday_histories: list[IntradayHistory] = []
        intraday_symbols = sorted(symbol for symbol in top_dates if symbol in by_symbol)
        for index, symbol_name in enumerate(intraday_symbols, start=1):
            symbol = by_symbol[symbol_name]
            try:
                intraday = _fetch_intraday_history(
                    kite,
                    symbol,
                    as_of,
                    audit_dates,
                    int(args.lookback_days),
                )
            except Exception as exc:
                logger.error(
                    "GENERATE_STOCK_UNIVERSE_INTRADAY_FAILED | symbol=%s | error=%s\n%s",
                    symbol.symbol,
                    exc,
                    traceback.format_exc(),
                )
                intraday = IntradayHistory(symbol=symbol, bars=(), error=str(exc))
                intraday_failures.append({"symbol": symbol.symbol, "error": str(exc)})
            intraday_histories.append(intraday)
            if intraday.error and not any(
                row["symbol"] == symbol.symbol for row in intraday_failures
            ):
                intraday_failures.append(
                    {"symbol": symbol.symbol, "error": intraday.error}
                )
            if index < len(intraday_symbols) and float(GEN.intraday_rate_sleep_sec) > 0:
                time.sleep(float(GEN.intraday_rate_sleep_sec))

        policy_behaviors = build_policy_mover_behaviors(
            histories,
            intraday_histories,
            daily_ranked,
            mover_stats,
        )
        early_move_rows = build_early_move_stats(intraday_histories, daily_ranked)
        try:
            (
                main_report,
                missed_report,
                policy_report,
                early_move_report,
            ) = _write_reports(
                args.report_dir,
                result,
                policy_behaviors,
                early_move_rows,
                run_time,
            )
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

    frequently_missed = [
        {
            "symbol": row.symbol,
            "top20_days": row.top20_days_60d,
            "top20_weeks": row.top20_weeks_60d,
            "reason": row.missed_mover_reason,
            "rank": row.base_rank,
        }
        for row in sorted(
            (
                row
                for row in result.candidates
                if row.top20_days_60d > 0 and not row.proposed_active
            ),
            key=lambda row: (-row.top20_days_60d, -row.top20_weeks_60d, row.symbol),
        )[:20]
    ]

    summary = {
        "mode": "APPLY" if args.apply else "REVIEW",
        "as_of": as_of.isoformat(),
        "completed_date": completed_date.isoformat(),
        "audit_start": audit_dates[0].isoformat(),
        "audit_end": audit_dates[-1].isoformat(),
        "audit_trading_days": len(audit_dates),
        "audit_weeks": mover_summary.audit_weeks,
        "target_active_count": result.target_count,
        "total_eq_count": result.total_eq_count,
        "enabled_count": result.enabled_count,
        "current_operational_active_count": result.current_operational_active_count,
        "derivative_eligible_count": result.derivative_eligible_count,
        "valid_candidate_count": result.valid_candidate_count,
        "proposed_active_count": len(result.selected_symbols),
        "retained_count": len(result.retained_symbols),
        "activate_count": len(result.added_symbols),
        "deactivate_count": len(result.removed_symbols),
        "history_coverage_pct": round(result.history_coverage_pct, 4),
        "derivative_coverage_pct": round(result.derivative_coverage_pct, 4),
        "cutoff_score": result.cutoff_score,
        "top_mover_appearances": mover_summary.total_top_mover_appearances,
        "captured_top_mover_appearances": mover_summary.captured_appearances,
        "top_mover_capture_pct": round(mover_summary.capture_pct, 4),
        "missed_policy_disabled_appearances": mover_summary.policy_disabled_appearances,
        "missed_enabled_not_selected_appearances": mover_summary.enabled_not_selected_appearances,
        "missed_missing_derivative_appearances": mover_summary.missing_derivative_appearances,
        "missed_insufficient_history_appearances": mover_summary.insufficient_history_appearances,
        "frequently_missed": frequently_missed,
        "whitelist_missing": list(whitelist_preflight.missing_symbols),
        "whitelist_disabled": list(whitelist_preflight.disabled_symbols),
        "whitelist_missing_derivative": list(
            whitelist_preflight.missing_derivative_symbols
        ),
        "whitelist_invalid_history": invalid_whitelist_history,
        "critical_invalid_symbols": list(result.critical_invalid_symbols),
        "safe_to_apply": safe_to_apply,
        "unsafe_reasons": unsafe_reasons,
        "fetch_failures": fetch_failures,
        "policy_mover_rows": len(policy_behaviors),
        "policy_behavior_classifications": {
            classification: sum(
                1
                for row in policy_behaviors
                if row.behavior_classification == classification
            )
            for classification in sorted(
                {row.behavior_classification for row in policy_behaviors}
            )
        },
        "early_move_rows": len(early_move_rows),
        "mostly_early_move_only_count": sum(
            1
            for row in early_move_rows
            if row.early_move_classification == "MOSTLY_EARLY_MOVE_ONLY"
        ),
        "often_early_move_only_count": sum(
            1
            for row in early_move_rows
            if row.early_move_classification == "OFTEN_EARLY_MOVE_ONLY"
        ),
        "early_move_limited_sample_count": sum(
            1
            for row in early_move_rows
            if row.early_move_classification == "LIMITED_SAMPLE"
        ),
        "intraday_failures": intraday_failures,
        "apply_result": apply_result,
        "main_report": str(main_report) if main_report else None,
        "missed_movers_report": str(missed_report) if missed_report else None,
        "policy_movers_report": str(policy_report) if policy_report else None,
        "early_move_only_report": str(early_move_report) if early_move_report else None,
    }
    logger.info("GENERATE_STOCK_UNIVERSE_SUMMARY | %s", json.dumps(summary, sort_keys=True))
    logger.info(
        "GENERATE_STOCK_UNIVERSE_ADDITIONS | %s",
        ", ".join(result.added_symbols) or "NONE",
    )
    logger.info(
        "GENERATE_STOCK_UNIVERSE_REMOVALS | %s",
        ", ".join(result.removed_symbols) or "NONE",
    )

    return 0 if not fetch_failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
