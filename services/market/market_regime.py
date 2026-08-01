"""Market Regime service contract and initial neutral implementation.

The service is intentionally non-influential in this patch. It freezes the
causal assessment contract and returns explicit UNAVAILABLE/UNKNOWN context
until the market-intelligence evaluator is implemented and replay-validated.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, Protocol

from enums.advisor_context import ContextAvailability, MarketRegimeState
from services.advisor_context.contracts import (
    MarketRegimeAssessment,
    MarketRegimeHysteresis,
)
from utils.datetime_utils import to_ist_naive


class MarketRegimeProviderProtocol(Protocol):
    def assess(self, *, as_of: datetime) -> MarketRegimeAssessment:
        ...


class MarketRegimeService:
    """Current neutral provider; future implementation keeps this contract."""

    def __init__(self) -> None:
        self._cache: Dict[datetime, MarketRegimeAssessment] = {}

    def assess(self, *, as_of: datetime) -> MarketRegimeAssessment:
        assessment_time = to_ist_naive(as_of)
        if assessment_time is None:
            raise ValueError("MarketRegimeService requires a valid as_of datetime")
        cached = self._cache.get(assessment_time)
        if cached is not None:
            return cached
        assessment = MarketRegimeAssessment(
            as_of=assessment_time,
            availability=ContextAvailability.UNAVAILABLE,
            state=MarketRegimeState.UNKNOWN,
            confidence=0.0,
            age_seconds=None,
            buy_support=0.0,
            sell_support=0.0,
            continuation_support=0.0,
            reversal_support=0.0,
            evidence=(),
            reason_codes=("MARKET_REGIME_NOT_IMPLEMENTED",),
            hysteresis=MarketRegimeHysteresis(),
            metrics={},
        )
        self._cache[assessment_time] = assessment
        return assessment


__all__ = ["MarketRegimeProviderProtocol", "MarketRegimeService"]
