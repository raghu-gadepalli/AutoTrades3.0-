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

    These values describe current evidence only. Persistent directional and
    balance lifecycle is owned by the Persistent Episode Engine.
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


class BoundaryEpisodeStatus(StringEnum):
    APPROACHING = "APPROACHING"
    OUTSIDE_ATTEMPT = "OUTSIDE_ATTEMPT"
    UNRESOLVED = "UNRESOLVED"
    ACCEPTANCE_BUILDING = "ACCEPTANCE_BUILDING"
    ACCEPTED = "ACCEPTED"
    FAILURE_BUILDING = "FAILURE_BUILDING"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"
    STALE = "STALE"


class BoundaryResolution(StringEnum):
    UNRESOLVED = "UNRESOLVED"
    ACCEPTED = "ACCEPTED"
    FAILED = "FAILED"


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


class DirectionalEpisodeState(StringEnum):
    NONE = "NONE"
    DIRECTIONAL = "DIRECTIONAL"
    MATURE = "MATURE"
    REVERSAL_WATCH = "REVERSAL_WATCH"
    REVERSAL_LEG = "REVERSAL_LEG"
    COMPLETED = "COMPLETED"


class DirectionalEpisodeOrigin(StringEnum):
    NONE = "NONE"
    OBSERVATION_CONFIRMATION = "OBSERVATION_CONFIRMATION"
    REVERSAL_EVENT_HANDOFF = "REVERSAL_EVENT_HANDOFF"


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
    DIRECTIONAL_MATURED = "DIRECTIONAL_MATURED"
    REVERSAL_WATCH_STARTED = "REVERSAL_WATCH_STARTED"
    DIRECTIONAL_REVERSAL_CONFIRMED = "DIRECTIONAL_REVERSAL_CONFIRMED"
    DIRECTIONAL_REVERSAL_LEG_STARTED = "DIRECTIONAL_REVERSAL_LEG_STARTED"
    DIRECTIONAL_REVERSAL_LEG_ESTABLISHED = "DIRECTIONAL_REVERSAL_LEG_ESTABLISHED"
    DIRECTIONAL_REVERSAL_LEG_FAILED = "DIRECTIONAL_REVERSAL_LEG_FAILED"
    DIRECTIONAL_TREND_RESTORED = "DIRECTIONAL_TREND_RESTORED"
    DIRECTIONAL_CONTINUATION_CONFIRMED = "DIRECTIONAL_CONTINUATION_CONFIRMED"
    DIRECTIONAL_REACCELERATION_CONFIRMED = "DIRECTIONAL_REACCELERATION_CONFIRMED"
    DIRECTIONAL_COMPLETED = "DIRECTIONAL_COMPLETED"
    BALANCE_FORMING_STARTED = "BALANCE_FORMING_STARTED"
    BALANCE_PROBABLE = "BALANCE_PROBABLE"
    BALANCE_LOCKED = "BALANCE_LOCKED"
    BALANCE_ESCAPE_STARTED = "BALANCE_ESCAPE_STARTED"
    BALANCE_ESCAPE_ACCEPTED = "BALANCE_ESCAPE_ACCEPTED"
    BALANCE_ESCAPE_FAILED = "BALANCE_ESCAPE_FAILED"
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


class DirectionObservationSource(StringEnum):
    OBSERVATION_STATE = "OBSERVATION_STATE"
    DIRECTIONAL_BIAS = "DIRECTIONAL_BIAS"
    TREND_DIRECTION = "TREND_DIRECTION"


class DirectionalEfficiencySource(StringEnum):
    NONE = "NONE"
    PRICE_ACTION = "PRICE_ACTION"
    TREND = "TREND"


class MaturityObservationSource(StringEnum):
    CURRENT_LEG = "CURRENT_LEG"
    EXTENSION = "EXTENSION"
    OBSERVATION_STATE = "OBSERVATION_STATE"


class ReversalWatchSource(StringEnum):
    EXHAUSTION = "EXHAUSTION"
    REJECTION = "REJECTION"
    FAILED_EXTREME = "FAILED_EXTREME"
    STRUCTURAL_FAILURE = "STRUCTURAL_FAILURE"
    OBSERVATION_STATE = "OBSERVATION_STATE"


__all__ = [
    "StringEnum",
    "TradeSide",
    "BoundarySide",
    "DirectionalBias",
    "QualityStatus",
    "EvidencePolarity",
    "AuctionStateName",
    "AdvisorAction",
    "BoundaryEpisodeStatus",
    "BoundaryResolution",
    "SetupFamily",
    "ContextAlignment",
    "DirectionalEpisodeState",
    "DirectionalEpisodeOrigin",
    "BalanceEpisodeState",
    "AuctionEventType",
    "StructuralPermissionResult",
    "SetupEventAction",
    "DirectionObservationSource",
    "DirectionalEfficiencySource",
    "MaturityObservationSource",
    "ReversalWatchSource",
]
