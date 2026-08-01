from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from models.trade_models import StockRank
from services.selection.stock_rank import StockRankEvaluator


TS = datetime(2026, 7, 30, 12, 0)


def _ns(**kwargs):
    return SimpleNamespace(**kwargs)


def _snapshot(
    *,
    symbol: str,
    ts: datetime,
    close: float,
    previous_close: float,
    today_open: float,
    move15_pct: float,
    move30_pct: float,
    move60_pct: float,
    sod_pct: float,
    move15_atr: float,
    move30_atr: float,
    move60_atr: float,
    bar_rvol: float = 1.5,
    range_low: float | None = None,
    range_high: float | None = None,
    range_started_at: datetime | None = None,
    containment: float | None = None,
    failed_escapes: int = 0,
    rearm_required: bool = False,
):
    range_active = range_low is not None and range_high is not None
    balance = _ns(
        frozen_low=range_low,
        frozen_high=range_high,
        episode_id="EP:1" if range_active else None,
        range_id="RANGE:1" if range_active else None,
        started_at=range_started_at,
        containment_ratio=containment or 0.0,
        failed_escape_count=failed_escapes,
        rearm_required=rearm_required,
        attempt_limit_reached=False,
    )
    auction = _ns(
        status="OK" if range_active else "NOT_RUN",
        lifecycle=_ns(balance=balance) if range_active else None,
    )
    accepted_range = _ns(
        provisional=not range_active,
        low=range_low,
        high=range_high,
        range_id="RANGE:1" if range_active else None,
        start_time=range_started_at,
    )
    accepted = _ns(
        range=accepted_range,
        promoted_time=range_started_at,
        age_bars=0,
        metrics=_ns(close_occupancy_ratio=containment),
    )
    return _ns(
        symbol=symbol,
        snapshot_time=ts,
        close=close,
        bar=_ns(high=close + 0.10, low=close - 0.10),
        levels=_ns(
            prev_day=_ns(close=previous_close),
            today=_ns(open=today_open),
        ),
        indicators=_ns(
            atr=_ns(value=1.0, pct=1.0),
            vwap=_ns(value=close),
        ),
        volume=_ns(bar_rvol=bar_rvol),
        market_windows=_ns(
            m15=_ns(move_pct=move15_pct, move_atr=move15_atr),
            m30=_ns(move_pct=move30_pct, move_atr=move30_atr),
            m60=_ns(move_pct=move60_pct, move_atr=move60_atr),
            sod=_ns(move_pct=sod_pct, move_atr=sod_pct),
        ),
        auction=auction,
        structure=_ns(accepted=accepted),
    )


def test_stock_rank_model_has_required_identity_and_indexes() -> None:
    table = StockRank.__table__
    unique_names = {constraint.name for constraint in table.constraints if constraint.name}
    index_names = {index.name for index in table.indexes}

    assert "uq_stock_rank_symbol_time" in unique_names
    assert {
        "idx_stock_rank_run",
        "idx_stock_rank_time_position",
        "idx_stock_rank_day_symbol",
        "idx_stock_rank_day_class",
        "idx_stock_rank_day_tier",
        "idx_stock_rank_day_score",
    }.issubset(index_names)


def test_clean_directional_mover_outranks_stalled_gap_range() -> None:
    evaluator = StockRankEvaluator()

    mover_history = [
        _snapshot(
            symbol="MOVER",
            ts=TS - timedelta(minutes=3 * (14 - idx)),
            close=100.0 + idx * 0.30,
            previous_close=99.0,
            today_open=100.0,
            move15_pct=0.60,
            move30_pct=1.10,
            move60_pct=1.80,
            sod_pct=2.50,
            move15_atr=0.70,
            move30_atr=1.20,
            move60_atr=2.00,
        )
        for idx in range(15)
    ]
    mover = mover_history[-1]

    closes = [105.00, 105.25, 104.95, 105.30, 104.90, 105.28, 104.92,
              105.26, 104.94, 105.24, 104.96, 105.22, 104.98, 105.20, 105.00]
    range_start = TS - timedelta(minutes=42)
    range_history = [
        _snapshot(
            symbol="GAPRANGE",
            ts=TS - timedelta(minutes=3 * (14 - idx)),
            close=close,
            previous_close=100.0,
            today_open=105.0,
            move15_pct=0.03,
            move30_pct=0.08,
            move60_pct=0.10,
            sod_pct=5.0,
            move15_atr=0.03,
            move30_atr=0.08,
            move60_atr=0.10,
            range_low=104.80,
            range_high=105.40,
            range_started_at=range_start,
            containment=0.90,
            failed_escapes=1,
        )
        for idx, close in enumerate(closes)
    ]
    gap_range = range_history[-1]

    ranked = evaluator.rank(
        [gap_range, mover],
        {"MOVER": mover_history, "GAPRANGE": range_history},
    )

    assert [row.symbol for row in ranked] == ["MOVER", "GAPRANGE"]
    assert ranked[0].classification == "MOVING_UP"
    assert ranked[0].attention_tier == "PRIORITY"
    assert ranked[1].classification in {"RANGE_BOUND", "STALLED_GAP_RANGE"}
    assert ranked[1].attention_tier == "SUPPRESSED"
    assert ranked[1].range_penalty > ranked[0].range_penalty
    assert ranked[1].total_score < ranked[0].total_score


def test_gap_is_context_not_permanent_movement_score() -> None:
    evaluator = StockRankEvaluator()
    range_start = TS - timedelta(minutes=45)
    closes = [105.0, 105.2, 104.9, 105.25, 104.95, 105.20, 104.92,
              105.18, 104.94, 105.16, 104.96, 105.14, 104.98, 105.12, 105.0]
    history = [
        _snapshot(
            symbol="GAP",
            ts=TS - timedelta(minutes=3 * (14 - idx)),
            close=close,
            previous_close=100.0,
            today_open=105.0,
            move15_pct=0.02,
            move30_pct=0.05,
            move60_pct=0.08,
            sod_pct=5.0,
            move15_atr=0.02,
            move30_atr=0.05,
            move60_atr=0.08,
            range_low=104.8,
            range_high=105.4,
            range_started_at=range_start,
            containment=0.92,
            failed_escapes=2,
            rearm_required=True,
        )
        for idx, close in enumerate(closes)
    ]

    result = evaluator.evaluate(history[-1], history)

    assert result.gap_pct == 5.0
    assert result.classification in {"RANGE_BOUND", "STALLED_GAP_RANGE"}
    assert result.range_penalty >= 65.0
    assert result.total_score < result.movement_score



def test_attention_tier_requires_absolute_score_and_actionable_classification() -> None:
    evaluator = StockRankEvaluator()
    weak = _snapshot(
        symbol="WEAK", ts=TS, close=100.0, previous_close=100.0, today_open=100.0,
        move15_pct=0.01, move30_pct=0.02, move60_pct=0.03, sod_pct=0.05,
        move15_atr=0.01, move30_atr=0.02, move60_atr=0.03, bar_rvol=0.2,
    )
    result = evaluator.rank([weak], {"WEAK": [weak]})[0]
    assert result.rank_position == 1
    assert result.attention_tier == "SUPPRESSED"


def test_replay_cadence_selection_uses_six_minute_spacing() -> None:
    from tests.replays.replay_stock_rank import select_cadences

    times = [TS + timedelta(minutes=value) for value in (0, 3, 6, 9, 12)]
    selected = select_cadences(times, 6)
    assert selected == [times[0], times[2], times[4]]


def test_stock_rank_config_has_production_service_contract() -> None:
    from configs.stock_rank_config import STOCK_RANK_CONFIG

    assert STOCK_RANK_CONFIG.cadence_minutes == 6
    assert STOCK_RANK_CONFIG.snapshot_completion_lag_seconds == 300
    assert STOCK_RANK_CONFIG.priority_rank_max == 25
    assert STOCK_RANK_CONFIG.secondary_rank_max == 50
