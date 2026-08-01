from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field


class UniverseConfig(BaseModel):
    """Application policy for the EQ universe.

    ``filter_stock_universe`` owns ``symbols.enabled`` from this policy.
    Whitelist membership takes precedence over blacklist membership.
    """

    blacklist: List[str] = Field(default_factory=lambda: [
        # Adani / event driven
        "ADANIENT",
        "ADANIPOWER",
        "ADANIENSOL",

        # PSU / theme driven
        "IREDA",
        "IRFC",
        "RVNL",
        "NBCC",
        "NHPC",

        # Retail / speculative
        "IDEA",
        "SUZLON",

        # New age / sentiment driven
        "NYKAA",
        "SWIGGY",

        # High beta banking
        "YESBANK",
        "RBLBANK",

        # Renewable / narrative driven
        "WAAREEENER",

        # Shipping / infra
        "COCHINSHIP",
        "CONCOR",

        # Airports / exchange
        "GMRAIRPORT",
        "IEX",

        # Property
        "GODREJPROP",

        # PSU banks
        "BANKINDIA",
        "CANBK",
        "PNB",
        "UNIONBANK",
        "BANDHANBNK",
        "FEDERALBNK",
        "IDFCFIRSTB",

        # Auto / ancillary
        "ASHOKLEY",
        "SONACOMS",
        "MOTHERSON",

        # Metals
        "SAIL",
        "NMDC",

        # Operator / erratic behavior
        "DIXON",
        "KAYNES",
        "KEI",
        "KFINTECH",

        # Reversal-prone / inconsistent opportunity
        "NAUKRI",
        "PGEL",
        "PHOENIXLTD",
        "SUPREMEIND",

        # Merger / restructuring uncertainty
        "PFC",
        "RECLTD",

        # Low movement / opportunity cost
        "WIPRO",
        "NTPC",

        # Empirically inconsistent behavior
        "CUMMINSIND",
        "IOC",
        "VBL",
        "ZYDUSLIFE",

        # Index products
        "FINNIFTY",
        "MIDCPNIFTY",
        "NIFTYNXT50",
    ])

    whitelist: List[str] = Field(default_factory=lambda: [
        "NIFTY 50",
        "NIFTY BANK",
        "KOTAKBANK",
        "ICICIBANK",
        "AXISBANK",
        "HCLTECH",
        "MARUTI",
    ])
    
class FilterConfig(BaseModel):
    """Defaults for the enabled-universe policy operation.

    Whitelist/blacklist membership lives in ``UniverseConfig``.  This section
    contains only the additional price-policy and operating defaults.
    """

    log_file: str = "/var/www/autotrades/operations/filter_stock_universe.log"
    report_dir: str = "reports"
    minimum_price: float = 200.0
    quote_batch_size: int = 200
    minimum_quote_coverage_pct: float = 90.0


class UniverseGenerationConfig(BaseModel):
    """Defaults for ad hoc active-universe curation.

    The operation ranks only persisted ``enabled=True`` EQ symbols, proposes a
    stable active universe, and reports 60-day top-mover capture.  It is not a
    live service and writes ``symbols.active`` only with ``--apply``.
    """

    log_file: str = "/var/www/autotrades/operations/generate_stock_universe.log"
    report_dir: str = "reports"

    # Configurable observation-universe size.  This may later be reduced to 80
    # or 70 after comparing signal load with mover capture.
    active_limit: int = 100

    # Most recent completed trading-day window used for both scoring and the
    # top-mover report.
    audit_trading_days: int = 60
    top_movers_per_day: int = 20
    calendar_lookback_days: int = 180
    minimum_history_days: int = 60
    atr_period: int = 14

    # Retain a current operational member only when its score is genuinely
    # close to the weakest proposed newcomer.
    hysteresis_score_gap: float = 0.02

    minimum_history_coverage_pct: float = 90.0
    minimum_derivative_coverage_pct: float = 90.0
    maximum_history_staleness_days: int = 7

    historical_interval: str = "day"
    historical_rate_sleep_sec: float = 0.08
    market_close_time: str = "15:30:00"

    # Supplemental report sections.  These never alter selection scores.
    # Policy-mover behaviour uses ordered 15-minute bars so a strong move that
    # later reverses is not mistaken for an orderly one-sided daily candle.
    policy_mover_min_top20_days: int = 3
    policy_reversal_min_excursion_pct: float = 1.0
    policy_reversal_retracement_ratio: float = 0.50
    policy_full_reversal_flag_rate_pct: float = 20.0
    policy_low_close_retention_ratio: float = 0.40
    policy_two_sided_ratio_threshold: float = 0.35
    policy_gap_driven_share_threshold: float = 0.50
    policy_event_cluster_max_week_share: float = 0.60
    policy_behavior_flag_rate_pct: float = 30.0

    intraday_interval: str = "15minute"
    market_open_time: str = "09:15:00"
    early_window_minutes: int = 45
    early_move_min_session_excursion_pct: float = 1.0
    early_move_min_share_pct: float = 70.0
    # After the first 45 minutes, a day is considered consumed only when price
    # makes little fresh range outside the opening range and most later bars
    # remain contained inside it.  Retracement inside the opening range is not
    # treated as a new opportunity.
    early_move_max_post_range_extension_pct: float = 0.35
    early_move_min_post_contained_pct: float = 70.0
    early_move_containment_tolerance_pct: float = 0.10
    early_move_late_opportunity_extension_pct: float = 0.60
    intraday_min_bars_per_day: int = 20
    # Avoid confident labels when only a handful of meaningful mover days are
    # available.  The numeric evidence remains in the report.
    early_move_min_classification_days: int = 5
    early_move_high_rate_pct: float = 50.0
    early_move_moderate_rate_pct: float = 30.0
    intraday_rate_sleep_sec: float = 0.35

    # Cross-sectional percentile weights.  Normal movement and liquidity carry
    # 80%; frequency and weekly spread of top-20 appearances carry 20%.
    weight_median_excursion_pct: float = 0.25
    weight_turnover: float = 0.25
    weight_atr_pct: float = 0.15
    weight_directional_efficiency: float = 0.15
    weight_top20_days: float = 0.10
    weight_top20_weeks: float = 0.10


class ScannerConfig(BaseModel):
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    filter: FilterConfig = Field(default_factory=FilterConfig)
    universe_generation: UniverseGenerationConfig = Field(default_factory=UniverseGenerationConfig)


SCANNER_CONFIG = ScannerConfig()
