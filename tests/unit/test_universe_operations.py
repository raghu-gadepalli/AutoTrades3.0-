from __future__ import annotations

from operations.filter_stock_universe import build_policy_decisions
from operations.generate_stock_universe import (
    UniverseCandidate,
    calculate_historical_metrics,
    select_active_universe,
)


def test_policy_filter_whitelist_overrides_blacklist() -> None:
    decisions = build_policy_decisions(
        rows=[("LT", "EQ", False), ("IDEA", "EQ", True), ("INFY", "EQ", False)],
        whitelist={"LT"},
        blacklist={"LT", "IDEA"},
    )
    by_symbol = {row.symbol: row for row in decisions}

    assert by_symbol["LT"].proposed_enabled is True
    assert by_symbol["LT"].reason == "WHITELIST_OVERRIDES_BLACKLIST"
    assert by_symbol["IDEA"].proposed_enabled is False
    assert by_symbol["IDEA"].reason == "BLACKLIST"
    assert by_symbol["INFY"].proposed_enabled is True
    assert by_symbol["INFY"].reason == "ELIGIBLE_EQ"


def test_historical_metrics_use_completed_daily_path() -> None:
    bars = []
    price = 100.0
    for index in range(70):
        close = price + (index * 0.5)
        bars.append(
            {
                "open": close - 0.2,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 100000.0 + index,
            }
        )

    metrics = calculate_historical_metrics(bars, minimum_history_days=60, atr_period=14)

    assert metrics.history_days == 70
    assert metrics.latest_close == bars[-1]["close"]
    assert metrics.atr_pct > 0
    assert metrics.median_range_pct > 0
    assert metrics.median_turnover_lakh > 0
    assert 0 <= metrics.directional_efficiency <= 1
    assert 0 <= metrics.active_day_ratio <= 1


def _candidate(
    symbol: str,
    score: float,
    *,
    active: bool = False,
    whitelisted: bool = False,
) -> UniverseCandidate:
    return UniverseCandidate(
        symbol=symbol,
        enabled=True,
        current_active=active,
        whitelisted=whitelisted,
        blacklisted=False,
        derivative_available=True,
        valid=True,
        error="",
        history_days=80,
        latest_close=100.0,
        atr_pct=1.0,
        median_range_pct=1.0,
        median_turnover_lakh=1000.0,
        directional_efficiency=0.5,
        active_day_ratio=0.8,
        price_economics=1.0,
        total_score=score,
    )


def test_active_selection_retains_current_member_inside_hysteresis_window() -> None:
    candidates = [
        _candidate("CORE", 0.10, active=True, whitelisted=True),
        _candidate("NEW1", 0.90),
        _candidate("NEW2", 0.85),
        _candidate("HELD", 0.80, active=True),
    ]

    result = select_active_universe(
        candidates,
        active_limit=3,
        hysteresis_slots=1,
        minimum_coverage_pct=90.0,
    )

    assert result.safe_to_apply is True
    assert set(result.selected_symbols) == {"CORE", "NEW1", "HELD"}
    by_symbol = {row.symbol: row for row in result.candidates}
    assert by_symbol["HELD"].selection_reason == "RETAINED_HYSTERESIS"
    assert by_symbol["NEW2"].proposed_active is False
