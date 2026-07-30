from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from services.trade.executor import trade_executor as executor


def _payload():
    return {
        "derivatives": {
            "future": {
                "instrument": "DABUR26AUGFUT",
                "last_price": 434.80,
            },
            "option_ladder": {
                "calls": [
                    {"symbol": "DABUR26AUG430CE", "ltp": 16.10},
                    {"symbol": "DABUR26AUG440CE", "ltp": 11.30},
                ],
                "puts": [
                    {"symbol": "DABUR26AUG430PE", "ltp": 11.30},
                ],
            },
            "options_lite": {
                "top_calls": [],
                "top_puts": [],
            },
        }
    }


def test_exact_derivative_price_resolves_future_and_option() -> None:
    payload = _payload()
    assert executor._derivative_price_from_snapshot_payload(
        payload,
        trade_symbol="DABUR26AUGFUT",
        instrument_type="FUT",
    ) == Decimal("434.8")
    assert executor._derivative_price_from_snapshot_payload(
        payload,
        trade_symbol="DABUR26AUG430CE",
        instrument_type="CE",
    ) == Decimal("16.1")


def test_exact_derivative_price_never_substitutes_another_strike() -> None:
    assert executor._derivative_price_from_snapshot_payload(
        _payload(),
        trade_symbol="DABUR26AUG450CE",
        instrument_type="CE",
    ) is None


def test_virtual_derivative_price_uses_underlying_snapshot(monkeypatch) -> None:
    snapshot_time = datetime(2026, 7, 29, 12, 15)
    monkeypatch.setattr(executor, "_execution_use_snapshot_for_virtual", lambda: True)
    monkeypatch.setattr(executor, "_execution_use_live_price_for_virtual", lambda: False)
    monkeypatch.setattr(
        executor.SnapshotSchema,
        "fetch_latest_for_symbol_asof",
        lambda symbol, asof: None,
    )
    monkeypatch.setattr(
        executor,
        "_latest_snapshot_record",
        lambda symbol, asof_time: SimpleNamespace(
            snapshot_time=snapshot_time,
            data=_payload(),
        ),
    )

    price, price_time = executor._virtual_fill_price_time(
        "DABUR26AUG430CE",
        "SELL",
        None,
        asof_time=snapshot_time,
        equity_ref="DABUR",
        instrument_type="CE",
        last_known_price=15.95,
    )

    assert price == Decimal("16.1")
    assert price_time == snapshot_time


def test_exit_fill_cannot_be_terminal_without_positive_price() -> None:
    trade = SimpleNamespace(
        trade_type="BUY",
        executed_entry_price=Decimal("15.75"),
        entry_price=Decimal("15.75"),
        executed_entry_qty=1250,
        quantity=1250,
        exit_time=None,
    )
    with pytest.raises(ValueError, match="Exit fill price must be positive"):
        executor._build_exit_fill_updates(
            trade,
            exit_px=0,
            qty=1250,
            when=datetime(2026, 7, 29, 12, 15),
        )
