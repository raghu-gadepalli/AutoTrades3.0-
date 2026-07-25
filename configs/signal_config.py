from __future__ import annotations

from pydantic import BaseModel, Field


class SignalServiceConfig(BaseModel):
    window_start: str = "09:19:00"
    window_end: str = "15:31:00"
    retry_interval_seconds: int = 15
    log_file: str = "/var/www/autotrades/scripts/signals.log"
    derivatives_required: bool = False
    backtest_mode: bool = False


class SignalResolutionConfig(BaseModel):

    stage_rank: dict[str, int] = Field(default_factory=lambda: {
        "FORCE_EXIT": 0,
        "EXIT_BIAS": 10,
        "WEAKENING": 20,
        "TRANSITION": 30,
        "DISCOVERY": 40,
        "BUILDING": 50,
        "PROTECT": 60,
        "ACTIVE": 70,
        "EXPAND": 80,
    })
    quality_rank: dict[str, int] = Field(default_factory=lambda: {
        "LOW": 1,
        "MEDIUM": 2,
        "HIGH": 3,
    })
    trade_action_rank: dict[str, int] = Field(default_factory=lambda: {
        "NO_ACTION": 0,
        "HOLD_POSITION": 10,
        "TIGHTEN_STOP": 20,
        "PARTIAL_EXIT": 30,
        "EXIT_POSITION": 40,
        "FORCE_EXIT": 50,
        "CREATE_TRADE": 100,
    })
    signal_action_rank: dict[str, int] = Field(default_factory=lambda: {
        "BLOCK": 0,
        "WAIT": 10,
        "WAIT_FOR_RECLAIM": 20,
        "BLOCK_NEW_ENTRY": 25,
        "HOLD": 30,
        "ALLOW": 100,
    })
    entry_posture_rank: dict[str, int] = Field(default_factory=lambda: {
        "BLOCK": -30,
        "WAIT": -15,
        "CAUTION": 5,
        "ALLOW": 15,
    })
    signal_decision_rank: dict[str, int] = Field(default_factory=lambda: {
        "WATCH": 0,
        "NO_ACTION": 0,
        "REVIEW_OPPOSITE": 5,
        "HOLD": 20,
        "UPDATE": 30,
        "DOWNGRADE": 35,
        "CREATE": 90,
        "PROMOTE": 100,
        "REPLACE_CREATE": 95,
        "REPLACE": 70,
        "CLOSE": 10,
        "INVALIDATE": 0,
    })
    signal_state_rank: dict[str, int] = Field(default_factory=lambda: {
        "BLOCKED": -100,
        "WATCH": 0,
        "TRACKING": 10,
        "ACCEPTED": 100,
        "READY": 100,
        "ACTIVE": 80,
        "MANAGE": 40,
        "CLOSED": -100,
        "REPLACED": -100,
        "INVALIDATED": -100,
    })


class AuditConfig(BaseModel):
    enabled: bool = True
    entity_type: str = "SIGNAL"
    evaluation_stage: str = "SIGNAL_GENERATOR"


class SignalGeneratorConfig(BaseModel):
    default_lifecycle: str = "DEFAULT"
    audit: AuditConfig = Field(default_factory=AuditConfig)
    service: SignalServiceConfig = Field(default_factory=SignalServiceConfig)
    resolution: SignalResolutionConfig = Field(default_factory=SignalResolutionConfig)


SIGNAL_CONFIG = SignalGeneratorConfig()