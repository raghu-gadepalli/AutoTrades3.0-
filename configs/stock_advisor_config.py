"""Independent StockAdvisor deployment policy.

The Advisor does not re-evaluate setup validity.  It applies only conservative
new-signal deployment checks using objective day-so-far and accepted-range
context persisted in the snapshot.
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


class StockAdvisorPolicyConfig(BaseModel):
    """Simple deployment policy applied at signal-evaluation time.

    SHADOW records the raw ALLOW/WATCH/BLOCK decision but preserves current
    signal creation. ENFORCE applies WATCH/BLOCK to new signal deployment.
    Existing signal and trade lifecycles remain untouched.
    """

    model_config = STRICT_CONFIG

    enabled: bool = True
    mode: Literal["SHADOW", "ENFORCE"] = "SHADOW"
    config_version: str = "STOCK_ADVISOR_V2_SIMPLE_SESSION_RANGE"

    # Preserve the already-tested exhaustion protection for same-direction
    # continuation-style deployments.  Opposite exhaustion reversals are not
    # blocked by this rule.
    block_exhausted_direction: bool = True
    exhaustion_block_families: Tuple[str, ...] = (
        "BREAKOUT_INITIATION",
        "ACCEPTED_BREAKOUT",
        "CONTINUATION",
        "REACCELERATION",
    )

    # Do not deploy ordinary directional candidates while price remains inside
    # an active accepted range.  Failed breakouts and the strict exhaustion
    # reversal subtype retain their own setup logic.
    defer_inside_accepted_range: bool = True
    accepted_range_tolerance_atr: float = Field(default=0.15, ge=0.0)
    inside_range_exempt_families: Tuple[str, ...] = ("FAILED_BREAKOUT",)
    inside_range_exempt_subtypes: Tuple[str, ...] = ("EXHAUSTION_REVERSAL",)

    # Do not chase a same-direction signal at the day-so-far extreme after the
    # stock has already travelled materially from the opposite session extreme.
    defer_extreme_chase: bool = True
    extreme_near_atr: float = Field(default=0.20, ge=0.0)
    extreme_min_prior_move_atr: float = Field(default=1.25, ge=0.0)

    # A conservative exception for a genuinely accepted range escape.  A mere
    # breakout initiation/probe is intentionally not enough.
    strong_confirmation_families: Tuple[str, ...] = ("ACCEPTED_BREAKOUT",)
    strong_confirmation_min_outside_atr: float = Field(default=0.15, ge=0.0)
    strong_confirmation_states_buy: Tuple[str, ...] = (
        "FRESH_EXPANSION",
        "ORDERLY_UPTREND",
        "REACCELERATION",
    )
    strong_confirmation_states_sell: Tuple[str, ...] = (
        "FRESH_EXPANSION",
        "ORDERLY_DOWNTREND",
        "REACCELERATION",
    )

    # Do not immediately fight a persistent day-so-far path.  These metrics are
    # calculated causally from every completed candle since the current session
    # low/high, not from a short rolling window.
    defer_against_session_path: bool = True
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


__all__ = ["StockAdvisorPolicyConfig", "STOCK_ADVISOR_CONFIG"]
