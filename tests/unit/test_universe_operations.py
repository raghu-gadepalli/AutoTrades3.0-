from __future__ import annotations

from datetime import date, datetime, timedelta

from operations.filter_stock_universe import EqPolicyRow, build_policy_decisions
from operations.generate_stock_universe import (
    EarlyMoveStats,
    IntradayHistory,
    MoverStats,
    PolicyMoverBehavior,
    SymbolHistory,
    UniverseCandidate,
    UniverseSymbol,
    _completed_bar_date,
    _write_reports,
    build_top_mover_stats,
    calculate_early_move_stats,
    calculate_historical_metrics,
    calculate_intraday_day_profile,
    calculate_policy_mover_behavior,
    inspect_whitelist_preflight,
    score_candidates,
    select_active_universe,
    summarize_mover_capture,
)
from utils.datetime_utils import IST


def test_policy_filter_whitelist_overrides_blacklist() -> None:
    rows = [
        EqPolicyRow("LT", "EQ", "NSE", False, 150.0),
        EqPolicyRow("IDEA", "EQ", "NSE", True, 20.0),
        EqPolicyRow("INFY", "EQ", "NSE", False, 1400.0),
    ]
    decisions = build_policy_decisions(
        rows=rows,
        whitelist={"LT"},
        blacklist={"LT", "IDEA"},
        quote_prices={"LT": 150.0, "IDEA": 20.0, "INFY": 1400.0},
        minimum_price=200.0,
    )
    by_symbol = {row.symbol: row for row in decisions}

    assert by_symbol["LT"].proposed_enabled is True
    assert by_symbol["LT"].reason == "WHITELIST_OVERRIDES_BLACKLIST"
    assert by_symbol["IDEA"].proposed_enabled is False
    assert by_symbol["INFY"].proposed_enabled is True


def test_policy_filter_applies_minimum_price_and_preserves_unresolved_state() -> None:
    rows = [
        EqPolicyRow("CHEAP", "EQ", "NSE", True, 190.0),
        EqPolicyRow("EXPENSIVE", "EQ", "NSE", False, 250.0),
        EqPolicyRow("NOQUOTE", "EQ", "NSE", False, None),
    ]
    decisions = build_policy_decisions(
        rows=rows,
        whitelist=set(),
        blacklist=set(),
        quote_prices={"CHEAP": 199.95, "EXPENSIVE": 250.0},
        minimum_price=200.0,
    )
    by_symbol = {row.symbol: row for row in decisions}

    assert by_symbol["CHEAP"].proposed_enabled is False
    assert by_symbol["CHEAP"].reason == "BELOW_MIN_PRICE"
    assert by_symbol["EXPENSIVE"].proposed_enabled is True
    assert by_symbol["NOQUOTE"].proposed_enabled is False
    assert by_symbol["NOQUOTE"].reason == "QUOTE_UNAVAILABLE"


def _bars(count: int = 60, *, start: date = date(2026, 5, 1), scale: float = 1.0):
    rows = []
    price = 100.0
    for index in range(count):
        open_price = price + index * 0.2
        rows.append(
            {
                "bar_date": start + timedelta(days=index),
                "open": open_price,
                "high": open_price + 1.2 * scale,
                "low": open_price - 0.8 * scale,
                "close": open_price + 0.5 * scale,
                "volume": 100000 + index * 100,
                "excursion_pct": (1.2 * scale / open_price) * 100.0,
                "follow_through_pct": (0.5 * scale / open_price) * 100.0,
            }
        )
        price = open_price
    return rows


def test_historical_metrics_use_60_day_window_and_consistency() -> None:
    metrics = calculate_historical_metrics(_bars(), minimum_history_days=60, atr_period=14)

    assert metrics.history_days == 60
    assert metrics.atr_pct > 0
    assert metrics.median_excursion_pct > 0
    assert metrics.p90_excursion_pct >= metrics.median_excursion_pct
    assert metrics.median_turnover_lakh > 0
    assert 0 <= metrics.directional_efficiency <= 1
    assert 0 <= metrics.movement_consistency <= 1


def _symbol(
    name: str,
    *,
    enabled: bool = True,
    active: bool = False,
    derivative: bool = True,
    whitelisted: bool = False,
) -> UniverseSymbol:
    return UniverseSymbol(
        symbol=name,
        token="1",
        exchange="NSE",
        enabled=enabled,
        active=active,
        derivative_available=derivative,
        whitelisted=whitelisted,
    )


def test_top_mover_stats_count_days_and_distinct_weeks() -> None:
    dates = [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 8)]

    def history(name: str, excursions: list[float]) -> SymbolHistory:
        bars = []
        for day, excursion in zip(dates, excursions):
            bars.append(
                {
                    "bar_date": day,
                    "open": 100.0,
                    "high": 100.0 + excursion,
                    "low": 99.0,
                    "close": 100.5,
                    "volume": 1000.0,
                    "excursion_pct": excursion,
                    "follow_through_pct": 0.5,
                }
            )
        return SymbolHistory(_symbol(name), tuple(bars))

    stats, daily = build_top_mover_stats(
        [
            history("ALPHA", [5.0, 5.0, 5.0]),
            history("BETA", [4.0, 1.0, 4.0]),
            history("GAMMA", [1.0, 4.0, 1.0]),
        ],
        dates,
        top_movers_per_day=2,
    )

    assert stats["ALPHA"].top20_days == 3
    assert stats["ALPHA"].top20_weeks == 2
    assert stats["ALPHA"].top20_best_rank == 1
    assert stats["BETA"].top20_days == 2
    assert len(daily) == 3


def _candidate(
    symbol: str,
    score_seed: float,
    *,
    enabled: bool = True,
    active: bool = False,
    whitelisted: bool = False,
    derivative: bool = True,
    valid: bool = True,
    top20_days: int = 0,
    top20_weeks: int = 0,
) -> UniverseCandidate:
    return UniverseCandidate(
        symbol=symbol,
        enabled=enabled,
        active_flag_before=active,
        operationally_active_before=enabled and active,
        whitelisted=whitelisted,
        derivative_available=derivative,
        selection_eligible=enabled and derivative,
        valid_history=valid,
        error="" if valid else "missing_history",
        history_days=60 if valid else 0,
        latest_bar_date=date(2026, 7, 31) if valid else None,
        latest_close=100.0 if valid else None,
        atr_pct=score_seed if valid else None,
        median_excursion_pct=score_seed if valid else None,
        p90_excursion_pct=score_seed * 1.5 if valid else None,
        median_turnover_lakh=score_seed * 1000 if valid else None,
        directional_efficiency=min(score_seed / 10, 1.0) if valid else None,
        movement_consistency=0.5 if valid else None,
        top20_days_60d=top20_days,
        top20_weeks_60d=top20_weeks,
    )


def test_scoring_rewards_persistent_movers_when_core_metrics_match() -> None:
    occasional = _candidate("OCCASIONAL", 2.0, top20_days=4, top20_weeks=1)
    persistent = _candidate("PERSISTENT", 2.0, top20_days=4, top20_weeks=4)
    anchor = _candidate("ANCHOR", 3.0, top20_days=10, top20_weeks=8)

    scored = score_candidates([occasional, persistent, anchor])
    by_symbol = {row.symbol: row for row in scored}

    assert by_symbol["PERSISTENT"].total_score > by_symbol["OCCASIONAL"].total_score


def test_selection_uses_enabled_and_active_as_operational_state() -> None:
    candidates = score_candidates(
        [
            _candidate("ACTIVE", 3.0, enabled=True, active=True),
            _candidate("RAW_ACTIVE_DISABLED", 9.0, enabled=False, active=True),
            _candidate("NEW", 2.0, enabled=True, active=False),
        ]
    )
    result = select_active_universe(
        candidates,
        active_limit=2,
        hysteresis_score_gap=0.0,
        minimum_history_coverage_pct=90.0,
        minimum_derivative_coverage_pct=90.0,
    )

    assert result.current_operational_active_count == 1
    assert set(result.selected_symbols) == {"ACTIVE", "NEW"}
    by_symbol = {row.symbol: row for row in result.candidates}
    assert by_symbol["RAW_ACTIVE_DISABLED"].active_action == "REMAIN_INACTIVE"
    assert by_symbol["RAW_ACTIVE_DISABLED"].selection_reason == "DISABLED_BY_FILTER"


def test_whitelist_is_forced_inside_configured_limit() -> None:
    candidates = score_candidates(
        [
            _candidate("WHITE", 0.1, whitelisted=True),
            _candidate("STRONG", 5.0),
            _candidate("MEDIUM", 4.0),
        ]
    )
    result = select_active_universe(
        candidates,
        active_limit=2,
        hysteresis_score_gap=0.0,
        minimum_history_coverage_pct=90.0,
        minimum_derivative_coverage_pct=90.0,
    )

    assert set(result.selected_symbols) == {"WHITE", "STRONG"}
    by_symbol = {row.symbol: row for row in result.candidates}
    assert by_symbol["WHITE"].selection_reason == "WHITELIST"


def test_hysteresis_retains_only_close_incumbent() -> None:
    close_incumbent = _candidate("HELD_CLOSE", 2.95, active=True)
    newcomer = _candidate("NEW", 3.0)
    stronger = _candidate("CORE", 4.0, active=True)
    rows = score_candidates([close_incumbent, newcomer, stronger])

    # Override total scores to exercise the score-gap contract directly.
    adjusted = []
    for row in rows:
        score = {"CORE": 0.90, "NEW": 0.80, "HELD_CLOSE": 0.79}[row.symbol]
        adjusted.append(row.__class__(**{**row.__dict__, "total_score": score}))

    result = select_active_universe(
        adjusted,
        active_limit=2,
        hysteresis_score_gap=0.02,
        minimum_history_coverage_pct=90.0,
        minimum_derivative_coverage_pct=90.0,
    )

    assert set(result.selected_symbols) == {"CORE", "HELD_CLOSE"}


def test_materially_weaker_incumbent_is_not_retained() -> None:
    rows = [
        _candidate("CORE", 4.0, active=True),
        _candidate("NEW", 3.0),
        _candidate("HELD_WEAK", 1.0, active=True),
    ]
    scored = score_candidates(rows)
    adjusted = []
    for row in scored:
        score = {"CORE": 0.90, "NEW": 0.80, "HELD_WEAK": 0.60}[row.symbol]
        adjusted.append(row.__class__(**{**row.__dict__, "total_score": score}))

    result = select_active_universe(
        adjusted,
        active_limit=2,
        hysteresis_score_gap=0.02,
        minimum_history_coverage_pct=90.0,
        minimum_derivative_coverage_pct=90.0,
    )

    assert set(result.selected_symbols) == {"CORE", "NEW"}


def test_mover_capture_classifies_policy_and_selection_misses() -> None:
    candidates = score_candidates(
        [
            _candidate("CAPTURED", 4.0, top20_days=5, top20_weeks=3),
            _candidate("UNSELECTED", 2.0, top20_days=4, top20_weeks=2),
            _candidate("DISABLED", 9.0, enabled=False, top20_days=3, top20_weeks=2),
        ]
    )
    result = select_active_universe(
        candidates,
        active_limit=1,
        hysteresis_score_gap=0.0,
        minimum_history_coverage_pct=90.0,
        minimum_derivative_coverage_pct=90.0,
    )
    summary = summarize_mover_capture(
        result,
        [date(2026, 6, 1) + timedelta(days=index) for index in range(10)],
    )

    assert summary.total_top_mover_appearances == 12
    assert summary.captured_appearances == 5
    assert summary.enabled_not_selected_appearances == 4
    assert summary.policy_disabled_appearances == 3


def test_whitelist_preflight_blocks_disabled_or_missing_derivative() -> None:
    universe = [
        _symbol("NIFTY 50", enabled=False, derivative=True, whitelisted=True),
        _symbol("MARUTI", enabled=True, derivative=False, whitelisted=True),
    ]
    preflight = inspect_whitelist_preflight(universe)

    assert "NIFTY 50" in preflight.disabled_symbols
    assert "MARUTI" in preflight.missing_derivative_symbols
    assert preflight.safe_to_apply is False


def test_underfilled_target_blocks_apply() -> None:
    candidates = score_candidates([_candidate("ONLY", 2.0)])
    result = select_active_universe(
        candidates,
        active_limit=2,
        hysteresis_score_gap=0.0,
        minimum_history_coverage_pct=90.0,
        minimum_derivative_coverage_pct=90.0,
    )

    assert result.safe_to_apply is False
    assert "selection_underfilled:1<2" in result.unsafe_reasons


def test_today_as_of_before_close_uses_previous_completed_day() -> None:
    as_of = datetime(2026, 7, 31, 23, 59, 59, tzinfo=IST)
    now = datetime(2026, 7, 31, 14, 0, 0, tzinfo=IST)

    assert _completed_bar_date(as_of, now=now) == date(2026, 7, 30)



def _policy_reversal_day(day: date) -> list[dict]:
    start = datetime.combine(day, datetime.min.time(), tzinfo=IST).replace(
        hour=9, minute=15
    )
    values = [
        (100.0, 101.0, 99.8, 100.8),
        (100.8, 102.0, 100.5, 101.8),
        (101.8, 103.0, 101.5, 102.8),
        (102.8, 102.9, 101.5, 101.7),
        (101.7, 101.9, 100.2, 100.4),
        (100.4, 100.6, 99.8, 100.0),
    ]
    return [
        {
            "timestamp": start + timedelta(minutes=15 * index),
            "bar_date": day,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1000.0,
        }
        for index, (open_price, high, low, close) in enumerate(values)
    ]


def test_intraday_profile_detects_post_peak_reversal() -> None:
    day = date(2026, 6, 1)
    profile = calculate_intraday_day_profile(
        _policy_reversal_day(day),
        market_open_time=datetime.strptime("09:15:00", "%H:%M:%S").time(),
        early_window_minutes=45,
        containment_tolerance_pct=0.10,
    )

    assert profile.dominant_direction == "UP"
    assert profile.peak_retracement_ratio >= 1.0
    assert profile.full_reversal is True
    assert profile.close_retention_ratio == 0.0


def test_policy_mover_behavior_flags_intraday_reversal_and_event_clustering() -> None:
    symbol = _symbol("MANAGED", enabled=False)
    days = [date(2026, 6, 1) + timedelta(days=index) for index in range(4)]
    daily_bars = []
    intraday_bars = []
    for day in days:
        daily_bars.append(
            {
                "bar_date": day,
                "open": 100.0,
                "high": 103.0,
                "low": 99.8,
                "close": 100.0,
                "volume": 100000.0,
                "excursion_pct": 3.0,
                "follow_through_pct": 0.0,
            }
        )
        intraday_bars.extend(_policy_reversal_day(day))

    behavior = calculate_policy_mover_behavior(
        SymbolHistory(symbol, tuple(daily_bars)),
        IntradayHistory(symbol, tuple(intraday_bars)),
        days,
        minimum_excursion_pct=1.0,
        reversal_retracement_ratio=0.50,
        full_reversal_flag_rate_pct=20.0,
        low_close_retention_ratio=0.40,
        two_sided_ratio_threshold=0.35,
        gap_driven_share_threshold=0.50,
        event_cluster_max_week_share=0.60,
        behavior_flag_rate_pct=30.0,
        market_open_time=datetime.strptime("09:15:00", "%H:%M:%S").time(),
        early_window_minutes=45,
        containment_tolerance_pct=0.10,
        minimum_bars_per_day=6,
    )

    assert behavior.intraday_days_available == 4
    assert behavior.reversal_prone_days == 4
    assert behavior.full_reversal_days == 4
    assert behavior.reversal_prone_pct == 100.0
    assert "REVERSAL_PRONE" in behavior.behavior_classification
    assert "FULL_REVERSAL_PRONE" in behavior.behavior_classification
    assert "LOW_CLOSE_RETENTION" in behavior.behavior_classification
    assert "EVENT_CLUSTERED" in behavior.behavior_classification


def _intraday_day(day: date, *, early_only: bool) -> list[dict]:
    rows = []
    start = datetime.combine(day, datetime.min.time(), tzinfo=IST).replace(hour=9, minute=15)
    open_price = 100.0
    for index in range(6):
        timestamp = start + timedelta(minutes=15 * index)
        if early_only:
            high = 100.2 if index < 3 else 98.6
            low = 98.0 if index < 3 else 98.1
            close = 98.2 if index < 3 else 98.3
        else:
            high = 100.2
            low = 99.2 if index < 3 else 97.5
            close = 99.3 if index < 3 else 97.7
        rows.append(
            {
                "timestamp": timestamp,
                "bar_date": day,
                "open": open_price if index == 0 else rows[-1]["close"],
                "high": high,
                "low": low,
                "close": close,
                "volume": 1000.0,
            }
        )
    return rows


def test_early_move_stats_separates_exhausted_open_from_late_extension() -> None:
    first_day = date(2026, 6, 1)
    second_day = date(2026, 6, 2)
    bars = _intraday_day(first_day, early_only=True) + _intraday_day(
        second_day, early_only=False
    )

    stats = calculate_early_move_stats(
        IntradayHistory(_symbol("EARLY"), tuple(bars)),
        [first_day, second_day],
        market_open_time=datetime.strptime("09:15:00", "%H:%M:%S").time(),
        early_window_minutes=45,
        minimum_session_excursion_pct=1.0,
        minimum_early_share_pct=70.0,
        maximum_post_range_extension_pct=0.35,
        minimum_post_contained_pct=70.0,
        containment_tolerance_pct=0.10,
        late_opportunity_extension_pct=0.60,
        minimum_bars_per_day=6,
        minimum_classification_days=5,
        high_rate_pct=50.0,
        moderate_rate_pct=30.0,
    )

    assert stats.intraday_days_available == 2
    assert stats.early_move_only_days == 1
    assert stats.early_move_only_pct == 50.0
    assert stats.late_opportunity_days == 1
    assert stats.median_post_contained_pct > 0
    assert stats.early_move_classification == "LIMITED_SAMPLE"


def test_early_move_stats_classifies_when_sample_is_sufficient() -> None:
    days = [date(2026, 6, 1) + timedelta(days=index) for index in range(5)]
    bars = []
    for day in days:
        bars.extend(_intraday_day(day, early_only=True))

    stats = calculate_early_move_stats(
        IntradayHistory(_symbol("EARLY5"), tuple(bars)),
        days,
        market_open_time=datetime.strptime("09:15:00", "%H:%M:%S").time(),
        early_window_minutes=45,
        minimum_session_excursion_pct=1.0,
        minimum_early_share_pct=70.0,
        maximum_post_range_extension_pct=0.35,
        minimum_post_contained_pct=70.0,
        containment_tolerance_pct=0.10,
        late_opportunity_extension_pct=0.60,
        minimum_bars_per_day=6,
        minimum_classification_days=5,
        high_rate_pct=50.0,
        moderate_rate_pct=30.0,
    )

    assert stats.meaningful_intraday_days == 5
    assert stats.early_move_only_days == 5
    assert stats.early_move_classification == "MOSTLY_EARLY_MOVE_ONLY"


def test_policy_mover_neutral_label_does_not_claim_orderliness() -> None:
    symbol = _symbol("NEUTRAL", enabled=False)
    days = [date(2026, 6, 1) + timedelta(days=index) for index in range(5)]
    daily_bars = []
    intraday_bars = []
    for day in days:
        daily_bars.append(
            {
                "bar_date": day,
                "open": 100.0,
                "high": 102.0,
                "low": 99.8,
                "close": 101.8,
                "volume": 100000.0,
                "excursion_pct": 2.0,
                "follow_through_pct": 1.8,
            }
        )
        intraday_bars.extend(_intraday_day(day, early_only=False))

    behavior = calculate_policy_mover_behavior(
        SymbolHistory(symbol, tuple(daily_bars)),
        IntradayHistory(symbol, tuple(intraday_bars)),
        days,
        minimum_excursion_pct=1.0,
        reversal_retracement_ratio=2.0,
        full_reversal_flag_rate_pct=101.0,
        low_close_retention_ratio=-1.0,
        two_sided_ratio_threshold=2.0,
        gap_driven_share_threshold=2.0,
        event_cluster_max_week_share=2.0,
        behavior_flag_rate_pct=101.0,
        market_open_time=datetime.strptime("09:15:00", "%H:%M:%S").time(),
        early_window_minutes=45,
        containment_tolerance_pct=0.10,
        minimum_bars_per_day=6,
    )

    assert behavior.behavior_classification == "NO_ADVERSE_PATTERN_DETECTED"


def test_report_writer_keeps_supplemental_sections_separate(tmp_path) -> None:
    candidates = score_candidates(
        [
            _candidate("SELECTED", 4.0, top20_days=5, top20_weeks=4),
            _candidate(
                "DISABLED",
                3.0,
                enabled=False,
                top20_days=4,
                top20_weeks=1,
            ),
        ]
    )
    result = select_active_universe(
        candidates,
        active_limit=1,
        hysteresis_score_gap=0.0,
        minimum_history_coverage_pct=90.0,
        minimum_derivative_coverage_pct=90.0,
    )
    policy = PolicyMoverBehavior(
        symbol="DISABLED",
        top20_days_60d=4,
        top20_weeks_60d=1,
        top20_best_rank_60d=1,
        top20_average_rank_60d=4.0,
        top20_last_seen_date=date(2026, 7, 30),
        daily_history_days=60,
        median_excursion_pct=2.0,
        directional_efficiency=0.25,
        movement_consistency=0.50,
        intraday_days_available=4,
        median_close_retention_ratio=0.20,
        median_peak_retracement_ratio=0.80,
        reversal_prone_days=3,
        reversal_prone_pct=75.0,
        full_reversal_days=2,
        full_reversal_pct=50.0,
        median_two_sided_ratio=0.30,
        two_sided_days=1,
        two_sided_pct=25.0,
        gap_driven_days=0,
        gap_driven_pct=0.0,
        maximum_week_share_pct=100.0,
        behavior_classification="EVENT_CLUSTERED|REVERSAL_PRONE",
        error="",
    )
    early = EarlyMoveStats(
        symbol="SELECTED",
        top20_days_60d=5,
        intraday_days_available=5,
        meaningful_intraday_days=5,
        early_move_only_days=3,
        early_move_only_pct=60.0,
        median_early_move_share_pct=85.0,
        median_post_range_extension_pct=0.10,
        median_post_contained_pct=90.0,
        late_opportunity_days=1,
        late_opportunity_pct=20.0,
        first_intraday_date=date(2026, 7, 1),
        last_intraday_date=date(2026, 7, 30),
        early_move_classification="MOSTLY_EARLY_MOVE_ONLY",
    )

    paths = _write_reports(
        str(tmp_path),
        result,
        [policy],
        [early],
        datetime(2026, 7, 31, 18, 0, tzinfo=IST),
    )

    assert len(paths) == 4
    assert all(path.exists() for path in paths)
    assert "policy_movers" in paths[2].name
    assert "early_move_only" in paths[3].name
