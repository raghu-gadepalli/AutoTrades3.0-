"""Current Auction event, permission and balance lifecycle contracts.

Directional continuity is defined in ``directional_contracts``.  This module
contains only contracts still consumed by the current Auction, setup and
signal paths.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Optional, Tuple

from pydantic import Field, model_validator

from enums.auction_engine import (
    AuctionEventType,
    BalanceEpisodeState,
    DirectionalBias,
    SetupEventAction,
    SetupFamily,
    StructuralPermissionResult,
)
from services.auction_engine.contracts import ContractModel


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

class AuctionEvidenceHistoryTrend(ContractModel):
    hma_order: str
    hma_spread_atr: Optional[float] = None
    ema_slow: Optional[float] = Field(default=None, gt=0.0)
    ema_ref: Optional[float] = Field(default=None, gt=0.0)

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

__all__ = [
    "AuctionEvent",
    "StructuralSetupPermission",
    "AuthoritativeSetupEventRoute",
    "BalanceEpisodeProjection",
    "AuctionEvidenceHistoryTrend",
    "BalanceEpisodeMemory",
]
