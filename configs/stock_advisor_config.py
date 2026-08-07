"""Strict StockAdvisor deployment-intelligence policy.

The Advisor is a causal deployment-quality gate.  It does not create setups,
change Auction structure or episode state, close signals, or manage trades.  The defaults below
are deliberately conservative and fully explainable so replay can determine
whether WATCH/BLOCK decisions should be retained or relaxed.
"""
from __future__ import annotations

from typing import Literal, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator


STRICT_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    validate_default=True,
)

AdvisorRuleAction = Literal["ALLOW", "WATCH", "BLOCK"]
AdvisorContextInfluence = Literal["NONE", "DIAGNOSTIC", "WEIGHTED"]


class MarketRegimeAdvisorContextConfig(BaseModel):
    model_config = STRICT_CONFIG

    enabled: bool = True
    influence: AdvisorContextInfluence = "NONE"


class MatureRangeChurnPolicyConfig(BaseModel):
    model_config = STRICT_CONFIG

    enabled: bool = True
    action: AdvisorRuleAction = "BLOCK"
    families: Tuple[str, ...] = (
        "BREAKOUT_INITIATION",
        "ACCEPTED_BREAKOUT",
        "CONTINUATION",
        "REACCELERATION",
    )
    min_episode_age_bars: int = Field(default=12, ge=1)
    max_range_width_pct: float = Field(default=0.80, gt=0.0)
    min_containment_ratio: float = Field(default=0.68, ge=0.0, le=1.0)
    max_path_efficiency: float = Field(default=0.35, ge=0.0, le=1.0)
    min_midpoint_crossings: int = Field(default=4, ge=0)
    min_vwap_crossings: int = Field(default=3, ge=0)
    min_total_travel_range_multiple: float = Field(default=2.50, ge=0.0)
    min_structure_flips: int = Field(default=3, ge=0)
    min_supporting_churn_signals: int = Field(default=1, ge=1, le=4)


class BarrierPolicyConfig(BaseModel):
    model_config = STRICT_CONFIG

    enabled: bool = True
    action: AdvisorRuleAction = "WATCH"
    families: Tuple[str, ...] = (
        "BREAKOUT_INITIATION",
        "ACCEPTED_BREAKOUT",
        "CONTINUATION",
        "REACCELERATION",
    )
    near_atr: float = Field(default=0.40, gt=0.0)
    near_pct: float = Field(default=0.20, gt=0.0)
    include_prior_session_extreme: bool = True
    include_opening_range: bool = True
    include_previous_day_extreme: bool = True
    include_ema_slow: bool = True
    include_ema_ref: bool = True


class RepeatedEpisodePolicyConfig(BaseModel):
    model_config = STRICT_CONFIG

    enabled: bool = True
    action: AdvisorRuleAction = "BLOCK"
    min_prior_deployments_same_episode: int = Field(default=1, ge=1)
    require_same_side: bool = True
    balance_min_escape_attempts: int = Field(default=2, ge=1)
    balance_min_failed_escapes: int = Field(default=1, ge=1)


class StockMapBoundaryTransitionPolicyConfig(BaseModel):
    """Research-only gate for causal StockMap reversal re-entry deployment."""

    model_config = STRICT_CONFIG

    enabled: bool = True
    research_only: bool = True
    exclusive: bool = True
    families: Tuple[str, ...] = ("REVERSAL",)
    non_applicable_action: AdvisorRuleAction = "WATCH"
    wait_action: AdvisorRuleAction = "WATCH"
    allow_action: AdvisorRuleAction = "ALLOW"


class DeferredEntryFreshnessPolicyConfig(BaseModel):
    model_config = STRICT_CONFIG

    enabled: bool = True
    action: AdvisorRuleAction = "WATCH"
    min_age_minutes: float = Field(default=9.0, ge=0.0)
    history_bars: int = Field(default=12, ge=5)
    pullback_min_atr: float = Field(default=0.30, gt=0.0)
    resumption_min_atr: float = Field(default=0.15, gt=0.0)
    consolidation_bars: int = Field(default=3, ge=2)
    consolidation_max_atr: float = Field(default=0.55, gt=0.0)
    breakout_buffer_atr: float = Field(default=0.05, ge=0.0)
    accepted_fresh_event_types: Tuple[str, ...] = (
        "BALANCE_ESCAPE_STARTED",
        "BALANCE_ESCAPE_ACCEPTED",
        "DIRECTIONAL_REVERSED",
    )


class StockAdvisorPolicyConfig(BaseModel):
    """Configuration consumed by StockAdvisor and deferred-entry gating."""

    model_config = STRICT_CONFIG

    enabled: bool = True

    inside_accepted_range_action: AdvisorRuleAction = "WATCH"
    inside_range_exempt_families: Tuple[str, ...] = ("FAILED_BREAKOUT",)
    inside_range_exempt_subtypes: Tuple[str, ...] = (
        "EXHAUSTION_REVERSAL",
    )

    accepted_breakout_current_context_action: AdvisorRuleAction = "BLOCK"
    accepted_breakout_current_context_families: Tuple[str, ...] = (
        "ACCEPTED_BREAKOUT",
    )

    day_history_limit: int = Field(default=160, ge=10)
    prior_opportunity_limit: int = Field(default=50, ge=1)

    market_regime_context: MarketRegimeAdvisorContextConfig = Field(
        default_factory=MarketRegimeAdvisorContextConfig
    )

    mature_range_churn: MatureRangeChurnPolicyConfig = Field(
        default_factory=MatureRangeChurnPolicyConfig
    )
    barriers: BarrierPolicyConfig = Field(default_factory=BarrierPolicyConfig)
    repeated_episode: RepeatedEpisodePolicyConfig = Field(
        default_factory=RepeatedEpisodePolicyConfig
    )
    stockmap_boundary_transition: StockMapBoundaryTransitionPolicyConfig = Field(
        default_factory=StockMapBoundaryTransitionPolicyConfig
    )
    deferred_entry: DeferredEntryFreshnessPolicyConfig = Field(
        default_factory=DeferredEntryFreshnessPolicyConfig
    )

    @model_validator(mode="after")
    def validate_actions(self) -> "StockAdvisorPolicyConfig":
        # The first enforcement patch is intentionally limited to the requested
        # posture.  Prevent accidental configuration drift that changes BLOCK
        # rules into silent ALLOWs during replay.
        if self.mature_range_churn.action != "BLOCK":
            raise ValueError("mature_range_churn.action must remain BLOCK")
        if self.repeated_episode.action != "BLOCK":
            raise ValueError("repeated_episode.action must remain BLOCK")
        if self.barriers.action != "WATCH":
            raise ValueError("barriers.action must remain WATCH")
        if self.deferred_entry.action != "WATCH":
            raise ValueError("deferred_entry.action must remain WATCH")
        boundary = self.stockmap_boundary_transition
        if boundary.non_applicable_action != "WATCH":
            raise ValueError(
                "stockmap_boundary_transition.non_applicable_action must remain WATCH"
            )
        if boundary.wait_action != "WATCH":
            raise ValueError("stockmap_boundary_transition.wait_action must remain WATCH")
        if boundary.allow_action != "ALLOW":
            raise ValueError("stockmap_boundary_transition.allow_action must remain ALLOW")
        if boundary.enabled and not boundary.exclusive:
            raise ValueError(
                "research stockmap_boundary_transition must remain exclusive when enabled"
            )
        if self.market_regime_context.influence != "NONE":
            raise ValueError(
                "market_regime_context.influence must remain NONE until regime implementation"
            )
        return self


STOCK_ADVISOR_CONFIG = StockAdvisorPolicyConfig()


__all__ = [
    "AdvisorRuleAction",
    "AdvisorContextInfluence",
    "MarketRegimeAdvisorContextConfig",
    "MatureRangeChurnPolicyConfig",
    "BarrierPolicyConfig",
    "RepeatedEpisodePolicyConfig",
    "StockMapBoundaryTransitionPolicyConfig",
    "DeferredEntryFreshnessPolicyConfig",
    "StockAdvisorPolicyConfig",
    "STOCK_ADVISOR_CONFIG",
]
