from services.account_governor.reporting import (
    empty_account_governor_fields,
    flatten_account_governor_audit_payload,
)


def test_empty_reporting_fields_are_explicitly_absent() -> None:
    fields = empty_account_governor_fields()
    assert fields["account_governor_context_present"] is False
    assert fields["account_governor_state"] is None


def test_reporting_flattens_neutral_contract() -> None:
    fields = flatten_account_governor_audit_payload(
        {
            "request": {
                "userid": "TCQ489",
                "as_of": "2026-07-31T10:24:00",
                "source": "TRADE_GENERATOR",
                "proposed_legs": [{"instrument_type": "EQ", "symbol": "COFORGE"}],
            },
            "assessment": {
                "userid": "TCQ489",
                "as_of": "2026-07-31T10:24:00",
                "availability": "UNAVAILABLE",
                "state": "UNKNOWN",
                "influence": "NONE",
                "new_entry_allowed": None,
                "force_flat": False,
                "reason_codes": ["ACCOUNT_GOVERNOR_NOT_IMPLEMENTED"],
                "metrics": {},
                "limits": {},
            },
        }
    )
    assert fields["account_governor_context_present"] is True
    assert fields["account_governor_availability"] == "UNAVAILABLE"
    assert fields["account_governor_state"] == "UNKNOWN"
    assert fields["account_governor_influence"] == "NONE"
    assert fields["account_governor_new_entry_allowed"] is None
    assert fields["account_governor_force_flat"] is False
    assert "ACCOUNT_GOVERNOR_NOT_IMPLEMENTED" in fields["account_governor_reason_codes"]


def test_reporting_rejects_incomplete_audit_contract() -> None:
    try:
        flatten_account_governor_audit_payload({"request": {}})
    except ValueError as exc:
        assert "assessment" in str(exc)
    else:  # pragma: no cover - explicit failure branch
        raise AssertionError("incomplete Account Governor payload was accepted")


def test_signal_trade_replay_declares_account_governor_report_contract() -> None:
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "replays"
        / "replay_signal_trade_pipeline.py"
    ).read_text(encoding="utf-8")
    for token in (
        "_account_governor.csv",
        '"account_governor_assessments"',
        '"account_governor_availability_counts"',
        '"account_governor_state_counts"',
        '"account_governor_influence_counts"',
        "ACCOUNT_GOVERNOR_AUDIT_COUNT_MISMATCH",
    ):
        assert token in source
