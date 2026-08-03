from __future__ import annotations

from datetime import datetime

import pytest

from enums.auction_engine import (
    AuctionEventType,
    DirectionalBias,
    SetupFamily,
    StructuralPermissionResult,
)
from schemas.snapshot import SnapshotSchema
from services.auction_engine.episode_contracts import AuctionEvent, StructuralSetupPermission
from services.auction_engine.event_driven_setup_engine import (
    EventDrivenSetupEngine,
    EventDrivenSetupManager,
)
from services.auction_engine.setup_event_router import AuthoritativeSetupEventRouter
from tests.unit.test_auction_authority_snapshot import _finalize, _snapshot


TS = datetime(2026, 7, 27, 11, 0)


def _event_snapshot(
    event_type: AuctionEventType,
    family: SetupFamily,
    *,
    direction: DirectionalBias = DirectionalBias.UP,
    result: StructuralPermissionResult = StructuralPermissionResult.PERMIT,
    close: float = 101.2,
    data: dict | None = None,
    episode_id: str = "EPISODE:1",
) -> SnapshotSchema:
    row = _finalize(_snapshot(TS, close=close, direction="UP" if direction is DirectionalBias.UP else "DOWN"))
    payload = row.model_dump(mode="python", by_alias=True)
    lifecycle = payload["auction"]["lifecycle"]
    event_id = f"EVENT:{event_type.value}"
    event = AuctionEvent(
        event_id=event_id,
        event_type=event_type,
        episode_id=episode_id,
        symbol="TEST",
        trading_day=TS.date(),
        event_time=TS,
        direction=direction,
        reason_codes=("TEST_EVENT",),
        data=data or {},
    )
    permission = StructuralSetupPermission(
        setup_family=family,
        result=result,
        source_event_ids=(event_id,),
        source_event_types=(event_type,),
        balance_state=row.auction.lifecycle.balance.current_state,
        reason_codes=("TEST_PERMISSION",),
    )
    lifecycle["events"] = [event.model_dump(mode="python")]
    lifecycle["permissions"] = [permission.model_dump(mode="python")]
    return SnapshotSchema.model_validate(payload)


@pytest.mark.parametrize(
    ("event_type", "family", "direction", "close", "data"),
    (
        (
            AuctionEventType.BALANCE_ESCAPE_STARTED,
            SetupFamily.BREAKOUT_INITIATION,
            DirectionalBias.UP,
            101.2,
            {"frozen_low": 99.0, "frozen_high": 101.0},
        ),
        (
            AuctionEventType.BALANCE_ESCAPE_ACCEPTED,
            SetupFamily.ACCEPTED_BREAKOUT,
            DirectionalBias.UP,
            101.2,
            {"frozen_low": 99.0, "frozen_high": 101.0},
        ),
        (
            AuctionEventType.BALANCE_ESCAPE_FAILED,
            SetupFamily.FAILED_BREAKOUT,
            DirectionalBias.DOWN,
            100.5,
            {"frozen_low": 99.0, "frozen_high": 101.0},
        ),
        (
            AuctionEventType.DIRECTIONAL_CONTINUATION_CONFIRMED,
            SetupFamily.CONTINUATION,
            DirectionalBias.UP,
            102.0,
            {"origin_price": 101.4, "protection_level": 100.8},
        ),
        (
            AuctionEventType.DIRECTIONAL_REACCELERATION_CONFIRMED,
            SetupFamily.REACCELERATION,
            DirectionalBias.UP,
            102.0,
            {"origin_price": 101.4, "protection_level": 100.8},
        ),
        (
            AuctionEventType.DIRECTIONAL_REVERSAL_LEG_ESTABLISHED,
            SetupFamily.REVERSAL,
            DirectionalBias.UP,
            102.0,
            {"origin_price": 100.0},
        ),
    ),
)
def test_all_families_create_only_from_permitted_authoritative_event(
    event_type, family, direction, close, data
) -> None:
    snapshot = _event_snapshot(
        event_type,
        family,
        direction=direction,
        close=close,
        data=data,
    )
    routes = AuthoritativeSetupEventRouter().route(snapshot.auction.lifecycle)
    evaluations = EventDrivenSetupEngine().evaluate(snapshot, routes)
    approved = [item for item in evaluations if item.approved]
    assert len(approved) == 1
    candidate = approved[0].candidate
    assert candidate is not None
    assert candidate.setup_family is family
    assert candidate.source_event_type is event_type
    assert candidate.source_event_id == f"EVENT:{event_type.value}"
    assert candidate.source_episode_id == "EPISODE:1"
    assert candidate.structural_result is StructuralPermissionResult.PERMIT


def test_wait_or_block_never_creates_candidate() -> None:
    snapshot = _event_snapshot(
        AuctionEventType.DIRECTIONAL_REVERSAL_LEG_ESTABLISHED,
        SetupFamily.REVERSAL,
        result=StructuralPermissionResult.BLOCK,
        close=102.0,
        data={"origin_price": 100.0},
    )
    routes = AuthoritativeSetupEventRouter().route(snapshot.auction.lifecycle)
    evaluations = EventDrivenSetupEngine().evaluate(snapshot, routes)
    assert len(evaluations) == 1
    assert evaluations[0].approved is False
    assert evaluations[0].candidate is None
    assert evaluations[0].blockers == ("STRUCTURAL_PERMISSION_BLOCK",)


def test_manager_defers_opposite_authoritative_candidates() -> None:
    up = _event_snapshot(
        AuctionEventType.DIRECTIONAL_REVERSAL_LEG_ESTABLISHED,
        SetupFamily.REVERSAL,
        close=102.0,
        data={"origin_price": 100.0},
    )
    down = _event_snapshot(
        AuctionEventType.BALANCE_ESCAPE_FAILED,
        SetupFamily.FAILED_BREAKOUT,
        direction=DirectionalBias.DOWN,
        close=100.5,
        data={"frozen_low": 99.0, "frozen_high": 101.0},
    )
    engine = EventDrivenSetupEngine()
    up_eval = engine.evaluate(up, AuthoritativeSetupEventRouter().route(up.auction.lifecycle))[0]
    down_eval = engine.evaluate(down, AuthoritativeSetupEventRouter().route(down.auction.lifecycle))[0]
    decision = EventDrivenSetupManager().select(up, (up_eval, down_eval))
    assert decision.selected_candidate is None
    assert set(decision.deferred_candidate_ids) == {
        up_eval.candidate.candidate_id,
        down_eval.candidate.candidate_id,
    }


def test_reversal_prefers_preserved_confirmation_boundary_for_geometry() -> None:
    snapshot = _event_snapshot(
        AuctionEventType.DIRECTIONAL_REVERSAL_LEG_ESTABLISHED,
        SetupFamily.REVERSAL,
        direction=DirectionalBias.DOWN,
        close=98.0,
        data={
            "origin_price": 99.0,
            "reversal_confirmation_level": 100.0,
        },
    )
    routes = AuthoritativeSetupEventRouter().route(snapshot.auction.lifecycle)
    evaluation = EventDrivenSetupEngine().evaluate(snapshot, routes)[0]

    assert evaluation.approved is True
    assert evaluation.candidate is not None
    assert evaluation.candidate.stop_anchor_price == pytest.approx(100.0)
    assert evaluation.candidate.stop_anchor_type == "REVERSAL_CONFIRMATION_LEVEL"
    assert evaluation.candidate.reference_price == pytest.approx(99.0)


def test_reversal_stop_boundary_does_not_replace_entry_freshness_origin() -> None:
    snapshot = _event_snapshot(
        AuctionEventType.DIRECTIONAL_REVERSAL_LEG_ESTABLISHED,
        SetupFamily.REVERSAL,
        direction=DirectionalBias.UP,
        close=104.8,
        data={
            "origin_price": 103.0,
            "reversal_confirmation_level": 100.0,
        },
    )
    routes = AuthoritativeSetupEventRouter().route(snapshot.auction.lifecycle)
    evaluation = EventDrivenSetupEngine().evaluate(snapshot, routes)[0]

    assert evaluation.approved is True
    assert evaluation.candidate is not None
    assert evaluation.candidate.stop_anchor_price == pytest.approx(100.0)
    assert evaluation.candidate.reference_price == pytest.approx(103.0)
