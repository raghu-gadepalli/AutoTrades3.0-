"""Strict configuration for replay-only Auction policy experiments.

This module is intentionally separate from production Auction configuration.
Changing these values affects only the diagnostic replay program under tests/.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator


STRICT_EXPERIMENT_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    validate_default=True,
)


class ExperimentRunConfig(BaseModel):
    model_config = STRICT_EXPERIMENT_CONFIG

    trading_day: date = date(2026, 7, 27)
    symbols: Tuple[str, ...] = ()
    replay_userid: str = "DR1812"
    batch_size: int = Field(default=500, ge=1)
    report_dir: str = "reports"
    report_prefix: str = "exhaustion_priority_experiment"


class GapContextConfig(BaseModel):
    model_config = STRICT_EXPERIMENT_CONFIG

    large_gap_threshold_pct: float = Field(default=1.0, ge=0.0)


class RangeAbstentionConfig(BaseModel):
    model_config = STRICT_EXPERIMENT_CONFIG

    enabled: bool = True
    require_breakout_eligible_range: bool = True
    require_non_provisional_range: bool = True


class ExhaustionConfirmationConfig(BaseModel):
    model_config = STRICT_EXPERIMENT_CONFIG

    enabled: bool = True
    normal_displacement_atr: float = Field(default=0.50, gt=0.0)
    large_gap_displacement_atr: float = Field(default=0.35, gt=0.0)
    strong_counter_bar_body_atr: float = Field(default=0.35, gt=0.0)
    extreme_tolerance_atr: float = Field(default=0.05, ge=0.0)
    require_initial_vwap_room_for_confirmation: bool = False
    minimum_initial_vwap_room_atr: float = Field(default=0.75, ge=0.0)
    minimum_initial_vwap_room_pct: float = Field(default=0.005, ge=0.0)
    episode_expiry_minutes: float = Field(default=90.0, gt=0.0)
    reversal_invalidation_atr: float = Field(default=0.00, ge=0.0)
    continuation_acceptance_atr: float = Field(default=0.10, ge=0.0)
    reversal_failure_closes: int = Field(default=2, ge=1)


class OutcomeConfig(BaseModel):
    model_config = STRICT_EXPERIMENT_CONFIG

    horizon_bars: Tuple[int, ...] = (3, 6, 9, 20)

    @model_validator(mode="after")
    def validate_horizons(self) -> "OutcomeConfig":
        if not self.horizon_bars:
            raise ValueError("At least one outcome horizon is required")
        if len(set(self.horizon_bars)) != len(self.horizon_bars):
            raise ValueError("Outcome horizons must be unique")
        if any(value <= 0 for value in self.horizon_bars):
            raise ValueError("Outcome horizons must be positive")
        return self


class AuctionPolicyExperimentConfig(BaseModel):
    model_config = STRICT_EXPERIMENT_CONFIG

    mode: Literal["REPORT_ONLY"] = "REPORT_ONLY"
    run: ExperimentRunConfig = Field(default_factory=ExperimentRunConfig)
    gap: GapContextConfig = Field(default_factory=GapContextConfig)
    range_abstention: RangeAbstentionConfig = Field(
        default_factory=RangeAbstentionConfig
    )
    exhaustion: ExhaustionConfirmationConfig = Field(
        default_factory=ExhaustionConfirmationConfig
    )
    outcomes: OutcomeConfig = Field(default_factory=OutcomeConfig)


AUCTION_POLICY_EXPERIMENT_CONFIG = AuctionPolicyExperimentConfig()


__all__ = [
    "STRICT_EXPERIMENT_CONFIG",
    "ExperimentRunConfig",
    "GapContextConfig",
    "RangeAbstentionConfig",
    "ExhaustionConfirmationConfig",
    "OutcomeConfig",
    "AuctionPolicyExperimentConfig",
    "AUCTION_POLICY_EXPERIMENT_CONFIG",
]
