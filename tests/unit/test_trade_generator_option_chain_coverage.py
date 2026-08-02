from types import SimpleNamespace

import services.trade.generator.tradegen_helper as tradegen_helper
from services.trade.generator.tradegen_helper import (
    TradeGenHelper,
    _option_chain_coverage_check,
)


def _snapshot(*, spot: float, call_strikes: list[float], put_strikes: list[float]):
    return {
        "snapshot_time": "2026-07-31T10:33:00+05:30",
        "derivatives": {
            "spot_price": spot,
            "option_ladder": {
                "atm_strike": call_strikes[-1] if call_strikes else None,
                "calls": [
                    {"strike": strike, "symbol": f"TEST{int(strike)}CE"}
                    for strike in call_strikes
                ],
                "puts": [
                    {"strike": strike, "symbol": f"TEST{int(strike)}PE"}
                    for strike in put_strikes
                ],
            },
        },
    }


def test_option_chain_coverage_blocks_boundary_clamped_atm() -> None:
    result = _option_chain_coverage_check(
        _snapshot(
            spot=622.75,
            call_strikes=[390, 400, 410, 420, 430, 440],
            put_strikes=[390, 400, 410, 420, 430, 440],
        ),
        option_type="CE",
    )

    assert result["ok"] is False
    assert result["error"] == "OPTION_CHAIN_SPOT_OUTSIDE_COVERAGE"
    assert result["details"]["spot"] == 622.75
    assert result["details"]["min_strike"] == 390.0
    assert result["details"]["max_strike"] == 440.0


def test_option_chain_coverage_allows_refreshed_chain() -> None:
    result = _option_chain_coverage_check(
        _snapshot(
            spot=623.0,
            call_strikes=[580, 600, 620, 625, 630, 650, 690],
            put_strikes=[580, 600, 620, 625, 630, 650, 690],
        ),
        option_type="CE",
    )

    assert result["ok"] is True
    assert result["details"]["min_strike"] == 580.0
    assert result["details"]["max_strike"] == 690.0


def test_build_signal_plan_blocks_before_instrument_selection(monkeypatch) -> None:
    snapshot_dict = _snapshot(
        spot=622.75,
        call_strikes=[390, 400, 410, 420, 430, 440],
        put_strikes=[390, 400, 410, 420, 430, 440],
    )
    user = SimpleNamespace(
        userid="USER1",
        equity=1,
        futures=1,
        options=1,
    )
    signal = SimpleNamespace(
        signal_id="SIG1",
        symbol="KALYANKJIL",
        equity_ref="KALYANKJIL",
        side="BUY",
        setup="NORMAL_REVERSAL",
        status="OPEN",
        last_snapshot=snapshot_dict,
    )
    snapshot = SimpleNamespace(close=622.75)

    monkeypatch.setattr(tradegen_helper.UserSchema, "fetch_user", lambda userid: user)
    monkeypatch.setattr(
        TradeGenHelper,
        "_resolve_signal",
        staticmethod(lambda signal_id: signal),
    )
    monkeypatch.setattr(
        tradegen_helper,
        "_fetch_snapshot_for_signal",
        lambda current_signal: snapshot,
    )

    result = TradeGenHelper.build_signal_plan(
        userid="USER1",
        signal_id="SIG1",
        instrument_choice="MULTI",
    )

    assert result["ok"] is False
    assert result["error"] == "OPTION_CHAIN_SPOT_OUTSIDE_COVERAGE"
    assert result["details"]["symbol"] == "KALYANKJIL"
    assert result["details"]["instrument_choice"] == "MULTI"
