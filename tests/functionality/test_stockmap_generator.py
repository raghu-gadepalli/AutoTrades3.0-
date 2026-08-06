from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from services.stockmap.stockmap_generator import (
    StockMapGenerator,
    _add_bootstrap_indicators,
    _latest_completed_boundary,
)

IST = ZoneInfo("Asia/Kolkata")


def _synthetic_15m(days: int = 14) -> pd.DataFrame:
    rows = []
    current_day = date(2026, 7, 20)
    price = 100.0
    created_days = 0
    while created_days < days:
        if current_day.weekday() < 5:
            start = datetime.combine(current_day, datetime.min.time(), tzinfo=IST).replace(
                hour=9,
                minute=15,
            )
            for index in range(24):
                ts = start + timedelta(minutes=15 * index)
                drift = 0.08 if created_days % 3 else -0.03
                open_ = price
                close = max(1.0, open_ + drift + (0.02 if index % 4 == 0 else -0.01))
                high = max(open_, close) + 0.12
                low = min(open_, close) - 0.12
                rows.append(
                    {
                        "date": ts,
                        "open": open_,
                        "high": high,
                        "low": low,
                        "close": close,
                        "volume": 1000 + index * 10,
                    }
                )
                price = close
            created_days += 1
        current_day += timedelta(days=1)
    return pd.DataFrame(rows)


def test_latest_completed_boundary_returns_completed_candle_start_label():
    assert _latest_completed_boundary(
        datetime(2026, 8, 5, 10, 7, tzinfo=IST)
    ) == datetime(2026, 8, 5, 9, 45, tzinfo=IST)
    assert _latest_completed_boundary(
        datetime(2026, 8, 5, 15, 31, tzinfo=IST)
    ) == datetime(2026, 8, 5, 15, 0, tzinfo=IST)


def test_replay_builds_one_causal_map_per_completed_15m_candle():
    frame = _add_bootstrap_indicators(_synthetic_15m())
    replay_day = frame.iloc[-1]["date"].date()

    generator = object.__new__(StockMapGenerator)
    generator.symbol = "TEST"
    generator.token = 1
    generator.fetcher = None

    maps = generator._replay_frame(
        frame,
        persist=False,
        persist_day=replay_day,
        calculation_mode="TEST_REPLAY",
    )

    assert len(maps) == 24
    assert maps[0].stockmap_time.hour == 9
    assert maps[0].stockmap_time.minute == 15
    assert maps[-1].stockmap_time.hour == 15
    assert maps[-1].stockmap_time.minute == 0

    for stockmap in maps:
        assert stockmap.bar.candle_time == stockmap.stockmap_time
        assert stockmap.diagnostics.source_end_time <= stockmap.stockmap_time
        assert stockmap.memory.stockmap_time == stockmap.stockmap_time
        assert set(stockmap.memory.model_dump()) == {"stockmap_time", "state"}
        assert stockmap.structure.accepted.range.source
        assert stockmap.indicators.ema.ema100 is not None
        assert stockmap.indicators.ema.ema200 is not None


def test_incremental_update_matches_full_replay(monkeypatch):
    frame = _add_bootstrap_indicators(_synthetic_15m())

    replay_generator = object.__new__(StockMapGenerator)
    replay_generator.symbol = "TEST"
    replay_generator.token = 1
    replay_generator.fetcher = None
    full_maps = replay_generator._replay_frame(
        frame,
        persist=False,
        calculation_mode="TEST_FULL_REPLAY",
    )

    previous = full_maps[-4]
    expected = full_maps[-1]
    class FakeFetcher:
        def fetch_range(self, token, start, end):
            return frame.copy()

    persisted = []
    monkeypatch.setattr(
        "schemas.stockmap.StockMapSchema.fetch_stockmap",
        staticmethod(lambda symbol, stockmap_time: None),
    )
    monkeypatch.setattr(
        "schemas.stockmap.StockMapSchema.fetch_previous_for_symbol",
        staticmethod(lambda symbol, before_time: previous),
    )
    monkeypatch.setattr(
        "schemas.stockmap.StockMapSchema.create_stockmap",
        staticmethod(lambda stockmap: persisted.append(stockmap) or stockmap),
    )

    incremental_generator = object.__new__(StockMapGenerator)
    incremental_generator.symbol = "TEST"
    incremental_generator.token = 1
    incremental_generator.fetcher = FakeFetcher()

    actual = incremental_generator.generate_stockmap(
        expected.stockmap_time + timedelta(minutes=15),
        persist_stockmap=True,
        skip_existing=True,
    )

    assert actual is not None
    assert len(persisted) == 3
    assert actual.stockmap_time == expected.stockmap_time
    assert actual.indicators == expected.indicators
    assert actual.structure == expected.structure
    assert actual.location == expected.location


def test_generate_day_resumes_from_previous_session(monkeypatch):
    frame = _add_bootstrap_indicators(_synthetic_15m())
    trading_days = sorted(frame["date"].dt.date.unique())
    previous_day = trading_days[-2]
    replay_day = trading_days[-1]

    control_generator = object.__new__(StockMapGenerator)
    control_generator.symbol = "TEST"
    control_generator.token = 1
    control_generator.fetcher = None
    full_maps = control_generator._replay_frame(
        frame,
        persist=False,
        calculation_mode="TEST_CONTROL",
    )
    previous = [m for m in full_maps if m.stockmap_time.date() == previous_day][-1]
    expected_day = [m for m in full_maps if m.stockmap_time.date() == replay_day]

    persisted = []

    class FakeFetcher:
        def fetch_range(self, token, start, end):
            return frame.copy()

    def fetch_stockmap(symbol, stockmap_time):
        return next(
            (m for m in persisted if m.stockmap_time == stockmap_time),
            None,
        )

    def fetch_previous(symbol, before_time):
        candidates = [previous] + [m for m in persisted if m.stockmap_time < before_time]
        return max(candidates, key=lambda m: m.stockmap_time)

    def create_stockmap(stockmap):
        persisted.append(stockmap)
        return stockmap

    def fetch_range(symbol, start_time, end_time):
        return sorted(
            [
                m
                for m in persisted
                if start_time <= m.stockmap_time <= end_time
            ],
            key=lambda m: m.stockmap_time,
        )

    monkeypatch.setattr(
        "schemas.stockmap.StockMapSchema.fetch_stockmap",
        staticmethod(fetch_stockmap),
    )
    monkeypatch.setattr(
        "schemas.stockmap.StockMapSchema.fetch_previous_for_symbol",
        staticmethod(fetch_previous),
    )
    monkeypatch.setattr(
        "schemas.stockmap.StockMapSchema.create_stockmap",
        staticmethod(create_stockmap),
    )
    monkeypatch.setattr(
        "schemas.stockmap.StockMapSchema.fetch_range",
        staticmethod(fetch_range),
    )

    generator = object.__new__(StockMapGenerator)
    generator.symbol = "TEST"
    generator.token = 1
    generator.fetcher = FakeFetcher()

    actual_day = generator.generate_day(
        replay_day,
        persist_stockmaps=True,
        skip_existing=True,
    )

    assert len(persisted) == 24
    assert len(actual_day) == 24
    assert actual_day[0].stockmap_time.hour == 9
    assert actual_day[0].stockmap_time.minute == 15
    assert actual_day[-1].stockmap_time.hour == 15
    assert actual_day[-1].stockmap_time.minute == 0
    assert actual_day[-1].indicators == expected_day[-1].indicators
    assert actual_day[-1].structure == expected_day[-1].structure
    assert actual_day[-1].location == expected_day[-1].location
