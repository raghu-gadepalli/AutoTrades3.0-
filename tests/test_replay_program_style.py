from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"


def _source(name: str) -> str:
    return (TESTS / name).read_text(encoding="utf-8")


def test_signal_replay_uses_visible_defaults_and_optional_clear_data() -> None:
    source = _source("replay_auction_signal_generator.py")
    assert 'DEFAULT_TRADING_DAY = "2026-07-27"' in source
    assert (
        'DEFAULT_SYMBOLS = "LT,BHEL,INDIGO,MAXHEALTH,PERSISTENT,'
        'PNBHOUSING,TCS"'
    ) in source
    assert "DEFAULT_CLEAR_DATA = False" in source
    assert '"--clear-data"' in source
    assert "argparse.BooleanOptionalAction" in source
    assert "required=True" not in source
    assert '"--allowed-database"' not in source
    assert "replay_database_guard" not in source


def test_trade_pipeline_replay_uses_same_default_override_style() -> None:
    source = _source("replay_auction_signal_trade_pipeline.py")
    assert 'DEFAULT_TRADING_DAY = "2026-07-27"' in source
    assert (
        'DEFAULT_SYMBOLS = "LT,BHEL,INDIGO,MAXHEALTH,PERSISTENT,'
        'PNBHOUSING,TCS"'
    ) in source
    assert "DEFAULT_CLEAR_DATA = False" in source
    assert '"--clear-data"' in source
    assert "argparse.BooleanOptionalAction" in source
    assert '"--allowed-database"' not in source
    assert "replay_database_guard" not in source
    assert "Replay scope is not clean" not in source
    assert "_assert_no_external_active_context" not in source


def test_retired_monolithic_replay_program_is_removed() -> None:
    assert not (TESTS / "replay_pipeline.py").exists()


def test_snapshot_and_unprocessed_replays_use_configured_database_directly() -> None:
    for name in (
        "replay_snapshots.py",
        "replay_unprocessed.py",
        "replay_unprocessed_multi.py",
    ):
        source = _source(name)
        assert '"--allowed-database"' not in source
        assert "replay_database_guard" not in source


def test_replay_database_guard_programs_are_removed() -> None:
    assert not (TESTS / "replay_database_guard.py").exists()
    assert not (TESTS / "test_replay_database_guard.py").exists()
