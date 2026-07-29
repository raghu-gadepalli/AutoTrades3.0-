"""Independent StockAdvisor deployment policy.

The Advisor does not re-evaluate setup validity. It applies only conservative
new-signal deployment checks using current Auction observation context.
"""
from __future__ import annotations

from typing import Literal, Tuple

from pydantic import BaseModel, ConfigDict


STRICT_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    validate_default=True,
)

AdvisorRuleAction = Literal["ALLOW", "WATCH", "BLOCK"]


class StockAdvisorPolicyConfig(BaseModel):
    """Only policy fields consumed by the current StockAdvisor."""

    model_config = STRICT_CONFIG

    enabled: bool = True

    same_direction_exhaustion_action: AdvisorRuleAction = "BLOCK"
    same_direction_exhaustion_families: Tuple[str, ...] = (
        "BREAKOUT_INITIATION",
        "ACCEPTED_BREAKOUT",
        "CONTINUATION",
        "REACCELERATION",
    )

    inside_accepted_range_action: AdvisorRuleAction = "WATCH"
    inside_range_exempt_families: Tuple[str, ...] = ("FAILED_BREAKOUT",)
    inside_range_exempt_subtypes: Tuple[str, ...] = ("EXHAUSTION_REVERSAL",)

    accepted_breakout_current_context_action: AdvisorRuleAction = "BLOCK"
    accepted_breakout_current_context_families: Tuple[str, ...] = (
        "ACCEPTED_BREAKOUT",
    )


STOCK_ADVISOR_CONFIG = StockAdvisorPolicyConfig()


__all__ = [
    "AdvisorRuleAction",
    "StockAdvisorPolicyConfig",
    "STOCK_ADVISOR_CONFIG",
]
