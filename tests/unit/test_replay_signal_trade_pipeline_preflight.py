from types import SimpleNamespace

from tests.replays import replay_signal_trade_pipeline as replay


def test_resolve_snapshot_symbol_scope_skips_only_missing_symbols():
    requested = ["BHEL", "INDIGO", "LT", "TCS"]
    snapshots = [
        SimpleNamespace(symbol="TCS"),
        SimpleNamespace(symbol="BHEL"),
        SimpleNamespace(symbol="BHEL"),
    ]

    replayed, missing = replay._resolve_snapshot_symbol_scope(requested, snapshots)

    assert replayed == ["BHEL", "TCS"]
    assert missing == ["INDIGO", "LT"]


def test_resolve_snapshot_symbol_scope_reports_zero_coverage():
    replayed, missing = replay._resolve_snapshot_symbol_scope(
        ["INDIGO", "LT"],
        [],
    )

    assert replayed == []
    assert missing == ["INDIGO", "LT"]


def test_zero_snapshot_preflight_does_not_clear_data(monkeypatch, tmp_path):
    clear_calls = []

    monkeypatch.setattr(replay, "_validate_replay_user", lambda *_: None)
    monkeypatch.setattr(replay, "_configured_database_name", lambda: "autotrades")
    monkeypatch.setattr(replay, "_load_snapshots", lambda **_: [])
    monkeypatch.setattr(replay, "_clear_data", lambda: clear_calls.append(True))
    monkeypatch.setattr(replay, "setup_logging", lambda **_: None)

    exit_code = replay.main(
        [
            "--date",
            "2026-07-31",
            "--symbols",
            "INDIGO,LT",
            "--userid",
            "DR1812",
            "--clear-data",
            "--report-dir",
            str(tmp_path),
        ]
    )

    assert exit_code == 2
    assert clear_calls == []
