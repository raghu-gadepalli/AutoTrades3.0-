"""Authoritative domain enums for the AutoTrades Auction Engine.

The enums in this module are shared by strict contracts and typed configuration.
They contain no lifecycle implementation and no compatibility aliases.
"""

from __future__ import annotations

from enum import Enum


class StringEnum(str, Enum):
    """Python 3.10-compatible string enum."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class TradeSide(StringEnum):
    BUY = "BUY"
    SELL = "SELL"
    ANY = "ANY"
    NONE = "NONE"

    @property
    def opposite(self) -> "TradeSide":
        if self is TradeSide.BUY:
            return TradeSide.SELL
        if self is TradeSide.SELL:
            return TradeSide.BUY
        return self


class BoundarySide(StringEnum):
    UPPER = "UPPER"
    LOWER = "LOWER"
    NONE = "NONE"


class FreshDirection(StringEnum):
    UP = "UP"
    DOWN = "DOWN"
    UNRESOLVED = "UNRESOLVED"
    UNAVAILABLE = "UNAVAILABLE"


class DirectionalTransition(StringEnum):
    NONE = "NONE"
    STARTED = "STARTED"
    CONTINUED = "CONTINUED"
    REVERSED = "REVERSED"
    ENDED = "ENDED"
    DEFERRED = "DEFERRED"


class DirectionalBias(StringEnum):
    UP = "UP"
    DOWN = "DOWN"
    NEUTRAL = "NEUTRAL"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


class QualityStatus(StringEnum):
    GOOD = "GOOD"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    MISSING = "MISSING"
    INVALID = "INVALID"
    UNKNOWN = "UNKNOWN"


class EvidencePolarity(StringEnum):
    SUPPORT = "SUPPORT"
    CONTRADICT = "CONTRADICT"
    NEUTRAL = "NEUTRAL"
    UNKNOWN = "UNKNOWN"


class AuctionStateName(StringEnum):
    """Current-snapshot Auction observation classification.

    These values describe current evidence only. Directional and balance
    continuity is published separately by the current Auction engine.
    """

    UNKNOWN = "UNKNOWN"
    BALANCE = "BALANCE"
    COMPRESSION = "COMPRESSION"
    BOUNDARY_INTERACTION = "BOUNDARY_INTERACTION"
    FRESH_EXPANSION = "FRESH_EXPANSION"
    ORDERLY_UPTREND = "ORDERLY_UPTREND"
    ORDERLY_DOWNTREND = "ORDERLY_DOWNTREND"
    CONTROLLED_PULLBACK = "CONTROLLED_PULLBACK"
    RECOMPRESSION = "RECOMPRESSION"
    REACCELERATION = "REACCELERATION"
    MATURE_EXTENSION = "MATURE_EXTENSION"
    TREND_FAILURE = "TREND_FAILURE"
    REVERSAL = "REVERSAL"
    CHAOTIC_ROTATION = "CHAOTIC_ROTATION"


class AdvisorAction(StringEnum):
    NO_ACTION = "NO_ACTION"
    ALLOW = "ALLOW"
    WATCH = "WATCH"
    BLOCK = "BLOCK"






class SetupFamily(StringEnum):
    BREAKOUT_INITIATION = "BREAKOUT_INITIATION"
    ACCEPTED_BREAKOUT = "ACCEPTED_BREAKOUT"
    FAILED_BREAKOUT = "FAILED_BREAKOUT"
    CONTINUATION = "CONTINUATION"
    REACCELERATION = "REACCELERATION"
    REVERSAL = "REVERSAL"


class ContextAlignment(StringEnum):
    SUPPORT = "SUPPORT"
    CONFLICT = "CONFLICT"
    NEUTRAL = "NEUTRAL"
    UNKNOWN = "UNKNOWN"


class BalanceEpisodeState(StringEnum):
    NONE = "NONE"
    FORMING = "FORMING"
    PROBABLE = "PROBABLE"
    LOCKED = "LOCKED"
    ESCAPE_WATCH = "ESCAPE_WATCH"
    ACCEPTED_OUTSIDE = "ACCEPTED_OUTSIDE"
    FAILED_BACK_INSIDE = "FAILED_BACK_INSIDE"
    COMPLETED = "COMPLETED"


class AuctionEventType(StringEnum):
    DIRECTIONAL_STARTED = "DIRECTIONAL_STARTED"
    DIRECTIONAL_REVERSED = "DIRECTIONAL_REVERSED"
    DIRECTIONAL_ENDED = "DIRECTIONAL_ENDED"
    BALANCE_FORMING_STARTED = "BALANCE_FORMING_STARTED"
    BALANCE_PROBABLE = "BALANCE_PROBABLE"
    BALANCE_LOCKED = "BALANCE_LOCKED"
    BALANCE_ESCAPE_STARTED = "BALANCE_ESCAPE_STARTED"
    BALANCE_ESCAPE_ACCEPTED = "BALANCE_ESCAPE_ACCEPTED"
    BALANCE_ESCAPE_FAILED = "BALANCE_ESCAPE_FAILED"
    BALANCE_REARMED = "BALANCE_REARMED"
    BALANCE_ATTEMPT_LIMIT_REACHED = "BALANCE_ATTEMPT_LIMIT_REACHED"
    BALANCE_COMPLETED = "BALANCE_COMPLETED"


class StructuralPermissionResult(StringEnum):
    PERMIT = "PERMIT"
    WAIT = "WAIT"
    BLOCK = "BLOCK"


class SetupEventAction(StringEnum):
    WATCH = "WATCH"
    EVALUATE = "EVALUATE"
    INVALIDATE = "INVALIDATE"
    CLOSE = "CLOSE"


__all__ = [
    "StringEnum",
    "TradeSide",
    "BoundarySide",
    "FreshDirection",
    "DirectionalTransition",
    "DirectionalBias",
    "QualityStatus",
    "EvidencePolarity",
    "AuctionStateName",
    "AdvisorAction",
    "SetupFamily",
    "ContextAlignment",
    "BalanceEpisodeState",
    "AuctionEventType",
    "StructuralPermissionResult",
    "SetupEventAction",
]
