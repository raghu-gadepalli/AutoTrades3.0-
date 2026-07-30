from __future__ import annotations

from pydantic import BaseModel


class IntradayLifecycleConfig(BaseModel):
    """Shared intraday lifecycle cutoffs.

    Signal lifecycle closes first on a normal 3-minute snapshot cadence.  The
    TradeMonitor cutoff remains an independent operational fail-safe two
    minutes later.
    """

    timezone_name: str = "Asia/Kolkata"
    signal_cutoff_time: str = "15:18:00"
    trade_fail_safe_cutoff_time: str = "15:20:00"


INTRADAY_LIFECYCLE_CONFIG = IntradayLifecycleConfig()
