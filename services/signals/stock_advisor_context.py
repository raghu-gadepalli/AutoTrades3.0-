"""Pure StockAdvisor context calculations.

No database access and no setup creation live here.  Functions consume causal
snapshots/opportunities and return immutable summaries used by the Advisor.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from configs.stock_advisor_config import (
    BarrierPolicyConfig,
    DeferredEntryFreshnessPolicyConfig,
    MatureRangeChurnPolicyConfig,
    RepeatedEpisodePolicyConfig,
)
from enums.auction_engine import AuctionEventType, DirectionalBias, TradeSide
from schemas.snapshot import SnapshotSchema
from schemas.stock_opportunity import StockOpportunitySchema


@dataclass(frozen=True)
class AdvisorDayPathSummary:
    sample_count: int
    episode_age_bars: int
    range_width_pct: Optional[float]
    containment_ratio: Optional[float]
    path_efficiency: Optional[float]
    midpoint_crossings: int
    vwap_crossings: int
    total_travel_points: float
    total_travel_range_multiple: Optional[float]
    structure_flips: int
    close_first: Optional[float]
    close_last: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class AdvisorBarrierSummary:
    active: bool
    barrier_type: Optional[str]
    barrier_price: Optional[float]
    distance_points: Optional[float]
    distance_atr: Optional[float]
    distance_pct: Optional[float]
    threshold_points: Optional[float]
    candidates: Tuple[Tuple[str, float], ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "active": self.active,
            "barrier_type": self.barrier_type,
            "barrier_price": self.barrier_price,
            "distance_points": self.distance_points,
            "distance_atr": self.distance_atr,
            "distance_pct": self.distance_pct,
            "threshold_points": self.threshold_points,
            "candidates": [
                {"type": kind, "price": price} for kind, price in self.candidates
            ],
        }


@dataclass(frozen=True)
class AdvisorEpisodeHistorySummary:
    prior_count_day: int
    prior_count_same_episode: int
    prior_same_side_same_episode: int
    prior_opposite_side_same_episode: int
    prior_opportunity_keys: Tuple[str, ...]
    exhausted_objective_context: bool
    exhaustion_facts: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prior_count_day": self.prior_count_day,
            "prior_count_same_episode": self.prior_count_same_episode,
            "prior_same_side_same_episode": self.prior_same_side_same_episode,
            "prior_opposite_side_same_episode": self.prior_opposite_side_same_episode,
            "prior_opportunity_keys": list(self.prior_opportunity_keys),
            "exhausted_objective_context": self.exhausted_objective_context,
            "exhaustion_facts": list(self.exhaustion_facts),
        }


@dataclass(frozen=True)
class DeferredEntryFreshnessSummary:
    applicable: bool
    fresh: bool
    reason: str
    age_minutes: Optional[float]
    matching_fresh_events: Tuple[str, ...]
    pullback_detected: bool
    pullback_atr: Optional[float]
    resumption_detected: bool
    resumption_atr: Optional[float]
    consolidation_detected: bool
    consolidation_range_atr: Optional[float]
    consolidation_break_detected: bool
    bars_considered: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "applicable": self.applicable,
            "fresh": self.fresh,
            "reason": self.reason,
            "age_minutes": self.age_minutes,
            "matching_fresh_events": list(self.matching_fresh_events),
            "pullback_detected": self.pullback_detected,
            "pullback_atr": self.pullback_atr,
            "resumption_detected": self.resumption_detected,
            "resumption_atr": self.resumption_atr,
            "consolidation_detected": self.consolidation_detected,
            "consolidation_range_atr": self.consolidation_range_atr,
            "consolidation_break_detected": self.consolidation_break_detected,
            "bars_considered": self.bars_considered,
        }


def _normalise_time(value: datetime) -> datetime:
    if value.tzinfo is not None:
        from utils.datetime_utils import IST
        value = value.astimezone(IST)
    return value.replace(tzinfo=None)


def _crossings(values: Sequence[Optional[float]], *, zero: float = 0.0) -> int:
    last_side = 0
    count = 0
    for value in values:
        if value is None or not math.isfinite(float(value)):
            continue
        delta = float(value) - zero
        side = 1 if delta > 0 else (-1 if delta < 0 else 0)
        if side == 0:
            continue
        if last_side and side != last_side:
            count += 1
        last_side = side
    return count


def summarise_day_path(
    snapshots: Sequence[SnapshotSchema],
    *,
    range_low: Optional[float],
    range_high: Optional[float],
    episode_started_at: Optional[datetime],
    containment_ratio: Optional[float],
) -> AdvisorDayPathSummary:
    rows = list(snapshots)
    if episode_started_at is not None:
        cutoff = _normalise_time(episode_started_at)
        rows = [
            row
            for row in rows
            if _normalise_time(row.snapshot_time) >= cutoff
        ]
    closes = [float(row.close) for row in rows]
    travel = sum(abs(b - a) for a, b in zip(closes, closes[1:]))
    displacement = abs(closes[-1] - closes[0]) if len(closes) >= 2 else 0.0
    efficiency = (displacement / travel) if travel > 0 else None

    width = None
    width_pct = None
    midpoint_crossings = 0
    travel_multiple = None
    if range_low is not None and range_high is not None:
        low = float(range_low)
        high = float(range_high)
        if not math.isfinite(low) or not math.isfinite(high) or low <= 0 or high <= low:
            raise ValueError("Advisor range geometry must be positive and ordered")
        width = high - low
        midpoint = (high + low) / 2.0
        width_pct = width / midpoint * 100.0
        midpoint_crossings = _crossings(closes, zero=midpoint)
        travel_multiple = travel / width if width > 0 else None

    vwap_sides: List[Optional[float]] = []
    for row in rows:
        value = row.indicators.vwap.value
        if value is None:
            vwap_sides.append(None)
        else:
            vwap_sides.append(float(row.close) - float(value))
    vwap_crossings = _crossings(vwap_sides)

    structure_flips = 0
    if rows:
        structure_flips = max(
            int(row.structure.flip_count_today) for row in rows
        ) - min(int(row.structure.flip_count_today) for row in rows)
        structure_flips = max(0, structure_flips)

    return AdvisorDayPathSummary(
        sample_count=len(rows),
        episode_age_bars=len(rows),
        range_width_pct=width_pct,
        containment_ratio=(
            float(containment_ratio) if containment_ratio is not None else None
        ),
        path_efficiency=efficiency,
        midpoint_crossings=midpoint_crossings,
        vwap_crossings=vwap_crossings,
        total_travel_points=travel,
        total_travel_range_multiple=travel_multiple,
        structure_flips=structure_flips,
        close_first=(closes[0] if closes else None),
        close_last=(closes[-1] if closes else None),
    )


def is_mature_narrow_range_churn(
    summary: AdvisorDayPathSummary,
    *,
    failed_escape_count: int,
    policy: MatureRangeChurnPolicyConfig,
) -> Tuple[bool, Tuple[str, ...]]:
    facts: List[str] = []
    if summary.episode_age_bars < policy.min_episode_age_bars:
        return False, ()
    if summary.range_width_pct is None or summary.range_width_pct > policy.max_range_width_pct:
        return False, ()
    if (
        summary.containment_ratio is None
        or summary.containment_ratio < policy.min_containment_ratio
    ):
        return False, ()
    if (
        summary.path_efficiency is None
        or summary.path_efficiency > policy.max_path_efficiency
    ):
        return False, ()
    if summary.midpoint_crossings < policy.min_midpoint_crossings:
        return False, ()

    supporting = 0
    if summary.vwap_crossings >= policy.min_vwap_crossings:
        supporting += 1
        facts.append("VWAP_CROSSINGS")
    if (
        summary.total_travel_range_multiple is not None
        and summary.total_travel_range_multiple
        >= policy.min_total_travel_range_multiple
    ):
        supporting += 1
        facts.append("REPEATED_TRAVEL_WITHIN_RANGE")
    if summary.structure_flips >= policy.min_structure_flips:
        supporting += 1
        facts.append("STRUCTURE_FLIPS")
    if int(failed_escape_count) > 0:
        supporting += 1
        facts.append("FAILED_ESCAPE_HISTORY")

    matched = supporting >= policy.min_supporting_churn_signals
    if matched:
        facts = [
            "MATURE_RANGE",
            "NARROW_RANGE",
            "HIGH_CONTAINMENT",
            "LOW_PATH_EFFICIENCY",
            "MIDPOINT_ROTATION",
            *facts,
        ]
    return matched, tuple(facts)


def _finite_positive(value: Any) -> Optional[float]:
    if value is None:
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def summarise_barrier(
    snapshot: SnapshotSchema,
    prior_snapshots: Sequence[SnapshotSchema],
    *,
    side: TradeSide,
    policy: BarrierPolicyConfig,
    excluded_price: Optional[float] = None,
) -> AdvisorBarrierSummary:
    close = float(snapshot.close)
    atr = float(snapshot.indicators.atr.value)
    threshold = max(atr * policy.near_atr, close * policy.near_pct / 100.0)
    candidates: List[Tuple[str, float]] = []

    def add(kind: str, value: Any) -> None:
        price = _finite_positive(value)
        if price is None:
            return
        if excluded_price is not None and math.isclose(
            price, float(excluded_price), rel_tol=1e-9, abs_tol=max(1e-9, atr * 0.01)
        ):
            return
        if side is TradeSide.BUY and price > close:
            candidates.append((kind, price))
        elif side is TradeSide.SELL and price < close:
            candidates.append((kind, price))

    levels = snapshot.levels
    if policy.include_opening_range and levels.opening_range.ready:
        add(
            "ORH" if side is TradeSide.BUY else "ORL",
            levels.opening_range.high if side is TradeSide.BUY else levels.opening_range.low,
        )
    if policy.include_previous_day_extreme:
        add(
            "PDH" if side is TradeSide.BUY else "PDL",
            levels.prev_day.high if side is TradeSide.BUY else levels.prev_day.low,
        )
    if policy.include_prior_session_extreme and prior_snapshots:
        if side is TradeSide.BUY:
            add("SESSION_HIGH", max(float(row.bar.high) for row in prior_snapshots))
        else:
            add("SESSION_LOW", min(float(row.bar.low) for row in prior_snapshots))
    if policy.include_ema_slow:
        add("EMA_SLOW", snapshot.indicators.ema.slow)
    if policy.include_ema_ref:
        add("EMA_REF", snapshot.indicators.ema.ref)

    if not candidates:
        return AdvisorBarrierSummary(False, None, None, None, None, None, threshold, ())

    if side is TradeSide.BUY:
        kind, price = min(candidates, key=lambda item: item[1] - close)
        distance = price - close
    else:
        kind, price = min(candidates, key=lambda item: close - item[1])
        distance = close - price
    active = distance <= threshold
    return AdvisorBarrierSummary(
        active=active,
        barrier_type=kind,
        barrier_price=price,
        distance_points=distance,
        distance_atr=(distance / atr if atr > 0 else None),
        distance_pct=(distance / close * 100.0),
        threshold_points=threshold,
        candidates=tuple(sorted(candidates, key=lambda item: item[1])),
    )


def summarise_episode_history(
    prior: Sequence[StockOpportunitySchema],
    *,
    source_episode_id: str,
    side: TradeSide,
    snapshot: SnapshotSchema,
    policy: RepeatedEpisodePolicyConfig,
) -> AdvisorEpisodeHistorySummary:
    same_episode = [
        row
        for row in prior
        if row.source_episode_id == source_episode_id
        or row.latest_episode_id == source_episode_id
    ]
    same_side = [row for row in same_episode if str(row.side).upper() == side.value]
    opposite = [row for row in same_episode if str(row.side).upper() != side.value]

    facts: List[str] = []
    balance = snapshot.auction.balance
    directional = snapshot.auction.directional
    if balance is None or directional is None:
        raise ValueError(
            "Advisor episode history requires Auction projections"
        )

    if balance.episode_id == source_episode_id:
        if balance.escape_attempt_count >= policy.balance_min_escape_attempts:
            facts.append("MULTIPLE_BALANCE_ESCAPE_ATTEMPTS")
        if balance.failed_escape_count >= policy.balance_min_failed_escapes:
            facts.append("FAILED_BALANCE_ESCAPE")
        if balance.rearm_required:
            facts.append("BALANCE_REARM_REQUIRED")
        if balance.attempt_limit_reached:
            facts.append("BALANCE_ATTEMPT_LIMIT_REACHED")

    prior_count_for_rule = len(same_side) if policy.require_same_side else len(same_episode)
    exhausted = bool(
        prior_count_for_rule >= policy.min_prior_deployments_same_episode
        and facts
    )
    return AdvisorEpisodeHistorySummary(
        prior_count_day=len(prior),
        prior_count_same_episode=len(same_episode),
        prior_same_side_same_episode=len(same_side),
        prior_opposite_side_same_episode=len(opposite),
        prior_opportunity_keys=tuple(row.opportunity_key for row in same_episode),
        exhausted_objective_context=exhausted,
        exhaustion_facts=tuple(facts),
    )


def _matching_fresh_events(
    snapshot: SnapshotSchema,
    *,
    side: TradeSide,
    accepted: Iterable[str],
) -> Tuple[str, ...]:
    accepted_set = {str(item).strip().upper() for item in accepted}
    direction = DirectionalBias.UP if side is TradeSide.BUY else DirectionalBias.DOWN
    found = []
    for event in snapshot.auction.events:
        if event.event_type.value in accepted_set and event.direction is direction:
            found.append(event.event_type.value)
    return tuple(found)


def _pullback_resumption(
    rows: Sequence[SnapshotSchema],
    *,
    side: TradeSide,
    atr: float,
    policy: DeferredEntryFreshnessPolicyConfig,
) -> Tuple[bool, Optional[float], bool, Optional[float]]:
    if len(rows) < 4:
        return False, None, False, None
    before_current = list(rows[:-1])
    current = rows[-1]
    closes = [float(row.close) for row in before_current]

    if side is TradeSide.SELL:
        extreme = min(closes)
        extreme_index = max(index for index, value in enumerate(closes) if value == extreme)
        after_extreme = closes[extreme_index:]
        pullback_extreme = max(after_extreme)
        pullback_points = pullback_extreme - extreme
        pullback = pullback_points >= atr * policy.pullback_min_atr
        resumption_points = pullback_extreme - float(current.close)
        recent_floor = min(closes[-2:])
        resumption = bool(
            pullback
            and resumption_points >= atr * policy.resumption_min_atr
            and float(current.close) < recent_floor
            and float(current.bar.close) < float(current.bar.open)
        )
    else:
        extreme = max(closes)
        extreme_index = max(index for index, value in enumerate(closes) if value == extreme)
        after_extreme = closes[extreme_index:]
        pullback_extreme = min(after_extreme)
        pullback_points = extreme - pullback_extreme
        pullback = pullback_points >= atr * policy.pullback_min_atr
        resumption_points = float(current.close) - pullback_extreme
        recent_ceiling = max(closes[-2:])
        resumption = bool(
            pullback
            and resumption_points >= atr * policy.resumption_min_atr
            and float(current.close) > recent_ceiling
            and float(current.bar.close) > float(current.bar.open)
        )
    return (
        pullback,
        pullback_points / atr if atr > 0 else None,
        resumption,
        resumption_points / atr if atr > 0 else None,
    )


def _consolidation_break(
    rows: Sequence[SnapshotSchema],
    *,
    side: TradeSide,
    atr: float,
    policy: DeferredEntryFreshnessPolicyConfig,
) -> Tuple[bool, Optional[float], bool]:
    required = policy.consolidation_bars
    if len(rows) < required + 1:
        return False, None, False
    base = rows[-(required + 1):-1]
    current = rows[-1]
    high = max(float(row.bar.high) for row in base)
    low = min(float(row.bar.low) for row in base)
    range_atr = (high - low) / atr if atr > 0 else None
    consolidated = bool(range_atr is not None and range_atr <= policy.consolidation_max_atr)
    buffer_points = atr * policy.breakout_buffer_atr
    if side is TradeSide.BUY:
        broken = consolidated and float(current.close) > high + buffer_points
    else:
        broken = consolidated and float(current.close) < low - buffer_points
    return consolidated, range_atr, bool(broken)


def evaluate_deferred_entry_freshness(
    *,
    snapshots: Sequence[SnapshotSchema],
    signal_created_time: datetime,
    side: TradeSide,
    policy: DeferredEntryFreshnessPolicyConfig,
) -> DeferredEntryFreshnessSummary:
    if not snapshots:
        raise ValueError("Deferred freshness requires causal snapshots")
    rows = list(snapshots)[-policy.history_bars:]
    current = rows[-1]
    created = _normalise_time(signal_created_time)
    current_time = _normalise_time(current.snapshot_time)
    if current_time < created:
        raise ValueError("Deferred freshness current snapshot precedes signal")
    age_minutes = (current_time - created).total_seconds() / 60.0
    if age_minutes < policy.min_age_minutes:
        return DeferredEntryFreshnessSummary(
            applicable=False,
            fresh=True,
            reason="ENTRY_WITHIN_INITIAL_MATURATION_WINDOW",
            age_minutes=round(age_minutes, 3),
            matching_fresh_events=(),
            pullback_detected=False,
            pullback_atr=None,
            resumption_detected=False,
            resumption_atr=None,
            consolidation_detected=False,
            consolidation_range_atr=None,
            consolidation_break_detected=False,
            bars_considered=len(rows),
        )

    matching = _matching_fresh_events(
        current,
        side=side,
        accepted=policy.accepted_fresh_event_types,
    )
    atr = float(current.indicators.atr.value)
    pullback, pullback_atr, resumption, resumption_atr = _pullback_resumption(
        rows,
        side=side,
        atr=atr,
        policy=policy,
    )
    consolidated, consolidation_atr, consolidation_break = _consolidation_break(
        rows,
        side=side,
        atr=atr,
        policy=policy,
    )

    fresh = bool(matching or resumption or consolidation_break)
    if matching:
        reason = "FRESH_AUTHORITATIVE_ENTRY_EVENT"
    elif resumption:
        reason = "PULLBACK_RESUMPTION_CONFIRMED"
    elif consolidation_break:
        reason = "CONSOLIDATION_BREAK_CONFIRMED"
    else:
        reason = "UNCLEAR_DELAYED_ENTRY_FRESHNESS"
    return DeferredEntryFreshnessSummary(
        applicable=True,
        fresh=fresh,
        reason=reason,
        age_minutes=round(age_minutes, 3),
        matching_fresh_events=matching,
        pullback_detected=pullback,
        pullback_atr=(round(pullback_atr, 6) if pullback_atr is not None else None),
        resumption_detected=resumption,
        resumption_atr=(round(resumption_atr, 6) if resumption_atr is not None else None),
        consolidation_detected=consolidated,
        consolidation_range_atr=(
            round(consolidation_atr, 6) if consolidation_atr is not None else None
        ),
        consolidation_break_detected=consolidation_break,
        bars_considered=len(rows),
    )


__all__ = [
    "AdvisorDayPathSummary",
    "AdvisorBarrierSummary",
    "AdvisorEpisodeHistorySummary",
    "DeferredEntryFreshnessSummary",
    "summarise_day_path",
    "is_mature_narrow_range_churn",
    "summarise_barrier",
    "summarise_episode_history",
    "evaluate_deferred_entry_freshness",
]
