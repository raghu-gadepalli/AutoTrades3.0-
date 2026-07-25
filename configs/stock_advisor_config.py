"""Independent StockAdvisor deployment policy.

Auction/snapshot generation persists objective stock/setup context only.
StockAdvisor is evaluated by SignalGenerator so the same stored snapshots can
be replayed under different Advisor rules without regenerating market history.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Literal, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator


STRICT_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    validate_default=True,
)


class StockAdvisorPolicyConfig(BaseModel):
    """Deployment policy applied at signal-evaluation time.

    SHADOW records the raw ALLOW/WATCH/BLOCK decision but preserves current
    signal creation. ENFORCE applies WATCH/BLOCK to new signal deployment.
    Existing signal lifecycle remains driven by Auction/opportunity evidence.
    """

    model_config = STRICT_CONFIG

    enabled: bool = True
    mode: Literal["SHADOW", "ENFORCE"] = "SHADOW"
    config_version: str = "STOCK_ADVISOR_V1_1_BACKGROUND_RANGE"

    block_exhausted_direction: bool = True
    exhaustion_block_families: Tuple[str, ...] = (
        "BREAKOUT_INITIATION",
        "ACCEPTED_BREAKOUT",
        "CONTINUATION",
        "REACCELERATION",
    )
    block_balanced_non_directional: bool = True
    block_rotational_range_edge: bool = True
    block_rotational_inside_range: bool = True
    watch_reversal_inside_balanced_range: bool = True
    watch_unconfirmed_fresh_escape: bool = True
    allow_confirmed_fresh_expansion_override: bool = True

    buy_range_edge_percentile: float = Field(default=0.70, ge=0.50, le=1.0)
    sell_range_edge_percentile: float = Field(default=0.30, ge=0.0, le=0.50)
    fresh_escape_min_atr: float = Field(default=0.15, ge=0.0)
    fresh_escape_efficiency_min: float = Field(default=0.45, ge=0.0, le=1.0)
    rotational_side_switches_to_block: int = Field(default=1, ge=1)

    # Time remains diagnostic only. Any future generation/execution window is
    # an explicit operating policy, not a hidden late-day quality gate.
    time_of_day_gate_enabled: bool = False

    @model_validator(mode="after")
    def _validate_range_edges(self) -> "StockAdvisorPolicyConfig":
        if self.sell_range_edge_percentile >= self.buy_range_edge_percentile:
            raise ValueError(
                "Expected sell_range_edge_percentile < buy_range_edge_percentile"
            )
        return self

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
