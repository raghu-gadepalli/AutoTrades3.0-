"""Authoritative sequential Auction episode controller.

A caller processes each symbol-day from its first completed snapshot in strict
chronological order. The controller owns one directional episode and one
balance episode, emits deterministic transition events, and resolves the
central structural setup-permission matrix. It does not manage signals, trades,
targets, stops or database persistence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG, AuctionEngineConfig
from enums.auction_engine import (
    AuctionEventType,
    AuctionStateName,
    BalanceEpisodeState,
    DirectionObservationSource,
    DirectionalBias,
    DirectionalEfficiencySource,
    DirectionalEpisodeOrigin,
    DirectionalEpisodeState,
    MaturityObservationSource,
    ReversalWatchSource,
)
from services.auction_engine.episode_contracts import (
    AuctionEvent,
    AuctionLifecycleProjection,
    AuctionObservation,
    BalanceEpisodeProjection,
    DirectionalEpisodeProjection,
)
from services.auction_engine.structural_permissions import StructuralPermissionMatrix


class EpisodeChronologyError(ValueError):
    """Raised when one symbol-day is not processed in strict time order."""


@dataclass
class _DirectionalMemory:
    sequence: int = 0
    episode_id: Optional[str] = None
    state: DirectionalEpisodeState = DirectionalEpisodeState.NONE
    direction: DirectionalBias = DirectionalBias.UNKNOWN
    origin_source: DirectionalEpisodeOrigin = DirectionalEpisodeOrigin.NONE
    parent_episode_id: Optional[str] = None
    origin_event_id: Optional[str] = None
    started_at: Optional[datetime] = None
    state_started_at: Optional[datetime] = None
    state_age_bars: int = 0
    origin_price: Optional[float] = None
    extreme_price: Optional[float] = None
    extreme_time: Optional[datetime] = None
    protection_level: Optional[float] = None
    protection_source: str = ""
    protection_time: Optional[datetime] = None
    start_candidate_side: DirectionalBias = DirectionalBias.UNKNOWN
    start_candidate_bars: int = 0
    rejection_seen: bool = False
    rejection_seen_at: Optional[datetime] = None
    continuation_failure_seen: bool = False
    continuation_failure_seen_at: Optional[datetime] = None
    continuation_failure_progress_bars: int = 0
    first_adverse_bar_time: Optional[datetime] = None
    first_adverse_bar_level: Optional[float] = None
    first_adverse_bar_close: Optional[float] = None
    reversal_confirmation_level: Optional[float] = None
    reversal_confirmation_source: str = ""
    reversal_confirmation_level_time: Optional[datetime] = None
    reversal_confirmation_breach_closes: int = 0
    reversal_watch_age_bars: int = 0
    reversal_leg_progress_bars: int = 0
    reversal_leg_failure_closes: int = 0
    reversal_leg_progress_atr: float = 0.0
    trend_restore_bars: int = 0
    opposite_control_bars: int = 0
    inactive_bars: int = 0
    emitted_event_ids: Set[str] = field(default_factory=set)
    last_close: Optional[float] = None
    last_observation_state: AuctionStateName = AuctionStateName.UNKNOWN
    last_observation_state_time: Optional[datetime] = None
    last_reason_codes: Tuple[str, ...] = ()


@dataclass
class _BalanceMemory:
    sequence: int = 0
    episode_id: Optional[str] = None
    state: BalanceEpisodeState = BalanceEpisodeState.NONE
    started_at: Optional[datetime] = None
    state_started_at: Optional[datetime] = None
    state_age_bars: int = 0
    range_id: Optional[str] = None
    candidate_low: Optional[float] = None
    candidate_high: Optional[float] = None
    source_range_ids: List[str] = field(default_factory=list)
    candidate_merge_count: int = 0
    candidate_bar_expansion_count: int = 0
    candidate_last_valid_at: Optional[datetime] = None
    frozen_low: Optional[float] = None
    frozen_high: Optional[float] = None
    containment_bars: int = 0
    forming_bars_observed: int = 0
    marginal_excursion_bars: int = 0
    meaningful_escape_bars: int = 0
    forming_invalid_bars: int = 0
    escape_direction: DirectionalBias = DirectionalBias.UNKNOWN
    outside_close_count: int = 0
    reentry_close_count: int = 0
    escape_attempt_count: int = 0
    failed_escape_count: int = 0
    up_escape_attempt_count: int = 0
    down_escape_attempt_count: int = 0
    last_escape_direction: DirectionalBias = DirectionalBias.UNKNOWN
    last_escape_started_at: Optional[datetime] = None
    last_escape_failed_at: Optional[datetime] = None
    rearm_required: bool = False
    rearm_inside_close_count: int = 0
    rearm_bars_elapsed: int = 0
    attempt_limit_reached: bool = False
    emitted_event_ids: Set[str] = field(default_factory=set)
    last_reason_codes: Tuple[str, ...] = ()


@dataclass
class _SymbolMemory:
    trading_day: date
    last_snapshot_time: Optional[datetime] = None
    last_observation_hash: str = ""
    last_evaluation: Optional[AuctionLifecycleProjection] = None
    directional: _DirectionalMemory = field(default_factory=_DirectionalMemory)
    balance: _BalanceMemory = field(default_factory=_BalanceMemory)


class PersistentEpisodeEngine:
    """Advance authoritative directional and balance lifecycle by one snapshot."""

    def __init__(self, config: AuctionEngineConfig = AUCTION_ENGINE_CONFIG) -> None:
        self.config = config
        self.cfg = config.episode
        self.directional_cfg = self.cfg.directional
        self.balance_cfg = self.cfg.balance
        self.permission_matrix = StructuralPermissionMatrix(config)
        self._memory: Dict[str, _SymbolMemory] = {}

    def reset(self, symbol: Optional[str] = None) -> None:
        if symbol is None:
            self._memory.clear()
            return
        key = str(symbol).strip().upper()
        self._memory.pop(key, None)

    def advance(self, observation: AuctionObservation) -> AuctionLifecycleProjection:
        if not isinstance(observation, AuctionObservation):
            raise TypeError("PersistentEpisodeEngine.advance requires AuctionObservation")
        symbol = observation.symbol
        memory = self._memory[symbol] if symbol in self._memory else None
        if memory is None or memory.trading_day != observation.trading_day:
            memory = _SymbolMemory(trading_day=observation.trading_day)
            self._memory[symbol] = memory

        observation_hash = observation.stable_hash()
        if memory.last_snapshot_time is not None:
            if observation.snapshot_time < memory.last_snapshot_time:
                raise EpisodeChronologyError(
                    f"Out-of-order episode observation for {symbol}: "
                    f"{observation.snapshot_time} < {memory.last_snapshot_time}"
                )
            if observation.snapshot_time == memory.last_snapshot_time:
                if (
                    memory.last_observation_hash == observation_hash
                    and memory.last_evaluation is not None
                ):
                    return memory.last_evaluation
                raise EpisodeChronologyError(
                    f"Conflicting duplicate episode observation for {symbol} "
                    f"@ {observation.snapshot_time}"
                )

        events: List[AuctionEvent] = []
        directional_previous = memory.directional.state
        balance_previous = memory.balance.state

        # Balance advances first so a newly locked balance can prevent a new
        # directional episode on the same completed snapshot. Existing
        # directional episodes continue to record structural events, while the
        # setup-permission projection applies balance precedence afterward.
        balance_reasons = self._advance_balance(
            memory.balance,
            observation,
            events,
        )
        directional_reasons = self._advance_directional(
            memory.directional,
            observation,
            events,
            balance_state=memory.balance.state,
        )
        permissions = self.permission_matrix.evaluate(
            balance_state=memory.balance.state,
            events=tuple(events),
        )

        evaluation = AuctionLifecycleProjection(
            symbol=symbol,
            trading_day=observation.trading_day,
            snapshot_time=observation.snapshot_time,
            directional=DirectionalEpisodeProjection(
                episode_id=memory.directional.episode_id,
                previous_state=directional_previous,
                current_state=memory.directional.state,
                direction=memory.directional.direction,
                origin_source=memory.directional.origin_source,
                parent_episode_id=memory.directional.parent_episode_id,
                origin_event_id=memory.directional.origin_event_id,
                started_at=memory.directional.started_at,
                state_started_at=memory.directional.state_started_at,
                state_age_bars=memory.directional.state_age_bars,
                origin_price=memory.directional.origin_price,
                extreme_price=memory.directional.extreme_price,
                extreme_time=memory.directional.extreme_time,
                protection_level=memory.directional.protection_level,
                protection_source=memory.directional.protection_source,
                reversal_confirmation_level=(
                    memory.directional.reversal_confirmation_level
                ),
                reversal_confirmation_source=(
                    memory.directional.reversal_confirmation_source
                ),
                reversal_confirmation_level_time=(
                    memory.directional.reversal_confirmation_level_time
                ),
                first_adverse_bar_time=memory.directional.first_adverse_bar_time,
                first_adverse_bar_level=memory.directional.first_adverse_bar_level,
                first_adverse_bar_close=memory.directional.first_adverse_bar_close,
                rejection_seen=memory.directional.rejection_seen,
                continuation_failure_seen=(
                    memory.directional.continuation_failure_seen
                ),
                continuation_failure_time=(
                    memory.directional.continuation_failure_seen_at
                ),
                reversal_confirmation_breach_closes=(
                    memory.directional.reversal_confirmation_breach_closes
                ),
                reversal_leg_progress_bars=(
                    memory.directional.reversal_leg_progress_bars
                ),
                reversal_leg_failure_closes=(
                    memory.directional.reversal_leg_failure_closes
                ),
                reversal_leg_progress_atr=(
                    memory.directional.reversal_leg_progress_atr
                ),
                reason_codes=directional_reasons,
            ),
            balance=BalanceEpisodeProjection(
                episode_id=memory.balance.episode_id,
                previous_state=balance_previous,
                current_state=memory.balance.state,
                started_at=memory.balance.started_at,
                state_started_at=memory.balance.state_started_at,
                state_age_bars=memory.balance.state_age_bars,
                range_id=memory.balance.range_id,
                candidate_low=memory.balance.candidate_low,
                candidate_high=memory.balance.candidate_high,
                source_range_ids=tuple(memory.balance.source_range_ids),
                candidate_merge_count=memory.balance.candidate_merge_count,
                candidate_bar_expansion_count=(
                    memory.balance.candidate_bar_expansion_count
                ),
                candidate_last_valid_at=memory.balance.candidate_last_valid_at,
                forming_invalid_bars=memory.balance.forming_invalid_bars,
                frozen_low=memory.balance.frozen_low,
                frozen_high=memory.balance.frozen_high,
                containment_bars=memory.balance.containment_bars,
                forming_bars_observed=memory.balance.forming_bars_observed,
                marginal_excursion_bars=memory.balance.marginal_excursion_bars,
                meaningful_escape_bars=memory.balance.meaningful_escape_bars,
                containment_ratio=self._balance_containment_ratio(memory.balance),
                escape_direction=memory.balance.escape_direction,
                outside_close_count=memory.balance.outside_close_count,
                reentry_close_count=memory.balance.reentry_close_count,
                escape_attempt_count=memory.balance.escape_attempt_count,
                failed_escape_count=memory.balance.failed_escape_count,
                up_escape_attempt_count=memory.balance.up_escape_attempt_count,
                down_escape_attempt_count=memory.balance.down_escape_attempt_count,
                last_escape_direction=memory.balance.last_escape_direction,
                last_escape_started_at=memory.balance.last_escape_started_at,
                last_escape_failed_at=memory.balance.last_escape_failed_at,
                rearm_required=memory.balance.rearm_required,
                rearm_inside_close_count=memory.balance.rearm_inside_close_count,
                rearm_bars_elapsed=memory.balance.rearm_bars_elapsed,
                attempt_limit_reached=memory.balance.attempt_limit_reached,
                reason_codes=balance_reasons,
            ),
            events=tuple(events),
            permissions=permissions,
            diagnostics={
                "observation_state": observation.observation_state.value,
                "observation_directional_bias": observation.directional_bias.value,
                "observation_current_leg_mature": observation.current_leg_mature,
                "observation_exhaustion_active": observation.exhaustion_active,
                "observation_accepted_range_id": observation.accepted_range_id,
                "directional_start_candidate_side": (
                    memory.directional.start_candidate_side.value
                ),
                "directional_start_candidate_bars": (
                    memory.directional.start_candidate_bars
                ),
                "directional_reversal_watch_age_bars": (
                    memory.directional.reversal_watch_age_bars
                ),
                "balance_forming_invalid_bars": memory.balance.forming_invalid_bars,
                "balance_forming_bars_observed": memory.balance.forming_bars_observed,
                "balance_marginal_excursion_bars": memory.balance.marginal_excursion_bars,
                "balance_meaningful_escape_bars": memory.balance.meaningful_escape_bars,
                "balance_containment_ratio": self._balance_containment_ratio(memory.balance),
                "balance_candidate_merge_count": memory.balance.candidate_merge_count,
                "balance_candidate_bar_expansion_count": (
                    memory.balance.candidate_bar_expansion_count
                ),
                "balance_escape_attempt_count": memory.balance.escape_attempt_count,
                "balance_failed_escape_count": memory.balance.failed_escape_count,
                "balance_up_escape_attempt_count": (
                    memory.balance.up_escape_attempt_count
                ),
                "balance_down_escape_attempt_count": (
                    memory.balance.down_escape_attempt_count
                ),
                "balance_last_escape_direction": (
                    memory.balance.last_escape_direction.value
                ),
                "balance_last_escape_started_at": (
                    memory.balance.last_escape_started_at
                ),
                "balance_last_escape_failed_at": (
                    memory.balance.last_escape_failed_at
                ),
                "balance_rearm_required": memory.balance.rearm_required,
                "balance_rearm_inside_close_count": (
                    memory.balance.rearm_inside_close_count
                ),
                "balance_rearm_bars_elapsed": memory.balance.rearm_bars_elapsed,
                "balance_attempt_limit_reached": (
                    memory.balance.attempt_limit_reached
                ),
                "directional_origin_source": memory.directional.origin_source.value,
                "directional_parent_episode_id": memory.directional.parent_episode_id,
                "directional_origin_event_id": memory.directional.origin_event_id,
            },
            engine_name=self.config.engine.engine_name,
            engine_version=self.config.engine.engine_version,
        )
        memory.last_snapshot_time = observation.snapshot_time
        memory.last_observation_hash = observation_hash
        memory.last_evaluation = evaluation
        return evaluation

    def _advance_directional(
        self,
        memory: _DirectionalMemory,
        observation: AuctionObservation,
        events: List[AuctionEvent],
        *,
        balance_state: BalanceEpisodeState,
    ) -> Tuple[str, ...]:
        reasons: List[str] = []
        previous_close = memory.last_close
        previous_observation_state = memory.last_observation_state
        if memory.state is not DirectionalEpisodeState.NONE:
            memory.state_age_bars += 1

        observed_side = self._observed_direction(observation)
        can_start = (
            observation.observation_state
            in self.directional_cfg.start_observation_states
        )
        balance_blocks_start = (
            balance_state in self.directional_cfg.start_blocking_balance_states
        )

        if memory.state is DirectionalEpisodeState.COMPLETED:
            self._reset_completed_directional(memory)

        if memory.state is DirectionalEpisodeState.NONE:
            if balance_blocks_start:
                memory.start_candidate_side = DirectionalBias.UNKNOWN
                memory.start_candidate_bars = 0
                reasons.append("DIRECTIONAL_START_BLOCKED_BY_ACTIVE_BALANCE")
            elif can_start and observed_side in (
                DirectionalBias.UP,
                DirectionalBias.DOWN,
            ):
                if memory.start_candidate_side is observed_side:
                    memory.start_candidate_bars += 1
                else:
                    memory.start_candidate_side = observed_side
                    memory.start_candidate_bars = 1
                reasons.append("DIRECTIONAL_START_CANDIDATE_PROGRESS")
                if (
                    memory.start_candidate_bars
                    >= self.directional_cfg.start_confirmation_bars
                ):
                    self._start_directional_episode(
                        memory,
                        observation,
                        observed_side,
                        events,
                    )
                    reasons.append("DIRECTIONAL_START_CONFIRMED")
            else:
                memory.start_candidate_side = DirectionalBias.UNKNOWN
                memory.start_candidate_bars = 0
                reasons.append("NO_DIRECTIONAL_START")
            memory.last_close = observation.close
            memory.last_observation_state = observation.observation_state
            memory.last_observation_state_time = observation.snapshot_time
            memory.last_reason_codes = tuple(reasons)
            return memory.last_reason_codes

        made_new_extreme = self._update_directional_extreme(memory, observation)
        self._update_protection(memory, observation)

        if observed_side is DirectionalBias.UNKNOWN:
            memory.inactive_bars += 1
        else:
            memory.inactive_bars = 0

        if memory.state is not DirectionalEpisodeState.REVERSAL_LEG:
            if observed_side is _opposite_direction(memory.direction):
                memory.opposite_control_bars += 1
            else:
                memory.opposite_control_bars = 0

        if memory.state is DirectionalEpisodeState.REVERSAL_LEG:
            reasons.extend(
                self._advance_reversal_leg(
                    memory,
                    observation,
                    events,
                )
            )

        elif memory.state is DirectionalEpisodeState.DIRECTIONAL:
            maturity_direction_aligned = bool(
                memory.origin_source
                is not DirectionalEpisodeOrigin.REVERSAL_EVENT_HANDOFF
                or observed_side is memory.direction
            )
            if self._parent_trend_restoration_required(memory, observation):
                self._emit_parent_trend_restoration(
                    memory,
                    observation,
                    events,
                )
                self._complete_directional(
                    memory,
                    observation,
                    events,
                    (
                        "ESTABLISHED_REVERSAL_LOST_PARENT_SIDE_CONTROL",
                        "PARENT_TREND_RESTORATION_COMPLETED_REVERSAL_EPISODE",
                    ),
                )
                reasons.append("PARENT_TREND_RESTORED_AFTER_ESTABLISHED_REVERSAL")
            elif self._maturity_observed(observation) and maturity_direction_aligned:
                self._transition_directional(
                    memory,
                    observation,
                    DirectionalEpisodeState.MATURE,
                    AuctionEventType.DIRECTIONAL_MATURED,
                    events,
                    ("DIRECTIONAL_MATURITY_OBSERVED",),
                )
                reasons.append("DIRECTIONAL_MATURITY_OBSERVED")
            elif self._directional_completion_required(memory):
                self._complete_directional(
                    memory,
                    observation,
                    events,
                    ("DIRECTIONAL_CONTEXT_COMPLETED",),
                )
                reasons.append("DIRECTIONAL_CONTEXT_COMPLETED")
            else:
                reasons.append("DIRECTIONAL_EPISODE_RETAINED")

        elif memory.state is DirectionalEpisodeState.MATURE:
            if self._parent_trend_restoration_required(memory, observation):
                self._emit_parent_trend_restoration(
                    memory,
                    observation,
                    events,
                )
                self._complete_directional(
                    memory,
                    observation,
                    events,
                    (
                        "MATURE_REVERSAL_LOST_PARENT_SIDE_CONTROL",
                        "PARENT_TREND_RESTORATION_COMPLETED_REVERSAL_EPISODE",
                    ),
                )
                reasons.append("PARENT_TREND_RESTORED_AFTER_MATURE_REVERSAL")
            elif self._reversal_watch_trigger(observation, memory.direction):
                self._clear_reversal_watch(memory)
                self._transition_directional(
                    memory,
                    observation,
                    DirectionalEpisodeState.REVERSAL_WATCH,
                    AuctionEventType.REVERSAL_WATCH_STARTED,
                    events,
                    self._reversal_watch_trigger_reasons(observation),
                )
                self._seed_reversal_evidence(
                    memory,
                    observation,
                    previous_close=previous_close,
                    made_new_extreme=made_new_extreme,
                )
                reasons.append("REVERSAL_WATCH_TRIGGERED")
            elif self._directional_completion_required(memory):
                self._complete_directional(
                    memory,
                    observation,
                    events,
                    ("MATURE_DIRECTIONAL_CONTEXT_COMPLETED",),
                )
                reasons.append("MATURE_DIRECTIONAL_CONTEXT_COMPLETED")
            else:
                reasons.append("MATURE_EPISODE_RETAINED")

        elif memory.state is DirectionalEpisodeState.REVERSAL_WATCH:
            memory.reversal_watch_age_bars += 1
            self._accumulate_reversal_evidence(
                memory,
                observation,
                previous_close=previous_close,
                made_new_extreme=made_new_extreme,
            )
            if self._reversal_confirmed(memory):
                original_direction = memory.direction
                reversal_side = _opposite_direction(original_direction)
                parent_episode_id = memory.episode_id
                if parent_episode_id is None:
                    raise ValueError("Reversal confirmation requires active episode identity")
                reversal_event = self._emit_directional_event(
                    memory,
                    observation,
                    AuctionEventType.DIRECTIONAL_REVERSAL_CONFIRMED,
                    events,
                    (
                        "FIRST_ADVERSE_STAGE_RETAINED",
                        "CONTINUATION_FAILURE_STAGE_RETAINED",
                        "EPISODE_REVERSAL_CONFIRMATION_LEVEL_BREACHED",
                    ),
                    event_direction=reversal_side,
                )
                if reversal_event is None:
                    raise ValueError(
                        "Fresh reversal confirmation must emit a transition event"
                    )
                source_confirmation_level = memory.reversal_confirmation_level
                source_confirmation_source = memory.reversal_confirmation_source
                source_confirmation_time = memory.reversal_confirmation_level_time
                self._complete_directional(
                    memory,
                    observation,
                    events,
                    ("REVERSAL_EVENT_COMPLETED_ORIGINAL_DIRECTIONAL_EPISODE",),
                )
                self._reset_completed_directional(memory)
                self._start_reversal_leg(
                    memory,
                    observation,
                    reversal_side,
                    events,
                    parent_episode_id=parent_episode_id,
                    origin_event_id=reversal_event.event_id,
                    source_confirmation_level=source_confirmation_level,
                    source_confirmation_source=source_confirmation_source,
                    source_confirmation_time=source_confirmation_time,
                )
                reasons.extend(
                    (
                        "DIRECTIONAL_REVERSAL_EVENT_EMITTED",
                        "REVERSAL_LEG_HANDOFF_STARTED",
                        f"HANDOFF_FROM_{original_direction.value}_TO_{reversal_side.value}",
                    )
                )
            elif self._trend_restored(memory, observation, made_new_extreme):
                self._emit_directional_event(
                    memory,
                    observation,
                    AuctionEventType.DIRECTIONAL_TREND_RESTORED,
                    events,
                    ("ORIGINAL_DIRECTION_REESTABLISHED_AFTER_WATCH",),
                    event_direction=memory.direction,
                )
                self._set_directional_state(
                    memory,
                    observation,
                    DirectionalEpisodeState.DIRECTIONAL,
                )
                self._clear_reversal_watch(memory)
                reasons.append("DIRECTIONAL_TREND_RESTORED_TO_DIRECTIONAL")
            elif memory.reversal_watch_age_bars >= self.directional_cfg.reversal_watch_max_bars:
                self._emit_directional_event(
                    memory,
                    observation,
                    AuctionEventType.DIRECTIONAL_TREND_RESTORED,
                    events,
                    ("REVERSAL_WATCH_EXPIRED_WITHOUT_CONFIRMATION",),
                    event_direction=memory.direction,
                )
                self._set_directional_state(
                    memory,
                    observation,
                    DirectionalEpisodeState.DIRECTIONAL,
                )
                self._clear_reversal_watch(memory)
                reasons.append("REVERSAL_WATCH_EXPIRED_TO_DIRECTIONAL")
            else:
                reasons.append("REVERSAL_WATCH_RETAINED")

        phase_reason = self._emit_directional_setup_phase_event(
            memory,
            observation,
            events,
            previous_observation_state=previous_observation_state,
        )
        if phase_reason is not None:
            reasons.append(phase_reason)

        memory.last_close = observation.close
        memory.last_observation_state = observation.observation_state
        memory.last_observation_state_time = observation.snapshot_time
        memory.last_reason_codes = tuple(reasons)
        return memory.last_reason_codes

    def _emit_directional_setup_phase_event(
        self,
        memory: _DirectionalMemory,
        observation: AuctionObservation,
        events: List[AuctionEvent],
        *,
        previous_observation_state: AuctionStateName,
    ) -> Optional[str]:
        """Emit one authoritative trend-resumption event for a completed pause.

        The objective observation provider owns pause classification.  The
        episode controller only attaches that confirmed observation transition
        to the active directional episode.  It does not evaluate setup quality.
        """
        if memory.state not in {
            DirectionalEpisodeState.DIRECTIONAL,
            DirectionalEpisodeState.MATURE,
        }:
            return None
        if observation.observation_state is not AuctionStateName.REACCELERATION:
            return None
        if self._observed_direction(observation) is not memory.direction:
            return None

        if previous_observation_state is AuctionStateName.CONTROLLED_PULLBACK:
            event_type = AuctionEventType.DIRECTIONAL_CONTINUATION_CONFIRMED
            reason_codes = (
                "CONTROLLED_PULLBACK_COMPLETED",
                "DIRECTIONAL_RESUMPTION_CONFIRMED",
            )
            reason = "DIRECTIONAL_CONTINUATION_CONFIRMED"
        elif previous_observation_state is AuctionStateName.RECOMPRESSION:
            event_type = AuctionEventType.DIRECTIONAL_REACCELERATION_CONFIRMED
            reason_codes = (
                "RECOMPRESSION_COMPLETED",
                "FRESH_DIRECTIONAL_EXPANSION_CONFIRMED",
            )
            reason = "DIRECTIONAL_REACCELERATION_CONFIRMED"
        else:
            return None

        self._emit_directional_event(
            memory,
            observation,
            event_type,
            events,
            reason_codes,
            event_direction=memory.direction,
            extra_data={
                "previous_observation_state": previous_observation_state.value,
                "observation_state": observation.observation_state.value,
                "previous_observation_state_time": (
                    memory.last_observation_state_time
                ),
            },
        )
        return reason

    def _advance_balance(
        self,
        memory: _BalanceMemory,
        observation: AuctionObservation,
        events: List[AuctionEvent],
    ) -> Tuple[str, ...]:
        reasons: List[str] = []
        if memory.state is not BalanceEpisodeState.NONE:
            memory.state_age_bars += 1

        if memory.state is BalanceEpisodeState.COMPLETED:
            self._reset_completed_balance(memory)

        if memory.state is BalanceEpisodeState.NONE:
            if self._accepted_range_can_form(observation):
                self._start_balance_episode(memory, observation, events)
                reasons.append("BALANCE_FORMING_STARTED")
            else:
                reasons.append("NO_QUALIFIED_ACCEPTED_RANGE")
            memory.last_reason_codes = tuple(reasons)
            return memory.last_reason_codes

        if memory.state in {
            BalanceEpisodeState.FORMING,
            BalanceEpisodeState.PROBABLE,
        }:
            merge_reason = self._merge_balance_candidate(memory, observation)
            if merge_reason is not None:
                reasons.append(merge_reason)

            classification, forming_reason = (
                self._advance_forming_balance_from_bar(memory, observation)
            )
            memory.forming_bars_observed += 1
            reasons.append(forming_reason)

            if classification == "CONTAINED":
                memory.containment_bars += 1
                memory.meaningful_escape_bars = 0
                memory.forming_invalid_bars = 0
                memory.candidate_last_valid_at = observation.snapshot_time
                reasons.append("BALANCE_CONTAINMENT_PROGRESS")
            elif classification == "MARGINAL":
                memory.marginal_excursion_bars += 1
                memory.meaningful_escape_bars = 0
                memory.forming_invalid_bars = 0
                reasons.append("BALANCE_FORMING_MARGINAL_INTERRUPTION")
            elif classification == "MEANINGFUL_ESCAPE":
                memory.meaningful_escape_bars += 1
                memory.forming_invalid_bars = memory.meaningful_escape_bars
                reasons.append("BALANCE_FORMING_MEANINGFUL_ESCAPE_PROGRESS")
            else:
                raise ValueError(
                    f"Unknown balance forming classification: {classification}"
                )

            if (
                memory.meaningful_escape_bars
                >= self.balance_cfg.forming_reset_bars
            ):
                self._set_balance_state(
                    memory,
                    observation,
                    BalanceEpisodeState.COMPLETED,
                )
                self._emit_balance_event(
                    memory,
                    observation,
                    AuctionEventType.BALANCE_COMPLETED,
                    events,
                    ("BALANCE_FORMING_INVALIDATED_BY_SUSTAINED_ESCAPE",),
                )
                reasons.append("BALANCE_FORMING_INVALIDATED")
            elif self._balance_lock_ready(memory):
                self._freeze_balance_candidate(memory)
                self._transition_balance(
                    memory,
                    observation,
                    BalanceEpisodeState.LOCKED,
                    AuctionEventType.BALANCE_LOCKED,
                    events,
                    (
                        "ACCUMULATED_BALANCE_OCCUPANCY_CONFIRMED",
                        "BALANCE_HYSTERESIS_LOCK_CONFIRMED",
                    ),
                )
                reasons.append("BALANCE_LOCK_CONFIRMED")
            elif (
                memory.state is BalanceEpisodeState.FORMING
                and self._balance_probable_ready(memory)
            ):
                self._transition_balance(
                    memory,
                    observation,
                    BalanceEpisodeState.PROBABLE,
                    AuctionEventType.BALANCE_PROBABLE,
                    events,
                    (
                        "ACCUMULATED_BALANCE_OCCUPANCY_PROBABLE",
                        "PROBABLE_BALANCE_REMAINS_DIAGNOSTIC_ONLY",
                    ),
                )
                reasons.append("BALANCE_PROBABLE_CONFIRMED")
            elif memory.state is BalanceEpisodeState.PROBABLE:
                reasons.append("PROBABLE_BALANCE_RETAINED")
            else:
                reasons.append("BALANCE_FORMING_RETAINED")

        elif memory.state is BalanceEpisodeState.LOCKED:
            escape_side = self._escape_side(memory, observation)
            if escape_side in (DirectionalBias.UP, DirectionalBias.DOWN):
                self._record_escape_attempt(memory, observation, escape_side)
                memory.escape_direction = escape_side
                memory.outside_close_count = 1
                memory.reentry_close_count = 0
                self._transition_balance(
                    memory,
                    observation,
                    BalanceEpisodeState.ESCAPE_WATCH,
                    AuctionEventType.BALANCE_ESCAPE_STARTED,
                    events,
                    (
                        "MEANINGFUL_CLOSE_OUTSIDE_FROZEN_BALANCE",
                        "BALANCE_ESCAPE_ATTEMPT_RECORDED",
                    ),
                    event_direction=escape_side,
                )
                reasons.append("BALANCE_ESCAPE_WATCH_STARTED")
                reasons.append("BALANCE_ESCAPE_ATTEMPT_RECORDED")
            else:
                memory.forming_bars_observed += 1
                memory.containment_bars += 1
                reasons.append("FROZEN_BALANCE_RETAINED")

        elif memory.state is BalanceEpisodeState.ESCAPE_WATCH:
            escape_side = self._escape_side(memory, observation)
            if escape_side is memory.escape_direction:
                memory.outside_close_count += 1
                memory.reentry_close_count = 0
                reasons.append("BALANCE_ESCAPE_ACCEPTANCE_PROGRESS")
                if (
                    memory.outside_close_count
                    >= self.balance_cfg.escape_acceptance_closes
                ):
                    self._transition_balance(
                        memory,
                        observation,
                        BalanceEpisodeState.ACCEPTED_OUTSIDE,
                        AuctionEventType.BALANCE_ESCAPE_ACCEPTED,
                        events,
                        ("OUTSIDE_ACCEPTANCE_CONFIRMED",),
                        event_direction=memory.escape_direction,
                    )
                    reasons.append("BALANCE_ESCAPE_ACCEPTED")
            elif self._inside_frozen_balance(memory, observation.close):
                memory.reentry_close_count += 1
                reasons.append("BALANCE_ESCAPE_REENTRY_PROGRESS")
                if (
                    memory.reentry_close_count
                    >= self.balance_cfg.failed_reentry_closes
                ):
                    self._record_failed_escape(memory, observation)
                    self._transition_balance(
                        memory,
                        observation,
                        BalanceEpisodeState.FAILED_BACK_INSIDE,
                        AuctionEventType.BALANCE_ESCAPE_FAILED,
                        events,
                        (
                            "ESCAPE_FAILED_BACK_INSIDE_FROZEN_BALANCE",
                            "BALANCE_REARM_REQUIRED_AFTER_FAILED_ESCAPE",
                        ),
                        event_direction=_opposite_direction(memory.escape_direction),
                    )
                    reasons.append("BALANCE_ESCAPE_FAILED")
                    reasons.append("BALANCE_REARM_REQUIRED")
                    if memory.attempt_limit_reached:
                        self._emit_balance_event(
                            memory,
                            observation,
                            AuctionEventType.BALANCE_ATTEMPT_LIMIT_REACHED,
                            events,
                            (
                                "BALANCE_ESCAPE_ATTEMPT_LIMIT_REACHED",
                                "NEW_ACCEPTED_RANGE_REQUIRED_BEFORE_NEXT_ESCAPE",
                            ),
                            event_direction=memory.escape_direction,
                        )
                        reasons.append("BALANCE_ESCAPE_ATTEMPT_LIMIT_REACHED")
            else:
                reasons.append("BALANCE_ESCAPE_WATCH_RETAINED")

        elif memory.state is BalanceEpisodeState.FAILED_BACK_INSIDE:
            reasons.extend(
                self._advance_failed_escape_rearm(
                    memory,
                    observation,
                    events,
                )
            )

        elif memory.state is BalanceEpisodeState.ACCEPTED_OUTSIDE:
            self._set_balance_state(
                memory,
                observation,
                BalanceEpisodeState.COMPLETED,
            )
            self._emit_balance_event(
                memory,
                observation,
                AuctionEventType.BALANCE_COMPLETED,
                events,
                ("ACCEPTED_ESCAPE_COMPLETED_BALANCE_EPISODE",),
                event_direction=memory.escape_direction,
            )
            reasons.append("ACCEPTED_BALANCE_EPISODE_COMPLETED")

        memory.last_reason_codes = tuple(reasons)
        return memory.last_reason_codes

    def _record_escape_attempt(
        self,
        memory: _BalanceMemory,
        observation: AuctionObservation,
        side: DirectionalBias,
    ) -> None:
        if side not in (DirectionalBias.UP, DirectionalBias.DOWN):
            raise ValueError("Balance escape attempt requires UP or DOWN direction")
        if memory.rearm_required:
            raise ValueError("Balance escape attempt cannot start while rearm is required")
        memory.escape_attempt_count += 1
        if side is DirectionalBias.UP:
            memory.up_escape_attempt_count += 1
        else:
            memory.down_escape_attempt_count += 1
        memory.last_escape_direction = side
        memory.last_escape_started_at = observation.snapshot_time
        memory.rearm_inside_close_count = 0
        memory.rearm_bars_elapsed = 0

    def _record_failed_escape(
        self,
        memory: _BalanceMemory,
        observation: AuctionObservation,
    ) -> None:
        if memory.escape_direction not in (DirectionalBias.UP, DirectionalBias.DOWN):
            raise ValueError("Failed escape requires an active escape direction")
        memory.failed_escape_count += 1
        memory.last_escape_failed_at = observation.snapshot_time
        memory.rearm_required = True
        memory.rearm_inside_close_count = 0
        memory.rearm_bars_elapsed = 0
        side_attempts = (
            memory.up_escape_attempt_count
            if memory.escape_direction is DirectionalBias.UP
            else memory.down_escape_attempt_count
        )
        memory.attempt_limit_reached = bool(
            memory.escape_attempt_count
            >= self.balance_cfg.max_escape_attempts_per_episode
            or side_attempts
            >= self.balance_cfg.max_same_side_escape_attempts
        )

    def _advance_failed_escape_rearm(
        self,
        memory: _BalanceMemory,
        observation: AuctionObservation,
        events: List[AuctionEvent],
    ) -> Tuple[str, ...]:
        if not memory.rearm_required:
            raise ValueError("FAILED_BACK_INSIDE balance must require rearm")

        reasons: List[str] = []
        memory.rearm_bars_elapsed += 1
        inside = self._inside_frozen_balance(memory, observation.close)
        if inside:
            memory.rearm_inside_close_count += 1
            memory.forming_bars_observed += 1
            memory.containment_bars += 1
            reasons.append("BALANCE_REARM_INSIDE_CONTAINMENT_PROGRESS")
        else:
            memory.rearm_inside_close_count = 0
            reasons.append("BALANCE_REARM_INTERRUPTED_BY_OUTSIDE_CLOSE")

        if memory.attempt_limit_reached:
            if self._materially_new_accepted_range_available(memory, observation):
                memory.rearm_required = False
                memory.attempt_limit_reached = False
                self._set_balance_state(
                    memory,
                    observation,
                    BalanceEpisodeState.COMPLETED,
                )
                self._emit_balance_event(
                    memory,
                    observation,
                    AuctionEventType.BALANCE_COMPLETED,
                    events,
                    (
                        "ESCAPE_ATTEMPT_LIMIT_RELEASED_BY_NEW_ACCEPTED_RANGE",
                        "OLD_BALANCE_EPISODE_COMPLETED_FOR_STRUCTURAL_RESET",
                    ),
                )
                reasons.append("BALANCE_ATTEMPT_LIMIT_RELEASED_BY_NEW_RANGE")
                return tuple(reasons)
            reasons.append("BALANCE_ATTEMPT_LIMIT_REQUIRES_NEW_RANGE")
            return tuple(reasons)

        rearm_ready = bool(
            memory.rearm_bars_elapsed
            >= self.balance_cfg.failed_escape_rearm_min_bars
            and memory.rearm_inside_close_count
            >= self.balance_cfg.failed_escape_rearm_inside_closes
        )
        if not rearm_ready:
            reasons.append("BALANCE_REARM_PENDING")
            return tuple(reasons)

        memory.rearm_required = False
        memory.attempt_limit_reached = False
        memory.escape_direction = DirectionalBias.UNKNOWN
        memory.outside_close_count = 0
        memory.reentry_close_count = 0
        memory.rearm_inside_close_count = 0
        memory.rearm_bars_elapsed = 0
        self._transition_balance(
            memory,
            observation,
            BalanceEpisodeState.LOCKED,
            AuctionEventType.BALANCE_REARMED,
            events,
            (
                "FAILED_ESCAPE_REARM_CONTAINMENT_CONFIRMED",
                "FROZEN_BALANCE_ELIGIBLE_FOR_NEW_ESCAPE",
            ),
        )
        reasons.append("FAILED_ESCAPE_REARMED_TO_LOCKED_BALANCE")
        return tuple(reasons)

    def _materially_new_accepted_range_available(
        self,
        memory: _BalanceMemory,
        observation: AuctionObservation,
    ) -> bool:
        if not self._accepted_range_can_form(observation):
            return False
        if observation.accepted_range_id == memory.range_id:
            return False
        if (
            memory.frozen_low is None
            or memory.frozen_high is None
            or observation.accepted_range_low is None
            or observation.accepted_range_high is None
        ):
            raise ValueError("New accepted-range comparison requires full geometry")
        overlap = self._range_overlap_ratio(
            memory.frozen_low,
            memory.frozen_high,
            observation.accepted_range_low,
            observation.accepted_range_high,
        )
        return overlap <= self.balance_cfg.attempt_limit_new_range_overlap_max


    def _start_directional_episode(
        self,
        memory: _DirectionalMemory,
        observation: AuctionObservation,
        side: DirectionalBias,
        events: List[AuctionEvent],
        *,
        origin_source: DirectionalEpisodeOrigin = (
            DirectionalEpisodeOrigin.OBSERVATION_CONFIRMATION
        ),
        parent_episode_id: Optional[str] = None,
        origin_event_id: Optional[str] = None,
    ) -> None:
        if origin_source is DirectionalEpisodeOrigin.REVERSAL_EVENT_HANDOFF:
            if parent_episode_id is None or origin_event_id is None:
                raise ValueError(
                    "Reversal-event handoff requires parent episode and event identity"
                )
        elif parent_episode_id is not None or origin_event_id is not None:
            raise ValueError(
                "Observation-confirmed directional start cannot carry reversal handoff linkage"
            )
        memory.sequence += 1
        memory.episode_id = self._episode_id(
            observation.symbol,
            observation.trading_day,
            "DIR",
            memory.sequence,
            observation.snapshot_time,
            side,
        )
        memory.state = DirectionalEpisodeState.DIRECTIONAL
        memory.direction = side
        memory.origin_source = origin_source
        memory.parent_episode_id = parent_episode_id
        memory.origin_event_id = origin_event_id
        memory.started_at = observation.snapshot_time
        memory.state_started_at = observation.snapshot_time
        memory.state_age_bars = 1
        memory.origin_price = observation.close
        memory.extreme_price = (
            observation.high if side is DirectionalBias.UP else observation.low
        )
        memory.extreme_time = observation.snapshot_time
        if origin_source is DirectionalEpisodeOrigin.REVERSAL_EVENT_HANDOFF:
            memory.protection_level = None
            memory.protection_source = ""
            memory.protection_time = None
            start_reasons = (
                "ATOMIC_REVERSAL_EVENT_DIRECTIONAL_HANDOFF",
                f"PARENT_EPISODE:{parent_episode_id}",
            )
        else:
            memory.protection_level = observation.trend_protection_level
            memory.protection_source = observation.trend_protection_source
            memory.protection_time = observation.trend_protection_time
            start_reasons = ("MULTIBAR_DIRECTIONAL_CONTEXT_ESTABLISHED",)
        memory.start_candidate_side = DirectionalBias.UNKNOWN
        memory.start_candidate_bars = 0
        memory.inactive_bars = 0
        memory.opposite_control_bars = 0
        memory.reversal_leg_progress_bars = 0
        memory.reversal_leg_failure_closes = 0
        memory.reversal_leg_progress_atr = 0.0
        memory.last_close = observation.close
        self._clear_reversal_watch(memory)
        self._emit_directional_event(
            memory,
            observation,
            AuctionEventType.DIRECTIONAL_STARTED,
            events,
            start_reasons,
            event_direction=side,
        )

    def _start_reversal_leg(
        self,
        memory: _DirectionalMemory,
        observation: AuctionObservation,
        side: DirectionalBias,
        events: List[AuctionEvent],
        *,
        parent_episode_id: str,
        origin_event_id: str,
        source_confirmation_level: Optional[float],
        source_confirmation_source: str,
        source_confirmation_time: Optional[datetime],
    ) -> None:
        # Preserve the source reversal boundary across the handoff.  The
        # reversal setup is evaluated only when the new leg establishes, which
        # can be several completed snapshots later.  Clearing this geometry at
        # handoff leaves the setup evaluator with only the leg origin and can
        # turn a valid high/low reversal into unusably tight stop geometry.

        memory.sequence += 1
        memory.episode_id = self._episode_id(
            observation.symbol,
            observation.trading_day,
            "DIR",
            memory.sequence,
            observation.snapshot_time,
            side,
        )
        memory.state = DirectionalEpisodeState.REVERSAL_LEG
        memory.direction = side
        memory.origin_source = DirectionalEpisodeOrigin.REVERSAL_EVENT_HANDOFF
        memory.parent_episode_id = parent_episode_id
        memory.origin_event_id = origin_event_id
        memory.started_at = observation.snapshot_time
        memory.state_started_at = observation.snapshot_time
        memory.state_age_bars = 1
        memory.origin_price = observation.close
        memory.extreme_price = (
            observation.high if side is DirectionalBias.UP else observation.low
        )
        memory.extreme_time = observation.snapshot_time
        memory.protection_level = None
        memory.protection_source = ""
        memory.protection_time = None
        memory.start_candidate_side = DirectionalBias.UNKNOWN
        memory.start_candidate_bars = 0
        memory.inactive_bars = 0
        memory.opposite_control_bars = 0
        memory.reversal_leg_progress_bars = 0
        memory.reversal_leg_failure_closes = 0
        memory.reversal_leg_progress_atr = 0.0
        memory.last_close = observation.close
        self._clear_reversal_watch(memory)
        memory.reversal_confirmation_level = source_confirmation_level
        memory.reversal_confirmation_source = source_confirmation_source
        memory.reversal_confirmation_level_time = source_confirmation_time
        self._emit_directional_event(
            memory,
            observation,
            AuctionEventType.DIRECTIONAL_REVERSAL_LEG_STARTED,
            events,
            (
                "REVERSAL_EVENT_CREATED_TRANSITIONAL_LEG",
                f"PARENT_EPISODE:{parent_episode_id}",
            ),
            event_direction=side,
        )

    def _advance_reversal_leg(
        self,
        memory: _DirectionalMemory,
        observation: AuctionObservation,
        events: List[AuctionEvent],
    ) -> Tuple[str, ...]:
        if memory.origin_price is None:
            raise ValueError("REVERSAL_LEG requires origin price")
        if memory.direction not in (DirectionalBias.UP, DirectionalBias.DOWN):
            raise ValueError("REVERSAL_LEG requires a directional side")

        if memory.direction is DirectionalBias.UP:
            progress_points = observation.close - memory.origin_price
            price_reaccepted_parent_side = observation.close < memory.origin_price
        else:
            progress_points = memory.origin_price - observation.close
            price_reaccepted_parent_side = observation.close > memory.origin_price
        memory.reversal_leg_progress_atr = max(0.0, progress_points / observation.atr)

        # Establishment requires the leg to reach the configured displacement
        # once and then retain the reversal side of the handoff origin.  The
        # old implementation required every confirming close to remain beyond
        # the full displacement threshold, so a shallow pause after genuine
        # progress reset proof to zero.
        reached_minimum_progress = bool(
            memory.reversal_leg_progress_atr
            >= self.directional_cfg.reversal_leg_min_progress_atr
        )
        retained_reversal_side = bool(
            progress_points >= 0.0
            and memory.reversal_leg_progress_bars > 0
        )
        if reached_minimum_progress or retained_reversal_side:
            memory.reversal_leg_progress_bars += 1
        else:
            memory.reversal_leg_progress_bars = 0

        # A close through the handoff origin is not sufficient to declare the
        # leg failed while the objective trend evidence still supports the new
        # side.  This prevents stale observation-state continuity from
        # cancelling a reversal that current trend evidence continues to prove.
        trend_still_supports_reversal = bool(
            observation.trend_direction is memory.direction
        )
        failed_now = bool(
            price_reaccepted_parent_side
            and not trend_still_supports_reversal
        )
        if failed_now:
            memory.reversal_leg_failure_closes += 1
        else:
            memory.reversal_leg_failure_closes = 0

        if (
            memory.reversal_leg_failure_closes
            >= self.directional_cfg.reversal_leg_failure_closes
        ):
            self._emit_directional_event(
                memory,
                observation,
                AuctionEventType.DIRECTIONAL_REVERSAL_LEG_FAILED,
                events,
                (
                    "REVERSAL_LEG_REACCEPTED_BEYOND_HANDOFF_ORIGIN",
                    "REVERSAL_LEG_FAILED_BEFORE_DIRECTION_ESTABLISHED",
                ),
                event_direction=_opposite_direction(memory.direction),
            )
            self._complete_directional(
                memory,
                observation,
                events,
                ("FAILED_REVERSAL_LEG_COMPLETED",),
            )
            return (
                "REVERSAL_LEG_FAILURE_PROGRESS",
                "REVERSAL_LEG_FAILED",
            )

        if (
            memory.reversal_leg_progress_bars
            >= self.directional_cfg.reversal_leg_establishment_closes
        ):
            self._set_directional_state(
                memory,
                observation,
                DirectionalEpisodeState.DIRECTIONAL,
            )
            self._emit_directional_event(
                memory,
                observation,
                AuctionEventType.DIRECTIONAL_REVERSAL_LEG_ESTABLISHED,
                events,
                (
                    "REVERSAL_LEG_SUSTAINED_DIRECTIONAL_PROGRESS",
                    "OPPOSITE_DIRECTION_ESTABLISHED_AFTER_REVERSAL",
                ),
                event_direction=memory.direction,
            )
            return (
                "REVERSAL_LEG_PROGRESS_CONFIRMED",
                "REVERSAL_LEG_PROMOTED_TO_DIRECTIONAL",
            )

        if failed_now:
            return ("REVERSAL_LEG_FAILURE_PROGRESS",)
        if memory.reversal_leg_progress_atr > 0.0:
            return ("REVERSAL_LEG_DIRECTIONAL_PROGRESS",)
        return ("REVERSAL_LEG_RETAINED_WITHOUT_ESTABLISHMENT",)

    def _start_balance_episode(
        self,
        memory: _BalanceMemory,
        observation: AuctionObservation,
        events: List[AuctionEvent],
    ) -> None:
        if (
            observation.accepted_range_id is None
            or observation.accepted_range_low is None
            or observation.accepted_range_high is None
        ):
            raise ValueError("Qualified accepted range requires identity and geometry")
        memory.sequence += 1
        memory.episode_id = self._episode_id(
            observation.symbol,
            observation.trading_day,
            "BAL",
            memory.sequence,
            observation.snapshot_time,
            DirectionalBias.NEUTRAL,
        )
        memory.state = BalanceEpisodeState.FORMING
        memory.started_at = observation.snapshot_time
        memory.state_started_at = observation.snapshot_time
        memory.state_age_bars = 1
        memory.range_id = None
        memory.candidate_low = observation.accepted_range_low
        memory.candidate_high = observation.accepted_range_high
        memory.source_range_ids = [observation.accepted_range_id]
        memory.candidate_merge_count = 0
        memory.candidate_bar_expansion_count = 0
        memory.candidate_last_valid_at = observation.snapshot_time
        memory.frozen_low = None
        memory.frozen_high = None
        memory.containment_bars = 1
        memory.forming_bars_observed = 1
        memory.marginal_excursion_bars = 0
        memory.meaningful_escape_bars = 0
        memory.forming_invalid_bars = 0
        memory.escape_direction = DirectionalBias.UNKNOWN
        memory.outside_close_count = 0
        memory.reentry_close_count = 0
        memory.escape_attempt_count = 0
        memory.failed_escape_count = 0
        memory.up_escape_attempt_count = 0
        memory.down_escape_attempt_count = 0
        memory.last_escape_direction = DirectionalBias.UNKNOWN
        memory.last_escape_started_at = None
        memory.last_escape_failed_at = None
        memory.rearm_required = False
        memory.rearm_inside_close_count = 0
        memory.rearm_bars_elapsed = 0
        memory.attempt_limit_reached = False
        self._emit_balance_event(
            memory,
            observation,
            AuctionEventType.BALANCE_FORMING_STARTED,
            events,
            ("QUALIFIED_ACCEPTED_RANGE_OBSERVED",),
        )

    def _transition_directional(
        self,
        memory: _DirectionalMemory,
        observation: AuctionObservation,
        state: DirectionalEpisodeState,
        event_type: AuctionEventType,
        events: List[AuctionEvent],
        reason_codes: Tuple[str, ...],
        *,
        event_direction: Optional[DirectionalBias] = None,
    ) -> None:
        self._set_directional_state(memory, observation, state)
        self._emit_directional_event(
            memory,
            observation,
            event_type,
            events,
            reason_codes,
            event_direction=event_direction,
        )

    @staticmethod
    def _set_directional_state(
        memory: _DirectionalMemory,
        observation: AuctionObservation,
        state: DirectionalEpisodeState,
    ) -> None:
        memory.state = state
        memory.state_started_at = observation.snapshot_time
        memory.state_age_bars = 1
        if state is DirectionalEpisodeState.REVERSAL_WATCH:
            memory.reversal_watch_age_bars = 1

    def _transition_balance(
        self,
        memory: _BalanceMemory,
        observation: AuctionObservation,
        state: BalanceEpisodeState,
        event_type: AuctionEventType,
        events: List[AuctionEvent],
        reason_codes: Tuple[str, ...],
        *,
        event_direction: DirectionalBias = DirectionalBias.UNKNOWN,
    ) -> None:
        self._set_balance_state(memory, observation, state)
        self._emit_balance_event(
            memory,
            observation,
            event_type,
            events,
            reason_codes,
            event_direction=event_direction,
        )

    @staticmethod
    def _set_balance_state(
        memory: _BalanceMemory,
        observation: AuctionObservation,
        state: BalanceEpisodeState,
    ) -> None:
        memory.state = state
        memory.state_started_at = observation.snapshot_time
        memory.state_age_bars = 1

    def _complete_directional(
        self,
        memory: _DirectionalMemory,
        observation: AuctionObservation,
        events: List[AuctionEvent],
        reason_codes: Tuple[str, ...],
    ) -> None:
        self._set_directional_state(
            memory,
            observation,
            DirectionalEpisodeState.COMPLETED,
        )
        self._emit_directional_event(
            memory,
            observation,
            AuctionEventType.DIRECTIONAL_COMPLETED,
            events,
            reason_codes,
            event_direction=memory.direction,
        )

    def _emit_directional_event(
        self,
        memory: _DirectionalMemory,
        observation: AuctionObservation,
        event_type: AuctionEventType,
        events: List[AuctionEvent],
        reason_codes: Tuple[str, ...],
        *,
        event_direction: Optional[DirectionalBias] = None,
        extra_data: Optional[Mapping[str, Any]] = None,
    ) -> Optional[AuctionEvent]:
        if memory.episode_id is None:
            raise ValueError("Directional event requires episode_id")
        event_id = self._event_id(memory.episode_id, event_type, observation.snapshot_time)
        if event_id in memory.emitted_event_ids:
            return None
        memory.emitted_event_ids.add(event_id)
        event_data: Dict[str, Any] = {
            "origin_price": memory.origin_price,
            "origin_source": memory.origin_source.value,
            "parent_episode_id": memory.parent_episode_id,
            "origin_event_id": memory.origin_event_id,
            "extreme_price": memory.extreme_price,
            "protection_level": memory.protection_level,
            "protection_source": memory.protection_source,
            "reversal_confirmation_level": memory.reversal_confirmation_level,
            "reversal_confirmation_source": memory.reversal_confirmation_source,
            "reversal_confirmation_level_time": (
                memory.reversal_confirmation_level_time
            ),
            "first_adverse_bar_time": memory.first_adverse_bar_time,
            "first_adverse_bar_level": memory.first_adverse_bar_level,
            "first_adverse_bar_close": memory.first_adverse_bar_close,
            "continuation_failure_time": memory.continuation_failure_seen_at,
            "reversal_leg_progress_bars": memory.reversal_leg_progress_bars,
            "reversal_leg_failure_closes": memory.reversal_leg_failure_closes,
            "reversal_leg_progress_atr": memory.reversal_leg_progress_atr,
        }
        if extra_data:
            event_data.update(dict(extra_data))
        event = AuctionEvent(
            event_id=event_id,
            event_type=event_type,
            episode_id=memory.episode_id,
            symbol=observation.symbol,
            trading_day=observation.trading_day,
            event_time=observation.snapshot_time,
            direction=(
                event_direction
                if event_direction is not None
                else memory.direction
            ),
            reason_codes=reason_codes,
            data=event_data,
        )
        events.append(event)
        return event

    def _emit_balance_event(
        self,
        memory: _BalanceMemory,
        observation: AuctionObservation,
        event_type: AuctionEventType,
        events: List[AuctionEvent],
        reason_codes: Tuple[str, ...],
        *,
        event_direction: DirectionalBias = DirectionalBias.UNKNOWN,
    ) -> None:
        if memory.episode_id is None:
            raise ValueError("Balance event requires episode_id")
        event_id = self._event_id(memory.episode_id, event_type, observation.snapshot_time)
        if event_id in memory.emitted_event_ids:
            return
        memory.emitted_event_ids.add(event_id)
        events.append(
            AuctionEvent(
                event_id=event_id,
                event_type=event_type,
                episode_id=memory.episode_id,
                symbol=observation.symbol,
                trading_day=observation.trading_day,
                event_time=observation.snapshot_time,
                direction=event_direction,
                reason_codes=reason_codes,
                data={
                    "range_id": memory.range_id,
                    "candidate_low": memory.candidate_low,
                    "candidate_high": memory.candidate_high,
                    "source_range_ids": tuple(memory.source_range_ids),
                    "candidate_merge_count": memory.candidate_merge_count,
                    "candidate_bar_expansion_count": (
                        memory.candidate_bar_expansion_count
                    ),
                    "candidate_last_valid_at": memory.candidate_last_valid_at,
                    "frozen_low": memory.frozen_low,
                    "frozen_high": memory.frozen_high,
                    "containment_bars": memory.containment_bars,
                    "forming_bars_observed": memory.forming_bars_observed,
                    "marginal_excursion_bars": memory.marginal_excursion_bars,
                    "meaningful_escape_bars": memory.meaningful_escape_bars,
                    "containment_ratio": self._balance_containment_ratio(memory),
                    "escape_attempt_count": memory.escape_attempt_count,
                    "failed_escape_count": memory.failed_escape_count,
                    "up_escape_attempt_count": memory.up_escape_attempt_count,
                    "down_escape_attempt_count": memory.down_escape_attempt_count,
                    "last_escape_direction": memory.last_escape_direction.value,
                    "last_escape_started_at": memory.last_escape_started_at,
                    "last_escape_failed_at": memory.last_escape_failed_at,
                    "rearm_required": memory.rearm_required,
                    "rearm_inside_close_count": memory.rearm_inside_close_count,
                    "rearm_bars_elapsed": memory.rearm_bars_elapsed,
                    "attempt_limit_reached": memory.attempt_limit_reached,
                },
            )
        )

    def _seed_reversal_evidence(
        self,
        memory: _DirectionalMemory,
        observation: AuctionObservation,
        *,
        previous_close: Optional[float],
        made_new_extreme: bool,
    ) -> None:
        self._accumulate_reversal_evidence(
            memory,
            observation,
            previous_close=previous_close,
            made_new_extreme=made_new_extreme,
        )

    def _accumulate_reversal_evidence(
        self,
        memory: _DirectionalMemory,
        observation: AuctionObservation,
        *,
        previous_close: Optional[float],
        made_new_extreme: bool,
    ) -> None:
        if made_new_extreme and memory.first_adverse_bar_time is not None:
            self._clear_reversal_stages_after_fresh_extreme(memory)

        rejection_observation_now = bool(
            observation.rejection_observed
            or observation.failed_extreme_observed
        )
        adverse_bar_now = self._first_adverse_bar_observed(
            memory,
            observation,
            previous_close=previous_close,
        )
        if memory.first_adverse_bar_time is None and (
            rejection_observation_now or adverse_bar_now
        ):
            source = (
                "OBSERVATION_REJECTION_OR_FAILED_EXTREME"
                if rejection_observation_now
                else "EPISODE_PRICE_ACTION_FIRST_ADVERSE_BAR"
            )
            self._set_first_adverse_stage(
                memory,
                observation,
                source=source,
            )

        failure_observation_now = bool(
            observation.structural_failure_confirmed
            or observation.observation_state
            in self.directional_cfg.reversal_watch_observation_states
        )
        price_action_failure_now = self._price_action_continuation_failure_observed(
            memory,
            observation,
            made_new_extreme=made_new_extreme,
        )
        if failure_observation_now:
            memory.continuation_failure_progress_bars = (
                self.directional_cfg.reversal_continuation_failure_bars
            )
        elif price_action_failure_now:
            memory.continuation_failure_progress_bars += 1
        elif not memory.continuation_failure_seen:
            memory.continuation_failure_progress_bars = 0

        if (
            not memory.continuation_failure_seen
            and memory.continuation_failure_progress_bars
            >= self.directional_cfg.reversal_continuation_failure_bars
        ):
            memory.continuation_failure_seen = True
            memory.continuation_failure_seen_at = observation.snapshot_time

        level = memory.reversal_confirmation_level
        level_time = memory.reversal_confirmation_level_time
        if level is None or level_time is None:
            memory.reversal_confirmation_breach_closes = 0
            return
        # The candle that establishes the transition boundary cannot confirm it.
        if observation.snapshot_time <= level_time:
            memory.reversal_confirmation_breach_closes = 0
            return

        tolerance = (
            observation.atr
            * self.directional_cfg.reversal_confirmation_level_tolerance_atr
        )
        if memory.direction is DirectionalBias.UP:
            breached = observation.close < (level - tolerance)
        else:
            breached = observation.close > (level + tolerance)
        if breached:
            memory.reversal_confirmation_breach_closes += 1
        else:
            memory.reversal_confirmation_breach_closes = 0

    def _first_adverse_bar_observed(
        self,
        memory: _DirectionalMemory,
        observation: AuctionObservation,
        *,
        previous_close: Optional[float],
    ) -> bool:
        if previous_close is None:
            return False
        minimum_move = (
            observation.atr * self.directional_cfg.reversal_first_adverse_min_close_atr
        )
        if memory.direction is DirectionalBias.UP:
            return observation.close < (previous_close - minimum_move)
        if memory.direction is DirectionalBias.DOWN:
            return observation.close > (previous_close + minimum_move)
        raise ValueError("First adverse bar requires directional episode")

    @staticmethod
    def _price_action_continuation_failure_observed(
        memory: _DirectionalMemory,
        observation: AuctionObservation,
        *,
        made_new_extreme: bool,
    ) -> bool:
        if (
            memory.first_adverse_bar_time is None
            or memory.first_adverse_bar_close is None
            or observation.snapshot_time < memory.first_adverse_bar_time
        ):
            return False
        if memory.direction is DirectionalBias.UP:
            return observation.close <= memory.first_adverse_bar_close
        if memory.direction is DirectionalBias.DOWN:
            return observation.close >= memory.first_adverse_bar_close
        raise ValueError("Continuation failure requires directional episode")

    @staticmethod
    def _set_first_adverse_stage(
        memory: _DirectionalMemory,
        observation: AuctionObservation,
        *,
        source: str,
    ) -> None:
        if memory.direction is DirectionalBias.UP:
            level = observation.low
            level_source = f"{source}:BAR_LOW"
        elif memory.direction is DirectionalBias.DOWN:
            level = observation.high
            level_source = f"{source}:BAR_HIGH"
        else:
            raise ValueError("First adverse stage requires directional episode")
        memory.first_adverse_bar_time = observation.snapshot_time
        memory.first_adverse_bar_level = level
        memory.first_adverse_bar_close = observation.close
        memory.rejection_seen = True
        memory.rejection_seen_at = observation.snapshot_time
        memory.reversal_confirmation_level = level
        memory.reversal_confirmation_source = level_source
        memory.reversal_confirmation_level_time = observation.snapshot_time
        memory.reversal_confirmation_breach_closes = 0

    @staticmethod
    def _clear_reversal_stages_after_fresh_extreme(
        memory: _DirectionalMemory,
    ) -> None:
        memory.rejection_seen = False
        memory.rejection_seen_at = None
        memory.continuation_failure_seen = False
        memory.continuation_failure_seen_at = None
        memory.continuation_failure_progress_bars = 0
        memory.first_adverse_bar_time = None
        memory.first_adverse_bar_level = None
        memory.first_adverse_bar_close = None
        memory.reversal_confirmation_level = None
        memory.reversal_confirmation_source = ""
        memory.reversal_confirmation_level_time = None
        memory.reversal_confirmation_breach_closes = 0

    def _reversal_confirmed(self, memory: _DirectionalMemory) -> bool:
        if memory.reversal_confirmation_level is None:
            return False
        if self.directional_cfg.reversal_require_rejection and not memory.rejection_seen:
            return False
        if (
            self.directional_cfg.reversal_require_continuation_failure
            and not memory.continuation_failure_seen
        ):
            return False
        return (
            memory.reversal_confirmation_breach_closes
            >= self.directional_cfg.reversal_confirmation_closes
        )

    def _trend_restored(
        self,
        memory: _DirectionalMemory,
        observation: AuctionObservation,
        made_new_extreme: bool,
    ) -> bool:
        same_side_state = (
            observation.observation_state
            in self.directional_cfg.up_observation_states
            if memory.direction is DirectionalBias.UP
            else observation.observation_state
            in self.directional_cfg.down_observation_states
        )
        if made_new_extreme and same_side_state and not observation.structural_failure_confirmed:
            memory.trend_restore_bars += 1
        else:
            memory.trend_restore_bars = 0
        return (
            memory.trend_restore_bars
            >= self.directional_cfg.trend_restoration_confirmation_bars
        )

    def _parent_trend_restoration_required(
        self,
        memory: _DirectionalMemory,
        observation: AuctionObservation,
    ) -> bool:
        """Return True when an established reversal loses control to its parent side.

        This is intentionally narrower than a generic directional restart.  It
        applies only to a reversal-event handoff that already established and
        then accumulated the configured opposite-control closes.
        """
        if (
            memory.origin_source
            is not DirectionalEpisodeOrigin.REVERSAL_EVENT_HANDOFF
            or memory.parent_episode_id is None
            or memory.direction
            not in (DirectionalBias.UP, DirectionalBias.DOWN)
        ):
            return False
        if (
            memory.opposite_control_bars
            < self.directional_cfg.opposite_completion_bars
        ):
            return False
        parent_side = _opposite_direction(memory.direction)
        if self._observed_direction(observation) is not parent_side:
            return False
        protection = observation.trend_protection_level
        if protection is None:
            return False
        if parent_side is DirectionalBias.UP:
            return protection < observation.close
        return protection > observation.close

    def _emit_parent_trend_restoration(
        self,
        memory: _DirectionalMemory,
        observation: AuctionObservation,
        events: List[AuctionEvent],
    ) -> None:
        """Emit a creation-capable parent-trend restoration transition.

        Setup geometry is sourced from the current objective parent-side
        protection level.  No synthetic stop or compatibility fallback is
        introduced; absent or invalid geometry is left for the setup evaluator
        to reject explicitly.
        """
        if memory.parent_episode_id is None or memory.episode_id is None:
            raise ValueError(
                "Parent-trend restoration requires child and parent episode identity"
            )
        parent_side = _opposite_direction(memory.direction)
        self._emit_directional_event(
            memory,
            observation,
            AuctionEventType.DIRECTIONAL_TREND_RESTORED,
            events,
            (
                "ESTABLISHED_REVERSAL_LOST_OPPOSITE_CONTROL",
                "PARENT_DIRECTION_REESTABLISHED_AFTER_REVERSAL_HANDOFF",
            ),
            event_direction=parent_side,
            extra_data={
                "origin_price": observation.close,
                "protection_level": observation.trend_protection_level,
                "protection_source": observation.trend_protection_source,
                "restored_parent_episode_id": memory.parent_episode_id,
                "completed_reversal_episode_id": memory.episode_id,
                "completed_reversal_extreme_price": memory.extreme_price,
                "exhaustion_was_active": observation.exhaustion_active,
                "exhausted_side_before_restoration": (
                    observation.exhausted_side.value
                    if observation.exhaustion_active
                    else DirectionalBias.UNKNOWN.value
                ),
                "exhaustion_resolution": (
                    "PARENT_TREND_RESTORED_AFTER_ESTABLISHED_REVERSAL"
                ),
            },
        )

    def _directional_completion_required(self, memory: _DirectionalMemory) -> bool:
        return bool(
            memory.opposite_control_bars
            >= self.directional_cfg.opposite_completion_bars
            or memory.inactive_bars
            >= self.directional_cfg.inactive_completion_bars
        )

    def _update_directional_extreme(
        self,
        memory: _DirectionalMemory,
        observation: AuctionObservation,
    ) -> bool:
        if memory.extreme_price is None:
            memory.extreme_price = (
                observation.high
                if memory.direction is DirectionalBias.UP
                else observation.low
            )
            memory.extreme_time = observation.snapshot_time
            return True
        if memory.direction is DirectionalBias.UP and observation.high > memory.extreme_price:
            memory.extreme_price = observation.high
            memory.extreme_time = observation.snapshot_time
            return True
        if memory.direction is DirectionalBias.DOWN and observation.low < memory.extreme_price:
            memory.extreme_price = observation.low
            memory.extreme_time = observation.snapshot_time
            return True
        return False

    def _update_protection(
        self,
        memory: _DirectionalMemory,
        observation: AuctionObservation,
    ) -> None:
        level = observation.trend_protection_level
        if level is None:
            return
        if self._observed_direction(observation) is not memory.direction:
            return
        if memory.protection_level is None:
            accept = True
        elif memory.direction is DirectionalBias.UP:
            accept = level >= memory.protection_level
        else:
            accept = level <= memory.protection_level
        if not accept:
            return
        memory.protection_level = level
        memory.protection_source = observation.trend_protection_source
        memory.protection_time = observation.trend_protection_time

    @staticmethod
    def _reset_completed_directional(memory: _DirectionalMemory) -> None:
        memory.episode_id = None
        memory.state = DirectionalEpisodeState.NONE
        memory.direction = DirectionalBias.UNKNOWN
        memory.origin_source = DirectionalEpisodeOrigin.NONE
        memory.parent_episode_id = None
        memory.origin_event_id = None
        memory.started_at = None
        memory.state_started_at = None
        memory.state_age_bars = 0
        memory.origin_price = None
        memory.extreme_price = None
        memory.extreme_time = None
        memory.protection_level = None
        memory.protection_source = ""
        memory.protection_time = None
        memory.start_candidate_side = DirectionalBias.UNKNOWN
        memory.start_candidate_bars = 0
        memory.opposite_control_bars = 0
        memory.inactive_bars = 0
        memory.reversal_leg_progress_bars = 0
        memory.reversal_leg_failure_closes = 0
        memory.reversal_leg_progress_atr = 0.0
        memory.last_close = None
        memory.last_observation_state = AuctionStateName.UNKNOWN
        memory.last_observation_state_time = None
        PersistentEpisodeEngine._clear_reversal_watch(memory)

    @staticmethod
    def _reset_completed_balance(memory: _BalanceMemory) -> None:
        memory.episode_id = None
        memory.state = BalanceEpisodeState.NONE
        memory.started_at = None
        memory.state_started_at = None
        memory.state_age_bars = 0
        memory.range_id = None
        memory.candidate_low = None
        memory.candidate_high = None
        memory.source_range_ids = []
        memory.candidate_merge_count = 0
        memory.candidate_bar_expansion_count = 0
        memory.candidate_last_valid_at = None
        memory.frozen_low = None
        memory.frozen_high = None
        memory.containment_bars = 0
        memory.forming_bars_observed = 0
        memory.marginal_excursion_bars = 0
        memory.meaningful_escape_bars = 0
        memory.forming_invalid_bars = 0
        memory.escape_direction = DirectionalBias.UNKNOWN
        memory.outside_close_count = 0
        memory.reentry_close_count = 0
        memory.escape_attempt_count = 0
        memory.failed_escape_count = 0
        memory.up_escape_attempt_count = 0
        memory.down_escape_attempt_count = 0
        memory.last_escape_direction = DirectionalBias.UNKNOWN
        memory.last_escape_started_at = None
        memory.last_escape_failed_at = None
        memory.rearm_required = False
        memory.rearm_inside_close_count = 0
        memory.rearm_bars_elapsed = 0
        memory.attempt_limit_reached = False

    @staticmethod
    def _clear_reversal_watch(memory: _DirectionalMemory) -> None:
        memory.rejection_seen = False
        memory.rejection_seen_at = None
        memory.continuation_failure_seen = False
        memory.continuation_failure_seen_at = None
        memory.continuation_failure_progress_bars = 0
        memory.first_adverse_bar_time = None
        memory.first_adverse_bar_level = None
        memory.first_adverse_bar_close = None
        memory.reversal_confirmation_level = None
        memory.reversal_confirmation_source = ""
        memory.reversal_confirmation_level_time = None
        memory.reversal_confirmation_breach_closes = 0
        memory.reversal_watch_age_bars = 0
        memory.trend_restore_bars = 0

    def _accepted_range_can_form(self, observation: AuctionObservation) -> bool:
        if not self._accepted_range_source_qualifies(observation):
            return False
        if not observation.accepted_range_inside:
            return False
        if observation.range_width_atr is None:
            return False
        return observation.range_width_atr <= self.balance_cfg.range_width_atr_max

    def _accepted_range_source_qualifies(
        self,
        observation: AuctionObservation,
    ) -> bool:
        if (
            observation.accepted_range_id is None
            or observation.accepted_range_low is None
            or observation.accepted_range_high is None
        ):
            return False
        if (
            self.balance_cfg.require_non_provisional_source_range
            and observation.accepted_range_provisional
        ):
            return False
        if (
            self.balance_cfg.require_breakout_eligible_source_range
            and not observation.accepted_range_breakout_eligible
        ):
            return False
        return True

    def _merge_balance_candidate(
        self,
        memory: _BalanceMemory,
        observation: AuctionObservation,
    ) -> Optional[str]:
        if not self._accepted_range_source_qualifies(observation):
            return None
        if (
            memory.candidate_low is None
            or memory.candidate_high is None
            or observation.accepted_range_low is None
            or observation.accepted_range_high is None
            or observation.accepted_range_id is None
        ):
            raise ValueError("FORMING balance requires candidate and source geometry")
        overlap = self._range_overlap_ratio(
            memory.candidate_low,
            memory.candidate_high,
            observation.accepted_range_low,
            observation.accepted_range_high,
        )
        if overlap < self.balance_cfg.candidate_merge_overlap_min:
            return "BALANCE_SOURCE_RANGE_INCOMPATIBLE_WITH_CANDIDATE"
        merged_low = min(memory.candidate_low, observation.accepted_range_low)
        merged_high = max(memory.candidate_high, observation.accepted_range_high)
        merged_width_atr = (merged_high - merged_low) / observation.atr
        if merged_width_atr > self.balance_cfg.range_width_atr_max:
            return "BALANCE_CANDIDATE_EXPANSION_REJECTED_TOO_WIDE"
        changed = bool(
            merged_low != memory.candidate_low
            or merged_high != memory.candidate_high
        )
        new_source = observation.accepted_range_id not in memory.source_range_ids
        if new_source:
            memory.source_range_ids.append(observation.accepted_range_id)
        if changed:
            memory.candidate_low = merged_low
            memory.candidate_high = merged_high
            memory.candidate_merge_count += 1
            return "BALANCE_CANDIDATE_GEOMETRY_EXPANDED"
        if new_source:
            memory.candidate_merge_count += 1
            return "BALANCE_SOURCE_RANGE_MERGED"
        return "BALANCE_SOURCE_RANGE_RETAINED"

    @staticmethod
    def _range_overlap_ratio(
        first_low: float,
        first_high: float,
        second_low: float,
        second_high: float,
    ) -> float:
        intersection = max(
            0.0,
            min(first_high, second_high) - max(first_low, second_low),
        )
        union = max(first_high, second_high) - min(first_low, second_low)
        if union <= 0.0:
            raise ValueError("Balance range union must be positive")
        return intersection / union

    def _advance_forming_balance_from_bar(
        self,
        memory: _BalanceMemory,
        observation: AuctionObservation,
    ) -> Tuple[str, str]:
        if memory.candidate_low is None or memory.candidate_high is None:
            raise ValueError("Balance forming requires candidate geometry")
        if observation.directional_efficiency is None:
            return "MARGINAL", "BALANCE_FORMING_MISSING_DIRECTIONAL_EFFICIENCY"
        if observation.overlap_ratio is None:
            return "MARGINAL", "BALANCE_FORMING_MISSING_OVERLAP_RATIO"

        tolerance = (
            observation.atr * self.balance_cfg.forming_excursion_tolerance_atr
        )
        if observation.close < (memory.candidate_low - tolerance):
            return "MEANINGFUL_ESCAPE", "BALANCE_FORMING_MEANINGFUL_DOWNSIDE_ESCAPE"
        if observation.close > (memory.candidate_high + tolerance):
            return "MEANINGFUL_ESCAPE", "BALANCE_FORMING_MEANINGFUL_UPSIDE_ESCAPE"

        if observation.directional_efficiency > self.balance_cfg.efficiency_max:
            return "MARGINAL", "BALANCE_FORMING_DIRECTIONAL_EFFICIENCY_TOO_HIGH"
        if observation.overlap_ratio < self.balance_cfg.overlap_min:
            return "MARGINAL", "BALANCE_FORMING_OVERLAP_TOO_LOW"

        expanded_low = min(memory.candidate_low, observation.low)
        expanded_high = max(memory.candidate_high, observation.high)
        expanded_width_atr = (expanded_high - expanded_low) / observation.atr
        changed = bool(
            expanded_low != memory.candidate_low
            or expanded_high != memory.candidate_high
        )
        if changed:
            if expanded_width_atr > self.balance_cfg.range_width_atr_max:
                return "MARGINAL", "BALANCE_FORMING_BAR_EXPANSION_TOO_WIDE"
            memory.candidate_low = expanded_low
            memory.candidate_high = expanded_high
            memory.candidate_bar_expansion_count += 1
            return "CONTAINED", "BALANCE_CANDIDATE_EXPANDED_FROM_BAR_GEOMETRY"
        return "CONTAINED", "BALANCE_BAR_CONTAINED_IN_CANDIDATE"

    @staticmethod
    def _balance_containment_ratio(memory: _BalanceMemory) -> float:
        if memory.forming_bars_observed <= 0:
            return 0.0
        return memory.containment_bars / memory.forming_bars_observed

    def _balance_probable_ready(self, memory: _BalanceMemory) -> bool:
        return bool(
            memory.forming_bars_observed
            >= self.balance_cfg.probable_min_observations
            and memory.containment_bars
            >= self.balance_cfg.probable_min_contained_bars
            and self._balance_containment_ratio(memory)
            >= self.balance_cfg.probable_containment_ratio_min
            and memory.meaningful_escape_bars == 0
        )

    def _balance_lock_ready(self, memory: _BalanceMemory) -> bool:
        return bool(
            memory.forming_bars_observed
            >= self.balance_cfg.lock_min_observations
            and memory.containment_bars
            >= self.balance_cfg.lock_min_contained_bars
            and self._balance_containment_ratio(memory)
            >= self.balance_cfg.lock_containment_ratio_min
            and memory.meaningful_escape_bars == 0
        )

    @staticmethod
    def _freeze_balance_candidate(memory: _BalanceMemory) -> None:
        if memory.candidate_low is None or memory.candidate_high is None:
            raise ValueError("Balance lock requires candidate geometry")
        if not memory.source_range_ids:
            raise ValueError("Balance lock requires at least one source range id")
        memory.range_id = memory.source_range_ids[0]
        memory.frozen_low = memory.candidate_low
        memory.frozen_high = memory.candidate_high

    def _escape_side(
        self,
        memory: _BalanceMemory,
        observation: AuctionObservation,
    ) -> DirectionalBias:
        if memory.frozen_low is None or memory.frozen_high is None:
            raise ValueError("Balance escape evaluation requires frozen range")
        threshold = observation.atr * self.balance_cfg.escape_min_atr
        if observation.close > memory.frozen_high + threshold:
            return DirectionalBias.UP
        if observation.close < memory.frozen_low - threshold:
            return DirectionalBias.DOWN
        return DirectionalBias.UNKNOWN

    @staticmethod
    def _inside_frozen_balance(memory: _BalanceMemory, close: float) -> bool:
        if memory.frozen_low is None or memory.frozen_high is None:
            raise ValueError("Balance containment requires frozen range")
        return memory.frozen_low <= close <= memory.frozen_high

    def _maturity_observed(self, observation: AuctionObservation) -> bool:
        for source in self.directional_cfg.maturity_sources:
            if source is MaturityObservationSource.CURRENT_LEG:
                if observation.current_leg_mature:
                    return True
            elif source is MaturityObservationSource.EXTENSION:
                if observation.extension_mature:
                    return True
            elif source is MaturityObservationSource.OBSERVATION_STATE:
                if (
                    observation.observation_state
                    in self.directional_cfg.maturity_observation_states
                ):
                    return True
            else:  # pragma: no cover - strict enum exhaustiveness
                raise ValueError(f"Unsupported maturity observation source: {source}")
        return False

    def _reversal_watch_trigger(
        self,
        observation: AuctionObservation,
        direction: DirectionalBias,
    ) -> bool:
        for source in self.directional_cfg.reversal_watch_sources:
            if source is ReversalWatchSource.EXHAUSTION:
                if (
                    observation.exhaustion_active
                    and observation.exhausted_side is direction
                ):
                    return True
            elif source is ReversalWatchSource.REJECTION:
                if observation.rejection_observed:
                    return True
            elif source is ReversalWatchSource.FAILED_EXTREME:
                if observation.failed_extreme_observed:
                    return True
            elif source is ReversalWatchSource.STRUCTURAL_FAILURE:
                if observation.structural_failure_confirmed:
                    return True
            elif source is ReversalWatchSource.OBSERVATION_STATE:
                if (
                    observation.observation_state
                    in self.directional_cfg.reversal_watch_observation_states
                ):
                    return True
            else:  # pragma: no cover - strict enum exhaustiveness
                raise ValueError(f"Unsupported reversal-watch source: {source}")
        return False

    def _reversal_watch_trigger_reasons(
        self,
        observation: AuctionObservation,
    ) -> Tuple[str, ...]:
        reasons: List[str] = []
        for source in self.directional_cfg.reversal_watch_sources:
            if source is ReversalWatchSource.EXHAUSTION:
                if observation.exhaustion_active:
                    reasons.append("EXHAUSTION_CONTEXT_ACTIVE")
            elif source is ReversalWatchSource.REJECTION:
                if observation.rejection_observed:
                    reasons.append("REJECTION_OBSERVED")
            elif source is ReversalWatchSource.FAILED_EXTREME:
                if observation.failed_extreme_observed:
                    reasons.append("FAILED_EXTREME_OBSERVED")
            elif source is ReversalWatchSource.STRUCTURAL_FAILURE:
                if observation.structural_failure_confirmed:
                    reasons.append("STRUCTURAL_FAILURE_CONFIRMED")
            elif source is ReversalWatchSource.OBSERVATION_STATE:
                if (
                    observation.observation_state
                    in self.directional_cfg.reversal_watch_observation_states
                ):
                    reasons.append("OBSERVATION_STATE_REVERSAL_WATCH")
            else:  # pragma: no cover - strict enum exhaustiveness
                raise ValueError(f"Unsupported reversal-watch source: {source}")
        return tuple(reasons)

    def _observed_direction(
        self,
        observation: AuctionObservation,
    ) -> DirectionalBias:
        for source in self.directional_cfg.direction_source_precedence:
            if source is DirectionObservationSource.OBSERVATION_STATE:
                if (
                    observation.observation_state
                    in self.directional_cfg.up_observation_states
                ):
                    side = DirectionalBias.UP
                elif (
                    observation.observation_state
                    in self.directional_cfg.down_observation_states
                ):
                    side = DirectionalBias.DOWN
                else:
                    side = DirectionalBias.UNKNOWN
            elif source is DirectionObservationSource.DIRECTIONAL_BIAS:
                side = observation.directional_bias
            elif source is DirectionObservationSource.TREND_DIRECTION:
                side = observation.trend_direction
            else:  # pragma: no cover - strict enum exhaustiveness
                raise ValueError(f"Unsupported direction observation source: {source}")
            if side in (DirectionalBias.UP, DirectionalBias.DOWN):
                return side
        return DirectionalBias.UNKNOWN

    @staticmethod
    def _episode_id(
        symbol: str,
        trading_day: date,
        family: str,
        sequence: int,
        started_at: datetime,
        direction: DirectionalBias,
    ) -> str:
        return (
            f"{family}:{symbol}:{trading_day.isoformat()}:"
            f"{sequence:03d}:{direction.value}:{started_at.strftime('%H%M%S')}"
        )

    @staticmethod
    def _event_id(
        episode_id: str,
        event_type: AuctionEventType,
        event_time: datetime,
    ) -> str:
        return f"{episode_id}:{event_type.value}:{event_time.strftime('%H%M%S')}"



def _opposite_direction(direction: DirectionalBias) -> DirectionalBias:
    if direction is DirectionalBias.UP:
        return DirectionalBias.DOWN
    if direction is DirectionalBias.DOWN:
        return DirectionalBias.UP
    return DirectionalBias.UNKNOWN




__all__ = [
    "EpisodeChronologyError",
    "PersistentEpisodeEngine",
]
