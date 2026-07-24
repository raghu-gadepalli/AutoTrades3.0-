#!/usr/bin/env python3
"""Process every persisted snapshot that is not marked as processed.

This is the small downstream replay utility used after snapshots have already
been generated.  It never generates snapshots, never clears data, and has no
command-line arguments or date/symbol filters.

Processing contract
-------------------

    Snapshot.processed = True
        -> skip

    Snapshot.processed = False
        -> run SignalGenerator
        -> optionally run the trade pipeline
        -> mark processed only after the selected work completes

Set ``GENERATE_TRADES`` below:

    False -> SignalGenerator only
    True  -> SignalGenerator, TradeGenerator, virtual TradeExecutor entry,
             TradeMonitor, and virtual TradeExecutor exit

Restarting the program is safe: completed snapshots remain processed and are
skipped; an interrupted snapshot remains unprocessed and is retried.  Signal
and trade persistence remain responsible for their normal idempotency.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from itertools import groupby
import logging
import os
import sys
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

# Allow imports from the project root.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from configs.execution_config import EXECUTION_CONFIG
from database.database import get_trades_db
from logconfig import setup_logging
from models.trade_models import Snapshot as SnapshotORM
from schemas.snapshot import SnapshotSchema
from schemas.user import UserSchema
from schemas.user_trade import UserTradeSchema
from services.signals.signal_generator import SignalGenerator
from services.trade.executor import trade_executor as executor_module
from services.trade.executor.trade_executor import TradeExecutor
from services.trade.generator import tradegen_helper as tradegen_helper_module
from services.trade.generator import tradegen_validator as tradegen_validator_module
from services.trade.generator.trade_generator import TradeGenerator
from services.trade.monitor.trade_monitor import TradeMonitor


# =============================================================================
# HARD-CODED OPTIONS
# =============================================================================

# False: generate/update signals only.
# True:  also generate, execute, monitor, and exit VIRTUAL trades.
GENERATE_TRADES: bool = True

# Used only when GENERATE_TRADES=True.  This user must be AUTOGEN-eligible and
# must use VIRTUAL execution.  The utility never processes another user's
# trades.
REPLAY_USERID: str = "DR1812"

LOG_FILE = "reports/replay_unprocessed.log"
IST = ZoneInfo("Asia/Kolkata")

logger = logging.getLogger(__name__)

SnapshotKey = Tuple[str, datetime]


# =============================================================================
# Snapshot queue helpers
# =============================================================================


def _fetch_unprocessed_keys() -> List[SnapshotKey]:
    """Return the complete restart queue in chronological order."""
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

    return [
        (str(symbol).strip().upper(), snapshot_time)
        for symbol, snapshot_time in rows
    ]


def _load_snapshot(symbol: str, snapshot_time: datetime) -> SnapshotSchema:
    """Load one snapshot with DB-only LTP fields restored."""
    with get_trades_db() as db:
        row = (
            db.query(SnapshotORM)
            .filter(SnapshotORM.symbol == symbol)
            .filter(SnapshotORM.snapshot_time == snapshot_time)
            .first()
        )

    if row is None or not isinstance(row.data, dict):
        raise RuntimeError(f"Snapshot payload not found: {symbol} @ {snapshot_time}")

    payload = dict(row.data)
    payload["ltp"] = float(row.ltp) if row.ltp is not None else None
    payload["ltp_time"] = row.ltp_time
    return SnapshotSchema.from_db_dict(payload)


def _mark_processed(symbol: str, snapshot_time: datetime) -> None:
    if not SnapshotSchema.mark_processed(symbol, snapshot_time):
        raise RuntimeError(
            f"Snapshot could not be marked processed: {symbol} @ {snapshot_time}"
        )


def _remaining_unprocessed_count() -> int:
    with get_trades_db() as db:
        return int(
            db.query(SnapshotORM)
            .filter(SnapshotORM.processed == False)  # noqa: E712
            .count()
        )


# =============================================================================
# Replay safety helpers
# =============================================================================


def _enum_str(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().upper()


def _validate_replay_user(userid: str) -> UserSchema:
    user_key = str(userid or "").strip()
    if not user_key:
        raise ValueError("REPLAY_USERID cannot be blank when GENERATE_TRADES=True")

    user = UserSchema.fetch_user(user_key)
    if user is None:
        raise LookupError(f"Replay user not found: {user_key}")

    failures: List[str] = []
    if int(getattr(user, "active", 0) or 0) != 1:
        failures.append("users.active must be 1")
    if int(getattr(user, "logged_in", 0) or 0) != 1:
        failures.append("users.logged_in must be 1")
    if int(getattr(user, "autotrade", 0) or 0) != 1:
        failures.append("users.autotrade must be 1")
    if _enum_str(getattr(user, "execution_mode", None)) != "VIRTUAL":
        failures.append("users.execution_mode must be VIRTUAL")

    if failures:
        raise ValueError(
            f"Replay user {user_key} is not eligible: " + "; ".join(failures)
        )
    return user


@contextmanager
def _restrict_monitor_to_user(userid: str) -> Iterator[None]:
    """Prevent TradeMonitor from reading another user's open positions."""
    replay_userid = str(userid).strip()
    original_descriptor = UserTradeSchema.__dict__["fetch_open_positions"]
    original_callable: Callable[..., List[UserTradeSchema]] = (
        UserTradeSchema.fetch_open_positions
    )

    def fetch_open_positions(
        *, userid: Optional[str] = None, symbol: Optional[str] = None
    ) -> List[UserTradeSchema]:
        if userid is not None and str(userid).strip() != replay_userid:
            raise ValueError("Replay monitor attempted to read another userid")
        return original_callable(userid=replay_userid, symbol=symbol)

    UserTradeSchema.fetch_open_positions = staticmethod(fetch_open_positions)
    try:
        yield
    finally:
        UserTradeSchema.fetch_open_positions = original_descriptor


@contextmanager
def _deterministic_replay_clock() -> Iterator[Callable[[datetime], None]]:
    """Route trade-generation/execution wall clocks to the active snapshot."""
    clock: Dict[str, Optional[datetime]] = {"current": None}

    original_helper_now = tradegen_helper_module.business_now_naive
    original_validator_now = tradegen_validator_module.business_now_naive
    original_executor_now = executor_module._now_ist_naive

    def replay_now() -> datetime:
        current = clock["current"]
        if current is None:
            raise RuntimeError("Replay clock used before snapshot time was assigned")
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
    """Force deterministic snapshot pricing for the VIRTUAL replay user."""
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
# Pipeline stages
# =============================================================================


def _run_signal(snapshot: SnapshotSchema) -> str:
    action = SignalGenerator(snapshot).generate()
    return str(action or "NO_ACTION")


def _run_trade_pipeline(
    *,
    snapshot_time: datetime,
    trade_generator: TradeGenerator,
    trade_executor: TradeExecutor,
    trade_monitor: TradeMonitor,
) -> Dict[str, int]:
    """Run the normal downstream trade stages once for this timestamp."""
    created = trade_generator.generate_user_trades(REPLAY_USERID) or []
    created_count = len(created) if isinstance(created, list) else int(created or 0)

    entry_updates = int(
        trade_executor.execute_user_once(
            userid=REPLAY_USERID,
            limit=500,
            snapshot_time=snapshot_time,
        )
        or 0
    )

    monitor_updates = int(
        trade_monitor.run_once(snapshot_time=snapshot_time) or 0
    )
    monitor_errors = list(trade_monitor.last_pass_errors)
    if monitor_errors:
        raise RuntimeError(
            "TradeMonitor completed with per-trade errors: "
            f"count={len(monitor_errors)} errors={monitor_errors}"
        )

    exit_updates = int(
        trade_executor.execute_user_once(
            userid=REPLAY_USERID,
            limit=500,
            snapshot_time=snapshot_time,
        )
        or 0
    )

    return {
        "trades_created": created_count,
        "entry_updates": entry_updates,
        "monitor_updates": monitor_updates,
        "exit_updates": exit_updates,
    }


# =============================================================================
# Driver
# =============================================================================


def run() -> int:
    keys = _fetch_unprocessed_keys()
    if not keys:
        logger.info("No unprocessed snapshots found.")
        return 0

    distinct_symbols = {symbol for symbol, _ in keys}
    logger.info(
        "Starting replay_unprocessed | snapshots=%d symbols=%d earliest=%s "
        "latest=%s generate_trades=%s userid=%s",
        len(keys),
        len(distinct_symbols),
        keys[0][1],
        keys[-1][1],
        GENERATE_TRADES,
        REPLAY_USERID if GENERATE_TRADES else "NOT_USED",
    )

    if GENERATE_TRADES:
        _validate_replay_user(REPLAY_USERID)

    stats: Counter[str] = Counter()
    blocked_symbols: set[str] = set()
    stop_after_group_failure = False

    trade_generator = TradeGenerator() if GENERATE_TRADES else None
    trade_executor = TradeExecutor() if GENERATE_TRADES else None
    trade_monitor = TradeMonitor() if GENERATE_TRADES else None

    @contextmanager
    def processing_context() -> Iterator[Optional[Callable[[datetime], None]]]:
        if not GENERATE_TRADES:
            yield None
            return
        with (
            _restrict_monitor_to_user(REPLAY_USERID),
            _deterministic_replay_clock() as set_replay_time,
            _snapshot_execution_mode(),
        ):
            yield set_replay_time

    with processing_context() as set_replay_time:
        for snapshot_time, grouped_keys in groupby(keys, key=lambda item: item[1]):
            group = list(grouped_keys)
            eligible_group = [
                key for key in group if key[0] not in blocked_symbols
            ]
            if not eligible_group:
                stats["skipped_after_symbol_error"] += len(group)
                continue

            if set_replay_time is not None:
                set_replay_time(snapshot_time)

            successful: List[SnapshotKey] = []
            action_counts: Counter[str] = Counter()

            for symbol, key_time in eligible_group:
                try:
                    snapshot = _load_snapshot(symbol, key_time)
                    action = _run_signal(snapshot)
                    action_counts[action] += 1
                    successful.append((symbol, key_time))
                    stats["signals_processed"] += 1
                except Exception:
                    blocked_symbols.add(symbol)
                    stats["signal_errors"] += 1
                    logger.exception(
                        "Signal generation failed; this snapshot and later "
                        "snapshots for the symbol remain unprocessed | "
                        "symbol=%s snapshot_time=%s",
                        symbol,
                        key_time,
                    )

            if not successful:
                logger.error(
                    "No snapshot succeeded at snapshot_time=%s; continuing with "
                    "other symbols while failed symbols remain blocked.",
                    snapshot_time,
                )
                continue

            trade_result: Dict[str, int] = {}
            if GENERATE_TRADES:
                assert trade_generator is not None
                assert trade_executor is not None
                assert trade_monitor is not None
                try:
                    trade_result = _run_trade_pipeline(
                        snapshot_time=snapshot_time,
                        trade_generator=trade_generator,
                        trade_executor=trade_executor,
                        trade_monitor=trade_monitor,
                    )
                    stats.update(trade_result)
                except Exception:
                    stats["trade_pipeline_errors"] += 1
                    logger.exception(
                        "Trade pipeline failed; successful signal snapshots at "
                        "this timestamp remain unprocessed | snapshot_time=%s",
                        snapshot_time,
                    )
                    # Do not advance the trade clock beyond an unresolved group.
                    stop_after_group_failure = True

            if stop_after_group_failure:
                break

            for symbol, key_time in successful:
                try:
                    _mark_processed(symbol, key_time)
                    stats["snapshots_marked_processed"] += 1
                except Exception:
                    blocked_symbols.add(symbol)
                    stats["mark_processed_errors"] += 1
                    logger.exception(
                        "Completed snapshot could not be acknowledged and will "
                        "be retried | symbol=%s snapshot_time=%s",
                        symbol,
                        key_time,
                    )

            logger.info(
                "REPLAY_GROUP | snapshot_time=%s selected=%d successful=%d "
                "actions=%s trades=%s",
                snapshot_time,
                len(eligible_group),
                len(successful),
                dict(sorted(action_counts.items())),
                trade_result,
            )

    remaining = _remaining_unprocessed_count()
    logger.info(
        "Replay complete | stats=%s blocked_symbols=%s remaining_unprocessed=%d",
        dict(sorted(stats.items())),
        sorted(blocked_symbols),
        remaining,
    )
    return 1 if stats["trade_pipeline_errors"] else 0


def main() -> int:
    setup_logging(log_file=LOG_FILE)
    global logger
    logger = logging.getLogger(__name__)

    logger.info(
        "replay_unprocessed configuration | generate_trades=%s userid=%s "
        "clearing=NEVER snapshot_generation=NEVER",
        GENERATE_TRADES,
        REPLAY_USERID if GENERATE_TRADES else "NOT_USED",
    )

    try:
        return run()
    except KeyboardInterrupt:
        logger.info("Interrupted; completed snapshots remain processed and the rest can be retried.")
        return 130
    except Exception:
        logger.exception("replay_unprocessed failed during startup/preflight")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
