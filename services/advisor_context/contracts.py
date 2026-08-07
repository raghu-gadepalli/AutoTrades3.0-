"""Strict immutable contracts for context supplied to StockAdvisor."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from enums.advisor_context import (
    ContextAvailability,
    ContextInfluence,
    MarketRegimeState,
)
from utils.datetime_utils import to_ist_naive


class _ContextContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_assignment=True,
        validate_default=True,
        allow_inf_nan=False,
    )



class MarketRegimeEvidence(_ContextContract):
    code: str = Field(min_length=1)
    source: str = Field(min_length=1)
    observed_at: datetime
    value: Any = None
    details: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("code", mode="before")
    @classmethod
    def _normalise_code(cls, value: object) -> str:
        code = str(value or "").strip().upper()
        if not code:
            raise ValueError("MarketRegimeEvidence code is required")
        return code

    @field_validator("source", mode="before")
    @classmethod
    def _normalise_source(cls, value: object) -> str:
        source = str(value or "").strip().upper()
        if not source:
            raise ValueError("MarketRegimeEvidence source is required")
        return source


class MarketRegimeHysteresis(_ContextContract):
    state_started_at: Optional[datetime] = None
    candidate_state: Optional[MarketRegimeState] = None
    candidate_since: Optional[datetime] = None
    candidate_confirmations: int = Field(default=0, ge=0)
    required_confirmations: int = Field(default=0, ge=0)
    transition_pending: bool = False

    @model_validator(mode="after")
    def _validate_hysteresis(self) -> "MarketRegimeHysteresis":
        candidate_fields = (
            self.candidate_state,
            self.candidate_since,
        )
        if self.transition_pending and any(item is None for item in candidate_fields):
            raise ValueError("Pending regime transition requires candidate state and time")
        if not self.transition_pending and any(item is not None for item in candidate_fields):
            raise ValueError("Non-pending regime transition cannot contain candidate state")
        if self.candidate_confirmations > self.required_confirmations and self.required_confirmations > 0:
            raise ValueError("Regime confirmations cannot exceed required confirmations")
        return self


class MarketRegimeAssessment(_ContextContract):
    as_of: datetime
    availability: ContextAvailability
    state: MarketRegimeState
    confidence: float = Field(ge=0.0, le=1.0)
    age_seconds: Optional[float] = Field(default=None, ge=0.0)

    buy_support: float = Field(default=0.0, ge=0.0, le=1.0)
    sell_support: float = Field(default=0.0, ge=0.0, le=1.0)
    continuation_support: float = Field(default=0.0, ge=0.0, le=1.0)
    reversal_support: float = Field(default=0.0, ge=0.0, le=1.0)

    evidence: Tuple[MarketRegimeEvidence, ...] = ()
    reason_codes: Tuple[str, ...] = ()
    hysteresis: MarketRegimeHysteresis = Field(default_factory=MarketRegimeHysteresis)
    metrics: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("reason_codes", mode="before")
    @classmethod
    def _normalise_reasons(cls, value: object) -> Tuple[str, ...]:
        return tuple(str(item).strip().upper() for item in (value or ()) if str(item).strip())

    @model_validator(mode="after")
    def _validate_assessment(self) -> "MarketRegimeAssessment":
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("MarketRegimeAssessment reason codes must be unique")
        as_of = to_ist_naive(self.as_of)
        if as_of is None:
            raise ValueError("MarketRegimeAssessment as_of must be valid")
        for fact in self.evidence:
            observed_at = to_ist_naive(fact.observed_at)
            if observed_at is None or observed_at > as_of:
                raise ValueError("MarketRegime evidence must be causal")

        if self.availability is ContextAvailability.UNAVAILABLE:
            if self.state is not MarketRegimeState.UNKNOWN:
                raise ValueError("Unavailable market regime must be UNKNOWN")
            if self.confidence != 0.0 or self.age_seconds is not None:
                raise ValueError("Unavailable market regime cannot claim confidence or age")
            if any(
                value != 0.0
                for value in (
                    self.buy_support,
                    self.sell_support,
                    self.continuation_support,
                    self.reversal_support,
                )
            ):
                raise ValueError("Unavailable market regime cannot claim directional support")
            if self.evidence:
                raise ValueError("Unavailable market regime cannot contain evaluated evidence")
            return self

        if self.state is MarketRegimeState.UNKNOWN:
            raise ValueError("Available or stale market regime cannot be UNKNOWN")
        if self.age_seconds is None or self.hysteresis.state_started_at is None:
            raise ValueError("Available or stale market regime requires state age and start time")
        started = to_ist_naive(self.hysteresis.state_started_at)
        if started is None or started > as_of:
            raise ValueError("Market regime state start must be causal")
        return self


class StockAdvisorContextAssessment(_ContextContract):
    symbol: str = Field(min_length=1, max_length=32)
    as_of: datetime
    market_regime: MarketRegimeAssessment
    market_regime_influence: ContextInfluence

    @field_validator("symbol", mode="before")
    @classmethod
    def _normalise_symbol(cls, value: object) -> str:
        symbol = str(value or "").strip().upper()
        if not symbol:
            raise ValueError("StockAdvisor context symbol is required")
        return symbol

    @model_validator(mode="after")
    def _validate_context(self) -> "StockAdvisorContextAssessment":
        as_of = to_ist_naive(self.as_of)
        regime_as_of = to_ist_naive(self.market_regime.as_of)
        if as_of is None or regime_as_of != as_of:
            raise ValueError("StockAdvisor context timestamps must match")
        return self

    def to_diagnostics(self) -> Dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=False)


__all__ = [
    "MarketRegimeEvidence",
    "MarketRegimeHysteresis",
    "MarketRegimeAssessment",
    "StockAdvisorContextAssessment",
]
