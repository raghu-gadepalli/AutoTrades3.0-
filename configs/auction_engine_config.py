"""Strict configuration for the snapshot-integrated Auction Engine.

Auction owns local market interpretation and snapshot-carried continuity.
SignalGenerator owns signal persistence and lifecycle; Auction performs no
database persistence and reads no active signal or trade state.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Literal, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from enums.auction_engine import (
    AuctionEventType,
    AuctionStateName,
    BalanceEpisodeState,
    DirectionObservationSource,
    DirectionalEfficiencySource,
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
    """Top-level runtime and chronology controls."""

    model_config = STRICT_CONFIG

    engine_name: str = Field(default="AUTOTRADES_AUCTION_ENGINE", min_length=1)
    engine_version: str = Field(
        default="1.1.0",
        min_length=1,
        pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$",
    )
    config_version: str = Field(
        default="AUCTION_AUTHORITY_REFACTOR_V3A",
        min_length=1,
        pattern=r"^[A-Z0-9_]+$",
    )

    timezone: str = Field(default="Asia/Kolkata", min_length=1)
    snapshot_interval_minutes: float = Field(default=3.0, gt=0.0)
    earliest_evaluation_time: str = "09:15:00"
    earliest_create_time: str = "09:30:00"
    latest_create_time: str = "15:00:00"
    supported_symbol_types: Tuple[str, ...] = ("EQ",)
    default_replay_excluded_symbols: Tuple[str, ...] = ("NIFTY 50", "NIFTY BANK")
    development_database_name: str = Field(default="backtest", min_length=1)
    protected_database_names: Tuple[str, ...] = ("autotrades",)

    @field_validator(
        "earliest_evaluation_time",
        "earliest_create_time",
        "latest_create_time",
    )
    @classmethod
    def _validate_clock_text(cls, value: str) -> str:
        parts = value.split(":")
        if len(parts) != 3:
            raise ValueError("Time values must use HH:MM:SS")
        try:
            hh, mm, ss = (int(x) for x in parts)
        except Exception as exc:  # pragma: no cover - defensive
            raise ValueError("Time values must use HH:MM:SS") from exc
        if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
            raise ValueError("Invalid clock time")
        return f"{hh:02d}:{mm:02d}:{ss:02d}"

    @field_validator("supported_symbol_types")
    @classmethod
    def _validate_symbol_types(cls, value: Tuple[str, ...]) -> Tuple[str, ...]:
        cleaned = tuple(item.strip().upper() for item in value)
        _require_unique_nonempty("supported_symbol_types", cleaned)
        if any(not item for item in cleaned):
            raise ValueError("supported_symbol_types cannot contain blank values")
        return cleaned

    @field_validator("default_replay_excluded_symbols")
    @classmethod
    def _validate_excluded_symbols(cls, value: Tuple[str, ...]) -> Tuple[str, ...]:
        cleaned = tuple(item.strip().upper() for item in value)
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("default_replay_excluded_symbols cannot contain duplicates")
        if any(not item for item in cleaned):
            raise ValueError("default_replay_excluded_symbols cannot contain blanks")
        return cleaned

    @field_validator("development_database_name")
    @classmethod
    def _validate_development_database_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("development_database_name cannot be blank")
        return cleaned

    @field_validator("protected_database_names")
    @classmethod
    def _validate_protected_database_names(cls, value: Tuple[str, ...]) -> Tuple[str, ...]:
        cleaned = tuple(item.strip() for item in value)
        _require_unique_nonempty("protected_database_names", cleaned)
        if any(not item for item in cleaned):
            raise ValueError("protected_database_names cannot contain blanks")
        return cleaned

    @model_validator(mode="after")
    def _validate_time_order(self) -> "AuctionEngineRuntimeConfig":
        if not (
            self.earliest_evaluation_time
            <= self.earliest_create_time
            <= self.latest_create_time
        ):
            raise ValueError(
                "Expected earliest_evaluation_time <= earliest_create_time "
                "<= latest_create_time"
            )
        return self


class AuctionEvidenceConfig(BaseModel):
    """Common Evidence Ledger ownership and causality rules."""

    model_config = STRICT_CONFIG

    enabled: bool = True
    causal_only: bool = True
    reject_future_timestamps: bool = True
    missing_data_is_unknown: bool = True

    # Neutral snapshot facts can be reused; existing setup conclusions cannot be
    # accepted as proof by the new engine.
    reuse_neutral_snapshot_facts: bool = True
    consume_existing_setup_conclusions: bool = False
    retain_source_paths: bool = True
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
    objective_domains: Tuple[str, ...] = (
        "price_action",
        "boundary",
        "trend",
        "compression",
        "extension",
        "opportunity",
        "market",
        "derivatives",
        "data_quality",
    )

    minimum_history_bars: int = Field(default=5, ge=1)
    floating_point_tolerance: float = Field(default=1e-9, gt=0.0)
    derivatives_preferred_windows: Tuple[str, ...] = ("15m", "5m", "60m", "sod")

    # Phase-2 report defaults. These are evidence-description thresholds, not
    # setup-entry thresholds. They remain versioned and observation-only.
    strong_bar_move_atr: float = Field(default=0.50, gt=0.0)
    strong_bar_body_fraction: float = Field(default=0.55, ge=0.0, le=1.0)
    directional_close_position: float = Field(default=0.70, ge=0.50, le=1.0)
    compression_range_width_atr_max: float = Field(default=2.00, gt=0.0)
    compression_hma_spread_atr_max: float = Field(default=0.35, gt=0.0)
    extension_move_from_anchor_atr: float = Field(default=2.00, gt=0.0)
    extension_vwap_distance_atr: float = Field(default=1.50, gt=0.0)
    extension_rsi_high: float = Field(default=75.0, ge=50.0, le=100.0)
    extension_rsi_low: float = Field(default=25.0, ge=0.0, le=50.0)
    extension_bollinger_high: float = 1.00
    extension_bollinger_low: float = 0.00
    maturity_components_required: int = Field(default=2, ge=1)

    # Phase-2.1 chronology-first evidence corrections. Rolling metrics are
    # authoritative once enough causal bars exist; snapshot structure metrics
    # remain an early-history fallback and a diagnostic only.
    rolling_efficiency_bars: int = Field(default=8, ge=4)
    rolling_overlap_bars: int = Field(default=6, ge=3)
    ema_slow_context_lookback_bars: int = Field(default=5, ge=2)
    atr_context_lookback_bars: int = Field(default=5, ge=2)
    compression_recent_bars: int = Field(default=5, ge=3)
    compression_reference_bars: int = Field(default=12, ge=5)
    compression_contraction_ratio_max: float = Field(default=0.80, gt=0.0, le=1.0)
    compression_hma_contraction_ratio_max: float = Field(default=0.80, gt=0.0, le=1.0)
    extension_min_history_bars_for_maturity: int = Field(default=8, ge=2)
    extension_progress_decay_min: float = Field(default=0.35, ge=0.0, le=1.0)
    extension_maturity_requires_directional_distance: bool = True
    extension_maturity_requires_progress_or_rejection: bool = True

    # Phase-2.2: compression is an episode candidate only when the local range
    # is quiet as well as contained.  State persistence performs the final
    # multi-bar confirmation and freezes the box.
    compression_max_bar_move_atr: float = Field(default=0.40, gt=0.0)
    compression_require_low_efficiency_and_overlap: bool = True


class AuctionStatePolicyConfig(BaseModel):
    """Auction-state lifecycle policy without setup-specific thresholds."""

    model_config = STRICT_CONFIG

    enabled: bool = True
    persistence_namespace: str = "AUCTION_STATE"
    persist_transition_history: bool = True
    require_explicit_transition_reasons: bool = True
    require_support_and_contradiction_channels: bool = True
    allow_single_opaque_confidence_score: bool = False

    minimum_state_hold_bars: int = Field(default=2, ge=1)
    state_expiry_minutes: float = Field(default=30.5, gt=0.0)
    strict_chronology: bool = True
    history_bars: int = Field(default=12, ge=3)

    # Phase-2.2 persistent auction episodes and transition hysteresis.
    initial_state_confirmation_bars: int = Field(default=2, ge=1)
    ordinary_transition_confirmation_bars: int = Field(default=2, ge=1)
    trend_establishment_bars: int = Field(default=2, ge=1)
    trend_neutralisation_confirmation_bars: int = Field(default=5, ge=2)
    compression_confirmation_bars: int = Field(default=3, ge=1)
    pullback_confirmation_bars: int = Field(default=2, ge=1)
    recompression_confirmation_bars: int = Field(default=3, ge=1)
    chaos_confirmation_bars: int = Field(default=2, ge=1)
    trend_failure_confirmation_bars: int = Field(default=2, ge=1)
    failure_level_confirmation_bars: int = Field(default=2, ge=1)
    failure_structure_confirmation_bars: int = Field(default=2, ge=1)
    # Retained-structure loss is local weakening evidence.  It may confirm a
    # persistent trend failure only when price is near the frozen structural
    # protection level and adverse directional context remains current.
    failure_structure_proximity_atr: float = Field(default=0.50, ge=0.0)
    failure_structure_requires_directional_corroboration: bool = True
    failure_structure_requires_value_migration_corroboration: bool = False
    failure_watch_max_bars: int = Field(default=6, ge=2)
    trend_failure_max_bars: int = Field(default=12, ge=3)
    reversal_confirmation_bars: int = Field(default=2, ge=1)
    trend_recovery_confirmation_bars: int = Field(default=2, ge=1)

    fresh_expansion_min_hold_bars: int = Field(default=2, ge=1)
    reacceleration_min_hold_bars: int = Field(default=2, ge=1)
    mature_extension_min_hold_bars: int = Field(default=2, ge=1)
    trend_failure_min_hold_bars: int = Field(default=2, ge=1)
    reversal_min_hold_bars: int = Field(default=3, ge=1)
    chaotic_min_hold_bars: int = Field(default=2, ge=1)

    pullback_max_bars: int = Field(default=10, ge=2)
    recompression_max_bars: int = Field(default=15, ge=3)
    event_state_max_bars: int = Field(default=5, ge=2)

    current_leg_extension_atr: float = Field(default=1.50, gt=0.0)
    current_leg_current_extension_atr: float = Field(default=1.00, gt=0.0)
    current_leg_min_bars_for_maturity: int = Field(default=4, ge=2)
    current_leg_no_progress_bars: int = Field(default=2, ge=1)
    current_leg_progress_tolerance_atr: float = Field(default=0.05, ge=0.0)
    current_leg_max_retracement_atr: float = Field(default=0.75, ge=0.0)
    current_leg_max_retracement_fraction: float = Field(default=0.40, ge=0.0, le=1.0)
    current_leg_reanchor_progress_atr: float = Field(default=0.75, gt=0.0)

    # Deterministic Phase-2 state-report thresholds. These classify the local
    # auction only; they do not create setup candidates or signals.
    boundary_interaction_distance_atr: float = Field(default=0.25, gt=0.0)
    fresh_expansion_outside_atr: float = Field(default=0.15, ge=0.0)
    orderly_trend_efficiency_min: float = Field(default=0.45, ge=0.0, le=1.0)
    balance_efficiency_max: float = Field(default=0.35, ge=0.0, le=1.0)
    balance_overlap_min: float = Field(default=0.55, ge=0.0, le=1.0)
    controlled_pullback_max_adverse_atr: float = Field(default=0.75, gt=0.0)
    reacceleration_displacement_atr: float = Field(default=0.35, gt=0.0)
    trend_failure_opposite_displacement_atr: float = Field(default=0.50, gt=0.0)
    failure_level_breach_atr: float = Field(default=0.10, ge=0.0)
    trend_protection_min_improvement_atr: float = Field(default=0.05, ge=0.0)
    chaotic_flip_count_min: int = Field(default=3, ge=1)
    chaotic_efficiency_max: float = Field(default=0.35, ge=0.0, le=1.0)
    chaotic_independent_channels_min: int = Field(default=2, ge=1, le=3)
    chaotic_bar_direction_flips_min: int = Field(default=4, ge=1)
    use_cumulative_day_flip_counts_for_state: bool = False

    # Slow context is descriptive, never a standalone signal trigger.  EMA100
    # and EMA200 are deliberately lagging; their compression/expansion is used
    # only with price containment, efficiency and HMA context.
    slow_ema_close_spread_atr_max: float = Field(default=0.75, ge=0.0)
    slow_ema_flat_slope_atr_per_bar_max: float = Field(default=0.04, ge=0.0)
    slow_ema_spread_expansion_atr_per_bar_min: float = Field(default=0.01, ge=0.0)
    atr_contraction_ratio_max: float = Field(default=0.90, gt=0.0)
    atr_expansion_ratio_min: float = Field(default=1.10, gt=0.0)
    stock_context_directional_efficiency_min: float = Field(default=0.45, ge=0.0, le=1.0)
    stock_context_balance_confirmation_bars: int = Field(default=2, ge=1)
    stock_context_early_expansion_displacement_atr: float = Field(default=0.35, ge=0.0)
    accepted_range_tolerance_atr: float = Field(default=0.15, ge=0.0)

    # Exhaustion is persistent market context independent of whether an
    # EXHAUSTION_REVERSAL candidate has enough VWAP room to become a trade.
    exhaustion_context_enabled: bool = True
    exhaustion_context_min_extension_atr: float = Field(default=1.50, ge=0.0)
    exhaustion_context_min_leg_age_bars: int = Field(default=3, ge=1)
    exhaustion_context_progress_decay_min: float = Field(default=0.35, ge=0.0, le=1.0)
    exhaustion_context_max_bars: int = Field(default=6, ge=1)
    exhaustion_context_clear_confirmation_bars: int = Field(default=2, ge=1)

    chaotic_rotation_blocks_create: bool = True
    mature_extension_blocks_late_breakout: bool = True
    unknown_state_blocks_create: bool = True

    state_names: Tuple[str, ...] = (
        "UNKNOWN",
        "BALANCE",
        "COMPRESSION",
        "BOUNDARY_INTERACTION",
        "FRESH_EXPANSION",
        "ORDERLY_UPTREND",
        "ORDERLY_DOWNTREND",
        "CONTROLLED_PULLBACK",
        "RECOMPRESSION",
        "REACCELERATION",
        "MATURE_EXTENSION",
        "TREND_FAILURE",
        "REVERSAL",
        "CHAOTIC_ROTATION",
    )


class BoundaryPolicyConfig(BaseModel):
    """Unified boundary selection and immutable episode lifecycle."""

    model_config = STRICT_CONFIG

    enabled: bool = True
    persistence_namespace: str = "BOUNDARY_RESOLUTION"

    # The report prototypes already established dynamic intraday balance as the
    # initial authoritative boundary universe.  Fixed levels can be added later
    # through reviewed selector policy.
    dynamic_boundaries_only: bool = True
    # Baseline: only a non-provisional, breakout-eligible accepted range may
    # originate accepted/failed breakout episodes. Candidate/raw fallbacks and
    # the opening-range seed remain observable but non-tradable.
    require_accepted_range: bool = True
    require_breakout_eligible_accepted_range: bool = True
    allow_candidate_range_fallback: bool = False
    allow_raw_range_fallback: bool = False
    allow_orb_seeded_accepted_range: bool = False
    immutable_event_identity: bool = True
    freeze_range_at_attempt: bool = True
    share_episode_between_accepted_and_failed: bool = True

    terminal_event_reactivation_allowed: bool = False
    consumed_event_reactivation_allowed: bool = False
    newer_range_supersedes_older_episode: bool = True

    # Phase-3A observation-only boundary progression thresholds.  These
    # classify episode state only; they do not make setup candidates CREATE-capable.
    allowed_dynamic_sources: Tuple[str, ...] = (
        "INTRADAY_BALANCE",
        "DYNAMIC",
        "MICRO_COMPRESSION",
    )
    approach_distance_atr: float = Field(default=0.35, gt=0.0)
    attempt_excursion_atr: float = Field(default=0.10, ge=0.0)
    strong_wick_excursion_atr: float = Field(default=0.25, ge=0.0)
    acceptance_close_buffer_atr: float = Field(default=0.10, ge=0.0)
    acceptance_required_outside_closes: int = Field(default=2, ge=1)
    acceptance_retest_tolerance_atr: float = Field(default=0.10, ge=0.0)
    failure_reentry_depth_atr: float = Field(default=0.10, ge=0.0)
    failure_required_inside_closes: int = Field(default=2, ge=1)
    failure_followthrough_atr: float = Field(default=0.10, ge=0.0)
    failure_deep_reentry_multiplier: float = Field(default=2.0, ge=1.0)
    range_missing_stale_bars: int = Field(default=4, ge=1)
    terminal_reset_depth_atr: float = Field(default=0.10, ge=0.0)

    episode_idle_expiry_minutes: float = Field(default=30.0, gt=0.0)
    failure_watch_valid_minutes: float = Field(default=15.5, gt=0.0)
    reset_required_inside_closes: int = Field(default=2, ge=1)

    statuses: Tuple[str, ...] = (
        "APPROACHING",
        "OUTSIDE_ATTEMPT",
        "UNRESOLVED",
        "ACCEPTANCE_BUILDING",
        "ACCEPTED",
        "FAILURE_BUILDING",
        "FAILED",
        "EXPIRED",
        "SUPERSEDED",
        "STALE",
    )


class BreakoutInitiationPolicyConfig(BaseModel):
    """Early fresh-expansion interpretation.

    Exact numeric trigger thresholds remain deferred until evidence/state reports
    are implemented.  This section fixes only the architecture and lifecycle
    choices already agreed in the approach document.
    """

    model_config = STRICT_CONFIG

    enabled: bool = True
    observation_only: bool = True
    create_enabled: bool = False

    require_recent_valid_balance: bool = True
    require_strong_displacement: bool = True
    require_immediate_hold_or_shallow_retest: bool = True
    require_structural_room: bool = True
    require_session_time_remaining: bool = True
    require_fresh_unconsumed_first_move: bool = True

    delayed_two_close_observation_enabled: bool = True
    delayed_two_close_create_enabled: bool = False
    block_after_structural_acceptance: bool = True
    block_after_failed_first_opportunity: bool = True

    # Phase-3B.2 local-candidate gates. Breakout reward is open-ended: a
    # measured move is a diagnostic reference, not a hard target.  Freshness is
    # controlled by distance from the broken boundary and auction maturity.
    confirmation_window_bars: int = Field(default=2, ge=1)
    minimum_displacement_atr: float = Field(default=0.25, ge=0.0)
    minimum_close_outside_atr: float = Field(default=0.10, ge=0.0)
    shallow_retest_tolerance_atr: float = Field(default=0.15, ge=0.0)
    max_entry_distance_atr: float = Field(default=0.75, gt=0.0)
    minimum_session_minutes: float = Field(default=30.0, ge=0.0)
    assumed_target_room_gate_enabled: bool = False
    measured_move_reference_enabled: bool = True
    actual_barrier_diagnostics_enabled: bool = True


class AcceptedOutcomePolicyConfig(BaseModel):
    """Accepted resolution of a unified boundary episode."""

    model_config = STRICT_CONFIG

    enabled: bool = True
    observation_only: bool = True
    create_enabled: bool = False

    require_sustained_outside_trade: bool = True
    require_hold_or_retest: bool = True
    require_value_beyond_boundary: bool = True
    # Accepted breakouts do not have a knowable terminal target.  External
    # levels and measured moves are diagnostics; eligibility is based on
    # acceptance quality, freshness, auction maturity and the session window.
    require_external_room: bool = False
    block_consumed_first_leg: bool = False
    invalidate_on_reabsorption: bool = True

    max_entry_distance_atr: float = Field(default=0.75, gt=0.0)
    minimum_session_minutes: float = Field(default=30.0, ge=0.0)
    assumed_target_room_gate_enabled: bool = False
    measured_move_reference_enabled: bool = True
    actual_barrier_diagnostics_enabled: bool = True


class FailedOutcomePolicyConfig(BaseModel):
    """Failed resolution of a unified boundary episode."""

    model_config = STRICT_CONFIG

    enabled: bool = True
    observation_only: bool = True
    create_enabled: bool = False

    require_genuine_outside_attempt: bool = True
    require_meaningful_reentry: bool = True
    require_inside_hold: bool = True
    require_directional_followthrough: bool = True
    require_room_toward_value: bool = True
    invalidate_on_renewed_acceptance: bool = True

    classify_context_subtype: bool = True
    supported_subtypes: Tuple[str, ...] = (
        "TREND_ALIGNED_FAILED_AUCTION",
        "NEUTRAL_RANGE_FAILED_AUCTION",
        "COUNTERTREND_FAILED_AUCTION",
    )

    post_resolution_followthrough_atr: float = Field(default=0.10, ge=0.0)
    # Failed-auction room is measured only to the opposite frozen-range edge.
    # Midpoint and VWAP remain intermediate diagnostics, not eligibility targets.
    room_target_mode: Literal["OPPOSITE_FROZEN_RANGE_EDGE"] = "OPPOSITE_FROZEN_RANGE_EDGE"
    midpoint_vwap_diagnostics_enabled: bool = True
    minimum_room_atr: float = Field(default=0.50, ge=0.0)
    minimum_room_pct: float = Field(default=0.005, ge=0.0)


class ContinuationPolicyConfig(BaseModel):
    """Trend continuation and reacceleration observation policy."""

    model_config = STRICT_CONFIG

    enabled: bool = True
    observation_only: bool = True
    create_enabled: bool = False
    persistence_namespace: str = "REACCELERATION_OBSERVER"

    require_established_orderly_trend: bool = True
    require_controlled_pullback_or_recompression: bool = True
    require_retained_trend_structure: bool = True
    require_fresh_price_action_displacement: bool = True
    require_structural_room: bool = True
    hma_may_support_but_not_trigger: bool = True

    boundary_continuation_enabled: bool = True
    non_boundary_reacceleration_create_enabled: bool = False

    max_entry_distance_atr: float = Field(default=0.75, gt=0.0)
    minimum_session_minutes: float = Field(default=30.0, ge=0.0)
    assumed_target_room_gate_enabled: bool = False
    measured_move_reference_enabled: bool = True
    actual_barrier_diagnostics_enabled: bool = True


class ReversalPolicyConfig(BaseModel):
    """Confirmed reversal setup with normal/exhaustion subtype classification.

    A reversal candidate is created only after the persistent Auction State
    Engine has moved from TREND_FAILURE to REVERSAL.  The two interpretations
    intentionally have different economics:

    * EXHAUSTION_REVERSAL is initially a mean-reversion deployment whose first
      target is VWAP and therefore requires adequate room to VWAP.
    * NORMAL_REVERSAL is a structural transfer of control.  It is open-ended,
      like an accepted breakout, and carries no assumed-target room gate.

    An exhaustion event may later mature into a normal structural reversal
    when the opposite side establishes directional control.
    """

    model_config = STRICT_CONFIG

    enabled: bool = True
    observation_only: bool = False
    create_enabled: bool = True
    persistence_namespace: str = "REVERSAL"

    normal_reversal_enabled: bool = True
    exhaustion_reversal_enabled: bool = True
    require_confirmed_reversal_state: bool = True
    require_structural_trend_failure: bool = True
    require_failure_level: bool = True
    exhaustion_require_vwap_room: bool = True
    normal_reversal_open_ended: bool = True
    promote_exhaustion_to_normal_on_structural_control: bool = True
    indicators_may_support_but_not_trigger: bool = True

    minimum_room_atr: float = Field(default=0.75, ge=0.0)
    minimum_room_pct: float = Field(default=0.005, ge=0.0)
    max_entry_distance_from_failure_level_atr: float = Field(default=2.50, gt=0.0)
    watch_valid_minutes: float = Field(default=15.5, gt=0.0)
    minimum_session_minutes: float = Field(default=30.0, ge=0.0)
    exhaustion_extension_atr_min: float = Field(default=1.50, ge=0.0)
    exhaustion_progress_decay_min: float = Field(default=0.35, ge=0.0, le=1.0)


class AuctionDecisionPolicyConfig(BaseModel):
    """Opportunity Router, Setup Manager and final deterministic action policy."""

    model_config = STRICT_CONFIG

    enabled: bool = True
    create_enabled: bool = False

    require_router_eligibility: bool = True
    combine_same_direction_support: bool = True
    rank_opposition_by_freshness_and_structure: bool = True
    stale_candidates_must_not_influence_decision: bool = True
    terminal_candidates_must_not_influence_decision: bool = True

    no_candidate_action: Literal["HOLD", "NO_ACTION"] = "HOLD"
    ineligible_state_action: Literal["BLOCK"] = "BLOCK"
    unresolved_material_opposition_action: Literal["DEFER"] = "DEFER"

    hide_reasons_in_opaque_score: bool = False

    # Phase-4A cross-opportunity arbitration.  These do not redefine setup
    # eligibility; they only control reconciliation of already-valid records.
    rotation_lookback_minutes: float = Field(default=90.0, gt=0.0)
    rotation_side_switches_to_defer: int = Field(default=2, ge=1)
    unresolved_watch_opposition_enabled: bool = True



class AuctionDiagnosticsConfig(BaseModel):
    """Report-first diagnostics and deterministic comparison controls."""

    model_config = STRICT_CONFIG

    enabled: bool = True
    report_only: bool = True
    include_run_manifest: bool = True
    include_evidence_facts: bool = True
    include_state_transitions: bool = True
    include_boundary_progression: bool = True
    include_all_candidates: bool = True
    include_ineligible_candidates: bool = True
    include_manager_opposition: bool = True

    outcome_horizons_bars: Tuple[int, ...] = (3, 6, 9)
    include_full_session_outcomes: bool = True
    include_eod_outcomes: bool = True
    include_trade_capture_later: bool = True

    @field_validator("outcome_horizons_bars")
    @classmethod
    def _validate_horizons(cls, value: Tuple[int, ...]) -> Tuple[int, ...]:
        if not value:
            raise ValueError("At least one outcome horizon is required")
        cleaned = tuple(int(x) for x in value)
        if any(x <= 0 for x in cleaned):
            raise ValueError("Outcome horizons must be positive")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("Outcome horizons must be unique")
        return tuple(sorted(cleaned))


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

    require_non_provisional_source_range: bool = True
    require_breakout_eligible_source_range: bool = True
    directional_efficiency_source_precedence: Tuple[
        DirectionalEfficiencySource, ...
    ] = (
        DirectionalEfficiencySource.PRICE_ACTION,
        DirectionalEfficiencySource.TREND,
    )

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
        _require_unique_nonempty(
            "directional_efficiency_source_precedence",
            self.directional_efficiency_source_precedence,
        )
        if DirectionalEfficiencySource.NONE in self.directional_efficiency_source_precedence:
            raise ValueError(
                "directional_efficiency_source_precedence cannot contain NONE"
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
    """Resolved, immutable configuration tree for the new engine."""

    model_config = STRICT_CONFIG

    engine: AuctionEngineRuntimeConfig = Field(default_factory=AuctionEngineRuntimeConfig)
    evidence: AuctionEvidenceConfig = Field(default_factory=AuctionEvidenceConfig)
    state: AuctionStatePolicyConfig = Field(default_factory=AuctionStatePolicyConfig)
    boundary: BoundaryPolicyConfig = Field(default_factory=BoundaryPolicyConfig)
    initiation: BreakoutInitiationPolicyConfig = Field(default_factory=BreakoutInitiationPolicyConfig)
    acceptance: AcceptedOutcomePolicyConfig = Field(default_factory=AcceptedOutcomePolicyConfig)
    failure: FailedOutcomePolicyConfig = Field(default_factory=FailedOutcomePolicyConfig)
    continuation: ContinuationPolicyConfig = Field(default_factory=ContinuationPolicyConfig)
    reversal: ReversalPolicyConfig = Field(default_factory=ReversalPolicyConfig)
    decision: AuctionDecisionPolicyConfig = Field(default_factory=AuctionDecisionPolicyConfig)
    diagnostics: AuctionDiagnosticsConfig = Field(default_factory=AuctionDiagnosticsConfig)
    episode: AuctionEpisodePolicyConfig = Field(default_factory=AuctionEpisodePolicyConfig)

    def resolved_dict(self) -> Dict[str, Any]:
        """Return the JSON-safe resolved configuration used by a replay run."""

        return self.model_dump(mode="json")

    def stable_hash(self) -> str:
        """Return a deterministic SHA-256 hash of the resolved configuration."""

        payload = json.dumps(
            self.resolved_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


AUCTION_ENGINE_CONFIG = AuctionEngineConfig()


__all__ = [
    "STRICT_CONFIG",
    "AuctionEngineRuntimeConfig",
    "AuctionEvidenceConfig",
    "AuctionStatePolicyConfig",
    "BoundaryPolicyConfig",
    "BreakoutInitiationPolicyConfig",
    "AcceptedOutcomePolicyConfig",
    "FailedOutcomePolicyConfig",
    "ContinuationPolicyConfig",
    "ReversalPolicyConfig",
    "AuctionDecisionPolicyConfig",
    "AuctionDiagnosticsConfig",
    "DirectionalEpisodePolicyConfig",
    "BalanceEpisodePolicyConfig",
    "StructuralEventRuleConfig",
    "StructuralStateRuleConfig",
    "StructuralPermissionPolicyConfig",
    "AuctionEpisodePolicyConfig",
    "AuctionEngineConfig",
    "AUCTION_ENGINE_CONFIG",
]
