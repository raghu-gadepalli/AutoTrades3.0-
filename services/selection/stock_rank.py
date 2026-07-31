#!/usr/bin/env python3
"""Diagnostic rolling stock movement ranking from completed snapshots.

The service reads one common completed snapshot cadence for the active/enabled
EQ universe, computes a cross-sectional movement-quality ranking, persists one
``stock_rank`` row per symbol, exports the same run to ``reports/`` and logs an
end-of-run summary.

This first version is observation-only.  It does not change symbols.active,
generate_signals, Auction, StockAdvisor, signals, opportunities or trades.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# Allow direct execution: python services/selection/stock_rank.py
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from configs.stock_rank_config import STOCK_RANK_CONFIG, StockRankConfig
from logconfig import setup_logging
from schemas.snapshot import SnapshotSchema
from schemas.stock_rank import StockRankSchema
from schemas.symbol import SymbolSchema
from utils.datetime_utils import IST, business_now

logger = logging.getLogger(__name__)

# =============================================================================
# SOURCE DEFAULTS
# =============================================================================
# These defaults are intentionally visible in the program, matching the replay
# utilities.  Every value can be overridden from the command line.
#
# None for date/time means:
#   - current IST business day
#   - latest common completed snapshot cadence at or before the run time
DEFAULT_TRADING_DAY: Optional[str] = None
DEFAULT_AS_OF: Optional[str] = None
DEFAULT_SYMBOLS: Optional[str] = None
DEFAULT_ALL_ENABLED = False
DEFAULT_REPORT_DIR = "reports"
DEFAULT_LOG_FILE: Optional[str] = "/var/www/autotrades/scripts/stock_rank.log"


@dataclass(frozen=True)
class RangeContext:
    active: bool
    episode_id: Optional[str]
    range_id: Optional[str]
    started_at: Optional[datetime]
    low: Optional[float]
    high: Optional[float]
    age_bars: int
    width_pct: Optional[float]
    containment_ratio: Optional[float]
    midpoint_crossings: int
    vwap_crossings: int
    failed_escape_count: int
    rearm_required: bool
    attempt_limit_reached: bool
    total_travel_range_multiple: Optional[float]


@dataclass(frozen=True)
class StockRankResult:
    symbol: str
    rank_time: datetime
    rank_position: int
    universe_size: int
    direction: str
    classification: str
    total_score: float
    movement_score: float
    quality_score: float
    range_penalty: float
    stall_penalty: float
    close_price: float
    previous_close: Optional[float]
    today_open: Optional[float]
    gap_pct: Optional[float]
    session_move_pct: Optional[float]
    post_open_move_pct: Optional[float]
    move_15m_pct: Optional[float]
    move_30m_pct: Optional[float]
    move_60m_pct: Optional[float]
    move_15m_atr: Optional[float]
    move_30m_atr: Optional[float]
    move_60m_atr: Optional[float]
    atr_value: float
    atr_pct: Optional[float]
    directional_efficiency: Optional[float]
    recent_efficiency: Optional[float]
    direction_consistency: float
    acceleration_score: float
    volume_ratio: Optional[float]
    freshness_score: float
    bars_since_extreme: int
    range_context: RangeContext
    metrics: Dict[str, Any]

    def to_schema(self, *, run_id: str) -> StockRankSchema:
        rank_time = _to_ist_naive(self.rank_time)
        return StockRankSchema(
            run_id=run_id,
            trading_day=rank_time.date(),
            rank_time=rank_time,
            symbol=self.symbol,
            rank_position=self.rank_position,
            universe_size=self.universe_size,
            direction=self.direction,
            classification=self.classification,
            total_score=self.total_score,
            movement_score=self.movement_score,
            quality_score=self.quality_score,
            range_penalty=self.range_penalty,
            stall_penalty=self.stall_penalty,
            close_price=self.close_price,
            previous_close=self.previous_close,
            today_open=self.today_open,
            gap_pct=self.gap_pct,
            session_move_pct=self.session_move_pct,
            post_open_move_pct=self.post_open_move_pct,
            move_15m_pct=self.move_15m_pct,
            move_30m_pct=self.move_30m_pct,
            move_60m_pct=self.move_60m_pct,
            move_15m_atr=self.move_15m_atr,
            move_30m_atr=self.move_30m_atr,
            move_60m_atr=self.move_60m_atr,
            atr_value=self.atr_value,
            atr_pct=self.atr_pct,
            directional_efficiency=self.directional_efficiency,
            recent_efficiency=self.recent_efficiency,
            direction_consistency=self.direction_consistency,
            acceleration_score=self.acceleration_score,
            volume_ratio=self.volume_ratio,
            freshness_score=self.freshness_score,
            bars_since_extreme=self.bars_since_extreme,
            range_active=self.range_context.active,
            range_episode_id=self.range_context.episode_id,
            range_id=self.range_context.range_id,
            range_age_bars=self.range_context.age_bars,
            range_width_pct=self.range_context.width_pct,
            containment_ratio=self.range_context.containment_ratio,
            midpoint_crossings=self.range_context.midpoint_crossings,
            vwap_crossings=self.range_context.vwap_crossings,
            failed_escape_count=self.range_context.failed_escape_count,
            rearm_required=self.range_context.rearm_required,
            attempt_limit_reached=self.range_context.attempt_limit_reached,
            metrics_json=self.metrics,
        )


def _to_ist_naive(value: datetime) -> datetime:
    if value.tzinfo is not None:
        value = value.astimezone(IST)
    return value.replace(tzinfo=None)


def _finite(value: Any) -> Optional[float]:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _positive(value: Any) -> Optional[float]:
    number = _finite(value)
    return number if number is not None and number > 0.0 else None


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(max(float(value), low), high)


def _norm_abs(value: Optional[float], normalizer: float) -> float:
    if value is None or normalizer <= 0.0:
        return 0.0
    return _clip(abs(float(value)) / float(normalizer))


def _pct(current: Optional[float], anchor: Optional[float]) -> Optional[float]:
    if current is None or anchor is None or anchor <= 0.0:
        return None
    return (current - anchor) / anchor * 100.0


def _sign(value: Optional[float], *, epsilon: float = 1e-9) -> int:
    if value is None or abs(float(value)) <= epsilon:
        return 0
    return 1 if float(value) > 0.0 else -1


def _crossings(values: Sequence[Optional[float]], *, zero: float = 0.0) -> int:
    count = 0
    previous_side = 0
    for value in values:
        if value is None or not math.isfinite(float(value)):
            continue
        side = _sign(float(value) - zero)
        if side == 0:
            continue
        if previous_side and side != previous_side:
            count += 1
        previous_side = side
    return count


def _efficiency(closes: Sequence[float]) -> Optional[float]:
    if len(closes) < 2:
        return None
    travel = sum(abs(b - a) for a, b in zip(closes, closes[1:]))
    if travel <= 0.0:
        return 0.0
    return _clip(abs(closes[-1] - closes[0]) / travel)


def _window_values(snapshot: SnapshotSchema) -> Dict[str, Optional[float]]:
    return {
        "m15_pct": _finite(snapshot.market_windows.m15.move_pct),
        "m30_pct": _finite(snapshot.market_windows.m30.move_pct),
        "m60_pct": _finite(snapshot.market_windows.m60.move_pct),
        "sod_pct": _finite(snapshot.market_windows.sod.move_pct),
        "m15_atr": _finite(snapshot.market_windows.m15.move_atr),
        "m30_atr": _finite(snapshot.market_windows.m30.move_atr),
        "m60_atr": _finite(snapshot.market_windows.m60.move_atr),
    }


def _direction(values: Dict[str, Optional[float]]) -> str:
    # Prefer the 30-minute path, then 15-minute, 60-minute and session move.
    for key in ("m30_atr", "m15_atr", "m60_atr", "sod_pct"):
        side = _sign(values.get(key))
        if side > 0:
            return "UP"
        if side < 0:
            return "DOWN"
    return "FLAT"


def _direction_consistency(
    values: Dict[str, Optional[float]],
    direction: str,
) -> float:
    expected = 1 if direction == "UP" else (-1 if direction == "DOWN" else 0)
    if expected == 0:
        return 0.0
    signs = [
        _sign(values.get("m15_atr")),
        _sign(values.get("m30_atr")),
        _sign(values.get("m60_atr")),
    ]
    usable = [side for side in signs if side != 0]
    if not usable:
        return 0.0
    return sum(1 for side in usable if side == expected) / len(usable)


def _acceleration_score(values: Dict[str, Optional[float]]) -> float:
    m15 = abs(values["m15_atr"]) if values["m15_atr"] is not None else 0.0
    m30 = abs(values["m30_atr"]) if values["m30_atr"] is not None else 0.0
    # Compare recent ATR movement per minute with the 30-minute rate.
    rate15 = m15 / 15.0
    rate30 = m30 / 30.0
    if rate15 <= 0.0 and rate30 <= 0.0:
        return 0.0
    if rate30 <= 0.0:
        return 1.0
    return _clip(0.5 + (rate15 - rate30) / max(rate30, 1e-9) * 0.5)


def _bars_since_extreme(
    history: Sequence[SnapshotSchema],
    direction: str,
) -> int:
    rows = list(history)
    if not rows:
        return 0
    if direction == "UP":
        index = max(range(len(rows)), key=lambda idx: float(rows[idx].bar.high))
    elif direction == "DOWN":
        index = min(range(len(rows)), key=lambda idx: float(rows[idx].bar.low))
    else:
        return len(rows) - 1
    return max(0, len(rows) - 1 - index)


def _range_context(
    snapshot: SnapshotSchema,
    history: Sequence[SnapshotSchema],
) -> RangeContext:
    low: Optional[float] = None
    high: Optional[float] = None
    episode_id: Optional[str] = None
    range_id: Optional[str] = None
    started_at: Optional[datetime] = None
    age_bars = 0
    containment: Optional[float] = None
    failed_escape_count = 0
    rearm_required = False
    attempt_limit_reached = False

    if snapshot.auction.status == "OK" and snapshot.auction.lifecycle is not None:
        balance = snapshot.auction.lifecycle.balance
        if balance.frozen_low is not None and balance.frozen_high is not None:
            low = float(balance.frozen_low)
            high = float(balance.frozen_high)
            episode_id = balance.episode_id
            range_id = balance.range_id
            started_at = balance.started_at
            containment = float(balance.containment_ratio)
            failed_escape_count = int(balance.failed_escape_count)
            rearm_required = bool(balance.rearm_required)
            attempt_limit_reached = bool(balance.attempt_limit_reached)

    accepted = snapshot.structure.accepted
    if low is None or high is None:
        if (
            not bool(accepted.range.provisional)
            and accepted.range.low is not None
            and accepted.range.high is not None
        ):
            low = float(accepted.range.low)
            high = float(accepted.range.high)
            range_id = accepted.range.range_id
            started_at = accepted.range.start_time or accepted.promoted_time
            age_bars = int(accepted.age_bars)
            if accepted.metrics.close_occupancy_ratio is not None:
                containment = float(accepted.metrics.close_occupancy_ratio)

    active = bool(low is not None and high is not None and high > low > 0.0)
    if not active:
        return RangeContext(
            active=False,
            episode_id=episode_id,
            range_id=range_id,
            started_at=started_at,
            low=None,
            high=None,
            age_bars=0,
            width_pct=None,
            containment_ratio=containment,
            midpoint_crossings=0,
            vwap_crossings=0,
            failed_escape_count=failed_escape_count,
            rearm_required=rearm_required,
            attempt_limit_reached=attempt_limit_reached,
            total_travel_range_multiple=None,
        )

    rows = list(history)
    if started_at is not None:
        start_naive = _to_ist_naive(started_at)
        rows = [row for row in rows if _to_ist_naive(row.snapshot_time) >= start_naive]
    if not rows:
        rows = [snapshot]
    age_bars = max(age_bars, len(rows))

    closes = [float(row.close) for row in rows]
    midpoint = (high + low) / 2.0
    midpoint_crossings = _crossings(closes, zero=midpoint)
    vwap_deltas: List[Optional[float]] = []
    for row in rows:
        vwap = _positive(row.indicators.vwap.value)
        vwap_deltas.append(float(row.close) - vwap if vwap is not None else None)
    vwap_crossings = _crossings(vwap_deltas)
    travel = sum(abs(b - a) for a, b in zip(closes, closes[1:]))
    width = high - low
    width_pct = width / midpoint * 100.0

    return RangeContext(
        active=True,
        episode_id=episode_id,
        range_id=range_id,
        started_at=started_at,
        low=low,
        high=high,
        age_bars=age_bars,
        width_pct=width_pct,
        containment_ratio=containment,
        midpoint_crossings=midpoint_crossings,
        vwap_crossings=vwap_crossings,
        failed_escape_count=failed_escape_count,
        rearm_required=rearm_required,
        attempt_limit_reached=attempt_limit_reached,
        total_travel_range_multiple=(travel / width if width > 0.0 else None),
    )


def _range_penalty(
    context: RangeContext,
    efficiency: Optional[float],
) -> Tuple[float, Dict[str, float]]:
    if not context.active:
        return 0.0, {
            "age": 0.0,
            "narrow": 0.0,
            "containment": 0.0,
            "inefficiency": 0.0,
            "rotation": 0.0,
            "vwap_rotation": 0.0,
            "failed_escape": 0.0,
            "rearm": 0.0,
        }

    age = _clip(context.age_bars / 12.0)
    narrow = (
        _clip(1.0 - float(context.width_pct) / 1.20)
        if context.width_pct is not None
        else 0.0
    )
    containment = (
        _clip((float(context.containment_ratio) - 0.50) / 0.50)
        if context.containment_ratio is not None
        else 0.0
    )
    inefficiency = 1.0 - _clip(efficiency if efficiency is not None else 0.0)
    rotation = _clip(context.midpoint_crossings / 4.0)
    vwap_rotation = _clip(context.vwap_crossings / 3.0)
    failed_escape = _clip(context.failed_escape_count / 2.0)
    rearm = 1.0 if (context.rearm_required or context.attempt_limit_reached) else 0.0

    parts = {
        "age": age,
        "narrow": narrow,
        "containment": containment,
        "inefficiency": inefficiency,
        "rotation": rotation,
        "vwap_rotation": vwap_rotation,
        "failed_escape": failed_escape,
        "rearm": rearm,
    }
    penalty = 100.0 * (
        0.12 * age
        + 0.13 * narrow
        + 0.18 * containment
        + 0.20 * inefficiency
        + 0.14 * rotation
        + 0.08 * vwap_rotation
        + 0.08 * failed_escape
        + 0.07 * rearm
    )
    return round(_clip(penalty, 0.0, 100.0), 4), parts


class StockRankEvaluator:
    """Pure one-symbol ranking calculation."""

    def __init__(self, config: StockRankConfig = STOCK_RANK_CONFIG) -> None:
        self.config = config

    def evaluate(
        self,
        snapshot: SnapshotSchema,
        history: Sequence[SnapshotSchema],
    ) -> StockRankResult:
        rows = sorted(list(history), key=lambda row: _to_ist_naive(row.snapshot_time))
        if not rows or _to_ist_naive(rows[-1].snapshot_time) != _to_ist_naive(snapshot.snapshot_time):
            rows = [*rows, snapshot]
            rows.sort(key=lambda row: _to_ist_naive(row.snapshot_time))
        rows = rows[-self.config.history_bars :]

        windows = _window_values(snapshot)
        direction = _direction(windows)
        closes = [float(row.close) for row in rows]
        recent_closes = closes[-self.config.recent_efficiency_bars :]
        day_efficiency = _efficiency(closes)
        recent_efficiency = _efficiency(recent_closes)
        direction_consistency = _direction_consistency(windows, direction)
        acceleration = _acceleration_score(windows)
        bars_since_extreme = _bars_since_extreme(rows, direction)
        freshness = _clip(
            1.0 - bars_since_extreme / float(self.config.freshness_bars_norm)
        )

        recent_pct_component = (
            0.45 * _norm_abs(windows["m15_pct"], self.config.move_15m_norm_pct)
            + 0.35 * _norm_abs(windows["m30_pct"], self.config.move_30m_norm_pct)
            + 0.20 * _norm_abs(windows["m60_pct"], self.config.move_60m_norm_pct)
        )
        recent_atr_component = (
            0.45 * _norm_abs(windows["m15_atr"], self.config.move_15m_norm_atr)
            + 0.35 * _norm_abs(windows["m30_atr"], self.config.move_30m_norm_atr)
            + 0.20 * _norm_abs(windows["m60_atr"], self.config.move_60m_norm_atr)
        )
        efficiency_component = recent_efficiency if recent_efficiency is not None else 0.0
        volume_ratio = _finite(snapshot.volume.bar_rvol)
        volume_component = _clip((volume_ratio or 0.0) / self.config.bar_rvol_norm)
        session_component = _norm_abs(
            windows["sod_pct"], self.config.session_move_norm_pct
        )

        movement_components = {
            "recent_pct": recent_pct_component,
            "recent_atr": recent_atr_component,
            "efficiency": efficiency_component,
            "volume": volume_component,
            "freshness": freshness,
            "direction_consistency": direction_consistency,
            "acceleration": acceleration,
            "session_move": session_component,
        }
        movement_score = 100.0 * (
            self.config.weight_recent_pct * recent_pct_component
            + self.config.weight_recent_atr * recent_atr_component
            + self.config.weight_efficiency * efficiency_component
            + self.config.weight_volume * volume_component
            + self.config.weight_freshness * freshness
            + self.config.weight_direction_consistency * direction_consistency
            + self.config.weight_acceleration * acceleration
            + self.config.weight_session_move * session_component
        )
        quality_score = 100.0 * (
            0.40 * efficiency_component
            + 0.25 * freshness
            + 0.20 * direction_consistency
            + 0.15 * acceleration
        )

        range_context = _range_context(snapshot, rows)
        range_penalty, range_components = _range_penalty(
            range_context, recent_efficiency
        )

        recent_30_abs = abs(windows["m30_pct"] or 0.0)
        low_recent_move = _clip(
            1.0
            - recent_30_abs
            / max(self.config.stalled_gap_max_recent_30m_pct, 1e-9)
        )
        stale_extreme = _clip(
            bars_since_extreme / float(self.config.freshness_bars_norm)
        )
        stall_penalty = 100.0 * (0.55 * low_recent_move + 0.45 * stale_extreme)
        stall_penalty = round(_clip(stall_penalty, 0.0, 100.0), 4)

        total_score = (
            self.config.total_movement_weight * movement_score
            + self.config.total_quality_weight * quality_score
            - self.config.range_penalty_weight * range_penalty
            - self.config.stall_penalty_weight * stall_penalty
        )
        total_score = round(_clip(total_score, 0.0, 100.0), 4)
        movement_score = round(_clip(movement_score, 0.0, 100.0), 4)
        quality_score = round(_clip(quality_score, 0.0, 100.0), 4)

        previous_close = _positive(snapshot.levels.prev_day.close)
        today_open = _positive(snapshot.levels.today.open)
        close = float(snapshot.close)
        gap_pct = _pct(today_open, previous_close)
        post_open_move_pct = _pct(close, today_open)
        session_move_pct = windows["sod_pct"]

        stalled_gap = bool(
            gap_pct is not None
            and abs(gap_pct) >= self.config.stalled_gap_min_abs_pct
            and recent_30_abs <= self.config.stalled_gap_max_recent_30m_pct
            and range_penalty >= self.config.stalled_gap_min_range_penalty
        )
        if range_penalty >= self.config.range_bound_penalty_threshold:
            classification = "RANGE_BOUND"
        elif stalled_gap:
            classification = "STALLED_GAP_RANGE"
        elif total_score >= self.config.moving_score_threshold and direction != "FLAT":
            classification = f"MOVING_{direction}"
        elif total_score >= self.config.developing_score_threshold and direction != "FLAT":
            classification = f"DEVELOPING_{direction}"
        else:
            classification = "NEUTRAL"

        atr = float(snapshot.indicators.atr.value)
        metrics = {
            "window_values": windows,
            "movement_components": movement_components,
            "range_components": range_components,
            "range_context": {
                "active": range_context.active,
                "episode_id": range_context.episode_id,
                "range_id": range_context.range_id,
                "started_at": range_context.started_at,
                "low": range_context.low,
                "high": range_context.high,
                "age_bars": range_context.age_bars,
                "width_pct": range_context.width_pct,
                "containment_ratio": range_context.containment_ratio,
                "midpoint_crossings": range_context.midpoint_crossings,
                "vwap_crossings": range_context.vwap_crossings,
                "failed_escape_count": range_context.failed_escape_count,
                "rearm_required": range_context.rearm_required,
                "attempt_limit_reached": range_context.attempt_limit_reached,
                "total_travel_range_multiple": range_context.total_travel_range_multiple,
            },
            "history_bars_used": len(rows),
            "stalled_gap": stalled_gap,
            "scoring_version": "STOCK_RANK_DIAGNOSTIC_V1",
        }

        return StockRankResult(
            symbol=snapshot.symbol.strip().upper(),
            rank_time=snapshot.snapshot_time,
            rank_position=1,
            universe_size=1,
            direction=direction,
            classification=classification,
            total_score=total_score,
            movement_score=movement_score,
            quality_score=quality_score,
            range_penalty=range_penalty,
            stall_penalty=stall_penalty,
            close_price=close,
            previous_close=previous_close,
            today_open=today_open,
            gap_pct=gap_pct,
            session_move_pct=session_move_pct,
            post_open_move_pct=post_open_move_pct,
            move_15m_pct=windows["m15_pct"],
            move_30m_pct=windows["m30_pct"],
            move_60m_pct=windows["m60_pct"],
            move_15m_atr=windows["m15_atr"],
            move_30m_atr=windows["m30_atr"],
            move_60m_atr=windows["m60_atr"],
            atr_value=atr,
            atr_pct=_finite(snapshot.indicators.atr.pct),
            directional_efficiency=day_efficiency,
            recent_efficiency=recent_efficiency,
            direction_consistency=round(direction_consistency, 6),
            acceleration_score=round(acceleration, 6),
            volume_ratio=volume_ratio,
            freshness_score=round(freshness, 6),
            bars_since_extreme=bars_since_extreme,
            range_context=range_context,
            metrics=metrics,
        )

    def rank(
        self,
        snapshots: Sequence[SnapshotSchema],
        histories: Dict[str, Sequence[SnapshotSchema]],
    ) -> List[StockRankResult]:
        evaluated: List[StockRankResult] = []
        for snapshot in snapshots:
            symbol = snapshot.symbol.strip().upper()
            evaluated.append(self.evaluate(snapshot, histories.get(symbol, ())))
        evaluated.sort(
            key=lambda row: (
                -row.total_score,
                -row.movement_score,
                row.range_penalty,
                row.symbol,
            )
        )
        size = len(evaluated)
        return [
            replace(row, rank_position=index, universe_size=size)
            for index, row in enumerate(evaluated, start=1)
        ]


class StockRankService:
    """Database/report orchestration around the pure evaluator."""

    def __init__(self, config: StockRankConfig = STOCK_RANK_CONFIG) -> None:
        self.config = config
        self.evaluator = StockRankEvaluator(config)

    def _symbols(self, *, active_only: bool) -> List[str]:
        rows = SymbolSchema.fetch_symbols(
            active=1 if active_only else None,
            type_filter="EQ",
        ) or []
        symbols = sorted({row.symbol.strip().upper() for row in rows if row.symbol.strip()})
        if not symbols:
            raise ValueError("StockRank found no enabled EQ symbols")
        return symbols

    def run(
        self,
        *,
        trading_day: Optional[date] = None,
        through_time: Optional[datetime] = None,
        symbols: Optional[Sequence[str]] = None,
        active_only: Optional[bool] = None,
        report_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self.config.enabled:
            raise RuntimeError("StockRank is disabled")

        trading_day = trading_day or business_now().date()
        use_active = self.config.active_symbols_only if active_only is None else bool(active_only)
        requested_symbols = sorted(
            {
                str(symbol or "").strip().upper()
                for symbol in (symbols or self._symbols(active_only=use_active))
                if str(symbol or "").strip()
            }
        )
        rank_time, cadence_coverage, requested_count = SnapshotSchema.fetch_latest_rankable_time(
            trading_day=trading_day,
            symbols=requested_symbols,
            minimum_coverage_ratio=self.config.minimum_snapshot_coverage_ratio,
            through_time=(_to_ist_naive(through_time) if through_time is not None else None),
        )
        snapshots = SnapshotSchema.fetch_rank_snapshots_at_time(
            rank_time=rank_time,
            symbols=requested_symbols,
        )
        actual_symbols = {snapshot.symbol.strip().upper() for snapshot in snapshots}
        missing_symbols = sorted(set(requested_symbols) - actual_symbols)

        histories: Dict[str, Sequence[SnapshotSchema]] = {}
        failures: Dict[str, str] = {}
        valid_snapshots: List[SnapshotSchema] = []
        for snapshot in snapshots:
            symbol = snapshot.symbol.strip().upper()
            try:
                histories[symbol] = SnapshotSchema.fetch_day_context_for_advisor(
                    symbol=symbol,
                    trading_day=trading_day,
                    through_time=snapshot.snapshot_time,
                    limit=self.config.history_bars,
                    include_current=True,
                )
                valid_snapshots.append(snapshot)
            except Exception as exc:
                failures[symbol] = f"{type(exc).__name__}: {exc}"
                logger.exception(
                    "StockRank failed causal snapshot history for %s @ %s",
                    symbol,
                    snapshot.snapshot_time,
                )

        ranked = self.evaluator.rank(valid_snapshots, histories)
        if not ranked:
            raise RuntimeError("StockRank produced no valid rankings")

        rank_time_naive = _to_ist_naive(rank_time)
        run_id = f"STOCK_RANK:{rank_time_naive.strftime('%Y%m%dT%H%M%S')}"
        schemas = [row.to_schema(run_id=run_id) for row in ranked]
        persisted = StockRankSchema.upsert_many(schemas)
        report_path = self._export_report(
            rows=persisted,
            report_dir=report_dir or self.config.report_dir,
        )

        summary = {
            "run_id": run_id,
            "trading_day": trading_day.isoformat(),
            "rank_time": rank_time_naive.isoformat(sep=" "),
            "requested_symbols": requested_count,
            "cadence_coverage": cadence_coverage,
            "ranked_symbols": len(persisted),
            "missing_symbols": missing_symbols,
            "failed_symbols": failures,
            "range_bound_count": sum(
                1
                for row in persisted
                if row.classification in {"RANGE_BOUND", "STALLED_GAP_RANGE"}
            ),
            "moving_count": sum(
                1 for row in persisted if row.classification.startswith("MOVING_")
            ),
            "developing_count": sum(
                1 for row in persisted if row.classification.startswith("DEVELOPING_")
            ),
            "report_path": str(report_path),
        }
        self._log_summary(summary, persisted)
        return {"summary": summary, "rows": persisted}

    def _resolve_report_dir(self, report_dir: str) -> Path:
        path = Path(report_dir)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _export_report(
        self,
        *,
        rows: Sequence[StockRankSchema],
        report_dir: str,
    ) -> Path:
        target_dir = self._resolve_report_dir(report_dir)
        rank_time = rows[0].rank_time
        filename = f"stock_rank_{rank_time.strftime('%Y%m%d_%H%M%S')}.csv"
        path = target_dir / filename

        report_rows: List[Dict[str, Any]] = []
        for row in rows:
            payload = row.report_row()
            payload["metrics_json"] = json.dumps(
                payload["metrics_json"],
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            report_rows.append(payload)

        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(report_rows[0].keys()))
            writer.writeheader()
            writer.writerows(report_rows)
        return path

    def _log_summary(
        self,
        summary: Dict[str, Any],
        rows: Sequence[StockRankSchema],
    ) -> None:
        logger.info(
            "STOCK_RANK_SUMMARY | run_id=%s rank_time=%s requested=%d coverage=%d ranked=%d moving=%d developing=%d range_bound=%d missing=%d failed=%d report=%s",
            summary["run_id"],
            summary["rank_time"],
            summary["requested_symbols"],
            summary["cadence_coverage"],
            summary["ranked_symbols"],
            summary["moving_count"],
            summary["developing_count"],
            summary["range_bound_count"],
            len(summary["missing_symbols"]),
            len(summary["failed_symbols"]),
            summary["report_path"],
        )

        for row in rows[: self.config.top_log_count]:
            logger.info(
                "STOCK_RANK_TOP | rank=%d symbol=%s score=%.2f class=%s dir=%s move15=%s move30=%s move60=%s efficiency=%s range_penalty=%.2f stall_penalty=%.2f",
                row.rank_position,
                row.symbol,
                row.total_score,
                row.classification,
                row.direction,
                _format_metric(row.move_15m_pct),
                _format_metric(row.move_30m_pct),
                _format_metric(row.move_60m_pct),
                _format_metric(row.recent_efficiency),
                row.range_penalty,
                row.stall_penalty,
            )

        range_rows = [
            row
            for row in rows
            if row.classification in {"RANGE_BOUND", "STALLED_GAP_RANGE"}
        ][: self.config.range_log_count]
        for row in range_rows:
            logger.info(
                "STOCK_RANK_RANGE | rank=%d symbol=%s class=%s score=%.2f gap=%s move30=%s range_age=%d width=%s containment=%s midpoint_cross=%d vwap_cross=%d failures=%d penalty=%.2f",
                row.rank_position,
                row.symbol,
                row.classification,
                row.total_score,
                _format_metric(row.gap_pct),
                _format_metric(row.move_30m_pct),
                row.range_age_bars,
                _format_metric(row.range_width_pct),
                _format_metric(row.containment_ratio),
                row.midpoint_crossings,
                row.vwap_crossings,
                row.failed_escape_count,
                row.range_penalty,
            )

        if summary["missing_symbols"]:
            logger.warning(
                "STOCK_RANK_MISSING | symbols=%s",
                ",".join(summary["missing_symbols"]),
            )
        if summary["failed_symbols"]:
            logger.warning(
                "STOCK_RANK_FAILED | %s",
                summary["failed_symbols"],
            )


def _format_metric(value: Optional[float]) -> str:
    return "NA" if value is None else f"{float(value):.4f}"


def _parse_day(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    return date.fromisoformat(value)


def _parse_as_of(value: Optional[str], trading_day: date) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    if "T" in text or " " in text:
        parsed = datetime.fromisoformat(text)
    else:
        parsed = datetime.combine(trading_day, dtime.fromisoformat(text))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Persist and export diagnostic snapshot-based stock ranking. "
            "All options have visible source defaults and command-line overrides."
        )
    )
    parser.add_argument(
        "--trading-day",
        default=DEFAULT_TRADING_DAY,
        help=(
            "Trading day YYYY-MM-DD "
            "(default: current IST business day)"
            if DEFAULT_TRADING_DAY is None
            else f"Trading day YYYY-MM-DD (default: {DEFAULT_TRADING_DAY})"
        ),
    )
    parser.add_argument(
        "--as-of",
        default=DEFAULT_AS_OF,
        help=(
            "HH:MM:SS or ISO datetime upper bound for rank cadence "
            "(default: latest common completed cadence)"
            if DEFAULT_AS_OF is None
            else f"Rank cadence upper bound (default: {DEFAULT_AS_OF})"
        ),
    )
    parser.add_argument(
        "--symbols",
        default=DEFAULT_SYMBOLS,
        help=(
            "Comma-separated symbols "
            "(default: universe selected by --all-enabled/--no-all-enabled)"
            if DEFAULT_SYMBOLS is None
            else f"Comma-separated symbols (default: {DEFAULT_SYMBOLS})"
        ),
    )
    parser.add_argument(
        "--all-enabled",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_ALL_ENABLED,
        help=(
            "Rank all enabled EQ symbols; use --no-all-enabled for active-only "
            f"(default: {DEFAULT_ALL_ENABLED})"
        ),
    )
    parser.add_argument(
        "--report-dir",
        default=DEFAULT_REPORT_DIR,
        help=f"Report directory (default: {DEFAULT_REPORT_DIR})",
    )
    parser.add_argument(
        "--log-file",
        default=DEFAULT_LOG_FILE,
        help=f"Log file (default: {DEFAULT_LOG_FILE})",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    setup_logging(log_file=args.log_file)
    run_logger = logging.getLogger(__name__)

    trading_day = _parse_day(args.trading_day) or business_now().date()
    through_time = _parse_as_of(args.as_of, trading_day)
    symbols = (
        [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
        if args.symbols
        else None
    )

    run_logger.info(
        "=== StockRank diagnostic run starting | day=%s as_of=%s universe=%s ===",
        trading_day,
        through_time,
        "ALL_ENABLED" if args.all_enabled else "ACTIVE",
    )
    try:
        StockRankService().run(
            trading_day=trading_day,
            through_time=through_time,
            symbols=symbols,
            active_only=not args.all_enabled,
            report_dir=args.report_dir,
        )
        return 0
    except Exception:
        run_logger.exception("StockRank diagnostic run failed")
        return 1
    finally:
        run_logger.info("=== StockRank diagnostic run stopped ===")


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_TRADING_DAY",
    "DEFAULT_AS_OF",
    "DEFAULT_SYMBOLS",
    "DEFAULT_ALL_ENABLED",
    "DEFAULT_REPORT_DIR",
    "DEFAULT_LOG_FILE",
    "RangeContext",
    "StockRankResult",
    "StockRankEvaluator",
    "StockRankService",
    "main",
]
