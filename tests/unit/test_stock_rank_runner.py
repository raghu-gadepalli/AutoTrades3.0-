from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import scripts.run_stock_rank as runner


def test_runner_has_no_command_line_override_surface() -> None:
    assert not hasattr(runner, "_build_parser")


def test_run_cycle_passes_production_cadence_contract() -> None:
    fake = Mock()
    fake.run.return_value = {
        "summary": {
            "status": "NO_NEW_CADENCE",
            "rank_time": "2026-08-01 10:00:00",
            "requested_symbols": 100,
            "cadence_coverage": 100,
            "ranked_symbols": 0,
            "persisted": False,
        },
        "rows": [],
    }
    runner.logger = Mock()
    last = datetime(2026, 8, 1, 10, 0)
    result = runner.run_cycle(
        now=datetime(2026, 8, 1, 10, 5, tzinfo=runner.IST),
        service=fake,
        last_rank_time=last,
    )
    assert result == last
    kwargs = fake.run.call_args.kwargs
    assert kwargs["persist"] is True
    assert kwargs["minimum_interval_minutes"] == 6
    assert kwargs["maximum_rank_age_minutes"] == 12
