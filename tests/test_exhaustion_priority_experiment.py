from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from configs.auction_experiment_config import AUCTION_POLICY_EXPERIMENT_CONFIG
from tests.experiment_exhaustion_priority import (
    ExhaustionEpisode,
    MissingExhaustionMemoryError,
    MissingExhaustionReasonCodesError,
    auction_exhaustion_reason_codes,
    confirmation,
    maturity_qualified,
    range_blocks_setup,
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
    accepted_range_established_at: datetime | None = None,
    stock_context_reason_codes: tuple[str, ...] = (),
    auction_state_memory: dict[str, object] | None = None,
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
                accepted_range_established_at=accepted_range_established_at,
                reason_codes=list(stock_context_reason_codes),
            )
        ),
        memory=SimpleNamespace(
            auction=SimpleNamespace(state_memory=auction_state_memory)
        ),
    )


def _episode(
    *,
    exhausted_side: str,
    reversal_side: str,
    initial_room_qualified: bool = True,
    maturity_is_qualified: bool = True,
) -> ExhaustionEpisode:
    start = datetime(2026, 7, 27, 9, 45)
    return ExhaustionEpisode(
        episode_id="TEST",
        symbol="TEST",
        sequence_number=1,
        exhausted_side=exhausted_side,
        reversal_side=reversal_side,
        initiation_time=start,
        auction_exhaustion_started_at=start,
        initiation_close=100.0,
        initiation_atr=2.0,
        initiation_vwap=96.0 if exhausted_side == "UP" else 104.0,
        initiation_reason_codes=("CURRENT_LEG_MATURE",),
        stock_context_reason_codes=("EXHAUSTION_CONTEXT_ACTIVE",),
        auction_exhaustion_reason_codes=("CURRENT_LEG_MATURE",),
        maturity_reason_source="AUCTION_STATE_MEMORY",
        maturity_qualified=maturity_is_qualified,
        initial_extreme=104.0 if exhausted_side == "UP" else 96.0,
        extreme_price=104.0 if exhausted_side == "UP" else 96.0,
        extreme_time=start,
        extreme_bar_index=10,
        gap_pct=1.5,
        large_gap=True,
        initial_vwap_room_points=8.0,
        initial_vwap_room_atr=4.0,
        initial_vwap_room_pct=0.08,
        initial_room_qualified=initial_room_qualified,
        expires_at=start + timedelta(minutes=120),
    )


def test_maturity_requires_configured_reason_code() -> None:
    assert maturity_qualified(
        reason_codes=("CURRENT_LEG_MATURE",),
        config=AUCTION_POLICY_EXPERIMENT_CONFIG,
    ) is True
    assert maturity_qualified(
        reason_codes=("REJECTION_OR_FAILED_EXTREME",),
        config=AUCTION_POLICY_EXPERIMENT_CONFIG,
    ) is False


def test_maturity_reasons_are_read_from_auction_state_memory() -> None:
    snapshot = _snapshot(
        ts=datetime(2026, 7, 27, 9, 45),
        open_price=103.0,
        high=104.0,
        low=102.5,
        close=103.5,
        atr=2.0,
        stock_context_reason_codes=(
            "STOCK_CONTEXT_OBJECTIVE_SESSION_PATH",
            "EXHAUSTION_CONTEXT_ACTIVE",
        ),
        auction_state_memory={
            "exhaustion_reason_codes": [
                "CURRENT_LEG_MATURE",
                "PRIOR_MOVE_EXTENDED",
            ]
        },
    )
    reasons = auction_exhaustion_reason_codes(snapshot)
    assert reasons == ("CURRENT_LEG_MATURE", "PRIOR_MOVE_EXTENDED")
    assert maturity_qualified(
        reason_codes=reasons,
        config=AUCTION_POLICY_EXPERIMENT_CONFIG,
    ) is True


def test_missing_auction_state_memory_fails_strictly() -> None:
    snapshot = _snapshot(
        ts=datetime(2026, 7, 27, 9, 45),
        open_price=103.0,
        high=104.0,
        low=102.5,
        close=103.5,
        atr=2.0,
        auction_state_memory=None,
    )
    with pytest.raises(MissingExhaustionMemoryError):
        auction_exhaustion_reason_codes(snapshot)


def test_missing_exhaustion_reason_list_fails_strictly() -> None:
    snapshot = _snapshot(
        ts=datetime(2026, 7, 27, 9, 45),
        open_price=103.0,
        high=104.0,
        low=102.5,
        close=103.5,
        atr=2.0,
        auction_state_memory={},
    )
    with pytest.raises(MissingExhaustionReasonCodesError):
        auction_exhaustion_reason_codes(snapshot)


def test_same_bar_confirmation_is_rejected() -> None:
    prior = _snapshot(
        ts=datetime(2026, 7, 27, 9, 42),
        open_price=103.0,
        high=103.8,
        low=102.5,
        close=103.5,
        atr=2.0,
    )
    current = _snapshot(
        ts=datetime(2026, 7, 27, 9, 45),
        open_price=103.5,
        high=104.0,
        low=101.5,
        close=102.0,
        atr=2.0,
    )
    confirmed, reason, displacement = confirmation(
        episode=_episode(exhausted_side="UP", reversal_side="SELL"),
        prior_bars=(prior,),
        current=current,
        current_bar_index=10,
        config=AUCTION_POLICY_EXPERIMENT_CONFIG,
    )
    assert confirmed is False
    assert reason == "WAIT_FOR_LATER_COMPLETED_BAR"
    assert displacement == 0.0


def test_initial_room_is_mandatory() -> None:
    prior = _snapshot(
        ts=datetime(2026, 7, 27, 9, 45),
        open_price=103.5,
        high=104.0,
        low=102.8,
        close=103.6,
        atr=2.0,
    )
    current = _snapshot(
        ts=datetime(2026, 7, 27, 9, 48),
        open_price=103.4,
        high=103.5,
        low=101.6,
        close=101.8,
        atr=2.0,
    )
    confirmed, reason, displacement = confirmation(
        episode=_episode(
            exhausted_side="UP",
            reversal_side="SELL",
            initial_room_qualified=False,
        ),
        prior_bars=(prior,),
        current=current,
        current_bar_index=11,
        config=AUCTION_POLICY_EXPERIMENT_CONFIG,
    )
    assert confirmed is False
    assert reason == "INITIAL_VWAP_ROOM_NOT_QUALIFIED"
    assert displacement == 0.0


def test_upside_exhaustion_confirms_on_later_local_support_break() -> None:
    first = _snapshot(
        ts=datetime(2026, 7, 27, 9, 42),
        open_price=103.0,
        high=103.8,
        low=102.9,
        close=103.6,
        atr=2.0,
    )
    second = _snapshot(
        ts=datetime(2026, 7, 27, 9, 45),
        open_price=103.5,
        high=104.0,
        low=102.8,
        close=103.6,
        atr=2.0,
    )
    current = _snapshot(
        ts=datetime(2026, 7, 27, 9, 48),
        open_price=103.4,
        high=103.5,
        low=101.6,
        close=101.8,
        atr=2.0,
    )
    confirmed, reason, displacement = confirmation(
        episode=_episode(exhausted_side="UP", reversal_side="SELL"),
        prior_bars=(first, second),
        current=current,
        current_bar_index=11,
        config=AUCTION_POLICY_EXPERIMENT_CONFIG,
    )
    assert confirmed is True
    assert reason == "LOWER_HIGH_AND_LOCAL_SUPPORT_BREAK"
    assert displacement == pytest.approx(1.1)


def test_downside_exhaustion_confirms_symmetrically() -> None:
    first = _snapshot(
        ts=datetime(2026, 7, 24, 9, 42),
        open_price=97.0,
        high=97.1,
        low=96.2,
        close=96.4,
        atr=2.0,
    )
    second = _snapshot(
        ts=datetime(2026, 7, 24, 9, 45),
        open_price=96.5,
        high=97.2,
        low=96.0,
        close=96.4,
        atr=2.0,
    )
    current = _snapshot(
        ts=datetime(2026, 7, 24, 9, 48),
        open_price=96.6,
        high=98.3,
        low=96.5,
        close=98.2,
        atr=2.0,
    )
    confirmed, reason, displacement = confirmation(
        episode=_episode(exhausted_side="DOWN", reversal_side="BUY"),
        prior_bars=(first, second),
        current=current,
        current_bar_index=11,
        config=AUCTION_POLICY_EXPERIMENT_CONFIG,
    )
    assert confirmed is True
    assert reason == "HIGHER_LOW_AND_LOCAL_RESISTANCE_BREAK"
    assert displacement == pytest.approx(1.1)


def test_reversal_failure_requires_buffered_accepted_closes() -> None:
    episode = _episode(exhausted_side="UP", reversal_side="SELL")
    episode.confirmation_time = datetime(2026, 7, 27, 9, 48)
    episode.confirmation_price = 101.8
    first = _snapshot(
        ts=datetime(2026, 7, 27, 10, 0),
        open_price=104.1,
        high=104.5,
        low=104.0,
        close=104.6,
        atr=2.0,
    )
    second = _snapshot(
        ts=datetime(2026, 7, 27, 10, 3),
        open_price=104.2,
        high=104.8,
        low=104.1,
        close=104.5,
        atr=2.0,
    )
    update_failure(episode, first, AUCTION_POLICY_EXPERIMENT_CONFIG)
    assert episode.failure_candidate_time == datetime(2026, 7, 27, 10, 0)
    assert episode.reversal_failed_time is None
    update_failure(episode, second, AUCTION_POLICY_EXPERIMENT_CONFIG)
    assert episode.reversal_failed_time == datetime(2026, 7, 27, 10, 3)
    assert episode.continuation_unlocked_time == datetime(2026, 7, 27, 10, 3)
    assert episode.completion_reason == "REVERSAL_FAILED_WITH_ACCEPTANCE"


def test_provisional_opening_range_without_established_at_is_not_locked() -> None:
    provisional = _snapshot(
        ts=datetime(2026, 7, 27, 9, 18),
        open_price=100.0,
        high=101.0,
        low=99.5,
        close=100.2,
        atr=1.0,
        accepted_range_id="PROVISIONAL:2026-07-27",
        accepted_range_inside=True,
        accepted_range_breakout_eligible=False,
        accepted_range_provisional=True,
        accepted_range_established_at=None,
    )
    assert range_locked(provisional, AUCTION_POLICY_EXPERIMENT_CONFIG) is False


def test_range_lock_requires_age_and_confirmed_range() -> None:
    established = datetime(2026, 7, 27, 10, 45)
    too_young = _snapshot(
        ts=datetime(2026, 7, 27, 10, 54),
        open_price=100.0,
        high=101.0,
        low=99.5,
        close=100.2,
        atr=1.0,
        accepted_range_id="R1",
        accepted_range_inside=True,
        accepted_range_breakout_eligible=True,
        accepted_range_provisional=False,
        accepted_range_established_at=established,
    )
    mature = _snapshot(
        ts=datetime(2026, 7, 27, 10, 57),
        open_price=100.0,
        high=101.0,
        low=99.5,
        close=100.2,
        atr=1.0,
        accepted_range_id="R1",
        accepted_range_inside=True,
        accepted_range_breakout_eligible=True,
        accepted_range_provisional=False,
        accepted_range_established_at=established,
    )
    assert range_locked(too_young, AUCTION_POLICY_EXPERIMENT_CONFIG) is False
    assert range_locked(mature, AUCTION_POLICY_EXPERIMENT_CONFIG) is True


def test_failed_breakout_is_exempt_inside_locked_range() -> None:
    assert range_blocks_setup(
        setup="FAILED_BREAKOUT",
        locked=True,
        config=AUCTION_POLICY_EXPERIMENT_CONFIG,
    ) is False
    assert range_blocks_setup(
        setup="REVERSAL",
        locked=True,
        config=AUCTION_POLICY_EXPERIMENT_CONFIG,
    ) is True
    assert range_blocks_setup(
        setup="ACCEPTED_BREAKOUT",
        locked=True,
        config=AUCTION_POLICY_EXPERIMENT_CONFIG,
    ) is True
