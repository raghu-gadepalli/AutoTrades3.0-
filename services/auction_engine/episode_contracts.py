"""Strict production contracts for authoritative Auction lifecycle state.

The Persistent Episode Engine advances one symbol-day from completed snapshots.
It owns directional and balance lifecycle, emits deterministic events once, and
publishes structural setup permissions. Signal, target, stop, trade and database
lifecycle remain outside these contracts.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Optional, Tuple

from pydantic import Field, model_validator

from enums.auction_engine import (
    AuctionEventType,
    AuctionStateName,
    BalanceEpisodeState,
    DirectionalBias,
    DirectionalEfficiencySource,
    DirectionalEpisodeOrigin,
    DirectionalEpisodeState,
    SetupEventAction,
    SetupFamily,
    StructuralPermissionResult,
)
from services.auction_engine.contracts import BarEvidence, ContractModel, EvidenceSnapshot


class AuctionObservation(ContractModel):
    """Objective completed-snapshot input applied to prior lifecycle state."""

    symbol: str = Field(min_length=1)
    trading_day: date
    snapshot_time: datetime
    close: float = Field(gt=0.0)
    high: float = Field(gt=0.0)
    low: float = Field(gt=0.0)
    atr: float = Field(gt=0.0)

    observation_state: AuctionStateName
    directional_bias: DirectionalBias
    trend_direction: DirectionalBias
    current_leg_mature: bool
    extension_mature: bool

    exhaustion_active: bool
    exhausted_side: DirectionalBias
    rejection_observed: bool
    failed_extreme_observed: bool
    structural_failure_confirmed: bool

    trend_protection_level: Optional[float] = Field(default=None, gt=0.0)
    trend_protection_source: str = ""
    trend_protection_time: Optional[datetime] = None

    accepted_range_id: Optional[str] = None
    accepted_range_low: Optional[float] = Field(default=None, gt=0.0)
    accepted_range_high: Optional[float] = Field(default=None, gt=0.0)
    accepted_range_established_at: Optional[datetime] = None
    accepted_range_provisional: bool
    accepted_range_breakout_eligible: bool
    accepted_range_inside: bool
    # Relative close location within range width. It is intentionally unbounded:
    # below 0 is below the range and above 1 is above the range.
    accepted_range_position: Optional[float] = None
    accepted_range_outside_atr: Optional[float] = None
    range_width_atr: Optional[float] = Field(default=None, ge=0.0)
    directional_efficiency: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    directional_efficiency_source: DirectionalEfficiencySource
    overlap_ratio: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    source_reason_codes: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_observation(self) -> "AuctionObservation":
        if self.trading_day != self.snapshot_time.date():
            raise ValueError("AuctionObservation trading_day must match snapshot_time")
        if self.high < self.low:
            raise ValueError("AuctionObservation high must be >= low")
        if self.high < max(self.close, self.low):
            raise ValueError("AuctionObservation high cannot be below close")
        if self.low > min(self.close, self.high):
            raise ValueError("AuctionObservation low cannot be above close")
        if (self.accepted_range_low is None) != (self.accepted_range_high is None):
            raise ValueError("Accepted range low/high must be supplied together")
        if self.accepted_range_low is not None and self.accepted_range_high is not None:
            if self.accepted_range_high <= self.accepted_range_low:
                raise ValueError("Accepted range high must exceed low")
        if self.accepted_range_inside and self.accepted_range_low is None:
            raise ValueError("accepted_range_inside requires accepted range geometry")
        if self.accepted_range_position is not None and self.accepted_range_low is None:
            raise ValueError("accepted_range_position requires accepted range geometry")
        if self.exhaustion_active:
            if self.exhausted_side not in (DirectionalBias.UP, DirectionalBias.DOWN):
                raise ValueError("Active exhaustion requires an exhausted side")
        if self.directional_efficiency is None:
            if self.directional_efficiency_source is not DirectionalEfficiencySource.NONE:
                raise ValueError(
                    "Missing directional efficiency requires source NONE"
                )
        elif self.directional_efficiency_source is DirectionalEfficiencySource.NONE:
            raise ValueError(
                "Directional efficiency value requires a concrete source"
            )
        if self.trend_protection_level is None:
            if self.trend_protection_source or self.trend_protection_time is not None:
                raise ValueError(
                    "Trend protection source/time require trend_protection_level"
                )
        else:
            if not self.trend_protection_source or self.trend_protection_time is None:
                raise ValueError(
                    "Trend protection level requires source and observation time"
                )
        return self


class AuctionEvent(ContractModel):
    """One deterministic lifecycle transition emitted at most once."""

    event_id: str = Field(min_length=1)
    event_type: AuctionEventType
    episode_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    trading_day: date
    event_time: datetime
    direction: DirectionalBias = DirectionalBias.UNKNOWN
    reason_codes: Tuple[str, ...] = ()
    data: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_event(self) -> "AuctionEvent":
        if self.trading_day != self.event_time.date():
            raise ValueError("AuctionEvent trading_day must match event_time")
        return self


class StructuralSetupPermission(ContractModel):
    """Final structural result for one setup family at one snapshot."""

    setup_family: SetupFamily
    result: StructuralPermissionResult
    source_event_ids: Tuple[str, ...] = ()
    source_event_types: Tuple[AuctionEventType, ...] = ()
    balance_state: BalanceEpisodeState
    reason_codes: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_permission(self) -> "StructuralSetupPermission":
        if len(self.source_event_ids) != len(set(self.source_event_ids)):
            raise ValueError("Structural permission event IDs must be unique")
        if len(self.source_event_types) != len(set(self.source_event_types)):
            raise ValueError("Structural permission event types must be unique")
        if bool(self.source_event_ids) != bool(self.source_event_types):
            raise ValueError(
                "Structural permission event IDs and event types must be supplied together"
            )
        return self


class AuthoritativeSetupEventRoute(ContractModel):
    """One setup-family route derived only from an authoritative Auction event."""

    source_event_id: str = Field(min_length=1)
    source_event_type: AuctionEventType
    source_episode_id: str = Field(min_length=1)
    setup_family: SetupFamily
    action: SetupEventAction
    direction: DirectionalBias
    structural_result: Optional[StructuralPermissionResult] = None
    reason_codes: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_route(self) -> "AuthoritativeSetupEventRoute":
        if self.action is SetupEventAction.EVALUATE:
            if self.structural_result is None:
                raise ValueError(
                    "EVALUATE setup route requires a structural permission result"
                )
        elif self.action in {SetupEventAction.INVALIDATE, SetupEventAction.CLOSE}:
            if self.structural_result is not None:
                raise ValueError(
                    "INVALIDATE/CLOSE setup routes cannot carry creation permission"
                )
        return self


class DirectionalEpisodeProjection(ContractModel):
    episode_id: Optional[str] = None
    previous_state: DirectionalEpisodeState
    current_state: DirectionalEpisodeState
    direction: DirectionalBias
    origin_source: DirectionalEpisodeOrigin
    parent_episode_id: Optional[str] = None
    origin_event_id: Optional[str] = None
    started_at: Optional[datetime] = None
    state_started_at: Optional[datetime] = None
    state_age_bars: int = Field(ge=0)
    origin_price: Optional[float] = Field(default=None, gt=0.0)
    extreme_price: Optional[float] = Field(default=None, gt=0.0)
    extreme_time: Optional[datetime] = None
    protection_level: Optional[float] = Field(default=None, gt=0.0)
    protection_source: str = ""
    reversal_confirmation_level: Optional[float] = Field(default=None, gt=0.0)
    reversal_confirmation_source: str = ""
    reversal_confirmation_level_time: Optional[datetime] = None
    first_adverse_bar_time: Optional[datetime] = None
    first_adverse_bar_level: Optional[float] = Field(default=None, gt=0.0)
    first_adverse_bar_close: Optional[float] = Field(default=None, gt=0.0)
    rejection_seen: bool
    continuation_failure_seen: bool
    continuation_failure_time: Optional[datetime] = None
    reversal_confirmation_breach_closes: int = Field(ge=0)
    reversal_leg_progress_bars: int = Field(ge=0)
    reversal_leg_failure_closes: int = Field(ge=0)
    reversal_leg_progress_atr: float = Field(ge=0.0)
    reason_codes: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_directional(self) -> "DirectionalEpisodeProjection":
        if self.current_state is DirectionalEpisodeState.NONE:
            if self.episode_id is not None:
                raise ValueError("Directional NONE cannot have episode_id")
            if self.origin_source is not DirectionalEpisodeOrigin.NONE:
                raise ValueError("Directional NONE requires origin_source NONE")
            if self.parent_episode_id is not None or self.origin_event_id is not None:
                raise ValueError("Directional NONE cannot retain origin linkage")
        else:
            if self.episode_id is None or self.started_at is None or self.state_started_at is None:
                raise ValueError("Active directional episode requires identity and timestamps")
            if self.direction not in (DirectionalBias.UP, DirectionalBias.DOWN):
                raise ValueError("Active directional episode requires UP or DOWN direction")
            if self.origin_source is DirectionalEpisodeOrigin.NONE:
                raise ValueError("Active directional episode requires an origin source")
            if self.origin_source is DirectionalEpisodeOrigin.REVERSAL_EVENT_HANDOFF:
                if self.parent_episode_id is None or self.origin_event_id is None:
                    raise ValueError("Reversal handoff requires parent episode and origin event")
            elif self.parent_episode_id is not None or self.origin_event_id is not None:
                raise ValueError(
                    "Observation-confirmed directional origin cannot carry handoff linkage"
                )
        if (self.first_adverse_bar_time is None) != (self.first_adverse_bar_level is None):
            raise ValueError("First adverse bar time/level must be supplied together")
        if self.first_adverse_bar_level is None and self.first_adverse_bar_close is not None:
            raise ValueError("First adverse bar close requires adverse bar geometry")
        if self.first_adverse_bar_level is not None and self.first_adverse_bar_close is None:
            raise ValueError("First adverse bar geometry requires close")
        if self.continuation_failure_seen and self.continuation_failure_time is None:
            raise ValueError("Continuation failure requires a timestamp")
        if not self.continuation_failure_seen and self.continuation_failure_time is not None:
            raise ValueError("Continuation failure timestamp requires the stage")
        if self.reversal_confirmation_level is None:
            if self.reversal_confirmation_level_time is not None:
                raise ValueError("Reversal confirmation time requires a level")
            if self.reversal_confirmation_source:
                raise ValueError("Reversal confirmation source requires a level")
        elif self.reversal_confirmation_level_time is None:
            raise ValueError("Reversal confirmation level requires a timestamp")
        if self.current_state is DirectionalEpisodeState.REVERSAL_LEG:
            if self.origin_source is not DirectionalEpisodeOrigin.REVERSAL_EVENT_HANDOFF:
                raise ValueError("REVERSAL_LEG requires reversal-event handoff origin")
        return self


class BalanceEpisodeProjection(ContractModel):
    episode_id: Optional[str] = None
    previous_state: BalanceEpisodeState
    current_state: BalanceEpisodeState
    started_at: Optional[datetime] = None
    state_started_at: Optional[datetime] = None
    state_age_bars: int = Field(ge=0)
    range_id: Optional[str] = None
    candidate_low: Optional[float] = Field(default=None, gt=0.0)
    candidate_high: Optional[float] = Field(default=None, gt=0.0)
    source_range_ids: Tuple[str, ...] = ()
    candidate_merge_count: int = Field(ge=0)
    candidate_bar_expansion_count: int = Field(ge=0)
    candidate_last_valid_at: Optional[datetime] = None
    forming_invalid_bars: int = Field(ge=0)
    frozen_low: Optional[float] = Field(default=None, gt=0.0)
    frozen_high: Optional[float] = Field(default=None, gt=0.0)
    containment_bars: int = Field(ge=0)
    forming_bars_observed: int = Field(ge=0)
    marginal_excursion_bars: int = Field(ge=0)
    meaningful_escape_bars: int = Field(ge=0)
    containment_ratio: float = Field(ge=0.0, le=1.0)
    escape_direction: DirectionalBias
    outside_close_count: int = Field(ge=0)
    reentry_close_count: int = Field(ge=0)
    escape_attempt_count: int = Field(default=0, ge=0)
    failed_escape_count: int = Field(default=0, ge=0)
    up_escape_attempt_count: int = Field(default=0, ge=0)
    down_escape_attempt_count: int = Field(default=0, ge=0)
    last_escape_direction: DirectionalBias = DirectionalBias.UNKNOWN
    last_escape_started_at: Optional[datetime] = None
    last_escape_failed_at: Optional[datetime] = None
    rearm_required: bool = False
    rearm_inside_close_count: int = Field(default=0, ge=0)
    rearm_bars_elapsed: int = Field(default=0, ge=0)
    attempt_limit_reached: bool = False
    reason_codes: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_balance(self) -> "BalanceEpisodeProjection":
        if self.current_state is BalanceEpisodeState.NONE:
            if self.episode_id is not None:
                raise ValueError("Balance NONE cannot have episode_id")
            return self
        if self.episode_id is None or self.started_at is None or self.state_started_at is None:
            raise ValueError("Active balance episode requires identity and timestamps")
        if self.current_state in {
            BalanceEpisodeState.FORMING,
            BalanceEpisodeState.PROBABLE,
        }:
            if self.candidate_low is None or self.candidate_high is None:
                raise ValueError("FORMING/PROBABLE balance requires candidate geometry")
        elif self.current_state in {
            BalanceEpisodeState.LOCKED,
            BalanceEpisodeState.ESCAPE_WATCH,
            BalanceEpisodeState.ACCEPTED_OUTSIDE,
            BalanceEpisodeState.FAILED_BACK_INSIDE,
        }:
            if self.range_id is None or self.frozen_low is None or self.frozen_high is None:
                raise ValueError("Locked/resolving balance requires frozen geometry")
        if self.candidate_low is not None and self.candidate_high is not None:
            if self.candidate_high <= self.candidate_low:
                raise ValueError("Balance candidate_high must exceed candidate_low")
        if self.frozen_low is not None and self.frozen_high is not None:
            if self.frozen_high <= self.frozen_low:
                raise ValueError("Balance frozen_high must exceed frozen_low")
        if self.forming_bars_observed == 0:
            if self.containment_bars or self.marginal_excursion_bars or self.meaningful_escape_bars:
                raise ValueError("Balance evidence counts require observed forming bars")
        else:
            expected = self.containment_bars / self.forming_bars_observed
            if abs(self.containment_ratio - expected) > 1e-9:
                raise ValueError("Balance containment_ratio must match evidence counts")
        if self.up_escape_attempt_count + self.down_escape_attempt_count != self.escape_attempt_count:
            raise ValueError("Balance directional attempt counts must equal total attempts")
        if self.failed_escape_count > self.escape_attempt_count:
            raise ValueError("Balance failed escapes cannot exceed total attempts")
        if self.rearm_required and self.current_state is not BalanceEpisodeState.FAILED_BACK_INSIDE:
            raise ValueError("Balance rearm_required requires FAILED_BACK_INSIDE state")
        if self.attempt_limit_reached and not self.rearm_required:
            raise ValueError("Balance attempt limit requires rearm_required")
        return self


class AuctionLifecycleProjection(ContractModel):
    """Authoritative public lifecycle projection for one completed snapshot."""

    symbol: str = Field(min_length=1)
    trading_day: date
    snapshot_time: datetime
    directional: DirectionalEpisodeProjection
    balance: BalanceEpisodeProjection
    events: Tuple[AuctionEvent, ...] = ()
    permissions: Tuple[StructuralSetupPermission, ...] = ()
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
    engine_name: str = Field(min_length=1)
    engine_version: str = Field(min_length=1)
    # Accepted only so previously persisted snapshots remain readable.
    # Runtime logic never compares or depends on either value.
    config_version: Optional[str] = None
    config_hash: Optional[str] = None

    @model_validator(mode="after")
    def _validate_projection(self) -> "AuctionLifecycleProjection":
        if self.trading_day != self.snapshot_time.date():
            raise ValueError(
                "AuctionLifecycleProjection trading_day must match snapshot_time"
            )
        event_ids = tuple(event.event_id for event in self.events)
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("Auction lifecycle event IDs must be unique")
        for event in self.events:
            if event.symbol != self.symbol or event.event_time != self.snapshot_time:
                raise ValueError("Auction lifecycle events must align with projection")
        permission_families = tuple(item.setup_family for item in self.permissions)
        if len(permission_families) != len(set(permission_families)):
            raise ValueError("Only one structural permission is allowed per setup family")
        event_id_set = set(event_ids)
        for permission in self.permissions:
            if not set(permission.source_event_ids).issubset(event_id_set):
                raise ValueError(
                    "Structural permission references an event outside the projection"
                )
            if permission.balance_state is not self.balance.current_state:
                raise ValueError(
                    "Structural permission balance_state must match lifecycle projection"
                )
        return self


class AuctionEvidenceHistoryTrend(ContractModel):
    hma_order: str
    hma_spread_atr: Optional[float] = None
    ema_slow: Optional[float] = Field(default=None, gt=0.0)
    ema_ref: Optional[float] = Field(default=None, gt=0.0)


class AuctionEvidenceHistoryEntry(ContractModel):
    close: float = Field(gt=0.0)
    bar: BarEvidence
    trend: AuctionEvidenceHistoryTrend
    atr: Optional[float] = Field(default=None, gt=0.0)


class DirectionalObservationMemory(ContractModel):
    """Persistent objective observation context; never a setup lifecycle."""

    trading_day: Optional[date] = None
    last_snapshot_time: Optional[datetime] = None
    observation_count: int = Field(ge=0)
    current_state: AuctionStateName
    state_age_bars: int = Field(ge=0)
    pending_state: Optional[AuctionStateName] = None
    pending_bars: int = Field(ge=0)
    trend_candidate_side: DirectionalBias
    trend_candidate_bars: int = Field(ge=0)
    established_side: DirectionalBias
    trend_onset_time: Optional[datetime] = None
    trend_anchor_price: Optional[float] = Field(default=None, gt=0.0)
    trend_extreme_price: Optional[float] = Field(default=None, gt=0.0)
    protection_level: Optional[float] = Field(default=None, gt=0.0)
    protection_source: str
    protection_time: Optional[datetime] = None
    leg_anchor_time: Optional[datetime] = None
    leg_anchor_price: Optional[float] = Field(default=None, gt=0.0)
    leg_extreme_price: Optional[float] = Field(default=None, gt=0.0)
    leg_age_bars: int = Field(ge=0)
    leg_no_progress_bars: int = Field(ge=0)
    leg_maturity_consumed: bool
    leg_maturity_onset_time: Optional[datetime] = None
    leg_maturity_extreme_price: Optional[float] = Field(default=None, gt=0.0)
    pullback_candidate_bars: int = Field(ge=0)
    pullback_age_bars: int = Field(ge=0)
    pullback_extreme_price: Optional[float] = Field(default=None, gt=0.0)
    compression_candidate_bars: int = Field(ge=0)
    compression_box_low: Optional[float] = Field(default=None, gt=0.0)
    compression_box_high: Optional[float] = Field(default=None, gt=0.0)
    recompression_age_bars: int = Field(ge=0)
    reacceleration_age_bars: int = Field(ge=0)
    exhaustion_active: bool
    exhaustion_side: DirectionalBias
    exhaustion_started_at: Optional[datetime] = None
    exhaustion_last_seen_at: Optional[datetime] = None
    exhaustion_age_bars: int = Field(ge=0)
    exhaustion_clear_bars: int = Field(ge=0)
    failure_breach_bars: int = Field(ge=0)
    trend_failure_age_bars: int = Field(ge=0)
    reversal_confirmation_bars: int = Field(ge=0)
    reversal_side: DirectionalBias
    last_close: Optional[float] = Field(default=None, gt=0.0)
    last_reason_codes: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_observation_memory(self) -> "DirectionalObservationMemory":
        if self.last_snapshot_time is not None:
            if self.trading_day is None or self.last_snapshot_time.date() != self.trading_day:
                raise ValueError("Observation memory timestamp must match trading_day")
        if self.established_side is DirectionalBias.UNKNOWN:
            if self.protection_level is not None or self.leg_anchor_price is not None:
                raise ValueError("Unestablished observation memory cannot retain trend geometry")
        return self


class DirectionalEpisodeMemory(ContractModel):
    sequence: int = Field(ge=0)
    episode_id: Optional[str] = None
    state: DirectionalEpisodeState
    direction: DirectionalBias
    origin_source: DirectionalEpisodeOrigin
    parent_episode_id: Optional[str] = None
    origin_event_id: Optional[str] = None
    started_at: Optional[datetime] = None
    state_started_at: Optional[datetime] = None
    state_age_bars: int = Field(ge=0)
    origin_price: Optional[float] = Field(default=None, gt=0.0)
    extreme_price: Optional[float] = Field(default=None, gt=0.0)
    extreme_time: Optional[datetime] = None
    protection_level: Optional[float] = Field(default=None, gt=0.0)
    protection_source: str
    protection_time: Optional[datetime] = None
    start_candidate_side: DirectionalBias
    start_candidate_bars: int = Field(ge=0)
    rejection_seen: bool
    rejection_seen_at: Optional[datetime] = None
    continuation_failure_seen: bool
    continuation_failure_seen_at: Optional[datetime] = None
    continuation_failure_progress_bars: int = Field(ge=0)
    first_adverse_bar_time: Optional[datetime] = None
    first_adverse_bar_level: Optional[float] = Field(default=None, gt=0.0)
    first_adverse_bar_close: Optional[float] = Field(default=None, gt=0.0)
    reversal_confirmation_level: Optional[float] = Field(default=None, gt=0.0)
    reversal_confirmation_source: str
    reversal_confirmation_level_time: Optional[datetime] = None
    reversal_confirmation_breach_closes: int = Field(ge=0)
    reversal_watch_age_bars: int = Field(ge=0)
    reversal_leg_progress_bars: int = Field(ge=0)
    reversal_leg_failure_closes: int = Field(ge=0)
    reversal_leg_progress_atr: float = Field(ge=0.0)
    trend_restore_bars: int = Field(ge=0)
    opposite_control_bars: int = Field(ge=0)
    inactive_bars: int = Field(ge=0)
    emitted_event_ids: Tuple[str, ...] = ()
    last_close: Optional[float] = Field(default=None, gt=0.0)
    last_observation_state: AuctionStateName
    last_observation_state_time: Optional[datetime]
    last_reason_codes: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_directional_memory(self) -> "DirectionalEpisodeMemory":
        if len(self.emitted_event_ids) != len(set(self.emitted_event_ids)):
            raise ValueError("Directional emitted_event_ids must be unique")
        if self.state is DirectionalEpisodeState.NONE:
            if self.episode_id is not None:
                raise ValueError("Directional NONE memory cannot retain episode_id")
        elif self.episode_id is None or self.started_at is None or self.state_started_at is None:
            raise ValueError("Active directional memory requires identity and timestamps")
        if self.last_observation_state_time is None:
            if self.last_observation_state is not AuctionStateName.UNKNOWN:
                raise ValueError(
                    "Missing last observation time requires UNKNOWN observation state"
                )
        return self


class BalanceEpisodeMemory(ContractModel):
    sequence: int = Field(ge=0)
    episode_id: Optional[str] = None
    state: BalanceEpisodeState
    started_at: Optional[datetime] = None
    state_started_at: Optional[datetime] = None
    state_age_bars: int = Field(ge=0)
    range_id: Optional[str] = None
    candidate_low: Optional[float] = Field(default=None, gt=0.0)
    candidate_high: Optional[float] = Field(default=None, gt=0.0)
    source_range_ids: Tuple[str, ...] = ()
    candidate_merge_count: int = Field(ge=0)
    candidate_bar_expansion_count: int = Field(ge=0)
    candidate_last_valid_at: Optional[datetime] = None
    frozen_low: Optional[float] = Field(default=None, gt=0.0)
    frozen_high: Optional[float] = Field(default=None, gt=0.0)
    containment_bars: int = Field(ge=0)
    forming_bars_observed: int = Field(ge=0)
    marginal_excursion_bars: int = Field(ge=0)
    meaningful_escape_bars: int = Field(ge=0)
    forming_invalid_bars: int = Field(ge=0)
    escape_direction: DirectionalBias
    outside_close_count: int = Field(ge=0)
    reentry_close_count: int = Field(ge=0)
    escape_attempt_count: int = Field(default=0, ge=0)
    failed_escape_count: int = Field(default=0, ge=0)
    up_escape_attempt_count: int = Field(default=0, ge=0)
    down_escape_attempt_count: int = Field(default=0, ge=0)
    last_escape_direction: DirectionalBias = DirectionalBias.UNKNOWN
    last_escape_started_at: Optional[datetime] = None
    last_escape_failed_at: Optional[datetime] = None
    rearm_required: bool = False
    rearm_inside_close_count: int = Field(default=0, ge=0)
    rearm_bars_elapsed: int = Field(default=0, ge=0)
    attempt_limit_reached: bool = False
    emitted_event_ids: Tuple[str, ...] = ()
    last_reason_codes: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_balance_memory(self) -> "BalanceEpisodeMemory":
        if len(self.emitted_event_ids) != len(set(self.emitted_event_ids)):
            raise ValueError("Balance emitted_event_ids must be unique")
        if len(self.source_range_ids) != len(set(self.source_range_ids)):
            raise ValueError("Balance source_range_ids must be unique")
        if self.state is BalanceEpisodeState.NONE:
            if self.episode_id is not None:
                raise ValueError("Balance NONE memory cannot retain episode_id")
        elif self.episode_id is None or self.started_at is None or self.state_started_at is None:
            raise ValueError("Active balance memory requires identity and timestamps")
        if self.up_escape_attempt_count + self.down_escape_attempt_count != self.escape_attempt_count:
            raise ValueError("Balance memory directional attempt counts must equal total attempts")
        if self.failed_escape_count > self.escape_attempt_count:
            raise ValueError("Balance memory failed escapes cannot exceed total attempts")
        if self.rearm_required and self.state is not BalanceEpisodeState.FAILED_BACK_INSIDE:
            raise ValueError("Balance memory rearm_required requires FAILED_BACK_INSIDE state")
        if self.attempt_limit_reached and not self.rearm_required:
            raise ValueError("Balance memory attempt limit requires rearm_required")
        return self


class AuctionEpisodeMemory(ContractModel):
    symbol: str = Field(min_length=1)
    trading_day: date
    last_snapshot_time: Optional[datetime] = None
    last_observation_hash: str
    evidence_history: Tuple[AuctionEvidenceHistoryEntry, ...] = ()
    observation: DirectionalObservationMemory
    directional: DirectionalEpisodeMemory
    balance: BalanceEpisodeMemory

    @model_validator(mode="after")
    def _validate_memory(self) -> "AuctionEpisodeMemory":
        if self.last_snapshot_time is not None and self.last_snapshot_time.date() != self.trading_day:
            raise ValueError("Auction memory last_snapshot_time must match trading_day")
        history_times = tuple(item.bar.snapshot_time for item in self.evidence_history)
        if tuple(sorted(history_times)) != history_times:
            raise ValueError("Auction evidence history must be chronological")
        if len(history_times) != len(set(history_times)):
            raise ValueError("Auction evidence history times must be unique")
        if self.last_snapshot_time is None:
            if self.last_observation_hash or self.evidence_history:
                raise ValueError("Initial Auction memory cannot contain prior observation data")
        elif not self.last_observation_hash:
            raise ValueError("Advanced Auction memory requires last_observation_hash")
        return self


class AuctionAuthorityResult(ContractModel):
    symbol: str = Field(min_length=1)
    snapshot_time: datetime
    evidence: EvidenceSnapshot
    observation: AuctionObservation
    lifecycle: AuctionLifecycleProjection

    @model_validator(mode="after")
    def _validate_authority_result(self) -> "AuctionAuthorityResult":
        if self.evidence.symbol != self.symbol or self.observation.symbol != self.symbol:
            raise ValueError("Auction authority result symbol mismatch")
        if self.lifecycle.symbol != self.symbol:
            raise ValueError("Auction lifecycle symbol mismatch")
        if not (self.evidence.snapshot_time == self.observation.snapshot_time == self.lifecycle.snapshot_time == self.snapshot_time):
            raise ValueError("Auction authority result snapshot_time mismatch")
        return self


__all__ = [
    "AuctionObservation",
    "AuctionEvent",
    "StructuralSetupPermission",
    "AuthoritativeSetupEventRoute",
    "DirectionalEpisodeProjection",
    "BalanceEpisodeProjection",
    "AuctionLifecycleProjection",
    "AuctionEvidenceHistoryTrend",
    "AuctionEvidenceHistoryEntry",
    "DirectionalObservationMemory",
    "DirectionalEpisodeMemory",
    "BalanceEpisodeMemory",
    "AuctionEpisodeMemory",
    "AuctionAuthorityResult",
]
