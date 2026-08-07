from __future__ import annotations

import json

import pytest

from services.advisor_context.reporting import flatten_advisor_context_for_csv


def _diagnostics() -> dict:
    return {
        "deployment_scope": "NEW_SIGNAL_ONLY",
        "advisor_context": {
            "symbol": "TCS",
            "as_of": "2026-07-31T11:24:00",
            "market_regime_influence": "NONE",
            "market_regime": {
                "as_of": "2026-07-31T11:24:00",
                "availability": "UNAVAILABLE",
                "state": "UNKNOWN",
                "confidence": 0.0,
                "age_seconds": None,
                "buy_support": 0.0,
                "sell_support": 0.0,
                "continuation_support": 0.0,
                "reversal_support": 0.0,
                "evidence": [],
                "reason_codes": ["MARKET_REGIME_NOT_IMPLEMENTED"],
                "hysteresis": {
                    "state_started_at": None,
                    "candidate_state": None,
                    "candidate_since": None,
                    "candidate_confirmations": 0,
                    "required_confirmations": 0,
                    "transition_pending": False,
                },
                "metrics": {},
            },
        },
    }


@pytest.mark.parametrize("advisor_action", ["ALLOW", "BLOCK", "WATCH"])
def test_flattens_context_for_every_advisor_action(advisor_action: str) -> None:
    diagnostics = _diagnostics()

    row = flatten_advisor_context_for_csv(
        advisor_action=advisor_action,
        advisor_diagnostics=diagnostics,
    )

    assert row["advisor_context_present"] is True
    assert row["advisor_context_symbol"] == "TCS"
    assert row["advisor_context_as_of"] == "2026-07-31T11:24:00"
    assert row["market_regime_influence"] == "NONE"
    assert row["market_regime_availability"] == "UNAVAILABLE"
    assert row["market_regime_state"] == "UNKNOWN"
    assert row["market_regime_confidence"] == 0.0
    assert json.loads(row["market_regime_reason_codes"]) == [
        "MARKET_REGIME_NOT_IMPLEMENTED"
    ]
    assert json.loads(row["market_regime_hysteresis"])[
        "transition_pending"
    ] is False
    assert json.loads(row["advisor_diagnostics_json"])[
        "deployment_scope"
    ] == "NEW_SIGNAL_ONLY"


def test_non_advisor_evaluation_has_blank_context_columns() -> None:
    row = flatten_advisor_context_for_csv(
        advisor_action=None,
        advisor_diagnostics=None,
    )

    assert row["advisor_context_present"] is False
    assert row["market_regime_availability"] is None
    assert row["market_regime_state"] is None
    assert row["advisor_diagnostics_json"] is None


def test_advisor_evaluation_requires_diagnostics() -> None:
    with pytest.raises(ValueError, match="advisor_diagnostics"):
        flatten_advisor_context_for_csv(
            advisor_action="ALLOW",
            advisor_diagnostics=None,
        )


def test_advisor_evaluation_requires_context_contract() -> None:
    with pytest.raises(ValueError, match="advisor_diagnostics.advisor_context"):
        flatten_advisor_context_for_csv(
            advisor_action="BLOCK",
            advisor_diagnostics={"deployment_scope": "NEW_SIGNAL_ONLY"},
        )


def test_advisor_evaluation_requires_market_regime_context() -> None:
    diagnostics = _diagnostics()
    diagnostics["advisor_context"].pop("market_regime")

    with pytest.raises(ValueError, match="advisor_context.market_regime"):
        flatten_advisor_context_for_csv(
            advisor_action="WATCH",
            advisor_diagnostics=diagnostics,
        )
