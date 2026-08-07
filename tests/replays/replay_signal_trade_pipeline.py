#!/usr/bin/env python3
"""Strict end-to-end replay of the signal/trade pipeline.

Pipeline per persisted snapshot
-------------------------------

    stored validated snapshot.auction
        -> SignalGenerator
        -> TradeGenerator (selected replay user only)
        -> TradeExecutor entry pass (selected replay scope only)
        -> TradeMonitor (selected replay scope only)
        -> TradeExecutor exit pass (selected replay scope only)

Safety and scope
----------------
- Does NOT regenerate snapshots or Auction.
- Does not read or write any retired Auction persistence tables.
- Does NOT mark snapshots processed.
- Forces snapshot pricing and requires a VIRTUAL replay user.
- Restricts executor and monitor reads to the selected userid, trading day,
  and underlying symbols so unrelated live/test trades cannot be touched.
- CLEAR_DATA defaults to False. When enabled, it clears the complete replay
  output tables before processing; when disabled, the replay continues from the
  configured database state.
- The configured trades database is used directly; there is no replay-specific
  database allowlist.

Default COFORGE adaptive multi-instrument command (PowerShell)
--------------------------------------------------

    $env:PYTHONPATH = "$PWD;$PWD\\tests"
    python tests/replays/replay_signal_trade_pipeline.py

The defaults are declared near the top of this file. Every value can be
overridden from the command line, for example ``--test-mode SIGNAL_EXIT``.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import csv
from datetime import date, datetime, time, timedelta
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import or_

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from configs.execution_config import EXECUTION_CONFIG
from configs.monitor_config import MONITOR_CONFIG
from configs.signal_config import SIGNAL_CONFIG
from database.database import get_trades_db, trades_engine
from enums.enums import EntryStatus, ExitStatus
from logconfig import setup_logging
from models.trade_models import AuditLog as AuditLogORM
from models.trade_models import Signal as SignalORM
from models.trade_models import StockOpportunity as StockOpportunityORM
from models.trade_models import UserTrade as UserTradeORM
from schemas.signal import SignalSchema
from schemas.snapshot import SnapshotSchema
from schemas.user import UserSchema
from schemas.user_trade import TradeManagementSchema, UserTradeSchema
from services.signals.signal_generator import SignalGenerator
from services.trade.executor import trade_executor as executor_module
from services.trade.executor.trade_executor import TradeExecutor
from services.trade.generator import tradegen_helper as tradegen_helper_module
from services.trade.generator import tradegen_validator as tradegen_validator_module
from services.account_governor.reporting import (
    empty_account_governor_fields,
    flatten_account_governor_audit_payload,
)
from services.trade.generator.trade_generator import TradeGenerator
from services.trade.monitor.trade_monitor import TradeMonitor
from utils.json_utils import sanitize_json
from tests.replays.replay_execution_prices import (
    DEFAULT_REPLAY_EXECUTION_PRICE_SOURCE,
    SUPPORTED_REPLAY_PRICE_SOURCES,
    replay_execution_price_source,
)

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

# Visible source defaults. Every value can be overridden from the command line.
DEFAULT_TRADING_DAY = "2026-07-27"
DEFAULT_SYMBOLS = "LT,BHEL,INDIGO,MAXHEALTH,PERSISTENT,PNBHOUSING,TCS"
DEFAULT_USERID = "DR1812"
DEFAULT_INSTRUMENT_CHOICE = "MULTI"
DEFAULT_TEST_MODE = "ADAPTIVE_EXIT"
DEFAULT_CLEAR_DATA = False
DEFAULT_REQUIRE_TRADE = True
DEFAULT_REQUIRE_EXIT = True
DEFAULT_REQUIRE_DERIVATIVES = True
DEFAULT_START_TIME: Optional[str] = None
DEFAULT_END_TIME: Optional[str] = None
DEFAULT_BATCH_SIZE = 500
DEFAULT_REPORT_DIR = "reports"
DEFAULT_LOG_FILE: Optional[str] = None
DEFAULT_EXECUTION_PRICE_SOURCE = DEFAULT_REPLAY_EXECUTION_PRICE_SOURCE

_EXPECTED_TRADEGEN_NONFATAL = {
    "TRADE_DECISION_NOT_ALLOWED",
    "SIGNAL_ALREADY_DEPLOYED",
}


def _resolve_snapshot_symbol_scope(
    requested_symbols: Sequence[str],
    snapshots: Sequence[Any],
) -> Tuple[List[str], List[str]]:
    """Return replayable and missing symbols without changing request order."""

    observed_symbols = {
        str(snapshot.symbol).strip().upper()
        for snapshot in snapshots
        if str(snapshot.symbol).strip()
    }
    replayed_symbols = [
        symbol for symbol in requested_symbols if symbol in observed_symbols
    ]
    missing_symbols = [
        symbol for symbol in requested_symbols if symbol not in observed_symbols
    ]
    return replayed_symbols, missing_symbols


def _args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Replay persisted snapshots through SignalGenerator, "
            "TradeGenerator, virtual TradeExecutor, and strict TradeMonitor. "
            "All options have visible source defaults and command-line overrides."
        )
    )
    parser.add_argument(
        "--day", "--date",
        dest="date",
        default=DEFAULT_TRADING_DAY,
        help=f"Trading day YYYY-MM-DD (default: {DEFAULT_TRADING_DAY})",
    )
    parser.add_argument(
        "--symbols",
        default=DEFAULT_SYMBOLS,
        help=f"Comma-separated equity symbols (default: {DEFAULT_SYMBOLS})",
    )
    parser.add_argument(
        "--userid",
        default=DEFAULT_USERID,
        help=f"Dedicated VIRTUAL replay userid (default: {DEFAULT_USERID})",
    )
    parser.add_argument(
        "--instrument-choice",
        default=DEFAULT_INSTRUMENT_CHOICE,
        choices=("EQ", "FUT", "CE", "PE", "MULTI"),
        help=f"Signal trade package (default: {DEFAULT_INSTRUMENT_CHOICE})",
    )
    parser.add_argument(
        "--test-mode",
        default=DEFAULT_TEST_MODE,
        choices=("SIGNAL_EXIT", "ADAPTIVE_EXIT"),
        help=(
            "SIGNAL_EXIT disables adaptive target/stop exits so the Auction "
            "signal lifecycle must close the trade; ADAPTIVE_EXIT keeps normal "
            f"monitor exits enabled (default: {DEFAULT_TEST_MODE})"
        ),
    )
    parser.add_argument(
        "--clear-data",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_CLEAR_DATA,
        help=(
            "Clear complete auditlog, user_trades, signals, and "
            "stock_opportunities tables before replay "
            f"(default: {DEFAULT_CLEAR_DATA})"
        ),
    )
    parser.add_argument(
        "--require-trade",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_REQUIRE_TRADE,
        help=f"Require at least one created trade (default: {DEFAULT_REQUIRE_TRADE})",
    )
    parser.add_argument(
        "--require-exit",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_REQUIRE_EXIT,
        help=f"Require at least one FILLED exit (default: {DEFAULT_REQUIRE_EXIT})",
    )
    parser.add_argument(
        "--require-derivatives",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_REQUIRE_DERIVATIVES,
        help=(
            "Require strict FUT and directional-option creation/pricing validation "
            f"(default: {DEFAULT_REQUIRE_DERIVATIVES})"
        ),
    )
    parser.add_argument(
        "--start-time",
        default=DEFAULT_START_TIME,
        help="Optional inclusive HH:MM[:SS] filter",
    )
    parser.add_argument(
        "--end-time",
        default=DEFAULT_END_TIME,
        help="Optional inclusive HH:MM[:SS] filter",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Snapshot fetch batch size (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--execution-price-source",
        default=DEFAULT_EXECUTION_PRICE_SOURCE,
        choices=SUPPORTED_REPLAY_PRICE_SOURCES,
        help=(
            "Virtual replay fill source. 1m_candle uses exact persisted one-minute "
            "candle OPEN with no fallback; snapshot preserves prior replay behavior "
            f"(default: {DEFAULT_EXECUTION_PRICE_SOURCE})"
        ),
    )
    parser.add_argument("--report-dir", default=DEFAULT_REPORT_DIR)
    parser.add_argument("--log-file", default=DEFAULT_LOG_FILE)
    return parser.parse_args(argv)


def _symbols(raw: str) -> List[str]:
    values = sorted({item.strip().upper() for item in raw.split(",") if item.strip()})
    if not values:
        raise ValueError("At least one symbol is required")
    return values


def _parse_clock(raw: Optional[str]) -> Optional[time]:
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    return time.fromisoformat(value)


def _day_bounds(trading_day: date) -> Tuple[datetime, datetime]:
    start = datetime.combine(trading_day, time.min)
    return start, start + timedelta(days=1)


def _enum_str(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().upper()


def _required_mapping(value: Any, path: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{path} must be an object")
    return value


def _required_value(mapping: Dict[str, Any], key: str, path: str) -> Any:
    if key not in mapping:
        raise ValueError(f"{path}.{key} is required")
    return mapping[key]


def _optional_value(mapping: Dict[str, Any], key: str, default: Any = None) -> Any:
    return mapping[key] if key in mapping else default


def _write_csv(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    data = [sanitize_json(row) for row in rows]
    path.parent.mkdir(parents=True, exist_ok=True)
    if not data:
        path.write_text("", encoding="utf-8")
        return
    fields = sorted({key for row in data for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(data)


def _load_snapshots(
    *,
    trading_day: date,
    symbols: List[str],
    batch_size: int,
    start_clock: Optional[time],
    end_clock: Optional[time],
) -> List[SnapshotSchema]:
    output: List[SnapshotSchema] = []
    after_time: Optional[datetime] = None
    after_symbol = ""
    limit = max(1, int(batch_size))

    while True:
        batch = SnapshotSchema.fetch_day_replay_batch(
            trading_day=trading_day,
            after_time=after_time,
            after_symbol=after_symbol,
            symbols=symbols,
            limit=limit,
        )
        if not batch:
            break
        output.extend(batch)
        last = batch[-1]
        after_time = last.snapshot_time.replace(tzinfo=None)
        after_symbol = last.symbol
        if len(batch) < limit:
            break

    filtered: List[SnapshotSchema] = []
    for snapshot in output:
        observed = snapshot.snapshot_time.astimezone(IST).time()
        if start_clock is not None and observed < start_clock:
            continue
        if end_clock is not None and observed > end_clock:
            continue
        filtered.append(snapshot)

    filtered.sort(key=lambda row: (row.snapshot_time, row.symbol))
    return filtered


def _validate_replay_user(userid: str, instrument_choice: str) -> UserSchema:
    user = UserSchema.fetch_user(userid)
    if user is None:
        raise LookupError(f"Replay user not found: {userid}")

    failures: List[str] = []
    if int(getattr(user, "active", 0) or 0) != 1:
        failures.append("users.active must be 1")
    if int(getattr(user, "logged_in", 0) or 0) != 1:
        failures.append("users.logged_in must be 1")
    if int(getattr(user, "autotrade", 0) or 0) != 1:
        failures.append("users.autotrade must be 1")
    if _enum_str(getattr(user, "execution_mode", None)) != "VIRTUAL":
        failures.append("users.execution_mode must be VIRTUAL")

    requires_equity = instrument_choice in {"EQ", "MULTI"}
    requires_futures = instrument_choice in {"FUT", "MULTI"}
    requires_options = instrument_choice in {"CE", "PE", "MULTI"}
    if requires_equity and int(getattr(user, "equity", 0) or 0) != 1:
        failures.append("users.equity must be 1")
    if requires_futures and int(getattr(user, "futures", 0) or 0) != 1:
        failures.append("users.futures must be 1")
    if requires_options and int(getattr(user, "options", 0) or 0) != 1:
        failures.append("users.options must be 1")

    if failures:
        raise ValueError(
            f"Replay user {userid} is not eligible: " + "; ".join(failures)
        )
    return user


def _scope_signal_query(db: Any, trading_day: date, symbols: List[str], lifecycle: str) -> Any:
    start, end = _day_bounds(trading_day)
    return (
        db.query(SignalORM)
        .filter(SignalORM.symbol.in_(symbols))
        .filter(SignalORM.lifecycle == lifecycle)
        .filter(SignalORM.first_seen_time >= start)
        .filter(SignalORM.first_seen_time < end)
    )


def _scope_trade_query(db: Any, trading_day: date, symbols: List[str], userid: str) -> Any:
    start, end = _day_bounds(trading_day)
    return (
        db.query(UserTradeORM)
        .filter(UserTradeORM.userid == userid)
        .filter(UserTradeORM.equity_ref.in_(symbols))
        .filter(UserTradeORM.entry_time >= start)
        .filter(UserTradeORM.entry_time < end)
    )


def _audit_scope_values(
    *,
    symbols: Sequence[str],
    signals: Sequence[Any] = (),
    trades: Sequence[Any] = (),
) -> Tuple[List[str], List[str], List[str]]:
    """Return deterministic signal, trade, and package-symbol audit scope.

    Audit rows are not guaranteed to use the underlying equity symbol. Trade
    executor and monitor rows use each package leg's actual instrument symbol,
    while signal and trade-generator rows may be keyed by signal/trade entity
    IDs. Build the complete replay scope from persisted replay rows so cleanup,
    export, and validation all operate on the same package.
    """

    signal_ids = sorted(
        {
            str(getattr(signal, "signal_id", "") or "").strip()
            for signal in signals
            if str(getattr(signal, "signal_id", "") or "").strip()
        }
    )
    trade_ids = sorted(
        {
            str(getattr(trade, "id", "") or "").strip()
            for trade in trades
            if str(getattr(trade, "id", "") or "").strip()
        }
    )
    package_symbols = sorted(
        {
            str(value or "").strip().upper()
            for value in (
                list(symbols)
                + [getattr(trade, "equity_ref", None) for trade in trades]
                + [getattr(trade, "symbol", None) for trade in trades]
            )
            if str(value or "").strip()
        }
    )
    return signal_ids, trade_ids, package_symbols


def _scope_audit_query(
    db: Any,
    trading_day: date,
    symbols: List[str],
    userid: str,
    *,
    signal_ids: Sequence[str] = (),
    trade_ids: Sequence[str] = (),
    instrument_symbols: Sequence[str] = (),
) -> Any:
    start, end = _day_bounds(trading_day)
    scoped_symbols = sorted(
        {
            str(value or "").strip().upper()
            for value in list(symbols) + list(instrument_symbols)
            if str(value or "").strip()
        }
    )
    scoped_entity_ids = sorted(
        {
            str(value or "").strip()
            for value in list(signal_ids) + list(trade_ids)
            if str(value or "").strip()
        }
    )

    identity_filters = []
    if scoped_symbols:
        identity_filters.append(AuditLogORM.symbol.in_(scoped_symbols))
    if scoped_entity_ids:
        identity_filters.append(AuditLogORM.entity_id.in_(scoped_entity_ids))
    if not identity_filters:
        raise ValueError("Audit replay scope requires symbols or entity IDs")

    return (
        db.query(AuditLogORM)
        .filter(or_(*identity_filters))
        .filter(AuditLogORM.ts >= start)
        .filter(AuditLogORM.ts < end)
        .filter(or_(AuditLogORM.userid == userid, AuditLogORM.userid.is_(None)))
    )


def _configured_database_name() -> str:
    database_name = str(trades_engine.url.database or "").strip()
    return database_name or "UNKNOWN"


def _clear_data() -> Dict[str, int]:
    """Clear replay output tables; persisted snapshots and market data remain."""
    with get_trades_db() as db:
        audit_deleted = int(
            db.query(AuditLogORM).delete(synchronize_session=False)
        )
        trades_deleted = int(
            db.query(UserTradeORM).delete(synchronize_session=False)
        )
        opportunities_deleted = int(
            db.query(StockOpportunityORM).delete(synchronize_session=False)
        )
        signals_deleted = int(
            db.query(SignalORM).delete(synchronize_session=False)
        )
        db.commit()
    return {
        "signals": signals_deleted,
        "opportunities": opportunities_deleted,
        "trades": trades_deleted,
        "audit": audit_deleted,
    }


def _signals_in_scope(
    *, trading_day: date, symbols: List[str], lifecycle: str
) -> List[SignalSchema]:
    with get_trades_db() as db:
        rows = (
            _scope_signal_query(db, trading_day, symbols, lifecycle)
            .order_by(SignalORM.first_seen_time.asc(), SignalORM.id.asc())
            .all()
        )
    return [SignalSchema.model_validate(row) for row in rows]


def _latest_signal_for_symbol(
    *, trading_day: date, symbol: str, lifecycle: str
) -> Optional[SignalSchema]:
    with get_trades_db() as db:
        row = (
            _scope_signal_query(db, trading_day, [symbol], lifecycle)
            .order_by(SignalORM.id.desc())
            .first()
        )
    return SignalSchema.model_validate(row) if row is not None else None


def _trades_in_scope(
    *, trading_day: date, symbols: List[str], userid: str
) -> List[UserTradeSchema]:
    with get_trades_db() as db:
        rows = (
            _scope_trade_query(db, trading_day, symbols, userid)
            .order_by(UserTradeORM.entry_time.asc(), UserTradeORM.id.asc())
            .all()
        )
    return [UserTradeSchema.model_validate(row) for row in rows]


def _trades_for_signal(userid: str, signal_id: str) -> List[UserTradeSchema]:
    with get_trades_db() as db:
        rows = (
            db.query(UserTradeORM)
            .filter(UserTradeORM.userid == userid)
            .filter(UserTradeORM.signal_id == signal_id)
            .order_by(UserTradeORM.id.asc())
            .all()
        )
    return [UserTradeSchema.model_validate(row) for row in rows]


def _scoped_open_positions(
    *, trading_day: date, symbols: List[str], userid: str
) -> List[UserTradeSchema]:
    start, end = _day_bounds(trading_day)
    with get_trades_db() as db:
        rows = (
            db.query(UserTradeORM)
            .filter(UserTradeORM.userid == userid)
            .filter(UserTradeORM.equity_ref.in_(symbols))
            .filter(UserTradeORM.entry_time >= start)
            .filter(UserTradeORM.entry_time < end)
            .filter(UserTradeORM.entry_status == EntryStatus.FILLED.value)
            .filter(
                or_(
                    UserTradeORM.exit_status.is_(None),
                    UserTradeORM.exit_status != ExitStatus.FILLED.value,
                )
            )
            .order_by(UserTradeORM.entry_time.asc(), UserTradeORM.id.asc())
            .all()
        )
    return [UserTradeSchema.model_validate(row) for row in rows]


def _scoped_executor_candidates(
    *, trading_day: date, symbols: List[str], userid: str, limit: int
) -> List[UserTradeSchema]:
    start, end = _day_bounds(trading_day)
    actionable = or_(
        UserTradeORM.entry_status.in_([EntryStatus.READY.value, EntryStatus.SUBMITTED.value]),
        UserTradeORM.exit_status.in_([ExitStatus.READY.value, ExitStatus.SUBMITTED.value]),
    )
    with get_trades_db() as db:
        rows = (
            db.query(UserTradeORM)
            .filter(UserTradeORM.userid == userid)
            .filter(UserTradeORM.equity_ref.in_(symbols))
            .filter(UserTradeORM.entry_time >= start)
            .filter(UserTradeORM.entry_time < end)
            .filter(actionable)
            .order_by(UserTradeORM.id.asc())
            .limit(max(1, int(limit)))
            .all()
        )
    return [UserTradeSchema.model_validate(row) for row in rows]


@contextmanager
def _restrict_trade_services(
    *, trading_day: date, symbols: List[str], userid: str
) -> Iterator[None]:
    """Restrict monitor/executor reads to this replay's test scope."""

    original_open_positions = UserTradeSchema.__dict__["fetch_open_positions"]
    original_executor_fetch = executor_module._fetch_candidates_for_user

    def fetch_open_positions(
        *, userid: Optional[str] = None, symbol: Optional[str] = None
    ) -> List[UserTradeSchema]:
        if userid is not None and str(userid).strip() != str(replay_userid).strip():
            raise ValueError("Replay monitor attempted to read another userid")
        rows = _scoped_open_positions(
            trading_day=trading_day,
            symbols=symbols,
            userid=replay_userid,
        )
        if symbol is not None:
            wanted = str(symbol).strip().upper()
            rows = [row for row in rows if str(row.symbol).strip().upper() == wanted]
        return rows

    def fetch_executor_candidates(requested_userid: str, limit: int = 500) -> List[UserTradeSchema]:
        if str(requested_userid).strip() != str(replay_userid).strip():
            raise ValueError("Replay executor attempted to read another userid")
        return _scoped_executor_candidates(
            trading_day=trading_day,
            symbols=symbols,
            userid=replay_userid,
            limit=limit,
        )

    replay_userid = userid
    UserTradeSchema.fetch_open_positions = staticmethod(fetch_open_positions)
    executor_module._fetch_candidates_for_user = fetch_executor_candidates
    try:
        yield
    finally:
        UserTradeSchema.fetch_open_positions = original_open_positions
        executor_module._fetch_candidates_for_user = original_executor_fetch



@contextmanager
def _deterministic_replay_clock() -> Iterator[Any]:
    """Route generator/executor wall-clock helpers to the active snapshot time."""
    clock: Dict[str, Optional[datetime]] = {"current": None}
    original_helper_now = tradegen_helper_module.business_now_naive
    original_validator_now = tradegen_validator_module.business_now_naive
    original_executor_now = executor_module._now_ist_naive

    def replay_now() -> datetime:
        current = clock["current"]
        if current is None:
            raise RuntimeError("replay clock used before a snapshot time was assigned")
        return current.astimezone(IST).replace(tzinfo=None) if current.tzinfo else current

    def set_time(value: datetime) -> None:
        if not isinstance(value, datetime):
            raise TypeError("replay clock requires datetime snapshot_time")
        clock["current"] = value

    tradegen_helper_module.business_now_naive = replay_now
    tradegen_validator_module.business_now_naive = replay_now
    executor_module._now_ist_naive = replay_now
    try:
        yield set_time
    finally:
        tradegen_helper_module.business_now_naive = original_helper_now
        tradegen_validator_module.business_now_naive = original_validator_now
        executor_module._now_ist_naive = original_executor_now


@contextmanager
def _monitor_test_policy(test_mode: str) -> Iterator[None]:
    """Temporarily select normal adaptive exit or signal-lifecycle exit testing."""
    mode = str(test_mode).strip().upper()
    tm = MONITOR_CONFIG.trade_management
    old_target = bool(tm.exit_on_current_target)
    old_stop = bool(tm.exit_on_current_stop)
    if mode == "SIGNAL_EXIT":
        tm.exit_on_current_target = False
        tm.exit_on_current_stop = False
    elif mode != "ADAPTIVE_EXIT":
        raise ValueError(f"Unsupported replay test mode: {mode}")
    try:
        yield
    finally:
        tm.exit_on_current_target = old_target
        tm.exit_on_current_stop = old_stop

def _signal_context(signal: Optional[SignalSchema]) -> Dict[str, Any]:
    if signal is None:
        return {
            "signal_id": None,
            "signal_stage": None,
            "signal_status": None,
            "signal_reason": None,
            "management_posture": None,
            "lifecycle_trade_action": None,
            "should_exit_signal": None,
            "signal_auction_action": None,
            "signal_auction_state": None,
            "signal_directional_alignment": None,
            "opportunity_key": None,
            "candidate_id": None,
            "boundary_event_key": None,
        }

    meta = _required_mapping(signal.meta_json, "signal.meta_json")
    contract = _required_mapping(
        _required_value(meta, "downstream_contract", "signal.meta_json"),
        "signal.meta_json.downstream_contract",
    )
    version = str(
        _required_value(contract, "version", "signal.meta_json.downstream_contract")
    ).strip()
    if version != "AUCTION_SIGNAL_DOWNSTREAM_V2":
        raise ValueError(f"Unsupported signal downstream contract: {version}")

    lifecycle = _required_mapping(
        _required_value(meta, "lifecycle", "signal.meta_json"),
        "signal.meta_json.lifecycle",
    )
    management = _required_mapping(
        _required_value(meta, "management", "signal.meta_json"),
        "signal.meta_json.management",
    )
    setup_levels = _required_mapping(
        _required_value(meta, "setup_levels", "signal.meta_json"),
        "signal.meta_json.setup_levels",
    )

    return {
        "signal_id": signal.signal_id,
        "signal_stage": signal.stage.value,
        "signal_status": signal.status.value,
        "signal_reason": signal.status_reason,
        "management_posture": _required_value(
            management, "action", "signal.meta_json.management"
        ),
        "lifecycle_trade_action": _required_value(
            lifecycle, "trade_action", "signal.meta_json.lifecycle"
        ),
        "should_exit_signal": _required_value(
            management, "should_exit_signal", "signal.meta_json.management"
        ),
        "signal_auction_action": _required_value(
            management, "auction_action", "signal.meta_json.management"
        ),
        "signal_auction_state": _required_value(
            management, "auction_state", "signal.meta_json.management"
        ),
        "signal_directional_alignment": _required_value(
            management, "directional_alignment", "signal.meta_json.management"
        ),
        "opportunity_key": _required_value(
            setup_levels, "opportunity_key", "signal.meta_json.setup_levels"
        ),
        "candidate_id": _required_value(
            setup_levels, "candidate_id", "signal.meta_json.setup_levels"
        ),
        "boundary_event_key": _required_value(
            setup_levels, "boundary_event_key", "signal.meta_json.setup_levels"
        ),
    }


def _trade_row(trade: UserTradeSchema) -> Dict[str, Any]:
    management_raw = trade.trade_management
    if management_raw is None:
        raise ValueError(f"Trade {trade.id} is missing trade_management")
    management = TradeManagementSchema.model_validate(management_raw).model_dump(
        mode="python"
    )
    if management["version"] != 2 or management["mode"] != "AUCTION_ADAPTIVE_V2":
        raise ValueError(
            f"Trade {trade.id} has unsupported management contract: "
            f"version={management['version']} mode={management['mode']}"
        )

    return {
        "trade_id": trade.id,
        "userid": trade.userid,
        "signal_id": trade.signal_id,
        "source": trade.source,
        "symbol": trade.symbol,
        "equity_ref": trade.equity_ref,
        "instrument_type": trade.instrument_type.value,
        "trade_type": trade.trade_type.value,
        "execution_mode": trade.execution_mode,
        "entry_status": trade.entry_status.value,
        "exit_status": trade.exit_status.value,
        "entry_time": trade.entry_time,
        "entry_intent_time": trade.entry_intent_time,
        "entry_exec_time": trade.entry_exec_time,
        "planned_entry_price": trade.entry_price,
        "executed_entry_price": trade.executed_entry_price,
        "quantity": trade.quantity,
        "last_time": trade.last_time,
        "last_price": trade.last_price,
        "last_pnl": trade.last_pnl,
        "last_pnl_value": trade.last_pnl_value,
        "max_price": trade.max_price,
        "min_price": trade.min_price,
        "exit_reason": trade.exit_reason,
        "exit_rule": trade.exit_rule,
        "exit_intent_time": trade.exit_intent_time,
        "exit_exec_time": trade.exit_exec_time,
        "executed_exit_price": trade.executed_exit_price,
        "executed_exit_qty": trade.executed_exit_qty,
        "exit_pnl": trade.exit_pnl,
        "trade_management_version": management["version"],
        "trade_management_mode": management["mode"],
        "trade_posture": management["posture"],
        "management_posture": management["management_posture"],
        "management_reason_code": management["management_reason_code"],
        "signal_stage": management["signal_stage"],
        "signal_status": management["signal_status"],
        "lifecycle_trade_action": management["lifecycle_trade_action"],
        "directional_alignment": management["directional_alignment"],
        "auction_action": management["auction_action"],
        "auction_state": management["auction_state"],
        "should_exit_signal": management["should_exit_signal"],
        "current_stop_price": management["current_stop_price"],
        "current_target_price": management["current_target_price"],
        "current_profit_r": management["current_profit_r"],
        "mfe_profit_r": management["mfe_profit_r"],
        "group_role": management["group_role"],
        "last_update_reason": management["last_update_reason"],
    }


def _audit_rows(
    *,
    trading_day: date,
    symbols: List[str],
    userid: str,
    signals: Sequence[Any],
    trades: Sequence[Any],
) -> List[Dict[str, Any]]:
    signal_ids, trade_ids, package_symbols = _audit_scope_values(
        symbols=symbols,
        signals=signals,
        trades=trades,
    )
    with get_trades_db() as db:
        rows = (
            _scope_audit_query(
                db,
                trading_day,
                symbols,
                userid,
                signal_ids=signal_ids,
                trade_ids=trade_ids,
                instrument_symbols=package_symbols,
            )
            .order_by(AuditLogORM.ts.asc(), AuditLogORM.id.asc())
            .all()
        )

    output: List[Dict[str, Any]] = []
    for row in rows:
        payload = sanitize_json(row.payload_json)
        governor_fields = empty_account_governor_fields()
        if str(row.entity_type or "").strip().upper() == "ACCOUNT_GOVERNOR":
            governor_fields = flatten_account_governor_audit_payload(payload)
        output.append(
            {
                "id": row.id,
                "ts": row.ts,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "symbol": row.symbol,
                "userid": row.userid,
                "evaluation_stage": row.evaluation_stage,
                "previous_state": row.previous_state,
                "new_state": row.new_state,
                "action": row.action,
                "reason_code": row.reason_code,
                "reason_text": row.reason_text,
                "confidence": row.confidence,
                "payload_json": json.dumps(payload, sort_keys=True),
                **governor_fields,
            }
        )
    return output


def _tradegen_outcome(result: Dict[str, Any]) -> Tuple[str, Optional[str], int]:
    if not isinstance(result, dict):
        raise TypeError("TradeGenerator result must be an object")
    ok = bool(_optional_value(result, "ok", False))
    error_raw = _optional_value(result, "error")
    error = str(error_raw).strip() if error_raw is not None else None
    created_count = int(_optional_value(result, "created_count", 0) or 0)
    if ok:
        return "CREATED", None, created_count
    if error in _EXPECTED_TRADEGEN_NONFATAL:
        return "NOT_ELIGIBLE", error, 0
    raise RuntimeError(f"Unexpected TradeGenerator failure: {result}")


def _validate_final_results(
    *,
    signals: List[SignalSchema],
    trades: List[UserTradeSchema],
    require_trade: bool,
    require_exit: bool,
) -> None:
    signal_ids = [signal.signal_id for signal in signals]
    if len(signal_ids) != len(set(signal_ids)):
        raise AssertionError("Duplicate signal_id values persisted")

    trade_identity = [
        (trade.userid, trade.signal_id, trade.instrument_type.value) for trade in trades
    ]
    if len(trade_identity) != len(set(trade_identity)):
        raise AssertionError("Duplicate userid/signal/instrument trade rows persisted")

    for trade in trades:
        _trade_row(trade)
        if _enum_str(trade.execution_mode) != "VIRTUAL":
            raise AssertionError(f"Replay created non-VIRTUAL trade {trade.id}")

    if require_trade and not trades:
        raise AssertionError("Replay expected at least one trade, but none was created")

    filled_entries = [
        trade for trade in trades if trade.entry_status.value == EntryStatus.FILLED.value
    ]
    if trades and not filled_entries:
        raise AssertionError("Trade rows were created but no entry reached FILLED")

    if require_exit:
        filled_exits = [
            trade for trade in trades if trade.exit_status.value == ExitStatus.FILLED.value
        ]
        if not filled_exits:
            raise AssertionError("Replay expected at least one FILLED exit")



def _validate_replay_timestamps(
    *, trades: List[UserTradeSchema], trading_day: date
) -> None:
    fields = (
        "entry_time",
        "entry_intent_time",
        "entry_exec_time",
        "entry_reconciled_at",
        "exec_last_checked_at",
        "exit_intent_time",
        "exit_exec_time",
        "exit_reconciled_at",
        "reconcile_last_checked_at",
        "last_time",
    )
    for trade in trades:
        for field in fields:
            value = getattr(trade, field, None)
            if value is None:
                continue
            if not isinstance(value, datetime):
                raise TypeError(f"trade {trade.id} {field} must be datetime")
            observed = value.astimezone(IST).date() if value.tzinfo else value.date()
            if observed != trading_day:
                raise AssertionError(
                    f"trade {trade.id} {field} escaped replay day: {value}"
                )

def _snapshot_time_naive_ist(value: datetime) -> datetime:
    return value.astimezone(IST).replace(tzinfo=None) if value.tzinfo else value


def _derivative_quote_for_trade(
    *, snapshot: SnapshotSchema, trade: UserTradeSchema
) -> Dict[str, Any]:
    instrument_type = trade.instrument_type.value
    derivatives = snapshot.derivatives.model_dump(mode="python")

    if instrument_type == "FUT":
        future = _required_mapping(
            _required_value(derivatives, "future", "snapshot.derivatives"),
            "snapshot.derivatives.future",
        )
        expected_symbol = str(
            _required_value(future, "instrument", "snapshot.derivatives.future")
        ).strip().upper()
        expected_price = float(
            _required_value(future, "last_price", "snapshot.derivatives.future")
        )
    elif instrument_type in {"CE", "PE"}:
        ladder = _required_mapping(
            _required_value(derivatives, "option_ladder", "snapshot.derivatives"),
            "snapshot.derivatives.option_ladder",
        )
        side_key = "calls" if instrument_type == "CE" else "puts"
        contracts = _required_value(
            ladder, side_key, "snapshot.derivatives.option_ladder"
        )
        if not isinstance(contracts, list):
            raise TypeError(
                f"snapshot.derivatives.option_ladder.{side_key} must be an array"
            )
        matches = []
        for contract in contracts:
            if not isinstance(contract, dict):
                raise TypeError(
                    f"snapshot.derivatives.option_ladder.{side_key} entries must be objects"
                )
            contract_symbol = str(
                _required_value(contract, "symbol", f"option_ladder.{side_key}")
            ).strip().upper()
            if contract_symbol == trade.symbol.strip().upper():
                matches.append(contract)
        if len(matches) != 1:
            raise ValueError(
                f"Expected exactly one derivative quote for {trade.symbol}; "
                f"found {len(matches)}"
            )
        contract = matches[0]
        expected_symbol = str(
            _required_value(contract, "symbol", f"option_ladder.{side_key}")
        ).strip().upper()
        expected_price = float(
            _required_value(contract, "ltp", f"option_ladder.{side_key}")
        )
    else:
        raise ValueError(
            f"Derivative quote validation does not support {instrument_type}"
        )

    if expected_price <= 0:
        raise ValueError(f"Derivative quote for {expected_symbol} must be positive")
    return {
        "expected_symbol": expected_symbol,
        "expected_entry_price": expected_price,
        "snapshot_time": snapshot.snapshot_time,
    }


def _derivative_validation_rows(
    *, snapshots: List[SnapshotSchema], trades: List[UserTradeSchema]
) -> List[Dict[str, Any]]:
    by_key = {
        (
            snapshot.symbol.strip().upper(),
            _snapshot_time_naive_ist(snapshot.snapshot_time),
        ): snapshot
        for snapshot in snapshots
    }
    rows: List[Dict[str, Any]] = []
    for trade in trades:
        instrument_type = trade.instrument_type.value
        if instrument_type not in {"FUT", "CE", "PE"}:
            continue
        # Derivative legs are priced when the package becomes READY.  Keep
        # entry_time as the signal-origin anchor and validate against the
        # later entry-intent snapshot whenever it is available.
        validation_time = trade.entry_intent_time or trade.entry_time
        if validation_time is None:
            raise ValueError(
                f"Derivative trade {trade.id} is missing entry_intent_time and entry_time"
            )
        validation_time_source = (
            "entry_intent_time"
            if trade.entry_intent_time is not None
            else "entry_time_fallback"
        )
        key = (
            trade.equity_ref.strip().upper(),
            _snapshot_time_naive_ist(validation_time),
        )
        if key not in by_key:
            raise LookupError(f"No entry snapshot for derivative trade {trade.id}: {key}")
        quote = _derivative_quote_for_trade(snapshot=by_key[key], trade=trade)
        planned = float(trade.entry_price)
        executed = (
            float(trade.executed_entry_price)
            if trade.executed_entry_price is not None
            else None
        )
        tolerance = max(1e-9, abs(quote["expected_entry_price"]) * 1e-9)
        symbol_matches = trade.symbol.strip().upper() == quote["expected_symbol"]
        planned_matches = abs(planned - quote["expected_entry_price"]) <= tolerance
        executed_matches = (
            executed is not None
            and abs(executed - quote["expected_entry_price"]) <= tolerance
        )
        rows.append(
            {
                "trade_id": trade.id,
                "instrument_type": instrument_type,
                "trade_symbol": trade.symbol,
                "validation_time_source": validation_time_source,
                "validation_time": validation_time,
                "entry_snapshot_time": quote["snapshot_time"],
                "expected_symbol": quote["expected_symbol"],
                "expected_entry_price": quote["expected_entry_price"],
                "planned_entry_price": planned,
                "executed_entry_price": executed,
                "symbol_matches": symbol_matches,
                "planned_price_matches": planned_matches,
                "executed_price_matches": executed_matches,
                "validation_status": (
                    "PASSED"
                    if symbol_matches and planned_matches and executed_matches
                    else "FAILED"
                ),
            }
        )
    return rows


def _as_naive_ist_datetime(value: Any, *, context: str) -> datetime:
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise ValueError(f"{context} cannot be blank")
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise TypeError(f"{context} must be datetime")
    return _snapshot_time_naive_ist(value)


def _validate_account_governor_timestamps(
    *,
    rows: List[Dict[str, Any]],
    trades: List[UserTradeSchema],
    trading_day: date,
) -> None:
    package_intents: Dict[Tuple[str, str], set[datetime]] = {}
    for trade in trades:
        signal_id = str(trade.signal_id or "").strip()
        userid = str(trade.userid or "").strip().upper()
        if not signal_id or trade.entry_intent_time is None:
            continue
        key = (userid, signal_id)
        package_intents.setdefault(key, set()).add(
            _as_naive_ist_datetime(
                trade.entry_intent_time,
                context=f"trade {trade.id} entry_intent_time",
            )
        )

    for row in rows:
        as_of = _as_naive_ist_datetime(
            row["account_governor_as_of"],
            context=f"Account Governor row {row['id']} as_of",
        )
        audit_ts = _as_naive_ist_datetime(
            row["ts"],
            context=f"Account Governor row {row['id']} ts",
        )
        if as_of.date() != trading_day or audit_ts.date() != trading_day:
            raise AssertionError(
                f"Account Governor row {row['id']} escaped replay day: "
                f"as_of={as_of} ts={audit_ts}"
            )
        if as_of != audit_ts:
            raise AssertionError(
                f"Account Governor row {row['id']} audit timestamp mismatch: "
                f"as_of={as_of} ts={audit_ts}"
            )

        key = (
            str(row["userid"] or "").strip().upper(),
            str(row["entity_id"] or "").strip(),
        )
        intent_times = package_intents.get(key, set())
        if len(intent_times) != 1:
            raise AssertionError(
                f"Account Governor row {row['id']} expected one package "
                f"entry_intent_time for {key}; found {sorted(intent_times)}"
            )
        intent_time = next(iter(intent_times))
        if as_of != intent_time:
            raise AssertionError(
                f"Account Governor row {row['id']} authorization time does not "
                f"match package entry_intent_time: as_of={as_of} "
                f"entry_intent_time={intent_time}"
            )


def _validate_derivative_coverage(
    *, trades: List[UserTradeSchema], rows: List[Dict[str, Any]]
) -> None:
    instruments = {trade.instrument_type.value for trade in trades}
    if "FUT" not in instruments:
        raise AssertionError("Derivative replay did not create a FUT trade")
    if not ({"CE", "PE"} & instruments):
        raise AssertionError("Derivative replay did not create an option trade")
    failed = [row for row in rows if row["validation_status"] != "PASSED"]
    if failed:
        raise AssertionError(f"Derivative entry quote validation failed: {failed}")


def _record_validation_failure(
    failures: List[Dict[str, Any]],
    *,
    code: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    row = {
        "code": str(code).strip().upper(),
        "message": str(message).strip(),
        "details": sanitize_json(details or {}),
    }
    failures.append(row)
    logger.error(
        "REPLAY_VALIDATION_FAILED | code=%s | message=%s | details=%s",
        row["code"],
        row["message"],
        json.dumps(row["details"], sort_keys=True, default=str),
    )


def _capture_validation(
    failures: List[Dict[str, Any]],
    *,
    code: str,
    validator: Any,
) -> None:
    try:
        validator()
    except Exception as exc:
        _record_validation_failure(
            failures,
            code=code,
            message=str(exc),
            details={"error_type": type(exc).__name__},
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _args(argv)
    trading_day = date.fromisoformat(args.date)
    symbols = _symbols(args.symbols)
    userid = str(args.userid).strip()
    if not userid:
        raise ValueError("--userid cannot be blank")
    lifecycle = SIGNAL_CONFIG.default_lifecycle.strip().upper()
    instrument_choice = str(args.instrument_choice).strip().upper()
    test_mode = str(args.test_mode).strip().upper()
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    log_file = args.log_file or str(report_dir / "replay_signal_trade_pipeline.log")
    setup_logging(log_file=log_file)
    database_name = _configured_database_name()
    global logger
    logger = logging.getLogger(__name__)

    logger.info(
        "Resolved replay configuration | database=%s date=%s symbols=%s userid=%s "
        "instrument=%s test_mode=%s clear_data=%s require_trade=%s "
        "require_exit=%s require_derivatives=%s execution_price_source=%s report_dir=%s",
        database_name,
        trading_day,
        symbols,
        userid,
        instrument_choice,
        test_mode,
        bool(args.clear_data),
        bool(args.require_trade),
        bool(args.require_exit),
        bool(args.require_derivatives),
        args.execution_price_source,
        report_dir,
    )
    _validate_replay_user(userid, instrument_choice)

    requested_symbols = list(symbols)
    snapshots = _load_snapshots(
        trading_day=trading_day,
        symbols=requested_symbols,
        batch_size=max(1, int(args.batch_size)),
        start_clock=_parse_clock(args.start_time),
        end_clock=_parse_clock(args.end_time),
    )
    symbols, missing_symbols = _resolve_snapshot_symbol_scope(
        requested_symbols, snapshots
    )

    if not symbols:
        logger.error(
            "REPLAY_PREFLIGHT_FAILED | no stored snapshots for requested scope | "
            "day=%s requested_symbols=%s | no data was cleared",
            trading_day,
            requested_symbols,
        )
        return 2

    if missing_symbols:
        logger.warning(
            "REPLAY_SYMBOLS_SKIPPED | no snapshots for selected symbols | "
            "day=%s skipped=%s replayed=%s",
            trading_day,
            missing_symbols,
            symbols,
        )

    cleared = {"signals": 0, "opportunities": 0, "trades": 0, "audit": 0}
    if args.clear_data:
        cleared = _clear_data()

    old_use_snapshot = EXECUTION_CONFIG.use_snapshot
    old_live_virtual = EXECUTION_CONFIG.use_live_price_for_virtual
    EXECUTION_CONFIG.use_snapshot = True
    EXECUTION_CONFIG.use_live_price_for_virtual = False

    timeline_rows: List[Dict[str, Any]] = []
    trade_observation_rows: List[Dict[str, Any]] = []
    signal_action_counts: Counter[str] = Counter()
    signal_stage_counts: Counter[str] = Counter()
    tradegen_outcome_counts: Counter[str] = Counter()
    executor_entry_total = 0
    monitor_update_total = 0
    monitor_error_total = 0
    monitor_error_rows: List[Dict[str, Any]] = []
    executor_exit_total = 0
    first_signal_exit_request_time: Optional[datetime] = None

    trade_generator = TradeGenerator()
    trade_executor = TradeExecutor()
    trade_monitor = TradeMonitor()

    logger.info(
        "Starting strict Auction signal/trade replay | day=%s requested_symbols=%s "
        "replayed_symbols=%s skipped_symbols=%s userid=%s instrument=%s "
        "test_mode=%s snapshots=%d cleared=%s",
        trading_day,
        requested_symbols,
        symbols,
        missing_symbols,
        userid,
        instrument_choice,
        test_mode,
        len(snapshots),
        cleared,
    )

    try:
        with _restrict_trade_services(
            trading_day=trading_day,
            symbols=symbols,
            userid=userid,
        ), _deterministic_replay_clock() as set_replay_time, _monitor_test_policy(test_mode), replay_execution_price_source(args.execution_price_source):
            for index, snapshot in enumerate(snapshots, start=1):
                set_replay_time(snapshot.snapshot_time)
                signal_action = SignalGenerator(snapshot).generate() or "NO_ACTION"
                signal_action_counts[signal_action] += 1

                signal = _latest_signal_for_symbol(
                    trading_day=trading_day,
                    symbol=snapshot.symbol,
                    lifecycle=lifecycle,
                )
                signal_ctx = _signal_context(signal)
                if signal_ctx["signal_stage"] is not None:
                    signal_stage_counts[str(signal_ctx["signal_stage"])] += 1
                if (
                    signal_ctx["should_exit_signal"] is True
                    and first_signal_exit_request_time is None
                ):
                    first_signal_exit_request_time = snapshot.snapshot_time

                tradegen_attempted = False
                tradegen_outcome = "NO_SIGNAL"
                tradegen_error: Optional[str] = None
                tradegen_created_count = 0

                if signal is not None:
                    existing = _trades_for_signal(userid, signal.signal_id)
                    if existing:
                        tradegen_outcome = "ALREADY_DEPLOYED"
                    else:
                        tradegen_attempted = True
                        result = trade_generator.generate_for_user_signal(
                            userid=userid,
                            signal_id=signal.signal_id,
                            instrument_choice=instrument_choice,
                            source="TRADE_GENERATOR",
                        )
                        (
                            tradegen_outcome,
                            tradegen_error,
                            tradegen_created_count,
                        ) = _tradegen_outcome(result)
                    tradegen_outcome_counts[tradegen_outcome] += 1

                entry_updates = trade_executor.execute_user_once(
                    userid=userid,
                    limit=500,
                    snapshot_time=snapshot.snapshot_time,
                )
                executor_entry_total += int(entry_updates or 0)

                monitor_updates = trade_monitor.run_once(
                    snapshot_time=snapshot.snapshot_time
                )
                monitor_update_total += int(monitor_updates or 0)
                pass_monitor_errors = list(trade_monitor.last_pass_errors)
                monitor_error_total += len(pass_monitor_errors)
                for monitor_error in pass_monitor_errors:
                    monitor_error_rows.append(
                        {
                            "snapshot_index": index,
                            "snapshot_time": snapshot.snapshot_time,
                            **monitor_error,
                        }
                    )

                exit_updates = trade_executor.execute_user_once(
                    userid=userid,
                    limit=500,
                    snapshot_time=snapshot.snapshot_time,
                )
                executor_exit_total += int(exit_updates or 0)

                current_trades = _trades_in_scope(
                    trading_day=trading_day,
                    symbols=symbols,
                    userid=userid,
                )
                entry_status_counts = Counter(
                    trade.entry_status.value for trade in current_trades
                )
                exit_status_counts = Counter(
                    trade.exit_status.value for trade in current_trades
                )

                auction_projection = snapshot.auction
                if (
                    auction_projection.status != "OK"
                    or auction_projection.directional is None
                    or auction_projection.balance is None
                ):
                    raise ValueError(
                        "snapshot authoritative Auction projection is required"
                    )
                snapshot_event_types = [
                    event.event_type.value for event in auction_projection.events
                ]
                snapshot_event_ids = [
                    event.event_id for event in auction_projection.events
                ]
                snapshot_permissions = [
                    f"{permission.setup_family.value}:{permission.result.value}"
                    for permission in auction_projection.permissions
                ]

                timeline_rows.append(
                    {
                        "index": index,
                        "symbol": snapshot.symbol,
                        "snapshot_time": snapshot.snapshot_time,
                        "snapshot_auction_action": (
                            ",".join(snapshot_event_types)
                            if snapshot_event_types else "NO_EVENT"
                        ),
                        "snapshot_auction_state": (
                            auction_projection.directional.direction.value
                        ),
                        "snapshot_balance_state": (
                            auction_projection.balance.current_state.value
                        ),
                        "snapshot_authoritative_event_ids": json.dumps(
                            snapshot_event_ids
                        ),
                        "snapshot_permission_results": json.dumps(
                            snapshot_permissions
                        ),
                        "selected_opportunity_key": signal_ctx.get(
                            "opportunity_key"
                        ),
                        "selected_candidate_id": signal_ctx.get("candidate_id"),
                        "signal_action": signal_action,
                        **signal_ctx,
                        "tradegen_attempted": tradegen_attempted,
                        "tradegen_outcome": tradegen_outcome,
                        "tradegen_error": tradegen_error,
                        "tradegen_created_count": tradegen_created_count,
                        "executor_entry_updates": int(entry_updates or 0),
                        "monitor_updates": int(monitor_updates or 0),
                        "monitor_error_count": len(pass_monitor_errors),
                        "executor_exit_updates": int(exit_updates or 0),
                        "trade_count": len(current_trades),
                        "entry_status_counts": json.dumps(
                            dict(sorted(entry_status_counts.items())), sort_keys=True
                        ),
                        "exit_status_counts": json.dumps(
                            dict(sorted(exit_status_counts.items())), sort_keys=True
                        ),
                    }
                )

                for trade in current_trades:
                    trade_row = _trade_row(trade)
                    trade_observation_rows.append(
                        {
                            "snapshot_index": index,
                            "observation_snapshot_time": snapshot.snapshot_time,
                            "current_signal_stage": signal_ctx["signal_stage"],
                            "current_signal_status": signal_ctx["signal_status"],
                            "current_signal_reason": signal_ctx["signal_reason"],
                            "current_signal_management_posture": signal_ctx["management_posture"],
                            "current_signal_trade_action": signal_ctx["lifecycle_trade_action"],
                            "current_signal_should_exit": signal_ctx["should_exit_signal"],
                            "current_signal_auction_action": signal_ctx["signal_auction_action"],
                            "current_signal_auction_state": signal_ctx["signal_auction_state"],
                            "current_signal_directional_alignment": signal_ctx["signal_directional_alignment"],
                            "trade_state_frozen_at_exit": trade.exit_status.value == ExitStatus.FILLED.value,
                            **trade_row,
                        }
                    )

                logger.info(
                    "REPLAY_TICK | %s @ %s | signal=%s stage=%s tradegen=%s "
                    "entry_exec=%s monitor=%s exit_exec=%s trades=%s entry=%s exit=%s",
                    snapshot.symbol,
                    snapshot.snapshot_time,
                    signal_action,
                    signal_ctx["signal_stage"],
                    tradegen_outcome,
                    entry_updates,
                    monitor_updates,
                    exit_updates,
                    len(current_trades),
                    dict(entry_status_counts),
                    dict(exit_status_counts),
                )
    finally:
        EXECUTION_CONFIG.use_snapshot = old_use_snapshot
        EXECUTION_CONFIG.use_live_price_for_virtual = old_live_virtual

    final_signals = _signals_in_scope(
        trading_day=trading_day,
        symbols=symbols,
        lifecycle=lifecycle,
    )
    final_trades = _trades_in_scope(
        trading_day=trading_day,
        symbols=symbols,
        userid=userid,
    )
    validation_failures: List[Dict[str, Any]] = []

    _capture_validation(
        validation_failures,
        code="FINAL_RESULT_CONTRACT",
        validator=lambda: _validate_final_results(
            signals=final_signals,
            trades=final_trades,
            require_trade=bool(args.require_trade),
            require_exit=bool(args.require_exit),
        ),
    )
    _capture_validation(
        validation_failures,
        code="REPLAY_TIMESTAMP_CONTRACT",
        validator=lambda: _validate_replay_timestamps(
            trades=final_trades,
            trading_day=trading_day,
        ),
    )

    derivative_rows: List[Dict[str, Any]] = []
    try:
        derivative_rows = _derivative_validation_rows(
            snapshots=snapshots, trades=final_trades
        )
    except Exception as exc:
        _record_validation_failure(
            validation_failures,
            code="DERIVATIVE_VALIDATION_BUILD",
            message=str(exc),
            details={"error_type": type(exc).__name__},
        )
    if args.require_derivatives:
        _capture_validation(
            validation_failures,
            code="DERIVATIVE_COVERAGE",
            validator=lambda: _validate_derivative_coverage(
                trades=final_trades, rows=derivative_rows
            ),
        )

    if test_mode == "SIGNAL_EXIT":
        if first_signal_exit_request_time is None:
            _record_validation_failure(
                validation_failures,
                code="SIGNAL_EXIT_REQUEST_MISSING",
                message="SIGNAL_EXIT replay never observed should_exit_signal=true",
            )

        filled_signal_exits = [
            trade
            for trade in final_trades
            if trade.exit_status.value == ExitStatus.FILLED.value
            and str(trade.exit_reason or "").strip().upper() == "SIGNAL_LIFECYCLE_EXIT"
        ]
        actual_exits = [
            {
                "trade_id": trade.id,
                "exit_status": trade.exit_status.value,
                "exit_reason": trade.exit_reason,
                "exit_rule": trade.exit_rule,
                "exit_exec_time": trade.exit_exec_time,
            }
            for trade in final_trades
        ]
        if not filled_signal_exits:
            _record_validation_failure(
                validation_failures,
                code="SIGNAL_LIFECYCLE_EXIT_NOT_FILLED",
                message=(
                    "SIGNAL_EXIT replay did not produce a filled "
                    "SIGNAL_LIFECYCLE_EXIT"
                ),
                details={"actual_exits": actual_exits},
            )

        for trade in filled_signal_exits:
            expected_rule = "exit_on_auction_signal_downstream_contract"
            actual_rule = str(trade.exit_rule or "").strip()
            if actual_rule != expected_rule:
                _record_validation_failure(
                    validation_failures,
                    code="SIGNAL_EXIT_RULE_MISMATCH",
                    message="signal lifecycle exit used a non-canonical exit_rule",
                    details={
                        "trade_id": trade.id,
                        "expected_rule": expected_rule,
                        "actual_rule": actual_rule,
                    },
                )
            if trade.exit_exec_time is None:
                _record_validation_failure(
                    validation_failures,
                    code="SIGNAL_EXIT_TIME_MISSING",
                    message="signal lifecycle exit is missing exit_exec_time",
                    details={"trade_id": trade.id},
                )
                continue
            if first_signal_exit_request_time is None:
                continue
            request_time = (
                first_signal_exit_request_time.astimezone(IST).replace(tzinfo=None)
                if first_signal_exit_request_time.tzinfo
                else first_signal_exit_request_time
            )
            if trade.exit_exec_time < request_time:
                _record_validation_failure(
                    validation_failures,
                    code="SIGNAL_EXIT_BEFORE_REQUEST",
                    message="signal lifecycle exit filled before the first exit request",
                    details={
                        "trade_id": trade.id,
                        "exit_exec_time": trade.exit_exec_time,
                        "first_signal_exit_request_time": request_time,
                    },
                )

    signal_rows = [
        {
            "signal_id": signal.signal_id,
            "symbol": signal.symbol,
            "equity_ref": signal.equity_ref,
            "setup": signal.setup,
            "side": signal.side.value,
            "stage": signal.stage.value,
            "status": signal.status.value,
            "status_reason": signal.status_reason,
            "first_seen_time": signal.first_seen_time,
            "actionable_time": signal.actionable_time,
            "last_eval_time": signal.last_eval_time,
            "last_snapshot_time": signal.last_snapshot_time,
            "created_price": signal.created_price,
            "last_price": signal.last_price,
            "last_pnl": signal.last_pnl,
            "max_pnl": signal.max_pnl,
            "min_pnl": signal.min_pnl,
            **_signal_context(signal),
        }
        for signal in final_signals
    ]
    trade_rows = [_trade_row(trade) for trade in final_trades]
    audit_rows = _audit_rows(
        trading_day=trading_day,
        symbols=symbols,
        userid=userid,
        signals=final_signals,
        trades=final_trades,
    )
    audit_stage_counts = Counter(
        str(row["evaluation_stage"] or "").strip().upper() for row in audit_rows
    )
    account_governor_rows = [
        row
        for row in audit_rows
        if str(row["entity_type"] or "").strip().upper() == "ACCOUNT_GOVERNOR"
    ]
    account_governor_availability_counts = Counter(
        str(row["account_governor_availability"] or "").strip().upper()
        for row in account_governor_rows
    )
    account_governor_state_counts = Counter(
        str(row["account_governor_state"] or "").strip().upper()
        for row in account_governor_rows
    )
    account_governor_influence_counts = Counter(
        str(row["account_governor_influence"] or "").strip().upper()
        for row in account_governor_rows
    )
    _capture_validation(
        validation_failures,
        code="ACCOUNT_GOVERNOR_TIMESTAMP_CONTRACT",
        validator=lambda: _validate_account_governor_timestamps(
            rows=account_governor_rows,
            trades=final_trades,
            trading_day=trading_day,
        ),
    )
    expected_account_governor_assessments = int(
        tradegen_outcome_counts.get("CREATED", 0)
    )
    if len(account_governor_rows) != expected_account_governor_assessments:
        _record_validation_failure(
            validation_failures,
            code="ACCOUNT_GOVERNOR_AUDIT_COUNT_MISMATCH",
            message=(
                "Account Governor diagnostic rows do not match created trade packages"
            ),
            details={
                "created_packages": expected_account_governor_assessments,
                "account_governor_rows": len(account_governor_rows),
            },
        )
    monitor_audit_rows = int(audit_stage_counts["TRADE_MONITOR"])
    if monitor_audit_rows != monitor_update_total:
        _record_validation_failure(
            validation_failures,
            code="TRADE_MONITOR_AUDIT_COUNT_MISMATCH",
            message="TradeMonitor audit rows do not match monitor updates",
            details={
                "monitor_updates": monitor_update_total,
                "audit_rows": monitor_audit_rows,
            },
        )
    executor_audit_rows = int(audit_stage_counts["TRADE_EXECUTOR"])
    expected_executor_audit_rows = int(executor_entry_total + executor_exit_total)
    if executor_audit_rows != expected_executor_audit_rows:
        _record_validation_failure(
            validation_failures,
            code="TRADE_EXECUTOR_AUDIT_COUNT_MISMATCH",
            message="TradeExecutor audit rows do not match executor updates",
            details={
                "executor_entry_updates": executor_entry_total,
                "executor_exit_updates": executor_exit_total,
                "expected_audit_rows": expected_executor_audit_rows,
                "audit_rows": executor_audit_rows,
            },
        )
    if monitor_error_total > 0:
        _record_validation_failure(
            validation_failures,
            code="TRADE_MONITOR_ITEM_ERRORS",
            message="TradeMonitor completed with one or more per-trade errors",
            details={"monitor_error_count": monitor_error_total},
        )

    final_entry_status = Counter(trade.entry_status.value for trade in final_trades)
    final_exit_status = Counter(trade.exit_status.value for trade in final_trades)
    final_instruments = Counter(trade.instrument_type.value for trade in final_trades)
    first_entry_fill = min(
        (trade.entry_exec_time for trade in final_trades if trade.entry_exec_time is not None),
        default=None,
    )
    first_exit_fill = min(
        (trade.exit_exec_time for trade in final_trades if trade.exit_exec_time is not None),
        default=None,
    )
    total_exit_pnl = sum(
        (float(trade.exit_pnl) for trade in final_trades if trade.exit_pnl is not None),
        0.0,
    )

    summary = sanitize_json(
        {
            "trading_day": trading_day,
            "database_name": database_name,
            "database_writes": True,
            "requested_symbols": requested_symbols,
            "symbols": symbols,
            "missing_symbols": missing_symbols,
            "userid": userid,
            "instrument_choice": instrument_choice,
            "test_mode": test_mode,
            "adaptive_target_exit_enabled": test_mode == "ADAPTIVE_EXIT",
            "adaptive_stop_exit_enabled": test_mode == "ADAPTIVE_EXIT",
            "derivatives_enabled": instrument_choice in {"FUT", "CE", "PE", "MULTI"},
            "require_derivatives": bool(args.require_derivatives),
            "derivative_validation_rows": len(derivative_rows),
            "derivative_validation_failures": sum(
                1 for row in derivative_rows if row["validation_status"] != "PASSED"
            ),
            "first_signal_exit_request_time": first_signal_exit_request_time,
            "lifecycle": lifecycle,
            "snapshots": len(snapshots),
            "first_snapshot_time": snapshots[0].snapshot_time,
            "last_snapshot_time": snapshots[-1].snapshot_time,
            "auction_recomputed": False,
            "snapshots_marked_processed": 0,
            "clear_data": bool(args.clear_data),
            "cleared": cleared,
            "signal_action_counts": dict(sorted(signal_action_counts.items())),
            "signal_stage_observation_counts": dict(sorted(signal_stage_counts.items())),
            "tradegen_outcome_counts": dict(sorted(tradegen_outcome_counts.items())),
            "executor_entry_updates": executor_entry_total,
            "monitor_updates": monitor_update_total,
            "monitor_error_count": monitor_error_total,
            "executor_exit_updates": executor_exit_total,
            "signals_persisted": len(final_signals),
            "trades_persisted": len(final_trades),
            "final_entry_status_counts": dict(sorted(final_entry_status.items())),
            "final_exit_status_counts": dict(sorted(final_exit_status.items())),
            "final_instrument_counts": dict(sorted(final_instruments.items())),
            "first_entry_fill_time": first_entry_fill,
            "first_exit_fill_time": first_exit_fill,
            "total_exit_pnl": round(total_exit_pnl, 4),
            "audit_rows": len(audit_rows),
            "audit_stage_counts": dict(sorted(audit_stage_counts.items())),
            "account_governor_assessments": len(account_governor_rows),
            "account_governor_context_presence_counts": {
                "PRESENT": sum(
                    1
                    for row in account_governor_rows
                    if row["account_governor_context_present"] is True
                )
            },
            "account_governor_availability_counts": dict(
                sorted(account_governor_availability_counts.items())
            ),
            "account_governor_state_counts": dict(
                sorted(account_governor_state_counts.items())
            ),
            "account_governor_influence_counts": dict(
                sorted(account_governor_influence_counts.items())
            ),
            "trade_monitor_audit_rows": monitor_audit_rows,
            "trade_executor_audit_rows": executor_audit_rows,
            "expected_trade_executor_audit_rows": expected_executor_audit_rows,
            "validation_status": "PASSED" if not validation_failures else "FAILED",
            "validation_error_count": len(validation_failures),
            "validation_errors": validation_failures,
            "final_exit_details": [
                {
                    "trade_id": trade.id,
                    "exit_status": trade.exit_status.value,
                    "exit_reason": trade.exit_reason,
                    "exit_rule": trade.exit_rule,
                    "exit_exec_time": trade.exit_exec_time,
                }
                for trade in final_trades
            ],
            "require_trade": bool(args.require_trade),
            "require_exit": bool(args.require_exit),
        }
    )

    stamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
    prefix = report_dir / f"signal_trade_pipeline_replay_{trading_day}_{stamp}"
    _write_csv(prefix.with_name(prefix.name + "_timeline.csv"), timeline_rows)
    _write_csv(
        prefix.with_name(prefix.name + "_trade_observations.csv"),
        trade_observation_rows,
    )
    _write_csv(prefix.with_name(prefix.name + "_signals.csv"), signal_rows)
    _write_csv(prefix.with_name(prefix.name + "_trades.csv"), trade_rows)
    _write_csv(prefix.with_name(prefix.name + "_audit.csv"), audit_rows)
    _write_csv(
        prefix.with_name(prefix.name + "_account_governor.csv"),
        account_governor_rows,
    )
    _write_csv(
        prefix.with_name(prefix.name + "_monitor_errors.csv"),
        monitor_error_rows,
    )
    _write_csv(
        prefix.with_name(prefix.name + "_validation.csv"),
        validation_failures,
    )
    _write_csv(
        prefix.with_name(prefix.name + "_derivative_validation.csv"),
        derivative_rows,
    )

    prefix.with_name(prefix.name + "_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    _write_csv(
        prefix.with_name(prefix.name + "_summary.csv"),
        [
            {
                **summary,
                "requested_symbols": json.dumps(summary["requested_symbols"]),
                "symbols": json.dumps(summary["symbols"]),
                "missing_symbols": json.dumps(summary["missing_symbols"]),
                "cleared": json.dumps(summary["cleared"], sort_keys=True),
                "signal_action_counts": json.dumps(
                    summary["signal_action_counts"], sort_keys=True
                ),
                "signal_stage_observation_counts": json.dumps(
                    summary["signal_stage_observation_counts"], sort_keys=True
                ),
                "tradegen_outcome_counts": json.dumps(
                    summary["tradegen_outcome_counts"], sort_keys=True
                ),
                "final_entry_status_counts": json.dumps(
                    summary["final_entry_status_counts"], sort_keys=True
                ),
                "final_exit_status_counts": json.dumps(
                    summary["final_exit_status_counts"], sort_keys=True
                ),
                "final_instrument_counts": json.dumps(
                    summary["final_instrument_counts"], sort_keys=True
                ),
                "account_governor_context_presence_counts": json.dumps(
                    summary["account_governor_context_presence_counts"],
                    sort_keys=True,
                ),
                "account_governor_availability_counts": json.dumps(
                    summary["account_governor_availability_counts"],
                    sort_keys=True,
                ),
                "account_governor_state_counts": json.dumps(
                    summary["account_governor_state_counts"],
                    sort_keys=True,
                ),
                "account_governor_influence_counts": json.dumps(
                    summary["account_governor_influence_counts"],
                    sort_keys=True,
                ),
                "validation_errors": json.dumps(
                    summary["validation_errors"], sort_keys=True, default=str
                ),
                "final_exit_details": json.dumps(
                    summary["final_exit_details"], sort_keys=True, default=str
                ),
            }
        ],
    )

    if validation_failures:
        logger.error(
            "Strict Auction signal/trade replay completed with validation failures | "
            "count=%d reports=%s_*",
            len(validation_failures),
            prefix,
        )
        return 2

    logger.info("Strict Auction signal/trade replay complete | %s", summary)
    logger.info("Reports: %s_*", prefix)
    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except KeyboardInterrupt:
        logger.warning("Replay interrupted by operator")
        exit_code = 130
    except Exception:
        logger.exception("REPLAY_FATAL_ERROR | unexpected failure before clean completion")
        exit_code = 1
    raise SystemExit(exit_code)
