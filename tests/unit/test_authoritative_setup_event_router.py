from __future__ import annotations

from datetime import datetime

from enums.auction_engine import (
    AuctionEventType,
    BalanceEpisodeState,
    DirectionalBias,
    SetupEventAction,
    SetupFamily,
    StructuralPermissionResult,
)
from services.auction_engine.episode_contracts import AuctionEvent
from services.auction_engine.setup_event_router import AuthoritativeSetupEventRouter
from services.auction_engine.structural_permissions import StructuralPermissionMatrix


TS = datetime(2026, 8, 3, 10, 12)


def _event(event_type: AuctionEventType, direction: DirectionalBias) -> AuctionEvent:
    episode_id = f"DIR-TEST-20260803-0002-{direction.value}"
    return AuctionEvent(
        event_id=f"EVT-TEST-{TS:%Y%m%d%H%M%S}-{event_type.value}-{episode_id}",
        event_type=event_type,
        episode_id=episode_id,
        symbol="TEST",
        trading_day=TS.date(),
        event_time=TS,
        direction=direction,
        reason_codes=("TEST_EVENT",),
        data={},
    )


def test_current_creation_events_include_balance_escape_and_directional_reversal() -> None:
    creation = AuthoritativeSetupEventRouter.creation_event_types()

    assert AuctionEventType.BALANCE_ESCAPE_STARTED in creation[
        SetupFamily.BREAKOUT_INITIATION
    ]
    assert AuctionEventType.BALANCE_ESCAPE_ACCEPTED in creation[
        SetupFamily.ACCEPTED_BREAKOUT
    ]
    assert AuctionEventType.BALANCE_ESCAPE_FAILED in creation[
        SetupFamily.FAILED_BREAKOUT
    ]
    assert AuctionEventType.DIRECTIONAL_REVERSED in creation[SetupFamily.REVERSAL]


def test_router_preserves_current_reversal_event_identity_and_permission() -> None:
    event = _event(AuctionEventType.DIRECTIONAL_REVERSED, DirectionalBias.DOWN)
    permissions = StructuralPermissionMatrix().evaluate(
        balance_state=BalanceEpisodeState.NONE,
        events=(event,),
    )

    routes = AuthoritativeSetupEventRouter().route_authority(
        events=(event,),
        permissions=permissions,
    )
    evaluate = [
        route
        for route in routes
        if route.setup_family is SetupFamily.REVERSAL
        and route.action is SetupEventAction.EVALUATE
    ]

    assert len(evaluate) == 1
    route = evaluate[0]
    assert route.source_event_id == event.event_id
    assert route.source_episode_id == event.episode_id
    assert route.direction is DirectionalBias.DOWN
    assert route.structural_result is StructuralPermissionResult.PERMIT
