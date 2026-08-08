#!/usr/bin/env python3
"""Production-like historical replay scheduler.

This module is the reusable replay orchestration layer. It deliberately does
not implement alternative signal, trade, execution, or monitoring logic.
Instead it advances a historical logical clock one minute at a time and invokes
existing production services in production order against restored persisted
snapshots.

Default stage order for each logical minute:

    snapshot stage      -> NO-OP (snapshots already restored/generated)
    signal stage        -> process causally due unprocessed snapshots
    trade stage         -> normal TradeGenerator
    executor pre-pass   -> normal TradeExecutor using replay_price_provider
    monitor stage       -> normal TradeMonitor using replay_price_provider
    executor post-pass  -> settle monitor-created exits

The default replay window is 09:18 through 15:15 IST, inclusive. Persisted
three-minute snapshots become signal-eligible only when their candle has
completed. Thus the snapshot labelled 09:15 is first processed at 09:18.

Failure contract:
- startup / unsafe preflight failures terminate the run;
- per-record and per-stage failures are logged with replay time and processing
  continues;
- failed snapshots remain unprocessed;
- later snapshots for the same symbol are not processed ahead of a failed
  earlier snapshot in the same pass.
"""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
import json
import logging
import os
import sys
import time
from typing import Callable, Dict, Iterator, List, Optional, Tuple
from zoneinfo import ZoneInfo

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from configs.execution_config import EXECUTION_CONFIG
from configs.intraday_lifecycle_config import INTRADAY_LIFECYCLE_CONFIG
from configs.monitor_config import MONITOR_CONFIG
from configs.signal_config import SIGNAL_CONFIG
from configs.snapshot_config import SNAPSHOT_CONFIG
from database.database import get_trades_db
from models.trade_models import Snapshot as SnapshotORM
from schemas.snapshot import SnapshotSchema
from schemas.user import UserSchema
from services.signals.signal_generator import SignalGenerator
from services.trade.executor import trade_executor as executor_module
from services.trade.executor.trade_executor import TradeExecutor
from services.trade.generator import tradegen_helper as tradegen_helper_module
from services.trade.generator import tradegen_validator as tradegen_validator_module
from services.trade.generator.trade_generator import TradeGenerator
from services.trade.monitor.trade_monitor import TradeMonitor
from configs.replay_config import REPLAY_CONFIG
from tests.replays.replay_price_provider import get_replay_price, replay_price_provider

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReplayStages:
    """Stage switches kept in one object rather than many CLI flags."""

    snapshots: bool = False
    signals: bool = True
    trade_generator: bool = True
    executor: bool = True
    monitor: bool = True


@dataclass(frozen=True)
class ReplayPipelineConfig:
    trading_day: date
    start_time: dtime = dtime(9, 18)
    end_time: dtime = dtime(15, 15)
    step_minutes: int = 1
    userid: Optional[str] = None
    stages: ReplayStages = field(default_factory=ReplayStages)


@dataclass
class ReplayPipelineResult:
    stats: Counter[str]
    remaining_unprocessed: int

    @property
    def had_errors(self) -> bool:
        error_keys = (
            "snapshot_load_errors",
            "signal_errors",
            "trade_generator_errors",
            "executor_user_fetch_errors",
            "executor_pre_monitor_user_errors",
            "executor_post_monitor_user_errors",
            "monitor_pass_errors",
            "monitor_item_errors",
        )
        return any(self.stats[key] for key in error_keys)


# =============================================================================
# Replay runtime boundaries
# =============================================================================


class _ReplayClock:
    def __init__(self) -> None:
        self._current: Optional[datetime] = None

    def set(self, value: datetime) -> None:
        if not isinstance(value, datetime):
            raise TypeError("Replay clock requires datetime")
        self._current = value

    def now(self) -> datetime:
        current = self._current
        if current is None:
            raise RuntimeError("Replay clock used before logical time was assigned")
        if current.tzinfo is not None:
            return current.astimezone(IST).replace(tzinfo=None)
        return current.replace(tzinfo=None)


@contextmanager
def _deterministic_replay_clock() -> Iterator[_ReplayClock]:
    """Route production trade clocks to the historical logical minute."""
    clock = _ReplayClock()

    original_helper_now = tradegen_helper_module.business_now_naive
    original_validator_now = tradegen_validator_module.business_now_naive
    original_executor_now = executor_module._now_ist_naive

    tradegen_helper_module.business_now_naive = clock.now
    tradegen_validator_module.business_now_naive = clock.now
    executor_module._now_ist_naive = clock.now
    try:
        yield clock
    finally:
        tradegen_helper_module.business_now_naive = original_helper_now
        tradegen_validator_module.business_now_naive = original_validator_now
        executor_module._now_ist_naive = original_executor_now


@contextmanager
def _replay_execution_mode() -> Iterator[None]:
    """Force replay-safe virtual execution; provider owns historical prices."""
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


@contextmanager
def _replay_cutoffs(*, end_time: dtime) -> Iterator[None]:
    """Map the 15:15 wall-clock replay close to the last completed 3m snapshot.

    SignalGenerator evaluates snapshot labels, while TradeGenerator/Monitor use
    wall-clock time. At a 15:15 replay close the final completed 3m snapshot is
    labelled 15:12, so that label is the replay signal cutoff.
    """
    old_signal_snapshot_cutoff = SIGNAL_CONFIG.intraday_cutoff_time
    old_trade_entry_cutoff = INTRADAY_LIFECYCLE_CONFIG.signal_cutoff_time
    old_monitor_cutoff = MONITOR_CONFIG.intraday_cutoff_time

    wall_cutoff = end_time.replace(second=0, microsecond=0)
    wall_cutoff_text = wall_cutoff.isoformat()
    cadence = int(SNAPSHOT_CONFIG.service.tick_minutes)
    cutoff_anchor = datetime.combine(date(2000, 1, 1), wall_cutoff)
    signal_label_cutoff = (cutoff_anchor - timedelta(minutes=cadence)).time()

    SIGNAL_CONFIG.intraday_cutoff_time = signal_label_cutoff.isoformat()
    INTRADAY_LIFECYCLE_CONFIG.signal_cutoff_time = wall_cutoff_text
    MONITOR_CONFIG.intraday_cutoff_time = wall_cutoff_text
    try:
        logger.info(
            "REPLAY_CUTOFFS | wall=%s signal_snapshot_label=%s monitor=%s",
            wall_cutoff_text,
            SIGNAL_CONFIG.intraday_cutoff_time,
            MONITOR_CONFIG.intraday_cutoff_time,
        )
        yield
    finally:
        SIGNAL_CONFIG.intraday_cutoff_time = old_signal_snapshot_cutoff
        INTRADAY_LIFECYCLE_CONFIG.signal_cutoff_time = old_trade_entry_cutoff
        MONITOR_CONFIG.intraday_cutoff_time = old_monitor_cutoff


def _completed_snapshot_asof(replay_time: datetime) -> datetime:
    cadence = int(SNAPSHOT_CONFIG.service.tick_minutes)
    if cadence < 1:
        raise RuntimeError("Snapshot service tick_minutes must be >= 1")
    return replay_time - timedelta(minutes=cadence)


@contextmanager
def _replay_executor_snapshot_visibility(clock: _ReplayClock) -> Iterator[None]:
    """Keep executor snapshot guards on completed 3m data only.

    Restored replay databases already contain future snapshot rows. Production
    would not have those rows yet. The executor's snapshot-based entry guard is
    therefore capped to the latest snapshot that could actually be completed at
    the logical replay minute, while execution price still comes from the 1m
    replay price provider at the current logical minute.
    """
    original_latest = executor_module._latest_snapshot_record

    def causal_latest(symbol: str, *, asof_time: datetime):
        del asof_time
        return original_latest(symbol, asof_time=_completed_snapshot_asof(clock.now()))

    executor_module._latest_snapshot_record = causal_latest
    try:
        yield
    finally:
        executor_module._latest_snapshot_record = original_latest


@contextmanager
def _replay_monitor_runtime(clock: _ReplayClock) -> Iterator[None]:
    """Give TradeMonitor production context plus minute-level replay price/time.

    The latest completed 3m snapshot remains the authoritative structural
    context. For ``1m_candle`` pricing only, the monitored instrument price is
    read at the current one-minute replay clock. Trade-management age/cutoff
    calculations also use that logical minute. No production TradeMonitor code
    is changed.
    """
    original_fetch = TradeMonitor._fetch_snapshot
    original_build = TradeMonitor._build_context
    original_price = TradeMonitor._price_from_snapshot_for_trade

    def causal_fetch(self, ut, asof_time=None):
        del asof_time
        return original_fetch(
            self,
            ut,
            asof_time=_completed_snapshot_asof(clock.now()),
        )

    def logical_build(self, *args, **kwargs):
        kwargs["last_time"] = clock.now()
        return original_build(self, *args, **kwargs)

    source = str(REPLAY_CONFIG.execution_price_source or "").strip().lower()

    def logical_1m_price(self, ut, snapshot):
        del self, snapshot
        return get_replay_price(getattr(ut, "symbol", None), clock.now())

    TradeMonitor._fetch_snapshot = causal_fetch
    TradeMonitor._build_context = logical_build
    if source == "1m_candle":
        TradeMonitor._price_from_snapshot_for_trade = logical_1m_price

    try:
        yield
    finally:
        TradeMonitor._fetch_snapshot = original_fetch
        TradeMonitor._build_context = original_build
        TradeMonitor._price_from_snapshot_for_trade = original_price


# =============================================================================
# Snapshot availability / signal stage
# =============================================================================


def resolve_trading_day(explicit_day: Optional[date]) -> date:
    if explicit_day is not None:
        return explicit_day

    with get_trades_db() as db:
        rows = db.query(SnapshotORM.snapshot_time).order_by(SnapshotORM.snapshot_time.asc()).all()

    days = sorted({row[0].date() for row in rows if row and isinstance(row[0], datetime)})
    if not days:
        raise RuntimeError("Replay preflight found no persisted snapshots")
    if len(days) != 1:
        raise RuntimeError(
            "Replay preflight found snapshots from multiple trading days; "
            "specify the replay day explicitly: " + ", ".join(day.isoformat() for day in days)
        )
    return days[0]


def _load_due_unprocessed(*, trading_day: date, replay_time: datetime) -> Tuple[List[SnapshotSchema], int]:
    cadence = int(SNAPSHOT_CONFIG.service.tick_minutes)
    if cadence < 1:
        raise RuntimeError("Snapshot service tick_minutes must be >= 1")

    available_label = replay_time - timedelta(minutes=cadence)
    day_start = datetime.combine(trading_day, dtime.min)
    day_end = day_start + timedelta(days=1)

    with get_trades_db() as db:
        rows = (
            db.query(SnapshotORM)
            .filter(SnapshotORM.processed == False)  # noqa: E712
            .filter(SnapshotORM.snapshot_time >= day_start)
            .filter(SnapshotORM.snapshot_time < day_end)
            .filter(SnapshotORM.snapshot_time <= available_label)
            .order_by(SnapshotORM.snapshot_time.asc(), SnapshotORM.symbol.asc())
            .all()
        )

    snapshots: List[SnapshotSchema] = []
    invalid = 0
    for row in rows:
        symbol = str(getattr(row, "symbol", "") or "").strip().upper()
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
                raise TypeError("snapshot JSON snapshot_time must be datetime")
            if payload_time.replace(tzinfo=None) != row.snapshot_time.replace(tzinfo=None):
                raise ValueError("snapshot JSON time differs from DB snapshot_time")

            payload["ltp"] = float(row.ltp) if row.ltp is not None else None
            payload["ltp_time"] = row.ltp_time
            snapshots.append(SnapshotSchema.from_db_dict(payload))
        except Exception:
            invalid += 1
            logger.exception(
                "REPLAY_SNAPSHOT_LOAD_ERROR | replay_time=%s symbol=%s snapshot_time=%s; "
                "row remains unprocessed",
                replay_time,
                symbol,
                getattr(row, "snapshot_time", None),
            )

    return snapshots, invalid


def _signal_tick(*, trading_day: date, replay_time: datetime, stats: Counter[str]) -> None:
    snapshots, invalid = _load_due_unprocessed(trading_day=trading_day, replay_time=replay_time)
    stats["snapshot_load_errors"] += invalid
    if not snapshots:
        return

    blocked_symbols: set[str] = set()
    logger.info("SIGNAL_TICK | replay_time=%s due=%d", replay_time, len(snapshots))

    for snapshot in snapshots:
        symbol = str(snapshot.symbol or "").strip().upper()
        if symbol in blocked_symbols:
            stats["signal_deferred_after_symbol_failure"] += 1
            logger.warning(
                "SIGNAL_DEFER_AFTER_FAILURE | replay_time=%s symbol=%s snapshot_time=%s",
                replay_time,
                symbol,
                snapshot.snapshot_time,
            )
            continue

        try:
            action = str(SignalGenerator(snapshot).generate() or "NO_ACTION")
            if not SnapshotSchema.mark_processed(snapshot.symbol, snapshot.snapshot_time):
                raise RuntimeError("snapshot could not be marked processed")
            stats["snapshots_processed"] += 1
            stats[f"signal_action:{action}"] += 1
        except Exception:
            blocked_symbols.add(symbol)
            stats["signal_errors"] += 1
            logger.exception(
                "SIGNAL_ERROR | replay_time=%s symbol=%s snapshot_time=%s; "
                "row remains unprocessed and later snapshots for this symbol are deferred",
                replay_time,
                symbol,
                snapshot.snapshot_time,
            )


# =============================================================================
# Production stage adapters
# =============================================================================


def _trade_tick(*, replay_time: datetime, trade_generator: TradeGenerator, userid: Optional[str], stats: Counter[str]) -> None:
    try:
        created = (
            trade_generator.generate_user_trades(userid)
            if userid
            else trade_generator.generate_user_trades()
        ) or []
        count = len(created) if isinstance(created, list) else int(created or 0)
        stats["trades_created"] += count
        if count:
            logger.info("TRADE_TICK | replay_time=%s created=%d", replay_time, count)
    except Exception:
        stats["trade_generator_errors"] += 1
        logger.exception("TRADE_TICK_ERROR | replay_time=%s; continuing", replay_time)


def _eligible_execution_userids(userid: Optional[str]) -> List[str]:
    users = UserSchema.fetch_tradeable_users() or []
    userids = [
        str(getattr(user, "userid", "") or "").strip()
        for user in users
        if int(getattr(user, "active", 0) or 0) == 1
        and int(getattr(user, "logged_in", 0) or 0) == 1
    ]
    userids = [value for value in userids if value]
    if userid:
        userids = [value for value in userids if value == userid]
    return userids


def _execute_one_user(userid: str, replay_time: datetime) -> Tuple[str, int]:
    executor = TradeExecutor()
    acted = executor.execute_user_once(
        userid=userid,
        limit=int(EXECUTION_CONFIG.limit),
        snapshot_time=replay_time,
    )
    return userid, int(acted or 0)


def _executor_tick(*, replay_time: datetime, phase: str, userid: Optional[str], stats: Counter[str]) -> None:
    try:
        userids = _eligible_execution_userids(userid)
    except Exception:
        stats["executor_user_fetch_errors"] += 1
        logger.exception("EXECUTOR_%s_USER_FETCH_ERROR | replay_time=%s", phase, replay_time)
        return

    if not userids:
        return

    max_workers = min(int(EXECUTION_CONFIG.max_workers), max(1, len(userids)))
    acted_total = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_execute_one_user, value, replay_time): value for value in userids}
        for future in as_completed(futures):
            current_userid = futures[future]
            try:
                _, acted = future.result()
                acted_total += acted
            except Exception:
                failed += 1
                logger.exception(
                    "EXECUTOR_%s_USER_ERROR | replay_time=%s userid=%s",
                    phase,
                    replay_time,
                    current_userid,
                )

    stats[f"executor_{phase.lower()}_updates"] += acted_total
    stats[f"executor_{phase.lower()}_user_errors"] += failed
    if acted_total or failed:
        logger.info(
            "EXECUTOR_%s | replay_time=%s users=%d acted=%d failed=%d",
            phase,
            replay_time,
            len(userids),
            acted_total,
            failed,
        )


def _monitor_tick(*, replay_time: datetime, trade_monitor: TradeMonitor, stats: Counter[str]) -> None:
    try:
        updated = int(trade_monitor.monitor(snapshot_time=replay_time) or 0)
        errors = list(trade_monitor.last_pass_errors)
        stats["monitor_updates"] += updated
        stats["monitor_item_errors"] += len(errors)
        for error in errors:
            logger.error("MONITOR_ITEM_ERROR | replay_time=%s error=%s", replay_time, error)
        if updated or errors:
            logger.info(
                "MONITOR_TICK | replay_time=%s updated=%d item_errors=%d",
                replay_time,
                updated,
                len(errors),
            )
    except Exception:
        stats["monitor_pass_errors"] += 1
        logger.exception("MONITOR_TICK_ERROR | replay_time=%s; continuing", replay_time)


def _remaining_due_unprocessed(trading_day: date, end_time: dtime) -> int:
    day_start = datetime.combine(trading_day, dtime.min)
    replay_end = datetime.combine(trading_day, end_time)
    final_available_label = _completed_snapshot_asof(replay_end)
    with get_trades_db() as db:
        return int(
            db.query(SnapshotORM)
            .filter(SnapshotORM.processed == False)  # noqa: E712
            .filter(SnapshotORM.snapshot_time >= day_start)
            .filter(SnapshotORM.snapshot_time <= final_available_label)
            .count()
        )


# =============================================================================
# Driver
# =============================================================================


def run_replay_pipeline(config: ReplayPipelineConfig) -> ReplayPipelineResult:
    if int(config.step_minutes) != 1:
        raise ValueError("Replay pipeline must run at one-minute logical cadence")
    if config.end_time < config.start_time:
        raise ValueError("Replay end must not be earlier than replay start")

    start = datetime.combine(config.trading_day, config.start_time)
    end = datetime.combine(config.trading_day, config.end_time)
    stats: Counter[str] = Counter()

    logger.info(
        "REPLAY_PIPELINE_START | day=%s window=%s..%s step=1m userid=%s stages=%s",
        config.trading_day,
        start,
        end,
        config.userid or "PRODUCTION_ELIGIBLE_USERS",
        config.stages,
    )

    with (
        _replay_execution_mode(),
        _replay_cutoffs(end_time=config.end_time),
        _deterministic_replay_clock() as clock,
        replay_price_provider(),
        _replay_executor_snapshot_visibility(clock),
        _replay_monitor_runtime(clock),
    ):
        trade_generator = TradeGenerator() if config.stages.trade_generator else None
        # TradeMonitor caches intraday cutoff in __init__; instantiate only after
        # replay cutoff overrides are active.
        trade_monitor = TradeMonitor() if config.stages.monitor else None

        current = start
        while current <= end:
            clock.set(current)
            stats["logical_minutes"] += 1

            # Snapshot stage is deliberately retained in pipeline order but is a
            # no-op for restored historical snapshots.
            if config.stages.snapshots:
                stats["snapshot_noop_ticks"] += 1
                logger.debug("SNAPSHOT_TICK_NOOP | replay_time=%s", current)

            if config.stages.signals:
                _signal_tick(
                    trading_day=config.trading_day,
                    replay_time=current,
                    stats=stats,
                )

            if config.stages.trade_generator:
                assert trade_generator is not None
                _trade_tick(
                    replay_time=current,
                    trade_generator=trade_generator,
                    userid=config.userid,
                    stats=stats,
                )

            # Production executor runs independently and more frequently than
            # TradeMonitor. With one historical price observation per minute,
            # one pass before monitor and one after monitor preserves the event
            # ordering without inventing sub-minute prices.
            if config.stages.executor:
                _executor_tick(
                    replay_time=current,
                    phase="PRE_MONITOR",
                    userid=config.userid,
                    stats=stats,
                )

            if config.stages.monitor:
                assert trade_monitor is not None
                _monitor_tick(
                    replay_time=current,
                    trade_monitor=trade_monitor,
                    stats=stats,
                )

            if config.stages.executor:
                _executor_tick(
                    replay_time=current,
                    phase="POST_MONITOR",
                    userid=config.userid,
                    stats=stats,
                )

            current += timedelta(minutes=1)

    remaining = _remaining_due_unprocessed(config.trading_day, config.end_time)
    logger.info(
        "REPLAY_PIPELINE_DONE | day=%s remaining_due_unprocessed=%d stats=%s",
        config.trading_day,
        remaining,
        dict(sorted(stats.items())),
    )
    return ReplayPipelineResult(stats=stats, remaining_unprocessed=remaining)
