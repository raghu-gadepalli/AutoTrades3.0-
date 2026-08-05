from datetime import date, datetime
from types import SimpleNamespace

from enums.auction_engine import (
    AuctionEventType,
    BalanceEpisodeState,
    DirectionalBias,
    SetupEventAction,
    SetupFamily,
    StructuralPermissionResult,
    TradeSide,
)
from services.auction_engine.episode_contracts import AuctionEvent
from services.auction_engine.event_driven_setup_engine import EventDrivenSetupEngine
from services.auction_engine.setup_event_router import AuthoritativeSetupEventRouter
from services.auction_engine.structural_permissions import StructuralPermissionMatrix


TS = datetime(2026, 8, 3, 10, 0)


def _event(
    event_type: AuctionEventType,
    *,
    direction: DirectionalBias = DirectionalBias.UP,
    data: dict | None = None,
) -> AuctionEvent:
    return AuctionEvent(
        event_id=f"EVT:{event_type.value}",
        event_type=event_type,
        symbol="TEST",
        trading_day=date(2026, 8, 3),
        event_time=TS,
        episode_id="DIR:1",
        direction=direction,
        reason_codes=("TEST",),
        data=data or {},
    )


def test_directional_reversed_uses_auction_permission_matrix() -> None:
    event = _event(AuctionEventType.DIRECTIONAL_REVERSED)
    permissions = StructuralPermissionMatrix().evaluate(
        balance_state=BalanceEpisodeState.NONE,
        events=(event,),
    )
    reversal = next(
        item for item in permissions if item.setup_family is SetupFamily.REVERSAL
    )
    assert reversal.result is StructuralPermissionResult.PERMIT
    assert reversal.source_event_ids == (event.event_id,)


def test_directional_end_closes_directional_setup_windows() -> None:
    event = _event(AuctionEventType.DIRECTIONAL_ENDED)
    routes = AuthoritativeSetupEventRouter().route_authority(
        events=(event,),
        permissions=(),
    )
    assert {route.setup_family for route in routes} == {
        SetupFamily.REVERSAL,
        SetupFamily.CONTINUATION,
        SetupFamily.REACCELERATION,
    }
    assert {route.action for route in routes} == {SetupEventAction.CLOSE}


def test_balance_state_can_block_same_snapshot_reversal() -> None:
    event = _event(AuctionEventType.DIRECTIONAL_REVERSED)
    permissions = StructuralPermissionMatrix().evaluate(
        balance_state=BalanceEpisodeState.ESCAPE_WATCH,
        events=(event,),
    )
    routes = AuthoritativeSetupEventRouter().route_authority(
        events=(event,),
        permissions=permissions,
    )
    evaluate_routes = [
        route for route in routes if route.action is SetupEventAction.EVALUATE
    ]
    invalidate_routes = [
        route for route in routes if route.action is SetupEventAction.INVALIDATE
    ]
    assert len(evaluate_routes) == 1
    assert evaluate_routes[0].setup_family is SetupFamily.REVERSAL
    assert evaluate_routes[0].structural_result is StructuralPermissionResult.BLOCK
    assert {route.setup_family for route in invalidate_routes} == set(SetupFamily)


def test_reversal_geometry_uses_confirmation_bars() -> None:
    event = _event(
        AuctionEventType.DIRECTIONAL_REVERSED,
        direction=DirectionalBias.UP,
        data={"start_price": 100.0},
    )
    snapshot = SimpleNamespace(
        close=102.0,
        memory=SimpleNamespace(
            structure=SimpleNamespace(
                bars_3m=(
                    SimpleNamespace(low=98.0, high=101.0),
                    SimpleNamespace(low=99.0, high=103.0),
                )
            )
        ),
    )
    geometry = EventDrivenSetupEngine()._geometry(
        snapshot,
        SetupFamily.REVERSAL,
        event,
        TradeSide.BUY,
        2.0,
    )
    assert geometry["blockers"] == []
    assert geometry["stop"] == 98.0
    assert geometry["target"] == 108.0
    assert geometry["reference"] == 100.0


def test_low_implicit_reward_risk_is_not_a_generic_rejection() -> None:
    event = _event(
        AuctionEventType.BALANCE_ESCAPE_STARTED,
        direction=DirectionalBias.UP,
        data={"frozen_low": 99.0, "frozen_high": 100.0},
    )
    permission = StructuralPermissionMatrix().evaluate(
        balance_state=BalanceEpisodeState.ESCAPE_WATCH,
        events=(event,),
    )
    route = AuthoritativeSetupEventRouter().route_authority(
        events=(event,),
        permissions=permission,
    )[0]
    snapshot = SimpleNamespace(
        symbol="TEST",
        snapshot_time=TS,
        close=100.6,
        indicators=SimpleNamespace(atr=SimpleNamespace(value=1.0)),
    )
    result = EventDrivenSetupEngine()._evaluate_route(snapshot, route, event)
    assert result.approved is True
    assert result.candidate is not None
    assert result.candidate.entry_price == 100.6
    assert result.candidate.stop_anchor_price == 99.85
    assert result.candidate.target_reference_price == 101.0
    assert "REWARD_RISK_BELOW_1" not in result.blockers
    payload = result.candidate.model_dump(mode="python")
    assert "risk_points" not in payload
    assert "expected_move_points" not in payload
    assert "expected_move_pct" not in payload
    assert "reward_risk" not in payload
