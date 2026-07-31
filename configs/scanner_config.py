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
    """Defaults for the policy-only enabled-universe operation."""

    log_file: str = "/var/www/autotrades/operations/filter_stock_universe.log"
    report_dir: str = "reports"


class UniverseGenerationConfig(BaseModel):
    """Defaults for ad hoc active-universe curation.

    This is not a live service configuration. The operation is run manually,
    defaults to review mode, and changes ``symbols.active`` only with
    ``--apply``.
    """

    log_file: str = "/var/www/autotrades/operations/generate_stock_universe.log"
    report_dir: str = "reports"

    active_limit: int = 100
    calendar_lookback_days: int = 180
    minimum_history_days: int = 60
    atr_period: int = 14
    hysteresis_slots: int = 10
    minimum_coverage_pct: float = 90.0

    historical_interval: str = "day"
    historical_rate_sleep_sec: float = 0.08
    market_close_time: str = "15:30:00"

    minimum_active_range_pct: float = 0.35
    economic_price_floor: float = 50.0
    economic_price_ceiling: float = 5000.0

    # Cross-sectional percentile score weights. Sum must equal 1.0.
    weight_atr_pct: float = 0.20
    weight_median_range_pct: float = 0.20
    weight_turnover: float = 0.25
    weight_directional_efficiency: float = 0.15
    weight_active_day_ratio: float = 0.10
    weight_derivative_availability: float = 0.05
    weight_price_economics: float = 0.05


class ScannerConfig(BaseModel):
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    filter: FilterConfig = Field(default_factory=FilterConfig)
    universe_generation: UniverseGenerationConfig = Field(default_factory=UniverseGenerationConfig)


SCANNER_CONFIG = ScannerConfig()
