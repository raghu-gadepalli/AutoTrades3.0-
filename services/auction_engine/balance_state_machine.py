"""Authoritative balance evidence and lifecycle state machine.

This module contains only the balance path retained by the current Auction
engine.  Directional authority is owned independently by
``directional_state_machine``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import List, Optional, Set, Tuple

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG, AuctionEngineConfig
from enums.auction_engine import AuctionEventType, BalanceEpisodeState, DirectionalBias
from schemas.snapshot import SnapshotSchema
from services.auction_engine.contracts import EvidenceSnapshot
from services.auction_engine.episode_contracts import (
    AuctionEvent,
    BalanceEpisodeMemory,
    BalanceEpisodeProjection,
)


@dataclass(frozen=True)
class BalanceObservation:
    symbol: str
    trading_day: object
    snapshot_time: object
    close: float
    high: float
    low: float
    atr: float
    accepted_range_id: Optional[str]
    accepted_range_low: Optional[float]
    accepted_range_high: Optional[float]
    accepted_range_provisional: bool
    accepted_range_breakout_eligible: bool
    accepted_range_inside: bool
    directional_efficiency: Optional[float]
    overlap_ratio: Optional[float]
    range_width_atr: Optional[float]

class BalanceObservationBuilder:
    def __init__(self, config: AuctionEngineConfig = AUCTION_ENGINE_CONFIG) -> None:
        self.config = config
        self.cfg = config.episode.balance

    def build(self, snapshot: SnapshotSchema, evidence: EvidenceSnapshot) -> BalanceObservation:
        accepted = snapshot.structure.accepted.range
        range_id = self._optional_text(accepted.range_id)
        low = self._optional_positive(accepted.low)
        high = self._optional_positive(accepted.high)
        if (low is None) != (high is None):
            raise ValueError("Accepted range low/high must be supplied together")
        if low is not None and high is not None and high <= low:
            raise ValueError("Accepted range high must exceed low")
        if range_id is None and (low is not None or high is not None):
            raise ValueError("Accepted range geometry requires range_id")
        if range_id is not None and (low is None or high is None):
            raise ValueError("Accepted range_id requires geometry")

        inside = False
        width_atr = None
        if low is not None and high is not None:
            width_atr = (high - low) / float(evidence.atr)
            tolerance = float(evidence.atr) * self.cfg.source_range_inside_tolerance_atr
            inside = low - tolerance <= evidence.close <= high + tolerance
        efficiency = (
            evidence.price_action.directional_efficiency
            if evidence.price_action.directional_efficiency is not None
            else evidence.trend.directional_efficiency
        )
        return BalanceObservation(
            symbol=evidence.symbol,
            trading_day=evidence.trading_day,
            snapshot_time=evidence.snapshot_time,
            close=evidence.close,
            high=evidence.bar.high,
            low=evidence.bar.low,
            atr=float(evidence.atr),
            accepted_range_id=range_id,
            accepted_range_low=low,
            accepted_range_high=high,
            accepted_range_provisional=bool(accepted.provisional),
            accepted_range_breakout_eligible=bool(accepted.breakout_eligible),
            accepted_range_inside=inside,
            directional_efficiency=efficiency,
            overlap_ratio=evidence.price_action.overlap_ratio,
            range_width_atr=width_atr,
        )

    @staticmethod
    def _optional_text(value: object) -> Optional[str]:
        text = str(value or "").strip()
        return text or None

    @staticmethod
    def _optional_positive(value: object) -> Optional[float]:
        if value is None:
            return None
        number = float(value)
        if number <= 0.0:
            raise ValueError("Accepted range prices must be positive")
        return number

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

class BalanceStateMachine:
    """Advance the retained balance lifecycle by one completed snapshot."""

    def __init__(self, config: AuctionEngineConfig = AUCTION_ENGINE_CONFIG) -> None:
        self.config = config
        self.balance_cfg = config.episode.balance

    def advance(
        self,
        *,
        previous_memory: BalanceEpisodeMemory,
        observation: BalanceObservation,
    ) -> tuple[BalanceEpisodeMemory, BalanceEpisodeProjection, Tuple[AuctionEvent, ...]]:
        payload = previous_memory.model_dump(mode="python")
        payload["source_range_ids"] = list(payload["source_range_ids"])
        payload["emitted_event_ids"] = set(payload["emitted_event_ids"])
        memory = _BalanceMemory(**payload)
        previous_state = memory.state
        events: List[AuctionEvent] = []
        reasons = self._advance_balance(memory, observation, events)
        contract = self._memory_contract(memory)
        projection = BalanceEpisodeProjection(
            episode_id=memory.episode_id,
            previous_state=previous_state,
            current_state=memory.state,
            started_at=memory.started_at,
            state_started_at=memory.state_started_at,
            state_age_bars=memory.state_age_bars,
            range_id=memory.range_id,
            candidate_low=memory.candidate_low,
            candidate_high=memory.candidate_high,
            source_range_ids=tuple(memory.source_range_ids),
            candidate_merge_count=memory.candidate_merge_count,
            candidate_bar_expansion_count=memory.candidate_bar_expansion_count,
            candidate_last_valid_at=memory.candidate_last_valid_at,
            forming_invalid_bars=memory.forming_invalid_bars,
            frozen_low=memory.frozen_low,
            frozen_high=memory.frozen_high,
            containment_bars=memory.containment_bars,
            forming_bars_observed=memory.forming_bars_observed,
            marginal_excursion_bars=memory.marginal_excursion_bars,
            meaningful_escape_bars=memory.meaningful_escape_bars,
            containment_ratio=self._balance_containment_ratio(memory),
            escape_direction=memory.escape_direction,
            outside_close_count=memory.outside_close_count,
            reentry_close_count=memory.reentry_close_count,
            escape_attempt_count=memory.escape_attempt_count,
            failed_escape_count=memory.failed_escape_count,
            up_escape_attempt_count=memory.up_escape_attempt_count,
            down_escape_attempt_count=memory.down_escape_attempt_count,
            last_escape_direction=memory.last_escape_direction,
            last_escape_started_at=memory.last_escape_started_at,
            last_escape_failed_at=memory.last_escape_failed_at,
            rearm_required=memory.rearm_required,
            rearm_inside_close_count=memory.rearm_inside_close_count,
            rearm_bars_elapsed=memory.rearm_bars_elapsed,
            attempt_limit_reached=memory.attempt_limit_reached,
            reason_codes=reasons,
        )
        return contract, projection, tuple(events)

    @staticmethod
    def _memory_contract(memory: _BalanceMemory) -> BalanceEpisodeMemory:
        payload = dict(memory.__dict__)
        payload["source_range_ids"] = tuple(memory.source_range_ids)
        payload["emitted_event_ids"] = tuple(sorted(memory.emitted_event_ids))
        return BalanceEpisodeMemory.model_validate(payload)

    def _advance_balance(
        self,
        memory: _BalanceMemory,
        observation: BalanceObservation,
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
        observation: BalanceObservation,
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
        observation: BalanceObservation,
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
        observation: BalanceObservation,
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
        observation: BalanceObservation,
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

    def _start_balance_episode(
        self,
        memory: _BalanceMemory,
        observation: BalanceObservation,
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

    def _transition_balance(
        self,
        memory: _BalanceMemory,
        observation: BalanceObservation,
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
        observation: BalanceObservation,
        state: BalanceEpisodeState,
    ) -> None:
        memory.state = state
        memory.state_started_at = observation.snapshot_time
        memory.state_age_bars = 1

    def _emit_balance_event(
        self,
        memory: _BalanceMemory,
        observation: BalanceObservation,
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

    def _accepted_range_can_form(self, observation: BalanceObservation) -> bool:
        if not self._accepted_range_source_qualifies(observation):
            return False
        if not observation.accepted_range_inside:
            return False
        if observation.range_width_atr is None:
            return False
        return observation.range_width_atr <= self.balance_cfg.range_width_atr_max

    def _accepted_range_source_qualifies(
        self,
        observation: BalanceObservation,
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
        observation: BalanceObservation,
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
        observation: BalanceObservation,
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
        observation: BalanceObservation,
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


__all__ = [
    "BalanceObservation",
    "BalanceObservationBuilder",
    "BalanceStateMachine",
]


def _opposite_direction(direction: DirectionalBias) -> DirectionalBias:
    if direction is DirectionalBias.UP:
        return DirectionalBias.DOWN
    if direction is DirectionalBias.DOWN:
        return DirectionalBias.UP
    return DirectionalBias.UNKNOWN
