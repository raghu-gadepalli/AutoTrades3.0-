from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG, AuctionEngineConfig
from enums.auction_engine import (
    AuctionEventType,
    AuctionStateName,
    BalanceEpisodeState,
    DirectionalBias,
    DirectionalEfficiencySource,
    DirectionalEpisodeOrigin,
    DirectionalEpisodeState,
    SetupFamily,
    StructuralPermissionResult,
)
from services.auction_engine.episode_contracts import AuctionObservation
from services.auction_engine.episode_engine import (
    EpisodeChronologyError,
    PersistentEpisodeEngine,
)


DAY = date(2026, 7, 27)
START = datetime(2026, 7, 27, 9, 15)


def _permission_results(projection) -> dict[SetupFamily, StructuralPermissionResult]:
    return {item.setup_family: item.result for item in projection.permissions}


def _observation(
    index: int,
    *,
    close: float = 100.0,
    high: float | None = None,
    low: float | None = None,
    observation_state: AuctionStateName = AuctionStateName.ORDERLY_UPTREND,
    trend_direction: DirectionalBias = DirectionalBias.UP,
    directional_bias: DirectionalBias = DirectionalBias.UP,
    current_leg_mature: bool = False,
    extension_mature: bool = False,
    exhaustion_active: bool = False,
    exhausted_side: DirectionalBias = DirectionalBias.UNKNOWN,
    rejection: bool = False,
    failed_extreme: bool = False,
    structural_failure: bool = False,
        protection: float | None = 99.0,
    accepted_range: bool = False,
    accepted_inside: bool = False,
    accepted_range_id: str = "RANGE:TEST",
    accepted_range_low: float = 99.0,
    accepted_range_high: float = 101.0,
    efficiency: float | None = 0.20,
    overlap: float | None = 0.70,
) -> AuctionObservation:
    ts = START + timedelta(minutes=3 * index)
    range_id = accepted_range_id if accepted_range else None
    range_low = accepted_range_low if accepted_range else None
    range_high = accepted_range_high if accepted_range else None
    return AuctionObservation(
        symbol="TEST",
        trading_day=DAY,
        snapshot_time=ts,
        close=close,
        high=(high if high is not None else max(close + 0.20, 100.20)),
        low=(low if low is not None else min(close - 0.20, 99.80)),
        atr=1.0,
        observation_state=observation_state,
        directional_bias=directional_bias,
        trend_direction=trend_direction,
        current_leg_mature=current_leg_mature,
        extension_mature=extension_mature,
        exhaustion_active=exhaustion_active,
        exhausted_side=exhausted_side,
        rejection_observed=rejection,
        failed_extreme_observed=failed_extreme,
        structural_failure_confirmed=structural_failure,
        trend_protection_level=protection,
        trend_protection_source="TEST_STRUCTURE" if protection is not None else "",
        trend_protection_time=ts if protection is not None else None,
        accepted_range_id=range_id,
        accepted_range_low=range_low,
        accepted_range_high=range_high,
        accepted_range_established_at=START if accepted_range else None,
        accepted_range_provisional=False,
        accepted_range_breakout_eligible=accepted_range,
        accepted_range_inside=accepted_inside,
        accepted_range_position=0.50 if accepted_inside else None,
        accepted_range_outside_atr=None,
        range_width_atr=(
            accepted_range_high - accepted_range_low
            if accepted_range
            else None
        ),
        directional_efficiency=efficiency,
        directional_efficiency_source=(
            DirectionalEfficiencySource.PRICE_ACTION
            if efficiency is not None
            else DirectionalEfficiencySource.NONE
        ),
        overlap_ratio=overlap,
        source_reason_codes=(),
    )


def _lock_balance(
    engine: PersistentEpisodeEngine,
    *,
    start_index: int = 0,
    range_id: str = "RANGE:TEST",
    low: float = 99.0,
    high: float = 101.0,
):
    projection = None
    for index in range(start_index, start_index + 8):
        projection = engine.advance(
            _observation(
                index,
                observation_state=AuctionStateName.BALANCE,
                trend_direction=DirectionalBias.UNKNOWN,
                directional_bias=DirectionalBias.NEUTRAL,
                protection=None,
                accepted_range=True,
                accepted_inside=True,
                accepted_range_id=range_id,
                accepted_range_low=low,
                accepted_range_high=high,
            )
        )
    assert projection is not None
    assert projection.balance.current_state is BalanceEpisodeState.LOCKED
    return projection


def test_episode_observation_accepts_unbounded_range_position() -> None:
    above = _observation(0, accepted_range=True, accepted_inside=False).model_copy(
        update={"accepted_range_position": 1.4090909090909138}
    )
    below = _observation(1, accepted_range=True, accepted_inside=False).model_copy(
        update={"accepted_range_position": -0.25}
    )

    validated_above = AuctionObservation.model_validate(above.model_dump())
    validated_below = AuctionObservation.model_validate(below.model_dump())

    assert validated_above.accepted_range_position == pytest.approx(1.4090909090909138)
    assert validated_below.accepted_range_position == pytest.approx(-0.25)


def test_episode_observation_range_position_requires_geometry() -> None:
    payload = _observation(0).model_dump()
    payload["accepted_range_position"] = 1.2

    with pytest.raises(
        ValueError,
        match="accepted_range_position requires accepted range geometry",
    ):
        AuctionObservation.model_validate(payload)



def test_auction_observation_requires_efficiency_source_alignment() -> None:
    payload = _observation(0).model_dump(mode="python")
    payload["directional_efficiency"] = None
    payload["directional_efficiency_source"] = (
        DirectionalEfficiencySource.PRICE_ACTION
    )

    with pytest.raises(
        ValueError,
        match="Missing directional efficiency requires source NONE",
    ):
        AuctionObservation.model_validate(payload)


def test_directional_start_threshold_is_config_driven() -> None:
    payload = AUCTION_ENGINE_CONFIG.resolved_dict()
    payload["episode"]["directional"]["start_confirmation_bars"] = 1
    config = AuctionEngineConfig.model_validate(payload)
    engine = PersistentEpisodeEngine(config)

    started = engine.advance(_observation(0))

    assert started.directional.current_state is DirectionalEpisodeState.DIRECTIONAL
    assert [event.event_type for event in started.events] == [
        AuctionEventType.DIRECTIONAL_STARTED
    ]


def test_directional_episode_emits_one_reversal_event_and_completes() -> None:
    engine = PersistentEpisodeEngine()

    first = engine.advance(_observation(0))
    assert first.directional.current_state is DirectionalEpisodeState.NONE

    started = engine.advance(_observation(1))
    assert started.directional.current_state is DirectionalEpisodeState.DIRECTIONAL
    assert [event.event_type for event in started.events] == [
        AuctionEventType.DIRECTIONAL_STARTED
    ]

    mature = engine.advance(_observation(2, current_leg_mature=True))
    assert mature.directional.current_state is DirectionalEpisodeState.MATURE

    watch = engine.advance(
        _observation(
            3,
            current_leg_mature=True,
            exhaustion_active=True,
            exhausted_side=DirectionalBias.UP,
            rejection=True,
        )
    )
    assert watch.directional.current_state is DirectionalEpisodeState.REVERSAL_WATCH
    assert watch.directional.rejection_seen is True
    assert watch.directional.continuation_failure_seen is True
    assert watch.directional.continuation_failure_time == START + timedelta(minutes=9)
    assert watch.directional.reversal_confirmation_level == pytest.approx(99.80)
    assert watch.directional.reversal_confirmation_source == (
        "OBSERVATION_REJECTION_OR_FAILED_EXTREME:BAR_LOW"
    )

    failure_stage = engine.advance(
        _observation(
            4,
            close=99.90,
            current_leg_mature=True,
            exhaustion_active=True,
            exhausted_side=DirectionalBias.UP,
            rejection=True,
            structural_failure=True,
        )
    )
    assert failure_stage.directional.current_state is DirectionalEpisodeState.REVERSAL_WATCH
    assert failure_stage.directional.continuation_failure_seen is True
    assert failure_stage.directional.reversal_confirmation_breach_closes == 0

    confirmed = engine.advance(
        _observation(
            5,
            close=99.50,
            observation_state=AuctionStateName.TREND_FAILURE,
            current_leg_mature=True,
            exhaustion_active=True,
            exhausted_side=DirectionalBias.UP,
            rejection=True,
            structural_failure=True,
        )
    )
    assert confirmed.directional.current_state is DirectionalEpisodeState.REVERSAL_LEG
    assert confirmed.directional.direction is DirectionalBias.DOWN
    assert (
        confirmed.directional.origin_source
        is DirectionalEpisodeOrigin.REVERSAL_EVENT_HANDOFF
    )
    reversal_events = [
        event
        for event in confirmed.events
        if event.event_type is AuctionEventType.DIRECTIONAL_REVERSAL_CONFIRMED
    ]
    assert len(reversal_events) == 1
    assert reversal_events[0].direction is DirectionalBias.DOWN
    assert confirmed.directional.parent_episode_id == reversal_events[0].episode_id
    assert confirmed.directional.origin_event_id == reversal_events[0].event_id
    assert reversal_events[0].data["reversal_confirmation_level"] == pytest.approx(99.80)
    assert _permission_results(confirmed)[SetupFamily.REVERSAL] is (
        StructuralPermissionResult.WAIT
    )

    first_leg_bar = engine.advance(
        _observation(
            6,
            close=98.90,
            observation_state=AuctionStateName.REVERSAL,
            trend_direction=DirectionalBias.UNKNOWN,
            directional_bias=DirectionalBias.DOWN,
            structural_failure=True,
        )
    )
    assert first_leg_bar.directional.current_state is DirectionalEpisodeState.REVERSAL_LEG
    assert first_leg_bar.directional.reversal_leg_progress_bars == 1

    established = engine.advance(
        _observation(
            7,
            close=98.80,
            observation_state=AuctionStateName.REVERSAL,
            trend_direction=DirectionalBias.UNKNOWN,
            directional_bias=DirectionalBias.DOWN,
            structural_failure=True,
        )
    )
    assert established.directional.current_state is DirectionalEpisodeState.DIRECTIONAL
    assert established.directional.direction is DirectionalBias.DOWN
    assert AuctionEventType.DIRECTIONAL_REVERSAL_LEG_ESTABLISHED in {
        event.event_type for event in established.events
    }
    assert not [
        event
        for event in established.events
        if event.event_type is AuctionEventType.DIRECTIONAL_REVERSAL_CONFIRMED
    ]


def test_trend_restoration_is_event_and_returns_directly_to_directional() -> None:
    engine = PersistentEpisodeEngine()
    engine.advance(_observation(0))
    engine.advance(_observation(1))
    engine.advance(_observation(2, current_leg_mature=True))
    watch = engine.advance(
        _observation(
            3,
            current_leg_mature=True,
            exhaustion_active=True,
            exhausted_side=DirectionalBias.UP,
            rejection=True,
        )
    )
    assert watch.directional.current_state is DirectionalEpisodeState.REVERSAL_WATCH

    first_restore_bar = engine.advance(
        _observation(4, close=101.0, observation_state=AuctionStateName.ORDERLY_UPTREND)
    )
    assert first_restore_bar.directional.current_state is DirectionalEpisodeState.REVERSAL_WATCH

    restored = engine.advance(
        _observation(5, close=102.0, observation_state=AuctionStateName.ORDERLY_UPTREND)
    )
    assert restored.directional.current_state is DirectionalEpisodeState.DIRECTIONAL
    assert AuctionEventType.DIRECTIONAL_TREND_RESTORED in {
        event.event_type for event in restored.events
    }
    assert restored.directional.reversal_confirmation_level is None


def test_directional_episode_tightens_protection_by_level_without_version_metadata() -> None:
    engine = PersistentEpisodeEngine()

    engine.advance(_observation(0, protection=99.0))
    started = engine.advance(
        _observation(1, protection=99.0)
    )
    assert started.directional.current_state is DirectionalEpisodeState.DIRECTIONAL
    assert started.directional.protection_level == pytest.approx(99.0)

    tightened_level = engine.advance(
        _observation(2, protection=99.5)
    )
    assert tightened_level.directional.protection_level == pytest.approx(99.5)
    assert tightened_level.directional.protection_source == "TEST_STRUCTURE"

    weaker_level_is_still_rejected = engine.advance(
        _observation(3, protection=99.25)
    )
    assert weaker_level_is_still_rejected.directional.protection_level == pytest.approx(99.5)


def test_balance_forming_merges_compatible_ranges_before_freezing() -> None:
    engine = PersistentEpisodeEngine()
    forming = engine.advance(
        _observation(
            0,
            observation_state=AuctionStateName.BALANCE,
            trend_direction=DirectionalBias.UNKNOWN,
            directional_bias=DirectionalBias.NEUTRAL,
            protection=None,
            accepted_range=True,
            accepted_inside=True,
            accepted_range_id="RANGE:A",
            accepted_range_low=99.0,
            accepted_range_high=101.0,
        )
    )
    assert forming.balance.current_state is BalanceEpisodeState.FORMING
    assert forming.balance.frozen_low is None
    assert forming.balance.candidate_low == pytest.approx(99.0)

    merged = engine.advance(
        _observation(
            1,
            observation_state=AuctionStateName.BALANCE,
            trend_direction=DirectionalBias.UNKNOWN,
            directional_bias=DirectionalBias.NEUTRAL,
            protection=None,
            accepted_range=True,
            accepted_inside=True,
            accepted_range_id="RANGE:B",
            accepted_range_low=98.9,
            accepted_range_high=101.1,
        )
    )
    assert merged.balance.candidate_low == pytest.approx(98.9)
    assert merged.balance.candidate_high == pytest.approx(101.1)
    assert merged.balance.source_range_ids == ("RANGE:A", "RANGE:B")
    assert merged.balance.candidate_merge_count == 1

    for index in range(2, 8):
        locked = engine.advance(
            _observation(
                index,
                observation_state=AuctionStateName.BALANCE,
                trend_direction=DirectionalBias.UNKNOWN,
                directional_bias=DirectionalBias.NEUTRAL,
                protection=None,
                accepted_range=False,
                accepted_inside=False,
            )
        )

    assert locked.balance.current_state is BalanceEpisodeState.LOCKED
    assert locked.balance.frozen_low == pytest.approx(98.9)
    assert locked.balance.frozen_high == pytest.approx(101.1)
    assert locked.balance.range_id == "RANGE:A"


def test_balance_locks_with_persistence_and_accepts_two_close_escape() -> None:
    engine = PersistentEpisodeEngine()
    evaluations = []
    for index in range(8):
        evaluations.append(
            engine.advance(
                _observation(
                    index,
                    observation_state=AuctionStateName.BALANCE,
                    trend_direction=DirectionalBias.UNKNOWN,
                    directional_bias=DirectionalBias.NEUTRAL,
                    protection=None,
                    accepted_range=True,
                    accepted_inside=True,
                )
            )
        )

    assert evaluations[-1].balance.current_state is BalanceEpisodeState.LOCKED
    locked_permissions = _permission_results(evaluations[-1])
    assert locked_permissions[SetupFamily.REVERSAL] is StructuralPermissionResult.BLOCK
    assert SetupFamily.FAILED_BREAKOUT not in locked_permissions

    escape = engine.advance(
        _observation(
            8,
            close=101.20,
            observation_state=AuctionStateName.BOUNDARY_INTERACTION,
            trend_direction=DirectionalBias.UNKNOWN,
            directional_bias=DirectionalBias.UP,
            protection=None,
            accepted_range=True,
            accepted_inside=False,
        )
    )
    assert escape.balance.current_state is BalanceEpisodeState.ESCAPE_WATCH

    accepted = engine.advance(
        _observation(
            9,
            close=101.30,
            observation_state=AuctionStateName.FRESH_EXPANSION,
            trend_direction=DirectionalBias.UNKNOWN,
            directional_bias=DirectionalBias.UP,
            protection=None,
            accepted_range=True,
            accepted_inside=False,
        )
    )
    assert accepted.balance.current_state is BalanceEpisodeState.ACCEPTED_OUTSIDE
    assert AuctionEventType.BALANCE_ESCAPE_ACCEPTED in {
        event.event_type for event in accepted.events
    }
    accepted_permissions = _permission_results(accepted)
    assert accepted_permissions[SetupFamily.ACCEPTED_BREAKOUT] is (
        StructuralPermissionResult.PERMIT
    )


def test_balance_failed_escape_requires_rearm_before_same_episode_relocks() -> None:
    engine = PersistentEpisodeEngine()
    locked = _lock_balance(engine)
    episode_id = locked.balance.episode_id

    started = engine.advance(
        _observation(
            8,
            close=101.20,
            observation_state=AuctionStateName.BOUNDARY_INTERACTION,
            trend_direction=DirectionalBias.UNKNOWN,
            directional_bias=DirectionalBias.UP,
            protection=None,
            accepted_range=True,
            accepted_inside=False,
        )
    )
    assert started.balance.escape_attempt_count == 1
    assert started.balance.up_escape_attempt_count == 1
    assert started.balance.down_escape_attempt_count == 0
    assert started.balance.last_escape_direction is DirectionalBias.UP
    assert started.balance.last_escape_started_at == START + timedelta(minutes=24)

    failed = engine.advance(
        _observation(
            9,
            close=100.50,
            observation_state=AuctionStateName.BALANCE,
            trend_direction=DirectionalBias.UNKNOWN,
            directional_bias=DirectionalBias.NEUTRAL,
            protection=None,
            accepted_range=True,
            accepted_inside=True,
        )
    )
    assert failed.balance.current_state is BalanceEpisodeState.FAILED_BACK_INSIDE
    assert failed.balance.episode_id == episode_id
    assert failed.balance.failed_escape_count == 1
    assert failed.balance.rearm_required is True
    assert failed.balance.rearm_inside_close_count == 0
    assert failed.balance.rearm_bars_elapsed == 0
    assert failed.balance.last_escape_failed_at == START + timedelta(minutes=27)
    assert _permission_results(failed)[SetupFamily.FAILED_BREAKOUT] is (
        StructuralPermissionResult.PERMIT
    )

    first_inside = engine.advance(
        _observation(
            10,
            close=100.40,
            observation_state=AuctionStateName.BALANCE,
            trend_direction=DirectionalBias.UNKNOWN,
            directional_bias=DirectionalBias.NEUTRAL,
            protection=None,
            accepted_range=True,
            accepted_inside=True,
        )
    )
    assert first_inside.balance.current_state is BalanceEpisodeState.FAILED_BACK_INSIDE
    assert first_inside.balance.rearm_required is True
    assert first_inside.balance.rearm_inside_close_count == 1
    assert first_inside.balance.rearm_bars_elapsed == 1
    assert AuctionEventType.BALANCE_REARMED not in {
        event.event_type for event in first_inside.events
    }

    relocked = engine.advance(
        _observation(
            11,
            close=100.30,
            observation_state=AuctionStateName.BALANCE,
            trend_direction=DirectionalBias.UNKNOWN,
            directional_bias=DirectionalBias.NEUTRAL,
            protection=None,
            accepted_range=True,
            accepted_inside=True,
        )
    )
    assert relocked.balance.current_state is BalanceEpisodeState.LOCKED
    assert relocked.balance.episode_id == episode_id
    assert relocked.balance.rearm_required is False
    assert relocked.balance.escape_attempt_count == 1
    assert relocked.balance.failed_escape_count == 1
    assert AuctionEventType.BALANCE_REARMED in {
        event.event_type for event in relocked.events
    }


def test_balance_rearm_blocks_immediate_opposite_escape() -> None:
    engine = PersistentEpisodeEngine()
    _lock_balance(engine)
    engine.advance(
        _observation(
            8,
            close=101.20,
            observation_state=AuctionStateName.BOUNDARY_INTERACTION,
            trend_direction=DirectionalBias.UNKNOWN,
            directional_bias=DirectionalBias.UP,
            protection=None,
            accepted_range=True,
            accepted_inside=False,
        )
    )
    engine.advance(
        _observation(
            9,
            close=100.50,
            observation_state=AuctionStateName.BALANCE,
            trend_direction=DirectionalBias.UNKNOWN,
            directional_bias=DirectionalBias.NEUTRAL,
            protection=None,
            accepted_range=True,
            accepted_inside=True,
        )
    )

    interrupted = engine.advance(
        _observation(
            10,
            close=98.80,
            observation_state=AuctionStateName.BOUNDARY_INTERACTION,
            trend_direction=DirectionalBias.UNKNOWN,
            directional_bias=DirectionalBias.DOWN,
            protection=None,
            accepted_range=True,
            accepted_inside=False,
        )
    )

    assert interrupted.balance.current_state is BalanceEpisodeState.FAILED_BACK_INSIDE
    assert interrupted.balance.rearm_required is True
    assert interrupted.balance.rearm_inside_close_count == 0
    assert interrupted.balance.rearm_bars_elapsed == 1
    assert interrupted.balance.escape_attempt_count == 1
    assert interrupted.balance.down_escape_attempt_count == 0
    assert AuctionEventType.BALANCE_ESCAPE_STARTED not in {
        event.event_type for event in interrupted.events
    }


def test_same_side_attempt_limit_requires_materially_new_range() -> None:
    engine = PersistentEpisodeEngine()
    first_locked = _lock_balance(engine)
    episode_id = first_locked.balance.episode_id

    # First UP attempt fails and then objectively rearms.
    engine.advance(
        _observation(
            8, close=101.20, observation_state=AuctionStateName.BOUNDARY_INTERACTION,
            trend_direction=DirectionalBias.UNKNOWN, directional_bias=DirectionalBias.UP,
            protection=None, accepted_range=True, accepted_inside=False,
        )
    )
    engine.advance(
        _observation(
            9, close=100.50, observation_state=AuctionStateName.BALANCE,
            trend_direction=DirectionalBias.UNKNOWN, directional_bias=DirectionalBias.NEUTRAL,
            protection=None, accepted_range=True, accepted_inside=True,
        )
    )
    engine.advance(
        _observation(
            10, close=100.40, observation_state=AuctionStateName.BALANCE,
            trend_direction=DirectionalBias.UNKNOWN, directional_bias=DirectionalBias.NEUTRAL,
            protection=None, accepted_range=True, accepted_inside=True,
        )
    )
    engine.advance(
        _observation(
            11, close=100.30, observation_state=AuctionStateName.BALANCE,
            trend_direction=DirectionalBias.UNKNOWN, directional_bias=DirectionalBias.NEUTRAL,
            protection=None, accepted_range=True, accepted_inside=True,
        )
    )

    # Second UP attempt reaches the same-side limit when it fails.
    second_started = engine.advance(
        _observation(
            12, close=101.25, observation_state=AuctionStateName.BOUNDARY_INTERACTION,
            trend_direction=DirectionalBias.UNKNOWN, directional_bias=DirectionalBias.UP,
            protection=None, accepted_range=True, accepted_inside=False,
        )
    )
    assert second_started.balance.escape_attempt_count == 2
    assert second_started.balance.up_escape_attempt_count == 2

    limited = engine.advance(
        _observation(
            13, close=100.60, observation_state=AuctionStateName.BALANCE,
            trend_direction=DirectionalBias.UNKNOWN, directional_bias=DirectionalBias.NEUTRAL,
            protection=None, accepted_range=True, accepted_inside=True,
        )
    )
    assert limited.balance.current_state is BalanceEpisodeState.FAILED_BACK_INSIDE
    assert limited.balance.attempt_limit_reached is True
    assert AuctionEventType.BALANCE_ATTEMPT_LIMIT_REACHED in {
        event.event_type for event in limited.events
    }

    still_limited = engine.advance(
        _observation(
            14, close=100.40, observation_state=AuctionStateName.BALANCE,
            trend_direction=DirectionalBias.UNKNOWN, directional_bias=DirectionalBias.NEUTRAL,
            protection=None, accepted_range=True, accepted_inside=True,
        )
    )
    assert still_limited.balance.current_state is BalanceEpisodeState.FAILED_BACK_INSIDE
    assert still_limited.balance.attempt_limit_reached is True
    assert still_limited.balance.episode_id == episode_id

    # A materially different accepted range completes the old episode.
    completed = engine.advance(
        _observation(
            15, close=104.0, observation_state=AuctionStateName.BALANCE,
            trend_direction=DirectionalBias.UNKNOWN, directional_bias=DirectionalBias.NEUTRAL,
            protection=None, accepted_range=True, accepted_inside=True,
            accepted_range_id="RANGE:NEW", accepted_range_low=103.0,
            accepted_range_high=105.0,
        )
    )
    assert completed.balance.current_state is BalanceEpisodeState.COMPLETED
    assert completed.balance.episode_id == episode_id
    assert AuctionEventType.BALANCE_COMPLETED in {
        event.event_type for event in completed.events
    }
    assert "BALANCE_ATTEMPT_LIMIT_RELEASED_BY_NEW_RANGE" in (
        completed.balance.reason_codes
    )

    new_episode = engine.advance(
        _observation(
            16, close=104.0, observation_state=AuctionStateName.BALANCE,
            trend_direction=DirectionalBias.UNKNOWN, directional_bias=DirectionalBias.NEUTRAL,
            protection=None, accepted_range=True, accepted_inside=True,
            accepted_range_id="RANGE:NEW", accepted_range_low=103.0,
            accepted_range_high=105.0,
        )
    )
    assert new_episode.balance.current_state is BalanceEpisodeState.FORMING
    assert new_episode.balance.episode_id != episode_id
    assert new_episode.balance.escape_attempt_count == 0



def test_locked_balance_records_but_blocks_structural_reversal_event() -> None:
    engine = PersistentEpisodeEngine()
    engine.advance(
        _observation(0, accepted_range=True, accepted_inside=True)
    )
    engine.advance(
        _observation(1, accepted_range=True, accepted_inside=True)
    )
    engine.advance(
        _observation(
            2,
            current_leg_mature=True,
            accepted_range=True,
            accepted_inside=True,
        )
    )
    engine.advance(
        _observation(
            3,
            current_leg_mature=True,
            exhaustion_active=True,
            exhausted_side=DirectionalBias.UP,
            rejection=True,
            accepted_range=True,
            accepted_inside=True,
        )
    )
    for index in range(4, 8):
        balance_progress = engine.advance(
            _observation(
                index,
                close=99.90,
                current_leg_mature=True,
                exhaustion_active=True,
                exhausted_side=DirectionalBias.UP,
                rejection=True,
                structural_failure=True,
                accepted_range=True,
                accepted_inside=True,
            )
        )

    assert balance_progress.balance.current_state is BalanceEpisodeState.LOCKED

    blocked = engine.advance(
        _observation(
            8,
            close=99.70,
            observation_state=AuctionStateName.TREND_FAILURE,
            current_leg_mature=True,
            exhaustion_active=True,
            exhausted_side=DirectionalBias.UP,
            rejection=True,
            structural_failure=True,
            accepted_range=True,
            accepted_inside=True,
        )
    )

    assert blocked.balance.current_state is BalanceEpisodeState.LOCKED
    assert blocked.directional.current_state is DirectionalEpisodeState.REVERSAL_LEG
    assert blocked.directional.direction is DirectionalBias.DOWN
    assert (
        blocked.directional.origin_source
        is DirectionalEpisodeOrigin.REVERSAL_EVENT_HANDOFF
    )
    assert _permission_results(blocked)[SetupFamily.REVERSAL] is (
        StructuralPermissionResult.BLOCK
    )

    first_progress = engine.advance(
        _observation(
            9,
            close=99.10,
            observation_state=AuctionStateName.ORDERLY_DOWNTREND,
            trend_direction=DirectionalBias.DOWN,
            directional_bias=DirectionalBias.DOWN,
            protection=100.10,
            accepted_range=True,
            accepted_inside=True,
        )
    )
    established = engine.advance(
        _observation(
            10,
            close=99.00,
            observation_state=AuctionStateName.ORDERLY_DOWNTREND,
            trend_direction=DirectionalBias.DOWN,
            directional_bias=DirectionalBias.DOWN,
            protection=100.10,
            accepted_range=True,
            accepted_inside=True,
        )
    )
    assert first_progress.directional.current_state is DirectionalEpisodeState.REVERSAL_LEG
    assert established.directional.current_state is DirectionalEpisodeState.DIRECTIONAL
    assert established.directional.direction is DirectionalBias.DOWN
    assert _permission_results(established)[SetupFamily.REVERSAL] is (
        StructuralPermissionResult.BLOCK
    )


def test_reversal_handoff_does_not_return_to_lagging_observation_direction() -> None:
    engine = PersistentEpisodeEngine()
    engine.advance(_observation(0))
    engine.advance(_observation(1))
    engine.advance(_observation(2, current_leg_mature=True))
    engine.advance(
        _observation(
            3,
            close=100.0,
            current_leg_mature=True,
            exhaustion_active=True,
            exhausted_side=DirectionalBias.UP,
            rejection=True,
        )
    )
    engine.advance(
        _observation(
            4,
            close=99.9,
            current_leg_mature=True,
            exhaustion_active=True,
            exhausted_side=DirectionalBias.UP,
            structural_failure=True,
        )
    )
    confirmed = engine.advance(
        _observation(
            5,
            close=99.5,
            observation_state=AuctionStateName.TREND_FAILURE,
            current_leg_mature=True,
            exhaustion_active=True,
            exhausted_side=DirectionalBias.UP,
            structural_failure=True,
        )
    )
    handoff_episode_id = confirmed.directional.episode_id
    assert confirmed.directional.direction is DirectionalBias.DOWN
    assert confirmed.directional.current_state is DirectionalEpisodeState.REVERSAL_LEG

    lag_one = engine.advance(
        _observation(
            6,
            close=98.9,
            observation_state=AuctionStateName.ORDERLY_UPTREND,
            trend_direction=DirectionalBias.UP,
            directional_bias=DirectionalBias.UP,
        )
    )
    lag_two = engine.advance(
        _observation(
            7,
            close=98.8,
            observation_state=AuctionStateName.ORDERLY_UPTREND,
            trend_direction=DirectionalBias.UP,
            directional_bias=DirectionalBias.UP,
        )
    )

    assert lag_one.directional.episode_id == handoff_episode_id
    assert lag_one.directional.current_state is DirectionalEpisodeState.REVERSAL_LEG
    assert lag_two.directional.episode_id == handoff_episode_id
    assert lag_two.directional.direction is DirectionalBias.DOWN
    assert lag_two.directional.current_state is DirectionalEpisodeState.DIRECTIONAL
    assert AuctionEventType.DIRECTIONAL_REVERSAL_LEG_ESTABLISHED in {
        event.event_type for event in lag_two.events
    }


def test_reversal_leg_fails_before_becoming_established_direction() -> None:
    engine = PersistentEpisodeEngine()
    engine.advance(_observation(0))
    engine.advance(_observation(1))
    engine.advance(_observation(2, current_leg_mature=True))
    engine.advance(
        _observation(
            3,
            current_leg_mature=True,
            exhaustion_active=True,
            exhausted_side=DirectionalBias.UP,
            rejection=True,
        )
    )
    engine.advance(
        _observation(
            4,
            close=99.90,
            current_leg_mature=True,
            exhaustion_active=True,
            exhausted_side=DirectionalBias.UP,
            structural_failure=True,
        )
    )
    confirmed = engine.advance(
        _observation(
            5,
            close=99.50,
            observation_state=AuctionStateName.TREND_FAILURE,
            current_leg_mature=True,
            exhaustion_active=True,
            exhausted_side=DirectionalBias.UP,
            structural_failure=True,
        )
    )
    assert confirmed.directional.current_state is DirectionalEpisodeState.REVERSAL_LEG

    first_failure = engine.advance(
        _observation(
            6,
            close=99.70,
            observation_state=AuctionStateName.ORDERLY_UPTREND,
            trend_direction=DirectionalBias.UP,
            directional_bias=DirectionalBias.UP,
        )
    )
    assert first_failure.directional.current_state is DirectionalEpisodeState.REVERSAL_LEG
    assert first_failure.directional.reversal_leg_failure_closes == 1

    failed = engine.advance(
        _observation(
            7,
            close=99.80,
            observation_state=AuctionStateName.ORDERLY_UPTREND,
            trend_direction=DirectionalBias.UP,
            directional_bias=DirectionalBias.UP,
        )
    )
    assert failed.directional.current_state is DirectionalEpisodeState.COMPLETED
    event_types = {event.event_type for event in failed.events}
    assert AuctionEventType.DIRECTIONAL_REVERSAL_LEG_FAILED in event_types
    assert AuctionEventType.DIRECTIONAL_COMPLETED in event_types
    assert AuctionEventType.DIRECTIONAL_REVERSAL_CONFIRMED not in event_types


def test_fresh_extreme_rejection_restarts_failure_stage_on_same_bar() -> None:
    engine = PersistentEpisodeEngine()
    engine.advance(_observation(0, close=100.0, high=100.2, low=99.8))
    engine.advance(_observation(1, close=100.4, high=100.6, low=100.1))
    engine.advance(
        _observation(
            2,
            close=100.6,
            high=100.8,
            low=100.4,
            current_leg_mature=True,
        )
    )
    first_watch = engine.advance(
        _observation(
            3,
            close=100.5,
            high=100.7,
            low=100.3,
            current_leg_mature=True,
            exhaustion_active=True,
            exhausted_side=DirectionalBias.UP,
            rejection=True,
        )
    )
    assert first_watch.directional.continuation_failure_time == (
        START + timedelta(minutes=9)
    )

    fresh_extreme_rejection = engine.advance(
        _observation(
            4,
            close=100.55,
            high=100.9,
            low=100.2,
            current_leg_mature=True,
            exhaustion_active=True,
            exhausted_side=DirectionalBias.UP,
            rejection=True,
        )
    )

    assert (
        fresh_extreme_rejection.directional.current_state
        is DirectionalEpisodeState.REVERSAL_WATCH
    )
    assert fresh_extreme_rejection.directional.first_adverse_bar_time == (
        START + timedelta(minutes=12)
    )
    assert fresh_extreme_rejection.directional.continuation_failure_seen is True
    assert fresh_extreme_rejection.directional.continuation_failure_time == (
        START + timedelta(minutes=12)
    )


def test_reversal_watch_builds_price_action_stages_without_rejection_observation() -> None:
    engine = PersistentEpisodeEngine()
    engine.advance(_observation(0, close=100.0, high=100.2, low=99.8))
    engine.advance(_observation(1, close=100.4, high=100.6, low=100.1))
    engine.advance(
        _observation(
            2,
            close=100.6,
            high=100.8,
            low=100.4,
            current_leg_mature=True,
        )
    )
    watch = engine.advance(
        _observation(
            3,
            close=100.5,
            high=100.7,
            low=100.3,
            current_leg_mature=True,
            exhaustion_active=True,
            exhausted_side=DirectionalBias.UP,
            rejection=False,
            failed_extreme=False,
        )
    )
    assert watch.directional.current_state is DirectionalEpisodeState.REVERSAL_WATCH
    assert watch.directional.first_adverse_bar_time == START + timedelta(minutes=9)
    assert watch.directional.first_adverse_bar_level == pytest.approx(100.3)
    assert watch.directional.rejection_seen is True
    assert watch.directional.continuation_failure_seen is True
    assert watch.directional.continuation_failure_time == START + timedelta(minutes=9)

    failure = engine.advance(
        _observation(
            4,
            close=100.45,
            high=100.6,
            low=100.35,
            current_leg_mature=True,
            exhaustion_active=True,
            exhausted_side=DirectionalBias.UP,
            rejection=False,
            structural_failure=False,
        )
    )
    assert failure.directional.continuation_failure_seen is True
    assert failure.directional.continuation_failure_time == START + timedelta(minutes=9)

    confirmed = engine.advance(
        _observation(
            5,
            close=100.2,
            high=100.4,
            low=100.0,
            current_leg_mature=True,
            exhaustion_active=True,
            exhausted_side=DirectionalBias.UP,
            rejection=False,
            structural_failure=False,
        )
    )
    assert AuctionEventType.DIRECTIONAL_REVERSAL_CONFIRMED in {
        event.event_type for event in confirmed.events
    }
    assert confirmed.directional.direction is DirectionalBias.DOWN
    assert confirmed.directional.current_state is DirectionalEpisodeState.REVERSAL_LEG


def test_balance_forming_survives_without_repeated_source_range_and_expands_from_bars() -> None:
    engine = PersistentEpisodeEngine()
    forming = engine.advance(
        _observation(
            0,
            close=100.0,
            high=100.2,
            low=99.8,
            observation_state=AuctionStateName.BALANCE,
            trend_direction=DirectionalBias.UNKNOWN,
            directional_bias=DirectionalBias.NEUTRAL,
            protection=None,
            accepted_range=True,
            accepted_inside=True,
            accepted_range_low=99.8,
            accepted_range_high=100.2,
        )
    )
    assert forming.balance.current_state is BalanceEpisodeState.FORMING

    bars = [
        (100.05, 100.25, 99.85),
        (99.95, 100.20, 99.75),
        (100.00, 100.22, 99.78),
        (100.02, 100.24, 99.76),
        (100.01, 100.21, 99.80),
        (99.99, 100.18, 99.79),
        (100.03, 100.23, 99.81),
    ]
    for index, (close, high, low) in enumerate(bars, start=1):
        result = engine.advance(
            _observation(
                index,
                close=close,
                high=high,
                low=low,
                observation_state=AuctionStateName.BALANCE,
                trend_direction=DirectionalBias.UNKNOWN,
                directional_bias=DirectionalBias.NEUTRAL,
                protection=None,
                accepted_range=False,
                accepted_inside=False,
            )
        )

    assert result.balance.current_state is BalanceEpisodeState.LOCKED
    assert result.balance.candidate_bar_expansion_count >= 1
    assert result.balance.frozen_low == pytest.approx(99.75)
    assert result.balance.frozen_high == pytest.approx(100.25)
    assert result.balance.candidate_last_valid_at == START + timedelta(minutes=21)


def test_balance_probable_accumulates_nonconsecutive_containment_before_lock() -> None:
    engine = PersistentEpisodeEngine()
    forming = engine.advance(
        _observation(
            0,
            observation_state=AuctionStateName.BALANCE,
            trend_direction=DirectionalBias.UNKNOWN,
            directional_bias=DirectionalBias.NEUTRAL,
            protection=None,
            accepted_range=True,
            accepted_inside=True,
        )
    )
    episode_id = forming.balance.episode_id

    for index in range(1, 4):
        result = engine.advance(
            _observation(
                index,
                observation_state=AuctionStateName.BALANCE,
                trend_direction=DirectionalBias.UNKNOWN,
                directional_bias=DirectionalBias.NEUTRAL,
                protection=None,
                accepted_range=False,
                accepted_inside=False,
            )
        )

    probable = engine.advance(
        _observation(
            4,
            observation_state=AuctionStateName.BALANCE,
            trend_direction=DirectionalBias.UNKNOWN,
            directional_bias=DirectionalBias.NEUTRAL,
            protection=None,
            accepted_range=False,
            accepted_inside=False,
            efficiency=0.20,
            overlap=0.20,
        )
    )
    assert probable.balance.current_state is BalanceEpisodeState.PROBABLE
    assert probable.balance.episode_id == episode_id
    assert probable.balance.containment_bars == 4
    assert probable.balance.forming_bars_observed == 5
    assert probable.balance.containment_ratio == pytest.approx(0.8)

    retained = engine.advance(
        _observation(
            5,
            observation_state=AuctionStateName.BALANCE,
            trend_direction=DirectionalBias.UNKNOWN,
            directional_bias=DirectionalBias.NEUTRAL,
            protection=None,
            accepted_range=False,
            accepted_inside=False,
            efficiency=0.20,
            overlap=0.20,
        )
    )
    assert retained.balance.current_state is BalanceEpisodeState.PROBABLE
    assert retained.balance.marginal_excursion_bars == 2

    for index in range(6, 8):
        locked = engine.advance(
            _observation(
                index,
                observation_state=AuctionStateName.BALANCE,
                trend_direction=DirectionalBias.UNKNOWN,
                directional_bias=DirectionalBias.NEUTRAL,
                protection=None,
                accepted_range=False,
                accepted_inside=False,
            )
        )

    assert locked.balance.current_state is BalanceEpisodeState.LOCKED
    assert locked.balance.episode_id == episode_id
    assert locked.balance.forming_bars_observed == 8
    assert locked.balance.containment_bars == 6
    assert locked.balance.containment_ratio == pytest.approx(0.75)


def test_conflicting_or_out_of_order_observation_fails() -> None:
    engine = PersistentEpisodeEngine()
    original = _observation(0)
    first = engine.advance(original)
    assert engine.advance(original) == first

    with pytest.raises(EpisodeChronologyError):
        engine.advance(_observation(0, close=100.10))

    engine.reset("TEST")
    engine.advance(_observation(2))
    with pytest.raises(EpisodeChronologyError):
        engine.advance(_observation(1))


def test_controlled_pullback_resumption_emits_authoritative_continuation_event() -> None:
    engine = PersistentEpisodeEngine()
    engine.advance(_observation(0))
    engine.advance(_observation(1))

    pullback = engine.advance(
        _observation(
            2,
            close=99.8,
            observation_state=AuctionStateName.CONTROLLED_PULLBACK,
            trend_direction=DirectionalBias.UP,
            directional_bias=DirectionalBias.UP,
        )
    )
    assert pullback.directional.current_state is DirectionalEpisodeState.DIRECTIONAL

    resumed = engine.advance(
        _observation(
            3,
            close=100.5,
            observation_state=AuctionStateName.REACCELERATION,
            trend_direction=DirectionalBias.UP,
            directional_bias=DirectionalBias.UP,
        )
    )
    continuation_events = [
        event
        for event in resumed.events
        if event.event_type
        is AuctionEventType.DIRECTIONAL_CONTINUATION_CONFIRMED
    ]
    assert len(continuation_events) == 1
    assert continuation_events[0].direction is DirectionalBias.UP
    assert continuation_events[0].data["previous_observation_state"] == (
        AuctionStateName.CONTROLLED_PULLBACK.value
    )
    assert _permission_results(resumed)[SetupFamily.CONTINUATION] is (
        StructuralPermissionResult.PERMIT
    )
    assert SetupFamily.REACCELERATION not in _permission_results(resumed)

    held = engine.advance(
        _observation(
            4,
            close=100.7,
            observation_state=AuctionStateName.REACCELERATION,
            trend_direction=DirectionalBias.UP,
            directional_bias=DirectionalBias.UP,
        )
    )
    assert AuctionEventType.DIRECTIONAL_CONTINUATION_CONFIRMED not in {
        event.event_type for event in held.events
    }


def test_recompression_expansion_emits_authoritative_reacceleration_event() -> None:
    engine = PersistentEpisodeEngine()
    engine.advance(_observation(0))
    engine.advance(_observation(1))

    recompression = engine.advance(
        _observation(
            2,
            close=100.1,
            observation_state=AuctionStateName.RECOMPRESSION,
            trend_direction=DirectionalBias.UP,
            directional_bias=DirectionalBias.UP,
        )
    )
    assert recompression.directional.current_state is DirectionalEpisodeState.DIRECTIONAL

    expanded = engine.advance(
        _observation(
            3,
            close=100.8,
            observation_state=AuctionStateName.REACCELERATION,
            trend_direction=DirectionalBias.UP,
            directional_bias=DirectionalBias.UP,
        )
    )
    reacceleration_events = [
        event
        for event in expanded.events
        if event.event_type
        is AuctionEventType.DIRECTIONAL_REACCELERATION_CONFIRMED
    ]
    assert len(reacceleration_events) == 1
    assert reacceleration_events[0].direction is DirectionalBias.UP
    assert reacceleration_events[0].data["previous_observation_state"] == (
        AuctionStateName.RECOMPRESSION.value
    )
    assert _permission_results(expanded)[SetupFamily.REACCELERATION] is (
        StructuralPermissionResult.PERMIT
    )
    assert SetupFamily.CONTINUATION not in _permission_results(expanded)
