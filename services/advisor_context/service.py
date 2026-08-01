"""Causal StockRank and Market Regime context assembly for StockAdvisor."""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Protocol

from configs.stock_advisor_config import StockAdvisorPolicyConfig
from enums.advisor_context import ContextAvailability, ContextInfluence, StockRankTier
from schemas.stock_rank import StockRankSchema
from services.market.market_regime import MarketRegimeProviderProtocol, MarketRegimeService
from services.advisor_context.contracts import (
    StockAdvisorContextAssessment,
    StockRankAssessment,
)
from utils.datetime_utils import to_ist_naive


class StockRankContextProviderProtocol(Protocol):
    def assess(
        self,
        *,
        symbol: str,
        as_of: datetime,
        max_age_seconds: float,
    ) -> StockRankAssessment:
        ...


class StockAdvisorContextProviderProtocol(Protocol):
    def assess(self, *, symbol: str, as_of: datetime) -> StockAdvisorContextAssessment:
        ...


class StockRankContextProvider:
    """Read the latest causal StockRank row for one symbol."""

    def assess(
        self,
        *,
        symbol: str,
        as_of: datetime,
        max_age_seconds: float,
    ) -> StockRankAssessment:
        clean_symbol = str(symbol or "").strip().upper()
        assessment_time = to_ist_naive(as_of)
        if not clean_symbol:
            raise ValueError("StockRank context requires symbol")
        if assessment_time is None:
            raise ValueError("StockRank context requires valid as_of datetime")
        row = StockRankSchema.fetch_latest_for_symbol_at_or_before(
            symbol=clean_symbol,
            through_time=assessment_time,
        )
        if row is None:
            return self.unavailable(
                symbol=clean_symbol,
                as_of=assessment_time,
                reason="STOCK_RANK_NOT_AVAILABLE",
            )

        rank_time = to_ist_naive(row.rank_time)
        if rank_time is None:
            raise ValueError("Persisted StockRank row has invalid rank_time")
        age_seconds = (assessment_time - rank_time).total_seconds()
        if age_seconds < 0.0:
            raise ValueError("Causal StockRank lookup returned a future row")
        fresh = age_seconds <= float(max_age_seconds)
        availability = (
            ContextAvailability.AVAILABLE if fresh else ContextAvailability.STALE
        )
        return StockRankAssessment(
            symbol=clean_symbol,
            as_of=assessment_time,
            availability=availability,
            rank_time=rank_time,
            age_seconds=round(age_seconds, 3),
            fresh=fresh,
            rank_position=row.rank_position,
            universe_size=row.universe_size,
            attention_tier=StockRankTier(row.attention_tier),
            direction=row.direction,
            classification=row.classification,
            total_score=row.total_score,
            movement_score=row.movement_score,
            quality_score=row.quality_score,
            range_penalty=row.range_penalty,
            stall_penalty=row.stall_penalty,
            reason_codes=(
                "STOCK_RANK_AVAILABLE" if fresh else "STOCK_RANK_STALE",
            ),
        )

    @staticmethod
    def unavailable(*, symbol: str, as_of: datetime, reason: str) -> StockRankAssessment:
        return StockRankAssessment(
            symbol=symbol,
            as_of=as_of,
            availability=ContextAvailability.UNAVAILABLE,
            rank_time=None,
            age_seconds=None,
            fresh=False,
            rank_position=None,
            universe_size=None,
            attention_tier=None,
            direction=None,
            classification=None,
            total_score=None,
            movement_score=None,
            quality_score=None,
            range_penalty=None,
            stall_penalty=None,
            reason_codes=(reason,),
        )


class AdvisorContextService:
    """Assemble all external context consumed by StockAdvisor."""

    def __init__(
        self,
        *,
        config: StockAdvisorPolicyConfig,
        stock_rank_provider: Optional[StockRankContextProviderProtocol] = None,
        market_regime_provider: Optional[MarketRegimeProviderProtocol] = None,
    ) -> None:
        self.config = config
        if config.stock_rank_context.influence != "DIAGNOSTIC":
            raise ValueError(
                "StockRank Advisor context must remain DIAGNOSTIC in this patch"
            )
        if config.market_regime_context.influence != "NONE":
            raise ValueError(
                "Market Regime Advisor context must remain NONE in this patch"
            )
        self.stock_rank_provider = stock_rank_provider or StockRankContextProvider()
        self.market_regime_provider = market_regime_provider or MarketRegimeService()

    def assess(self, *, symbol: str, as_of: datetime) -> StockAdvisorContextAssessment:
        clean_symbol = str(symbol or "").strip().upper()
        assessment_time = to_ist_naive(as_of)
        if not clean_symbol:
            raise ValueError("AdvisorContextService requires symbol")
        if assessment_time is None:
            raise ValueError("AdvisorContextService requires valid as_of datetime")

        rank_policy = self.config.stock_rank_context
        if rank_policy.enabled:
            stock_rank = self.stock_rank_provider.assess(
                symbol=clean_symbol,
                as_of=assessment_time,
                max_age_seconds=rank_policy.max_age_seconds,
            )
        else:
            stock_rank = StockRankContextProvider.unavailable(
                symbol=clean_symbol,
                as_of=assessment_time,
                reason="STOCK_RANK_CONTEXT_DISABLED",
            )

        regime_policy = self.config.market_regime_context
        if regime_policy.enabled:
            market_regime = self.market_regime_provider.assess(as_of=assessment_time)
        else:
            market_regime = MarketRegimeService().assess(as_of=assessment_time).model_copy(
                update={"reason_codes": ("MARKET_REGIME_CONTEXT_DISABLED",)}
            )

        return StockAdvisorContextAssessment(
            symbol=clean_symbol,
            as_of=assessment_time,
            stock_rank=stock_rank,
            market_regime=market_regime,
            stock_rank_influence=ContextInfluence(rank_policy.influence),
            market_regime_influence=ContextInfluence(regime_policy.influence),
        )


__all__ = [
    "StockRankContextProviderProtocol",
    "StockAdvisorContextProviderProtocol",
    "StockRankContextProvider",
    "AdvisorContextService",
]
