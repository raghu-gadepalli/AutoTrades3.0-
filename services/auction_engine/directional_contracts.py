"""Minimal directional Auction contracts used by snapshot generation.

The directional core intentionally persists only confirmed directional episode
identity and the counters required to start or reverse it.  Maturity,
restoration, exhaustion, continuation, reacceleration and dynamic protection
are not authoritative directional episode state.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Optional, Tuple

from pydantic import Field, model_validator

from enums.auction_engine import (
    BalanceEpisodeState,
    DirectionalBias,
    DirectionalTransition,
    FreshDirection,
)
from services.auction_engine.contracts import ContractModel, EvidenceSnapshot
from services.auction_engine.episode_contracts import (
    AuctionEvent,
    BalanceEpisodeMemory,
    BalanceEpisodeProjection,
    StructuralSetupPermission,
)


class FreshDirectionalEvidence(ContractModel):
    """Current completed-snapshot direction only; no retained side is used."""

    side: FreshDirection
    candidate_side: DirectionalBias = DirectionalBias.UNKNOWN
    observed_at: datetime
    trend_direction: DirectionalBias
    raw_structure_side: DirectionalBias
    slope_direction: DirectionalBias
    directional_efficiency: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    support_facts: Tuple[str, ...] = ()
    contradict_facts: Tuple[str, ...] = ()
    reason_codes: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_evidence(self) -> "FreshDirectionalEvidence":
        if self.side is FreshDirection.UP and self.candidate_side is not DirectionalBias.UP:
            raise ValueError("Confirmed UP evidence requires UP candidate_side")
        if self.side is FreshDirection.DOWN and self.candidate_side is not DirectionalBias.DOWN:
            raise ValueError("Confirmed DOWN evidence requires DOWN candidate_side")
        if self.candidate_side not in (
            DirectionalBias.UP,
            DirectionalBias.DOWN,
            DirectionalBias.UNKNOWN,
        ):
            raise ValueError("Fresh evidence candidate_side must be UP, DOWN or UNKNOWN")
        return self


class DirectionalProjection(ContractModel):
    """Public projection of the one active directional episode, if any."""

    active_episode_id: Optional[str] = None
    previous_episode_id: Optional[str] = None
    direction: DirectionalBias = DirectionalBias.UNKNOWN
    started_at: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None
    last_confirmed_at: Optional[datetime] = None
    start_price: Optional[float] = Field(default=None, gt=0.0)
    extreme_price: Optional[float] = Field(default=None, gt=0.0)
    age_bars: int = Field(default=0, ge=0)
    support_streak: int = Field(default=0, ge=0)
    opposition_streak: int = Field(default=0, ge=0)
    unresolved_streak: int = Field(default=0, ge=0)
    transition: DirectionalTransition = DirectionalTransition.NONE
    transition_reason: str = ""
    reason_codes: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_projection(self) -> "DirectionalProjection":
        if self.active_episode_id is None:
            if self.direction is not DirectionalBias.UNKNOWN:
                raise ValueError("No active episode requires UNKNOWN direction")
            if any(
                value is not None
                for value in (
                    self.started_at,
                    self.confirmed_at,
                    self.last_confirmed_at,
                    self.start_price,
                    self.extreme_price,
                )
            ):
                raise ValueError("No active episode cannot retain active geometry")
        else:
            if self.direction not in (DirectionalBias.UP, DirectionalBias.DOWN):
                raise ValueError("Active directional episode requires UP or DOWN")
            if self.started_at is None or self.confirmed_at is None:
                raise ValueError("Active directional episode requires timestamps")
            if self.start_price is None or self.extreme_price is None:
                raise ValueError("Active directional episode requires price geometry")
        return self


class DirectionalMemory(ContractModel):
    """Snapshot-carried continuity for the minimal directional tracker."""

    sequence: int = Field(default=0, ge=0)
    active_episode_id: Optional[str] = None
    previous_episode_id: Optional[str] = None
    direction: DirectionalBias = DirectionalBias.UNKNOWN
    started_at: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None
    last_confirmed_at: Optional[datetime] = None
    start_price: Optional[float] = Field(default=None, gt=0.0)
    extreme_price: Optional[float] = Field(default=None, gt=0.0)
    age_bars: int = Field(default=0, ge=0)
    support_streak: int = Field(default=0, ge=0)
    opposition_side: DirectionalBias = DirectionalBias.UNKNOWN
    opposition_started_at: Optional[datetime] = None
    opposition_start_price: Optional[float] = Field(default=None, gt=0.0)
    opposition_extreme_price: Optional[float] = Field(default=None, gt=0.0)
    opposition_streak: int = Field(default=0, ge=0)
    unresolved_streak: int = Field(default=0, ge=0)
    start_candidate_side: DirectionalBias = DirectionalBias.UNKNOWN
    start_candidate_started_at: Optional[datetime] = None
    start_candidate_price: Optional[float] = Field(default=None, gt=0.0)
    start_candidate_extreme_price: Optional[float] = Field(default=None, gt=0.0)
    start_candidate_streak: int = Field(default=0, ge=0)
    emitted_event_ids: Tuple[str, ...] = ()
    last_reason_codes: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_memory(self) -> "DirectionalMemory":
        if len(self.emitted_event_ids) != len(set(self.emitted_event_ids)):
            raise ValueError("Directional emitted_event_ids must be unique")
        if self.active_episode_id is None:
            if self.direction is not DirectionalBias.UNKNOWN:
                raise ValueError("No active episode requires UNKNOWN direction")
        elif self.direction not in (DirectionalBias.UP, DirectionalBias.DOWN):
            raise ValueError("Active episode requires UP or DOWN direction")
        if self.start_candidate_side is DirectionalBias.UNKNOWN:
            if self.start_candidate_streak != 0:
                raise ValueError("Missing start candidate requires zero streak")
            if any(
                value is not None
                for value in (
                    self.start_candidate_started_at,
                    self.start_candidate_price,
                    self.start_candidate_extreme_price,
                )
            ):
                raise ValueError("Missing start candidate cannot retain candidate geometry")
        elif self.start_candidate_extreme_price is None:
            raise ValueError("Active start candidate requires candidate extreme")
        if self.opposition_side is DirectionalBias.UNKNOWN:
            if self.opposition_streak != 0:
                raise ValueError("Missing opposition side requires zero streak")
            if any(
                value is not None
                for value in (
                    self.opposition_started_at,
                    self.opposition_start_price,
                    self.opposition_extreme_price,
                )
            ):
                raise ValueError("Missing opposition cannot retain opposition geometry")
        elif self.opposition_extreme_price is None:
            raise ValueError("Active opposition requires opposition extreme")
        return self


class AuctionMemory(ContractModel):
    symbol: str = Field(min_length=1)
    trading_day: date
    last_snapshot_time: Optional[datetime] = None
    directional: DirectionalMemory
    balance: BalanceEpisodeMemory

    @model_validator(mode="after")
    def _validate_memory(self) -> "AuctionMemory":
        if self.last_snapshot_time is not None:
            if self.last_snapshot_time.date() != self.trading_day:
                raise ValueError("Auction memory time must match trading_day")
        return self


class AuctionSnapshotProjection(ContractModel):
    status: str
    continuity_mode: str
    previous_snapshot_time: Optional[datetime] = None
    evidence: Optional[FreshDirectionalEvidence] = None
    directional: Optional[DirectionalProjection] = None
    balance: Optional[BalanceEpisodeProjection] = None
    events: Tuple[AuctionEvent, ...] = ()
    permissions: Tuple[StructuralSetupPermission, ...] = ()
    diagnostics: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_block(self) -> "AuctionSnapshotProjection":
        if self.status == "NOT_RUN":
            if any(value is not None for value in (self.evidence, self.directional, self.balance)):
                raise ValueError("NOT_RUN Auction block cannot contain output")
            if self.events or self.permissions or self.diagnostics:
                raise ValueError("NOT_RUN Auction block cannot contain results")
            return self
        if self.status != "OK":
            raise ValueError("Auction status must be NOT_RUN or OK")
        if self.evidence is None or self.directional is None or self.balance is None:
            raise ValueError("OK Auction block requires evidence and projections")
        if self.continuity_mode == "COLD_START":
            if self.previous_snapshot_time is not None:
                raise ValueError("COLD_START cannot reference previous snapshot")
        elif self.continuity_mode == "INCREMENTAL_PREVIOUS_SNAPSHOT":
            if self.previous_snapshot_time is None:
                raise ValueError("Incremental Auction requires previous snapshot time")
        else:
            raise ValueError("Unsupported Auction continuity_mode")
        event_ids = tuple(event.event_id for event in self.events)
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("Auction event IDs must be unique")
        return self


class AuctionAuthorityResult(ContractModel):
    symbol: str = Field(min_length=1)
    snapshot_time: datetime
    objective_evidence: EvidenceSnapshot
    fresh_direction: FreshDirectionalEvidence
    directional: DirectionalProjection
    balance: BalanceEpisodeProjection
    events: Tuple[AuctionEvent, ...] = ()
    permissions: Tuple[StructuralSetupPermission, ...] = ()
    diagnostics: Dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "FreshDirectionalEvidence",
    "DirectionalProjection",
    "DirectionalMemory",
    "AuctionMemory",
    "AuctionSnapshotProjection",
    "AuctionAuthorityResult",
]
