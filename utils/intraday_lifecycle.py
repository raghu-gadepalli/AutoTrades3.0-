from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from configs.intraday_lifecycle_config import INTRADAY_LIFECYCLE_CONFIG


def parse_hms(value: str) -> time:
    text = str(value or "").strip()
    if not text:
        raise ValueError("intraday cutoff time is required")
    try:
        parsed = time.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid intraday cutoff time: {text!r}") from exc
    if parsed.tzinfo is not None:
        raise ValueError("intraday cutoff time must not include timezone information")
    return parsed


def to_intraday_local(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("intraday cutoff evaluation requires datetime")
    if value.tzinfo is None:
        return value
    zone = ZoneInfo(INTRADAY_LIFECYCLE_CONFIG.timezone_name)
    return value.astimezone(zone).replace(tzinfo=None)


def cutoff_due(value: datetime, cutoff_time: str) -> bool:
    local = to_intraday_local(value)
    cutoff = parse_hms(cutoff_time)
    current = local.time().replace(tzinfo=None)
    return current >= cutoff


def validate_cutoff_order() -> None:
    signal_cutoff = parse_hms(INTRADAY_LIFECYCLE_CONFIG.signal_cutoff_time)
    trade_cutoff = parse_hms(
        INTRADAY_LIFECYCLE_CONFIG.trade_fail_safe_cutoff_time
    )
    if signal_cutoff >= trade_cutoff:
        raise ValueError(
            "signal_cutoff_time must be earlier than trade_fail_safe_cutoff_time"
        )


validate_cutoff_order()
