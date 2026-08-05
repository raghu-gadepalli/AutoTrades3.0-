from datetime import date, datetime, timedelta

from enums.auction_engine import (
    AuctionEventType,
    BalanceEpisodeState,
    DirectionalBias,
    DirectionalTransition,
    FreshDirection,
)
from services.auction_engine.directional_state_machine import DirectionalStateMachine
from services.auction_engine.directional_contracts import (
    FreshDirectionalEvidence,
    AuctionMemory,
    AuctionSnapshotProjection,
    DirectionalMemory,
)


def evidence(side: FreshDirection, ts: datetime) -> FreshDirectionalEvidence:
    bias = (
        DirectionalBias.UP
        if side is FreshDirection.UP
        else DirectionalBias.DOWN
        if side is FreshDirection.DOWN
        else DirectionalBias.UNKNOWN
    )
    return FreshDirectionalEvidence(
        side=side,
        candidate_side=bias,
        observed_at=ts,
        trend_direction=bias,
        raw_structure_side=bias,
        slope_direction=bias,
        directional_efficiency=0.7,
        support_facts=("TREND_DIRECTION", "RAW_STRUCTURE"),
        contradict_facts=(),
        reason_codes=(),
    )


def advance(machine, memory, side, ts, *, close=100.0, high=100.5, low=99.5):
    return machine.advance(
        symbol="TEST",
        trading_day=date(2026, 8, 3),
        snapshot_time=ts,
        close=close,
        high=high,
        low=low,
        evidence=evidence(side, ts),
        previous_memory=memory,
        balance_state=BalanceEpisodeState.NONE,
    )


def test_unresolved_never_starts_episode():
    machine = DirectionalStateMachine()
    memory, projection, events = advance(
        machine,
        DirectionalMemory(),
        FreshDirection.UNRESOLVED,
        datetime(2026, 8, 3, 9, 15),
    )
    assert memory.active_episode_id is None
    assert projection.transition is DirectionalTransition.DEFERRED
    assert events == ()


def test_two_fresh_observations_start_episode():
    machine = DirectionalStateMachine()
    ts = datetime(2026, 8, 3, 9, 15)
    memory, _, _ = advance(machine, DirectionalMemory(), FreshDirection.UP, ts)
    memory, projection, events = advance(
        machine, memory, FreshDirection.UP, ts + timedelta(minutes=3)
    )
    assert projection.transition is DirectionalTransition.STARTED
    assert projection.direction is DirectionalBias.UP
    assert [item.event_type for item in events] == [AuctionEventType.DIRECTIONAL_STARTED]


def test_two_opposite_observations_reverse_episode_atomically():
    machine = DirectionalStateMachine()
    ts = datetime(2026, 8, 3, 9, 15)
    memory = DirectionalMemory()
    for index, side in enumerate(
        (FreshDirection.UP, FreshDirection.UP, FreshDirection.DOWN, FreshDirection.DOWN)
    ):
        memory, projection, events = advance(
            machine, memory, side, ts + timedelta(minutes=3 * index)
        )
    assert projection.transition is DirectionalTransition.REVERSED
    assert projection.direction is DirectionalBias.DOWN
    assert projection.previous_episode_id is not None
    assert [item.event_type for item in events] == [AuctionEventType.DIRECTIONAL_REVERSED]


def test_new_snapshot_contract_has_no_version_or_evidence_hash():
    assert "last_evidence_hash" not in AuctionMemory.model_fields
    assert "last_observation_hash" not in AuctionMemory.model_fields
    assert "engine" not in AuctionSnapshotProjection.model_fields
    assert "engine_version" not in AuctionSnapshotProjection.model_fields
    assert "config_version" not in AuctionSnapshotProjection.model_fields
    assert "config_hash" not in AuctionSnapshotProjection.model_fields


def test_start_extreme_covers_entire_confirmation_sequence():
    machine = DirectionalStateMachine()
    ts = datetime(2026, 8, 3, 9, 15)
    memory, _, _ = advance(
        machine,
        DirectionalMemory(),
        FreshDirection.UP,
        ts,
        close=100.0,
        high=103.0,
        low=99.0,
    )
    memory, projection, _ = advance(
        machine,
        memory,
        FreshDirection.UP,
        ts + timedelta(minutes=3),
        close=101.0,
        high=102.0,
        low=100.0,
    )
    assert projection.extreme_price == 103.0
    assert memory.extreme_price == 103.0


def test_reversal_extreme_covers_entire_opposition_sequence():
    machine = DirectionalStateMachine()
    ts = datetime(2026, 8, 3, 9, 15)
    memory = DirectionalMemory()
    memory, _, _ = advance(machine, memory, FreshDirection.UP, ts)
    memory, _, _ = advance(machine, memory, FreshDirection.UP, ts + timedelta(minutes=3))
    memory, _, _ = advance(
        machine,
        memory,
        FreshDirection.DOWN,
        ts + timedelta(minutes=6),
        close=99.0,
        high=100.0,
        low=95.0,
    )
    memory, projection, _ = advance(
        machine,
        memory,
        FreshDirection.DOWN,
        ts + timedelta(minutes=9),
        close=98.0,
        high=99.0,
        low=96.0,
    )
    assert projection.extreme_price == 95.0
    assert memory.extreme_price == 95.0


def test_auction_memory_has_no_embedded_evidence_history():
    assert "evidence_history" not in AuctionMemory.model_fields


def test_unresolved_evidence_can_expose_candidate_side():
    item = FreshDirectionalEvidence(
        side=FreshDirection.UNRESOLVED,
        candidate_side=DirectionalBias.DOWN,
        observed_at=datetime(2026, 8, 3, 9, 45),
        trend_direction=DirectionalBias.DOWN,
        raw_structure_side=DirectionalBias.DOWN,
        slope_direction=DirectionalBias.DOWN,
        directional_efficiency=0.2,
        support_facts=("TREND_DIRECTION", "RAW_STRUCTURE"),
        contradict_facts=("HMA_ORDER",),
        reason_codes=("FRESH_DIRECTION_UNRESOLVED",),
    )
    assert item.candidate_side is DirectionalBias.DOWN


def test_support_after_one_opposition_bar_clears_all_opposition_geometry():
    machine = DirectionalStateMachine()
    ts = datetime(2026, 8, 3, 9, 15)
    memory = DirectionalMemory()
    memory, _, _ = advance(machine, memory, FreshDirection.UP, ts)
    memory, _, _ = advance(machine, memory, FreshDirection.UP, ts + timedelta(minutes=3))
    memory, _, _ = advance(
        machine,
        memory,
        FreshDirection.DOWN,
        ts + timedelta(minutes=6),
        close=99.0,
        high=100.0,
        low=95.0,
    )
    assert memory.opposition_side is DirectionalBias.DOWN
    assert memory.opposition_extreme_price == 95.0

    memory, projection, events = advance(
        machine,
        memory,
        FreshDirection.UP,
        ts + timedelta(minutes=9),
        close=101.0,
        high=102.0,
        low=100.0,
    )

    assert projection.transition is DirectionalTransition.CONTINUED
    assert events == ()
    assert memory.opposition_side is DirectionalBias.UNKNOWN
    assert memory.opposition_started_at is None
    assert memory.opposition_start_price is None
    assert memory.opposition_extreme_price is None
    assert memory.opposition_streak == 0


def test_unresolved_after_one_opposition_bar_clears_all_opposition_geometry():
    machine = DirectionalStateMachine()
    ts = datetime(2026, 8, 3, 9, 15)
    memory = DirectionalMemory()
    memory, _, _ = advance(machine, memory, FreshDirection.UP, ts)
    memory, _, _ = advance(machine, memory, FreshDirection.UP, ts + timedelta(minutes=3))
    memory, _, _ = advance(
        machine,
        memory,
        FreshDirection.DOWN,
        ts + timedelta(minutes=6),
        close=99.0,
        high=100.0,
        low=95.0,
    )
    assert memory.opposition_side is DirectionalBias.DOWN
    assert memory.opposition_extreme_price == 95.0

    memory, projection, events = advance(
        machine,
        memory,
        FreshDirection.UNRESOLVED,
        ts + timedelta(minutes=9),
        close=99.5,
        high=100.5,
        low=98.5,
    )

    assert projection.transition is DirectionalTransition.DEFERRED
    assert events == ()
    assert memory.opposition_side is DirectionalBias.UNKNOWN
    assert memory.opposition_started_at is None
    assert memory.opposition_start_price is None
    assert memory.opposition_extreme_price is None
    assert memory.opposition_streak == 0
