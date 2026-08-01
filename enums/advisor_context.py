"""Domain enums for StockAdvisor contextual intelligence.

These enums are intentionally separate from ``enums.auction_engine``. Auction
owns stock-level observation and setup authority; this module owns external
context supplied to StockAdvisor by StockRank and Market Regime services.
"""
from __future__ import annotations

from enum import Enum


class _StringEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class ContextAvailability(_StringEnum):
    AVAILABLE = "AVAILABLE"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"


class ContextInfluence(_StringEnum):
    NONE = "NONE"
    DIAGNOSTIC = "DIAGNOSTIC"
    WEIGHTED = "WEIGHTED"


class StockRankTier(_StringEnum):
    PRIORITY = "PRIORITY"
    SECONDARY = "SECONDARY"
    SUPPRESSED = "SUPPRESSED"


class MarketRegimeState(_StringEnum):
    UNKNOWN = "UNKNOWN"
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    HIGH_VOLATILITY_TREND = "HIGH_VOLATILITY_TREND"
    LOW_VOLATILITY_BALANCE = "LOW_VOLATILITY_BALANCE"
    RISK_OFF = "RISK_OFF"
    REVERSAL_PRONE = "REVERSAL_PRONE"
    MIXED_UNCERTAIN = "MIXED_UNCERTAIN"


__all__ = [
    "ContextAvailability",
    "ContextInfluence",
    "StockRankTier",
    "MarketRegimeState",
]
