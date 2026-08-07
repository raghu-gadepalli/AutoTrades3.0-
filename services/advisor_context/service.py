"""Causal Market Regime context assembly for StockAdvisor."""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Protocol

from configs.stock_advisor_config import StockAdvisorPolicyConfig
from enums.advisor_context import ContextInfluence
from services.market.market_regime import MarketRegimeProviderProtocol, MarketRegimeService
from services.advisor_context.contracts import StockAdvisorContextAssessment
from utils.datetime_utils import to_ist_naive


class StockAdvisorContextProviderProtocol(Protocol):
    def assess(self, *, symbol: str, as_of: datetime) -> StockAdvisorContextAssessment:
        ...


class AdvisorContextService:
    """Assemble external context consumed by StockAdvisor."""

    def __init__(
        self,
        *,
        config: StockAdvisorPolicyConfig,
        market_regime_provider: Optional[MarketRegimeProviderProtocol] = None,
    ) -> None:
        self.config = config
        if config.market_regime_context.influence != "NONE":
            raise ValueError(
                "Market Regime Advisor context must remain NONE in this patch"
            )
        self.market_regime_provider = market_regime_provider or MarketRegimeService()

    def assess(self, *, symbol: str, as_of: datetime) -> StockAdvisorContextAssessment:
        clean_symbol = str(symbol or "").strip().upper()
        assessment_time = to_ist_naive(as_of)
        if not clean_symbol:
            raise ValueError("AdvisorContextService requires symbol")
        if assessment_time is None:
            raise ValueError("AdvisorContextService requires valid as_of datetime")

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
            market_regime=market_regime,
            market_regime_influence=ContextInfluence(regime_policy.influence),
        )


__all__ = [
    "StockAdvisorContextProviderProtocol",
    "AdvisorContextService",
]
