"""Read-only adapter from Auction authority to existing setup evaluation.

The adapter does not rediscover structure, fabricate permission, create opportunities,
signals or trades, or mutate snapshots.  It routes the Auction event and
permission projection into the existing event-driven setup evaluator and manager.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional, Tuple

from pydantic import Field, model_validator

from enums.auction_engine import (
    AuctionEventType,
    DirectionalBias,
    SetupEventAction,
    SetupFamily,
    StructuralPermissionResult,
    TradeSide,
)
from schemas.snapshot import SnapshotSchema
from services.auction_engine.contracts import ContractModel
from services.auction_engine.event_driven_setup_engine import (
    EventDrivenSetupEngine,
    EventDrivenSetupManager,
)
from services.auction_engine.setup_event_router import AuthoritativeSetupEventRouter
from services.auction_engine.structural_permissions import StructuralPermissionMatrix


class SetupEvaluationStatus(str, Enum):
    APPROVED = "APPROVED"
    DEFERRED = "DEFERRED"
    REJECTED = "REJECTED"
    UNASSESSABLE = "UNASSESSABLE"


class SetupEvaluation(ContractModel):
    symbol: str = Field(min_length=1)
    snapshot_time: datetime
    source_event_id: str = Field(min_length=1)
    source_event_type: AuctionEventType
    source_episode_id: str = Field(min_length=1)
    setup_family: SetupFamily
    side: TradeSide
    status: SetupEvaluationStatus
    structural_result: Optional[StructuralPermissionResult] = None
    selected: bool = False
    candidate_id: Optional[str] = None
    entry_price: Optional[float] = Field(default=None, gt=0.0)
    stop_price: Optional[float] = Field(default=None, gt=0.0)
    target_price: Optional[float] = Field(default=None, gt=0.0)
    reference_price: Optional[float] = Field(default=None, gt=0.0)
    blockers: Tuple[str, ...] = ()
    reason_codes: Tuple[str, ...] = ()
    manager_reason_codes: Tuple[str, ...] = ()
    permission_source: str = "SNAPSHOT_PERSISTED"
    stored_permission_match: bool = True

    @model_validator(mode="after")
    def _validate_result(self) -> "SetupEvaluation":
        if self.status is SetupEvaluationStatus.APPROVED:
            if self.structural_result is not StructuralPermissionResult.PERMIT:
                raise ValueError("Approved evaluation requires PERMIT")
            if self.candidate_id is None:
                raise ValueError("Approved evaluation requires candidate_id")
            if self.blockers:
                raise ValueError("Approved evaluation cannot contain blockers")
        elif not self.blockers:
            raise ValueError("Non-approved evaluation requires blockers")
        if self.selected and self.status is not SetupEvaluationStatus.APPROVED:
            raise ValueError("Only an approved setup may be selected")
        return self


class SetupEvaluator:
    """Adapt Auction events to the existing read-only setup engine."""

    def __init__(self) -> None:
        self.permission_matrix = StructuralPermissionMatrix()
        self.router = AuthoritativeSetupEventRouter()
        self.engine = EventDrivenSetupEngine()
        self.manager = EventDrivenSetupManager()

    def evaluate(self, snapshot: SnapshotSchema) -> Tuple[SetupEvaluation, ...]:
        if not isinstance(snapshot, SnapshotSchema):
            raise TypeError("SetupEvaluator requires SnapshotSchema")
        if snapshot.auction.status != "OK":
            raise ValueError("Setup evaluation requires Auction status OK")

        permissions = tuple(snapshot.auction.permissions)
        self.permission_matrix.validate_persisted(
            balance_state=snapshot.auction.balance.current_state,
            events=snapshot.auction.events,
            permissions=permissions,
        )
        stored_permission_match = True
        routes = self.router.route_authority(
            events=snapshot.auction.events,
            permissions=permissions,
        )
        evaluate_routes = tuple(
            route for route in routes if route.action is SetupEventAction.EVALUATE
        )
        raw_results = []
        failures: List[SetupEvaluation] = []
        for route in evaluate_routes:
            try:
                result = self.engine.evaluate(snapshot, (route,))[0]
            except Exception as exc:
                failures.append(
                    SetupEvaluation(
                        symbol=snapshot.symbol.strip().upper(),
                        snapshot_time=snapshot.snapshot_time,
                        source_event_id=route.source_event_id,
                        source_event_type=route.source_event_type,
                        source_episode_id=route.source_episode_id,
                        setup_family=route.setup_family,
                        side=self._trade_side(route.direction),
                        status=SetupEvaluationStatus.UNASSESSABLE,
                        structural_result=route.structural_result,
                        blockers=(f"EVALUATION_ERROR:{type(exc).__name__}",),
                        reason_codes=(str(exc),),
                        stored_permission_match=stored_permission_match,
                    )
                )
                continue
            raw_results.append(result)

        manager = self.manager.select(snapshot, raw_results)
        selected_id = (
            manager.selected_candidate.candidate_id
            if manager.selected_candidate is not None
            else None
        )
        output: List[SetupEvaluation] = []
        for result in raw_results:
            candidate = result.candidate
            if result.approved:
                status = SetupEvaluationStatus.APPROVED
                blockers: Tuple[str, ...] = ()
            elif result.structural_result is StructuralPermissionResult.WAIT:
                status = SetupEvaluationStatus.DEFERRED
                blockers = result.blockers or ("STRUCTURAL_PERMISSION_WAIT",)
            else:
                status = SetupEvaluationStatus.REJECTED
                blockers = result.blockers
            output.append(
                SetupEvaluation(
                    symbol=snapshot.symbol.strip().upper(),
                    snapshot_time=snapshot.snapshot_time,
                    source_event_id=result.source_event_id,
                    source_event_type=result.source_event_type,
                    source_episode_id=result.source_episode_id,
                    setup_family=result.setup_family,
                    side=result.side,
                    status=status,
                    structural_result=result.structural_result,
                    selected=bool(
                        candidate is not None and candidate.candidate_id == selected_id
                    ),
                    candidate_id=(candidate.candidate_id if candidate is not None else None),
                    entry_price=(candidate.entry_price if candidate is not None else None),
                    stop_price=(candidate.stop_anchor_price if candidate is not None else None),
                    target_price=(
                        candidate.target_reference_price if candidate is not None else None
                    ),
                    reference_price=(candidate.reference_price if candidate is not None else None),
                    blockers=blockers,
                    reason_codes=result.reason_codes,
                    manager_reason_codes=manager.reason_codes,
                    stored_permission_match=stored_permission_match,
                )
            )
        output.extend(failures)
        return tuple(output)

    @staticmethod
    def _trade_side(direction: DirectionalBias) -> TradeSide:
        if direction is DirectionalBias.UP:
            return TradeSide.BUY
        if direction is DirectionalBias.DOWN:
            return TradeSide.SELL
        return TradeSide.NONE


__all__ = [
    "SetupEvaluationStatus",
    "SetupEvaluation",
    "SetupEvaluator",
]
