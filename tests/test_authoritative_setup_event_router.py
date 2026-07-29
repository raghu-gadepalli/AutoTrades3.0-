from __future__ import annotations

from enums.auction_engine import (
    AuctionEventType,
    AuctionStateName,
    DirectionalBias,
    SetupEventAction,
    SetupFamily,
    StructuralPermissionResult,
)
from services.auction_engine.episode_engine import PersistentEpisodeEngine
from services.auction_engine.setup_event_router import AuthoritativeSetupEventRouter
from tests.test_persistent_episode_engine import _observation


def test_every_setup_family_has_creation_capable_authoritative_event() -> None:
    creation = AuthoritativeSetupEventRouter.creation_event_types()

    assert set(creation) == set(SetupFamily)
    assert all(creation[family] for family in SetupFamily)
    assert AuctionEventType.BALANCE_ESCAPE_STARTED in creation[
        SetupFamily.BREAKOUT_INITIATION
    ]
    assert AuctionEventType.DIRECTIONAL_CONTINUATION_CONFIRMED in creation[
        SetupFamily.CONTINUATION
    ]
    assert AuctionEventType.DIRECTIONAL_REACCELERATION_CONFIRMED in creation[
        SetupFamily.REACCELERATION
    ]


def test_router_preserves_event_identity_and_structural_permission() -> None:
    engine = PersistentEpisodeEngine()
    engine.advance(_observation(0))
    engine.advance(_observation(1))
    engine.advance(
        _observation(
            2,
            observation_state=AuctionStateName.CONTROLLED_PULLBACK,
            trend_direction=DirectionalBias.UP,
            directional_bias=DirectionalBias.UP,
        )
    )
    lifecycle = engine.advance(
        _observation(
            3,
            close=100.8,
            observation_state=AuctionStateName.REACCELERATION,
            trend_direction=DirectionalBias.UP,
            directional_bias=DirectionalBias.UP,
        )
    )

    routes = AuthoritativeSetupEventRouter().route(lifecycle)
    continuation_routes = [
        route
        for route in routes
        if route.setup_family is SetupFamily.CONTINUATION
        and route.action is SetupEventAction.EVALUATE
    ]

    assert len(continuation_routes) == 1
    route = continuation_routes[0]
    event = next(
        event
        for event in lifecycle.events
        if event.event_type
        is AuctionEventType.DIRECTIONAL_CONTINUATION_CONFIRMED
    )
    assert route.source_event_id == event.event_id
    assert route.source_episode_id == event.episode_id
    assert route.direction is event.direction
    assert route.structural_result is StructuralPermissionResult.PERMIT
