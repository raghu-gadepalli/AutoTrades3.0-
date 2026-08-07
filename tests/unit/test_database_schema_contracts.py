from __future__ import annotations

from pathlib import Path
from sqlalchemy import BigInteger, Date, Numeric, UniqueConstraint

from models.trade_models import (
    Alert,
    AuditLogHistory,
    Base,
    Candle,
    DerivativesChain,
    Event,
    Instrument,
    Signal,
    Snapshot,
    Symbol,
    User,
    UserTrade,
    UserTradeHistory,
)
from schemas.alert import AlertSchema
from schemas.candle import CandleSchema
from schemas.derivatives import DerivativesChainSchema
from schemas.event import EventSchema
from schemas.instrument import InstrumentSchema
from schemas.signal import SignalSchema
from schemas.stock_opportunity import StockOpportunitySchema
from schemas.symbol import SymbolSchema
from schemas.user import UserSchema
from schemas.user_trade import UserTradeSchema


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "database" / "sql" / "20260801_align_live_schema_contracts.sql"


def _index_columns(model) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in index.columns)
        for index in model.__table__.indexes
    }


def _unique_columns(model) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in model.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def test_every_live_table_has_one_orm_mapping() -> None:
    expected_tables = {
        "alerts",
        "auditlog",
        "auditlog_history",
        "candles",
        "derivativeschain",
        "events",
        "instruments",
        "oms_funds",
        "oms_funds_history",
        "oms_orders",
        "oms_orders_history",
        "oms_positions",
        "oms_positions_history",
        "signals",
        "signals_history",
        "snapshots",
        "stock_opportunities",
        "symbols",
        "user_trades",
        "user_trades_history",
        "users",
    }
    assert set(Base.metadata.tables) == expected_tables


def test_db_backed_pydantic_fields_match_orm_columns() -> None:
    pairs = (
        (Alert, AlertSchema),
        (Candle, CandleSchema),
        (DerivativesChain, DerivativesChainSchema),
        (Event, EventSchema),
        (Instrument, InstrumentSchema),
        (Signal, SignalSchema),
        (Symbol, SymbolSchema),
        (User, UserSchema),
        (UserTrade, UserTradeSchema),
    )
    for orm_model, schema_model in pairs:
        assert set(orm_model.__table__.columns.keys()) == set(schema_model.model_fields)

    opportunity_columns = set(Base.metadata.tables["stock_opportunities"].columns.keys())
    assert opportunity_columns == set(StockOpportunitySchema.model_fields)


def test_schema_defaults_match_universe_field_ownership() -> None:
    assert SymbolSchema.model_fields["enabled"].default is False


def test_core_identity_primary_keys_are_explicit() -> None:
    assert tuple(column.name for column in User.__table__.primary_key.columns) == ("id",)
    assert tuple(column.name for column in Symbol.__table__.primary_key.columns) == ("id",)
    assert tuple(column.name for column in Alert.__table__.primary_key.columns) == ("id",)
    assert tuple(column.name for column in Event.__table__.primary_key.columns) == ("id",)
    assert tuple(column.name for column in Snapshot.__table__.primary_key.columns) == (
        "symbol",
        "snapshot_time",
    )
    assert tuple(column.name for column in DerivativesChain.__table__.primary_key.columns) == (
        "symbol",
        "snapshot_time",
    )
    assert tuple(column.name for column in UserTradeHistory.__table__.primary_key.columns) == (
        "hist_id",
    )


def test_user_identity_constraints_match_live_names() -> None:
    constraints = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in User.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert constraints["username"] == ("userid",)
    assert constraints["uq_users_mobile"] == ("mobile",)


def test_symbol_indexes_cover_operational_and_derivative_queries() -> None:
    assert ("symbol",) in _unique_columns(Symbol)
    assert ("type", "enabled", "active") in _index_columns(Symbol)
    assert (
        "equity_ref",
        "type",
        "expiry",
        "enabled",
        "strike_price",
    ) in _index_columns(Symbol)


def test_snapshot_indexes_cover_cross_section_and_unprocessed_reads() -> None:
    indexes = _index_columns(Snapshot)
    assert ("snapshot_time", "symbol") in indexes
    assert ("processed", "snapshot_time", "symbol") in indexes


def test_market_data_indexes_and_types_match_authoritative_master() -> None:
    candle_indexes = _index_columns(Candle)
    assert ("symbol", "frequency", "candle_time") in _unique_columns(Candle)
    assert ("candle_time",) in candle_indexes
    assert ("frequency",) in candle_indexes

    instrument_indexes = _index_columns(Instrument)
    assert ("instrument_token",) in _unique_columns(Instrument)
    assert ("name", "instrument_type", "expiry") in instrument_indexes
    assert {index.name for index in Instrument.__table__.indexes} >= {
        "exchange",
        "expiry",
        "instrument_type",
        "name",
        "tradingsymbol",
        "idx_instruments_underlying_expiry",
    }
    assert isinstance(Instrument.__table__.c.id.type, BigInteger)
    assert isinstance(Instrument.__table__.c.expiry.type, Date)
    assert isinstance(Instrument.__table__.c.last_price.type, Numeric)
    assert isinstance(Instrument.__table__.c.strike.type, Numeric)
    assert isinstance(Instrument.__table__.c.tick_size.type, Numeric)
    assert isinstance(Instrument.__table__.c.lot_size.type, Numeric)


def test_user_trade_indexes_are_non_redundant_and_match_query_paths() -> None:
    unique_columns = _unique_columns(UserTrade)
    assert ("userid", "signal_id", "symbol") in unique_columns
    assert ("userid", "signal_id", "instrument_type") in unique_columns

    indexes = _index_columns(UserTrade)
    assert indexes == {
        ("entry_status", "execution_mode"),
        ("exit_status", "execution_mode"),
        ("equity_ref",),
        ("signal_id",),
    }


def test_signal_indexes_include_keyset_pagination_paths() -> None:
    indexes = _index_columns(Signal)
    assert ("status", "last_eval_time", "id") in indexes
    assert ("last_eval_time", "id") in indexes


def test_user_trade_history_has_generated_day_and_archive_defaults() -> None:
    assert UserTradeHistory.__table__.c.hist_id.autoincrement is True
    assert UserTradeHistory.__table__.c.archived_on.server_default is not None
    assert UserTradeHistory.__table__.c.trading_date.computed is not None



def test_user_access_token_and_optional_audit_archive_identity() -> None:
    assert User.__table__.c.access_token.type.length == 255
    audit_unique = _unique_columns(AuditLogHistory)
    assert ("auditlog_id", "ts") in audit_unique


def test_alignment_migration_contains_all_critical_table_repairs() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    for fragment in (
        "ALTER TABLE users",
        "ALTER TABLE symbols",
        "ALTER TABLE alerts",
        "ALTER TABLE events",
        "ALTER TABLE snapshots",
        "ALTER TABLE derivativeschain",
        "ALTER TABLE instruments",
        "ALTER TABLE user_trades",
        "ALTER TABLE user_trades_history",
        "ADD PRIMARY KEY (symbol, snapshot_time)",
        "idx_snapshots_unprocessed",
        "idx_symbols_derivative_lookup",
        "idx_instruments_underlying_expiry",
    ):
        assert fragment in sql
