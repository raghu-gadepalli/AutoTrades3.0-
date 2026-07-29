from __future__ import annotations

from pydantic import BaseModel, Field


class TradeDefaultsConfig(BaseModel):
    min_eq_amt: float = 2500.0
    default_position_style: str = "NAKED"
    default_product_type: str = "MIS"


class TradeDecisionPolicyConfig(BaseModel):
    # Generic signal-entry price safeguard. This is deliberately minimal and
    # applies to every originating setup. TradeGenerator may deploy an OPEN
    # signal only when the evaluation snapshot price is strictly favorable
    # versus the signal creation price; equality remains DEFER.
    signal_entry_not_in_loss_enabled: bool = True
    signal_entry_wait_in_loss_code: str = "SIGNAL_ENTRY_WAIT_NOT_STRICTLY_PROFITABLE"
    signal_entry_wait_price_missing_code: str = "SIGNAL_ENTRY_WAIT_PRICE_UNAVAILABLE"

    # Manual signal trades are allowed through an explicit confirmation flow.
    # These thresholds create informational UI warnings only; they cannot
    # override a hard signal-exit posture.
    manual_entry_warning_enabled: bool = True
    manual_entry_delay_warning_minutes: float = 6.0
    manual_entry_move_warning_pct: float = 0.50

    block_duplicate_signal_trade: bool = True
    block_duplicate_symbol_trade: bool = True


class TradePolicyConfig(BaseModel):
    decision: TradeDecisionPolicyConfig = Field(default_factory=TradeDecisionPolicyConfig)


class TradeServiceConfig(BaseModel):
    window_start: str = "09:15:00"
    window_end: str = "15:31:00"
    retry_interval_seconds: int = 15
    log_file: str = "/var/www/autotrades/scripts/trades.log"

    defaults: TradeDefaultsConfig = Field(default_factory=TradeDefaultsConfig)
    policy: TradePolicyConfig = Field(default_factory=TradePolicyConfig)


TRADE_CONFIG = TradeServiceConfig()
