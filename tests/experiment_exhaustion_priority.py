"""Pure state helpers for the replay-only exhaustion-priority experiment."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Sequence, Tuple

from configs.auction_experiment_config import AuctionPolicyExperimentConfig


class MissingExhaustionMemoryError(ValueError):
    """Raised when active exhaustion has no Auction state memory."""


class MissingExhaustionReasonCodesError(ValueError):
    """Raised when active exhaustion has no strict memory reason-code list."""


def auction_exhaustion_reason_codes(snapshot: object) -> Tuple[str, ...]:
    """Return strict exhaustion reasons from private Auction state memory.

    Public ``stock_context.reason_codes`` describe the projected stock context and
    are intentionally not used for maturity qualification.
    """

    state_memory = snapshot.memory.auction.state_memory
    if state_memory is None:
        raise MissingExhaustionMemoryError(
            "memory.auction.state_memory is required when exhaustion is active"
        )
    if "exhaustion_reason_codes" not in state_memory:
        raise MissingExhaustionReasonCodesError(
            "memory.auction.state_memory['exhaustion_reason_codes'] is required "
            "when exhaustion is active"
        )
    raw = state_memory["exhaustion_reason_codes"]
    if not isinstance(raw, list):
        raise ValueError(
            "memory.auction.state_memory['exhaustion_reason_codes'] must be a list"
        )

    normalized = []
    for value in raw:
        if not isinstance(value, str):
            raise ValueError(
                "Every exhaustion reason code must be a non-empty string"
            )
        text = value.strip().upper()
        if not text:
            raise ValueError(
                "Every exhaustion reason code must be a non-empty string"
            )
        normalized.append(text)
    return tuple(normalized)


@dataclass
class ExhaustionEpisode:
    episode_id: str
    symbol: str
    sequence_number: int
    exhausted_side: str
    reversal_side: str
    initiation_time: datetime
    auction_exhaustion_started_at: datetime
    initiation_close: float
    initiation_atr: float
    initiation_vwap: float
    initiation_reason_codes: Tuple[str, ...]
    stock_context_reason_codes: Tuple[str, ...]
    auction_exhaustion_reason_codes: Tuple[str, ...]
    maturity_reason_source: str
    maturity_qualified: bool
    initial_extreme: float
    extreme_price: float
    extreme_time: datetime
    extreme_bar_index: int
    gap_pct: float
    large_gap: bool
    initial_vwap_room_points: float
    initial_vwap_room_atr: float
    initial_vwap_room_pct: float
    initial_room_qualified: bool
    expires_at: datetime
    context_inactive_bars: int = 0
    confirmation_time: Optional[datetime] = None
    confirmation_price: Optional[float] = None
    confirmation_reason: Optional[str] = None
    confirmation_displacement_atr: Optional[float] = None
    failure_candidate_time: Optional[datetime] = None
    reversal_failed_time: Optional[datetime] = None
    continuation_unlocked_time: Optional[datetime] = None
    failure_acceptance_closes: int = 0
    completed_time: Optional[datetime] = None
    completion_reason: Optional[str] = None
    baseline_signal_id: Optional[str] = None
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

    @property
    def fully_qualified(self) -> bool:
        return self.maturity_qualified and self.initial_room_qualified

    @property
    def unresolved(self) -> bool:
        return (
            self.completed_time is None
            and self.reversal_failed_time is None
        )


def range_locked(snapshot: object, config: AuctionPolicyExperimentConfig) -> bool:
    if not config.range_abstention.enabled:
        return False
    context = snapshot.auction.stock_context
    if context is None:
        raise ValueError("auction.stock_context is required")
    if context.accepted_range_id is None:
        return False
    if (
        config.range_abstention.require_non_provisional_range
        and context.accepted_range_provisional
    ):
        return False
    if (
        config.range_abstention.require_breakout_eligible_range
        and not context.accepted_range_breakout_eligible
    ):
        return False
    if not context.accepted_range_inside:
        return False
    if context.accepted_range_established_at is None:
        raise ValueError(
            "accepted_range_established_at is required for a confirmed, "
            "breakout-eligible accepted range"
        )
    snapshot_time = snapshot.snapshot_time.replace(tzinfo=None)
    established_at = context.accepted_range_established_at.replace(tzinfo=None)
    range_age_minutes = (snapshot_time - established_at).total_seconds() / 60.0
    return range_age_minutes >= config.range_abstention.minimum_range_age_minutes


def range_blocks_setup(
    *,
    setup: str,
    locked: bool,
    config: AuctionPolicyExperimentConfig,
) -> bool:
    if not locked:
        return False
    normalized = setup.strip().upper()
    return normalized not in config.range_abstention.allowed_range_resolution_setups


def maturity_qualified(
    *,
    reason_codes: Sequence[str],
    config: AuctionPolicyExperimentConfig,
) -> bool:
    normalized = {str(value).strip().upper() for value in reason_codes}
    matches = sum(
        1
        for required in config.exhaustion.required_initiation_reason_codes
        if required in normalized
    )
    return matches >= config.exhaustion.minimum_required_reason_matches


def room_from_initiation(
    *,
    exhausted_side: str,
    extreme: float,
    vwap: float,
    atr: float,
) -> Tuple[float, float, float]:
    if atr <= 0:
        raise ValueError("ATR must be positive")
    if extreme <= 0 or vwap <= 0:
        raise ValueError("Extreme and VWAP must be positive")
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
    prior_bars: Sequence[object],
    current: object,
    current_bar_index: int,
    config: AuctionPolicyExperimentConfig,
) -> Tuple[bool, str, float]:
    if not prior_bars:
        raise ValueError("At least one prior bar is required for confirmation")
    if current_bar_index <= episode.extreme_bar_index:
        return False, "WAIT_FOR_LATER_COMPLETED_BAR", 0.0
    bars_after_extreme = current_bar_index - episode.extreme_bar_index
    if bars_after_extreme < config.exhaustion.minimum_confirmation_bars_after_extreme:
        return False, "WAIT_FOR_MINIMUM_BARS_AFTER_EXTREME", 0.0
    if (
        config.exhaustion.require_initial_vwap_room_for_confirmation
        and not episode.initial_room_qualified
    ):
        return False, "INITIAL_VWAP_ROOM_NOT_QUALIFIED", 0.0
    if not episode.maturity_qualified:
        return False, "MATURITY_NOT_QUALIFIED", 0.0

    atr = float(current.indicators.atr.value)
    if atr <= 0:
        raise ValueError("Current ATR must be positive")
    threshold = (
        config.exhaustion.large_gap_displacement_atr
        if episode.large_gap
        else config.exhaustion.normal_displacement_atr
    )
    tolerance = config.exhaustion.extreme_tolerance_atr * atr
    local_low = min(float(bar.bar.low) for bar in prior_bars)
    local_high = max(float(bar.bar.high) for bar in prior_bars)
    previous = prior_bars[-1]

    if episode.exhausted_side == "UP":
        displacement_atr = (episode.extreme_price - float(current.close)) / atr
        bearish_body_atr = max(
            0.0,
            (float(current.bar.open) - float(current.bar.close)) / atr,
        )
        failed_fresh_high = (
            float(current.bar.high) <= episode.extreme_price + tolerance
        )
        local_structure_break = float(current.close) < local_low
        lower_high = float(current.bar.high) < float(previous.bar.high)
        strong_counter_bar = bool(
            bearish_body_atr >= config.exhaustion.strong_counter_bar_body_atr
            and float(current.close) < float(previous.close)
            and float(current.close) < local_low
        )
        confirmed = bool(
            displacement_atr >= threshold
            and failed_fresh_high
            and (local_structure_break or (lower_high and strong_counter_bar))
        )
        if local_structure_break and lower_high:
            reason = "LOWER_HIGH_AND_LOCAL_SUPPORT_BREAK"
        elif local_structure_break:
            reason = "LOCAL_SUPPORT_BREAK_AFTER_EXHAUSTION"
        else:
            reason = "STRONG_BEARISH_DISPLACEMENT_AFTER_EXHAUSTION"
        return confirmed, reason, displacement_atr

    if episode.exhausted_side == "DOWN":
        displacement_atr = (float(current.close) - episode.extreme_price) / atr
        bullish_body_atr = max(
            0.0,
            (float(current.bar.close) - float(current.bar.open)) / atr,
        )
        failed_fresh_low = (
            float(current.bar.low) >= episode.extreme_price - tolerance
        )
        local_structure_break = float(current.close) > local_high
        higher_low = float(current.bar.low) > float(previous.bar.low)
        strong_counter_bar = bool(
            bullish_body_atr >= config.exhaustion.strong_counter_bar_body_atr
            and float(current.close) > float(previous.close)
            and float(current.close) > local_high
        )
        confirmed = bool(
            displacement_atr >= threshold
            and failed_fresh_low
            and (local_structure_break or (higher_low and strong_counter_bar))
        )
        if local_structure_break and higher_low:
            reason = "HIGHER_LOW_AND_LOCAL_RESISTANCE_BREAK"
        elif local_structure_break:
            reason = "LOCAL_RESISTANCE_BREAK_AFTER_EXHAUSTION"
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
    if atr <= 0:
        raise ValueError("Current ATR must be positive")
    invalidation = config.exhaustion.reversal_invalidation_atr * atr
    acceptance = config.exhaustion.continuation_acceptance_atr * atr
    snapshot_time = snapshot.snapshot_time.replace(tzinfo=None)

    if episode.reversal_side == "SELL":
        invalidated = float(snapshot.bar.high) > episode.extreme_price + invalidation
        accepted_beyond = float(snapshot.close) > episode.extreme_price + acceptance
    elif episode.reversal_side == "BUY":
        invalidated = float(snapshot.bar.low) < episode.extreme_price - invalidation
        accepted_beyond = float(snapshot.close) < episode.extreme_price - acceptance
    else:
        raise ValueError(f"Unsupported reversal side: {episode.reversal_side}")

    if invalidated and episode.failure_candidate_time is None:
        episode.failure_candidate_time = snapshot_time

    if invalidated and accepted_beyond:
        episode.failure_acceptance_closes += 1
    else:
        episode.failure_acceptance_closes = 0

    if (
        episode.failure_acceptance_closes
        >= config.exhaustion.reversal_failure_closes
    ):
        episode.reversal_failed_time = snapshot_time
        episode.continuation_unlocked_time = snapshot_time
        episode.completed_time = snapshot_time
        episode.completion_reason = "REVERSAL_FAILED_WITH_ACCEPTANCE"


__all__ = [
    "ExhaustionEpisode",
    "range_locked",
    "range_blocks_setup",
    "maturity_qualified",
    "room_from_initiation",
    "confirmation",
    "update_failure",
]
