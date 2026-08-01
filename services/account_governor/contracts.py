"""Strict immutable contracts for account-level deployment governance."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from enums.account_governor import (
    AccountGovernorAvailability,
    AccountGovernorInfluence,
    AccountGovernorState,
)
from utils.datetime_utils import to_ist_naive


class _GovernorContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_assignment=True,
        validate_default=True,
        allow_inf_nan=False,
    )


class AccountGovernorPackageLeg(_GovernorContract):
    instrument_type: str = Field(min_length=1, max_length=12)
    symbol: str = Field(min_length=1, max_length=64)
    side: str = Field(min_length=1, max_length=8)
    entry_price: Decimal = Field(gt=Decimal("0"))
    quantity: Optional[int] = Field(default=None, ge=1)
    lotsize: int = Field(ge=1)

    @field_validator("instrument_type", "symbol", "side", mode="before")
    @classmethod
    def _normalise_text(cls, value: object) -> str:
        text = str(value or "").strip().upper()
        if not text:
            raise ValueError("Account Governor package leg text fields are required")
        return text

    @model_validator(mode="after")
    def _validate_leg(self) -> "AccountGovernorPackageLeg":
        if self.instrument_type not in {"EQ", "FUT", "CE", "PE"}:
            raise ValueError("Unsupported Account Governor instrument_type")
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("Unsupported Account Governor side")
        return self


class AccountGovernorRequest(_GovernorContract):
    userid: str = Field(min_length=1, max_length=50)
    as_of: datetime
    source: str = Field(min_length=1, max_length=50)
    signal_id: Optional[str] = Field(default=None, max_length=100)
    equity_ref: str = Field(min_length=1, max_length=64)
    side: str = Field(min_length=1, max_length=8)
    execution_mode: str = Field(min_length=1, max_length=20)
    product_type: str = Field(min_length=1, max_length=20)
    position_style: str = Field(min_length=1, max_length=20)
    intraday_only: bool
    proposed_legs: Tuple[AccountGovernorPackageLeg, ...] = Field(min_length=1)

    @field_validator(
        "userid",
        "source",
        "equity_ref",
        "side",
        "execution_mode",
        "product_type",
        "position_style",
        mode="before",
    )
    @classmethod
    def _normalise_text(cls, value: object) -> str:
        text = str(value or "").strip().upper()
        if not text:
            raise ValueError("Account Governor request text fields are required")
        return text

    @field_validator("signal_id", mode="before")
    @classmethod
    def _normalise_signal_id(cls, value: object) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @model_validator(mode="after")
    def _validate_request(self) -> "AccountGovernorRequest":
        as_of = to_ist_naive(self.as_of)
        if as_of is None:
            raise ValueError("AccountGovernorRequest as_of must be valid")
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("AccountGovernorRequest side must be BUY or SELL")
        if self.execution_mode not in {"REAL", "VIRTUAL"}:
            raise ValueError("AccountGovernorRequest execution_mode must be REAL or VIRTUAL")
        identities = [
            (leg.instrument_type, leg.symbol)
            for leg in self.proposed_legs
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("Account Governor proposed legs must be unique")
        return self


class AccountGovernorAssessment(_GovernorContract):
    userid: str = Field(min_length=1, max_length=50)
    as_of: datetime
    availability: AccountGovernorAvailability
    state: AccountGovernorState
    influence: AccountGovernorInfluence
    new_entry_allowed: Optional[bool] = None
    force_flat: bool = False
    reason_codes: Tuple[str, ...] = ()
    metrics: Dict[str, Any] = Field(default_factory=dict)
    limits: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("userid", mode="before")
    @classmethod
    def _normalise_userid(cls, value: object) -> str:
        userid = str(value or "").strip().upper()
        if not userid:
            raise ValueError("AccountGovernorAssessment userid is required")
        return userid

    @field_validator("reason_codes", mode="before")
    @classmethod
    def _normalise_reasons(cls, value: object) -> Tuple[str, ...]:
        return tuple(
            str(item).strip().upper()
            for item in (value or ())
            if str(item).strip()
        )

    @model_validator(mode="after")
    def _validate_assessment(self) -> "AccountGovernorAssessment":
        if to_ist_naive(self.as_of) is None:
            raise ValueError("AccountGovernorAssessment as_of must be valid")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("Account Governor reason codes must be unique")

        if self.availability is AccountGovernorAvailability.UNAVAILABLE:
            if self.state is not AccountGovernorState.UNKNOWN:
                raise ValueError("Unavailable Account Governor must be UNKNOWN")
            if self.influence is not AccountGovernorInfluence.NONE:
                raise ValueError("Unavailable Account Governor influence must be NONE")
            if self.new_entry_allowed is not None or self.force_flat:
                raise ValueError("Unavailable Account Governor cannot claim authority")
            if self.metrics or self.limits:
                raise ValueError("Unavailable Account Governor cannot claim evaluated metrics or limits")
            return self

        if self.state is AccountGovernorState.UNKNOWN:
            raise ValueError("Available Account Governor cannot be UNKNOWN")
        if self.new_entry_allowed is None:
            raise ValueError("Available Account Governor must decide new-entry permission")
        if self.state is AccountGovernorState.ALLOW:
            if not self.new_entry_allowed or self.force_flat:
                raise ValueError("ALLOW requires entry permission and no force-flat")
        elif self.state is AccountGovernorState.BLOCK_NEW_ENTRIES:
            if self.new_entry_allowed or self.force_flat:
                raise ValueError("BLOCK_NEW_ENTRIES must block entries without force-flat")
        elif self.state is AccountGovernorState.FORCE_FLAT:
            if self.new_entry_allowed or not self.force_flat:
                raise ValueError("FORCE_FLAT must block entries and require flattening")
        return self


__all__ = [
    "AccountGovernorPackageLeg",
    "AccountGovernorRequest",
    "AccountGovernorAssessment",
]
