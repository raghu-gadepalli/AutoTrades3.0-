from __future__ import annotations

from datetime import datetime

from enums.advisor_context import (
    ContextAvailability,
    ContextInfluence,
    MarketRegimeState,
)
from services.advisor_context.contracts import (
    MarketRegimeAssessment,
    MarketRegimeHysteresis,
    StockAdvisorContextAssessment,
)
from utils.datetime_utils import to_ist_naive


class StaticAdvisorContextProvider:
    def __init__(
        self,
        *,
        market_regime: MarketRegimeAssessment | None = None,
    ) -> None:
        self.market_regime = market_regime

    def assess(self, *, symbol: str, as_of: datetime) -> StockAdvisorContextAssessment:
        clean_symbol = str(symbol).strip().upper()
        assessment_time = to_ist_naive(as_of)
        assert assessment_time is not None
        market_regime = self.market_regime or MarketRegimeAssessment(
            as_of=assessment_time,
            availability=ContextAvailability.UNAVAILABLE,
            state=MarketRegimeState.UNKNOWN,
            confidence=0.0,
            reason_codes=("TEST_MARKET_REGIME_UNAVAILABLE",),
            hysteresis=MarketRegimeHysteresis(),
        )
        return StockAdvisorContextAssessment(
            symbol=clean_symbol,
            as_of=assessment_time,
            market_regime=market_regime,
            market_regime_influence=ContextInfluence.NONE,
        )
