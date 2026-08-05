from datetime import date, datetime

import pytest

from enums.auction_engine import (
    AuctionEventType,
    BalanceEpisodeState,
    DirectionalBias,
)
from services.auction_engine.episode_contracts import AuctionEvent
from services.auction_engine.structural_permissions import StructuralPermissionMatrix


def _reversal_event() -> AuctionEvent:
    return AuctionEvent(
        event_id="EVT:REVERSAL",
        event_type=AuctionEventType.DIRECTIONAL_REVERSED,
        symbol="TEST",
        trading_day=date(2026, 8, 3),
        event_time=datetime(2026, 8, 3, 10, 0),
        episode_id="DIR:2",
        direction=DirectionalBias.DOWN,
        reason_codes=("TEST",),
        data={"previous_episode_id": "DIR:1"},
    )


def test_missing_persisted_reversal_permission_is_rejected() -> None:
    event = _reversal_event()
    matrix = StructuralPermissionMatrix()

    with pytest.raises(
        ValueError,
        match="Persisted Auction permissions do not match authoritative events",
    ):
        matrix.validate_persisted(
            balance_state=BalanceEpisodeState.NONE,
            events=(event,),
            permissions=(),
        )


def test_generated_reversal_permission_is_valid_persisted_projection() -> None:
    event = _reversal_event()
    matrix = StructuralPermissionMatrix()
    permissions = matrix.evaluate(
        balance_state=BalanceEpisodeState.NONE,
        events=(event,),
    )

    matrix.validate_persisted(
        balance_state=BalanceEpisodeState.NONE,
        events=(event,),
        permissions=permissions,
    )
