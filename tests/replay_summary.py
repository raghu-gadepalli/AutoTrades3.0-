#!/usr/bin/env python3
"""
tests/replay_summary.py

Self-contained end-to-end replay/backtest runner for AutoTrades.

The script keeps the original replay_summary design: it generates or reuses
snapshots and then runs the normal production pipeline in chronological order:

    SnapshotGenerator
    -> SignalGenerator
    -> TradeGenerator
    -> TradeExecutor entry pass
    -> TradeMonitor
    -> TradeExecutor exit pass

There are no command-line arguments. Edit the hard-coded configuration below.

Restart contract
----------------
Snapshot.processed is the only restart marker:

    processed=True  -> skip that snapshot
    processed=False -> process it and set processed=True only after the complete
                       downstream pipeline step succeeds

For a fresh run, set CLEAR_PIPELINE_DATA=True. This globally clears auditlog,
signals and user_trades, resets the selected snapshots to processed=False, and
starts the pipeline from the configured replay range.

If the program stops, restart with CLEAR_PIPELINE_DATA=False. Existing rows and
processed flags are preserved; completed snapshots are skipped and unfinished
snapshots are retried. No checkpoint file or separate resume option is used.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

# Allow imports from project root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import AppConfig
from configs.execution_config import EXECUTION_CONFIG
from database.database import get_trades_db
from logconfig import setup_logging
from models.trade_models import (
    AuditLog as AuditLogORM,
    Signal as SignalORM,
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


# =============================================================================
# HARD-CODED CONFIGURATION
# =============================================================================

IST = ZoneInfo("Asia/Kolkata")

# Replay API ticks. At 09:18, SnapshotGenerator normally persists the completed
# 09:15 three-minute snapshot. At 15:30, it persists 15:27.
START = datetime(2026, 7, 24, 9, 18, tzinfo=IST)
END = datetime(2026, 7, 24, 15, 30, tzinfo=IST)
STEP_MINUTES = 3
TF_PRIMARY_MIN = 3

# Use exactly one form:
#   ["ALL"]                         -> all selected enabled EQ symbols
#   ["MARUTI", "INFY", "ASTRAL"] -> only those selected enabled EQ symbols
SYMBOLS: List[str] = ["ALL"]
SYMBOL_TYPE_FILTER = "EQ"

# True  -> enabled EQ symbols with active=True only.
# False -> all enabled EQ symbols, regardless of the intraday active flag.
ACTIVE_ONLY: bool = False

# True  -> if (symbol, snapshot_time) already exists, do not call the API again.
#          Existing processed=False snapshots are still run through the pipeline.
# False -> regenerate/upsert snapshots, including their Auction result. The
#          persisted snapshot is reset to processed=False by snapshot persistence.
SKIP_EXISTING_SNAPSHOTS: bool = True

# Market-data credentials come from this database user. A non-empty override
# replaces only that credential; blank values use the database value.
DATA_USER_ID = AppConfig.DATA_USER
API_KEY_OVERRIDE = ""
ACCESS_TOKEN_OVERRIDE = ""

# Set one replay user, or None to use TradeGenerator's normal eligible-user flow.
TEST_USERID: Optional[str] = "DR1812"

# Fresh-run switch.
# True:
#   - globally clears auditlog, user_trades and signals
#   - resets selected snapshots in the replay range to processed=False
# False:
#   - preserves all rows and flags
#   - skips processed=True snapshots and retries processed=False snapshots
CLEAR_PIPELINE_DATA: bool = True

# True exports signals, user_trades, auditlog and a one-row replay summary CSV.
# False writes only the normal log output.
GENERATE_CSV_REPORTS: bool = True
REPORT_DIR = Path("reports")
LOG_FILE = REPORT_DIR / "replay_summary.log"

MARKET_OPEN_HHMM: Tuple[int, int] = (9, 15)
MARKET_CLOSE_HHMM: Tuple[int, int] = (15, 30)

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
# Configuration and selection helpers
# =============================================================================


def _naive_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(IST).replace(tzinfo=None)


def _trading_day() -> date:
    start_day = START.astimezone(IST).date() if START.tzinfo else START.date()
    end_day = END.astimezone(IST).date() if END.tzinfo else END.date()
    if start_day != end_day:
        raise ValueError("START and END must be on the same trading day")
    if END < START:
        raise ValueError("END must not be earlier than START")
    if STEP_MINUTES <= 0:
        raise ValueError("STEP_MINUTES must be positive")
    return start_day


def _normalized_symbol_selection() -> List[str]:
    normalized: List[str] = []
    seen = set()
    for value in SYMBOLS:
        symbol = str(value or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        normalized.append(symbol)

    if not normalized:
        raise RuntimeError('SYMBOLS is empty. Use ["ALL"] or named EQ symbols.')
    if "ALL" in seen and normalized != ["ALL"]:
        raise RuntimeError('Use SYMBOLS=["ALL"] by itself.')
    return normalized


def _selected_symbol_rows() -> List[Any]:
    selection = _normalized_symbol_selection()
    active_filter = 1 if ACTIVE_ONLY else None
    rows = SymbolSchema.fetch_symbols(
        active=active_filter,
        type_filter=str(SYMBOL_TYPE_FILTER).strip().upper(),
    ) or []

    rows_by_symbol = {
        str(getattr(row, "symbol", "") or "").strip().upper(): row
        for row in rows
        if str(getattr(row, "symbol", "") or "").strip()
    }

    if selection == ["ALL"]:
        selected = [rows_by_symbol[key] for key in sorted(rows_by_symbol)]
    else:
        missing = [symbol for symbol in selection if symbol not in rows_by_symbol]
        if missing:
            raise RuntimeError(
                "Requested symbols are not in the selected enabled %s universe "
                "(ACTIVE_ONLY=%s): %s"
                % (SYMBOL_TYPE_FILTER, ACTIVE_ONLY, ", ".join(missing))
            )
        selected = [rows_by_symbol[symbol] for symbol in selection]

    if not selected:
        raise RuntimeError(
            f"No enabled {SYMBOL_TYPE_FILTER} symbols selected "
            f"(ACTIVE_ONLY={ACTIVE_ONLY})"
        )

    for row in selected:
        symbol = str(getattr(row, "symbol", "") or "").strip().upper()
        token = getattr(row, "token", None)
        if not symbol:
            raise RuntimeError(f"Selected symbol row has an empty symbol: {row}")
        if token is None:
            raise RuntimeError(f"Selected symbol {symbol} has no token")

    return selected


def _resolve_api_credentials() -> Tuple[str, str, str]:
    userid = str(DATA_USER_ID or "").strip()
    api_key_override = str(API_KEY_OVERRIDE or "").strip()
    access_token_override = str(ACCESS_TOKEN_OVERRIDE or "").strip()

    db_api_key = ""
    db_access_token = ""

    if userid:
        user = UserSchema.fetch_user(userid)
        if user is None:
            if not (api_key_override and access_token_override):
                raise RuntimeError(
                    f"DATA_USER_ID {userid!r} was not found and complete overrides "
                    "were not supplied"
                )
        else:
            db_api_key = str(user.apikey or "").strip()
            db_access_token = str(user.access_token or "").strip()
    elif not (api_key_override and access_token_override):
        raise RuntimeError(
            "DATA_USER_ID is blank and complete credential overrides were not supplied"
        )

    api_key = api_key_override or db_api_key
    access_token = access_token_override or db_access_token
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

    if api_key_override and access_token_override:
        source = "HARDCODED_OVERRIDES"
    elif api_key_override or access_token_override:
        source = "DATABASE_WITH_PARTIAL_OVERRIDE"
    else:
        source = "DATABASE"

    return api_key, access_token, source


def _expected_snapshot_time_for_tick(tick_time: datetime) -> Optional[datetime]:
    """Return the latest completed primary snapshot time for this API tick."""
    tick = tick_time if tick_time.tzinfo else tick_time.replace(tzinfo=IST)
    tick = tick.astimezone(IST)

    market_open = tick.replace(
        hour=MARKET_OPEN_HHMM[0],
        minute=MARKET_OPEN_HHMM[1],
        second=0,
        microsecond=0,
    )
    market_close = tick.replace(
        hour=MARKET_CLOSE_HHMM[0],
        minute=MARKET_CLOSE_HHMM[1],
        second=0,
        microsecond=0,
    )

    if tick <= market_open:
        return None
    if tick >= market_close:
        return market_close - timedelta(minutes=TF_PRIMARY_MIN)

    elapsed_minutes = int((tick - market_open).total_seconds() // 60)
    if elapsed_minutes % TF_PRIMARY_MIN == 0:
        elapsed_minutes -= TF_PRIMARY_MIN
    else:
        elapsed_minutes = (elapsed_minutes // TF_PRIMARY_MIN) * TF_PRIMARY_MIN

    if elapsed_minutes < 0:
        return None
    return market_open + timedelta(minutes=elapsed_minutes)


def _snapshot_range() -> Tuple[datetime, datetime]:
    first = _expected_snapshot_time_for_tick(START)
    last = _expected_snapshot_time_for_tick(END)
    if first is None or last is None:
        raise ValueError("START/END do not resolve to completed snapshot times")
    if last < first:
        raise ValueError("Resolved snapshot range is invalid")
    return _naive_ist(first), _naive_ist(last)


# =============================================================================
# Database helpers
# =============================================================================


def _snapshot_exists(symbol: str, snapshot_time: datetime) -> bool:
    symbol_key = str(symbol or "").strip().upper()
    db_time = _naive_ist(snapshot_time)
    with get_trades_db() as db:
        return (
            db.query(SnapshotORM.symbol)
            .filter(
                SnapshotORM.symbol == symbol_key,
                SnapshotORM.snapshot_time == db_time,
            )
            .first()
            is not None
        )


def _clear_pipeline_data_and_reset_flags(
    *,
    symbols: Sequence[str],
) -> None:
    if not CLEAR_PIPELINE_DATA:
        logger.info(
            "CLEAR_PIPELINE_DATA=False; preserving auditlog, signals, trades, "
            "and snapshot processed flags"
        )
        return

    first_snapshot_time, last_snapshot_time = _snapshot_range()

    with get_trades_db() as db:
        try:
            audit_deleted = int(
                db.query(AuditLogORM).delete(synchronize_session=False)
            )
            trades_deleted = int(
                db.query(TradeORM).delete(synchronize_session=False)
            )
            signals_deleted = int(
                db.query(SignalORM).delete(synchronize_session=False)
            )
            flags_reset = int(
                db.query(SnapshotORM)
                .filter(SnapshotORM.symbol.in_(list(symbols)))
                .filter(SnapshotORM.snapshot_time >= first_snapshot_time)
                .filter(SnapshotORM.snapshot_time <= last_snapshot_time)
                .update(
                    {SnapshotORM.processed: False},
                    synchronize_session=False,
                )
            )
            db.commit()
        except Exception:
            db.rollback()
            raise


    logger.info(
        "Fresh replay reset complete | auditlog=%d user_trades=%d signals=%d "
        "snapshot_flags=%d",
        audit_deleted,
        trades_deleted,
        signals_deleted,
        flags_reset,
    )


def _unprocessed_keys_asof(
    *,
    asof_time: datetime,
    symbols: Sequence[str],
) -> List[Tuple[str, datetime]]:
    first_snapshot_time, last_snapshot_time = _snapshot_range()
    return SnapshotSchema.fetch_unprocessed_keys_asof(
        snapshot_time=_naive_ist(asof_time),
        symbols=list(symbols),
        start_time=first_snapshot_time,
        end_time=last_snapshot_time,
    )


def _mark_snapshots_processed(keys: Sequence[Tuple[str, datetime]]) -> None:
    """Atomically acknowledge snapshots after the full pipeline step succeeds."""
    if not keys:
        return

    with get_trades_db() as db:
        try:
            for symbol, snapshot_time in keys:
                rec = (
                    db.query(SnapshotORM)
                    .filter(
                        SnapshotORM.symbol == str(symbol).strip().upper(),
                        SnapshotORM.snapshot_time == snapshot_time,
                    )
                    .one_or_none()
                )
                if rec is None:
                    raise RuntimeError(
                        f"Snapshot disappeared before acknowledgement: "
                        f"{symbol} @ {snapshot_time}"
                    )
                rec.processed = True
            db.commit()
        except Exception:
            db.rollback()
            raise


def _remaining_unprocessed_count(symbols: Sequence[str]) -> int:
    first_snapshot_time, last_snapshot_time = _snapshot_range()
    with get_trades_db() as db:
        return int(
            db.query(SnapshotORM)
            .filter(SnapshotORM.symbol.in_(list(symbols)))
            .filter(SnapshotORM.snapshot_time >= first_snapshot_time)
            .filter(SnapshotORM.snapshot_time <= last_snapshot_time)
            .filter(SnapshotORM.processed == False)  # noqa: E712
            .count()
        )


def _count_user_trades() -> int:
    with get_trades_db() as db:
        return int(db.query(TradeORM).count())


# =============================================================================
# Pipeline jobs
# =============================================================================


def job_generate_snapshots(
    *,
    current_time: datetime,
    symbol_rows: Sequence[Any],
    api_key: str,
    access_token: str,
) -> Dict[str, int]:
    expected_snapshot_time = _expected_snapshot_time_for_tick(current_time)
    result = {
        "created": 0,
        "skipped_existing": 0,
        "no_snapshot": 0,
        "failed": 0,
    }

    if expected_snapshot_time is None:
        logger.info("No completed snapshot is available at tick %s", current_time)
        return result

    logger.info(
        "Snapshot job | tick=%s expected_snapshot_time=%s symbols=%d",
        current_time,
        expected_snapshot_time,
        len(symbol_rows),
    )

    for row in symbol_rows:
        symbol = str(getattr(row, "symbol", "") or "").strip().upper()
        token = int(getattr(row, "token"))

        if SKIP_EXISTING_SNAPSHOTS and _snapshot_exists(
            symbol,
            expected_snapshot_time,
        ):
            result["skipped_existing"] += 1
            continue

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
                result["no_snapshot"] += 1
                logger.warning(
                    "SnapshotGenerator returned None | symbol=%s tick=%s",
                    symbol,
                    current_time,
                )
                continue

            result["created"] += 1
        except Exception:
            result["failed"] += 1
            logger.exception(
                "Snapshot generation failed | symbol=%s tick=%s expected=%s",
                symbol,
                current_time,
                expected_snapshot_time,
            )

    for key, value in result.items():
        run_stats[f"snapshots_{key}"] += int(value)
    return result


def _run_signal_stage_for_group(
    keys: Sequence[Tuple[str, datetime]],
) -> Tuple[List[Tuple[str, datetime]], List[Tuple[str, datetime]]]:
    successful: List[Tuple[str, datetime]] = []
    failed: List[Tuple[str, datetime]] = []

    for symbol, snapshot_time in keys:
        try:
            snapshot = SnapshotSchema.fetch_snapshot(symbol, snapshot_time)
            if snapshot is None:
                raise RuntimeError(
                    f"Snapshot payload not found: {symbol} @ {snapshot_time}"
                )
            SignalGenerator(snapshot).generate_signal()
            successful.append((symbol, snapshot_time))
            run_stats["signals_processed"] += 1
        except Exception:
            failed.append((symbol, snapshot_time))
            run_stats["signal_errors"] += 1
            logger.exception(
                "Signal generation failed; snapshot remains unprocessed | "
                "symbol=%s snapshot_time=%s",
                symbol,
                snapshot_time,
            )

    return successful, failed


def job_generate_trades(current_time: datetime) -> int:
    before = _count_user_trades()
    try:
        if TEST_USERID:
            created = TradeGenerator().generate_user_trades(TEST_USERID) or []
        else:
            created = TradeGenerator().generate_user_trades() or []
    except Exception:
        logger.exception("TradeGenerator failed @ %s", current_time)
        raise

    after = _count_user_trades()
    returned = len(created) if isinstance(created, list) else int(created or 0)
    db_delta = after - before
    logger.info(
        "Trade generation complete | time=%s returned=%d db_delta=%d total=%d "
        "userid=%s",
        current_time,
        returned,
        db_delta,
        after,
        TEST_USERID or "ELIGIBLE_USERS",
    )
    run_stats["trades_created_returned"] += returned
    run_stats["trades_created_db_delta"] += db_delta
    return returned


def job_execute_trades(current_time: datetime, label: str) -> int:
    try:
        result = TradeExecutor().execute_all(snapshot_time=current_time)
    except Exception:
        logger.exception("TradeExecutor failed | pass=%s @ %s", label, current_time)
        raise

    count = len(result) if isinstance(result, list) else int(result or 0)
    logger.info(
        "TradeExecutor complete | pass=%s time=%s result_count=%d raw=%s",
        label,
        current_time,
        count,
        result,
    )
    return count


def job_monitor_trades(current_time: datetime) -> int:
    try:
        result = TradeMonitor().monitor(snapshot_time=current_time)
    except Exception:
        logger.exception("TradeMonitor failed @ %s", current_time)
        raise

    count = len(result) if isinstance(result, list) else int(result or 0)
    logger.info(
        "TradeMonitor complete | time=%s updated=%d raw=%s",
        current_time,
        count,
        result,
    )
    return count


def _run_downstream_pipeline(snapshot_time: datetime) -> bool:
    """Run the timestamp-level pipeline once; return False on any stage failure."""
    try:
        t0 = time.time()
        n_trades = job_generate_trades(snapshot_time)
        job_stats["trades"].append(time.time() - t0)
        logger.info(
            "trades: result=%d elapsed=%.3fs",
            n_trades,
            job_stats["trades"][-1],
        )

        t0 = time.time()
        n_entry = job_execute_trades(snapshot_time, "entry-pass")
        job_stats["execute_entry"].append(time.time() - t0)
        run_stats["entry_results"] += n_entry
        logger.info(
            "execute_entry: result=%d elapsed=%.3fs",
            n_entry,
            job_stats["execute_entry"][-1],
        )

        t0 = time.time()
        n_monitor = job_monitor_trades(snapshot_time)
        job_stats["monitor"].append(time.time() - t0)
        run_stats["monitor_results"] += n_monitor
        logger.info(
            "monitor: result=%d elapsed=%.3fs",
            n_monitor,
            job_stats["monitor"][-1],
        )

        t0 = time.time()
        n_exit = job_execute_trades(snapshot_time, "exit-pass")
        job_stats["execute_exit"].append(time.time() - t0)
        run_stats["exit_results"] += n_exit
        logger.info(
            "execute_exit: result=%d elapsed=%.3fs",
            n_exit,
            job_stats["execute_exit"][-1],
        )
        return True
    except Exception:
        run_stats["pipeline_group_errors"] += 1
        logger.exception(
            "Timestamp pipeline failed; snapshots remain unprocessed | "
            "snapshot_time=%s",
            snapshot_time,
        )
        return False


def _process_unprocessed_snapshots_asof(
    *,
    asof_time: datetime,
    symbols: Sequence[str],
) -> int:
    """Process chronological unprocessed groups available at or before asof_time.

    A downstream stage runs once per snapshot timestamp, after all available
    symbols for that timestamp have passed SignalGenerator. Only the successful
    snapshot keys are acknowledged, and only after the complete downstream step
    succeeds.

    On any failure at the earliest unresolved timestamp, processing stops for
    this API tick. The next tick (or a restarted program) retries that unresolved
    timestamp before moving forward, preserving chronological replay order.
    """
    groups_completed = 0

    while True:
        keys = _unprocessed_keys_asof(asof_time=asof_time, symbols=symbols)
        if not keys:
            return groups_completed

        earliest_time = keys[0][1]
        group_keys = [key for key in keys if key[1] == earliest_time]

        logger.info(
            "Processing snapshot group | snapshot_time=%s count=%d",
            earliest_time,
            len(group_keys),
        )


        t0 = time.time()
        successful_keys, failed_keys = _run_signal_stage_for_group(group_keys)
        job_stats["signals"].append(time.time() - t0)
        logger.info(
            "signals: successful=%d failed=%d elapsed=%.3fs",
            len(successful_keys),
            len(failed_keys),
            job_stats["signals"][-1],
        )

        if not successful_keys:
            logger.error(
                "No snapshot in the earliest group completed SignalGenerator; "
                "deferring this and later timestamps | snapshot_time=%s",
                earliest_time,
            )
            return groups_completed

        if not _run_downstream_pipeline(earliest_time):
            return groups_completed

        try:
            _mark_snapshots_processed(successful_keys)
        except Exception:
            run_stats["mark_processed_errors"] += 1
            logger.exception(
                "Failed to mark completed snapshots processed; they will be retried | "
                "snapshot_time=%s keys=%s",
                earliest_time,
                successful_keys,
            )
            return groups_completed

        run_stats["snapshots_marked_processed"] += len(successful_keys)
        run_stats["pipeline_groups_completed"] += 1
        groups_completed += 1

        logger.info(
            "Snapshot group complete and acknowledged | snapshot_time=%s "
            "processed=%d unresolved=%d",
            earliest_time,
            len(successful_keys),
            len(failed_keys),
        )

        # A failed signal at this timestamp must be retried before advancing.
        if failed_keys:
            return groups_completed


# =============================================================================
# Reporting
# =============================================================================


def _enum_str(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().upper()


def _serialize_csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    return value


def _export_orm_table(model: Any, path: Path) -> int:
    columns = [column.name for column in model.__table__.columns]
    with get_trades_db() as db:
        primary_key = list(model.__table__.primary_key.columns)[0]
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


def _pipeline_summary() -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "signals": 0,
        "trades": 0,
        "audit_rows": 0,
        "entry_status": defaultdict(int),
        "exit_status": defaultdict(int),
        "instrument_type": defaultdict(int),
        "execution_mode": defaultdict(int),
    }

    with get_trades_db() as db:
        out["signals"] = int(db.query(SignalORM).count())
        out["audit_rows"] = int(db.query(AuditLogORM).count())
        trades = db.query(TradeORM).all()

    out["trades"] = len(trades)
    for trade in trades:
        out["entry_status"][_enum_str(getattr(trade, "entry_status", ""))] += 1
        out["exit_status"][_enum_str(getattr(trade, "exit_status", ""))] += 1
        out["instrument_type"][_enum_str(getattr(trade, "instrument_type", ""))] += 1
        out["execution_mode"][_enum_str(getattr(trade, "execution_mode", ""))] += 1

    return out


def _log_pipeline_summary() -> Dict[str, Any]:
    summary = _pipeline_summary()
    logger.info("=== DB REPLAY OUTPUT SUMMARY ===")
    logger.info(
        "signals=%s trades=%s audit_rows=%s",
        summary["signals"],
        summary["trades"],
        summary["audit_rows"],
    )
    for key in ("entry_status", "exit_status", "instrument_type", "execution_mode"):
        logger.info("%s=%s", key, dict(sorted(summary[key].items())))
    logger.info("================================")
    return summary


def _write_reports(
    *,
    started_at: datetime,
    elapsed_seconds: float,
    symbols: Sequence[str],
    credential_source: str,
    remaining_unprocessed: int,
    db_summary: Dict[str, Any],
) -> None:
    if not GENERATE_CSV_REPORTS:
        logger.info("GENERATE_CSV_REPORTS=False; CSV reports were not written")
        return

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S")

    exports = {
        "signals": _export_orm_table(
            SignalORM,
            REPORT_DIR / f"signals_{stamp}.csv",
        ),
        "user_trades": _export_orm_table(
            TradeORM,
            REPORT_DIR / f"user_trades_{stamp}.csv",
        ),
        "auditlog": _export_orm_table(
            AuditLogORM,
            REPORT_DIR / f"auditlog_{stamp}.csv",
        ),
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
        "active_only": ACTIVE_ONLY,
        "skip_existing_snapshots": SKIP_EXISTING_SNAPSHOTS,
        "clear_pipeline_data": CLEAR_PIPELINE_DATA,
        "test_userid": TEST_USERID or "ELIGIBLE_USERS",
        "data_user_id": DATA_USER_ID,
        "credential_source": credential_source,
        "remaining_unprocessed": remaining_unprocessed,
        "db_signals": db_summary["signals"],
        "db_trades": db_summary["trades"],
        "db_audit_rows": db_summary["audit_rows"],
        "entry_status": json.dumps(dict(sorted(db_summary["entry_status"].items()))),
        "exit_status": json.dumps(dict(sorted(db_summary["exit_status"].items()))),
        "instrument_type": json.dumps(dict(sorted(db_summary["instrument_type"].items()))),
        "execution_mode": json.dumps(dict(sorted(db_summary["execution_mode"].items()))),
        **dict(sorted(run_stats.items())),
        **{f"exported_{key}": value for key, value in exports.items()},
    }

    summary_path = REPORT_DIR / f"replay_summary_{stamp}.csv"
    with summary_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary_row.keys()))
        writer.writeheader()
        writer.writerow({key: _serialize_csv_value(value) for key, value in summary_row.items()})

    logger.info(
        "CSV reports written | signals=%d trades=%d audit=%d summary=%s",
        exports["signals"],
        exports["user_trades"],
        exports["auditlog"],
        summary_path,
    )


# =============================================================================
# Driver
# =============================================================================


def run_replay(
    *,
    symbol_rows: Sequence[Any],
    api_key: str,
    access_token: str,
) -> int:
    symbols = [
        str(getattr(row, "symbol", "") or "").strip().upper()
        for row in symbol_rows
    ]

    _clear_pipeline_data_and_reset_flags(
        symbols=symbols,
    )

    current = START
    loops = 0
    while current <= END:
        loops += 1
        run_stats["api_ticks"] += 1
        logger.info("=== REPLAY API TICK @ %s ===", current)

        t0 = time.time()
        snapshot_result = job_generate_snapshots(
            current_time=current,
            symbol_rows=symbol_rows,
            api_key=api_key,
            access_token=access_token,
        )
        job_stats["snapshots"].append(time.time() - t0)
        logger.info(
            "snapshots: %s elapsed=%.3fs",
            snapshot_result,
            job_stats["snapshots"][-1],
        )

        expected_snapshot_time = _expected_snapshot_time_for_tick(current)
        if expected_snapshot_time is not None:
            completed = _process_unprocessed_snapshots_asof(
                asof_time=expected_snapshot_time,
                symbols=symbols,
            )
            logger.info(
                "pipeline groups completed through %s: %d",
                expected_snapshot_time,
                completed,
            )

        current += timedelta(minutes=STEP_MINUTES)

    remaining = _remaining_unprocessed_count(symbols)
    if remaining:
        logger.warning(
            "Replay ended with %d unprocessed snapshots; restart with "
            "CLEAR_PIPELINE_DATA=False to retry them.",
            remaining,
        )

    logger.info("Replay loop complete | api_ticks=%d remaining_unprocessed=%d", loops, remaining)
    return remaining


def _log_timing_summary() -> None:
    logger.info("=== REPLAY TIMING SUMMARY ===")
    for name, times in job_stats.items():
        if not times:
            continue
        total = sum(times)
        average = total / len(times)
        logger.info(
            "%s: total=%.3fs avg=%.3fs runs=%d",
            name,
            total,
            average,
            len(times),
        )
    logger.info("=============================")


def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    setup_logging(log_file=str(LOG_FILE))
    global logger
    logger = logging.getLogger(__name__)

    started_at = datetime.now(IST)
    t0_all = time.time()

    # Startup/preflight failures stop the run because continuing would be unsafe.
    trading_day = _trading_day()
    symbol_rows = _selected_symbol_rows()
    symbols = [
        str(getattr(row, "symbol", "") or "").strip().upper()
        for row in symbol_rows
    ]
    api_key, access_token, credential_source = _resolve_api_credentials()

    logger.info(
        "Starting replay_summary | trading_day=%s start=%s end=%s step=%dm "
        "symbols=%s active_only=%s userid=%s clear=%s skip_existing=%s "
        "csv_reports=%s data_user=%s credentials=%s",
        trading_day,
        START,
        END,
        STEP_MINUTES,
        "ALL" if _normalized_symbol_selection() == ["ALL"] else symbols,
        ACTIVE_ONLY,
        TEST_USERID or "ELIGIBLE_USERS",
        CLEAR_PIPELINE_DATA,
        SKIP_EXISTING_SNAPSHOTS,
        GENERATE_CSV_REPORTS,
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

    remaining_unprocessed = -1
    try:
        remaining_unprocessed = run_replay(
            symbol_rows=symbol_rows,
            api_key=api_key,
            access_token=access_token,
        )
    finally:
        EXECUTION_CONFIG.use_snapshot = old_use_snapshot
        EXECUTION_CONFIG.use_live_price_for_virtual = old_use_live_price_for_virtual
        EXECUTION_CONFIG.force_virtual_for_replay = old_force_virtual_for_replay

    elapsed = time.time() - t0_all
    _log_timing_summary()
    db_summary = _log_pipeline_summary()
    _write_reports(
        started_at=started_at,
        elapsed_seconds=elapsed,
        symbols=symbols,
        credential_source=credential_source,
        remaining_unprocessed=remaining_unprocessed,
        db_summary=db_summary,
    )

    logger.info(
        "Finished replay_summary | elapsed=%.3fs remaining_unprocessed=%d",
        elapsed,
        remaining_unprocessed,
    )


if __name__ == "__main__":
    main()
