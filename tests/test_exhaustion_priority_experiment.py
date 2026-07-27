from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from configs.auction_experiment_config import AUCTION_POLICY_EXPERIMENT_CONFIG
from tests.experiment_exhaustion_priority import (
    ExhaustionEpisode,
    confirmation,
    range_locked,
    update_failure,
)


def _snapshot(
    *,
    ts: datetime,
    open_price: float,
    high: float,
    low: float,
    close: float,
    atr: float,
    accepted_range_id: str | None = None,
    accepted_range_inside: bool = False,
    accepted_range_breakout_eligible: bool = False,
    accepted_range_provisional: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        snapshot_time=ts,
        close=close,
        bar=SimpleNamespace(
            open=open_price,
            high=high,
            low=low,
            close=close,
        ),
        indicators=SimpleNamespace(
            atr=SimpleNamespace(value=atr),
        ),
        auction=SimpleNamespace(
            stock_context=SimpleNamespace(
                accepted_range_id=accepted_range_id,
                accepted_range_inside=accepted_range_inside,
                accepted_range_breakout_eligible=(
                    accepted_range_breakout_eligible
                ),
                accepted_range_provisional=accepted_range_provisional,
            )
        ),
    )


def _episode(*, exhausted_side: str, reversal_side: str) -> ExhaustionEpisode:
    start = datetime(2026, 7, 27, 9, 45)
    return ExhaustionEpisode(
        episode_id="TEST",
        symbol="TEST",
        exhausted_side=exhausted_side,
        reversal_side=reversal_side,
        initiation_time=start,
        initiation_close=100.0,
        initiation_atr=2.0,
        initiation_vwap=96.0 if exhausted_side == "UP" else 104.0,
        initial_extreme=104.0 if exhausted_side == "UP" else 96.0,
        extreme_price=104.0 if exhausted_side == "UP" else 96.0,
        extreme_time=start,
        gap_pct=1.5,
        large_gap=True,
        initial_vwap_room_points=8.0,
        initial_vwap_room_atr=4.0,
        initial_vwap_room_pct=0.08,
        initial_room_qualified=True,
        expires_at=start + timedelta(minutes=90),
    )


def test_upside_exhaustion_confirms_on_first_bearish_displacement() -> None:
    previous = _snapshot(
        ts=datetime(2026, 7, 27, 9, 48),
        open_price=103.5,
        high=104.0,
        low=102.8,
        close=103.6,
        atr=2.0,
    )
    current = _snapshot(
        ts=datetime(2026, 7, 27, 9, 51),
        open_price=103.4,
        high=103.5,
        low=101.8,
        close=102.0,
        atr=2.0,
    )
    confirmed, reason, displacement = confirmation(
        episode=_episode(exhausted_side="UP", reversal_side="SELL"),
        previous=previous,
        current=current,
        config=AUCTION_POLICY_EXPERIMENT_CONFIG,
    )
    assert confirmed is True
    assert reason == "LOWER_HIGH_AND_PREVIOUS_LOW_BREAK"
    assert displacement == 1.0


def test_downside_exhaustion_confirms_symmetrically() -> None:
    previous = _snapshot(
        ts=datetime(2026, 7, 24, 9, 48),
        open_price=96.5,
        high=97.2,
        low=96.0,
        close=96.4,
        atr=2.0,
    )
    current = _snapshot(
        ts=datetime(2026, 7, 24, 9, 51),
        open_price=96.6,
        high=98.2,
        low=96.5,
        close=98.0,
        atr=2.0,
    )
    confirmed, reason, displacement = confirmation(
        episode=_episode(exhausted_side="DOWN", reversal_side="BUY"),
        previous=previous,
        current=current,
        config=AUCTION_POLICY_EXPERIMENT_CONFIG,
    )
    assert confirmed is True
    assert reason == "HIGHER_LOW_AND_PREVIOUS_HIGH_BREAK"
    assert displacement == 1.0


def test_reversal_failure_requires_configured_acceptance_closes() -> None:
    episode = _episode(exhausted_side="UP", reversal_side="SELL")
    episode.confirmation_time = datetime(2026, 7, 27, 9, 51)
    episode.confirmation_price = 102.0
    first = _snapshot(
        ts=datetime(2026, 7, 27, 10, 0),
        open_price=104.1,
        high=104.5,
        low=104.0,
        close=104.3,
        atr=2.0,
    )
    second = _snapshot(
        ts=datetime(2026, 7, 27, 10, 3),
        open_price=104.2,
        high=104.7,
        low=104.1,
        close=104.4,
        atr=2.0,
    )
    update_failure(episode, first, AUCTION_POLICY_EXPERIMENT_CONFIG)
    assert episode.reversal_failed_time == datetime(2026, 7, 27, 10, 0)
    assert episode.continuation_unlocked_time is None
    update_failure(episode, second, AUCTION_POLICY_EXPERIMENT_CONFIG)
    assert episode.reversal_failed_time == datetime(2026, 7, 27, 10, 0)
    assert episode.continuation_unlocked_time == datetime(2026, 7, 27, 10, 3)


def test_range_lock_requires_confirmed_accepted_range() -> None:
    locked = _snapshot(
        ts=datetime(2026, 7, 27, 11, 0),
        open_price=100.0,
        high=101.0,
        low=99.5,
        close=100.2,
        atr=1.0,
        accepted_range_id="R1",
        accepted_range_inside=True,
        accepted_range_breakout_eligible=True,
        accepted_range_provisional=False,
    )
    provisional = _snapshot(
        ts=datetime(2026, 7, 27, 11, 3),
        open_price=100.0,
        high=101.0,
        low=99.5,
        close=100.2,
        atr=1.0,
        accepted_range_id="R2",
        accepted_range_inside=True,
        accepted_range_breakout_eligible=True,
        accepted_range_provisional=True,
    )
    assert range_locked(locked, AUCTION_POLICY_EXPERIMENT_CONFIG) is True
    assert range_locked(provisional, AUCTION_POLICY_EXPERIMENT_CONFIG) is False
