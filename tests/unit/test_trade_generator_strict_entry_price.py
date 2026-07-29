from types import SimpleNamespace

import services.trade.generator.tradegen_validator as validator


def _signal(*, side: str, created_price: float):
    return SimpleNamespace(side=side, created_price=created_price)


def test_buy_equality_defers(monkeypatch):
    monkeypatch.setattr(
        validator,
        "_signal_entry_prices",
        lambda signal: {
            "side": "BUY",
            "created_price": 100.0,
            "current_price": 100.0,
            "directional_move_pct": 0.0,
            "price_source": "SNAPSHOT_LTP",
            "price_snapshot_time": None,
        },
    )

    decision = validator._price_entry_decision(
        _signal(side="BUY", created_price=100.0),
        mode=validator.MODE_AUTO,
        warnings=[],
    )

    assert decision is not None
    assert decision.decision == validator.TRADE_DECISION_WAIT
    assert decision.allowed is False
    assert decision.details["at_breakeven"] is True
    assert decision.details["strictly_favorable"] is False
    assert decision.details["required_relation"] == "current_price > created_price"


def test_sell_equality_defers(monkeypatch):
    monkeypatch.setattr(
        validator,
        "_signal_entry_prices",
        lambda signal: {
            "side": "SELL",
            "created_price": 100.0,
            "current_price": 100.0,
            "directional_move_pct": 0.0,
            "price_source": "SNAPSHOT_CLOSE",
            "price_snapshot_time": None,
        },
    )

    decision = validator._price_entry_decision(
        _signal(side="SELL", created_price=100.0),
        mode=validator.MODE_AUTO,
        warnings=[],
    )

    assert decision is not None
    assert decision.decision == validator.TRADE_DECISION_WAIT
    assert decision.allowed is False
    assert decision.details["at_breakeven"] is True
    assert decision.details["strictly_favorable"] is False
    assert decision.details["required_relation"] == "current_price < created_price"


def test_buy_requires_strictly_higher_snapshot_price(monkeypatch):
    monkeypatch.setattr(
        validator,
        "_signal_entry_prices",
        lambda signal: {
            "side": "BUY",
            "created_price": 100.0,
            "current_price": 100.05,
            "directional_move_pct": 0.05,
            "price_source": "SNAPSHOT_LTP",
            "price_snapshot_time": None,
        },
    )

    assert validator._price_entry_decision(
        _signal(side="BUY", created_price=100.0),
        mode=validator.MODE_AUTO,
        warnings=[],
    ) is None


def test_sell_requires_strictly_lower_snapshot_price(monkeypatch):
    monkeypatch.setattr(
        validator,
        "_signal_entry_prices",
        lambda signal: {
            "side": "SELL",
            "created_price": 100.0,
            "current_price": 99.95,
            "directional_move_pct": 0.05,
            "price_source": "SNAPSHOT_LTP",
            "price_snapshot_time": None,
        },
    )

    assert validator._price_entry_decision(
        _signal(side="SELL", created_price=100.0),
        mode=validator.MODE_AUTO,
        warnings=[],
    ) is None
