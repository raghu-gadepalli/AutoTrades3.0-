from __future__ import annotations

from pydantic import BaseModel, Field


class RunControlConfig(BaseModel):
    tz: str = "Asia/Kolkata"
    holidays: list[str] = Field(default_factory=lambda: [
        "2026-01-15",
        "2026-01-26",
        "2026-03-03",
        "2026-03-26",
        "2026-03-31",
        "2026-04-03",
        "2026-04-14",
        "2026-05-01",
        "2026-05-28",
        "2026-06-26",
        "2026-09-14",
        "2026-10-02",
        "2026-10-20",
        "2026-11-10",
        "2026-11-24",
        "2026-12-25",
    ])
    blackout_dates: list[str] = Field(default_factory=list)
    whitelist_dates: list[str] = Field(default_factory=lambda: ["2026-02-01"])


class EventExtrasConfig(BaseModel):
    error_backoff_seconds: float = 5.0
    backoff_base_seconds: float = 5.0
    backoff_max_seconds: float = 60.0
    retry_attempts: int = 3


class EventServiceConfig(BaseModel):
    window_start: str = "09:15:00"
    window_end: str = "15:35:00"
    retry_interval_seconds: int = 1
    log_file: str = "/var/www/autotrades/scripts/event_handler.log"
    extras: EventExtrasConfig = Field(default_factory=EventExtrasConfig)


class InitResetConfig(BaseModel):
    log_file: str = "/var/www/autotrades/scripts/init_intraday_reset.log"

    archive_before_truncate: bool = True
    archive_auditlog: bool = True
    reset_eq_flags: bool = True
    reset_user_logins: bool = True

    virtual_autologin_userids: list[str] = Field(default_factory=lambda: ["ADMIN", "TCQ489"])

    # Explicit reset scope. The reset runner must not infer table ownership or
    # silently fall back to a hard-coded list when configuration is incomplete.
    intraday_tables: list[str] = Field(default_factory=lambda: [
        "user_trades",
        "signals",
        "snapshots",
        "candles",
        "derivativeschain",
        "oms_funds_history",
        "oms_positions_history",
        "oms_orders_history",
        "auditlog",
    ])

    # Retained for compatibility with the existing configuration surface.
    restart_identity: bool = True
    use_cascade: bool = False
    deactivate_types: list[str] = Field(default_factory=lambda: ["FUT", "CE", "PE"])


class ServiceConfig(BaseModel):
    tz: str = "Asia/Kolkata"
    run_control: RunControlConfig = Field(default_factory=RunControlConfig)
    event: EventServiceConfig = Field(default_factory=EventServiceConfig)
    init_reset: InitResetConfig = Field(default_factory=InitResetConfig)


SERVICE_CONFIG = ServiceConfig()