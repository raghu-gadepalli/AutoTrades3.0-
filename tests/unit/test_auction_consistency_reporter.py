from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

from services.auction_engine.consistency_reporter import build_report_row


class _Dumpable(SimpleNamespace):
    def model_dump(self, **_kwargs):
        def convert(value):
            if isinstance(value, _Dumpable):
                return {key: convert(item) for key, item in vars(value).items()}
            if isinstance(value, tuple):
                return [convert(item) for item in value]
            if isinstance(value, datetime):
                return value.isoformat()
            return value

        return {key: convert(value) for key, value in vars(self).items()}


def _snapshot(*, episode_direction="UP", trend_direction="UP", raw_side="BUY", slope_state="UP_ACCELERATING"):
    now = datetime(2026, 8, 3, 10, 0)
    directional = _Dumpable(
        episode_id="EP-1",
        previous_state="ACTIVE",
        current_state="ACTIVE",
        direction=episode_direction,
        origin_source="OBSERVATION_CONFIRMED",
        parent_episode_id=None,
        origin_event_id=None,
        started_at=now,
        state_started_at=now,
        state_age_bars=1,
        origin_price=100.0,
        extreme_price=101.0,
        extreme_time=now,
        protection_level=99.0,
        protection_source="TEST",
        reversal_confirmation_level=None,
        reversal_confirmation_source="",
        reversal_confirmation_level_time=None,
        reversal_leg_progress_bars=0,
        reversal_leg_progress_atr=0.0,
        reversal_leg_failure_closes=0,
        continuation_failure_seen=False,
        reason_codes=(),
    )
    balance = _Dumpable(
        episode_id=None,
        previous_state="NONE",
        current_state="NONE",
        state_age_bars=0,
        range_id=None,
        candidate_low=None,
        candidate_high=None,
        frozen_low=None,
        frozen_high=None,
        escape_direction="UNKNOWN",
        outside_close_count=0,
        reentry_close_count=0,
        escape_attempt_count=0,
        failed_escape_count=0,
        rearm_required=False,
        attempt_limit_reached=False,
        reason_codes=(),
    )
    observation = _Dumpable(
        observation_state="ORDERLY_UPTREND",
        directional_bias=episode_direction,
        trend_direction=trend_direction,
        current_leg_mature=False,
        extension_mature=False,
        exhaustion_active=False,
        exhausted_side="UNKNOWN",
        rejection_observed=False,
        failed_extreme_observed=False,
        structural_failure_confirmed=False,
        trend_protection_level=99.0,
        trend_protection_source="TEST",
        trend_protection_time=now,
        accepted_range_id=None,
        accepted_range_low=None,
        accepted_range_high=None,
        accepted_range_inside=False,
        accepted_range_position=None,
        directional_efficiency=0.8,
        directional_efficiency_source="STRUCTURE",
        overlap_ratio=0.2,
        source_reason_codes=(),
    )
    memory_directional = _Dumpable(
        sequence=1,
        start_candidate_side="UNKNOWN",
        start_candidate_bars=0,
        reversal_watch_age_bars=0,
        trend_restore_bars=0,
        opposite_control_bars=0,
        inactive_bars=0,
        last_observation_state="ORDERLY_UPTREND",
        last_observation_state_time=now,
        last_reason_codes=(),
    )
    lifecycle = _Dumpable(
        engine_name="persistent_episode_engine",
        engine_version="1",
        config_version=None,
        config_hash=None,
        directional=directional,
        balance=balance,
        events=(),
        permissions=(),
        diagnostics={},
    )
    auction_memory = _Dumpable(directional=memory_directional)
    return _Dumpable(
        symbol="TEST",
        snapshot_time=now,
        version="3",
        tf="3m",
        gen_signals=True,
        ltp=100.0,
        ltp_time=now,
        bar=_Dumpable(open=100.0, high=101.0, low=99.5, close=100.5, volume=1000.0),
        indicators=_Dumpable(atr=_Dumpable(value=1.5)),
        structure=_Dumpable(
            raw=_Dumpable(state="TREND", side=raw_side, reason="TEST"),
            accepted=_Dumpable(
                state="RANGE_ACCEPTED",
                range=_Dumpable(range_id=None, low=None, high=None),
                age_bars=0,
            ),
        ),
        price_action=_Dumpable(
            slope=_Dumpable(
                status="OK",
                state=slope_state,
                bars_3_atr_per_bar=0.2,
                bars_5_atr_per_bar=0.15,
            )
        ),
        auction=_Dumpable(
            status="OK",
            previous_snapshot_time=None,
            continuity_mode="COLD_START",
            engine=_Dumpable(name="persistent_episode_engine", version="1"),
            observation=observation,
            lifecycle=lifecycle,
        ),
        memory=_Dumpable(auction=auction_memory),
    )


def _row(snapshot, streak=0):
    return build_report_row(
        snapshot,
        db_processed=False,
        experiment_id="test",
        dataset_split="development",
        code_commit="abc",
        config_hash="cfg",
        previous=None,
        previous_opposite_streak=streak,
    )


def test_coherent_snapshot_is_reported_without_conflict():
    row, _state, streak = _row(_snapshot())

    assert row["consistency_class"] == "COHERENT"
    assert row["episode_vs_trend"] == "MATCH"
    assert row["fresh_opposite_evidence"] is False
    assert streak == 0


def test_persistent_opposite_evidence_is_visible_on_third_snapshot():
    snapshot = _snapshot(
        episode_direction="UP",
        trend_direction="DOWN",
        raw_side="SELL",
        slope_state="DOWN_ACCELERATING",
    )

    row, _state, streak = _row(snapshot, streak=2)

    assert streak == 3
    assert row["fresh_opposite_evidence"] is True
    assert row["consistency_class"] == "PERSISTENT_CONFLICT"
    assert "PERSISTENT_OPPOSITE_EVIDENCE" in row["consistency_flags"]


def test_missing_reversal_lineage_is_hard_contract_failure():
    snapshot = _snapshot()
    snapshot.auction.lifecycle.directional.origin_source = "REVERSAL_EVENT_HANDOFF"
    snapshot.auction.lifecycle.directional.parent_episode_id = None
    snapshot.auction.lifecycle.directional.origin_event_id = None

    row, _state, _streak = _row(snapshot)

    assert row["lineage_complete"] is False
    assert row["consistency_class"] == "HARD_CONTRACT_FAILURE"
    assert "REVERSAL_LINEAGE_MISSING" in row["consistency_flags"]
