from __future__ import annotations

from typing import Dict, List, Tuple

from pydantic import BaseModel, Field


class StockMapServiceConfig(BaseModel):
    """Runtime defaults. CLI arguments may override these values."""

    window_start: str = "09:30:05"
    # EQ closes at 15:15. Keep the runner alive briefly so the final
    # 15:00-15:15 candle can be generated after completion.
    window_end: str = "15:16:00"
    retry_interval_seconds: int = 15
    log_file: str = "/var/www/autotrades/scripts/stockmaps.log"
    max_workers: int = 4
    tick_minutes: int = 15
    cadence_delay_seconds: int = 5


class StockMapIndicatorConfig(BaseModel):
    frequency: str = "15minute"
    bootstrap_calendar_days: int = 60
    continuation_calendar_days: int = 14

    ema_lengths: Dict[str, int] = Field(
        default_factory=lambda: {
            "ema100": 100,
            "ema200": 200,
        }
    )
    atr_period: int = 14
    ema_slope_bars: int = 4

    # Kept because stockmap_helper intentionally mirrors snapshot_helper.
    volume_period: int = 20
    volume_slope_period: int = 20
    rsi_period: int = 14
    adx_period: int = 14
    bb_period: int = 20
    bb_std_mult: float = 2.0
    hma_lengths: Dict[str, int] = Field(
        default_factory=lambda: {
            "hmafast": 15,
            "hmamid1": 60,
            "hmamid2": 120,
            "hmaslow": 240,
        }
    )

    orb_start_hhmm: Tuple[int, int] = (9, 15)
    orb_end_hhmm: Tuple[int, int] = (9, 29)
    orb_ready_hhmm: Tuple[int, int] = (9, 30)


class StockMapThresholdConfig(BaseModel):
    rsi_zone: Dict[str, float] = Field(
        default_factory=lambda: {
            "os_extreme": 20.0,
            "os": 30.0,
            "ob": 70.0,
            "ob_extreme": 80.0,
        }
    )
    adx_band: Dict[str, float] = Field(
        default_factory=lambda: {"medium": 20.0, "strong": 30.0}
    )
    atr_pct_band: Dict[str, float] = Field(
        default_factory=lambda: {"medium": 0.70, "strong": 1.20}
    )
    rvol_pct_band: Dict[str, float] = Field(
        default_factory=lambda: {"low": 60.0, "high": 125.0}
    )
    bollinger_pos_zone: Dict[str, float] = Field(
        default_factory=lambda: {"near_lower": 0.20, "near_upper": 0.80}
    )


class StockMapStructureConfig(BaseModel):
    """Initial 15-minute structure settings.

    These deliberately begin with the proven Snapshot structure defaults. They
    are research configuration, not production-approved trading thresholds.
    """

    base_tf: str = "15m"

    structure_replay_bars: int = 120
    lookback_bars: int = 20
    swing_lookback: int = 8
    recent15_lookback: int = 5
    warmup_bars: int = 5
    use_symbol_last_snapshot_for_structure: bool = True

    opening_start: str = "09:15"
    opening_end: str = "09:30"
    active_start: str = "09:30"
    late_start: str = "14:45"

    prev_balance_threshold_pct: float = 10.0
    prev_balance_windows_minutes: List[int] = Field(
        default_factory=lambda: [45, 90, 180]
    )

    use_previous_day_range_as_initial_seed: bool = False
    initial_accepted_seed_source: str = "ORB"
    initial_accepted_seed_breakout_eligible: bool = False
    trust_intraday_after_minutes: int = 30

    min_intraday_range_bars: int = 6
    max_intraday_range_bars: int = 20
    recent_close_observation_bars: int = 6

    min_range_width_atr: float = 0.50
    preferred_min_range_width_atr: float = 0.75
    preferred_max_range_width_atr: float = 3.00

    min_adjacent_overlap_ratio: float = 0.55
    max_directional_efficiency: float = 0.35
    max_net_displacement_fraction: float = 0.45
    min_close_occupancy_ratio: float = 0.60
    min_boundary_interactions: int = 1
    boundary_interaction_zone_fraction: float = 0.20
    max_midpoint_drift_atr: float = 0.50
    max_boundary_drift_atr: float = 0.60

    balance_stable_evaluations: int = 2
    boundary_tolerance_atr: float = 0.20
    midpoint_tolerance_atr: float = 0.20
    min_range_overlap_for_same_balance: float = 0.70

    quality_replacement_margin: float = 10.0
    max_old_range_close_occupancy: float = 0.35
    replacement_recent_lookback_bars: int = 12
    replacement_min_observations: int = 3

    nested_range_max_width_ratio: float = 0.70
    nested_containment_tolerance_atr: float = 0.05

    overlap_evolution_min_overlap_ratio: float = 0.70
    overlap_evolution_quality_tolerance: float = 5.0
    overlap_evolution_min_occupancy_advantage: float = 0.15

    range_narrow_pct: float = 0.50
    range_normal_pct: float = 1.50
    range_wide_pct: float = 3.00
    compression_threshold_pct: float = 0.60

    promote_candidate_bars: int = 2
    candidate_state_required: int = 2
    structure_flip_confirm_count: int = 2

    max_flip_count_warning: int = 12
    dominant_state_lookback: int = 20
    flip_decay_window: int = 30

    orb_required_after: str = "09:30"
    debug_structure: bool = True
    allow_intraday_accepted_promotion: bool = True


class StockMapPriceActionConfig(BaseModel):
    movement_windows_minutes: Dict[str, int] = Field(
        default_factory=lambda: {"60m": 60, "30m": 30, "15m": 15}
    )
    slope_fast_bars: int = 3
    slope_slow_bars: int = 5
    slope_previous_bars: int = 3
    slope_flat_epsilon: float = 1e-9


class StockMapConfig(BaseModel):
    calculation_version: str = "STOCKMAP_V2"
    service: StockMapServiceConfig = Field(default_factory=StockMapServiceConfig)
    indicators: StockMapIndicatorConfig = Field(default_factory=StockMapIndicatorConfig)
    thresholds: StockMapThresholdConfig = Field(default_factory=StockMapThresholdConfig)
    structure: StockMapStructureConfig = Field(default_factory=StockMapStructureConfig)
    price_action: StockMapPriceActionConfig = Field(
        default_factory=StockMapPriceActionConfig
    )


STOCKMAP_CONFIG = StockMapConfig()
