"""Independent StockAdvisor deployment policy.

The Advisor does not re-evaluate setup validity. It applies only conservative
new-signal deployment checks using objective day-so-far and accepted-range
context persisted in the snapshot.

Each rule owns its deployment consequence. There is no global shadow/enforce
switch:

* ALLOW records the rule match but permits deployment.
* WATCH records a contextual concern and permits deployment.
* BLOCK suppresses only new signal creation while the rule remains true.

Existing signal and trade lifecycles are never changed by these rules.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Literal, Tuple

from pydantic import BaseModel, ConfigDict, Field


STRICT_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    validate_default=True,
)

AdvisorRuleAction = Literal["ALLOW", "WATCH", "BLOCK"]


class StockAdvisorPolicyConfig(BaseModel):
    """Per-rule deployment policy applied at signal-evaluation time."""

    model_config = STRICT_CONFIG

    enabled: bool = True
    config_version: str = "STOCK_ADVISOR_V4_CURRENT_DEPLOYMENT"

    # Same-direction continuation after a current exhaustion episode. Opposite
    # exhaustion reversals are not affected by this rule.
    same_direction_exhaustion_action: AdvisorRuleAction = "BLOCK"
    same_direction_exhaustion_families: Tuple[str, ...] = (
        "BREAKOUT_INITIATION",
        "ACCEPTED_BREAKOUT",
        "CONTINUATION",
        "REACCELERATION",
    )

    # Ordinary directional candidates while price remains inside an active
    # accepted range. Failed breakouts and the strict exhaustion reversal
    # subtype retain their own setup logic.
    inside_accepted_range_action: AdvisorRuleAction = "WATCH"
    accepted_range_tolerance_atr: float = Field(default=0.15, ge=0.0)
    inside_range_exempt_families: Tuple[str, ...] = ("FAILED_BREAKOUT",)
    inside_range_exempt_subtypes: Tuple[str, ...] = ("EXHAUSTION_REVERSAL",)

    # Same-direction deployment at the day-so-far extreme after the stock has
    # already travelled materially from the opposite session extreme.
    extreme_chase_action: AdvisorRuleAction = "WATCH"
    extreme_near_atr: float = Field(default=0.20, ge=0.0)
    extreme_min_prior_move_atr: float = Field(default=1.25, ge=0.0)

    # Accepted Breakout is a current deployment claim, not a historical one.
    # A previously confirmed candidate may remain in stock_opportunities, but
    # it cannot create a signal unless the current completed-candle close is still
    # beyond the same accepted boundary and that range remains tradable.
    accepted_breakout_current_context_action: AdvisorRuleAction = "BLOCK"
    accepted_breakout_current_context_families: Tuple[str, ...] = (
        "ACCEPTED_BREAKOUT",
    )

    # Counter-path deployment against a persistent day-so-far path. These
    # metrics are calculated causally from every completed candle since the
    # current session low/high, not from a short rolling window.
    against_session_path_action: AdvisorRuleAction = "WATCH"
    session_path_min_bars: int = Field(default=3, ge=1)
    session_path_min_move_atr: float = Field(default=0.75, ge=0.0)
    session_path_efficiency_min: float = Field(default=0.45, ge=0.0, le=1.0)
    session_path_directional_ratio_min: float = Field(default=0.60, ge=0.0, le=1.0)
    session_path_exempt_families: Tuple[str, ...] = ("FAILED_BREAKOUT",)
    session_path_exempt_subtypes: Tuple[str, ...] = ("EXHAUSTION_REVERSAL",)

    # Time remains diagnostic only. Any future operating window must be an
    # explicit service policy, not a hidden quality gate.
    time_of_day_gate_enabled: bool = False

    def resolved_dict(self) -> Dict[str, Any]:
        return self.model_dump(mode="json")

    def stable_hash(self) -> str:
        payload = json.dumps(
            self.resolved_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


STOCK_ADVISOR_CONFIG = StockAdvisorPolicyConfig()


__all__ = [
    "AdvisorRuleAction",
    "StockAdvisorPolicyConfig",
    "STOCK_ADVISOR_CONFIG",
]
