from __future__ import annotations

from typing import Dict, List, Tuple
from pydantic import BaseModel, Field


class SnapshotServiceConfig(BaseModel):
    window_start: str = "09:16:00"
    window_end: str = "15:31:00"
    retry_interval_seconds: int = 15
    log_file: str = "/var/www/autotrades/scripts/snapshots.log"
    max_workers: int = 4
    staleness_minutes: int = 8
    tick_minutes: int = 3


class IndicatorConfig(BaseModel):
    """
    Snapshot indicator calculation settings.

    This replaces the old config.py IndicatorConfig class for snapshot generation.
    """

    frequency: str = "minute"
    lookback_days: int = 30

    hma_lengths: Dict[str, int] = Field(default_factory=lambda: {
        "hmafast": 15,
        "hmamid1": 60,
        "hmamid2": 120,
        "hmaslow": 240,
    })

    ema_lengths: Dict[str, int] = Field(default_factory=lambda: {
        "ema_fast": 9,
        "ema_mid1": 20,
        "ema_mid2": 50,
        "ema_slow": 100,
        "ema_ref": 200,
    })

    eps_pct: float = 0.0003

    rsi_period: int = 14
    atr_period: int = 14
    adx_period: int = 14

    volume_period: int = 20
    volume_slope_period: int = 20

    intraday_lookback_minutes: int = 20

    bb_period: int = 20
    bb_std_mult: float = 2.0

    orb_start_hhmm: Tuple[int, int] = (9, 15)
    orb_end_hhmm: Tuple[int, int] = (9, 29)
    orb_ready_hhmm: Tuple[int, int] = (9, 30)


class ThresholdConfig(BaseModel):
    """
    Snapshot indicator threshold labels.

    Labels belong in enums/domain code; these are only numeric cutoffs.
    """

    rsi_zone: Dict[str, float] = Field(default_factory=lambda: {
        "os_extreme": 20.0,
        "os": 30.0,
        "ob": 70.0,
        "ob_extreme": 80.0,
    })

    adx_band: Dict[str, float] = Field(default_factory=lambda: {
        "medium": 20.0,
        "strong": 30.0,
    })

    atr_pct_band: Dict[str, float] = Field(default_factory=lambda: {
        "medium": 0.70,
        "strong": 1.20,
    })

    rvol_pct_band: Dict[str, float] = Field(default_factory=lambda: {
        "low": 60.0,
        "high": 125.0,
    })

    bollinger_pos_zone: Dict[str, float] = Field(default_factory=lambda: {
        "near_lower": 0.20,
        "near_upper": 0.80,
    })


class SessionPhaseConfig(BaseModel):
    opening: Dict[str, object] = Field(default_factory=lambda: {
        "start": "09:15",
        "end": "09:30",
        "vwap_reliable": False,
        "prefer_orb_pdh_pdl": True,
    })

    active: Dict[str, object] = Field(default_factory=lambda: {
        "start": "09:30",
        "end": "14:45",
        "vwap_reliable": True,
    })

    late: Dict[str, object] = Field(default_factory=lambda: {
        "start": "14:45",
        "end": "15:25",
        "vwap_reliable": True,
        "tighten_new_entries": True,
    })


class StructureConfig(BaseModel):
    """
    Structure/auction settings used by snapshot generation and snapshot helper.

    This intentionally carries all current values from the old config.py StructureConfig
    so we can migrate snapshot files without changing behavior.
    """

    base_tf: str = "3m"

    structure_replay_bars: int = 120
    lookback_bars: int = 20
    swing_lookback: int = 8
    recent15_lookback: int = 5
    warmup_bars: int = 5

    # Use previous persisted snapshot state_memory for live incremental
    # structure continuity. If no immediately previous same-day snapshot exists,
    # the generator rebuilds state_memory by replaying the current in-memory
    # candle set for that symbol.
    use_symbol_last_snapshot_for_structure: bool = True

    opening_start: str = "09:15"
    opening_end: str = "09:30"
    active_start: str = "09:30"
    late_start: str = "14:45"

    prev_balance_threshold_pct: float = 10.0
    prev_balance_windows_minutes: List[int] = Field(default_factory=lambda: [45, 90, 180])

    # Multi-level structure model. The opening range is one possible seed for
    # the first accepted balance. Accepted range, breakout reference levels,
    # ORB, previous-day levels, recent levels, and compression levels are
    # intentionally separate concepts.
    #
    # initial_accepted_seed_source controls only the first accepted-balance seed.
    # It does not make ORB a permanent hardcoded accepted range.
    use_previous_day_range_as_initial_seed: bool = False
    initial_accepted_seed_source: str = "ORB"
    # The opening range may seed structure continuity but cannot originate a
    # tradable accepted/failed breakout until an intraday balance evolves.
    initial_accepted_seed_breakout_eligible: bool = False
    trust_intraday_after_minutes: int = 15

    # Dynamic intraday balance discovery.  The latest completed candle is
    # deliberately excluded from this calculation by snapshot_helper; it may
    # only test/break a balance that existed before that candle began.
    min_intraday_range_bars: int = 6
    max_intraday_range_bars: int = 20

    # Compact causal close history exposed for Evidence. Evidence may apply its
    # own configured rules to these market facts without re-fetching candles.
    recent_close_observation_bars: int = 6

    # A very small box relative to normal candle movement is usually market
    # noise.  It can still be retained as MICRO_COMPRESSION, but only after it
    # clears this hard noise floor.  The preferred width band affects quality,
    # not structural validity; there is intentionally no hard maximum width.
    min_range_width_atr: float = 0.50
    preferred_min_range_width_atr: float = 0.75
    preferred_max_range_width_atr: float = 3.00

    # Deterministic balance classification.  These are starting values for
    # replay validation and remain fully configuration-driven.
    min_adjacent_overlap_ratio: float = 0.55
    max_directional_efficiency: float = 0.35
    max_net_displacement_fraction: float = 0.45
    min_close_occupancy_ratio: float = 0.60
    min_boundary_interactions: int = 1
    boundary_interaction_zone_fraction: float = 0.20
    max_midpoint_drift_atr: float = 0.50
    max_boundary_drift_atr: float = 0.60

    # A balance must be independently rediscovered with materially unchanged
    # boundaries on consecutive historical cut-offs before it is qualified.
    balance_stable_evaluations: int = 2
    boundary_tolerance_atr: float = 0.20
    midpoint_tolerance_atr: float = 0.20
    min_range_overlap_for_same_balance: float = 0.70

    # Dynamic accepted-range replacement.  Relocation is measured only from
    # candles that completed after the accepted range was established; the
    # original formation candles must never be reused as relocation evidence.
    quality_replacement_margin: float = 10.0
    max_old_range_close_occupancy: float = 0.35
    replacement_recent_lookback_bars: int = 12
    replacement_min_observations: int = 3

    # A nested refinement must be geometrically contained inside the accepted
    # range (within this small ATR tolerance), not merely narrower.
    nested_range_max_width_ratio: float = 0.70
    nested_containment_tolerance_atr: float = 0.05

    # A newer qualified balance may evolve an accepted range when the two
    # ranges still overlap materially, recent closes occupy the new balance
    # more strongly than the old one, and candidate quality is not materially
    # worse.  This handles gradual boundary migration without allowing a
    # disconnected narrow box to masquerade as a nested refinement.
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


class PriceActionSnapshotConfig(BaseModel):
    """Strategy-neutral price-action evidence settings used by snapshots.

    Keep these values out of snapshot helper code so intraday evidence can be
    tuned from one place without changing implementation logic.
    """

    # Movement windows to persist under snapshot.price_action.moves.
    # SOD is handled separately; these are rolling intraday windows in minutes.
    movement_windows_minutes: Dict[str, int] = Field(default_factory=lambda: {
        "60m": 60,
        "30m": 30,
        "15m": 15,
    })

    # Slope/gradient windows. With 3m snapshots these defaults represent
    # approximately 9m and 15m, but the helper uses bar count rather than
    # assuming a fixed wall-clock cadence.
    slope_fast_bars: int = 3
    slope_slow_bars: int = 5
    slope_previous_bars: int = 3

    # Small numerical dead-zone used only to label a slope as FLAT.
    slope_flat_epsilon: float = 1e-9


class SnapshotAuctionConfig(BaseModel):
    """Strict direct Auction enrichment of every generated snapshot."""

    # A same-day gap beyond this limit is an explicit generation error. The
    # engine must never silently reset or substitute missing continuity.
    max_incremental_gap_minutes: float = 4.0


class SnapshotConfig(BaseModel):
    service: SnapshotServiceConfig = Field(default_factory=SnapshotServiceConfig)
    single_frequencies: List[str] = Field(default_factory=lambda: [
        "minute", "3minute", "5minute", "10minute", "15minute", "30minute", "60minute"
    ])
    indicators: IndicatorConfig = Field(default_factory=IndicatorConfig)
    thresholds: ThresholdConfig = Field(default_factory=ThresholdConfig)
    session: SessionPhaseConfig = Field(default_factory=SessionPhaseConfig)
    structure: StructureConfig = Field(default_factory=StructureConfig)
    price_action: PriceActionSnapshotConfig = Field(default_factory=PriceActionSnapshotConfig)
    auction: SnapshotAuctionConfig = Field(default_factory=SnapshotAuctionConfig)


SNAPSHOT_CONFIG = SnapshotConfig()
