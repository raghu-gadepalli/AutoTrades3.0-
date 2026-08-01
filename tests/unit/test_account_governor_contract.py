from datetime import datetime

import pytest

from enums.account_governor import (
    AccountGovernorAvailability,
    AccountGovernorInfluence,
    AccountGovernorState,
)
from services.account_governor.contracts import (
    AccountGovernorAssessment,
    AccountGovernorPackageLeg,
    AccountGovernorRequest,
)
from services.account_governor.service import AccountGovernorService


def _request() -> AccountGovernorRequest:
    return AccountGovernorRequest(
        userid="tcq489",
        as_of=datetime(2026, 7, 31, 10, 24),
        source="trade_generator",
        signal_id="SIG-1",
        equity_ref="coforge",
        side="sell",
        execution_mode="virtual",
        product_type="mis",
        position_style="naked",
        intraday_only=True,
        proposed_legs=(
            AccountGovernorPackageLeg(
                instrument_type="eq",
                symbol="coforge",
                side="sell",
                entry_price=1500.0,
                quantity=1,
                lotsize=1,
            ),
        ),
    )


def test_neutral_service_returns_explicit_unavailable_unknown_none() -> None:
    request = _request()
    assessment = AccountGovernorService().assess_package(request=request)

    assert assessment.userid == "TCQ489"
    assert assessment.as_of == request.as_of
    assert assessment.availability is AccountGovernorAvailability.UNAVAILABLE
    assert assessment.state is AccountGovernorState.UNKNOWN
    assert assessment.influence is AccountGovernorInfluence.NONE
    assert assessment.new_entry_allowed is None
    assert assessment.force_flat is False
    assert assessment.reason_codes == ("ACCOUNT_GOVERNOR_NOT_IMPLEMENTED",)
    assert assessment.metrics == {}
    assert assessment.limits == {}


def test_request_normalises_identity_and_requires_unique_legs() -> None:
    request = _request()
    assert request.userid == "TCQ489"
    assert request.source == "TRADE_GENERATOR"
    assert request.equity_ref == "COFORGE"
    assert request.side == "SELL"
    assert request.proposed_legs[0].instrument_type == "EQ"

    leg = request.proposed_legs[0]
    with pytest.raises(ValueError, match="must be unique"):
        AccountGovernorRequest(
            **request.model_dump(exclude={"proposed_legs"}),
            proposed_legs=(leg, leg),
        )


def test_unavailable_assessment_cannot_claim_permission() -> None:
    with pytest.raises(ValueError, match="cannot claim authority"):
        AccountGovernorAssessment(
            userid="TCQ489",
            as_of=datetime(2026, 7, 31, 10, 24),
            availability=AccountGovernorAvailability.UNAVAILABLE,
            state=AccountGovernorState.UNKNOWN,
            influence=AccountGovernorInfluence.NONE,
            new_entry_allowed=True,
            force_flat=False,
            reason_codes=("NOT_IMPLEMENTED",),
        )


def test_available_states_have_strict_permission_semantics() -> None:
    allow = AccountGovernorAssessment(
        userid="TCQ489",
        as_of=datetime(2026, 7, 31, 10, 24),
        availability=AccountGovernorAvailability.AVAILABLE,
        state=AccountGovernorState.ALLOW,
        influence=AccountGovernorInfluence.DIAGNOSTIC,
        new_entry_allowed=True,
        force_flat=False,
        reason_codes=("WITHIN_LIMITS",),
        metrics={"open_packages": 1},
        limits={"max_open_packages": 3},
    )
    assert allow.new_entry_allowed is True

    with pytest.raises(ValueError, match="FORCE_FLAT"):
        AccountGovernorAssessment(
            userid="TCQ489",
            as_of=datetime(2026, 7, 31, 10, 24),
            availability=AccountGovernorAvailability.AVAILABLE,
            state=AccountGovernorState.FORCE_FLAT,
            influence=AccountGovernorInfluence.ENFORCED,
            new_entry_allowed=False,
            force_flat=False,
            reason_codes=("KILL_SWITCH",),
        )
