from datetime import datetime
from zoneinfo import ZoneInfo

from configs.intraday_lifecycle_config import INTRADAY_LIFECYCLE_CONFIG
from configs.monitor_config import MONITOR_CONFIG
from configs.signal_config import SIGNAL_CONFIG
from utils.intraday_lifecycle import cutoff_due, parse_hms


def test_signal_cutoff_precedes_independent_trade_fail_safe() -> None:
    signal_cutoff = parse_hms(INTRADAY_LIFECYCLE_CONFIG.signal_cutoff_time)
    trade_cutoff = parse_hms(
        INTRADAY_LIFECYCLE_CONFIG.trade_fail_safe_cutoff_time
    )

    assert signal_cutoff < trade_cutoff
    assert SIGNAL_CONFIG.intraday_cutoff_time == "15:18:00"
    assert MONITOR_CONFIG.intraday_cutoff_time == "15:20:00"


def test_cutoff_due_supports_naive_replay_and_aware_live_times() -> None:
    cutoff = INTRADAY_LIFECYCLE_CONFIG.signal_cutoff_time

    assert cutoff_due(datetime(2026, 7, 29, 15, 17, 59), cutoff) is False
    assert cutoff_due(datetime(2026, 7, 29, 15, 18, 0), cutoff) is True
    assert cutoff_due(
        datetime(2026, 7, 29, 9, 48, 0, tzinfo=ZoneInfo("UTC")),
        cutoff,
    ) is True
