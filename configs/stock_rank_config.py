"""Diagnostic rolling stock-movement ranking configuration.

StockRank observes completed snapshots and writes a cross-sectional ranking.  It
is deliberately read-only with respect to symbol eligibility, signals,
opportunities and trades.  The first implementation is diagnostic: all scores
and penalties are persisted so multi-day replay can determine whether ranking
should later influence new-signal eligibility.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


STRICT_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    validate_default=True,
)


class StockRankConfig(BaseModel):
    model_config = STRICT_CONFIG

    enabled: bool = True

    # Runtime / report behaviour.
    log_file: str = "/var/www/autotrades/scripts/stock_rank.log"
    report_dir: str = "reports"
    active_symbols_only: bool = True
    minimum_snapshot_coverage_ratio: float = Field(default=0.90, gt=0.0, le=1.0)
    history_bars: int = Field(default=40, ge=12, le=200)
    recent_efficiency_bars: int = Field(default=12, ge=5, le=60)
    top_log_count: int = Field(default=20, ge=1, le=100)
    range_log_count: int = Field(default=15, ge=1, le=100)

    # Movement normalization.  Each raw component is clipped to 0..1 before
    # weighting; these are not trade thresholds.
    move_15m_norm_pct: float = Field(default=0.50, gt=0.0)
    move_30m_norm_pct: float = Field(default=0.80, gt=0.0)
    move_60m_norm_pct: float = Field(default=1.25, gt=0.0)
    move_15m_norm_atr: float = Field(default=0.60, gt=0.0)
    move_30m_norm_atr: float = Field(default=0.90, gt=0.0)
    move_60m_norm_atr: float = Field(default=1.40, gt=0.0)
    session_move_norm_pct: float = Field(default=2.00, gt=0.0)
    bar_rvol_norm: float = Field(default=2.00, gt=0.0)
    freshness_bars_norm: int = Field(default=8, ge=1)

    # Movement score weights.  Opening/session displacement is deliberately a
    # small component so an old gap cannot dominate current movement quality.
    weight_recent_pct: float = Field(default=0.22, ge=0.0)
    weight_recent_atr: float = Field(default=0.18, ge=0.0)
    weight_efficiency: float = Field(default=0.18, ge=0.0)
    weight_volume: float = Field(default=0.08, ge=0.0)
    weight_freshness: float = Field(default=0.10, ge=0.0)
    weight_direction_consistency: float = Field(default=0.10, ge=0.0)
    weight_acceleration: float = Field(default=0.06, ge=0.0)
    weight_session_move: float = Field(default=0.08, ge=0.0)

    # Final score composition.
    total_movement_weight: float = Field(default=0.70, ge=0.0, le=1.0)
    total_quality_weight: float = Field(default=0.30, ge=0.0, le=1.0)
    range_penalty_weight: float = Field(default=0.50, ge=0.0, le=1.0)
    stall_penalty_weight: float = Field(default=0.25, ge=0.0, le=1.0)

    # Classification only.  StockRank never enables/disables or blocks trades.
    moving_score_threshold: float = Field(default=35.0, ge=0.0, le=100.0)
    developing_score_threshold: float = Field(default=20.0, ge=0.0, le=100.0)
    range_bound_penalty_threshold: float = Field(default=65.0, ge=0.0, le=100.0)
    stalled_gap_min_abs_pct: float = Field(default=1.00, ge=0.0)
    stalled_gap_max_recent_30m_pct: float = Field(default=0.25, ge=0.0)
    stalled_gap_min_range_penalty: float = Field(default=40.0, ge=0.0, le=100.0)

    @model_validator(mode="after")
    def validate_weights(self) -> "StockRankConfig":
        movement_weight_sum = sum(
            (
                self.weight_recent_pct,
                self.weight_recent_atr,
                self.weight_efficiency,
                self.weight_volume,
                self.weight_freshness,
                self.weight_direction_consistency,
                self.weight_acceleration,
                self.weight_session_move,
            )
        )
        if abs(movement_weight_sum - 1.0) > 1e-9:
            raise ValueError("StockRank movement weights must sum to 1.0")
        if abs(self.total_movement_weight + self.total_quality_weight - 1.0) > 1e-9:
            raise ValueError("StockRank total movement/quality weights must sum to 1.0")
        if self.developing_score_threshold > self.moving_score_threshold:
            raise ValueError("developing_score_threshold cannot exceed moving_score_threshold")
        return self


STOCK_RANK_CONFIG = StockRankConfig()


__all__ = ["StockRankConfig", "STOCK_RANK_CONFIG"]
