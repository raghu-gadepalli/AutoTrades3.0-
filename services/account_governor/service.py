"""Account Governor service contract and neutral diagnostic implementation.

The current provider deliberately does not authorise or block anything. Existing
TradeGenerator, UserSchema, instrument, and package-validation behaviour remains
authoritative until the Governor is implemented and replay-validated.
"""
from __future__ import annotations

from typing import Protocol

from enums.account_governor import (
    AccountGovernorAvailability,
    AccountGovernorInfluence,
    AccountGovernorState,
)
from services.account_governor.contracts import (
    AccountGovernorAssessment,
    AccountGovernorRequest,
)


class AccountGovernorProviderProtocol(Protocol):
    def assess_package(
        self,
        *,
        request: AccountGovernorRequest,
    ) -> AccountGovernorAssessment:
        ...


class AccountGovernorService:
    """Neutral provider used until account-governance rules are implemented."""

    def assess_package(
        self,
        *,
        request: AccountGovernorRequest,
    ) -> AccountGovernorAssessment:
        return AccountGovernorAssessment(
            userid=request.userid,
            as_of=request.as_of,
            availability=AccountGovernorAvailability.UNAVAILABLE,
            state=AccountGovernorState.UNKNOWN,
            influence=AccountGovernorInfluence.NONE,
            new_entry_allowed=None,
            force_flat=False,
            reason_codes=("ACCOUNT_GOVERNOR_NOT_IMPLEMENTED",),
            metrics={},
            limits={},
        )


__all__ = ["AccountGovernorProviderProtocol", "AccountGovernorService"]
