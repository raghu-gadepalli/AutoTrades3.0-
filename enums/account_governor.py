"""Domain enums for account-level deployment governance.

Account Governor is intentionally separate from Auction, Advisor context, and
legacy application enums. It governs whether an account may deploy or must
flatten; it does not judge the stock thesis or resolve instruments.
"""
from __future__ import annotations

from enum import Enum


class _StringEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class AccountGovernorAvailability(_StringEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class AccountGovernorState(_StringEnum):
    UNKNOWN = "UNKNOWN"
    ALLOW = "ALLOW"
    BLOCK_NEW_ENTRIES = "BLOCK_NEW_ENTRIES"
    FORCE_FLAT = "FORCE_FLAT"


class AccountGovernorInfluence(_StringEnum):
    NONE = "NONE"
    DIAGNOSTIC = "DIAGNOSTIC"
    ENFORCED = "ENFORCED"


__all__ = [
    "AccountGovernorAvailability",
    "AccountGovernorState",
    "AccountGovernorInfluence",
]
