"""Strict contracts for event-driven Auction setup evaluation.

Setup candidates exist only as interpretations of authoritative Auction events.
There is no setup-side lifecycle discovery, compatibility mapping, or fallback.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from enums.auction_engine import (
    AuctionEventType,
    SetupFamily,
    StructuralPermissionResult,
    TradeSide,
)


SETUP_CONTRACT_VERSION = "AUTHORITATIVE_SETUP_V1"


class SetupContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_assignment=True,
        validate_default=True,
        allow_inf_nan=False,
    )


class AuthoritativeSetupCandidate(SetupContractModel):
    contract_version: str = Field(default=SETUP_CONTRACT_VERSION, min_length=1)
    auction_engine_name: str = Field(min_length=1)
    auction_engine_version: str = Field(min_length=1)
    auction_config_version: str = Field(min_length=1)
    auction_config_hash: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    candidate_id: str = Field(min_length=1)
    opportunity_key: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    trading_day: date
    snapshot_time: datetime
    setup_family: SetupFamily
    setup_subtype: str = Field(min_length=1)
    side: TradeSide
    source_event_id: str = Field(min_length=1)
    source_event_type: AuctionEventType
    source_episode_id: str = Field(min_length=1)
    structural_result: StructuralPermissionResult
    entry_price: float = Field(gt=0.0)
    stop_anchor_price: float = Field(gt=0.0)
    stop_anchor_type: str = Field(min_length=1)
    target_reference_price: float = Field(gt=0.0)
    target_basis: str = Field(min_length=1)
    reference_price: float = Field(gt=0.0)
    reference_source: str = Field(min_length=1)
    risk_points: float = Field(gt=0.0)
    expected_move_points: float = Field(gt=0.0)
    expected_move_pct: float = Field(gt=0.0)
    reward_risk: float = Field(gt=0.0)
    valid_until: Optional[datetime]
    reason_codes: Tuple[str, ...] = ()

    @field_validator("symbol", "setup_subtype", mode="before")
    @classmethod
    def _normalise_upper_text(cls, value: object) -> str:
        text = str(value or "").strip().upper()
        if not text:
            raise ValueError("Uppercase setup text is required")
        return text

    @model_validator(mode="after")
    def _validate_candidate(self) -> "AuthoritativeSetupCandidate":
        if self.contract_version != SETUP_CONTRACT_VERSION:
            raise ValueError("Unsupported authoritative setup contract version")
        if self.trading_day != self.snapshot_time.date():
            raise ValueError("Candidate trading_day must match snapshot_time")
        if self.side not in {TradeSide.BUY, TradeSide.SELL}:
            raise ValueError("Candidate side must be BUY or SELL")
        if self.structural_result is not StructuralPermissionResult.PERMIT:
            raise ValueError("Authoritative setup candidate requires PERMIT")
        if self.side is TradeSide.BUY:
            if not self.stop_anchor_price < self.entry_price < self.target_reference_price:
                raise ValueError("BUY candidate requires stop < entry < target")
        else:
            if not self.target_reference_price < self.entry_price < self.stop_anchor_price:
                raise ValueError("SELL candidate requires target < entry < stop")
        if self.valid_until is not None and self.valid_until < self.snapshot_time:
            raise ValueError("Candidate valid_until cannot precede snapshot_time")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("Candidate reason codes must be unique")
        return self


class SetupEvaluationResult(SetupContractModel):
    source_event_id: str = Field(min_length=1)
    source_event_type: AuctionEventType
    source_episode_id: str = Field(min_length=1)
    setup_family: SetupFamily
    side: TradeSide
    structural_result: StructuralPermissionResult
    approved: bool
    candidate: Optional[AuthoritativeSetupCandidate]
    blockers: Tuple[str, ...] = ()
    reason_codes: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_evaluation(self) -> "SetupEvaluationResult":
        if self.approved != (self.candidate is not None):
            raise ValueError("Approved setup evaluation must contain exactly one candidate")
        if self.approved:
            if self.structural_result is not StructuralPermissionResult.PERMIT:
                raise ValueError("Approved setup evaluation requires PERMIT")
            if self.blockers:
                raise ValueError("Approved setup evaluation cannot contain blockers")
            assert self.candidate is not None
            if (
                self.candidate.source_event_id != self.source_event_id
                or self.candidate.source_event_type is not self.source_event_type
                or self.candidate.source_episode_id != self.source_episode_id
                or self.candidate.setup_family is not self.setup_family
                or self.candidate.side is not self.side
                or self.candidate.structural_result is not self.structural_result
            ):
                raise ValueError("Setup evaluation/candidate authority mismatch")
        elif not self.blockers:
            raise ValueError("Rejected setup evaluation requires blockers")
        return self


class SetupManagerDecision(SetupContractModel):
    symbol: str = Field(min_length=1)
    snapshot_time: datetime
    selected_candidate: Optional[AuthoritativeSetupCandidate]
    supporting_candidate_ids: Tuple[str, ...] = ()
    deferred_candidate_ids: Tuple[str, ...] = ()
    reason_codes: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_manager(self) -> "SetupManagerDecision":
        if self.selected_candidate is not None:
            if self.selected_candidate.symbol != self.symbol.strip().upper():
                raise ValueError("Selected candidate symbol must match manager symbol")
            if self.selected_candidate.snapshot_time != self.snapshot_time:
                raise ValueError("Selected candidate time must match manager time")
        all_ids = (*self.supporting_candidate_ids, *self.deferred_candidate_ids)
        if len(all_ids) != len(set(all_ids)):
            raise ValueError("Manager candidate identities must be unique")
        if (
            self.selected_candidate is not None
            and self.selected_candidate.candidate_id in all_ids
        ):
            raise ValueError("Selected candidate cannot also be supporting/deferred")
        return self


__all__ = [
    "SETUP_CONTRACT_VERSION",
    "AuthoritativeSetupCandidate",
    "SetupEvaluationResult",
    "SetupManagerDecision",
]
