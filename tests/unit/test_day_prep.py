from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import UniqueConstraint

import schemas.archive as archive_module
from configs.service_config import DayPrepConfig, SERVICE_CONFIG
from enums.enums import EntryStatus, ExitStatus
from models.trade_models import (
    AuditLog,
    AuditLogHistory,
    Candle,
    DerivativesChain,
    Signal,
    Snapshot,
    StockOpportunity,
    StockRank,
    User,
    UserFunds,
    UserOrders,
    UserPositions,
    UserTrade,
)
from schemas.archive import ArchiveResult, archive_columns
from schemas.signal import SignalSchema
from schemas.stock_rank import StockRankSchema
from schemas.user_trade import UserTradeSchema
from services.operations.day_prep import ClearResult, DayPrepService


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "database" / "sql" / "20260801_prepare_day_schema.sql"


def _capture_archive_spec(monkeypatch, schema_method):
    monkeypatch.setattr(archive_module, "archive_rows", lambda spec: spec)
    return schema_method()


def test_day_prep_config_is_explicit_and_audit_archive_is_off() -> None:
    config = DayPrepConfig()
    assert config.archive_auditlog is False
    assert config.reset_user_logins is True
    assert config.clear_oms_current_state is True
    assert config.strict_unresolved_trade_check is True
    assert SERVICE_CONFIG.day_prep.log_file.endswith("prepare_day.log")
    assert not hasattr(SERVICE_CONFIG, "init_reset")


def test_day_prep_never_owns_symbol_state() -> None:
    service = DayPrepService(DayPrepConfig())
    required = {model.__table__.name for model in service._required_models()}
    cleared = {model.__table__.name for model in service._clear_models()}

    assert "symbols" not in required
    assert "symbols" not in cleared
    assert cleared == {
        "user_trades",
        "signals",
        "stock_rank",
        "stock_opportunities",
        "snapshots",
        "candles",
        "derivativeschain",
        "auditlog",
        "oms_funds",
        "oms_positions",
        "oms_orders",
    }
    assert not {
        "signals_history",
        "user_trades_history",
        "stock_rank_history",
        "auditlog_history",
        "oms_funds_history",
        "oms_positions_history",
        "oms_orders_history",
    } & cleared


def test_archive_specs_derive_complete_matching_payloads(monkeypatch) -> None:
    specs = (
        _capture_archive_spec(monkeypatch, SignalSchema.archive_current_rows),
        _capture_archive_spec(monkeypatch, UserTradeSchema.archive_current_rows),
        _capture_archive_spec(monkeypatch, StockRankSchema.archive_current_rows),
    )

    expected = {
        "signals": (Signal, {"hist_id", "archived_on", "trading_date"}),
        "user_trades": (UserTrade, {"hist_id", "archived_on", "trading_date"}),
        "stock_rank": (StockRank, {"history_id", "archived_on"}),
    }

    for spec in specs:
        target_columns, source_columns = archive_columns(spec)
        source_model, excluded = expected[spec.name]
        assert len(target_columns) == len(source_columns)
        assert {column.name for column in source_columns} == {
            column.name for column in source_model.__table__.columns
        }
        assert excluded.isdisjoint({column.name for column in target_columns})

    rank_spec = specs[-1]
    target_columns, source_columns = archive_columns(rank_spec)
    mapping = dict(zip(
        (column.name for column in target_columns),
        (column.name for column in source_columns),
    ))
    assert mapping["stock_rank_id"] == "id"


def test_audit_archive_is_optional_but_idempotent_when_enabled() -> None:
    service = DayPrepService(DayPrepConfig(archive_auditlog=True))
    required = {model.__table__.name for model in service._required_models()}
    assert "auditlog_history" in required

    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in AuditLogHistory.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("auditlog_id", "ts") in unique_columns


def test_terminal_trade_contract_is_strict() -> None:
    is_terminal = DayPrepService._is_terminal_trade

    for status in (
        EntryStatus.EXPIRED.value,
        EntryStatus.CANCELLED.value,
        EntryStatus.REJECTED.value,
        EntryStatus.INVALID.value,
    ):
        assert is_terminal(status, ExitStatus.NONE.value)

    assert is_terminal(EntryStatus.FILLED.value, ExitStatus.FILLED.value)
    assert not is_terminal(EntryStatus.CREATED.value, ExitStatus.NONE.value)
    assert not is_terminal(EntryStatus.READY.value, ExitStatus.NONE.value)
    assert not is_terminal(EntryStatus.SUBMITTED.value, ExitStatus.NONE.value)
    assert not is_terminal(EntryStatus.FILLED.value, ExitStatus.NONE.value)
    assert not is_terminal(EntryStatus.FILLED.value, ExitStatus.CANCELLED.value)
    assert not is_terminal(EntryStatus.FILLED.value, ExitStatus.FAILED.value)


def test_prepare_archives_every_durable_table_before_any_clear(monkeypatch) -> None:
    config = DayPrepConfig(
        archive_auditlog=False,
        clear_oms_current_state=False,
        virtual_autologin_userids=[],
    )
    service = DayPrepService(config)
    events: list[str] = []

    monkeypatch.setattr(service, "_verify_tables_exist", lambda: events.append("preflight"))
    monkeypatch.setattr(service, "_unresolved_trade_rows", lambda: [])
    monkeypatch.setattr(service, "_opportunity_state_counts", lambda: {"OBSERVING": 2})

    def archive_all():
        events.extend(["archive:signals", "archive:user_trades", "archive:stock_rank"])
        return {
            name: ArchiveResult(name, 1, 1, 1).as_dict()
            for name in ("signals", "user_trades", "stock_rank")
        }

    monkeypatch.setattr(service, "_archive_rows", archive_all)
    monkeypatch.setattr(service, "_clear_models", lambda: (Signal, StockRank))

    def clear_model(model):
        events.append(f"clear:{model.__table__.name}")
        return ClearResult(model.__table__.name, 1, 0)

    monkeypatch.setattr(service, "_clear_model", clear_model)
    monkeypatch.setattr(service, "_reset_user_logins", lambda: 0)
    monkeypatch.setattr(service, "_enable_virtual_users", lambda: (0, []))

    summary = service.prepare()

    assert summary["success"] is True
    first_clear = min(i for i, event in enumerate(events) if event.startswith("clear:"))
    last_archive = max(i for i, event in enumerate(events) if event.startswith("archive:"))
    assert last_archive < first_clear
    assert summary["stock_opportunity_states_before_clear"] == {"OBSERVING": 2}


def test_prepare_blocks_before_archive_when_unresolved_trades_exist(monkeypatch) -> None:
    service = DayPrepService(
        DayPrepConfig(
            strict_unresolved_trade_check=True,
            virtual_autologin_userids=[],
        )
    )
    events: list[str] = []
    monkeypatch.setattr(service, "_verify_tables_exist", lambda: None)
    monkeypatch.setattr(
        service,
        "_unresolved_trade_rows",
        lambda: [{"id": 7, "entry_status": "FILLED", "exit_status": "NONE"}],
    )
    monkeypatch.setattr(service, "_archive_rows", lambda: events.append("archive"))

    with pytest.raises(RuntimeError, match="unresolved trades"):
        service.prepare()
    assert events == []


def test_user_access_token_contract_and_day_prep_migration() -> None:
    access_token = User.__table__.c.access_token
    assert access_token.type.length == 255
    assert access_token.nullable is False
    assert access_token.server_default is not None

    sql = MIGRATION.read_text(encoding="utf-8")
    assert "VARCHAR(255) NOT NULL DEFAULT ''" in sql
    assert "uq_auditlog_history_live_ts" in sql
    assert "auditlog_id, ts" in sql


def test_prepare_day_runner_has_no_command_line_override_surface() -> None:
    import scripts.prepare_day as runner

    assert not hasattr(runner, "_parser")


def test_prepare_day_runner_returns_nonzero_without_reraising(monkeypatch) -> None:
    import scripts.prepare_day as runner

    logger = Mock()
    monkeypatch.setattr(runner, "setup_logging", lambda **_kwargs: None)
    monkeypatch.setattr(
        runner,
        "logging",
        SimpleNamespace(getLogger=lambda *_args: logger),
    )
    monkeypatch.setattr(runner, "allow_run_today", lambda *_args: True)

    class FailingDayPrepService:
        def prepare(self) -> None:
            raise RuntimeError("archive verification failed")

    monkeypatch.setattr(runner, "DayPrepService", FailingDayPrepService)

    assert runner.main() == 1
    logger.exception.assert_called_once_with("DAY_PREP_FAILED")


def test_prepare_day_runner_returns_zero_on_success(monkeypatch) -> None:
    import scripts.prepare_day as runner

    service = Mock()
    monkeypatch.setattr(runner, "setup_logging", lambda **_kwargs: None)
    monkeypatch.setattr(runner, "allow_run_today", lambda *_args: True)
    monkeypatch.setattr(runner, "DayPrepService", lambda: service)

    assert runner.main() == 0
    service.prepare.assert_called_once_with()


def test_obsolete_initializer_removed_and_prepare_day_is_cron_runner() -> None:
    assert not (ROOT / "scripts" / "init_intraday_reset.py").exists()
    assert (ROOT / "scripts" / "prepare_day.py").exists()
    assert not (ROOT / "t_prepare_day.service").exists()