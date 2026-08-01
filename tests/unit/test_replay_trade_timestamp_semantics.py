from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

from services.trade.monitor import trade_monitor as monitor
from tests.replays import replay_signal_trade_pipeline as replay


def _trade(*, intent_time, entry_time):
    return SimpleNamespace(
        id=7,
        instrument_type=SimpleNamespace(value="FUT"),
        entry_intent_time=intent_time,
        entry_time=entry_time,
        equity_ref="COFORGE",
        symbol="COFORGE26AUGFUT",
        entry_price=Decimal("1725.0"),
        executed_entry_price=Decimal("1725.0"),
    )


def test_derivative_validation_prefers_entry_intent_time(monkeypatch):
    snapshots = [
        SimpleNamespace(symbol="COFORGE", snapshot_time=datetime(2026, 7, 31, 10, 24)),
        SimpleNamespace(symbol="COFORGE", snapshot_time=datetime(2026, 7, 31, 10, 27)),
    ]
    seen = {}

    def fake_quote(*, snapshot, trade):
        seen["snapshot_time"] = snapshot.snapshot_time
        return {
            "expected_symbol": trade.symbol,
            "expected_entry_price": 1725.0,
            "snapshot_time": snapshot.snapshot_time,
        }

    monkeypatch.setattr(replay, "_derivative_quote_for_trade", fake_quote)
    rows = replay._derivative_validation_rows(
        snapshots=snapshots,
        trades=[
            _trade(
                intent_time=datetime(2026, 7, 31, 10, 27),
                entry_time=datetime(2026, 7, 31, 10, 24),
            )
        ],
    )

    assert seen["snapshot_time"] == datetime(2026, 7, 31, 10, 27)
    assert rows[0]["validation_time_source"] == "entry_intent_time"
    assert rows[0]["validation_status"] == "PASSED"


def test_derivative_validation_falls_back_to_entry_time(monkeypatch):
    snapshot = SimpleNamespace(
        symbol="COFORGE", snapshot_time=datetime(2026, 7, 31, 10, 24)
    )

    monkeypatch.setattr(
        replay,
        "_derivative_quote_for_trade",
        lambda *, snapshot, trade: {
            "expected_symbol": trade.symbol,
            "expected_entry_price": 1725.0,
            "snapshot_time": snapshot.snapshot_time,
        },
    )
    rows = replay._derivative_validation_rows(
        snapshots=[snapshot],
        trades=[
            _trade(
                intent_time=None,
                entry_time=datetime(2026, 7, 31, 10, 24),
            )
        ],
    )

    assert rows[0]["validation_time_source"] == "entry_time_fallback"
    assert rows[0]["validation_status"] == "PASSED"


def test_monitor_normalization_uses_observation_time(monkeypatch):
    observed = datetime(2026, 7, 31, 11, 45)
    captured = {}

    def fake_normalize(**kwargs):
        captured.update(kwargs)
        return {"current_stop_price": 99, "current_target_price": 101}

    monkeypatch.setattr(
        monitor.TradeMonHelper,
        "normalize_trade_management",
        fake_normalize,
    )
    monkeypatch.setattr(monitor, "extract_underlying_atr", lambda _: Decimal("2"))

    monitor._normalize_trade_management_for_monitor(
        raw={"version": 2},
        side="BUY",
        basis_price=Decimal("100"),
        snapshot_dict={"snapshot_time": observed.isoformat()},
        asof_time=observed,
        instrument_type="EQ",
    )

    assert captured["asof_time"] == observed


def test_account_governor_timestamp_matches_package_intent():
    when = datetime(2026, 7, 31, 10, 27)
    rows = [
        {
            "id": 1,
            "ts": when,
            "userid": "DR1812",
            "entity_id": "SIG-1",
            "account_governor_as_of": when.isoformat(),
        }
    ]
    trades = [
        SimpleNamespace(
            id=1,
            userid="DR1812",
            signal_id="SIG-1",
            entry_intent_time=when,
        ),
        SimpleNamespace(
            id=2,
            userid="DR1812",
            signal_id="SIG-1",
            entry_intent_time=when,
        ),
    ]

    replay._validate_account_governor_timestamps(
        rows=rows,
        trades=trades,
        trading_day=date(2026, 7, 31),
    )
