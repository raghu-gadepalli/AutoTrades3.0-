"""Persistent causal Auction State Engine for Phase-2.5 reporting.

Phase 2.5 retains the episode-oriented
auction lifecycle.  It remains report-only and in-memory: no setup candidates,
signals, processed flags, or external lifecycle rows are written.

Key guarantees
--------------
* trend observations become an established thesis only after a confirmed state
  transition into fresh expansion, orderly trend or reversal;
* established trend context survives ordinary pauses and neutral candles, but
  can be neutralised after prolonged balance/chaos;
* compression, pullback, recompression, failure and reversal are confirmed over
  multiple observations;
* fresh expansion and reacceleration are anchored events with minimum dwell;
* trend-failure WATCH evidence is distinct from confirmed structural failure;
* trend failure cannot exist without a prior established trend and either a
  consecutive breach of a frozen structural protection level or corroborated
  local structure weakening near that level;
* directional-leg anchors measure progress but never serve as structural failure
  levels;
* reversal cannot exist without a confirmed trend-failure episode;
* current-leg maturity requires current extension and near-extreme location,
  not merely a stale historical maximum excursion;
* cumulative daily flip counters remain diagnostics only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG, AuctionEngineConfig
from services.auction_engine.contracts import (
    AuctionState,
    AuctionStateName,
    BoundarySide,
    ConfidenceChannel,
    DirectionalBias,
    EvidenceFact,
    EvidencePolarity,
    EvidenceSnapshot,
    QualityStatus,
    StockContext,
    stable_key,
)


class AuctionStateChronologyError(ValueError):
    """Raised when snapshots for a symbol are not evaluated chronologically."""


@dataclass
class StateEvaluation:
    """Report-only result accompanying the typed state contract."""

    state: AuctionState
    stock_context: StockContext
    proposed_state: AuctionStateName
    transitioned: bool
    flags: Dict[str, Any] = field(default_factory=dict)
    diagnostics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class _StateMemory:
    trading_day: date
    current_state: AuctionStateName = AuctionStateName.UNKNOWN
    entered_at: Optional[datetime] = None
    transition_time: Optional[datetime] = None
    last_snapshot_time: Optional[datetime] = None
    observation_count: int = 0
    state_age_bars: int = 0

    # Complete causal day-so-far path.  These values are updated from every
    # completed candle and reset whenever a new session high/low is printed.
    session_open_price: Optional[float] = None
    session_high_price: Optional[float] = None
    session_high_time: Optional[datetime] = None
    session_low_price: Optional[float] = None
    session_low_time: Optional[datetime] = None
    session_previous_close: Optional[float] = None
    path_from_low_points: float = 0.0
    path_from_low_steps: int = 0
    path_from_low_up_steps: int = 0
    path_from_high_points: float = 0.0
    path_from_high_steps: int = 0
    path_from_high_down_steps: int = 0

    pending_state: Optional[AuctionStateName] = None
    pending_bars: int = 0
    pending_required_bars: int = 0
    transition_policy_reason: str = ""

    # Persistent trend thesis.
    trend_candidate_side: DirectionalBias = DirectionalBias.UNKNOWN
    trend_candidate_bars: int = 0
    trend_neutral_candidate_bars: int = 0
    established_trend_side: DirectionalBias = DirectionalBias.UNKNOWN
    trend_onset_time: Optional[datetime] = None
    trend_anchor_price: Optional[float] = None
    trend_extreme_price: Optional[float] = None

    # Persistent structural protection for the established trend.  This is
    # distinct from the current directional-leg anchor, which measures progress
    # and is never authoritative proof of trend failure.
    trend_protection_level: Optional[float] = None
    trend_protection_side: DirectionalBias = DirectionalBias.UNKNOWN
    trend_protection_source: str = ""
    trend_protection_time: Optional[datetime] = None
    trend_protection_version: int = 0
    trend_protection_episode_key: Optional[str] = None

    # Current directional leg.  Reanchored at fresh expansion, reacceleration
    # and confirmed reversal.
    leg_anchor_time: Optional[datetime] = None
    leg_anchor_price: Optional[float] = None
    leg_extreme_price: Optional[float] = None
    leg_age_bars: int = 0
    leg_no_progress_bars: int = 0
    last_progress_time: Optional[datetime] = None
    leg_maturity_consumed: bool = False
    leg_maturity_onset_time: Optional[datetime] = None
    leg_maturity_extreme_price: Optional[float] = None

    # Compression episode candidate and frozen box.
    compression_candidate_onset: Optional[datetime] = None
    compression_candidate_bars: int = 0
    compression_candidate_low: Optional[float] = None
    compression_candidate_high: Optional[float] = None
    compression_clear_bars: int = 0
    compression_episode_key: Optional[str] = None
    compression_onset_time: Optional[datetime] = None
    compression_box_low: Optional[float] = None
    compression_box_high: Optional[float] = None

    # Trend pause episodes.
    pullback_candidate_bars: int = 0
    pullback_episode_key: Optional[str] = None
    pullback_onset_time: Optional[datetime] = None
    pullback_age_bars: int = 0
    pullback_extreme_price: Optional[float] = None
    pullback_depth_atr: float = 0.0

    recompression_episode_key: Optional[str] = None
    recompression_onset_time: Optional[datetime] = None
    recompression_age_bars: int = 0

    reacceleration_episode_key: Optional[str] = None
    reacceleration_onset_time: Optional[datetime] = None
    reacceleration_age_bars: int = 0

    # Failure/reversal watch lifecycle.
    failure_episode_key: Optional[str] = None
    failure_watch_onset: Optional[datetime] = None
    failure_watch_bars: int = 0
    failure_side: DirectionalBias = DirectionalBias.UNKNOWN
    failure_level: Optional[float] = None
    failure_level_source: str = ""
    failure_level_time: Optional[datetime] = None
    failure_level_version: int = 0
    failure_level_episode_key: Optional[str] = None
    failure_atr: Optional[float] = None
    failure_level_breach_bars: int = 0
    failure_structure_loss_bars: int = 0
    local_structure_weakening_bars: int = 0
    failure_close_distance_beyond_level_atr: float = 0.0
    structure_loss_distance_to_protection_atr: Optional[float] = None
    structure_loss_near_protection: bool = False
    structure_loss_directional_corroboration: bool = False
    structure_loss_value_migration_corroboration: bool = False
    structure_loss_confirmation_blockers: Tuple[str, ...] = ()
    failure_confirmation_reason: str = ""
    last_failure_confirmation_reason: str = ""
    last_failure_confirmation_time: Optional[datetime] = None
    failure_watch_reason_codes: Tuple[str, ...] = ()
    failure_watch_reset_reason: str = ""
    failure_watch_expired: bool = False
    last_failure_terminal_key: Optional[str] = None
    last_failure_terminal_reason: str = ""
    last_failure_terminal_time: Optional[datetime] = None
    # Frozen source structure retained after the active failure watch is cleared.
    # The setup engine consumes this causal handoff on a confirmed REVERSAL.
    last_failure_watch_onset: Optional[datetime] = None
    last_failure_side: DirectionalBias = DirectionalBias.UNKNOWN
    last_failure_original_trend_side: DirectionalBias = DirectionalBias.UNKNOWN
    last_failure_level: Optional[float] = None
    last_failure_level_source: str = ""
    last_failure_level_time: Optional[datetime] = None
    last_failure_level_version: int = 0
    last_failure_level_episode_key: Optional[str] = None
    last_failure_atr: Optional[float] = None
    last_failure_structure_low: Optional[float] = None
    last_failure_structure_high: Optional[float] = None
    last_failure_trend_anchor_price: Optional[float] = None
    last_failure_trend_extreme_price: Optional[float] = None
    trend_failure_age_bars: int = 0
    trend_failure_expired: bool = False
    trend_recovery_bars: int = 0
    reversal_confirmation_bars: int = 0
    reversal_side: DirectionalBias = DirectionalBias.UNKNOWN
    reversal_onset_time: Optional[datetime] = None

    # Persistent exhaustion context is independent of whether the opposite
    # EXHAUSTION_REVERSAL has enough VWAP room to be tradable.
    exhaustion_active: bool = False
    exhaustion_side: DirectionalBias = DirectionalBias.UNKNOWN
    exhaustion_started_at: Optional[datetime] = None
    exhaustion_last_seen_at: Optional[datetime] = None
    exhaustion_age_bars: int = 0
    exhaustion_clear_bars: int = 0
    exhaustion_reason_codes: Tuple[str, ...] = ()

    # Cross-indicator non-directional balance must persist before it can be
    # used as deployment context. A single quiet candle cannot classify the
    # stock as balanced.
    stock_context_balance_bars: int = 0

    chaos_candidate_bars: int = 0

    hma_direction_history: list[DirectionalBias] = field(default_factory=list)
    vwap_direction_history: list[DirectionalBias] = field(default_factory=list)
    structure_direction_history: list[DirectionalBias] = field(default_factory=list)
    bar_direction_history: list[DirectionalBias] = field(default_factory=list)

    last_evidence_hash: str = ""
    last_evaluation: Optional[StateEvaluation] = None


class AuctionStateEngine:
    """Classify causal evidence into a persistent local auction state."""

    _BALANCE_STATES = {
        AuctionStateName.UNKNOWN,
        AuctionStateName.BALANCE,
        AuctionStateName.COMPRESSION,
        AuctionStateName.BOUNDARY_INTERACTION,
        AuctionStateName.CHAOTIC_ROTATION,
    }
    _TREND_STATES = {
        AuctionStateName.FRESH_EXPANSION,
        AuctionStateName.ORDERLY_UPTREND,
        AuctionStateName.ORDERLY_DOWNTREND,
        AuctionStateName.CONTROLLED_PULLBACK,
        AuctionStateName.RECOMPRESSION,
        AuctionStateName.REACCELERATION,
        AuctionStateName.MATURE_EXTENSION,
        AuctionStateName.TREND_FAILURE,
        AuctionStateName.REVERSAL,
    }
    _PAUSE_STATES = {
        AuctionStateName.CONTROLLED_PULLBACK,
        AuctionStateName.RECOMPRESSION,
    }

    def __init__(self, config: AuctionEngineConfig = AUCTION_ENGINE_CONFIG) -> None:
        self.config = config
        self.cfg = config.state
        self.evidence_cfg = config.evidence
        self.version = config.engine.config_version
        self._memory: Dict[str, _StateMemory] = {}

    def reset(self, symbol: Optional[str] = None) -> None:
        if symbol is None:
            self._memory.clear()
        else:
            self._memory.pop(str(symbol).strip().upper(), None)

    def evaluate(self, evidence: EvidenceSnapshot) -> StateEvaluation:
        symbol = evidence.symbol
        memory = self._memory[symbol] if symbol in self._memory else None
        if memory is None or memory.trading_day != evidence.trading_day:
            memory = _StateMemory(trading_day=evidence.trading_day)
            self._memory[symbol] = memory

        evidence_hash = evidence.stable_hash()
        if memory.last_snapshot_time is not None:
            if evidence.snapshot_time < memory.last_snapshot_time:
                if self.cfg.strict_chronology:
                    raise AuctionStateChronologyError(
                        f"Out-of-order evidence for {symbol}: {evidence.snapshot_time} "
                        f"< {memory.last_snapshot_time}"
                    )
            elif evidence.snapshot_time == memory.last_snapshot_time:
                if memory.last_evidence_hash == evidence_hash and memory.last_evaluation is not None:
                    return memory.last_evaluation
                if self.cfg.strict_chronology:
                    raise AuctionStateChronologyError(
                        f"Conflicting duplicate evidence for {symbol} @ {evidence.snapshot_time}"
                    )

        previous_state = memory.current_state
        flags = self._condition_flags(evidence, memory)
        proposed, proposal_reasons = self._propose_state(evidence, memory, flags)
        selected, transitioned, policy_reasons = self._apply_transition_policy(
            memory, proposed, flags
        )

        if memory.entered_at is None:
            memory.entered_at = evidence.snapshot_time
            memory.transition_time = evidence.snapshot_time
        if transitioned:
            memory.entered_at = evidence.snapshot_time
            memory.transition_time = evidence.snapshot_time
            memory.state_age_bars = 1
            self._on_transition(memory, evidence, previous_state, selected, flags)
        else:
            memory.state_age_bars = max(1, memory.state_age_bars + 1)
            self._advance_active_episode_ages(memory, selected)

        # A trend thesis is neutralised only after a sustained neutral auction
        # has been confirmed.  This is deliberately separate from one-candle
        # trend observations and from the transition classifier itself.
        self._apply_post_selection_context(memory, evidence, selected, flags)

        memory.current_state = selected
        memory.observation_count += 1
        self._update_session_path_memory(memory, evidence)
        memory.last_snapshot_time = evidence.snapshot_time
        self._update_rotation_memory(memory, evidence)

        # Recompute memory-derived diagnostics after a transition (for example a
        # new leg anchor set by REACCELERATION).
        self._attach_memory_flags(flags, memory, evidence)
        stock_context = self._build_stock_context(
            evidence,
            memory,
            selected_state=selected,
            flags=flags,
        )

        supporting, contradicting = self._state_facts(evidence, selected, flags)
        channels = self._confidence_channels(evidence, flags)
        reason_codes = list(proposal_reasons) + list(policy_reasons)
        reason_codes.append(f"STATE_{selected.value}")
        if transitioned:
            reason_codes.append(f"TRANSITION_{previous_state.value}_TO_{selected.value}")
        elif proposed != selected:
            reason_codes.append(f"PENDING_{proposed.value}")
        else:
            reason_codes.append("STATE_HELD")

        state = AuctionState(
            state_key=stable_key(
                "auction-state",
                symbol,
                evidence.trading_day,
                selected,
                memory.entered_at,
            ),
            symbol=symbol,
            snapshot_time=evidence.snapshot_time,
            previous_state=previous_state,
            current_state=selected,
            transition_time=memory.transition_time or evidence.snapshot_time,
            entered_at=memory.entered_at or evidence.snapshot_time,
            expires_at=evidence.snapshot_time + timedelta(minutes=self.cfg.state_expiry_minutes),
            supporting_evidence=supporting,
            contradicting_evidence=contradicting,
            confidence_channels=channels,
            reason_codes=_unique(reason_codes),
            config_version=self.version,
        )

        diagnostics = {
            "observation_count": memory.observation_count,
            "state_age_bars": memory.state_age_bars,
            "pending_state": memory.pending_state.value if memory.pending_state else None,
            "pending_bars": memory.pending_bars,
            "pending_required_bars": memory.pending_required_bars,
            "transition_policy_reason": memory.transition_policy_reason,
            "trend_candidate_side": memory.trend_candidate_side.value,
            "trend_candidate_bars": memory.trend_candidate_bars,
            "trend_candidate_ready": bool(
                memory.trend_candidate_side in (DirectionalBias.UP, DirectionalBias.DOWN)
                and memory.trend_candidate_bars >= self.cfg.trend_establishment_bars
            ),
            "trend_neutral_candidate_bars": memory.trend_neutral_candidate_bars,
            "established_trend_side": memory.established_trend_side.value,
            "trend_onset_time": _iso(memory.trend_onset_time),
            "trend_anchor_price": memory.trend_anchor_price,
            "trend_extreme_price": memory.trend_extreme_price,
            "trend_protection_level": memory.trend_protection_level,
            "trend_protection_side": memory.trend_protection_side.value,
            "trend_protection_source": memory.trend_protection_source,
            "trend_protection_time": _iso(memory.trend_protection_time),
            "trend_protection_version": memory.trend_protection_version,
            "trend_protection_episode_key": memory.trend_protection_episode_key,
            "leg_anchor_time": _iso(memory.leg_anchor_time),
            "leg_anchor_price": memory.leg_anchor_price,
            "leg_extreme_price": memory.leg_extreme_price,
            "leg_age_bars": memory.leg_age_bars,
            "leg_no_progress_bars": memory.leg_no_progress_bars,
            "leg_maturity_consumed": memory.leg_maturity_consumed,
            "leg_maturity_onset_time": _iso(memory.leg_maturity_onset_time),
            "leg_maturity_extreme_price": memory.leg_maturity_extreme_price,
            "compression_episode_key": memory.compression_episode_key,
            "compression_onset_time": _iso(memory.compression_onset_time),
            "compression_candidate_bars": memory.compression_candidate_bars,
            "compression_box_low": memory.compression_box_low,
            "compression_box_high": memory.compression_box_high,
            "pullback_episode_key": memory.pullback_episode_key,
            "pullback_onset_time": _iso(memory.pullback_onset_time),
            "pullback_age_bars": memory.pullback_age_bars,
            "pullback_depth_atr": memory.pullback_depth_atr,
            "recompression_episode_key": memory.recompression_episode_key,
            "recompression_onset_time": _iso(memory.recompression_onset_time),
            "recompression_age_bars": memory.recompression_age_bars,
            "reacceleration_episode_key": memory.reacceleration_episode_key,
            "reacceleration_onset_time": _iso(memory.reacceleration_onset_time),
            "reacceleration_age_bars": memory.reacceleration_age_bars,
            "failure_episode_key": memory.failure_episode_key,
            "failure_watch_onset": _iso(memory.failure_watch_onset),
            "failure_watch_bars": memory.failure_watch_bars,
            "failure_side": memory.failure_side.value,
            "failure_level": memory.failure_level,
            "failure_level_source": memory.failure_level_source,
            "failure_level_time": _iso(memory.failure_level_time),
            "failure_level_version": memory.failure_level_version,
            "failure_level_episode_key": memory.failure_level_episode_key,
            "failure_level_breach_bars": memory.failure_level_breach_bars,
            "failure_structure_loss_bars": memory.failure_structure_loss_bars,
            "local_structure_weakening_bars": memory.local_structure_weakening_bars,
            "failure_close_distance_beyond_level_atr": memory.failure_close_distance_beyond_level_atr,
            "structure_loss_distance_to_protection_atr": memory.structure_loss_distance_to_protection_atr,
            "structure_loss_near_protection": memory.structure_loss_near_protection,
            "structure_loss_directional_corroboration": memory.structure_loss_directional_corroboration,
            "structure_loss_value_migration_corroboration": memory.structure_loss_value_migration_corroboration,
            "structure_loss_confirmation_blockers": list(memory.structure_loss_confirmation_blockers),
            "failure_confirmation_reason": memory.failure_confirmation_reason,
            "last_failure_confirmation_reason": memory.last_failure_confirmation_reason,
            "last_failure_confirmation_time": _iso(memory.last_failure_confirmation_time),
            "failure_watch_reason_codes": list(memory.failure_watch_reason_codes),
            "failure_watch_reset_reason": memory.failure_watch_reset_reason,
            "failure_watch_expired": memory.failure_watch_expired,
            "last_failure_terminal_key": memory.last_failure_terminal_key,
            "last_failure_terminal_reason": memory.last_failure_terminal_reason,
            "last_failure_terminal_time": _iso(memory.last_failure_terminal_time),
            "last_failure_watch_onset": _iso(memory.last_failure_watch_onset),
            "last_failure_side": memory.last_failure_side.value,
            "last_failure_original_trend_side": memory.last_failure_original_trend_side.value,
            "last_failure_level": memory.last_failure_level,
            "last_failure_level_source": memory.last_failure_level_source,
            "last_failure_level_time": _iso(memory.last_failure_level_time),
            "last_failure_level_version": memory.last_failure_level_version,
            "last_failure_level_episode_key": memory.last_failure_level_episode_key,
            "last_failure_atr": memory.last_failure_atr,
            "last_failure_structure_low": memory.last_failure_structure_low,
            "last_failure_structure_high": memory.last_failure_structure_high,
            "last_failure_trend_anchor_price": memory.last_failure_trend_anchor_price,
            "last_failure_trend_extreme_price": memory.last_failure_trend_extreme_price,
            "trend_failure_age_bars": memory.trend_failure_age_bars,
            "trend_failure_expired": memory.trend_failure_expired,
            "trend_recovery_bars": memory.trend_recovery_bars,
            "reversal_confirmation_bars": memory.reversal_confirmation_bars,
            "reversal_side": memory.reversal_side.value,
            "reversal_onset_time": _iso(memory.reversal_onset_time),
            "stock_context": stock_context.to_storage_dict(exclude_none=False),
            "exhaustion_context_active": memory.exhaustion_active,
            "exhausted_side": memory.exhaustion_side.value,
            "exhaustion_started_at": _iso(memory.exhaustion_started_at),
            "exhaustion_last_seen_at": _iso(memory.exhaustion_last_seen_at),
            "exhaustion_age_bars": memory.exhaustion_age_bars,
            "exhaustion_clear_bars": memory.exhaustion_clear_bars,
            "exhaustion_reason_codes": list(memory.exhaustion_reason_codes),
            "data_quality": evidence.data_quality.status.value,
            "proposal_reasons": list(proposal_reasons),
            "policy_reasons": list(policy_reasons),
        }

        evaluation = StateEvaluation(
            state=state,
            stock_context=stock_context,
            proposed_state=proposed,
            transitioned=transitioned,
            flags=flags,
            diagnostics=diagnostics,
        )
        memory.last_evidence_hash = evidence_hash
        memory.last_evaluation = evaluation
        return evaluation

    # ------------------------------------------------------------------
    # Evidence interpretation and persistent observations
    # ------------------------------------------------------------------
    def _condition_flags(self, evidence: EvidenceSnapshot, memory: _StateMemory) -> Dict[str, Any]:
        bar = evidence.bar
        trend = evidence.trend
        compression = evidence.compression
        boundary = evidence.boundary

        move_atr = _required_number(bar.move_atr, "bar.move_atr")
        body = _required_number(bar.body_fraction, "bar.body_fraction")
        close_position = _required_number(bar.close_position, "bar.close_position")
        directional_edge = self.evidence_cfg.directional_close_position
        strong_body = body >= self.evidence_cfg.strong_bar_body_fraction
        strong_up = bool(
            move_atr >= self.evidence_cfg.strong_bar_move_atr
            and strong_body
            and close_position >= directional_edge
        )
        strong_down = bool(
            move_atr <= -self.evidence_cfg.strong_bar_move_atr
            and strong_body
            and close_position <= (1.0 - directional_edge)
        )

        efficiency = trend.directional_efficiency
        overlap = evidence.price_action.overlap_ratio
        trend_support_up = self._trend_support_count(evidence, DirectionalBias.UP)
        trend_support_down = self._trend_support_count(evidence, DirectionalBias.DOWN)
        trend_up = bool(
            trend.direction == DirectionalBias.UP
            and (efficiency is None or efficiency >= self.cfg.orderly_trend_efficiency_min)
            and trend_support_up >= 2
        )
        trend_down = bool(
            trend.direction == DirectionalBias.DOWN
            and (efficiency is None or efficiency >= self.cfg.orderly_trend_efficiency_min)
            and trend_support_down >= 2
        )

        observed_trend_side = DirectionalBias.UNKNOWN
        if trend_up and not trend_down:
            observed_trend_side = DirectionalBias.UP
        elif trend_down and not trend_up:
            observed_trend_side = DirectionalBias.DOWN
        self._update_trend_candidate(memory, observed_trend_side, evidence)

        boundary_near = False
        boundary_outside = False
        outside_direction = DirectionalBias.UNKNOWN
        if boundary is not None and boundary.current_offset_atr is not None:
            boundary_near = abs(boundary.current_offset_atr) <= self.cfg.boundary_interaction_distance_atr
            boundary_outside = boundary.current_offset_atr >= self.cfg.fresh_expansion_outside_atr
            if boundary_outside:
                outside_direction = (
                    DirectionalBias.UP
                    if boundary.boundary_side == BoundarySide.UPPER
                    else DirectionalBias.DOWN
                )

        low_efficiency = efficiency is not None and efficiency <= self.cfg.balance_efficiency_max
        high_overlap = overlap is not None and overlap >= self.cfg.balance_overlap_min
        balance = bool(low_efficiency and high_overlap)

        raw_states = _required_raw_section(evidence, "source_states")
        raw_structure = _required_raw_section(evidence, "source_structure")
        cumulative_day_flip_count = max(
            _strict_int(raw_states["hma_flip_count"], "source_states.hma_flip_count"),
            _strict_int(raw_states["vwap_flip_count"], "source_states.vwap_flip_count"),
            _strict_int(raw_structure["structure_flip_count"], "source_structure.structure_flip_count"),
        )

        current_hma_direction = _direction_from_text(trend.hma_order)
        current_vwap_direction = _direction_from_text(trend.vwap_side)
        current_structure_direction = _direction_from_text(raw_structure["raw_side"])
        current_bar_direction = bar.direction
        local_flip_counts = {
            "hma": _rolling_flip_count(memory.hma_direction_history, current_hma_direction, self.cfg.history_bars),
            "vwap": _rolling_flip_count(memory.vwap_direction_history, current_vwap_direction, self.cfg.history_bars),
            "structure": _rolling_flip_count(memory.structure_direction_history, current_structure_direction, self.cfg.history_bars),
            "bar": _rolling_flip_count(memory.bar_direction_history, current_bar_direction, self.cfg.history_bars),
        }
        directional_channel_flips = {k: v for k, v in local_flip_counts.items() if k != "bar"}
        independent_flip_channels = sum(
            count >= self.cfg.chaotic_flip_count_min
            for count in directional_channel_flips.values()
        )
        flip_count = max(directional_channel_flips.values(), default=0)
        bar_flip_count = local_flip_counts["bar"]
        local_rotation = bool(
            independent_flip_channels >= self.cfg.chaotic_independent_channels_min
            and bar_flip_count >= self.cfg.chaotic_bar_direction_flips_min
        )
        if self.cfg.use_cumulative_day_flip_counts_for_state:
            local_rotation = local_rotation or cumulative_day_flip_count >= self.cfg.chaotic_flip_count_min
        chaos_observed = bool(
            local_rotation
            and (
                efficiency is None
                or efficiency <= self.cfg.chaotic_efficiency_max
                or trend.direction in (DirectionalBias.MIXED, DirectionalBias.UNKNOWN)
            )
        )
        memory.chaos_candidate_bars = (
            memory.chaos_candidate_bars + 1 if chaos_observed else 0
        )
        chaos_ready = memory.chaos_candidate_bars >= self.cfg.chaos_confirmation_bars

        compression_observed = bool(
            compression.compressed
            and not strong_up
            and not strong_down
            and abs(move_atr) <= self.evidence_cfg.compression_max_bar_move_atr
        )
        self._update_compression_candidate(memory, evidence, compression_observed)
        compression_ready = memory.compression_candidate_bars >= self.cfg.compression_confirmation_bars

        established = memory.established_trend_side
        adverse_to_trend = False
        trend_resume = False
        opposite_displacement = False
        structure_loss = False
        opposite_support = 0
        aligned_support = 0
        if established == DirectionalBias.UP:
            structure_loss = bool(
                current_structure_direction == DirectionalBias.DOWN
                or (
                    trend.retained_structure is False
                    and trend.direction == DirectionalBias.UP
                )
            )
            adverse_to_trend = bool(
                bar.direction == DirectionalBias.DOWN
                and abs(move_atr) <= self.cfg.controlled_pullback_max_adverse_atr
                and not structure_loss
            )
            trend_resume = bool(
                (strong_up or move_atr >= self.cfg.reacceleration_displacement_atr)
                and trend.direction != DirectionalBias.DOWN
                and trend_support_up >= 2
            )
            opposite_support = trend_support_down
            aligned_support = trend_support_up
            opposite_displacement = bool(
                move_atr <= -self.cfg.trend_failure_opposite_displacement_atr
            )
        elif established == DirectionalBias.DOWN:
            structure_loss = bool(
                current_structure_direction == DirectionalBias.UP
                or (
                    trend.retained_structure is False
                    and trend.direction == DirectionalBias.DOWN
                )
            )
            adverse_to_trend = bool(
                bar.direction == DirectionalBias.UP
                and abs(move_atr) <= self.cfg.controlled_pullback_max_adverse_atr
                and not structure_loss
            )
            trend_resume = bool(
                (strong_down or move_atr <= -self.cfg.reacceleration_displacement_atr)
                and trend.direction != DirectionalBias.UP
                and trend_support_down >= 2
            )
            opposite_support = trend_support_up
            aligned_support = trend_support_down
            opposite_displacement = bool(
                move_atr >= self.cfg.trend_failure_opposite_displacement_atr
            )

        pullback_observed = bool(
            established in (DirectionalBias.UP, DirectionalBias.DOWN)
            and adverse_to_trend
        )
        memory.pullback_candidate_bars = (
            memory.pullback_candidate_bars + 1 if pullback_observed else 0
        )
        pullback_ready = memory.pullback_candidate_bars >= self.cfg.pullback_confirmation_bars

        # Contextual opposition starts a failure WATCH.  It never proves trend
        # failure by itself.  Confirmation requires either a consecutive current
        # breach of the frozen structural protection level or corroborated local
        # structure weakening near that immutable level.
        protection_level_breached = self._trend_protection_level_breached(memory, evidence)
        structure_loss_observed = bool(
            established in (DirectionalBias.UP, DirectionalBias.DOWN)
            and structure_loss
            and trend.retained_structure is False
        )
        opposite_context_observed = bool(
            established in (DirectionalBias.UP, DirectionalBias.DOWN)
            and evidence.bar.direction == _opposite(established)
            and opposite_support >= 3
            and abs(move_atr) >= self.cfg.reacceleration_displacement_atr
        )
        failure_watch_reason_codes = []
        if opposite_displacement:
            failure_watch_reason_codes.append("OPPOSITE_DISPLACEMENT")
        if structure_loss_observed:
            failure_watch_reason_codes.append("LOCAL_STRUCTURE_WEAKENING")
        if opposite_context_observed:
            failure_watch_reason_codes.append("OPPOSITE_CONTEXT_ALIGNMENT")
        if protection_level_breached:
            failure_watch_reason_codes.append("PROTECTED_LEVEL_BREACH")
        failure_watch_observed = bool(
            established in (DirectionalBias.UP, DirectionalBias.DOWN)
            and failure_watch_reason_codes
        )
        if memory.current_state != AuctionStateName.TREND_FAILURE:
            failure_level_breached = self._update_failure_watch(
                memory,
                evidence,
                failure_watch_observed,
                structure_loss_observed=structure_loss_observed,
                watch_reason_codes=tuple(failure_watch_reason_codes),
            )
        else:
            failure_level_breached = self._failure_level_breached(memory, evidence)

        # A frozen protected-level breach is the authoritative confirmation
        # path.  Retained-structure loss is local weakening evidence and can
        # confirm only when it remains consecutive, price is near the frozen
        # protection level, and adverse directional evidence is current.
        signed_level_distance = self._failure_level_distance_atr(memory, evidence)
        structure_distance = (
            abs(signed_level_distance)
            if memory.failure_level is not None and evidence.atr is not None
            else None
        )
        near_protection = bool(
            structure_distance is not None
            and structure_distance <= self.cfg.failure_structure_proximity_atr
        )
        adverse_side = _opposite(established)
        directional_corroboration = bool(
            adverse_side in (DirectionalBias.UP, DirectionalBias.DOWN)
            and (
                trend.direction == adverse_side
                or observed_trend_side == adverse_side
                or (
                    bar.direction == adverse_side
                    and opposite_support >= 2
                )
            )
        )
        value_migration_corroboration = bool(
            adverse_side in (DirectionalBias.UP, DirectionalBias.DOWN)
            and (
                trend.value_migration == adverse_side
                or (adverse_side == DirectionalBias.UP and "ABOVE" in trend.vwap_side)
                or (adverse_side == DirectionalBias.DOWN and "BELOW" in trend.vwap_side)
            )
        )
        structure_blockers = []
        if memory.failure_episode_key is not None or structure_loss_observed:
            if memory.failure_structure_loss_bars < self.cfg.failure_structure_confirmation_bars:
                structure_blockers.append("INSUFFICIENT_CONSECUTIVE_LOCAL_STRUCTURE_WEAKENING")
            if memory.failure_level is None:
                structure_blockers.append("NO_FROZEN_TREND_PROTECTION_LEVEL")
            elif not near_protection:
                structure_blockers.append("PRICE_NOT_NEAR_FROZEN_TREND_PROTECTION")
            if (
                self.cfg.failure_structure_requires_directional_corroboration
                and not directional_corroboration
            ):
                structure_blockers.append("NO_CURRENT_ADVERSE_DIRECTIONAL_CORROBORATION")
            if (
                self.cfg.failure_structure_requires_value_migration_corroboration
                and not value_migration_corroboration
            ):
                structure_blockers.append("NO_CURRENT_ADVERSE_VALUE_MIGRATION")

        structure_loss_confirmed = bool(
            memory.failure_structure_loss_bars >= self.cfg.failure_structure_confirmation_bars
            and near_protection
            and (
                directional_corroboration
                or not self.cfg.failure_structure_requires_directional_corroboration
            )
            and (
                value_migration_corroboration
                or not self.cfg.failure_structure_requires_value_migration_corroboration
            )
        )
        level_breach_confirmed = bool(
            memory.failure_level_breach_bars >= self.cfg.failure_level_confirmation_bars
        )
        structural_failure_confirmed = bool(
            memory.failure_watch_bars >= self.cfg.trend_failure_confirmation_bars
            and (level_breach_confirmed or structure_loss_confirmed)
        )

        memory.local_structure_weakening_bars = memory.failure_structure_loss_bars
        memory.structure_loss_distance_to_protection_atr = structure_distance
        memory.structure_loss_near_protection = near_protection
        memory.structure_loss_directional_corroboration = directional_corroboration
        memory.structure_loss_value_migration_corroboration = value_migration_corroboration
        memory.structure_loss_confirmation_blockers = tuple(structure_blockers)

        # Active confirmation reflects the current snapshot only.  Historical
        # entry confirmation is retained separately for diagnostics.
        memory.failure_confirmation_reason = ""
        if structural_failure_confirmed:
            if level_breach_confirmed:
                reason = "FROZEN_PROTECTED_LEVEL_BREACH_CONFIRMED"
            else:
                reason = "CORROBORATED_LOCAL_STRUCTURE_WEAKENING_CONFIRMED"
            memory.failure_confirmation_reason = reason
            memory.last_failure_confirmation_reason = reason
            memory.last_failure_confirmation_time = evidence.snapshot_time
        trend_failure_ready = structural_failure_confirmed

        # Expiry is evaluated after both confirmation paths, because raw local
        # weakening streak length alone is not sufficient confirmation.
        if (
            memory.current_state != AuctionStateName.TREND_FAILURE
            and memory.failure_episode_key is not None
            and memory.failure_watch_bars >= self.cfg.failure_watch_max_bars
            and not structural_failure_confirmed
        ):
            self._terminate_failure_episode(
                memory,
                evidence.snapshot_time,
                "FAILURE_WATCH_EXPIRED_WITHOUT_CORROBORATED_STRUCTURAL_CONFIRMATION",
            )
            memory.failure_watch_expired = True
            memory.failure_confirmation_reason = ""
            trend_failure_ready = False

        recovery_observed = bool(
            memory.current_state == AuctionStateName.TREND_FAILURE
            and established in (DirectionalBias.UP, DirectionalBias.DOWN)
            and trend_resume
            and not structure_loss
        )
        memory.trend_recovery_bars = (
            memory.trend_recovery_bars + 1 if recovery_observed else 0
        )
        trend_recovery_ready = memory.trend_recovery_bars >= self.cfg.trend_recovery_confirmation_bars

        reversal_side = _opposite(established)
        reversal_observed = bool(
            memory.current_state == AuctionStateName.TREND_FAILURE
            and reversal_side in (DirectionalBias.UP, DirectionalBias.DOWN)
            and observed_trend_side == reversal_side
            and opposite_support >= 2
            and (
                evidence.price_action.followthrough
                or strong_up
                or strong_down
                or abs(move_atr) >= self.cfg.reacceleration_displacement_atr
            )
        )
        if reversal_observed:
            memory.reversal_confirmation_bars += 1
            memory.reversal_side = reversal_side
        else:
            memory.reversal_confirmation_bars = 0
        reversal_ready = memory.reversal_confirmation_bars >= self.cfg.reversal_confirmation_bars

        fresh_expansion = bool(
            (
                strong_up
                and outside_direction in (DirectionalBias.UP, DirectionalBias.UNKNOWN)
            )
            or (
                strong_down
                and outside_direction in (DirectionalBias.DOWN, DirectionalBias.UNKNOWN)
            )
        ) and bool(
            boundary_outside
            or memory.current_state in {
                AuctionStateName.BALANCE,
                AuctionStateName.COMPRESSION,
                AuctionStateName.BOUNDARY_INTERACTION,
                AuctionStateName.UNKNOWN,
            }
        )
        expansion_direction = (
            DirectionalBias.UP if strong_up else DirectionalBias.DOWN if strong_down else DirectionalBias.UNKNOWN
        )

        self._update_current_leg(memory, evidence)
        (
            leg_distance_atr,
            leg_current_distance_atr,
            leg_retracement_atr,
            leg_retracement_fraction,
        ) = self._leg_distances(memory, evidence)
        leg_progress_or_rejection = bool(
            memory.leg_no_progress_bars >= self.cfg.current_leg_no_progress_bars
            or evidence.price_action.rejection
            or evidence.price_action.failed_extreme
            or (
                evidence.extension.progress_decay is not None
                and evidence.extension.progress_decay >= self.evidence_cfg.extension_progress_decay_min
            )
        )
        current_leg_mature = bool(
            established in (DirectionalBias.UP, DirectionalBias.DOWN)
            and not memory.leg_maturity_consumed
            and memory.leg_age_bars >= self.cfg.current_leg_min_bars_for_maturity
            and leg_distance_atr is not None
            and leg_distance_atr >= self.cfg.current_leg_extension_atr
            and leg_current_distance_atr is not None
            and leg_current_distance_atr >= self.cfg.current_leg_current_extension_atr
            and leg_retracement_atr is not None
            and leg_retracement_atr <= self.cfg.current_leg_max_retracement_atr
            and leg_retracement_fraction is not None
            and leg_retracement_fraction <= self.cfg.current_leg_max_retracement_fraction
            and leg_progress_or_rejection
            and not pullback_ready
            and not structural_failure_confirmed
        )

        self._update_exhaustion_context(
            memory,
            evidence,
            established=established,
            current_leg_mature=current_leg_mature,
            leg_distance_atr=leg_distance_atr,
            leg_progress_or_rejection=leg_progress_or_rejection,
            trend_resume=trend_resume,
            reversal_ready=reversal_ready,
        )

        hma_word = str(trend.hma_order or "UNKNOWN").strip().upper()
        hma_tangled = bool(
            hma_word in {
                "UNKNOWN", "NEUTRAL", "MIXED", "FLAT", "RANGE",
                "BALANCE", "COMPRESSION", "INTERTWINED", "TANGLED",
            }
            or (
                trend.hma_spread_atr is not None
                and trend.hma_spread_atr <= self.evidence_cfg.compression_hma_spread_atr_max
                and trend.direction in {
                    DirectionalBias.NEUTRAL,
                    DirectionalBias.MIXED,
                    DirectionalBias.UNKNOWN,
                }
            )
        )
        slow_ema_non_directional = trend.ema_context in {
            "COMPRESSED_FLAT",
            "COMPRESSED_MIXED",
        }
        atr_contracting = evidence.compression.atr_state == "CONTRACTING"
        atr_expanding = evidence.compression.atr_state == "EXPANDING"
        balanced_observed = bool(
            (balance or compression_ready or chaos_ready)
            and (slow_ema_non_directional or hma_tangled or local_rotation)
            and not fresh_expansion
        )
        memory.stock_context_balance_bars = (
            memory.stock_context_balance_bars + 1 if balanced_observed else 0
        )
        balanced_non_directional = bool(
            memory.stock_context_balance_bars
            >= self.cfg.stock_context_balance_confirmation_bars
        )
        fresh_expansion_confirmed = bool(
            fresh_expansion
            and abs(move_atr)
            >= self.cfg.stock_context_early_expansion_displacement_atr
            and efficiency is not None
            and efficiency >= self.cfg.stock_context_directional_efficiency_min
            and (
                boundary_outside
                or atr_expanding
                or trend.ema_context in {"EARLY_DIVERGENCE_UP", "EARLY_DIVERGENCE_DOWN"}
                or not hma_tangled
            )
        )
        rotational_context = bool(
            chaos_ready
            or (balance and local_rotation)
            or (balance and hma_tangled and not slow_ema_non_directional and not atr_contracting)
        )

        neutral_context_observed = bool(
            established in (DirectionalBias.UP, DirectionalBias.DOWN)
            and not strong_up
            and not strong_down
            and not trend_resume
            and aligned_support <= 1
            and (
                chaos_ready
                or (balance and observed_trend_side not in (established,))
                or (compression_ready and observed_trend_side not in (established,))
            )
        )
        if neutral_context_observed:
            memory.trend_neutral_candidate_bars += 1
        else:
            memory.trend_neutral_candidate_bars = max(
                0, memory.trend_neutral_candidate_bars - 1
            )
        trend_neutralisation_ready = bool(
            memory.trend_neutral_candidate_bars
            >= self.cfg.trend_neutralisation_confirmation_bars
        )

        enough_history = self._enough_history(evidence, memory)
        flags: Dict[str, Any] = {
            "enough_history": enough_history,
            "strong_up": strong_up,
            "strong_down": strong_down,
            "trend_up": trend_up,
            "trend_down": trend_down,
            "observed_trend_side": observed_trend_side.value,
            "trend_candidate_ready": bool(
                memory.trend_candidate_side in (DirectionalBias.UP, DirectionalBias.DOWN)
                and memory.trend_candidate_bars >= self.cfg.trend_establishment_bars
            ),
            "trend_neutral_context_observed": neutral_context_observed,
            "trend_neutralisation_ready": trend_neutralisation_ready,
            "trend_support_up": trend_support_up,
            "trend_support_down": trend_support_down,
            "boundary_near": boundary_near,
            "boundary_outside": boundary_outside,
            "outside_direction": outside_direction.value,
            "balance": balance,
            "balanced_observed": balanced_observed,
            "stock_context_balance_bars": memory.stock_context_balance_bars,
            "balanced_non_directional": balanced_non_directional,
            "rotational_context": rotational_context,
            "fresh_expansion_confirmed": fresh_expansion_confirmed,
            "slow_ema_non_directional": slow_ema_non_directional,
            "ema_context": trend.ema_context,
            "ema_spread_atr": trend.ema_slow_ref_spread_atr,
            "ema_spread_change_atr_per_bar": trend.ema_spread_change_atr_per_bar,
            "hma_tangled": hma_tangled,
            "hma_context": hma_word,
            "hma_spread_atr": trend.hma_spread_atr,
            "atr_state": evidence.compression.atr_state,
            "atr_contraction_ratio": evidence.compression.atr_contraction_ratio,
            "compression_observed": compression_observed,
            "compression_ready": compression_ready,
            "compression": compression_ready,
            "low_efficiency": low_efficiency,
            "high_overlap": high_overlap,
            "stock_day_extension": bool(evidence.extension.extended),
            "stock_day_mature_extension": bool(evidence.extension.mature),
            "current_leg_mature": current_leg_mature,
            "current_leg_distance_atr": leg_distance_atr,
            "current_leg_current_distance_atr": leg_current_distance_atr,
            "current_leg_retracement_atr": leg_retracement_atr,
            "current_leg_retracement_fraction": leg_retracement_fraction,
            "current_leg_progress_or_rejection": leg_progress_or_rejection,
            "exhaustion_context_active": memory.exhaustion_active,
            "exhausted_side": memory.exhaustion_side.value,
            "exhaustion_started_at": _iso(memory.exhaustion_started_at),
            "exhaustion_age_bars": memory.exhaustion_age_bars,
            "exhaustion_reason_codes": list(memory.exhaustion_reason_codes),
            "chaos_observed": chaos_observed,
            "chaos_ready": chaos_ready,
            "chaos": chaos_ready,
            "flip_count": flip_count,
            "local_flip_counts": local_flip_counts,
            "independent_flip_channels": independent_flip_channels,
            "bar_flip_count": bar_flip_count,
            "cumulative_day_flip_count": cumulative_day_flip_count,
            "adverse_to_trend": adverse_to_trend,
            "pullback_observed": pullback_observed,
            "pullback_ready": pullback_ready,
            "trend_resume": trend_resume,
            "opposite_displacement": opposite_displacement,
            "structure_loss": structure_loss,
            "failure_watch_observed": failure_watch_observed,
            "failure_watch_reason_codes": list(memory.failure_watch_reason_codes),
            "failure_level_breached": failure_level_breached,
            "failure_close_distance_beyond_level_atr": memory.failure_close_distance_beyond_level_atr,
            "failure_structure_loss_observed": structure_loss_observed,
            "structural_failure_confirmed": structural_failure_confirmed,
            "failure_confirmation_reason": memory.failure_confirmation_reason,
            "failure_watch_expired": memory.failure_watch_expired,
            "trend_failure_ready": trend_failure_ready,
            "trend_recovery_ready": trend_recovery_ready,
            "reversal_observed": reversal_observed,
            "reversal_ready": reversal_ready,
            "fresh_expansion": fresh_expansion,
            "expansion_direction": expansion_direction.value,
            "retained_structure": trend.retained_structure,
            "move_atr": move_atr,
            "efficiency": efficiency,
            "overlap": overlap,
            "aligned_support": aligned_support,
            "opposite_support": opposite_support,
        }
        self._attach_memory_flags(flags, memory, evidence)
        return flags

    def _update_exhaustion_context(
        self,
        memory: _StateMemory,
        evidence: EvidenceSnapshot,
        *,
        established: DirectionalBias,
        current_leg_mature: bool,
        leg_distance_atr: Optional[float],
        leg_progress_or_rejection: bool,
        trend_resume: bool,
        reversal_ready: bool,
    ) -> None:
        if not self.cfg.exhaustion_context_enabled:
            self._clear_exhaustion_context(memory)
            return

        # Timestamp expiry is authoritative. A stale context must never remain
        # active merely because its age counter was restored or skipped.
        if memory.exhaustion_active and memory.exhaustion_started_at is not None:
            expires_at = memory.exhaustion_started_at + timedelta(
                minutes=(
                    self.cfg.exhaustion_context_max_bars
                    * self.config.engine.snapshot_interval_minutes
                )
            )
            if evidence.snapshot_time > expires_at:
                self._clear_exhaustion_context(memory)

        extension_move = evidence.extension.move_from_anchor_atr
        extension_large = bool(
            extension_move is not None
            and (
                (
                    established is DirectionalBias.UP
                    and float(extension_move) >= self.cfg.exhaustion_context_min_extension_atr
                )
                or (
                    established is DirectionalBias.DOWN
                    and float(extension_move) <= -self.cfg.exhaustion_context_min_extension_atr
                )
            )
        )
        progress_lost = bool(
            evidence.extension.progress_decay is not None
            and evidence.extension.progress_decay
            >= self.cfg.exhaustion_context_progress_decay_min
        )
        rejection = bool(
            evidence.price_action.rejection
            or evidence.price_action.failed_extreme
            or evidence.extension.failed_extreme_count > 0
        )
        exhaustion_observed = bool(
            established in (DirectionalBias.UP, DirectionalBias.DOWN)
            and memory.leg_age_bars >= self.cfg.exhaustion_context_min_leg_age_bars
            and (current_leg_mature or extension_large or evidence.extension.mature is True)
            and (leg_progress_or_rejection or progress_lost or rejection)
        )

        if (
            exhaustion_observed
            and (
                not memory.exhaustion_active
                or memory.exhaustion_side is established
            )
        ):
            if not memory.exhaustion_active:
                memory.exhaustion_active = True
                memory.exhaustion_side = established
                memory.exhaustion_started_at = evidence.snapshot_time
                memory.exhaustion_age_bars = 1
            else:
                memory.exhaustion_age_bars += 1
            memory.exhaustion_last_seen_at = evidence.snapshot_time
            memory.exhaustion_clear_bars = 0
            reasons = []
            if current_leg_mature:
                reasons.append("CURRENT_LEG_MATURE")
            if extension_large:
                reasons.append("PRIOR_MOVE_EXTENDED")
            if progress_lost:
                reasons.append("PROGRESS_DECAY")
            if rejection:
                reasons.append("REJECTION_OR_FAILED_EXTREME")
            memory.exhaustion_reason_codes = tuple(reasons)
            return

        if not memory.exhaustion_active:
            return

        memory.exhaustion_age_bars += 1

        # A confirmed reversal does not erase the contextual fact that the old
        # directional move exhausted.  The opposite candidate can trade only
        # after its own structural confirmation; the context expires naturally
        # or clears if the exhausted trend genuinely resumes.
        clear_observed = bool(
            trend_resume
            and established is memory.exhaustion_side
            and not progress_lost
            and not rejection
        )
        memory.exhaustion_clear_bars = (
            memory.exhaustion_clear_bars + 1 if clear_observed else 0
        )
        if (
            memory.exhaustion_clear_bars
            >= self.cfg.exhaustion_context_clear_confirmation_bars
            or memory.exhaustion_age_bars > self.cfg.exhaustion_context_max_bars
        ):
            self._clear_exhaustion_context(memory)

    @staticmethod
    def _clear_exhaustion_context(memory: _StateMemory) -> None:
        memory.exhaustion_active = False
        memory.exhaustion_side = DirectionalBias.UNKNOWN
        memory.exhaustion_started_at = None
        memory.exhaustion_last_seen_at = None
        memory.exhaustion_age_bars = 0
        memory.exhaustion_clear_bars = 0
        memory.exhaustion_reason_codes = ()

    def _update_session_path_memory(
        self,
        memory: _StateMemory,
        evidence: EvidenceSnapshot,
    ) -> None:
        """Update complete causal paths from the current day high and low."""

        bar = evidence.bar
        close = float(evidence.close)
        high = float(bar.high)
        low = float(bar.low)
        open_price = float(bar.open)
        tolerance = self.config.evidence.floating_point_tolerance
        previous_close = memory.session_previous_close

        if memory.session_open_price is None:
            memory.session_open_price = open_price

        new_low = bool(
            memory.session_low_price is None
            or low < float(memory.session_low_price) - tolerance
        )
        if new_low:
            memory.session_low_price = low
            memory.session_low_time = evidence.snapshot_time
            initial = max(0.0, close - low)
            memory.path_from_low_points = initial
            memory.path_from_low_steps = 1 if initial > tolerance else 0
            memory.path_from_low_up_steps = 1 if initial > tolerance else 0
        elif previous_close is not None:
            delta = close - float(previous_close)
            memory.path_from_low_points += abs(delta)
            memory.path_from_low_steps += 1
            if delta > tolerance:
                memory.path_from_low_up_steps += 1

        new_high = bool(
            memory.session_high_price is None
            or high > float(memory.session_high_price) + tolerance
        )
        if new_high:
            memory.session_high_price = high
            memory.session_high_time = evidence.snapshot_time
            initial = max(0.0, high - close)
            memory.path_from_high_points = initial
            memory.path_from_high_steps = 1 if initial > tolerance else 0
            memory.path_from_high_down_steps = 1 if initial > tolerance else 0
        elif previous_close is not None:
            delta = close - float(previous_close)
            memory.path_from_high_points += abs(delta)
            memory.path_from_high_steps += 1
            if delta < -tolerance:
                memory.path_from_high_down_steps += 1

        memory.session_previous_close = close

    def _build_stock_context(
        self,
        evidence: EvidenceSnapshot,
        memory: _StateMemory,
        *,
        selected_state: AuctionStateName,
        flags: Mapping[str, Any],
    ) -> StockContext:
        """Build objective day-so-far and accepted-range context.

        No BALANCED/ROTATIONAL/COMPRESSION regime is projected.  Those local
        Auction states remain available through ``current_auction_state`` while
        the Advisor consumes only observable geometry and session-path facts.
        """

        source_structure = _required_raw_section(evidence, "source_structure")
        range_id_raw = source_structure["accepted_range_id"]
        range_low = _optional_number(source_structure["accepted_range_low"])
        range_high = _optional_number(source_structure["accepted_range_high"])
        range_source = str(
            source_structure["accepted_range_source"] or "UNKNOWN"
        ).strip().upper()
        range_established_at = _optional_datetime(
            source_structure["accepted_range_established_at"]
        )
        range_provisional = bool(source_structure["accepted_range_provisional"])
        range_breakout_eligible = bool(
            source_structure["accepted_range_breakout_eligible"]
        )
        range_valid = bool(
            range_low is not None
            and range_high is not None
            and range_high > range_low
        )

        atr = _required_evidence_atr(evidence)
        range_position = None
        range_outside_atr = None
        range_inside = False
        if range_valid:
            assert range_low is not None and range_high is not None
            width = range_high - range_low
            range_position = (evidence.close - range_low) / width
            tolerance = atr * self.cfg.accepted_range_tolerance_atr
            range_inside = bool(
                range_low - tolerance <= evidence.close <= range_high + tolerance
            )
            if evidence.close > range_high:
                range_outside_atr = (evidence.close - range_high) / atr
            elif evidence.close < range_low:
                range_outside_atr = (range_low - evidence.close) / atr
            else:
                range_outside_atr = 0.0

        if (
            memory.session_open_price is None
            or memory.session_high_price is None
            or memory.session_high_time is None
            or memory.session_low_price is None
            or memory.session_low_time is None
        ):
            raise ValueError("Session path memory was not initialised")

        session_open = float(memory.session_open_price)
        session_high = float(memory.session_high_price)
        session_low = float(memory.session_low_price)
        session_width = session_high - session_low
        session_position = (
            (evidence.close - session_low) / session_width
            if session_width > self.config.evidence.floating_point_tolerance
            else 0.5
        )
        session_position = max(0.0, min(1.0, session_position))

        rise_points = max(0.0, evidence.close - session_low)
        decline_points = max(0.0, session_high - evidence.close)
        distance_to_high_atr = decline_points / atr
        distance_to_low_atr = rise_points / atr

        low_efficiency = _path_efficiency(
            rise_points,
            memory.path_from_low_points,
            self.config.evidence.floating_point_tolerance,
        )
        high_efficiency = _path_efficiency(
            decline_points,
            memory.path_from_high_points,
            self.config.evidence.floating_point_tolerance,
        )
        low_ratio = _step_ratio(
            memory.path_from_low_up_steps,
            memory.path_from_low_steps,
        )
        high_ratio = _step_ratio(
            memory.path_from_high_down_steps,
            memory.path_from_high_steps,
        )

        reasons = ["STOCK_CONTEXT_OBJECTIVE_SESSION_PATH"]
        if range_valid:
            reasons.append("ACCEPTED_RANGE_PRESENT")
            reasons.append(
                "PRICE_INSIDE_ACCEPTED_RANGE"
                if range_inside
                else "PRICE_OUTSIDE_ACCEPTED_RANGE"
            )
        else:
            reasons.append("ACCEPTED_RANGE_MISSING")
        if memory.exhaustion_active:
            reasons.append("EXHAUSTION_CONTEXT_ACTIVE")

        expires_at = None
        if memory.exhaustion_active and memory.exhaustion_started_at is not None:
            expires_at = memory.exhaustion_started_at + timedelta(
                minutes=(
                    self.cfg.exhaustion_context_max_bars
                    * self.config.engine.snapshot_interval_minutes
                )
            )

        directional_bias = memory.established_trend_side
        if directional_bias not in (DirectionalBias.UP, DirectionalBias.DOWN):
            directional_bias = _direction_from_text(evidence.trend.direction.value)

        return StockContext(
            symbol=evidence.symbol,
            snapshot_time=evidence.snapshot_time,
            current_auction_state=selected_state,
            directional_bias=directional_bias,
            accepted_range_id=(str(range_id_raw) if range_id_raw else None),
            accepted_range_source=range_source,
            accepted_range_low=range_low,
            accepted_range_high=range_high,
            accepted_range_established_at=range_established_at,
            accepted_range_provisional=range_provisional,
            accepted_range_breakout_eligible=range_breakout_eligible,
            accepted_range_inside=range_inside,
            accepted_range_position=range_position,
            accepted_range_outside_atr=range_outside_atr,
            session_open_price=session_open,
            session_high_price=session_high,
            session_high_time=memory.session_high_time,
            session_low_price=session_low,
            session_low_time=memory.session_low_time,
            session_position=session_position,
            distance_to_session_high_atr=distance_to_high_atr,
            distance_to_session_low_atr=distance_to_low_atr,
            rise_from_session_low_atr=rise_points / atr,
            rise_from_session_low_pct=(rise_points / session_low),
            decline_from_session_high_atr=decline_points / atr,
            decline_from_session_high_pct=(decline_points / session_high),
            path_from_session_low_bars=memory.path_from_low_steps,
            path_from_session_low_efficiency=low_efficiency,
            path_from_session_low_directional_ratio=low_ratio,
            path_from_session_high_bars=memory.path_from_high_steps,
            path_from_session_high_efficiency=high_efficiency,
            path_from_session_high_directional_ratio=high_ratio,
            exhaustion_active=memory.exhaustion_active,
            exhausted_side=memory.exhaustion_side,
            exhaustion_started_at=memory.exhaustion_started_at,
            exhaustion_expires_at=expires_at,
            reason_codes=_unique(reasons),
            diagnostics={
                "path_from_session_low_points": memory.path_from_low_points,
                "path_from_session_high_points": memory.path_from_high_points,
                "current_leg_distance_atr": flags["current_leg_distance_atr"],
                "current_leg_current_distance_atr": flags[
                    "current_leg_current_distance_atr"
                ],
            },
            config_version=self.version,
        )

    def _update_trend_candidate(
        self,
        memory: _StateMemory,
        observed_side: DirectionalBias,
        evidence: EvidenceSnapshot,
    ) -> None:
        if observed_side not in (DirectionalBias.UP, DirectionalBias.DOWN):
            memory.trend_candidate_bars = max(0, memory.trend_candidate_bars - 1)
            if memory.trend_candidate_bars == 0:
                memory.trend_candidate_side = DirectionalBias.UNKNOWN
            return

        if memory.trend_candidate_side == observed_side:
            memory.trend_candidate_bars += 1
        else:
            memory.trend_candidate_side = observed_side
            memory.trend_candidate_bars = 1

        # Phase 2.3: this method records only an observational candidate.  The
        # persistent trend thesis is established exclusively by a confirmed
        # state transition in ``_on_transition``.

    def _update_compression_candidate(
        self,
        memory: _StateMemory,
        evidence: EvidenceSnapshot,
        observed: bool,
    ) -> None:
        if observed:
            if memory.compression_candidate_bars == 0:
                memory.compression_candidate_onset = evidence.snapshot_time
                memory.compression_candidate_low = evidence.bar.low
                memory.compression_candidate_high = evidence.bar.high
            else:
                memory.compression_candidate_low = min(
                    memory.compression_candidate_low or evidence.bar.low,
                    evidence.bar.low,
                )
                memory.compression_candidate_high = max(
                    memory.compression_candidate_high or evidence.bar.high,
                    evidence.bar.high,
                )
            memory.compression_candidate_bars += 1
            memory.compression_clear_bars = 0
            if (
                memory.compression_episode_key is None
                and memory.compression_candidate_bars >= self.cfg.compression_confirmation_bars
            ):
                memory.compression_onset_time = memory.compression_candidate_onset
                memory.compression_box_low = memory.compression_candidate_low
                memory.compression_box_high = memory.compression_candidate_high
                memory.compression_episode_key = stable_key(
                    "compression",
                    evidence.symbol,
                    evidence.trading_day,
                    memory.compression_onset_time,
                    memory.compression_box_low,
                    memory.compression_box_high,
                )
            return

        memory.compression_candidate_bars = 0
        memory.compression_candidate_onset = None
        memory.compression_candidate_low = None
        memory.compression_candidate_high = None
        memory.compression_clear_bars += 1
        if memory.compression_clear_bars >= self.cfg.ordinary_transition_confirmation_bars:
            # Keep a frozen box while the state is still explicitly a compression
            # state; otherwise retire it so a later episode receives a new key.
            if memory.current_state not in {
                AuctionStateName.COMPRESSION,
                AuctionStateName.RECOMPRESSION,
                AuctionStateName.BOUNDARY_INTERACTION,
            }:
                self._clear_compression_episode(memory)

    def _update_failure_watch(
        self,
        memory: _StateMemory,
        evidence: EvidenceSnapshot,
        observed: bool,
        *,
        structure_loss_observed: bool,
        watch_reason_codes: Tuple[str, ...],
    ) -> bool:
        """Advance one immutable failure watch and return current level breach.

        Watch age may survive a neutral candle, but structural confirmation
        streaks never do.  Both the level breach and local-structure-weakening
        counters are consecutive/current streaks and reset immediately when the
        corresponding evidence disappears.
        """

        memory.failure_watch_expired = False
        active = memory.failure_episode_key is not None
        if observed and not active:
            self._start_failure_watch(memory, evidence, watch_reason_codes)
            active = True

        if not active:
            memory.failure_watch_reset_reason = ""
            return False

        memory.failure_watch_bars += 1
        level_breached = self._failure_level_breached(memory, evidence)
        memory.failure_close_distance_beyond_level_atr = self._failure_level_distance_atr(
            memory, evidence
        )

        if observed:
            memory.failure_watch_reason_codes = tuple(_unique(watch_reason_codes))
            memory.failure_watch_reset_reason = ""
            memory.failure_level_breach_bars = (
                memory.failure_level_breach_bars + 1 if level_breached else 0
            )
            memory.failure_structure_loss_bars = (
                memory.failure_structure_loss_bars + 1 if structure_loss_observed else 0
            )
            memory.local_structure_weakening_bars = memory.failure_structure_loss_bars
        else:
            # The broad WATCH remains alive briefly, but neither confirmation
            # path may carry stale evidence through a recovered candle.
            memory.failure_level_breach_bars = 0
            memory.failure_structure_loss_bars = 0
            memory.local_structure_weakening_bars = 0
            memory.failure_watch_reason_codes = ()
            memory.failure_watch_reset_reason = "CURRENT_FAILURE_EVIDENCE_CLEARED"

        return level_breached

    def _start_failure_watch(
        self,
        memory: _StateMemory,
        evidence: EvidenceSnapshot,
        watch_reason_codes: Tuple[str, ...],
    ) -> None:
        memory.failure_watch_onset = evidence.snapshot_time
        memory.failure_side = _opposite(memory.established_trend_side)
        memory.failure_level = memory.trend_protection_level
        memory.failure_level_source = memory.trend_protection_source or "UNKNOWN"
        memory.failure_level_time = memory.trend_protection_time
        memory.failure_level_version = memory.trend_protection_version
        memory.failure_level_episode_key = memory.trend_protection_episode_key
        memory.failure_atr = _required_evidence_atr(evidence)
        memory.failure_episode_key = stable_key(
            "trend-failure-watch",
            evidence.symbol,
            evidence.trading_day,
            memory.established_trend_side,
            memory.failure_watch_onset,
            memory.failure_level,
            memory.failure_level_version,
        )
        memory.failure_watch_bars = 0
        memory.failure_level_breach_bars = 0
        memory.failure_structure_loss_bars = 0
        memory.local_structure_weakening_bars = 0
        memory.failure_close_distance_beyond_level_atr = 0.0
        memory.structure_loss_distance_to_protection_atr = None
        memory.structure_loss_near_protection = False
        memory.structure_loss_directional_corroboration = False
        memory.structure_loss_value_migration_corroboration = False
        memory.structure_loss_confirmation_blockers = ()
        memory.failure_confirmation_reason = ""
        memory.failure_watch_reason_codes = tuple(_unique(watch_reason_codes))
        memory.failure_watch_reset_reason = ""

    def _trend_protection_level_breached(
        self,
        memory: _StateMemory,
        evidence: EvidenceSnapshot,
    ) -> bool:
        side = memory.established_trend_side
        level = memory.trend_protection_level
        if side not in (DirectionalBias.UP, DirectionalBias.DOWN) or level is None:
            return False
        tolerance = _required_evidence_atr(evidence) * self.cfg.failure_level_breach_atr
        if side == DirectionalBias.UP:
            return evidence.close < level - tolerance
        return evidence.close > level + tolerance

    def _failure_level_breached(
        self,
        memory: _StateMemory,
        evidence: EvidenceSnapshot,
    ) -> bool:
        side = memory.established_trend_side
        level = memory.failure_level
        if side not in (DirectionalBias.UP, DirectionalBias.DOWN) or level is None:
            return False
        tolerance = _required_evidence_atr(evidence) * self.cfg.failure_level_breach_atr
        if side == DirectionalBias.UP:
            return evidence.close < level - tolerance
        return evidence.close > level + tolerance

    @staticmethod
    def _failure_level_distance_atr(
        memory: _StateMemory,
        evidence: EvidenceSnapshot,
    ) -> float:
        level = memory.failure_level
        atr = _required_evidence_atr(evidence)
        if level is None or atr <= 0.0:
            return 0.0
        if memory.established_trend_side == DirectionalBias.UP:
            return (level - evidence.close) / atr
        if memory.established_trend_side == DirectionalBias.DOWN:
            return (evidence.close - level) / atr
        return 0.0

    def _terminate_failure_episode(
        self,
        memory: _StateMemory,
        timestamp: datetime,
        reason: str,
    ) -> None:
        if memory.failure_episode_key is not None:
            memory.last_failure_terminal_key = memory.failure_episode_key
            memory.last_failure_terminal_reason = reason
            memory.last_failure_terminal_time = timestamp
            memory.last_failure_watch_onset = memory.failure_watch_onset
            memory.last_failure_side = memory.failure_side
            memory.last_failure_original_trend_side = memory.established_trend_side
            memory.last_failure_level = memory.failure_level
            memory.last_failure_level_source = memory.failure_level_source
            memory.last_failure_level_time = memory.failure_level_time
            memory.last_failure_level_version = memory.failure_level_version
            memory.last_failure_level_episode_key = memory.failure_level_episode_key
            memory.last_failure_atr = memory.failure_atr
            memory.last_failure_trend_anchor_price = memory.trend_anchor_price
            memory.last_failure_trend_extreme_price = memory.trend_extreme_price

            level = memory.failure_level
            atr = memory.failure_atr
            extreme = memory.trend_extreme_price
            original = memory.established_trend_side
            if level is not None and original == DirectionalBias.DOWN:
                lower = extreme if extreme is not None and extreme < level else None
                if lower is None and atr is not None and atr > 0:
                    lower = level - atr
                memory.last_failure_structure_low = lower
                memory.last_failure_structure_high = level
            elif level is not None and original == DirectionalBias.UP:
                upper = extreme if extreme is not None and extreme > level else None
                if upper is None and atr is not None and atr > 0:
                    upper = level + atr
                memory.last_failure_structure_low = level
                memory.last_failure_structure_high = upper
        self._clear_failure_watch(memory)

    @staticmethod
    def _clear_failure_watch(memory: _StateMemory) -> None:
        memory.failure_episode_key = None
        memory.failure_watch_onset = None
        memory.failure_watch_bars = 0
        memory.failure_side = DirectionalBias.UNKNOWN
        memory.failure_level = None
        memory.failure_level_source = ""
        memory.failure_level_time = None
        memory.failure_level_version = 0
        memory.failure_level_episode_key = None
        memory.failure_atr = None
        memory.failure_level_breach_bars = 0
        memory.failure_structure_loss_bars = 0
        memory.local_structure_weakening_bars = 0
        memory.failure_close_distance_beyond_level_atr = 0.0
        memory.structure_loss_distance_to_protection_atr = None
        memory.structure_loss_near_protection = False
        memory.structure_loss_directional_corroboration = False
        memory.structure_loss_value_migration_corroboration = False
        memory.structure_loss_confirmation_blockers = ()
        memory.failure_confirmation_reason = ""
        memory.failure_watch_reason_codes = ()
        memory.failure_watch_reset_reason = ""

    def _update_current_leg(self, memory: _StateMemory, evidence: EvidenceSnapshot) -> None:
        side = memory.established_trend_side
        if side not in (DirectionalBias.UP, DirectionalBias.DOWN):
            return
        if memory.leg_anchor_price is None:
            self._reset_leg(memory, evidence, side)
            return
        if memory.leg_anchor_time == evidence.snapshot_time:
            return

        memory.leg_age_bars += 1
        atr = _required_evidence_atr(evidence)
        tolerance = atr * self.cfg.current_leg_progress_tolerance_atr
        if side == DirectionalBias.UP:
            new_extreme = evidence.bar.high
            prior = memory.leg_extreme_price if memory.leg_extreme_price is not None else new_extreme
            if (
                memory.leg_maturity_consumed
                and memory.leg_maturity_extreme_price is not None
                and new_extreme - memory.leg_maturity_extreme_price
                >= atr * self.cfg.current_leg_reanchor_progress_atr
            ):
                self._reset_leg(memory, evidence, side)
                return
            if new_extreme > prior + tolerance:
                memory.leg_extreme_price = new_extreme
                memory.leg_no_progress_bars = 0
                memory.last_progress_time = evidence.snapshot_time
            else:
                memory.leg_no_progress_bars += 1
        else:
            new_extreme = evidence.bar.low
            prior = memory.leg_extreme_price if memory.leg_extreme_price is not None else new_extreme
            if (
                memory.leg_maturity_consumed
                and memory.leg_maturity_extreme_price is not None
                and memory.leg_maturity_extreme_price - new_extreme
                >= atr * self.cfg.current_leg_reanchor_progress_atr
            ):
                self._reset_leg(memory, evidence, side)
                return
            if new_extreme < prior - tolerance:
                memory.leg_extreme_price = new_extreme
                memory.leg_no_progress_bars = 0
                memory.last_progress_time = evidence.snapshot_time
            else:
                memory.leg_no_progress_bars += 1

        if memory.trend_extreme_price is None:
            memory.trend_extreme_price = memory.leg_extreme_price
        elif side == DirectionalBias.UP:
            memory.trend_extreme_price = max(memory.trend_extreme_price, evidence.bar.high)
        else:
            memory.trend_extreme_price = min(memory.trend_extreme_price, evidence.bar.low)

        if memory.current_state == AuctionStateName.CONTROLLED_PULLBACK:
            self._update_pullback_metrics(memory, evidence)

    def _leg_distances(
        self,
        memory: _StateMemory,
        evidence: EvidenceSnapshot,
    ) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
        atr = evidence.atr
        side = memory.established_trend_side
        if not atr or not memory.leg_anchor_price or memory.leg_extreme_price is None:
            return None, None, None, None
        if side == DirectionalBias.UP:
            maximum = max(0.0, (memory.leg_extreme_price - memory.leg_anchor_price) / atr)
            current = (evidence.close - memory.leg_anchor_price) / atr
            retracement = max(0.0, (memory.leg_extreme_price - evidence.close) / atr)
            fraction = retracement / maximum if maximum > 0 else 0.0
            return maximum, current, retracement, fraction
        if side == DirectionalBias.DOWN:
            maximum = max(0.0, (memory.leg_anchor_price - memory.leg_extreme_price) / atr)
            current = (memory.leg_anchor_price - evidence.close) / atr
            retracement = max(0.0, (evidence.close - memory.leg_extreme_price) / atr)
            fraction = retracement / maximum if maximum > 0 else 0.0
            return maximum, current, retracement, fraction
        return None, None, None, None

    # ------------------------------------------------------------------
    # State proposal and transition policy
    # ------------------------------------------------------------------
    def _propose_state(
        self,
        evidence: EvidenceSnapshot,
        memory: _StateMemory,
        flags: Dict[str, Any],
    ) -> Tuple[AuctionStateName, Tuple[str, ...]]:
        current = memory.current_state
        established = memory.established_trend_side

        if not flags["enough_history"]:
            return AuctionStateName.UNKNOWN, ("WAITING_FOR_MINIMUM_HISTORY",)

        if current == AuctionStateName.TREND_FAILURE:
            if flags["reversal_ready"]:
                return AuctionStateName.REVERSAL, ("OPPOSITE_FOLLOWTHROUGH_CONFIRMED_AFTER_TREND_FAILURE",)
            if flags["trend_recovery_ready"]:
                return self._orderly_state(established), ("ORIGINAL_TREND_RECOVERED_AFTER_FAILURE_WATCH",)
            if memory.trend_failure_age_bars >= self.cfg.trend_failure_max_bars:
                memory.trend_failure_expired = True
                if flags["chaos_ready"]:
                    return AuctionStateName.CHAOTIC_ROTATION, ("UNRESOLVED_TREND_FAILURE_EXPIRED_TO_CHAOS",)
                if flags["compression_ready"]:
                    return AuctionStateName.COMPRESSION, ("UNRESOLVED_TREND_FAILURE_EXPIRED_TO_COMPRESSION",)
                if flags["balance"]:
                    return AuctionStateName.BALANCE, ("UNRESOLVED_TREND_FAILURE_EXPIRED_TO_BALANCE",)
                return AuctionStateName.UNKNOWN, ("UNRESOLVED_TREND_FAILURE_EXPIRED_TO_UNKNOWN",)
            return AuctionStateName.TREND_FAILURE, ("TREND_FAILURE_EPISODE_REMAINS_UNRESOLVED",)

        if current == AuctionStateName.REVERSAL:
            # Reversal is an episode, not an alternating one-bar label.  The
            # transition policy holds it for the configured dwell, then it can
            # graduate to the newly established orderly trend.
            if established in (DirectionalBias.UP, DirectionalBias.DOWN):
                return self._orderly_state(established), ("REVERSAL_GRADUATING_TO_NEW_ORDERLY_TREND",)
            return AuctionStateName.REVERSAL, ("REVERSAL_EPISODE_HELD",)

        if flags["trend_failure_ready"]:
            reason = str(flags["failure_confirmation_reason"])
            return AuctionStateName.TREND_FAILURE, (f"TREND_FAILURE_{reason}",)

        if flags["chaos_ready"] and current not in {
            AuctionStateName.FRESH_EXPANSION,
            AuctionStateName.REACCELERATION,
            AuctionStateName.TREND_FAILURE,
            AuctionStateName.REVERSAL,
        }:
            return AuctionStateName.CHAOTIC_ROTATION, ("CONFIRMED_LOCAL_MULTI_CHANNEL_ROTATION",)

        if current in self._PAUSE_STATES and flags["trend_resume"]:
            return AuctionStateName.REACCELERATION, ("FRESH_DISPLACEMENT_AFTER_CONFIRMED_TREND_PAUSE",)

        if flags["trend_neutralisation_ready"]:
            if flags["chaos_ready"]:
                return AuctionStateName.CHAOTIC_ROTATION, ("ESTABLISHED_TREND_NEUTRALISED_BY_PROLONGED_CHAOS",)
            if flags["compression_ready"]:
                return AuctionStateName.COMPRESSION, ("ESTABLISHED_TREND_NEUTRALISED_BY_PROLONGED_COMPRESSION",)
            return AuctionStateName.BALANCE, ("ESTABLISHED_TREND_NEUTRALISED_BY_PROLONGED_BALANCE",)

        if established in (DirectionalBias.UP, DirectionalBias.DOWN):
            if flags["pullback_ready"]:
                return AuctionStateName.CONTROLLED_PULLBACK, ("MULTIBAR_CONTROLLED_ADVERSE_MOVE",)

            if (
                flags["compression_ready"]
                and current in self._TREND_STATES
                and not flags["fresh_expansion"]
            ):
                return AuctionStateName.RECOMPRESSION, ("PERSISTENT_COMPACT_VALUE_INSIDE_ESTABLISHED_TREND",)

            if current == AuctionStateName.CONTROLLED_PULLBACK:
                if memory.pullback_age_bars < self.cfg.pullback_max_bars:
                    return AuctionStateName.CONTROLLED_PULLBACK, ("CONTROLLED_PULLBACK_EPISODE_ACTIVE",)

            if current == AuctionStateName.RECOMPRESSION:
                if memory.recompression_age_bars < self.cfg.recompression_max_bars:
                    return AuctionStateName.RECOMPRESSION, ("RECOMPRESSION_EPISODE_ACTIVE",)

            if flags["current_leg_mature"]:
                return AuctionStateName.MATURE_EXTENSION, ("CURRENT_DIRECTIONAL_LEG_MATURED",)

            # Preserve trend context through ordinary neutral candles.  A trend
            # does not disappear merely because the current bar is small.
            return self._orderly_state(established), ("ESTABLISHED_TREND_CONTEXT_RETAINED",)

        if flags["fresh_expansion"]:
            return AuctionStateName.FRESH_EXPANSION, ("STRONG_DEPARTURE_FROM_BALANCE_OR_BOUNDARY",)

        if flags["trend_up"] and flags["trend_candidate_ready"]:
            return AuctionStateName.ORDERLY_UPTREND, ("DIRECTIONAL_PROGRESS_UP_BUILDING",)
        if flags["trend_down"] and flags["trend_candidate_ready"]:
            return AuctionStateName.ORDERLY_DOWNTREND, ("DIRECTIONAL_PROGRESS_DOWN_BUILDING",)

        if flags["boundary_near"] or flags["boundary_outside"]:
            return AuctionStateName.BOUNDARY_INTERACTION, ("PRICE_INTERACTING_WITH_DYNAMIC_BOUNDARY",)

        if flags["compression_ready"]:
            return AuctionStateName.COMPRESSION, ("PERSISTENT_PRICE_CONTAINMENT_WITH_CONTRACTION",)

        if flags["balance"]:
            return AuctionStateName.BALANCE, ("LOW_EFFICIENCY_OVERLAPPING_AUCTION",)

        if current == AuctionStateName.CHAOTIC_ROTATION and not flags["chaos_ready"]:
            return AuctionStateName.BALANCE, ("ROTATION_STABILISED_TO_BALANCE",)

        return AuctionStateName.UNKNOWN, ("NO_REPRODUCIBLE_STATE_DEFINITION_MATCHED",)

    def _apply_transition_policy(
        self,
        memory: _StateMemory,
        proposed: AuctionStateName,
        flags: Dict[str, Any],
    ) -> Tuple[AuctionStateName, bool, Tuple[str, ...]]:
        current = memory.current_state
        memory.transition_policy_reason = ""

        if proposed == current:
            memory.pending_state = None
            memory.pending_bars = 0
            memory.pending_required_bars = 0
            memory.transition_policy_reason = "PROPOSED_STATE_MATCHES_CURRENT"
            return current, False, ("STATE_PROPOSAL_MATCHED_CURRENT",)

        allowed, reason = self._transition_allowed(memory, current, proposed, flags)
        if not allowed:
            memory.pending_state = None
            memory.pending_bars = 0
            memory.pending_required_bars = 0
            memory.transition_policy_reason = reason
            return current, False, (reason,)

        dwell_required = self._minimum_dwell(current)
        urgent = proposed in {
            AuctionStateName.FRESH_EXPANSION,
            AuctionStateName.REACCELERATION,
            AuctionStateName.TREND_FAILURE,
            AuctionStateName.REVERSAL,
        }
        if memory.observation_count > 0 and memory.state_age_bars < dwell_required and not urgent:
            memory.transition_policy_reason = f"MINIMUM_DWELL_{current.value}_{dwell_required}_BARS"
            return current, False, (memory.transition_policy_reason,)

        required = self._confirmation_bars(memory, current, proposed)
        if memory.pending_state == proposed:
            memory.pending_bars += 1
        else:
            memory.pending_state = proposed
            memory.pending_bars = 1
        memory.pending_required_bars = required

        if memory.pending_bars < required:
            memory.transition_policy_reason = f"AWAITING_{required}_BAR_CONFIRMATION_FOR_{proposed.value}"
            return current, False, (memory.transition_policy_reason,)

        memory.pending_state = None
        memory.pending_bars = 0
        memory.pending_required_bars = 0
        memory.transition_policy_reason = "TRANSITION_CONFIRMED"
        return proposed, True, ("TRANSITION_CONFIRMATION_SATISFIED",)

    def _transition_allowed(
        self,
        memory: _StateMemory,
        current: AuctionStateName,
        proposed: AuctionStateName,
        flags: Dict[str, Any],
    ) -> Tuple[bool, str]:
        established = memory.established_trend_side

        if proposed == AuctionStateName.TREND_FAILURE:
            if established not in (DirectionalBias.UP, DirectionalBias.DOWN):
                return False, "TREND_FAILURE_BLOCKED_WITHOUT_ESTABLISHED_TREND"
            if not flags["trend_failure_ready"]:
                return False, "TREND_FAILURE_BLOCKED_WITHOUT_MULTIBAR_CONFIRMATION"

        if proposed == AuctionStateName.REVERSAL:
            if current != AuctionStateName.TREND_FAILURE:
                return False, "REVERSAL_BLOCKED_WITHOUT_ACTIVE_TREND_FAILURE"
            if not flags["reversal_ready"]:
                return False, "REVERSAL_BLOCKED_WITHOUT_OPPOSITE_FOLLOWTHROUGH"

        if proposed == AuctionStateName.REACCELERATION and current not in self._PAUSE_STATES:
            return False, "REACCELERATION_BLOCKED_WITHOUT_PULLBACK_OR_RECOMPRESSION"

        if proposed in {
            AuctionStateName.CONTROLLED_PULLBACK,
            AuctionStateName.RECOMPRESSION,
            AuctionStateName.MATURE_EXTENSION,
        } and established not in (DirectionalBias.UP, DirectionalBias.DOWN):
            return False, f"{proposed.value}_BLOCKED_WITHOUT_ESTABLISHED_TREND"

        if proposed == AuctionStateName.ORDERLY_UPTREND and established == DirectionalBias.DOWN:
            return False, "OPPOSITE_ORDERLY_TREND_BLOCKED_BEFORE_CONFIRMED_REVERSAL"
        if proposed == AuctionStateName.ORDERLY_DOWNTREND and established == DirectionalBias.UP:
            return False, "OPPOSITE_ORDERLY_TREND_BLOCKED_BEFORE_CONFIRMED_REVERSAL"

        if proposed == AuctionStateName.FRESH_EXPANSION and current not in self._BALANCE_STATES:
            return False, "FRESH_EXPANSION_BLOCKED_OUTSIDE_BALANCE_BOUNDARY_CONTEXT"

        return True, "TRANSITION_ALLOWED"

    def _confirmation_bars(
        self,
        memory: _StateMemory,
        current: AuctionStateName,
        proposed: AuctionStateName,
    ) -> int:
        if proposed in {AuctionStateName.TREND_FAILURE, AuctionStateName.REVERSAL}:
            return 1  # Their watch counters already provide multi-bar confirmation.
        if proposed in {AuctionStateName.FRESH_EXPANSION, AuctionStateName.REACCELERATION}:
            return 1  # Price-action event; minimum dwell provides persistence.
        if proposed == AuctionStateName.COMPRESSION:
            return 1  # Compression candidate was already confirmed and frozen.
        if proposed == AuctionStateName.RECOMPRESSION:
            return 1  # Same as compression, within established trend context.
        if proposed == AuctionStateName.CHAOTIC_ROTATION:
            return 1  # Local rotation counter already confirmed it.
        if proposed in {AuctionStateName.ORDERLY_UPTREND, AuctionStateName.ORDERLY_DOWNTREND}:
            # The observational trend-candidate counter already provides the
            # multi-bar evidence.  A single confirmed transition step is then
            # sufficient to establish the persistent thesis.
            return 1
        if current == AuctionStateName.UNKNOWN:
            return self.cfg.initial_state_confirmation_bars
        if proposed == AuctionStateName.CONTROLLED_PULLBACK:
            return 1  # pullback candidate counter already confirmed it.
        return self.cfg.ordinary_transition_confirmation_bars

    def _minimum_dwell(self, state: AuctionStateName) -> int:
        mapping = {
            AuctionStateName.FRESH_EXPANSION: self.cfg.fresh_expansion_min_hold_bars,
            AuctionStateName.REACCELERATION: self.cfg.reacceleration_min_hold_bars,
            AuctionStateName.MATURE_EXTENSION: self.cfg.mature_extension_min_hold_bars,
            AuctionStateName.TREND_FAILURE: self.cfg.trend_failure_min_hold_bars,
            AuctionStateName.REVERSAL: self.cfg.reversal_min_hold_bars,
            AuctionStateName.CHAOTIC_ROTATION: self.cfg.chaotic_min_hold_bars,
        }
        return mapping[state] if state in mapping else self.cfg.minimum_state_hold_bars

    # ------------------------------------------------------------------
    # Transition side effects and episode anchors
    # ------------------------------------------------------------------
    def _on_transition(
        self,
        memory: _StateMemory,
        evidence: EvidenceSnapshot,
        previous: AuctionStateName,
        selected: AuctionStateName,
        flags: Dict[str, Any],
    ) -> None:
        if selected == AuctionStateName.FRESH_EXPANSION:
            side = DirectionalBias(flags["expansion_direction"])
            if side in (DirectionalBias.UP, DirectionalBias.DOWN):
                self._establish_trend(memory, evidence, side, anchor_from_bar=True)
            memory.trend_neutral_candidate_bars = 0
            self._reset_pause_episodes(memory)

        elif selected in {AuctionStateName.ORDERLY_UPTREND, AuctionStateName.ORDERLY_DOWNTREND}:
            side = (
                DirectionalBias.UP
                if selected == AuctionStateName.ORDERLY_UPTREND
                else DirectionalBias.DOWN
            )
            if previous == AuctionStateName.TREND_FAILURE:
                self._terminate_failure_episode(
                    memory,
                    evidence.snapshot_time,
                    "ORIGINAL_TREND_RECOVERED",
                )
            if memory.established_trend_side == DirectionalBias.UNKNOWN:
                self._establish_trend(memory, evidence, side, anchor_from_bar=True)
            memory.trend_neutral_candidate_bars = 0
            if previous == AuctionStateName.REVERSAL:
                self._reset_pause_episodes(memory)

        elif selected == AuctionStateName.CONTROLLED_PULLBACK:
            memory.pullback_onset_time = evidence.snapshot_time
            memory.pullback_age_bars = 1
            memory.pullback_extreme_price = (
                evidence.bar.low
                if memory.established_trend_side == DirectionalBias.UP
                else evidence.bar.high
            )
            memory.pullback_depth_atr = self._pullback_depth(memory, evidence)
            memory.pullback_episode_key = stable_key(
                "pullback",
                evidence.symbol,
                evidence.trading_day,
                memory.established_trend_side,
                memory.pullback_onset_time,
            )
            memory.recompression_episode_key = None
            memory.recompression_onset_time = None
            memory.recompression_age_bars = 0

        elif selected == AuctionStateName.RECOMPRESSION:
            memory.recompression_onset_time = evidence.snapshot_time
            memory.recompression_age_bars = 1
            memory.recompression_episode_key = stable_key(
                "recompression",
                evidence.symbol,
                evidence.trading_day,
                memory.established_trend_side,
                memory.compression_episode_key,
                memory.recompression_onset_time,
            )

        elif selected == AuctionStateName.REACCELERATION:
            memory.reacceleration_onset_time = evidence.snapshot_time
            memory.reacceleration_age_bars = 1
            memory.reacceleration_episode_key = stable_key(
                "reacceleration",
                evidence.symbol,
                evidence.trading_day,
                memory.established_trend_side,
                memory.pullback_episode_key or memory.recompression_episode_key,
                memory.reacceleration_onset_time,
            )
            anchor = memory.pullback_extreme_price
            if previous == AuctionStateName.CONTROLLED_PULLBACK and memory.pullback_extreme_price is not None:
                source = (
                    "CONFIRMED_PULLBACK_LOW"
                    if memory.established_trend_side == DirectionalBias.UP
                    else "CONFIRMED_PULLBACK_HIGH"
                )
                self._set_trend_protection(
                    memory,
                    evidence,
                    memory.pullback_extreme_price,
                    source,
                    memory.pullback_onset_time or evidence.snapshot_time,
                )
            elif previous == AuctionStateName.RECOMPRESSION:
                level = (
                    memory.compression_box_low
                    if memory.established_trend_side == DirectionalBias.UP
                    else memory.compression_box_high
                )
                if level is not None:
                    source = (
                        "RECOMPRESSION_BOX_LOW"
                        if memory.established_trend_side == DirectionalBias.UP
                        else "RECOMPRESSION_BOX_HIGH"
                    )
                    self._set_trend_protection(
                        memory,
                        evidence,
                        level,
                        source,
                        memory.recompression_onset_time or evidence.snapshot_time,
                    )
                    if anchor is None:
                        anchor = level
            self._reset_leg(memory, evidence, memory.established_trend_side, anchor_price=anchor)
            self._reset_pause_episodes(memory, keep_reacceleration=True)

        elif selected == AuctionStateName.MATURE_EXTENSION:
            memory.leg_maturity_consumed = True
            memory.leg_maturity_onset_time = evidence.snapshot_time
            memory.leg_maturity_extreme_price = memory.leg_extreme_price

        elif selected == AuctionStateName.TREND_FAILURE:
            memory.trend_failure_age_bars = 1
            memory.trend_failure_expired = False
            memory.reversal_confirmation_bars = 0
            memory.trend_recovery_bars = 0
            self._reset_pause_episodes(memory)

        elif selected == AuctionStateName.REVERSAL:
            side = memory.reversal_side
            memory.reversal_onset_time = evidence.snapshot_time
            self._terminate_failure_episode(
                memory,
                evidence.snapshot_time,
                "CONFIRMED_OPPOSITE_REVERSAL",
            )
            # A confirmed control transfer consumes the old exhausted move.
            # The new leg must build its own age and extension before it can
            # establish a fresh exhaustion episode.
            self._clear_exhaustion_context(memory)
            if side in (DirectionalBias.UP, DirectionalBias.DOWN):
                self._establish_trend(memory, evidence, side, anchor_from_bar=True)
            memory.trend_failure_age_bars = 0
            memory.trend_failure_expired = False
            memory.reversal_confirmation_bars = 0
            memory.trend_recovery_bars = 0
            self._reset_pause_episodes(memory)

        elif selected in {AuctionStateName.BALANCE, AuctionStateName.COMPRESSION, AuctionStateName.CHAOTIC_ROTATION}:
            if previous == AuctionStateName.TREND_FAILURE:
                self._terminate_failure_episode(
                    memory,
                    evidence.snapshot_time,
                    f"TREND_FAILURE_TERMINATED_TO_{selected.value}",
                )
            elif flags["trend_neutralisation_ready"] and memory.failure_episode_key:
                self._terminate_failure_episode(
                    memory,
                    evidence.snapshot_time,
                    f"FAILURE_WATCH_TERMINATED_BY_{selected.value}",
                )
            if flags["trend_neutralisation_ready"] or previous == AuctionStateName.TREND_FAILURE:
                self._clear_trend(memory)

        elif selected == AuctionStateName.UNKNOWN and previous == AuctionStateName.TREND_FAILURE:
            self._terminate_failure_episode(
                memory,
                evidence.snapshot_time,
                "TREND_FAILURE_TERMINATED_TO_UNKNOWN",
            )
            self._clear_trend(memory)

    def _advance_active_episode_ages(self, memory: _StateMemory, state: AuctionStateName) -> None:
        if state == AuctionStateName.CONTROLLED_PULLBACK:
            memory.pullback_age_bars += 1
        elif state == AuctionStateName.RECOMPRESSION:
            memory.recompression_age_bars += 1
        elif state == AuctionStateName.REACCELERATION:
            memory.reacceleration_age_bars += 1
        elif state == AuctionStateName.TREND_FAILURE:
            memory.trend_failure_age_bars += 1

    def _apply_post_selection_context(
        self,
        memory: _StateMemory,
        evidence: EvidenceSnapshot,
        selected: AuctionStateName,
        flags: Dict[str, Any],
    ) -> None:
        if (
            selected
            in {
                AuctionStateName.BALANCE,
                AuctionStateName.COMPRESSION,
                AuctionStateName.CHAOTIC_ROTATION,
                AuctionStateName.UNKNOWN,
            }
            and flags["trend_neutralisation_ready"]
            and memory.established_trend_side in (DirectionalBias.UP, DirectionalBias.DOWN)
        ):
            if memory.failure_episode_key:
                self._terminate_failure_episode(
                    memory,
                    evidence.snapshot_time,
                    f"FAILURE_WATCH_TERMINATED_BY_TREND_NEUTRALISATION_TO_{selected.value}",
                )
            self._clear_trend(memory)
            memory.trend_neutral_candidate_bars = 0

    def _establish_trend(
        self,
        memory: _StateMemory,
        evidence: EvidenceSnapshot,
        side: DirectionalBias,
        *,
        anchor_from_bar: bool,
    ) -> None:
        memory.established_trend_side = side
        memory.trend_onset_time = evidence.snapshot_time
        memory.trend_neutral_candidate_bars = 0
        anchor = evidence.close
        if anchor_from_bar:
            anchor = evidence.bar.low if side == DirectionalBias.UP else evidence.bar.high
        memory.trend_anchor_price = anchor
        memory.trend_extreme_price = evidence.bar.high if side == DirectionalBias.UP else evidence.bar.low
        memory.trend_protection_level = None
        memory.trend_protection_side = DirectionalBias.UNKNOWN
        memory.trend_protection_source = ""
        memory.trend_protection_time = None
        memory.trend_protection_version = 0
        memory.trend_protection_episode_key = None
        self._set_trend_protection(
            memory,
            evidence,
            anchor,
            "INITIAL_TREND_ANCHOR",
            evidence.snapshot_time,
            force=True,
        )
        memory.trend_candidate_side = side
        memory.trend_candidate_bars = max(memory.trend_candidate_bars, self.cfg.trend_establishment_bars)
        self._clear_failure_watch(memory)
        memory.failure_watch_expired = False
        memory.trend_failure_age_bars = 0
        memory.trend_failure_expired = False
        self._reset_leg(memory, evidence, side, anchor_price=anchor)

    def _set_trend_protection(
        self,
        memory: _StateMemory,
        evidence: EvidenceSnapshot,
        level: float,
        source: str,
        level_time: datetime,
        *,
        force: bool = False,
    ) -> bool:
        """Promote a confirmed structural level without weakening protection."""

        side = memory.established_trend_side
        if side not in (DirectionalBias.UP, DirectionalBias.DOWN):
            return False
        atr = _required_evidence_atr(evidence)
        minimum_improvement = atr * self.cfg.trend_protection_min_improvement_atr
        prior = memory.trend_protection_level
        if not force and prior is not None:
            if side == DirectionalBias.UP and level < prior + minimum_improvement:
                return False
            if side == DirectionalBias.DOWN and level > prior - minimum_improvement:
                return False
        memory.trend_protection_version += 1
        memory.trend_protection_level = float(level)
        memory.trend_protection_side = side
        memory.trend_protection_source = source
        memory.trend_protection_time = level_time
        memory.trend_protection_episode_key = stable_key(
            "trend-protection",
            evidence.symbol,
            evidence.trading_day,
            side,
            memory.trend_protection_version,
            source,
            level_time,
            level,
        )
        return True

    def _reset_leg(
        self,
        memory: _StateMemory,
        evidence: EvidenceSnapshot,
        side: DirectionalBias,
        *,
        anchor_price: Optional[float] = None,
    ) -> None:
        if side not in (DirectionalBias.UP, DirectionalBias.DOWN):
            memory.leg_anchor_time = None
            memory.leg_anchor_price = None
            memory.leg_extreme_price = None
            memory.leg_age_bars = 0
            memory.leg_no_progress_bars = 0
            memory.last_progress_time = None
            return
        if anchor_price is None:
            anchor_price = evidence.bar.low if side == DirectionalBias.UP else evidence.bar.high
        memory.leg_anchor_time = evidence.snapshot_time
        memory.leg_anchor_price = anchor_price
        memory.leg_extreme_price = evidence.bar.high if side == DirectionalBias.UP else evidence.bar.low
        memory.leg_age_bars = 1
        memory.leg_no_progress_bars = 0
        memory.last_progress_time = evidence.snapshot_time
        memory.leg_maturity_consumed = False
        memory.leg_maturity_onset_time = None
        memory.leg_maturity_extreme_price = None

    def _update_pullback_metrics(self, memory: _StateMemory, evidence: EvidenceSnapshot) -> None:
        if memory.established_trend_side == DirectionalBias.UP:
            memory.pullback_extreme_price = min(
                memory.pullback_extreme_price or evidence.bar.low,
                evidence.bar.low,
            )
        elif memory.established_trend_side == DirectionalBias.DOWN:
            memory.pullback_extreme_price = max(
                memory.pullback_extreme_price or evidence.bar.high,
                evidence.bar.high,
            )
        memory.pullback_depth_atr = max(
            memory.pullback_depth_atr,
            self._pullback_depth(memory, evidence),
        )

    def _pullback_depth(self, memory: _StateMemory, evidence: EvidenceSnapshot) -> float:
        if not evidence.atr or memory.trend_extreme_price is None:
            return 0.0
        if memory.established_trend_side == DirectionalBias.UP:
            return max(0.0, (memory.trend_extreme_price - evidence.bar.low) / evidence.atr)
        if memory.established_trend_side == DirectionalBias.DOWN:
            return max(0.0, (evidence.bar.high - memory.trend_extreme_price) / evidence.atr)
        return 0.0

    def _reset_pause_episodes(self, memory: _StateMemory, *, keep_reacceleration: bool = False) -> None:
        memory.pullback_candidate_bars = 0
        memory.pullback_episode_key = None
        memory.pullback_onset_time = None
        memory.pullback_age_bars = 0
        memory.pullback_extreme_price = None
        memory.pullback_depth_atr = 0.0
        memory.recompression_episode_key = None
        memory.recompression_onset_time = None
        memory.recompression_age_bars = 0
        if not keep_reacceleration:
            memory.reacceleration_episode_key = None
            memory.reacceleration_onset_time = None
            memory.reacceleration_age_bars = 0

    def _clear_trend(self, memory: _StateMemory) -> None:
        memory.established_trend_side = DirectionalBias.UNKNOWN
        memory.trend_candidate_side = DirectionalBias.UNKNOWN
        memory.trend_candidate_bars = 0
        memory.trend_neutral_candidate_bars = 0
        memory.trend_onset_time = None
        memory.trend_anchor_price = None
        memory.trend_extreme_price = None
        memory.trend_protection_level = None
        memory.trend_protection_side = DirectionalBias.UNKNOWN
        memory.trend_protection_source = ""
        memory.trend_protection_time = None
        memory.trend_protection_version = 0
        memory.trend_protection_episode_key = None
        self._reset_leg_fields(memory)
        self._reset_pause_episodes(memory)
        self._clear_failure_watch(memory)
        memory.failure_watch_expired = False
        memory.trend_failure_age_bars = 0
        memory.trend_failure_expired = False
        memory.reversal_confirmation_bars = 0
        memory.reversal_side = DirectionalBias.UNKNOWN

    def _reset_leg_fields(self, memory: _StateMemory) -> None:
        memory.leg_anchor_time = None
        memory.leg_anchor_price = None
        memory.leg_extreme_price = None
        memory.leg_age_bars = 0
        memory.leg_no_progress_bars = 0
        memory.last_progress_time = None
        memory.leg_maturity_consumed = False
        memory.leg_maturity_onset_time = None
        memory.leg_maturity_extreme_price = None

    def _clear_compression_episode(self, memory: _StateMemory) -> None:
        memory.compression_episode_key = None
        memory.compression_onset_time = None
        memory.compression_box_low = None
        memory.compression_box_high = None

    def _attach_memory_flags(
        self,
        flags: Dict[str, Any],
        memory: _StateMemory,
        evidence: EvidenceSnapshot,
    ) -> None:
        leg_distance, leg_current, leg_retracement, leg_retracement_fraction = self._leg_distances(
            memory, evidence
        )
        flags.update({
            "established_trend_side": memory.established_trend_side.value,
            "trend_candidate_side": memory.trend_candidate_side.value,
            "trend_candidate_bars": memory.trend_candidate_bars,
            "trend_candidate_ready": bool(
                memory.trend_candidate_side in (DirectionalBias.UP, DirectionalBias.DOWN)
                and memory.trend_candidate_bars >= self.cfg.trend_establishment_bars
            ),
            "trend_neutral_candidate_bars": memory.trend_neutral_candidate_bars,
            "trend_protection_level": memory.trend_protection_level,
            "trend_protection_side": memory.trend_protection_side.value,
            "trend_protection_source": memory.trend_protection_source,
            "trend_protection_time": _iso(memory.trend_protection_time),
            "trend_protection_version": memory.trend_protection_version,
            "trend_protection_episode_key": memory.trend_protection_episode_key,
            "compression_candidate_bars": memory.compression_candidate_bars,
            "compression_episode_key": memory.compression_episode_key,
            "compression_box_low": memory.compression_box_low,
            "compression_box_high": memory.compression_box_high,
            "pullback_episode_key": memory.pullback_episode_key,
            "pullback_age_bars": memory.pullback_age_bars,
            "pullback_depth_atr": memory.pullback_depth_atr,
            "recompression_episode_key": memory.recompression_episode_key,
            "recompression_age_bars": memory.recompression_age_bars,
            "reacceleration_episode_key": memory.reacceleration_episode_key,
            "reacceleration_age_bars": memory.reacceleration_age_bars,
            "failure_episode_key": memory.failure_episode_key,
            "failure_watch_bars": memory.failure_watch_bars,
            "failure_level": memory.failure_level,
            "failure_level_source": memory.failure_level_source,
            "failure_level_time": _iso(memory.failure_level_time),
            "failure_level_version": memory.failure_level_version,
            "failure_level_episode_key": memory.failure_level_episode_key,
            "failure_level_breach_bars": memory.failure_level_breach_bars,
            "failure_structure_loss_bars": memory.failure_structure_loss_bars,
            "local_structure_weakening_bars": memory.local_structure_weakening_bars,
            "failure_close_distance_beyond_level_atr": memory.failure_close_distance_beyond_level_atr,
            "structure_loss_distance_to_protection_atr": memory.structure_loss_distance_to_protection_atr,
            "structure_loss_near_protection": memory.structure_loss_near_protection,
            "structure_loss_directional_corroboration": memory.structure_loss_directional_corroboration,
            "structure_loss_value_migration_corroboration": memory.structure_loss_value_migration_corroboration,
            "structure_loss_confirmation_blockers": list(memory.structure_loss_confirmation_blockers),
            "failure_confirmation_reason": memory.failure_confirmation_reason,
            "last_failure_confirmation_reason": memory.last_failure_confirmation_reason,
            "last_failure_confirmation_time": _iso(memory.last_failure_confirmation_time),
            "failure_watch_reason_codes": list(memory.failure_watch_reason_codes),
            "failure_watch_reset_reason": memory.failure_watch_reset_reason,
            "failure_watch_expired": memory.failure_watch_expired,
            "last_failure_terminal_key": memory.last_failure_terminal_key,
            "last_failure_terminal_reason": memory.last_failure_terminal_reason,
            "last_failure_terminal_time": _iso(memory.last_failure_terminal_time),
            "last_failure_watch_onset": _iso(memory.last_failure_watch_onset),
            "last_failure_side": memory.last_failure_side.value,
            "last_failure_original_trend_side": memory.last_failure_original_trend_side.value,
            "last_failure_level": memory.last_failure_level,
            "last_failure_level_source": memory.last_failure_level_source,
            "last_failure_level_time": _iso(memory.last_failure_level_time),
            "last_failure_level_version": memory.last_failure_level_version,
            "last_failure_level_episode_key": memory.last_failure_level_episode_key,
            "last_failure_atr": memory.last_failure_atr,
            "last_failure_structure_low": memory.last_failure_structure_low,
            "last_failure_structure_high": memory.last_failure_structure_high,
            "last_failure_trend_anchor_price": memory.last_failure_trend_anchor_price,
            "last_failure_trend_extreme_price": memory.last_failure_trend_extreme_price,
            "trend_failure_age_bars": memory.trend_failure_age_bars,
            "trend_failure_expired": memory.trend_failure_expired,
            "reversal_confirmation_bars": memory.reversal_confirmation_bars,
            "stock_context_balance_bars": memory.stock_context_balance_bars,
            "exhaustion_context_active": memory.exhaustion_active,
            "exhausted_side": memory.exhaustion_side.value,
            "exhaustion_started_at": _iso(memory.exhaustion_started_at),
            "exhaustion_age_bars": memory.exhaustion_age_bars,
            "exhaustion_reason_codes": list(memory.exhaustion_reason_codes),
            "leg_anchor_price": memory.leg_anchor_price,
            "leg_extreme_price": memory.leg_extreme_price,
            "leg_age_bars": memory.leg_age_bars,
            "leg_no_progress_bars": memory.leg_no_progress_bars,
            "leg_maturity_consumed": memory.leg_maturity_consumed,
            "leg_maturity_onset_time": _iso(memory.leg_maturity_onset_time),
            "leg_maturity_extreme_price": memory.leg_maturity_extreme_price,
            "current_leg_distance_atr": leg_distance,
            "current_leg_current_distance_atr": leg_current,
            "current_leg_retracement_atr": leg_retracement,
            "current_leg_retracement_fraction": leg_retracement_fraction,
        })

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def _update_rotation_memory(self, memory: _StateMemory, evidence: EvidenceSnapshot) -> None:
        raw_structure = _required_raw_section(evidence, "source_structure")
        observations = (
            (memory.hma_direction_history, _direction_from_text(evidence.trend.hma_order)),
            (memory.vwap_direction_history, _direction_from_text(evidence.trend.vwap_side)),
            (memory.structure_direction_history, _direction_from_text(raw_structure["raw_side"])),
            (memory.bar_direction_history, evidence.bar.direction),
        )
        for history, direction in observations:
            history.append(direction)
            if len(history) > self.cfg.history_bars:
                del history[: len(history) - self.cfg.history_bars]

    def _trend_support_count(self, evidence: EvidenceSnapshot, side: DirectionalBias) -> int:
        trend = evidence.trend
        supports = 0
        if trend.direction == side:
            supports += 1
        if trend.value_migration == side:
            supports += 1
        if _direction_from_text(trend.hma_order) == side:
            supports += 1
        if side == DirectionalBias.UP and "ABOVE" in trend.vwap_side:
            supports += 1
        if side == DirectionalBias.DOWN and "BELOW" in trend.vwap_side:
            supports += 1
        if side == DirectionalBias.UP and trend.open_control == "ABOVE_OPEN":
            supports += 1
        if side == DirectionalBias.DOWN and trend.open_control == "BELOW_OPEN":
            supports += 1
        return supports

    def _enough_history(self, evidence: EvidenceSnapshot, memory: _StateMemory) -> bool:
        source_windows = _required_raw_section(evidence, "source_windows")
        if "sod" not in source_windows or not isinstance(source_windows["sod"], Mapping):
            raise ValueError("Evidence raw_facts.source_windows.sod is required")
        sod_bars = _strict_int(
            source_windows["sod"]["bars"],
            "source_windows.sod.bars",
        )
        observed = max(sod_bars, memory.observation_count + 1)
        return observed >= self.evidence_cfg.minimum_history_bars

    @staticmethod
    def _orderly_state(side: DirectionalBias) -> AuctionStateName:
        return (
            AuctionStateName.ORDERLY_UPTREND
            if side == DirectionalBias.UP
            else AuctionStateName.ORDERLY_DOWNTREND
            if side == DirectionalBias.DOWN
            else AuctionStateName.UNKNOWN
        )

    def _state_facts(
        self,
        evidence: EvidenceSnapshot,
        state: AuctionStateName,
        flags: Dict[str, Any],
    ) -> Tuple[Tuple[EvidenceFact, ...], Tuple[EvidenceFact, ...]]:
        ts = evidence.snapshot_time
        supporting = []
        contradicting = []

        def support(code: str, value: Any, source: str) -> None:
            supporting.append(_fact(code, ts, value, source))

        def contradict(code: str, value: Any, source: str) -> None:
            contradicting.append(_fact(code, ts, value, source, EvidencePolarity.CONTRADICT))

        if state == AuctionStateName.UNKNOWN:
            support("STATE_INSUFFICIENT_OR_MIXED_EVIDENCE", flags["enough_history"], "state.flags.enough_history")
        elif state == AuctionStateName.BALANCE:
            support("STATE_LOW_DIRECTIONAL_EFFICIENCY", flags["efficiency"], "trend.directional_efficiency")
            support("STATE_HIGH_OVERLAP", flags["overlap"], "price_action.overlap_ratio")
        elif state == AuctionStateName.COMPRESSION:
            support("STATE_PERSISTENT_PRICE_COMPRESSION", flags["compression_candidate_bars"], "state.compression_candidate_bars")
            support("STATE_FROZEN_COMPRESSION_BOX", flags["compression_episode_key"], "state.compression_episode_key")
        elif state == AuctionStateName.BOUNDARY_INTERACTION:
            support("STATE_DYNAMIC_BOUNDARY_INTERACTION", evidence.boundary.current_offset_atr if evidence.boundary else None, "boundary.current_offset_atr")
        elif state == AuctionStateName.FRESH_EXPANSION:
            support("STATE_STRONG_DISPLACEMENT", flags["move_atr"], "bar.move_atr")
            support("STATE_BOUNDARY_DEPARTURE", flags["outside_direction"], "boundary")
        elif state == AuctionStateName.ORDERLY_UPTREND:
            support("STATE_ESTABLISHED_UPTREND", flags["established_trend_side"], "state.established_trend_side")
            support("STATE_DIRECTIONAL_EFFICIENCY", flags["efficiency"], "trend.directional_efficiency")
        elif state == AuctionStateName.ORDERLY_DOWNTREND:
            support("STATE_ESTABLISHED_DOWNTREND", flags["established_trend_side"], "state.established_trend_side")
            support("STATE_DIRECTIONAL_EFFICIENCY", flags["efficiency"], "trend.directional_efficiency")
        elif state == AuctionStateName.CONTROLLED_PULLBACK:
            support("STATE_PERSISTENT_CONTROLLED_PULLBACK", flags["pullback_episode_key"], "state.pullback_episode_key")
            support("STATE_PULLBACK_DEPTH_ATR", flags["pullback_depth_atr"], "state.pullback_depth_atr")
        elif state == AuctionStateName.RECOMPRESSION:
            support("STATE_PERSISTENT_RECOMPRESSION", flags["recompression_episode_key"], "state.recompression_episode_key")
            support("STATE_FROZEN_COMPRESSION_BOX", flags["compression_episode_key"], "state.compression_episode_key")
        elif state == AuctionStateName.REACCELERATION:
            support("STATE_ANCHORED_REACCELERATION", flags["reacceleration_episode_key"], "state.reacceleration_episode_key")
            support("STATE_REACCELERATION_DISPLACEMENT", flags["move_atr"], "bar.move_atr")
        elif state == AuctionStateName.MATURE_EXTENSION:
            support("STATE_CURRENT_LEG_MAX_EXCURSION_ATR", flags["current_leg_distance_atr"], "state.current_leg_distance_atr")
            support("STATE_CURRENT_LEG_CURRENT_EXCURSION_ATR", flags["current_leg_current_distance_atr"], "state.current_leg_current_distance_atr")
            support("STATE_CURRENT_LEG_RETRACEMENT_ATR", flags["current_leg_retracement_atr"], "state.current_leg_retracement_atr")
            if evidence.trend.retained_structure is True:
                contradict("STATE_EXTENSION_WITH_TREND_RETAINED", True, "trend.retained_structure")
        elif state == AuctionStateName.TREND_FAILURE:
            support("STATE_FAILURE_WATCH_CONFIRMED", flags["failure_watch_bars"], "state.failure_watch_bars")
            support("STATE_STRUCTURAL_FAILURE_REASON", flags["failure_confirmation_reason"], "state.failure_confirmation_reason")
            support("STATE_PROTECTED_FAILURE_LEVEL", flags["failure_level"], "state.failure_level")
        elif state == AuctionStateName.REVERSAL:
            support("STATE_CONFIRMED_OPPOSITE_FOLLOWTHROUGH", flags["reversal_confirmation_bars"], "state.reversal_confirmation_bars")
        elif state == AuctionStateName.CHAOTIC_ROTATION:
            support("STATE_REPEATED_LOCAL_FLIPS", flags["local_flip_counts"], "auction_state.local_rotation_window")
            support("STATE_LOW_EFFICIENCY_ROTATION", flags["efficiency"], "trend.directional_efficiency")

        if state not in {AuctionStateName.MATURE_EXTENSION, AuctionStateName.TREND_FAILURE, AuctionStateName.REVERSAL} and flags["current_leg_mature"]:
            contradict("CONTRADICTION_CURRENT_LEG_MATURITY_RISK", True, "state.current_leg_mature")
        if state not in {AuctionStateName.BALANCE, AuctionStateName.COMPRESSION, AuctionStateName.RECOMPRESSION} and flags["compression_ready"]:
            contradict("CONTRADICTION_PERSISTENT_COMPRESSION_PRESENT", True, "state.compression_ready")
        if state not in {AuctionStateName.CHAOTIC_ROTATION, AuctionStateName.UNKNOWN} and flags["chaos_ready"]:
            contradict("CONTRADICTION_CHAOTIC_ROTATION", True, "auction_state.local_rotation_window")
        return tuple(supporting), tuple(contradicting)

    def _confidence_channels(
        self,
        evidence: EvidenceSnapshot,
        flags: Dict[str, Any],
    ) -> Tuple[ConfidenceChannel, ...]:
        coverage = _required_number(evidence.data_quality.coverage, "data_quality.coverage")
        efficiency = flags["efficiency"]
        overlap = flags["overlap"]
        balance_score = 0.0
        if efficiency is not None:
            balance_score += 45.0 * max(0.0, 1.0 - efficiency)
        if overlap is not None:
            balance_score += 30.0 * overlap
        if flags["compression_ready"]:
            balance_score += 25.0

        move = abs(_required_number(flags["move_atr"], "flags.move_atr"))
        expansion_score = min(45.0, move * 45.0)
        expansion_score += 25.0 if flags["boundary_outside"] else 0.0
        expansion_score += 15.0 if flags["strong_up"] or flags["strong_down"] else 0.0
        expansion_score += 15.0 if evidence.price_action.followthrough else 0.0

        trend_support = max(flags["trend_support_up"], flags["trend_support_down"])
        trend_score = min(55.0, trend_support * 11.0)
        if efficiency is not None:
            trend_score += min(30.0, efficiency * 30.0)
        if flags["established_trend_side"] != DirectionalBias.UNKNOWN.value:
            trend_score += 15.0

        extension_score = 0.0
        if flags["current_leg_distance_atr"] is not None:
            extension_score += min(35.0, flags["current_leg_distance_atr"] * 15.0)
        if flags["current_leg_current_distance_atr"] is not None:
            extension_score += min(35.0, max(0.0, flags["current_leg_current_distance_atr"]) * 20.0)
        if flags["current_leg_retracement_fraction"] is not None:
            extension_score += max(0.0, 15.0 * (1.0 - flags["current_leg_retracement_fraction"]))
        extension_score += min(20.0, flags["leg_no_progress_bars"] * 8.0)
        extension_score += 10.0 if flags["current_leg_mature"] else 0.0

        chaos_score = min(
            100.0,
            flags["independent_flip_channels"] * 25.0
            + flags["bar_flip_count"] * 5.0
            + (25.0 if flags["chaos_ready"] else 0.0),
        )

        return (
            ConfidenceChannel(
                name="data_quality",
                score=_clamp100(coverage * 100.0),
                quality=evidence.data_quality.status,
                reason_codes=evidence.data_quality.reason_codes,
            ),
            ConfidenceChannel(
                name="balance_compression",
                score=_clamp100(balance_score),
                quality=evidence.compression.quality.status,
                reason_codes=evidence.compression.reason_codes,
            ),
            ConfidenceChannel(
                name="fresh_expansion",
                score=_clamp100(expansion_score),
                quality=evidence.price_action.quality.status,
                supporting_fact_codes=tuple(f.code for f in evidence.price_action.supporting_facts),
                contradicting_fact_codes=tuple(f.code for f in evidence.price_action.contradicting_facts),
            ),
            ConfidenceChannel(
                name="trend",
                score=_clamp100(trend_score),
                quality=evidence.trend.quality.status,
                supporting_fact_codes=tuple(f.code for f in evidence.trend.supporting_facts),
                contradicting_fact_codes=tuple(f.code for f in evidence.trend.contradicting_facts),
            ),
            ConfidenceChannel(
                name="extension_maturity",
                score=_clamp100(extension_score),
                quality=evidence.extension.quality.status,
                supporting_fact_codes=tuple(f.code for f in evidence.extension.supporting_facts),
                contradicting_fact_codes=tuple(f.code for f in evidence.extension.contradicting_facts),
            ),
            ConfidenceChannel(
                name="chaotic_rotation",
                score=_clamp100(chaos_score),
                quality=QualityStatus.GOOD if flags["independent_flip_channels"] else QualityStatus.PARTIAL,
                reason_codes=("LOCAL_MULTI_CHANNEL_ROTATION",) if flags["chaos_ready"] else (),
            ),
        )


def _direction_from_text(value: Any) -> DirectionalBias:
    text = str(value or "").strip().upper()
    if any(token in text for token in ("UP", "BULL", "ABOVE", "BUY")):
        return DirectionalBias.UP
    if any(token in text for token in ("DOWN", "BEAR", "BELOW", "SELL")):
        return DirectionalBias.DOWN
    return DirectionalBias.UNKNOWN


def _opposite(side: DirectionalBias) -> DirectionalBias:
    if side == DirectionalBias.UP:
        return DirectionalBias.DOWN
    if side == DirectionalBias.DOWN:
        return DirectionalBias.UP
    return DirectionalBias.UNKNOWN


def _rolling_flip_count(
    history: Iterable[DirectionalBias],
    current: DirectionalBias,
    max_bars: int,
) -> int:
    values = list(history)[-max(0, max_bars - 1):] + [current]
    directional = [
        value for value in values
        if value in (DirectionalBias.UP, DirectionalBias.DOWN)
    ]
    return sum(
        directional[index] != directional[index - 1]
        for index in range(1, len(directional))
    )


def _fact(
    code: str,
    observed_at: datetime,
    value: Any,
    source_path: str,
    polarity: EvidencePolarity = EvidencePolarity.SUPPORT,
) -> EvidenceFact:
    return EvidenceFact(
        code=code,
        domain="auction_state",
        polarity=polarity,
        observed_at=observed_at,
        value=value,
        source_path=source_path,
        quality=QualityStatus.GOOD if value is not None else QualityStatus.UNKNOWN,
    )



def _required_evidence_atr(evidence: EvidenceSnapshot) -> float:
    if evidence.atr is None or evidence.atr <= 0:
        raise ValueError("Auction evidence ATR is required and must be positive")
    return float(evidence.atr)


def _required_number(value: Any, path: str) -> float:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{path} is required and must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path} is required and must be numeric") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"{path} must be finite")
    return number

def _required_raw_section(
    evidence: EvidenceSnapshot,
    key: str,
) -> Mapping[str, Any]:
    if key not in evidence.raw_facts:
        raise ValueError(f"Evidence raw_facts.{key} is required")
    section = evidence.raw_facts[key]
    if not isinstance(section, Mapping):
        raise ValueError(f"Evidence raw_facts.{key} must be a mapping")
    return section


def _optional_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Boolean is not a valid numeric Auction fact")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Expected numeric Auction fact, got {value!r}") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"Auction numeric fact must be finite, got {value!r}")
    return number


def _optional_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Expected datetime Auction fact, got {value!r}") from exc


def _path_efficiency(
    net_points: float,
    path_points: float,
    tolerance: float,
) -> Optional[float]:
    if path_points <= tolerance:
        return None
    return max(0.0, min(1.0, float(net_points) / float(path_points)))


def _step_ratio(favourable_steps: int, total_steps: int) -> Optional[float]:
    if total_steps <= 0:
        return None
    return max(0.0, min(1.0, float(favourable_steps) / float(total_steps)))


def _strict_int(value: Any, path: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{path} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path} must be an integer") from exc


def _clamp100(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _unique(values: Iterable[str]) -> Tuple[str, ...]:
    seen = set()
    out = []
    for value in values:
        text = str(value or "").strip().upper()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return tuple(out)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat(sep=" ") if value else None


__all__ = [
    "AuctionStateChronologyError",
    "AuctionStateEngine",
    "StateEvaluation",
]
