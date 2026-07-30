"""Strict configuration for the snapshot-integrated Auction Engine.

Only settings read by the current production path are retained. Auction owns
local market interpretation and snapshot-carried continuity. SignalGenerator
owns signal persistence and lifecycle; Auction performs no database persistence
and reads no active signal or trade state.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from enums.auction_engine import (
    AuctionEventType,
    AuctionStateName,
    BalanceEpisodeState,
    DirectionObservationSource,
    MaturityObservationSource,
    ReversalWatchSource,
    SetupFamily,
    StructuralPermissionResult,
)


STRICT_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    validate_default=True,
)


def _require_unique_nonempty(name: str, values: Tuple[Any, ...]) -> None:
    if not values:
        raise ValueError(f"{name} cannot be empty")
    if len(values) != len(set(values)):
        raise ValueError(f"{name} cannot contain duplicates")


class AuctionEngineRuntimeConfig(BaseModel):
    """Engine identity and cadence used by current snapshot processing."""

    model_config = STRICT_CONFIG

    engine_name: str = Field(default="AUTOTRADES_AUCTION_ENGINE", min_length=1)
    engine_version: str = Field(
        default="1.1.0",
        min_length=1,
        pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$",
    )
    snapshot_interval_minutes: float = Field(default=3.0, gt=0.0)


class AuctionEvidenceConfig(BaseModel):
    """Thresholds consumed by causal Auction evidence construction."""

    model_config = STRICT_CONFIG

    retain_raw_fact_diagnostics: bool = True
    required_top_level_blocks: Tuple[str, ...] = (
        "bar",
        "levels",
        "indicators",
        "volume",
        "market_windows",
        "price_action",
        "structure",
    )
    minimum_history_bars: int = Field(default=5, ge=1)
    floating_point_tolerance: float = Field(default=1e-9, gt=0.0)
    derivatives_preferred_windows: Tuple[str, ...] = ("15m", "5m", "60m", "sod")

    strong_bar_move_atr: float = Field(default=0.50, gt=0.0)
    strong_bar_body_fraction: float = Field(default=0.55, ge=0.0, le=1.0)
    directional_close_position: float = Field(default=0.70, ge=0.50, le=1.0)

    compression_range_width_atr_max: float = Field(default=2.00, gt=0.0)
    compression_hma_spread_atr_max: float = Field(default=0.35, gt=0.0)
    compression_max_bar_move_atr: float = Field(default=0.40, gt=0.0)
    compression_require_low_efficiency_and_overlap: bool = True
    compression_recent_bars: int = Field(default=5, ge=3)
    compression_reference_bars: int = Field(default=12, ge=5)
    compression_contraction_ratio_max: float = Field(default=0.80, gt=0.0, le=1.0)
    compression_hma_contraction_ratio_max: float = Field(
        default=0.80,
        gt=0.0,
        le=1.0,
    )

    rolling_efficiency_bars: int = Field(default=8, ge=4)
    rolling_overlap_bars: int = Field(default=6, ge=3)
    ema_slow_context_lookback_bars: int = Field(default=5, ge=2)
    atr_context_lookback_bars: int = Field(default=5, ge=2)

    extension_move_from_anchor_atr: float = Field(default=2.00, gt=0.0)
    extension_vwap_distance_atr: float = Field(default=1.50, gt=0.0)
    extension_rsi_high: float = Field(default=75.0, ge=50.0, le=100.0)
    extension_rsi_low: float = Field(default=25.0, ge=0.0, le=50.0)
    extension_bollinger_high: float = 1.00
    extension_bollinger_low: float = 0.00
    maturity_components_required: int = Field(default=2, ge=1)
    extension_min_history_bars_for_maturity: int = Field(default=8, ge=2)
    extension_progress_decay_min: float = Field(default=0.35, ge=0.0, le=1.0)
    extension_maturity_requires_directional_distance: bool = True
    extension_maturity_requires_progress_or_rejection: bool = True


class AuctionStatePolicyConfig(BaseModel):
    """Observation-state thresholds consumed by the current provider."""

    model_config = STRICT_CONFIG

    history_bars: int = Field(default=12, ge=3)
    minimum_state_hold_bars: int = Field(default=2, ge=1)
    initial_state_confirmation_bars: int = Field(default=2, ge=1)
    ordinary_transition_confirmation_bars: int = Field(default=2, ge=1)
    trend_establishment_bars: int = Field(default=2, ge=1)
    pullback_confirmation_bars: int = Field(default=2, ge=1)
    recompression_confirmation_bars: int = Field(default=3, ge=1)
    failure_level_confirmation_bars: int = Field(default=2, ge=1)
    reversal_confirmation_bars: int = Field(default=2, ge=1)

    reacceleration_min_hold_bars: int = Field(default=2, ge=1)
    mature_extension_min_hold_bars: int = Field(default=2, ge=1)
    trend_failure_min_hold_bars: int = Field(default=2, ge=1)
    reversal_min_hold_bars: int = Field(default=3, ge=1)

    pullback_max_bars: int = Field(default=10, ge=2)
    recompression_max_bars: int = Field(default=15, ge=3)

    current_leg_extension_atr: float = Field(default=1.50, gt=0.0)
    current_leg_current_extension_atr: float = Field(default=1.00, gt=0.0)
    current_leg_min_bars_for_maturity: int = Field(default=4, ge=2)
    current_leg_no_progress_bars: int = Field(default=2, ge=1)
    current_leg_progress_tolerance_atr: float = Field(default=0.05, ge=0.0)
    current_leg_max_retracement_atr: float = Field(default=0.75, ge=0.0)
    current_leg_max_retracement_fraction: float = Field(default=0.40, ge=0.0, le=1.0)
    current_leg_reanchor_progress_atr: float = Field(default=0.75, gt=0.0)

    orderly_trend_efficiency_min: float = Field(default=0.45, ge=0.0, le=1.0)
    controlled_pullback_max_adverse_atr: float = Field(default=0.75, gt=0.0)
    reacceleration_displacement_atr: float = Field(default=0.35, gt=0.0)
    trend_failure_opposite_displacement_atr: float = Field(default=0.50, gt=0.0)
    failure_level_breach_atr: float = Field(default=0.10, ge=0.0)
    trend_protection_min_improvement_atr: float = Field(default=0.05, ge=0.0)

    slow_ema_close_spread_atr_max: float = Field(default=0.75, ge=0.0)
    slow_ema_flat_slope_atr_per_bar_max: float = Field(default=0.04, ge=0.0)
    slow_ema_spread_expansion_atr_per_bar_min: float = Field(default=0.01, ge=0.0)
    atr_contraction_ratio_max: float = Field(default=0.90, gt=0.0)
    atr_expansion_ratio_min: float = Field(default=1.10, gt=0.0)

    exhaustion_context_min_extension_atr: float = Field(default=1.50, ge=0.0)
    exhaustion_context_min_leg_age_bars: int = Field(default=3, ge=1)
    exhaustion_context_max_bars: int = Field(default=6, ge=1)


class BoundaryPolicyConfig(BaseModel):
    """Accepted-range selection rules used by evidence construction."""

    model_config = STRICT_CONFIG

    require_accepted_range: bool = True
    require_breakout_eligible_accepted_range: bool = True
    allow_candidate_range_fallback: bool = False
    allow_raw_range_fallback: bool = False
    allow_orb_seeded_accepted_range: bool = False


class BreakoutInitiationPolicyConfig(BaseModel):
    model_config = STRICT_CONFIG

    max_entry_distance_atr: float = Field(default=0.75, gt=0.0)
    minimum_session_minutes: float = Field(default=30.0, ge=0.0)


class AcceptedOutcomePolicyConfig(BaseModel):
    model_config = STRICT_CONFIG

    max_entry_distance_atr: float = Field(default=0.75, gt=0.0)
    minimum_session_minutes: float = Field(default=30.0, ge=0.0)


class ContinuationPolicyConfig(BaseModel):
    model_config = STRICT_CONFIG

    max_entry_distance_atr: float = Field(default=0.75, gt=0.0)
    minimum_session_minutes: float = Field(default=30.0, ge=0.0)


class ReversalPolicyConfig(BaseModel):
    model_config = STRICT_CONFIG

    max_entry_distance_from_failure_level_atr: float = Field(default=2.50, gt=0.0)
    minimum_session_minutes: float = Field(default=30.0, ge=0.0)


class DirectionalEpisodePolicyConfig(BaseModel):
    """Authoritative directional-episode thresholds and observation policy."""

    model_config = STRICT_CONFIG

    start_confirmation_bars: int = Field(default=2, ge=1)
    opposite_completion_bars: int = Field(default=2, ge=1)
    inactive_completion_bars: int = Field(default=6, ge=2)

    reversal_watch_max_bars: int = Field(default=20, ge=2)
    reversal_confirmation_closes: int = Field(default=1, ge=1)
    reversal_require_rejection: bool = True
    reversal_require_continuation_failure: bool = True
    reversal_confirmation_level_tolerance_atr: float = Field(default=0.0, ge=0.0)
    reversal_first_adverse_min_close_atr: float = Field(default=0.0, ge=0.0)
    reversal_continuation_failure_bars: int = Field(default=1, ge=1)
    trend_restoration_confirmation_bars: int = Field(default=2, ge=1)

    reversal_leg_establishment_closes: int = Field(default=2, ge=1)
    reversal_leg_min_progress_atr: float = Field(default=0.50, ge=0.0)
    reversal_leg_failure_closes: int = Field(default=2, ge=1)

    start_observation_states: Tuple[AuctionStateName, ...] = (
        AuctionStateName.FRESH_EXPANSION,
        AuctionStateName.ORDERLY_UPTREND,
        AuctionStateName.ORDERLY_DOWNTREND,
        AuctionStateName.CONTROLLED_PULLBACK,
        AuctionStateName.RECOMPRESSION,
        AuctionStateName.REACCELERATION,
        AuctionStateName.MATURE_EXTENSION,
    )
    up_observation_states: Tuple[AuctionStateName, ...] = (
        AuctionStateName.ORDERLY_UPTREND,
    )
    down_observation_states: Tuple[AuctionStateName, ...] = (
        AuctionStateName.ORDERLY_DOWNTREND,
    )
    maturity_observation_states: Tuple[AuctionStateName, ...] = (
        AuctionStateName.MATURE_EXTENSION,
    )
    reversal_watch_observation_states: Tuple[AuctionStateName, ...] = (
        AuctionStateName.TREND_FAILURE,
    )
    start_blocking_balance_states: Tuple[BalanceEpisodeState, ...] = (
        BalanceEpisodeState.LOCKED,
        BalanceEpisodeState.ESCAPE_WATCH,
        BalanceEpisodeState.FAILED_BACK_INSIDE,
    )

    direction_source_precedence: Tuple[DirectionObservationSource, ...] = (
        DirectionObservationSource.OBSERVATION_STATE,
        DirectionObservationSource.DIRECTIONAL_BIAS,
        DirectionObservationSource.TREND_DIRECTION,
    )
    maturity_sources: Tuple[MaturityObservationSource, ...] = (
        MaturityObservationSource.CURRENT_LEG,
        MaturityObservationSource.EXTENSION,
        MaturityObservationSource.OBSERVATION_STATE,
    )
    reversal_watch_sources: Tuple[ReversalWatchSource, ...] = (
        ReversalWatchSource.EXHAUSTION,
        ReversalWatchSource.REJECTION,
        ReversalWatchSource.FAILED_EXTREME,
        ReversalWatchSource.STRUCTURAL_FAILURE,
        ReversalWatchSource.OBSERVATION_STATE,
    )

    @model_validator(mode="after")
    def _validate_directional_policy(self) -> "DirectionalEpisodePolicyConfig":
        _require_unique_nonempty("start_observation_states", self.start_observation_states)
        _require_unique_nonempty("up_observation_states", self.up_observation_states)
        _require_unique_nonempty("down_observation_states", self.down_observation_states)
        _require_unique_nonempty(
            "maturity_observation_states",
            self.maturity_observation_states,
        )
        _require_unique_nonempty(
            "reversal_watch_observation_states",
            self.reversal_watch_observation_states,
        )
        _require_unique_nonempty("direction_source_precedence", self.direction_source_precedence)
        _require_unique_nonempty("maturity_sources", self.maturity_sources)
        _require_unique_nonempty("reversal_watch_sources", self.reversal_watch_sources)
        _require_unique_nonempty(
            "start_blocking_balance_states",
            self.start_blocking_balance_states,
        )
        overlap = set(self.up_observation_states).intersection(self.down_observation_states)
        if overlap:
            raise ValueError("UP and DOWN observation-state mappings cannot overlap")
        return self


class BalanceEpisodePolicyConfig(BaseModel):
    """Authoritative balance-episode geometry, hysteresis and escape policy."""

    model_config = STRICT_CONFIG

    probable_min_observations: int = Field(default=5, ge=3)
    probable_min_contained_bars: int = Field(default=3, ge=2)
    probable_containment_ratio_min: float = Field(default=0.60, gt=0.0, le=1.0)

    lock_min_observations: int = Field(default=8, ge=4)
    lock_min_contained_bars: int = Field(default=5, ge=3)
    lock_containment_ratio_min: float = Field(default=0.625, gt=0.0, le=1.0)

    forming_reset_bars: int = Field(default=2, ge=1)
    efficiency_max: float = Field(default=0.35, ge=0.0, le=1.0)
    overlap_min: float = Field(default=0.55, ge=0.0, le=1.0)
    range_width_atr_max: float = Field(default=2.50, gt=0.0)
    source_range_inside_tolerance_atr: float = Field(default=0.15, ge=0.0)
    candidate_merge_overlap_min: float = Field(default=0.50, ge=0.0, le=1.0)
    forming_excursion_tolerance_atr: float = Field(default=0.15, ge=0.0)

    escape_min_atr: float = Field(default=0.15, ge=0.0)
    escape_acceptance_closes: int = Field(default=2, ge=1)
    failed_reentry_closes: int = Field(default=1, ge=1)

    # A failed escape does not immediately restore initiation permission. The
    # frozen balance must first regain stable inside containment.
    failed_escape_rearm_inside_closes: int = Field(default=2, ge=1)
    failed_escape_rearm_min_bars: int = Field(default=2, ge=1)

    # Raw escape attempts are episode facts, independent of whether a setup or
    # signal was eventually deployed. Once the limit is reached, a materially
    # new accepted range is required before another escape can be initiated.
    max_escape_attempts_per_episode: int = Field(default=3, ge=1)
    max_same_side_escape_attempts: int = Field(default=2, ge=1)
    attempt_limit_new_range_overlap_max: float = Field(
        default=0.50,
        ge=0.0,
        le=1.0,
    )

    require_non_provisional_source_range: bool = True
    require_breakout_eligible_source_range: bool = True

    @model_validator(mode="after")
    def _validate_balance_policy(self) -> "BalanceEpisodePolicyConfig":
        if self.lock_min_observations < self.probable_min_observations:
            raise ValueError(
                "Balance lock observations cannot be less than probable observations"
            )
        if self.lock_min_contained_bars < self.probable_min_contained_bars:
            raise ValueError(
                "Balance lock contained bars cannot be less than probable contained bars"
            )
        if self.lock_containment_ratio_min < self.probable_containment_ratio_min:
            raise ValueError(
                "Balance lock containment ratio cannot be below probable ratio"
            )
        if (
            self.failed_escape_rearm_min_bars
            < self.failed_escape_rearm_inside_closes
        ):
            raise ValueError(
                "Balance rearm minimum bars cannot be less than required inside closes"
            )
        if (
            self.max_same_side_escape_attempts
            > self.max_escape_attempts_per_episode
        ):
            raise ValueError(
                "Same-side escape attempt limit cannot exceed total episode limit"
            )
        return self


class StructuralEventRuleConfig(BaseModel):
    """Default setup permission created by one authoritative lifecycle event."""

    model_config = STRICT_CONFIG

    event_type: AuctionEventType
    setup_families: Tuple[SetupFamily, ...]
    result: StructuralPermissionResult

    @model_validator(mode="after")
    def _validate_event_rule(self) -> "StructuralEventRuleConfig":
        _require_unique_nonempty("setup_families", self.setup_families)
        return self


class StructuralStateRuleConfig(BaseModel):
    """Setup permission imposed by current authoritative balance state."""

    model_config = STRICT_CONFIG

    balance_state: BalanceEpisodeState
    setup_families: Tuple[SetupFamily, ...]
    result: StructuralPermissionResult

    @model_validator(mode="after")
    def _validate_state_rule(self) -> "StructuralStateRuleConfig":
        if self.balance_state in {
            BalanceEpisodeState.NONE,
            BalanceEpisodeState.COMPLETED,
        }:
            raise ValueError("Structural state rule requires an active balance state")
        _require_unique_nonempty("setup_families", self.setup_families)
        return self


class StructuralPermissionPolicyConfig(BaseModel):
    """Central event/state-to-setup permission matrix."""

    model_config = STRICT_CONFIG

    result_precedence: Tuple[StructuralPermissionResult, ...] = (
        StructuralPermissionResult.PERMIT,
        StructuralPermissionResult.WAIT,
        StructuralPermissionResult.BLOCK,
    )
    event_rules: Tuple[StructuralEventRuleConfig, ...] = (
        StructuralEventRuleConfig(
            event_type=AuctionEventType.DIRECTIONAL_REVERSAL_CONFIRMED,
            setup_families=(SetupFamily.REVERSAL,),
            result=StructuralPermissionResult.WAIT,
        ),
        StructuralEventRuleConfig(
            event_type=AuctionEventType.DIRECTIONAL_REVERSAL_LEG_ESTABLISHED,
            setup_families=(SetupFamily.REVERSAL,),
            result=StructuralPermissionResult.PERMIT,
        ),
        StructuralEventRuleConfig(
            event_type=AuctionEventType.DIRECTIONAL_TREND_RESTORED,
            setup_families=(SetupFamily.CONTINUATION,),
            result=StructuralPermissionResult.PERMIT,
        ),
        StructuralEventRuleConfig(
            event_type=AuctionEventType.DIRECTIONAL_CONTINUATION_CONFIRMED,
            setup_families=(SetupFamily.CONTINUATION,),
            result=StructuralPermissionResult.PERMIT,
        ),
        StructuralEventRuleConfig(
            event_type=AuctionEventType.DIRECTIONAL_REACCELERATION_CONFIRMED,
            setup_families=(SetupFamily.REACCELERATION,),
            result=StructuralPermissionResult.PERMIT,
        ),
        StructuralEventRuleConfig(
            event_type=AuctionEventType.BALANCE_ESCAPE_STARTED,
            setup_families=(SetupFamily.BREAKOUT_INITIATION,),
            result=StructuralPermissionResult.PERMIT,
        ),
        StructuralEventRuleConfig(
            event_type=AuctionEventType.BALANCE_ESCAPE_ACCEPTED,
            setup_families=(SetupFamily.ACCEPTED_BREAKOUT,),
            result=StructuralPermissionResult.PERMIT,
        ),
        StructuralEventRuleConfig(
            event_type=AuctionEventType.BALANCE_ESCAPE_FAILED,
            setup_families=(SetupFamily.FAILED_BREAKOUT,),
            result=StructuralPermissionResult.PERMIT,
        ),
    )
    state_rules: Tuple[StructuralStateRuleConfig, ...] = (
        StructuralStateRuleConfig(
            balance_state=BalanceEpisodeState.LOCKED,
            setup_families=(
                SetupFamily.CONTINUATION,
                SetupFamily.REACCELERATION,
                SetupFamily.REVERSAL,
            ),
            result=StructuralPermissionResult.BLOCK,
        ),
        StructuralStateRuleConfig(
            balance_state=BalanceEpisodeState.ESCAPE_WATCH,
            setup_families=(
                SetupFamily.CONTINUATION,
                SetupFamily.REACCELERATION,
                SetupFamily.REVERSAL,
            ),
            result=StructuralPermissionResult.BLOCK,
        ),
        StructuralStateRuleConfig(
            balance_state=BalanceEpisodeState.FAILED_BACK_INSIDE,
            setup_families=(
                SetupFamily.CONTINUATION,
                SetupFamily.REACCELERATION,
                SetupFamily.REVERSAL,
            ),
            result=StructuralPermissionResult.BLOCK,
        ),
        StructuralStateRuleConfig(
            balance_state=BalanceEpisodeState.ESCAPE_WATCH,
            setup_families=(
                SetupFamily.ACCEPTED_BREAKOUT,
                SetupFamily.FAILED_BREAKOUT,
            ),
            result=StructuralPermissionResult.WAIT,
        ),
    )

    @model_validator(mode="after")
    def _validate_permission_policy(self) -> "StructuralPermissionPolicyConfig":
        if set(self.result_precedence) != set(StructuralPermissionResult):
            raise ValueError(
                "result_precedence must contain PERMIT, WAIT and BLOCK exactly once"
            )
        if len(self.result_precedence) != len(set(self.result_precedence)):
            raise ValueError("result_precedence cannot contain duplicates")
        event_types = tuple(rule.event_type for rule in self.event_rules)
        if len(event_types) != len(set(event_types)):
            raise ValueError("Only one structural event rule is allowed per event type")
        creation_families = {
            family
            for rule in self.event_rules
            if rule.result is StructuralPermissionResult.PERMIT
            for family in rule.setup_families
        }
        if creation_families != set(SetupFamily):
            missing = sorted(
                family.value for family in set(SetupFamily) - creation_families
            )
            raise ValueError(
                "Every setup family requires at least one authoritative PERMIT event; "
                f"missing={missing}"
            )
        state_family_keys = []
        for rule in self.state_rules:
            state_family_keys.extend(
                (rule.balance_state, family) for family in rule.setup_families
            )
        if len(state_family_keys) != len(set(state_family_keys)):
            raise ValueError(
                "Only one structural state rule is allowed per balance-state/setup-family"
            )
        return self


class AuctionEpisodePolicyConfig(BaseModel):
    """Authoritative persistent directional/balance lifecycle policy."""

    model_config = STRICT_CONFIG

    directional: DirectionalEpisodePolicyConfig = Field(
        default_factory=DirectionalEpisodePolicyConfig
    )
    balance: BalanceEpisodePolicyConfig = Field(default_factory=BalanceEpisodePolicyConfig)
    permissions: StructuralPermissionPolicyConfig = Field(
        default_factory=StructuralPermissionPolicyConfig
    )


class AuctionEngineConfig(BaseModel):
    """Resolved immutable settings read by the current Auction path."""

    model_config = STRICT_CONFIG

    engine: AuctionEngineRuntimeConfig = Field(default_factory=AuctionEngineRuntimeConfig)
    evidence: AuctionEvidenceConfig = Field(default_factory=AuctionEvidenceConfig)
    state: AuctionStatePolicyConfig = Field(default_factory=AuctionStatePolicyConfig)
    boundary: BoundaryPolicyConfig = Field(default_factory=BoundaryPolicyConfig)
    initiation: BreakoutInitiationPolicyConfig = Field(
        default_factory=BreakoutInitiationPolicyConfig
    )
    acceptance: AcceptedOutcomePolicyConfig = Field(
        default_factory=AcceptedOutcomePolicyConfig
    )
    continuation: ContinuationPolicyConfig = Field(
        default_factory=ContinuationPolicyConfig
    )
    reversal: ReversalPolicyConfig = Field(default_factory=ReversalPolicyConfig)
    episode: AuctionEpisodePolicyConfig = Field(default_factory=AuctionEpisodePolicyConfig)

    def resolved_dict(self) -> Dict[str, Any]:
        return self.model_dump(mode="json")


AUCTION_ENGINE_CONFIG = AuctionEngineConfig()


__all__ = [
    "STRICT_CONFIG",
    "AuctionEngineRuntimeConfig",
    "AuctionEvidenceConfig",
    "AuctionStatePolicyConfig",
    "BoundaryPolicyConfig",
    "BreakoutInitiationPolicyConfig",
    "AcceptedOutcomePolicyConfig",
    "ContinuationPolicyConfig",
    "ReversalPolicyConfig",
    "DirectionalEpisodePolicyConfig",
    "BalanceEpisodePolicyConfig",
    "StructuralEventRuleConfig",
    "StructuralStateRuleConfig",
    "StructuralPermissionPolicyConfig",
    "AuctionEpisodePolicyConfig",
    "AuctionEngineConfig",
    "AUCTION_ENGINE_CONFIG",
]
