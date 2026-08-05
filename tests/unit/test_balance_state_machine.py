from __future__ import annotations

from datetime import datetime, timedelta

from enums.auction_engine import AuctionEventType, BalanceEpisodeState, DirectionalBias
from services.auction_engine.balance_state_machine import (
    BalanceObservation,
    BalanceStateMachine,
)
from services.auction_engine.engine import AuctionEngine


START = datetime(2026, 8, 3, 9, 15)


def _observation(index: int, *, close: float = 100.0) -> BalanceObservation:
    ts = START + timedelta(minutes=index * 3)
    return BalanceObservation(
        symbol="TEST",
        trading_day=START.date(),
        snapshot_time=ts,
        close=close,
        high=close + 0.2,
        low=close - 0.2,
        atr=1.0,
        accepted_range_id="RANGE-1",
        accepted_range_low=99.0,
        accepted_range_high=101.0,
        accepted_range_provisional=False,
        accepted_range_breakout_eligible=True,
        accepted_range_inside=99.0 <= close <= 101.0,
        directional_efficiency=0.20,
        overlap_ratio=0.75,
        range_width_atr=2.0,
    )


def _initial_memory():
    return AuctionEngine.initial_memory("TEST", START).balance


def test_balance_locks_after_accumulated_containment() -> None:
    machine = BalanceStateMachine()
    memory = _initial_memory()
    emitted = []

    for index in range(8):
        memory, projection, events = machine.advance(
            previous_memory=memory,
            observation=_observation(index),
        )
        emitted.extend(event.event_type for event in events)

    assert projection.current_state is BalanceEpisodeState.LOCKED
    assert projection.frozen_low == 99.0
    assert projection.frozen_high == 101.0
    assert AuctionEventType.BALANCE_FORMING_STARTED in emitted
    assert AuctionEventType.BALANCE_PROBABLE in emitted
    assert AuctionEventType.BALANCE_LOCKED in emitted


def test_balance_escape_acceptance_preserves_event_lineage() -> None:
    machine = BalanceStateMachine()
    memory = _initial_memory()
    event_types = []

    for index in range(8):
        memory, _, events = machine.advance(
            previous_memory=memory,
            observation=_observation(index),
        )
        event_types.extend(event.event_type for event in events)

    memory, watch, events = machine.advance(
        previous_memory=memory,
        observation=_observation(8, close=101.30),
    )
    event_types.extend(event.event_type for event in events)
    assert watch.current_state is BalanceEpisodeState.ESCAPE_WATCH
    assert watch.escape_direction is DirectionalBias.UP

    memory, accepted, events = machine.advance(
        previous_memory=memory,
        observation=_observation(9, close=101.40),
    )
    event_types.extend(event.event_type for event in events)
    assert accepted.current_state is BalanceEpisodeState.ACCEPTED_OUTSIDE

    memory, completed, events = machine.advance(
        previous_memory=memory,
        observation=_observation(10, close=101.45),
    )
    event_types.extend(event.event_type for event in events)
    assert completed.current_state is BalanceEpisodeState.COMPLETED
    assert event_types[-3:] == [
        AuctionEventType.BALANCE_ESCAPE_STARTED,
        AuctionEventType.BALANCE_ESCAPE_ACCEPTED,
        AuctionEventType.BALANCE_COMPLETED,
    ]
