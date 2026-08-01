"""Flatten Account Governor audit diagnostics for replay reports."""
from __future__ import annotations

import json
from typing import Any, Dict, Mapping


_COLUMNS = (
    "account_governor_context_present",
    "account_governor_userid",
    "account_governor_as_of",
    "account_governor_availability",
    "account_governor_state",
    "account_governor_influence",
    "account_governor_new_entry_allowed",
    "account_governor_force_flat",
    "account_governor_reason_codes",
    "account_governor_metrics_json",
    "account_governor_limits_json",
    "account_governor_request_json",
    "account_governor_assessment_json",
)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def empty_account_governor_fields() -> Dict[str, Any]:
    output = {column: None for column in _COLUMNS}
    output["account_governor_context_present"] = False
    return output


def flatten_account_governor_audit_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("Account Governor audit payload must be an object")
    request = payload.get("request")
    assessment = payload.get("assessment")
    if not isinstance(request, Mapping):
        raise ValueError("Account Governor audit payload.request must be an object")
    if not isinstance(assessment, Mapping):
        raise ValueError("Account Governor audit payload.assessment must be an object")
    return {
        "account_governor_context_present": True,
        "account_governor_userid": assessment.get("userid"),
        "account_governor_as_of": assessment.get("as_of"),
        "account_governor_availability": assessment.get("availability"),
        "account_governor_state": assessment.get("state"),
        "account_governor_influence": assessment.get("influence"),
        "account_governor_new_entry_allowed": assessment.get("new_entry_allowed"),
        "account_governor_force_flat": assessment.get("force_flat"),
        "account_governor_reason_codes": _json(assessment.get("reason_codes", [])),
        "account_governor_metrics_json": _json(assessment.get("metrics", {})),
        "account_governor_limits_json": _json(assessment.get("limits", {})),
        "account_governor_request_json": _json(request),
        "account_governor_assessment_json": _json(assessment),
    }


__all__ = [
    "empty_account_governor_fields",
    "flatten_account_governor_audit_payload",
]
