from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from services.trade.generator import tradegen_helper as module
from services.trade.generator.tradegen_helper import TradeLegPlan, TradePlan


def _plan() -> TradePlan:
    return TradePlan(
        user=SimpleNamespace(userid="TCQ489"),
        signal=SimpleNamespace(signal_id="SIG-1", quantity=0),
        snapshot=SimpleNamespace(),
        side="SELL",
        equity_ref="COFORGE",
        trade_time=datetime(2026, 7, 31, 10, 24),
        execution_mode="VIRTUAL",
        product_type="MIS",
        intraday_only=True,
        position_style="NAKED",
        legs=[
            TradeLegPlan(
                instrument_type="EQ",
                trade_symbol="COFORGE",
                lotsize=1,
                entry_price_exec=Decimal("1500"),
                risk_ref_price_eq=Decimal("1500"),
            ),
            TradeLegPlan(
                instrument_type="PE",
                trade_symbol="COFORGE26JUL1500PE",
                lotsize=475,
                entry_price_exec=Decimal("38"),
                risk_ref_price_eq=Decimal("1500"),
            ),
        ],
        source="TRADE_GENERATOR",
        message="AUTO",
    )


def test_plan_is_converted_to_resolved_governor_request() -> None:
    request = module._account_governor_request_from_plan(
        _plan(), authorization_time=datetime(2026, 7, 31, 10, 27)
    )
    assert request.userid == "TCQ489"
    assert request.as_of == datetime(2026, 7, 31, 10, 27)
    assert request.as_of != _plan().trade_time
    assert request.signal_id == "SIG-1"
    assert request.equity_ref == "COFORGE"
    assert len(request.proposed_legs) == 2
    assert request.proposed_legs[0].side == "SELL"
    assert request.proposed_legs[0].quantity is None
    assert request.proposed_legs[1].side == "BUY"
    assert request.proposed_legs[1].quantity == 475


def test_diagnostic_assessment_is_logged_without_authorising(monkeypatch) -> None:
    captured = {}

    def fake_write_auditlog(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(module, "write_auditlog", fake_write_auditlog)
    result = module._assess_and_audit_account_governor(
        request=module._account_governor_request_from_plan(
            _plan(), authorization_time=datetime(2026, 7, 31, 10, 27)
        )
    )

    assessment = result["assessment"]
    assert assessment["availability"] == "UNAVAILABLE"
    assert assessment["state"] == "UNKNOWN"
    assert assessment["influence"] == "NONE"
    assert assessment["new_entry_allowed"] is None
    assert result["audit_persisted"] is True
    assert captured["entity_type"] == "ACCOUNT_GOVERNOR"
    assert captured["evaluation_stage"] == "PACKAGE_AUTHORIZATION"
    assert captured["action"] == "ASSESS_PACKAGE"
    assert captured["force_persist"] is True


def test_manual_payload_is_converted_after_existing_validation() -> None:
    request = module._account_governor_request_from_manual_payload(
        payload={
            "userid": "TCQ489",
            "entry_time": datetime(2026, 7, 31, 11, 0),
            "source": "MANUAL",
            "signal_id": "MANUAL:123",
            "equity_ref": "TCS",
            "trade_type": "BUY",
            "execution_mode": "VIRTUAL",
            "product_type": "MIS",
            "position_style": "NAKED",
            "intraday_only": True,
            "instrument_type": "FUT",
            "symbol": "TCS26JULFUT",
            "entry_price": Decimal("3030"),
            "quantity": 175,
            "lotsize": 175,
        },
        authorization_time=datetime(2026, 7, 31, 11, 3),
    )
    assert request.source == "MANUAL"
    assert request.as_of == datetime(2026, 7, 31, 11, 3)
    assert request.signal_id == "MANUAL:123"
    assert request.proposed_legs[0].instrument_type == "FUT"
    assert request.proposed_legs[0].quantity == 175
