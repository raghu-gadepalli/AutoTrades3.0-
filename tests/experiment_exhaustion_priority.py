"""Pure state helpers for the replay-only exhaustion-priority experiment."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Tuple

from configs.auction_experiment_config import AuctionPolicyExperimentConfig


@dataclass
class ExhaustionEpisode:
    episode_id: str
    symbol: str
    exhausted_side: str
    reversal_side: str
    initiation_time: datetime
    initiation_close: float
    initiation_atr: float
    initiation_vwap: float
    initial_extreme: float
    extreme_price: float
    extreme_time: datetime
    gap_pct: float
    large_gap: bool
    initial_vwap_room_points: float
    initial_vwap_room_atr: float
    initial_vwap_room_pct: float
    initial_room_qualified: bool
    expires_at: datetime
    confirmation_time: Optional[datetime] = None
    confirmation_price: Optional[float] = None
    confirmation_reason: Optional[str] = None
    confirmation_displacement_atr: Optional[float] = None
    reversal_failed_time: Optional[datetime] = None
    continuation_unlocked_time: Optional[datetime] = None
    failure_acceptance_closes: int = 0
    completed_time: Optional[datetime] = None
    baseline_first_same_side_signal_time: Optional[datetime] = None
    baseline_first_same_side_signal_setup: Optional[str] = None
    baseline_first_same_side_signal_price: Optional[float] = None
    baseline_delay_minutes: Optional[float] = None
    mfe_points: Optional[float] = None
    mae_points: Optional[float] = None
    mfe_atr: Optional[float] = None
    mae_atr: Optional[float] = None
    mfe_pct: Optional[float] = None
    mae_pct: Optional[float] = None
    horizon_outcomes: Dict[str, Optional[float]] = field(default_factory=dict)


def range_locked(snapshot: object, config: AuctionPolicyExperimentConfig) -> bool:
    if not config.range_abstention.enabled:
        return False
    auction = snapshot.auction
    context = auction.stock_context
    if context is None:
        raise ValueError("auction.stock_context is required")
    if context.accepted_range_id is None:
        return False
    if not context.accepted_range_inside:
        return False
    if (
        config.range_abstention.require_breakout_eligible_range
        and not context.accepted_range_breakout_eligible
    ):
        return False
    if (
        config.range_abstention.require_non_provisional_range
        and context.accepted_range_provisional
    ):
        return False
    return True


def room_from_initiation(
    *,
    exhausted_side: str,
    extreme: float,
    vwap: float,
    atr: float,
) -> Tuple[float, float, float]:
    if exhausted_side == "UP":
        points = extreme - vwap
    elif exhausted_side == "DOWN":
        points = vwap - extreme
    else:
        raise ValueError(f"Unsupported exhausted side: {exhausted_side}")
    points = max(0.0, float(points))
    room_atr = points / atr
    room_pct = points / extreme
    return points, room_atr, room_pct


def confirmation(
    *,
    episode: ExhaustionEpisode,
    previous: object,
    current: object,
    config: AuctionPolicyExperimentConfig,
) -> Tuple[bool, str, float]:
    atr = float(current.indicators.atr.value)
    threshold = (
        config.exhaustion.large_gap_displacement_atr
        if episode.large_gap
        else config.exhaustion.normal_displacement_atr
    )
    tolerance = config.exhaustion.extreme_tolerance_atr * atr

    if episode.exhausted_side == "UP":
        displacement_atr = (episode.extreme_price - current.close) / atr
        bearish_body_atr = max(0.0, (current.bar.open - current.bar.close) / atr)
        broke_previous_low = current.close < previous.bar.low
        failed_fresh_high = current.bar.high <= episode.extreme_price + tolerance
        lower_high_break = (
            current.bar.high < previous.bar.high
            and current.close < previous.bar.low
        )
        strong_counter_bar = (
            bearish_body_atr >= config.exhaustion.strong_counter_bar_body_atr
            and current.close < previous.close
        )
        confirmed = (
            displacement_atr >= threshold
            and failed_fresh_high
            and (broke_previous_low or lower_high_break or strong_counter_bar)
        )
        if lower_high_break:
            reason = "LOWER_HIGH_AND_PREVIOUS_LOW_BREAK"
        elif broke_previous_low:
            reason = "PREVIOUS_LOW_BREAK_AFTER_EXHAUSTION"
        else:
            reason = "STRONG_BEARISH_DISPLACEMENT_AFTER_EXHAUSTION"
        return confirmed, reason, displacement_atr

    if episode.exhausted_side == "DOWN":
        displacement_atr = (current.close - episode.extreme_price) / atr
        bullish_body_atr = max(0.0, (current.bar.close - current.bar.open) / atr)
        broke_previous_high = current.close > previous.bar.high
        failed_fresh_low = current.bar.low >= episode.extreme_price - tolerance
        higher_low_break = (
            current.bar.low > previous.bar.low
            and current.close > previous.bar.high
        )
        strong_counter_bar = (
            bullish_body_atr >= config.exhaustion.strong_counter_bar_body_atr
            and current.close > previous.close
        )
        confirmed = (
            displacement_atr >= threshold
            and failed_fresh_low
            and (broke_previous_high or higher_low_break or strong_counter_bar)
        )
        if higher_low_break:
            reason = "HIGHER_LOW_AND_PREVIOUS_HIGH_BREAK"
        elif broke_previous_high:
            reason = "PREVIOUS_HIGH_BREAK_AFTER_EXHAUSTION"
        else:
            reason = "STRONG_BULLISH_DISPLACEMENT_AFTER_EXHAUSTION"
        return confirmed, reason, displacement_atr

    raise ValueError(f"Unsupported exhausted side: {episode.exhausted_side}")


def update_failure(
    episode: ExhaustionEpisode,
    snapshot: object,
    config: AuctionPolicyExperimentConfig,
) -> None:
    if episode.confirmation_time is None:
        return
    if episode.continuation_unlocked_time is not None:
        return

    atr = float(snapshot.indicators.atr.value)
    invalidation = config.exhaustion.reversal_invalidation_atr * atr
    acceptance = config.exhaustion.continuation_acceptance_atr * atr

    if episode.reversal_side == "SELL":
        invalidated = snapshot.bar.high > episode.extreme_price + invalidation
        accepted_beyond = snapshot.close > episode.extreme_price + acceptance
    elif episode.reversal_side == "BUY":
        invalidated = snapshot.bar.low < episode.extreme_price - invalidation
        accepted_beyond = snapshot.close < episode.extreme_price - acceptance
    else:
        raise ValueError(f"Unsupported reversal side: {episode.reversal_side}")

    if invalidated and episode.reversal_failed_time is None:
        episode.reversal_failed_time = snapshot.snapshot_time.replace(tzinfo=None)

    if accepted_beyond:
        episode.failure_acceptance_closes += 1
    else:
        episode.failure_acceptance_closes = 0

    if (
        episode.reversal_failed_time is not None
        and episode.failure_acceptance_closes
        >= config.exhaustion.reversal_failure_closes
    ):
        episode.continuation_unlocked_time = snapshot.snapshot_time.replace(
            tzinfo=None
        )


__all__ = [
    "ExhaustionEpisode",
    "range_locked",
    "room_from_initiation",
    "confirmation",
    "update_failure",
]
