"""Strict immutable contracts for context supplied to StockAdvisor."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from enums.advisor_context import (
    ContextAvailability,
    ContextInfluence,
    MarketRegimeState,
    StockRankTier,
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


class StockRankAssessment(_ContextContract):
    symbol: str = Field(min_length=1, max_length=32)
    as_of: datetime
    availability: ContextAvailability
    rank_time: Optional[datetime] = None
    age_seconds: Optional[float] = Field(default=None, ge=0.0)
    fresh: bool = False

    rank_position: Optional[int] = Field(default=None, ge=1)
    universe_size: Optional[int] = Field(default=None, ge=1)
    attention_tier: Optional[StockRankTier] = None
    direction: Optional[str] = None
    classification: Optional[str] = None

    total_score: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    movement_score: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    quality_score: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    range_penalty: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    stall_penalty: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    reason_codes: Tuple[str, ...] = ()

    @field_validator("symbol", mode="before")
    @classmethod
    def _normalise_symbol(cls, value: object) -> str:
        symbol = str(value or "").strip().upper()
        if not symbol:
            raise ValueError("StockRankAssessment symbol is required")
        return symbol

    @field_validator("direction", "classification", mode="before")
    @classmethod
    def _normalise_optional_text(cls, value: object) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip().upper()
        return text or None

    @field_validator("reason_codes", mode="before")
    @classmethod
    def _normalise_reasons(cls, value: object) -> Tuple[str, ...]:
        return tuple(str(item).strip().upper() for item in (value or ()) if str(item).strip())

    @model_validator(mode="after")
    def _validate_assessment(self) -> "StockRankAssessment":
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("StockRankAssessment reason codes must be unique")

        populated = (
            self.rank_time,
            self.age_seconds,
            self.rank_position,
            self.universe_size,
            self.attention_tier,
            self.direction,
            self.classification,
            self.total_score,
            self.movement_score,
            self.quality_score,
            self.range_penalty,
            self.stall_penalty,
        )
        if self.availability is ContextAvailability.UNAVAILABLE:
            if self.fresh or any(item is not None for item in populated):
                raise ValueError("Unavailable StockRank assessment cannot contain rank data")
            return self

        if any(item is None for item in populated):
            raise ValueError("Available or stale StockRank assessment requires complete rank data")
        assert self.rank_time is not None
        assert self.rank_position is not None
        assert self.universe_size is not None
        if self.rank_position > self.universe_size:
            raise ValueError("StockRankAssessment rank_position exceeds universe_size")
        rank_time = to_ist_naive(self.rank_time)
        as_of = to_ist_naive(self.as_of)
        if rank_time is None or as_of is None:
            raise ValueError("StockRankAssessment timestamps must be valid datetimes")
        if rank_time > as_of:
            raise ValueError("StockRankAssessment rank_time cannot be in the future")
        if self.availability is ContextAvailability.AVAILABLE and not self.fresh:
            raise ValueError("Available StockRank assessment must be fresh")
        if self.availability is ContextAvailability.STALE and self.fresh:
            raise ValueError("Stale StockRank assessment cannot be fresh")
        return self


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
    stock_rank: StockRankAssessment
    market_regime: MarketRegimeAssessment
    stock_rank_influence: ContextInfluence
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
        if self.stock_rank.symbol != self.symbol:
            raise ValueError("StockAdvisor context/StockRank symbol mismatch")
        as_of = to_ist_naive(self.as_of)
        rank_as_of = to_ist_naive(self.stock_rank.as_of)
        regime_as_of = to_ist_naive(self.market_regime.as_of)
        if as_of is None or rank_as_of != as_of or regime_as_of != as_of:
            raise ValueError("StockAdvisor context timestamps must match")
        return self

    def to_diagnostics(self) -> Dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=False)


__all__ = [
    "StockRankAssessment",
    "MarketRegimeEvidence",
    "MarketRegimeHysteresis",
    "MarketRegimeAssessment",
    "StockAdvisorContextAssessment",
]
