#!/usr/bin/env python3
"""
tests/replays/replay_pipeline.py

Clean, fixed-symbol, end-to-end replay/backtest runner for AutoTrades.

This program follows the same cadence-level order as the live services:

    generate snapshots for the configured symbols
    -> evaluate each generated snapshot through SignalGenerator
    -> mark each snapshot processed immediately after successful signal evaluation
    -> run TradeGenerator once for the cadence
    -> run TradeExecutor entry once for the cadence
    -> run TradeMonitor once for the cadence
    -> run TradeExecutor exit once for the cadence

This is intentionally a simple clean-run utility:

- fixed hard-coded symbol list
- fixed hard-coded replay window
- visible source defaults with CLI overrides
- no checkpoint/resume logic
- no raw candle deletion; historical data is fetched from the API on demand
- optional global clearing of auditlog, user_trades, stock_opportunities, signals and snapshots
- optional CSV exports

For large-universe work, use replay_snapshots.py followed by
replay_unprocessed.py instead.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple
from zoneinfo import ZoneInfo

# Allow imports from project root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from config import AppConfig
from configs.execution_config import EXECUTION_CONFIG
from database.database import get_trades_db
from logconfig import setup_logging
from models.trade_models import (
    AuditLog as AuditLogORM,
    Signal as SignalORM,
    StockOpportunity as StockOpportunityORM,
    Snapshot as SnapshotORM,
    UserTrade as TradeORM,
)
from schemas.snapshot import SnapshotSchema
from schemas.symbol import SymbolSchema
from schemas.user import UserSchema
from services.signals.signal_generator import SignalGenerator
from services.snapshot.snapshot_generator import SnapshotGenerator
from services.trade.executor.trade_executor import TradeExecutor
from services.trade.generator.trade_generator import TradeGenerator
from services.trade.monitor.trade_monitor import TradeMonitor
from tests.replays.replay_execution_prices import (
    DEFAULT_REPLAY_EXECUTION_PRICE_SOURCE,
    SUPPORTED_REPLAY_PRICE_SOURCES,
    replay_execution_price_source,
)


# =============================================================================
# SOURCE DEFAULTS - CLI values override these
# =============================================================================

IST = ZoneInfo("Asia/Kolkata")

# Snapshot API ticks. A tick at 09:18 normally persists the completed 09:15
# three-minute snapshot; a tick at 15:30 normally persists 15:27.
START = datetime(2026, 8, 3, 9, 18, tzinfo=IST)
END = datetime(2026, 8, 3, 15, 30, tzinfo=IST)
STEP_MINUTES = 3

# Fixed replay universe. The symbols must be enabled EQ symbols in the DB.
SYMBOLS: List[str] = ["TORNTPHARM", "DELHIVERY"]
SYMBOL_TYPE_FILTER = "EQ"

# The user for whom TradeGenerator creates the replay trades.
REPLAY_USERID = "DR1812"

# Market-data credentials are loaded from this DB user. Leave the override
# values blank to use the DB values. A non-empty value overrides only that field.
DATA_USER_ID = AppConfig.DATA_USER
API_KEY_OVERRIDE = ""
ACCESS_TOKEN_OVERRIDE = ""

# False preserves the configured database state. Set True only when a clean
# replay is required; auditlog, user_trades, stock_opportunities, signals and
# snapshots are then cleared. Candles and derivatives data are preserved.
CLEAR_DATA: bool = False

# True writes signals, user_trades, auditlog and a one-row summary CSV.
GENERATE_CSV_REPORTS: bool = True
REPORT_DIR = Path("reports")
LOG_FILE = REPORT_DIR / "replay_pipeline.log"
EXECUTION_PRICE_SOURCE = DEFAULT_REPLAY_EXECUTION_PRICE_SOURCE

logger = logging.getLogger(__name__)

job_stats: Dict[str, List[float]] = {
    "snapshots": [],
    "signals": [],
    "trades": [],
    "execute_entry": [],
    "monitor": [],
    "execute_exit": [],
}
run_stats: Dict[str, int] = defaultdict(int)


# =============================================================================
# Startup helpers
# =============================================================================


def _validate_replay_window() -> date:
    start = START.astimezone(IST) if START.tzinfo else START.replace(tzinfo=IST)
    end = END.astimezone(IST) if END.tzinfo else END.replace(tzinfo=IST)

    if end < start:
        raise ValueError("END must not be earlier than START")
    if start.date() != end.date():
        raise ValueError("START and END must be on the same trading day")
    if int(STEP_MINUTES) < 1:
        raise ValueError("STEP_MINUTES must be >= 1")
    return start.date()


def _normalized_symbols() -> List[str]:
    normalized: List[str] = []
    seen = set()
    for value in SYMBOLS:
        symbol = str(value or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        normalized.append(symbol)

    if not normalized:
        raise ValueError("SYMBOLS must contain at least one fixed EQ symbol")
    if "ALL" in seen:
        raise ValueError(
            'replay_pipeline.py is for a fixed symbol list; use replay_snapshots.py for "ALL"'
        )
    return normalized


def _selected_symbol_rows() -> List[Any]:
    requested = _normalized_symbols()

    # active=None deliberately ignores the intraday active flag. enabled=True is
    # still enforced inside SymbolSchema.fetch_symbols.
    rows = SymbolSchema.fetch_symbols(
        active=None,
        type_filter=SYMBOL_TYPE_FILTER,
    ) or []
    by_symbol = {
        str(getattr(row, "symbol", "") or "").strip().upper(): row
        for row in rows
        if str(getattr(row, "symbol", "") or "").strip()
    }

    missing = [symbol for symbol in requested if symbol not in by_symbol]
    if missing:
        raise RuntimeError(
            "Requested symbols are not enabled %s symbols: %s"
            % (SYMBOL_TYPE_FILTER, ", ".join(missing))
        )

    selected = [by_symbol[symbol] for symbol in requested]
    for row in selected:
        symbol = str(getattr(row, "symbol", "") or "").strip().upper()
        if getattr(row, "token", None) is None:
            raise RuntimeError(f"Selected symbol {symbol} has no instrument token")
    return selected


def _resolve_api_credentials() -> Tuple[str, str, str]:
    userid = str(DATA_USER_ID or "").strip()
    api_override = str(API_KEY_OVERRIDE or "").strip()
    token_override = str(ACCESS_TOKEN_OVERRIDE or "").strip()

    db_api_key = ""
    db_access_token = ""

    if userid:
        user = UserSchema.fetch_user(userid)
        if user is None:
            if not (api_override and token_override):
                raise RuntimeError(
                    f"DATA_USER_ID {userid!r} was not found and complete overrides were not supplied"
                )
        else:
            db_api_key = str(user.apikey or "").strip()
            db_access_token = str(user.access_token or "").strip()
    elif not (api_override and token_override):
        raise RuntimeError(
            "DATA_USER_ID is blank and complete credential overrides were not supplied"
        )

    api_key = api_override or db_api_key
    access_token = token_override or db_access_token
    if not api_key or not access_token:
        missing = []
        if not api_key:
            missing.append("apikey")
        if not access_token:
            missing.append("access_token")
        raise RuntimeError(
            "Replay market-data credentials are incomplete; missing %s"
            % ", ".join(missing)
        )

    if api_override and token_override:
        source = "HARDCODED_OVERRIDES"
    elif api_override or token_override:
        source = "DATABASE_WITH_PARTIAL_OVERRIDE"
    else:
        source = "DATABASE"
    return api_key, access_token, source


def _clear_replay_data() -> None:
    if not CLEAR_DATA:
        logger.info("CLEAR_DATA=False; existing replay rows are preserved")
        return

    with get_trades_db() as db:
        try:
            # Delete dependent/current pipeline rows before snapshots.
            trades_deleted = int(db.query(TradeORM).delete(synchronize_session=False))
            opportunities_deleted = int(
                db.query(StockOpportunityORM).delete(synchronize_session=False)
            )
            signals_deleted = int(db.query(SignalORM).delete(synchronize_session=False))
            audits_deleted = int(db.query(AuditLogORM).delete(synchronize_session=False))
            snapshots_deleted = int(db.query(SnapshotORM).delete(synchronize_session=False))
            db.commit()
        except Exception:
            db.rollback()
            raise

    logger.info(
        "Clean replay reset complete | user_trades=%d stock_opportunities=%d "
        "signals=%d auditlog=%d snapshots=%d",
        trades_deleted,
        opportunities_deleted,
        signals_deleted,
        audits_deleted,
        snapshots_deleted,
    )


# =============================================================================
# Cadence jobs
# =============================================================================


def _generate_snapshots_for_tick(
    *,
    current_time: datetime,
    symbol_rows: Sequence[Any],
    api_key: str,
    access_token: str,
) -> List[SnapshotSchema]:
    generated: List[SnapshotSchema] = []

    for row in symbol_rows:
        symbol = str(getattr(row, "symbol", "") or "").strip().upper()
        token = int(getattr(row, "token"))
        try:
            snapshot = SnapshotGenerator(
                token=token,
                symbol=symbol,
                api_key=api_key,
                access_token=access_token,
            ).generate_snapshot(
                end_date=current_time,
                persist_snapshot=True,
            )
            if snapshot is None:
                run_stats["snapshot_none"] += 1
                logger.warning(
                    "SnapshotGenerator returned None | symbol=%s tick=%s",
                    symbol,
                    current_time,
                )
                continue

            generated.append(snapshot)
            run_stats["snapshots_generated"] += 1
        except Exception:
            run_stats["snapshot_errors"] += 1
            logger.exception(
                "Snapshot generation failed | symbol=%s tick=%s",
                symbol,
                current_time,
            )

    return sorted(
        generated,
        key=lambda snapshot: (
            getattr(snapshot, "snapshot_time"),
            str(getattr(snapshot, "symbol", "") or ""),
        ),
    )


def _naive_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(IST).replace(tzinfo=None)


def _generate_signals_and_acknowledge(snapshots: Sequence[SnapshotSchema]) -> int:
    processed = 0

    for snapshot in snapshots:
        symbol = str(snapshot.symbol).strip().upper()
        snapshot_time = snapshot.snapshot_time
        try:
            SignalGenerator(snapshot).generate_signal()
        except Exception:
            run_stats["signal_errors"] += 1
            logger.exception(
                "Signal generation failed; snapshot remains unprocessed | symbol=%s snapshot_time=%s",
                symbol,
                snapshot_time,
            )
            continue

        try:
            marked = SnapshotSchema.mark_processed(
                symbol,
                _naive_ist(snapshot_time),
            )
            if not marked:
                raise RuntimeError("SnapshotSchema.mark_processed returned False")
        except Exception:
            run_stats["mark_processed_errors"] += 1
            logger.exception(
                "Signal succeeded but snapshot acknowledgement failed | symbol=%s snapshot_time=%s",
                symbol,
                snapshot_time,
            )
            continue

        processed += 1
        run_stats["signals_processed"] += 1

    return processed


def _count_user_trades() -> int:
    with get_trades_db() as db:
        return int(db.query(TradeORM).count())


def _generate_trades(current_time: datetime) -> int:
    before = _count_user_trades()
    try:
        created = TradeGenerator().generate_user_trades(REPLAY_USERID) or []
    except Exception:
        run_stats["trade_generation_errors"] += 1
        logger.exception("TradeGenerator failed @ %s", current_time)
        return 0

    after = _count_user_trades()
    returned = len(created) if isinstance(created, (list, tuple, set)) else int(created or 0)
    run_stats["trades_created_returned"] += returned
    run_stats["trades_created_db_delta"] += max(0, after - before)
    logger.info(
        "Trade generation complete | time=%s returned=%d db_delta=%d total=%d userid=%s",
        current_time,
        returned,
        after - before,
        after,
        REPLAY_USERID,
    )
    return returned


def _execute_trades(current_time: datetime, label: str) -> int:
    try:
        result = TradeExecutor().execute_all(snapshot_time=current_time)
    except Exception:
        run_stats[f"{label}_errors"] += 1
        logger.exception("TradeExecutor failed | pass=%s @ %s", label, current_time)
        return 0

    count = len(result) if isinstance(result, list) else int(result or 0)
    run_stats[f"{label}_results"] += count
    logger.info(
        "TradeExecutor complete | pass=%s time=%s result_count=%d raw=%s",
        label,
        current_time,
        count,
        result,
    )
    return count


def _monitor_trades(current_time: datetime) -> int:
    try:
        result = TradeMonitor().monitor(snapshot_time=current_time)
    except Exception:
        run_stats["monitor_errors"] += 1
        logger.exception("TradeMonitor failed @ %s", current_time)
        return 0

    count = len(result) if isinstance(result, list) else int(result or 0)
    run_stats["monitor_results"] += count
    logger.info(
        "TradeMonitor complete | time=%s updated=%d raw=%s",
        current_time,
        count,
        result,
    )
    return count


# =============================================================================
# Reporting
# =============================================================================


def _enum_str(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().upper()


def _serialize_csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    return value


def _export_orm_table(model: Any, path: Path) -> int:
    columns = [column.name for column in model.__table__.columns]
    primary_key = list(model.__table__.primary_key.columns)[0]

    with get_trades_db() as db:
        rows = db.query(model).order_by(primary_key).all()

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    column: _serialize_csv_value(getattr(row, column))
                    for column in columns
                }
            )
    return len(rows)


def _db_summary() -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "snapshots": 0,
        "processed_snapshots": 0,
        "signals": 0,
        "trades": 0,
        "audit_rows": 0,
        "entry_status": defaultdict(int),
        "exit_status": defaultdict(int),
        "instrument_type": defaultdict(int),
        "execution_mode": defaultdict(int),
    }

    with get_trades_db() as db:
        summary["snapshots"] = int(db.query(SnapshotORM).count())
        summary["processed_snapshots"] = int(
            db.query(SnapshotORM)
            .filter(SnapshotORM.processed == True)  # noqa: E712
            .count()
        )
        summary["signals"] = int(db.query(SignalORM).count())
        summary["audit_rows"] = int(db.query(AuditLogORM).count())
        trades = db.query(TradeORM).all()

    summary["trades"] = len(trades)
    for trade in trades:
        summary["entry_status"][_enum_str(getattr(trade, "entry_status", ""))] += 1
        summary["exit_status"][_enum_str(getattr(trade, "exit_status", ""))] += 1
        summary["instrument_type"][_enum_str(getattr(trade, "instrument_type", ""))] += 1
        summary["execution_mode"][_enum_str(getattr(trade, "execution_mode", ""))] += 1
    return summary


def _log_db_summary(summary: Dict[str, Any]) -> None:
    logger.info("=== DB REPLAY OUTPUT SUMMARY ===")
    logger.info(
        "snapshots=%s processed_snapshots=%s signals=%s trades=%s audit_rows=%s",
        summary["snapshots"],
        summary["processed_snapshots"],
        summary["signals"],
        summary["trades"],
        summary["audit_rows"],
    )
    for key in ("entry_status", "exit_status", "instrument_type", "execution_mode"):
        logger.info("%s=%s", key, dict(sorted(summary[key].items())))
    logger.info("================================")


def _write_csv_reports(
    *,
    started_at: datetime,
    elapsed_seconds: float,
    symbols: Sequence[str],
    credential_source: str,
    summary: Dict[str, Any],
) -> None:
    if not GENERATE_CSV_REPORTS:
        logger.info("GENERATE_CSV_REPORTS=False; no CSV reports were written")
        return

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
    prefix = f"replay_pipeline_{START.astimezone(IST).date().isoformat()}_{stamp}"

    exported = {
        "signals": _export_orm_table(SignalORM, REPORT_DIR / f"{prefix}_signals.csv"),
        "user_trades": _export_orm_table(TradeORM, REPORT_DIR / f"{prefix}_user_trades.csv"),
        "auditlog": _export_orm_table(AuditLogORM, REPORT_DIR / f"{prefix}_auditlog.csv"),
    }

    summary_row: Dict[str, Any] = {
        "run_started_at": started_at.isoformat(),
        "run_finished_at": datetime.now(IST).isoformat(),
        "elapsed_seconds": round(elapsed_seconds, 3),
        "replay_start": START.isoformat(),
        "replay_end": END.isoformat(),
        "step_minutes": STEP_MINUTES,
        "symbols": ",".join(symbols),
        "symbol_count": len(symbols),
        "replay_userid": REPLAY_USERID,
        "data_user_id": DATA_USER_ID,
        "credential_source": credential_source,
        "clear_data": CLEAR_DATA,
        "db_snapshots": summary["snapshots"],
        "db_processed_snapshots": summary["processed_snapshots"],
        "db_signals": summary["signals"],
        "db_trades": summary["trades"],
        "db_audit_rows": summary["audit_rows"],
        "entry_status": json.dumps(dict(sorted(summary["entry_status"].items()))),
        "exit_status": json.dumps(dict(sorted(summary["exit_status"].items()))),
        "instrument_type": json.dumps(dict(sorted(summary["instrument_type"].items()))),
        "execution_mode": json.dumps(dict(sorted(summary["execution_mode"].items()))),
        **dict(sorted(run_stats.items())),
        **{f"exported_{key}": value for key, value in exported.items()},
    }

    path = REPORT_DIR / f"{prefix}_summary.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary_row.keys()))
        writer.writeheader()
        writer.writerow(
            {key: _serialize_csv_value(value) for key, value in summary_row.items()}
        )

    logger.info(
        "CSV reports written | signals=%d trades=%d audit=%d summary=%s",
        exported["signals"],
        exported["user_trades"],
        exported["auditlog"],
        path,
    )


# =============================================================================
# Driver
# =============================================================================


def run_replay(
    *,
    symbol_rows: Sequence[Any],
    api_key: str,
    access_token: str,
) -> None:
    _clear_replay_data()

    current = START
    while current <= END:
        run_stats["cadences"] += 1
        logger.info("=== REPLAY PIPELINE @ %s ===", current)

        started = time.perf_counter()
        snapshots = _generate_snapshots_for_tick(
            current_time=current,
            symbol_rows=symbol_rows,
            api_key=api_key,
            access_token=access_token,
        )
        job_stats["snapshots"].append(time.perf_counter() - started)
        logger.info(
            "snapshots: generated=%d elapsed=%.3fs",
            len(snapshots),
            job_stats["snapshots"][-1],
        )

        started = time.perf_counter()
        processed = _generate_signals_and_acknowledge(snapshots)
        job_stats["signals"].append(time.perf_counter() - started)
        logger.info(
            "signals: processed=%d failed_or_unacknowledged=%d elapsed=%.3fs",
            processed,
            len(snapshots) - processed,
            job_stats["signals"][-1],
        )

        # Match the live service cadence: each downstream service gets one pass
        # for this replay clock, even when no new signal was created.
        started = time.perf_counter()
        trades = _generate_trades(current)
        job_stats["trades"].append(time.perf_counter() - started)
        logger.info(
            "trades: result=%d elapsed=%.3fs",
            trades,
            job_stats["trades"][-1],
        )

        started = time.perf_counter()
        entry = _execute_trades(current, "entry_pass")
        job_stats["execute_entry"].append(time.perf_counter() - started)
        logger.info(
            "execute_entry: result=%d elapsed=%.3fs",
            entry,
            job_stats["execute_entry"][-1],
        )

        started = time.perf_counter()
        monitored = _monitor_trades(current)
        job_stats["monitor"].append(time.perf_counter() - started)
        logger.info(
            "monitor: result=%d elapsed=%.3fs",
            monitored,
            job_stats["monitor"][-1],
        )

        started = time.perf_counter()
        exit_count = _execute_trades(current, "exit_pass")
        job_stats["execute_exit"].append(time.perf_counter() - started)
        logger.info(
            "execute_exit: result=%d elapsed=%.3fs",
            exit_count,
            job_stats["execute_exit"][-1],
        )

        current += timedelta(minutes=STEP_MINUTES)


def _log_timing_summary() -> None:
    logger.info("=== REPLAY TIMING SUMMARY ===")
    for name, values in job_stats.items():
        if not values:
            continue
        total = sum(values)
        logger.info(
            "%s: total=%.3fs avg=%.3fs runs=%d",
            name,
            total,
            total / len(values),
            len(values),
        )
    logger.info("=============================")


def _args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the fixed-symbol end-to-end replay pipeline. Defaults live in "
            "this file; CLI values override them."
        )
    )
    parser.add_argument("--day", default=START.date().isoformat())
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help="Override SYMBOLS; comma-separated items are also accepted",
    )
    parser.add_argument("--userid", default=REPLAY_USERID)
    parser.add_argument("--start-time", default=START.strftime("%H:%M:%S"))
    parser.add_argument("--end-time", default=END.strftime("%H:%M:%S"))
    parser.add_argument("--step-minutes", type=int, default=STEP_MINUTES)
    parser.add_argument(
        "--clear-data",
        action=argparse.BooleanOptionalAction,
        default=CLEAR_DATA,
    )
    parser.add_argument(
        "--csv-reports",
        action=argparse.BooleanOptionalAction,
        default=GENERATE_CSV_REPORTS,
    )
    parser.add_argument("--data-user", default=DATA_USER_ID)
    parser.add_argument("--api-key", default=API_KEY_OVERRIDE)
    parser.add_argument("--access-token", default=ACCESS_TOKEN_OVERRIDE)
    parser.add_argument("--report-dir", default=str(REPORT_DIR))
    parser.add_argument("--log-file", default=None)
    parser.add_argument(
        "--execution-price-source",
        default=EXECUTION_PRICE_SOURCE,
        choices=SUPPORTED_REPLAY_PRICE_SOURCES,
        help=(
            "Virtual replay fill source; 1m_candle is strict/no-fallback and "
            f"snapshot preserves prior behavior (default: {EXECUTION_PRICE_SOURCE})"
        ),
    )
    return parser.parse_args(argv)


def _cli_symbols(raw: Optional[Sequence[str]]) -> List[str]:
    if raw is None:
        return list(SYMBOLS)
    out: List[str] = []
    seen = set()
    for value in raw:
        for item in str(value or "").split(","):
            symbol = item.strip().upper()
            if symbol and symbol not in seen:
                seen.add(symbol)
                out.append(symbol)
    if not out:
        raise ValueError("--symbols cannot be empty")
    return out


def _cli_datetime(day_raw: str, clock_raw: str) -> datetime:
    day = date.fromisoformat(str(day_raw))
    clock = datetime.strptime(str(clock_raw), "%H:%M:%S").time()
    return datetime.combine(day, clock, tzinfo=IST)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = _args(argv)
    global logger, START, END, STEP_MINUTES, SYMBOLS, REPLAY_USERID
    global DATA_USER_ID, API_KEY_OVERRIDE, ACCESS_TOKEN_OVERRIDE, CLEAR_DATA
    global GENERATE_CSV_REPORTS, REPORT_DIR, LOG_FILE, EXECUTION_PRICE_SOURCE

    START = _cli_datetime(args.day, args.start_time)
    END = _cli_datetime(args.day, args.end_time)
    STEP_MINUTES = max(1, int(args.step_minutes))
    SYMBOLS = _cli_symbols(args.symbols)
    REPLAY_USERID = str(args.userid).strip()
    DATA_USER_ID = str(args.data_user).strip()
    API_KEY_OVERRIDE = str(args.api_key or "").strip()
    ACCESS_TOKEN_OVERRIDE = str(args.access_token or "").strip()
    CLEAR_DATA = bool(args.clear_data)
    GENERATE_CSV_REPORTS = bool(args.csv_reports)
    REPORT_DIR = Path(args.report_dir)
    LOG_FILE = Path(args.log_file) if args.log_file else REPORT_DIR / "replay_pipeline.log"
    EXECUTION_PRICE_SOURCE = str(args.execution_price_source).strip().lower()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    setup_logging(log_file=str(LOG_FILE))
    logger = logging.getLogger(__name__)

    started_at = datetime.now(IST)
    started_perf = time.perf_counter()

    # Startup/preflight failures terminate because continuing would be unsafe.
    trading_day = _validate_replay_window()
    symbol_rows = _selected_symbol_rows()
    symbols = [
        str(getattr(row, "symbol", "") or "").strip().upper()
        for row in symbol_rows
    ]
    api_key, access_token, credential_source = _resolve_api_credentials()

    logger.info(
        "Starting replay_pipeline | trading_day=%s start=%s end=%s step=%dm "
        "symbols=%s replay_user=%s clear=%s csv_reports=%s execution_price_source=%s "
        "data_user=%s credentials=%s",
        trading_day,
        START,
        END,
        STEP_MINUTES,
        symbols,
        REPLAY_USERID,
        CLEAR_DATA,
        GENERATE_CSV_REPORTS,
        EXECUTION_PRICE_SOURCE,
        DATA_USER_ID,
        credential_source,
    )

    old_use_snapshot = EXECUTION_CONFIG.use_snapshot
    old_use_live_price_for_virtual = EXECUTION_CONFIG.use_live_price_for_virtual
    old_force_virtual_for_replay = EXECUTION_CONFIG.force_virtual_for_replay

    EXECUTION_CONFIG.use_snapshot = True
    EXECUTION_CONFIG.use_live_price_for_virtual = False
    EXECUTION_CONFIG.force_virtual_for_replay = True

    logger.info(
        "Replay forced execution config | use_snapshot=%s "
        "use_live_price_for_virtual=%s force_virtual_for_replay=%s",
        EXECUTION_CONFIG.use_snapshot,
        EXECUTION_CONFIG.use_live_price_for_virtual,
        EXECUTION_CONFIG.force_virtual_for_replay,
    )

    try:
        with replay_execution_price_source(EXECUTION_PRICE_SOURCE):
            run_replay(
                symbol_rows=symbol_rows,
                api_key=api_key,
                access_token=access_token,
            )
    finally:
        EXECUTION_CONFIG.use_snapshot = old_use_snapshot
        EXECUTION_CONFIG.use_live_price_for_virtual = old_use_live_price_for_virtual
        EXECUTION_CONFIG.force_virtual_for_replay = old_force_virtual_for_replay

    elapsed = time.perf_counter() - started_perf
    _log_timing_summary()
    summary = _db_summary()
    _log_db_summary(summary)
    _write_csv_reports(
        started_at=started_at,
        elapsed_seconds=elapsed,
        symbols=symbols,
        credential_source=credential_source,
        summary=summary,
    )

    logger.info("Finished replay_pipeline | elapsed=%.3fs", elapsed)


if __name__ == "__main__":
    main()
