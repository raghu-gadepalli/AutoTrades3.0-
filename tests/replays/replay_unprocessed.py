#!/usr/bin/env python3
"""Process existing unprocessed snapshots with parallel signal generation.

This is the threaded companion to ``replay_unprocessed.py``.

It never generates snapshots and never clears database tables. The script reads
all rows where ``Snapshot.processed = False`` and processes them in chronological
snapshot cadences:

    all unprocessed snapshots at one snapshot_time
        -> SignalGenerator in parallel across symbols
        -> mark each successful snapshot processed immediately
        -> optional TradeGenerator once for the cadence
        -> optional TradeExecutor entry pass once for the cadence
        -> optional TradeMonitor pass once for the cadence
        -> optional TradeExecutor exit pass once for the cadence

Only signal evaluation is parallel. The downstream trade pipeline remains
single-threaded and runs once per cadence, matching the live service order.

Restart safety
--------------
A snapshot is marked processed inside its worker immediately after
SignalGenerator succeeds. Failed snapshots remain unprocessed. Later snapshots
for a symbol that failed are deferred during the current run so symbol-local
chronology is never skipped. Restarting the script automatically retries the
remaining rows.

There are no date filters, symbol filters, snapshot creation, clearing options,
or checkpoint files. Operational defaults are visible below and may be overridden
from the command line.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from itertools import groupby
import json
import logging
import os
import sys
import time
from typing import Callable, Dict, Iterator, List, Optional, Tuple
from zoneinfo import ZoneInfo

# Allow imports from the project root.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from configs.execution_config import EXECUTION_CONFIG
from database.database import get_trades_db
from logconfig import setup_logging
from models.trade_models import Snapshot as SnapshotORM
from schemas.snapshot import SnapshotSchema
from services.signals.signal_generator import SignalGenerator
from services.trade.executor import trade_executor as executor_module
from services.trade.executor.trade_executor import TradeExecutor
from services.trade.generator import tradegen_helper as tradegen_helper_module
from services.trade.generator import tradegen_validator as tradegen_validator_module
from services.trade.generator.trade_generator import TradeGenerator
from services.trade.monitor.trade_monitor import TradeMonitor
from tests.replays.replay_price_provider import replay_price_provider


# =============================================================================
# SOURCE DEFAULTS - CLI values override these
# =============================================================================

# False -> generate/update signals only.
# True  -> additionally run trade generation, entry, monitor and exit once per
#          snapshot cadence.
GENERATE_TRADES: bool = True

# The backtest is intended to be run for one user at a time. This value is only
# passed to the normal TradeGenerator; executor and monitor keep their normal
# service behavior.
REPLAY_USERID: Optional[str] = "DR1812"

# Parallelism applies only to SignalGenerator work for different symbols at the
# same snapshot cadence.
SIGNAL_MAX_WORKERS: int = 3

# Log individual signal evaluations above this duration as slow.
SLOW_SIGNAL_SECONDS: float = 5.0

LOG_FILE = "reports/replay_unprocessed.log"
IST = ZoneInfo("Asia/Kolkata")

logger = logging.getLogger(__name__)
SnapshotKey = Tuple[str, datetime]


# =============================================================================
# Result model
# =============================================================================


@dataclass(frozen=True)
class SignalResult:
    symbol: str
    snapshot_time: datetime
    action: str
    elapsed_seconds: float
    success: bool
    error: Optional[str] = None


# =============================================================================
# Snapshot queue
# =============================================================================


def _fetch_unprocessed_keys() -> List[SnapshotKey]:
    """Return the restart queue in chronological live order."""
    with get_trades_db() as db:
        rows = (
            db.query(SnapshotORM.symbol, SnapshotORM.snapshot_time)
            .filter(SnapshotORM.processed == False)  # noqa: E712
            .order_by(
                SnapshotORM.snapshot_time.asc(),
                SnapshotORM.symbol.asc(),
            )
            .all()
        )

    seen: set[SnapshotKey] = set()
    keys: List[SnapshotKey] = []
    for symbol, snapshot_time in rows:
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol or not isinstance(snapshot_time, datetime):
            continue
        key = (normalized_symbol, snapshot_time)
        if key in seen:
            logger.warning(
                "Duplicate unprocessed snapshot key found; processing once | "
                "symbol=%s snapshot_time=%s",
                normalized_symbol,
                snapshot_time,
            )
            continue
        seen.add(key)
        keys.append(key)
    return keys


def _load_unprocessed_group(
    snapshot_time: datetime,
) -> Tuple[List[SnapshotSchema], set[str]]:
    """Load one cadence and return symbols whose payload could not be loaded."""
    with get_trades_db() as db:
        rows = (
            db.query(SnapshotORM)
            .filter(
                SnapshotORM.processed == False,  # noqa: E712
                SnapshotORM.snapshot_time == snapshot_time,
            )
            .order_by(SnapshotORM.symbol.asc())
            .all()
        )

    snapshots: List[SnapshotSchema] = []
    invalid_symbols: set[str] = set()
    seen_symbols: set[str] = set()

    for row in rows:
        symbol = str(row.symbol or "").strip().upper()
        if symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)

        try:
            raw = row.data
            if isinstance(raw, str):
                payload = json.loads(raw)
            elif isinstance(raw, dict):
                payload = dict(raw)
            else:
                raise TypeError("snapshot.data must be an object")

            if str(payload["symbol"]).strip().upper() != symbol:
                raise ValueError("snapshot JSON symbol differs from DB symbol")

            payload_time = payload["snapshot_time"]
            if isinstance(payload_time, str):
                payload_time = datetime.fromisoformat(payload_time)
            if not isinstance(payload_time, datetime):
                raise TypeError("snapshot JSON snapshot_time must be a datetime")
            if payload_time.replace(tzinfo=None) != row.snapshot_time.replace(tzinfo=None):
                raise ValueError("snapshot JSON time differs from DB snapshot_time")

            payload["ltp"] = float(row.ltp) if row.ltp is not None else None
            payload["ltp_time"] = row.ltp_time
            snapshots.append(SnapshotSchema.from_db_dict(payload))
        except Exception:
            invalid_symbols.add(symbol)
            logger.exception(
                "Invalid snapshot payload; row remains unprocessed | symbol=%s "
                "snapshot_time=%s",
                symbol,
                row.snapshot_time,
            )

    return snapshots, invalid_symbols


def _mark_processed(snapshot: SnapshotSchema) -> None:
    if not SnapshotSchema.mark_processed(snapshot.symbol, snapshot.snapshot_time):
        raise RuntimeError(
            "Snapshot could not be marked processed: "
            f"{snapshot.symbol} @ {snapshot.snapshot_time}"
        )


def _remaining_unprocessed_count() -> int:
    with get_trades_db() as db:
        return int(
            db.query(SnapshotORM)
            .filter(SnapshotORM.processed == False)  # noqa: E712
            .count()
        )


# =============================================================================
# Replay-safe pricing/time context
# =============================================================================


@contextmanager
def _deterministic_replay_clock() -> Iterator[Callable[[datetime], None]]:
    """Make trade-generation/execution wall clocks follow replay snapshot time."""
    clock: Dict[str, Optional[datetime]] = {"current": None}

    original_helper_now = tradegen_helper_module.business_now_naive
    original_validator_now = tradegen_validator_module.business_now_naive
    original_executor_now = executor_module._now_ist_naive

    def replay_now() -> datetime:
        current = clock["current"]
        if current is None:
            raise RuntimeError("Replay clock used before a snapshot time was assigned")
        if current.tzinfo is not None:
            return current.astimezone(IST).replace(tzinfo=None)
        return current

    def set_time(value: datetime) -> None:
        if not isinstance(value, datetime):
            raise TypeError("Replay clock requires a datetime snapshot_time")
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
def _snapshot_execution_mode() -> Iterator[None]:
    """Use persisted snapshot prices and force virtual execution for replay."""
    old_use_snapshot = EXECUTION_CONFIG.use_snapshot
    old_live_virtual = EXECUTION_CONFIG.use_live_price_for_virtual
    old_force_virtual = EXECUTION_CONFIG.force_virtual_for_replay

    EXECUTION_CONFIG.use_snapshot = True
    EXECUTION_CONFIG.use_live_price_for_virtual = False
    EXECUTION_CONFIG.force_virtual_for_replay = True
    try:
        yield
    finally:
        EXECUTION_CONFIG.use_snapshot = old_use_snapshot
        EXECUTION_CONFIG.use_live_price_for_virtual = old_live_virtual
        EXECUTION_CONFIG.force_virtual_for_replay = old_force_virtual


# =============================================================================
# Parallel signal work
# =============================================================================


def _process_signal_snapshot(snapshot: SnapshotSchema) -> SignalResult:
    """Evaluate and acknowledge one snapshot inside a worker thread."""
    started = time.perf_counter()
    try:
        action = str(SignalGenerator(snapshot).generate() or "NO_ACTION")
        # Restart safety: acknowledge immediately after this snapshot's signal
        # evaluation succeeds. Trade services persist their own lifecycle.
        _mark_processed(snapshot)
        elapsed = time.perf_counter() - started
        log = logger.warning if elapsed >= float(SLOW_SIGNAL_SECONDS) else logger.debug
        log(
            "SIGNAL_SNAPSHOT_DONE | %s @ %s | action=%s elapsed=%.3fs",
            snapshot.symbol,
            snapshot.snapshot_time,
            action,
            elapsed,
        )
        return SignalResult(
            symbol=snapshot.symbol,
            snapshot_time=snapshot.snapshot_time,
            action=action,
            elapsed_seconds=elapsed,
            success=True,
        )
    except Exception as exc:
        elapsed = time.perf_counter() - started
        logger.exception(
            "Signal processing failed; snapshot remains unprocessed | symbol=%s "
            "snapshot_time=%s",
            snapshot.symbol,
            snapshot.snapshot_time,
        )
        return SignalResult(
            symbol=snapshot.symbol,
            snapshot_time=snapshot.snapshot_time,
            action="ERROR",
            elapsed_seconds=elapsed,
            success=False,
            error=f"{type(exc).__name__}: {exc}",
        )


def _generate_signals_for_cadence(
    snapshots: List[SnapshotSchema],
    *,
    failed_symbols: set[str],
    executor: ThreadPoolExecutor,
    stats: Counter[str],
) -> int:
    """Evaluate one cadence in parallel and wait before downstream jobs."""
    eligible = [
        snapshot for snapshot in snapshots if snapshot.symbol not in failed_symbols
    ]
    stats["skipped_after_symbol_error"] += len(snapshots) - len(eligible)

    if not eligible:
        return 0

    future_map: Dict[Future[SignalResult], SnapshotSchema] = {
        executor.submit(_process_signal_snapshot, snapshot): snapshot
        for snapshot in eligible
    }

    completed = 0
    results: List[SignalResult] = []
    for future in as_completed(future_map):
        snapshot = future_map[future]
        try:
            result = future.result()
        except Exception as exc:
            # Defensive boundary: _process_signal_snapshot already catches its
            # own errors, but a worker crash must not terminate later symbols.
            logger.exception(
                "Unexpected signal worker crash; snapshot remains unprocessed | "
                "symbol=%s snapshot_time=%s",
                snapshot.symbol,
                snapshot.snapshot_time,
            )
            result = SignalResult(
                symbol=snapshot.symbol,
                snapshot_time=snapshot.snapshot_time,
                action="WORKER_ERROR",
                elapsed_seconds=0.0,
                success=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        results.append(result)

    # Deterministic summary order even though futures complete out of order.
    results.sort(key=lambda item: item.symbol)
    for result in results:
        if result.success:
            completed += 1
            stats["snapshots_processed"] += 1
            stats[f"signal_action:{result.action}"] += 1
        else:
            failed_symbols.add(result.symbol)
            stats["signal_errors"] += 1
            logger.error(
                "Signal result failed; later snapshots for symbol deferred | "
                "symbol=%s snapshot_time=%s error=%s",
                result.symbol,
                result.snapshot_time,
                result.error,
            )

    return completed


# =============================================================================
# Live-style downstream cadence
# =============================================================================


def _run_trade_cadence(
    *,
    snapshot_time: datetime,
    trade_generator: TradeGenerator,
    trade_executor: TradeExecutor,
    trade_monitor: TradeMonitor,
    stats: Counter[str],
) -> None:
    """Run the same downstream order once for the whole replay cadence."""
    stage_started = time.perf_counter()
    try:
        created = trade_generator.generate_user_trades(REPLAY_USERID) or []
        created_count = len(created) if isinstance(created, list) else int(created or 0)
        stats["trades_created"] += created_count
        logger.info(
            "TRADE_GENERATOR_DONE | snapshot_time=%s created=%d elapsed=%.3fs",
            snapshot_time,
            created_count,
            time.perf_counter() - stage_started,
        )
    except Exception:
        stats["trade_generator_errors"] += 1
        logger.exception("TradeGenerator failed @ %s; continuing", snapshot_time)

    stage_started = time.perf_counter()
    try:
        entry_updates = int(trade_executor.execute_all(snapshot_time=snapshot_time) or 0)
        stats["entry_updates"] += entry_updates
        logger.info(
            "EXECUTOR_ENTRY_DONE | snapshot_time=%s updates=%d elapsed=%.3fs",
            snapshot_time,
            entry_updates,
            time.perf_counter() - stage_started,
        )
    except Exception:
        stats["entry_executor_errors"] += 1
        logger.exception("TradeExecutor entry pass failed @ %s; continuing", snapshot_time)

    stage_started = time.perf_counter()
    try:
        monitor_updates = int(trade_monitor.monitor(snapshot_time=snapshot_time) or 0)
        stats["monitor_updates"] += monitor_updates
        pass_errors = list(trade_monitor.last_pass_errors)
        stats["monitor_item_errors"] += len(pass_errors)
        for error in pass_errors:
            logger.error(
                "TradeMonitor per-trade failure; continuing | snapshot_time=%s error=%s",
                snapshot_time,
                error,
            )
        logger.info(
            "TRADE_MONITOR_DONE | snapshot_time=%s updates=%d item_errors=%d "
            "elapsed=%.3fs",
            snapshot_time,
            monitor_updates,
            len(pass_errors),
            time.perf_counter() - stage_started,
        )
    except Exception:
        stats["monitor_pass_errors"] += 1
        logger.exception("TradeMonitor pass failed @ %s; continuing", snapshot_time)

    stage_started = time.perf_counter()
    try:
        exit_updates = int(trade_executor.execute_all(snapshot_time=snapshot_time) or 0)
        stats["exit_updates"] += exit_updates
        logger.info(
            "EXECUTOR_EXIT_DONE | snapshot_time=%s updates=%d elapsed=%.3fs",
            snapshot_time,
            exit_updates,
            time.perf_counter() - stage_started,
        )
    except Exception:
        stats["exit_executor_errors"] += 1
        logger.exception("TradeExecutor exit pass failed @ %s; continuing", snapshot_time)


# =============================================================================
# Driver
# =============================================================================


def run() -> int:
    if int(SIGNAL_MAX_WORKERS) < 1:
        raise ValueError("SIGNAL_MAX_WORKERS must be >= 1")

    keys = _fetch_unprocessed_keys()
    if not keys:
        logger.info("No unprocessed snapshots found")
        return 0

    distinct_times = len({snapshot_time for _, snapshot_time in keys})
    distinct_symbols = len({symbol for symbol, _ in keys})
    workers = min(int(SIGNAL_MAX_WORKERS), max(1, distinct_symbols))
    logger.info(
        "Unprocessed threaded replay queue | snapshots=%d cadences=%d symbols=%d "
        "workers=%d first=%s last=%s generate_trades=%s userid=%s",
        len(keys),
        distinct_times,
        distinct_symbols,
        workers,
        keys[0][1],
        keys[-1][1],
        GENERATE_TRADES,
        REPLAY_USERID if GENERATE_TRADES else "NOT_USED",
    )

    stats: Counter[str] = Counter()
    failed_symbols: set[str] = set()
    started = time.perf_counter()

    trade_generator = TradeGenerator() if GENERATE_TRADES else None
    trade_executor = TradeExecutor() if GENERATE_TRADES else None
    trade_monitor = TradeMonitor() if GENERATE_TRADES else None

    @contextmanager
    def replay_context() -> Iterator[Optional[Callable[[datetime], None]]]:
        if not GENERATE_TRADES:
            yield None
            return
        with (
            _snapshot_execution_mode(),
            _deterministic_replay_clock() as set_time,
            replay_price_provider(),
        ):
            yield set_time

    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="replay-signal",
    ) as signal_executor, replay_context() as set_replay_time:
        for snapshot_time, key_group in groupby(keys, key=lambda item: item[1]):
            group_keys = list(key_group)
            cadence_started = time.perf_counter()

            if set_replay_time is not None:
                set_replay_time(snapshot_time)

            # Reload from DB so rows completed by a prior partial/concurrent run
            # are naturally excluded.
            snapshots, load_failed_symbols = _load_unprocessed_group(snapshot_time)
            failed_symbols.update(load_failed_symbols)
            stats["snapshot_load_errors"] += len(load_failed_symbols)

            signal_count = _generate_signals_for_cadence(
                snapshots,
                failed_symbols=failed_symbols,
                executor=signal_executor,
                stats=stats,
            )

            # Barrier is complete here: all signal workers for this cadence have
            # returned and successful rows are already marked processed.
            if GENERATE_TRADES:
                assert trade_generator is not None
                assert trade_executor is not None
                assert trade_monitor is not None
                _run_trade_cadence(
                    snapshot_time=snapshot_time,
                    trade_generator=trade_generator,
                    trade_executor=trade_executor,
                    trade_monitor=trade_monitor,
                    stats=stats,
                )

            stats["cadences_processed"] += 1
            logger.info(
                "CADENCE_DONE | snapshot_time=%s queued=%d loaded_unprocessed=%d "
                "signals_completed=%d workers=%d elapsed=%.3fs",
                snapshot_time,
                len(group_keys),
                len(snapshots),
                signal_count,
                workers,
                time.perf_counter() - cadence_started,
            )

    remaining = _remaining_unprocessed_count()
    logger.info(
        "Threaded replay complete | elapsed=%.3fs stats=%s failed_symbols=%s "
        "remaining_unprocessed=%d",
        time.perf_counter() - started,
        dict(sorted(stats.items())),
        sorted(failed_symbols),
        remaining,
    )

    # Per-snapshot failures are visible and retryable but do not terminate the
    # remainder of the replay. Return non-zero so automation can detect them.
    return 1 if stats["signal_errors"] or stats["snapshot_load_errors"] else 0


def _args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Process persisted unprocessed snapshots in causal cadence order. "
            "Defaults live in this file; CLI values override them."
        )
    )
    parser.add_argument(
        "--generate-trades",
        action=argparse.BooleanOptionalAction,
        default=GENERATE_TRADES,
        help=f"Run trade pipeline after each cadence (default: {GENERATE_TRADES})",
    )
    parser.add_argument("--userid", default=REPLAY_USERID)
    parser.add_argument("--max-workers", type=int, default=SIGNAL_MAX_WORKERS)
    parser.add_argument("--slow-signal-seconds", type=float, default=SLOW_SIGNAL_SECONDS)
    parser.add_argument("--log-file", default=LOG_FILE)
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _args(argv)
    global logger, GENERATE_TRADES, REPLAY_USERID, SIGNAL_MAX_WORKERS
    global SLOW_SIGNAL_SECONDS, LOG_FILE
    GENERATE_TRADES = bool(args.generate_trades)
    REPLAY_USERID = str(args.userid).strip() if args.userid is not None else None
    SIGNAL_MAX_WORKERS = max(1, int(args.max_workers))
    SLOW_SIGNAL_SECONDS = max(0.0, float(args.slow_signal_seconds))
    LOG_FILE = str(args.log_file)

    setup_logging(log_file=LOG_FILE)
    logger = logging.getLogger(__name__)

    logger.info(
        "Starting replay_unprocessed | generate_trades=%s userid=%s "
        "signal_workers=%d snapshot_generation=NEVER "
        "clearing=NEVER processing=LIVE_CADENCE_THREADED",
        GENERATE_TRADES,
        REPLAY_USERID if GENERATE_TRADES else "NOT_USED",
        SIGNAL_MAX_WORKERS,
    )

    try:
        return run()
    except KeyboardInterrupt:
        logger.info(
            "Interrupted; every completed signal snapshot is already marked "
            "processed. Restart the same script to continue."
        )
        return 130
    except Exception:
        logger.exception("replay_unprocessed failed during startup/preflight")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
