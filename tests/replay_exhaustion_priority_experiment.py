#!/usr/bin/env python3
"""Replay-only experiment for earlier Exhaustion Reversal priority.

The program reads persisted snapshots and current-day signals/trades, then
reports how a simpler policy would have behaved:

1. Abstain while price is inside a confirmed unresolved accepted range.
2. When Auction already reports a mature exhaustion episode, freeze its
   initiation economics and wait for the first deterministic counter-side
   price-action confirmation.
3. While that exhaustion thesis remains unresolved, flag same-direction trend
   signals as chase candidates.
4. Unlock same-direction continuation only after the counter-trend reversal is
   objectively invalidated by accepted closes beyond the frozen extreme.

This program is REPORT ONLY.  It does not create, update, close, or delete any
snapshot, opportunity, signal, trade, or audit record.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import csv
import json
import logging
import os
from pathlib import Path
import sys
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from configs.auction_experiment_config import (
    AUCTION_POLICY_EXPERIMENT_CONFIG,
    AuctionPolicyExperimentConfig,
)
from database.database import get_trades_db
from logconfig import setup_logging
from models.trade_models import Signal as SignalORM
from models.trade_models import UserTrade as UserTradeORM
from schemas.snapshot import SnapshotSchema
from tests.experiment_exhaustion_priority import (
    ExhaustionEpisode,
    confirmation,
    range_locked as is_range_locked,
    room_from_initiation,
    update_failure,
)
from utils.json_utils import sanitize_json


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BaselineSignal:
    signal_id: str
    symbol: str
    side: str
    setup: str
    signal_time: datetime
    created_price: Optional[float]
    signal_last_pnl_pct: float
    signal_last_pnl_value: float
    package_pnl: Optional[float]


@dataclass(frozen=True)
class SnapshotPolicyState:
    symbol: str
    snapshot_time: datetime
    close: float
    auction_state: str
    gap_pct: float
    large_gap: bool
    range_locked: bool
    accepted_range_id: Optional[str]
    exhaustion_active: bool
    exhaustion_thesis_unresolved: bool
    exhausted_side: str
    exhaustion_started_at: Optional[datetime]
    active_episode_id: Optional[str]
    proposed_reversal_confirmed: bool
    reversal_failed: bool
    continuation_unlocked: bool


@dataclass(frozen=True)
class ExperimentError:
    symbol: str
    snapshot_time: Optional[datetime]
    stage: str
    error_type: str
    message: str


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat(sep=" ") if value is not None else None


def _write_csv(path: Path, rows: Iterable[Dict[str, object]]) -> None:
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
    config: AuctionPolicyExperimentConfig,
) -> List[SnapshotSchema]:
    output: List[SnapshotSchema] = []
    after_time: Optional[datetime] = None
    after_symbol = ""
    symbols = list(config.run.symbols) if config.run.symbols else None

    while True:
        batch = SnapshotSchema.fetch_day_replay_batch(
            trading_day=config.run.trading_day,
            after_time=after_time,
            after_symbol=after_symbol,
            symbols=symbols,
            limit=config.run.batch_size,
        )
        if not batch:
            break
        output.extend(batch)
        last = batch[-1]
        after_time = _naive(last.snapshot_time)
        after_symbol = last.symbol
        if len(batch) < config.run.batch_size:
            break

    if not output:
        raise ValueError(
            f"No snapshots found for {config.run.trading_day.isoformat()}"
        )
    return output


def _load_baseline_signals(
    config: AuctionPolicyExperimentConfig,
) -> List[BaselineSignal]:
    day_start = datetime.combine(config.run.trading_day, time.min)
    day_end = day_start + timedelta(days=1)
    symbols = tuple(config.run.symbols)

    with get_trades_db() as db:
        signal_query = (
            db.query(SignalORM)
            .filter(SignalORM.first_seen_time >= day_start)
            .filter(SignalORM.first_seen_time < day_end)
        )
        if symbols:
            signal_query = signal_query.filter(SignalORM.symbol.in_(symbols))
        signal_rows = signal_query.order_by(
            SignalORM.first_seen_time.asc(),
            SignalORM.symbol.asc(),
            SignalORM.signal_id.asc(),
        ).all()

        trade_query = (
            db.query(UserTradeORM)
            .filter(UserTradeORM.userid == config.run.replay_userid)
            .filter(UserTradeORM.entry_time >= day_start)
            .filter(UserTradeORM.entry_time < day_end)
        )
        if symbols:
            trade_query = trade_query.filter(UserTradeORM.equity_ref.in_(symbols))
        trade_rows = trade_query.order_by(
            UserTradeORM.signal_id.asc(),
            UserTradeORM.id.asc(),
        ).all()

    pnl_by_signal: Dict[str, float] = defaultdict(float)
    incomplete_trade_pnl: Dict[str, bool] = defaultdict(bool)
    for trade in trade_rows:
        signal_id = str(trade.signal_id).strip()
        if not signal_id:
            raise ValueError("user_trades.signal_id cannot be empty")
        if trade.exit_pnl is None:
            incomplete_trade_pnl[signal_id] = True
        else:
            pnl_by_signal[signal_id] += float(trade.exit_pnl)

    output: List[BaselineSignal] = []
    for signal in signal_rows:
        if signal.first_seen_time is None:
            raise ValueError(
                f"Signal {signal.signal_id} is missing first_seen_time"
            )
        signal_id = str(signal.signal_id).strip()
        if not signal_id:
            raise ValueError("signals.signal_id cannot be empty")
        package_pnl: Optional[float]
        if incomplete_trade_pnl[signal_id]:
            package_pnl = None
        elif signal_id in pnl_by_signal:
            package_pnl = pnl_by_signal[signal_id]
        else:
            package_pnl = None
        output.append(
            BaselineSignal(
                signal_id=signal_id,
                symbol=str(signal.symbol).strip().upper(),
                side=str(signal.side).strip().upper(),
                setup=str(signal.setup).strip().upper(),
                signal_time=_naive(signal.first_seen_time),
                created_price=(
                    float(signal.created_price)
                    if signal.created_price is not None
                    else None
                ),
                signal_last_pnl_pct=float(signal.last_pnl),
                signal_last_pnl_value=float(signal.last_pnl_value),
                package_pnl=package_pnl,
            )
        )
    return output


def _gap_pct(snapshot: SnapshotSchema) -> float:
    today_open = snapshot.levels.today.open
    previous_close = snapshot.levels.prev_day.close
    if today_open is None or previous_close is None:
        raise ValueError(
            f"{snapshot.symbol} snapshot is missing today.open or prev_day.close"
        )
    if today_open <= 0 or previous_close <= 0:
        raise ValueError(
            f"{snapshot.symbol} today.open and prev_day.close must be positive"
        )
    return ((float(today_open) - float(previous_close)) / float(previous_close)) * 100.0


def _new_episode(
    snapshot: SnapshotSchema,
    config: AuctionPolicyExperimentConfig,
    gap_pct: float,
) -> ExhaustionEpisode:
    context = snapshot.auction.stock_context
    if context is None:
        raise ValueError("auction.stock_context is required")
    if not context.exhaustion_active:
        raise ValueError("Cannot create an episode while exhaustion_active=False")
    if context.exhaustion_started_at is None:
        raise ValueError("exhaustion_started_at is required when exhaustion is active")
    exhausted_side = str(context.exhausted_side).strip().upper()
    if exhausted_side == "UP":
        reversal_side = "SELL"
        extreme = float(snapshot.bar.high)
    elif exhausted_side == "DOWN":
        reversal_side = "BUY"
        extreme = float(snapshot.bar.low)
    else:
        raise ValueError(f"Unsupported exhausted side: {exhausted_side}")
    vwap = snapshot.indicators.vwap.value
    if vwap is None or vwap <= 0:
        raise ValueError("indicators.vwap.value is required and must be positive")
    atr = float(snapshot.indicators.atr.value)
    room_points, room_atr, room_pct = room_from_initiation(
        exhausted_side=exhausted_side,
        extreme=extreme,
        vwap=float(vwap),
        atr=atr,
    )
    started_at = _naive(context.exhaustion_started_at)
    episode_id = (
        f"EXPERIMENT:{snapshot.symbol}:{started_at.isoformat()}:{exhausted_side}"
    )
    large_gap = abs(gap_pct) >= config.gap.large_gap_threshold_pct
    qualified = (
        room_atr >= config.exhaustion.minimum_initial_vwap_room_atr
        and room_pct >= config.exhaustion.minimum_initial_vwap_room_pct
    )
    return ExhaustionEpisode(
        episode_id=episode_id,
        symbol=snapshot.symbol,
        exhausted_side=exhausted_side,
        reversal_side=reversal_side,
        initiation_time=started_at,
        initiation_close=float(snapshot.close),
        initiation_atr=atr,
        initiation_vwap=float(vwap),
        initial_extreme=extreme,
        extreme_price=extreme,
        extreme_time=_naive(snapshot.snapshot_time),
        gap_pct=gap_pct,
        large_gap=large_gap,
        initial_vwap_room_points=room_points,
        initial_vwap_room_atr=room_atr,
        initial_vwap_room_pct=room_pct,
        initial_room_qualified=qualified,
        expires_at=started_at
        + timedelta(minutes=config.exhaustion.episode_expiry_minutes),
    )


def _update_extreme(episode: ExhaustionEpisode, snapshot: SnapshotSchema) -> None:
    if episode.confirmation_time is not None:
        return
    if episode.exhausted_side == "UP" and snapshot.bar.high > episode.extreme_price:
        episode.extreme_price = float(snapshot.bar.high)
        episode.extreme_time = _naive(snapshot.snapshot_time)
    elif (
        episode.exhausted_side == "DOWN"
        and snapshot.bar.low < episode.extreme_price
    ):
        episode.extreme_price = float(snapshot.bar.low)
        episode.extreme_time = _naive(snapshot.snapshot_time)


def _process_snapshots(
    snapshots: Sequence[SnapshotSchema],
    config: AuctionPolicyExperimentConfig,
) -> Tuple[
    List[ExhaustionEpisode],
    List[SnapshotPolicyState],
    List[ExperimentError],
    Dict[str, List[SnapshotSchema]],
]:
    by_symbol: Dict[str, List[SnapshotSchema]] = defaultdict(list)
    for snapshot in snapshots:
        by_symbol[snapshot.symbol].append(snapshot)
    for symbol in by_symbol:
        by_symbol[symbol].sort(key=lambda item: _naive(item.snapshot_time))

    episodes: List[ExhaustionEpisode] = []
    timeline: List[SnapshotPolicyState] = []
    errors: List[ExperimentError] = []

    for symbol, rows in sorted(by_symbol.items()):
        active_episode: Optional[ExhaustionEpisode] = None
        previous: Optional[SnapshotSchema] = None
        try:
            symbol_gap_pct = _gap_pct(rows[0])
        except Exception as exc:
            errors.append(
                ExperimentError(
                    symbol=symbol,
                    snapshot_time=_naive(rows[0].snapshot_time),
                    stage="GAP_CLASSIFICATION",
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
            )
            continue

        for snapshot in rows:
            snapshot_time = _naive(snapshot.snapshot_time)
            try:
                context = snapshot.auction.stock_context
                if context is None:
                    raise ValueError("auction.stock_context is required")

                if config.exhaustion.enabled and context.exhaustion_active:
                    started_at = context.exhaustion_started_at
                    if started_at is None:
                        raise ValueError(
                            "exhaustion_started_at is required when exhaustion is active"
                        )
                    started_naive = _naive(started_at)
                    exhausted_side = str(context.exhausted_side).strip().upper()
                    previous_episode_resolved = bool(
                        active_episode is not None
                        and (
                            active_episode.reversal_failed_time is not None
                            or active_episode.completed_time is not None
                            or snapshot_time > active_episode.expires_at
                        )
                    )
                    new_identity = (
                        active_episode is None
                        or active_episode.exhausted_side != exhausted_side
                        or (
                            previous_episode_resolved
                            and active_episode.initiation_time != started_naive
                        )
                    )
                    if new_identity:
                        if active_episode is not None and active_episode.completed_time is None:
                            active_episode.completed_time = snapshot_time
                        active_episode = _new_episode(
                            snapshot,
                            config,
                            symbol_gap_pct,
                        )
                        episodes.append(active_episode)
                    else:
                        _update_extreme(active_episode, snapshot)

                    if (
                        previous is not None
                        and active_episode.confirmation_time is None
                        and (
                            not config.exhaustion.require_initial_vwap_room_for_confirmation
                            or active_episode.initial_room_qualified
                        )
                        and snapshot_time <= active_episode.expires_at
                    ):
                        confirmed, reason, displacement_atr = confirmation(
                            episode=active_episode,
                            previous=previous,
                            current=snapshot,
                            config=config,
                        )
                        if confirmed:
                            active_episode.confirmation_time = snapshot_time
                            active_episode.confirmation_price = float(snapshot.close)
                            active_episode.confirmation_reason = reason
                            active_episode.confirmation_displacement_atr = displacement_atr

                if active_episode is not None:
                    update_failure(active_episode, snapshot, config)
                    if (
                        snapshot_time > active_episode.expires_at
                        and active_episode.completed_time is None
                    ):
                        active_episode.completed_time = snapshot_time

                range_locked = is_range_locked(snapshot, config)
                continuation_unlocked = bool(
                    active_episode is not None
                    and active_episode.continuation_unlocked_time is not None
                    and active_episode.continuation_unlocked_time <= snapshot_time
                )
                timeline.append(
                    SnapshotPolicyState(
                        symbol=symbol,
                        snapshot_time=snapshot_time,
                        close=float(snapshot.close),
                        auction_state=(
                            snapshot.auction.state.current
                            if snapshot.auction.state is not None
                            else "UNKNOWN"
                        ),
                        gap_pct=symbol_gap_pct,
                        large_gap=(
                            abs(symbol_gap_pct)
                            >= config.gap.large_gap_threshold_pct
                        ),
                        range_locked=range_locked,
                        accepted_range_id=context.accepted_range_id,
                        exhaustion_active=context.exhaustion_active,
                        exhaustion_thesis_unresolved=bool(
                            active_episode is not None
                            and active_episode.reversal_failed_time is None
                            and snapshot_time <= active_episode.expires_at
                        ),
                        exhausted_side=(
                            active_episode.exhausted_side
                            if active_episode is not None
                            else str(context.exhausted_side).strip().upper()
                        ),
                        exhaustion_started_at=(
                            _naive(context.exhaustion_started_at)
                            if context.exhaustion_started_at is not None
                            else None
                        ),
                        active_episode_id=(
                            active_episode.episode_id
                            if active_episode is not None
                            else None
                        ),
                        proposed_reversal_confirmed=bool(
                            active_episode is not None
                            and active_episode.confirmation_time is not None
                            and active_episode.confirmation_time <= snapshot_time
                        ),
                        reversal_failed=bool(
                            active_episode is not None
                            and active_episode.reversal_failed_time is not None
                            and active_episode.reversal_failed_time <= snapshot_time
                        ),
                        continuation_unlocked=continuation_unlocked,
                    )
                )
            except Exception as exc:
                errors.append(
                    ExperimentError(
                        symbol=symbol,
                        snapshot_time=snapshot_time,
                        stage="SNAPSHOT_POLICY_EVALUATION",
                        error_type=type(exc).__name__,
                        message=str(exc),
                    )
                )
            previous = snapshot

    return episodes, timeline, errors, by_symbol


def _state_at_or_before(
    states: Sequence[SnapshotPolicyState],
    signal_time: datetime,
) -> Optional[SnapshotPolicyState]:
    selected: Optional[SnapshotPolicyState] = None
    for state in states:
        if state.snapshot_time > signal_time:
            break
        selected = state
    return selected


def _same_direction_chase(
    signal: BaselineSignal,
    state: SnapshotPolicyState,
) -> bool:
    if not state.exhaustion_thesis_unresolved or state.continuation_unlocked:
        return False
    if state.exhausted_side == "UP":
        return signal.side == "BUY"
    if state.exhausted_side == "DOWN":
        return signal.side == "SELL"
    return False


def _classify_baseline_signals(
    signals: Sequence[BaselineSignal],
    timeline: Sequence[SnapshotPolicyState],
) -> List[Dict[str, object]]:
    states_by_symbol: Dict[str, List[SnapshotPolicyState]] = defaultdict(list)
    for state in timeline:
        states_by_symbol[state.symbol].append(state)

    rows: List[Dict[str, object]] = []
    for signal in signals:
        states = states_by_symbol[signal.symbol]
        state = _state_at_or_before(states, signal.signal_time)
        if state is None:
            rows.append(
                {
                    "signal_id": signal.signal_id,
                    "symbol": signal.symbol,
                    "side": signal.side,
                    "setup": signal.setup,
                    "signal_time": _iso(signal.signal_time),
                    "classification": "NO_SNAPSHOT_STATE_AT_SIGNAL_TIME",
                    "range_locked": None,
                    "same_direction_chase": None,
                    "would_block": None,
                    "package_pnl": signal.package_pnl,
                    "signal_last_pnl_pct": signal.signal_last_pnl_pct,
                    "signal_last_pnl_value": signal.signal_last_pnl_value,
                }
            )
            continue
        range_block = state.range_locked
        chase_block = _same_direction_chase(signal, state)
        if range_block:
            classification = "BLOCK_RANGE_LOCKED"
        elif chase_block:
            classification = "BLOCK_UNRESOLVED_EXHAUSTION_CHASE"
        else:
            classification = "UNCHANGED_BY_EXPERIMENT_POLICY"
        rows.append(
            {
                "signal_id": signal.signal_id,
                "symbol": signal.symbol,
                "side": signal.side,
                "setup": signal.setup,
                "signal_time": _iso(signal.signal_time),
                "created_price": signal.created_price,
                "gap_pct": state.gap_pct,
                "large_gap": state.large_gap,
                "auction_state": state.auction_state,
                "range_locked": state.range_locked,
                "accepted_range_id": state.accepted_range_id,
                "exhaustion_active": state.exhaustion_active,
                "exhaustion_thesis_unresolved": state.exhaustion_thesis_unresolved,
                "exhausted_side": state.exhausted_side,
                "active_episode_id": state.active_episode_id,
                "proposed_reversal_confirmed": state.proposed_reversal_confirmed,
                "reversal_failed": state.reversal_failed,
                "continuation_unlocked": state.continuation_unlocked,
                "same_direction_chase": chase_block,
                "would_block": range_block or chase_block,
                "classification": classification,
                "package_pnl": signal.package_pnl,
                "signal_last_pnl_pct": signal.signal_last_pnl_pct,
                "signal_last_pnl_value": signal.signal_last_pnl_value,
            }
        )
    return rows


def _attach_baseline_comparison(
    episodes: Sequence[ExhaustionEpisode],
    signals: Sequence[BaselineSignal],
) -> None:
    signals_by_symbol: Dict[str, List[BaselineSignal]] = defaultdict(list)
    for signal in signals:
        signals_by_symbol[signal.symbol].append(signal)
    for symbol in signals_by_symbol:
        signals_by_symbol[symbol].sort(key=lambda item: item.signal_time)

    for episode in episodes:
        if episode.confirmation_time is None:
            continue
        for signal in signals_by_symbol[episode.symbol]:
            if signal.side != episode.reversal_side:
                continue
            if signal.signal_time < episode.confirmation_time:
                continue
            episode.baseline_first_same_side_signal_time = signal.signal_time
            episode.baseline_first_same_side_signal_setup = signal.setup
            episode.baseline_first_same_side_signal_price = signal.created_price
            episode.baseline_delay_minutes = (
                signal.signal_time - episode.confirmation_time
            ).total_seconds() / 60.0
            break


def _attach_outcomes(
    episodes: Sequence[ExhaustionEpisode],
    snapshots_by_symbol: Dict[str, List[SnapshotSchema]],
    config: AuctionPolicyExperimentConfig,
) -> None:
    for episode in episodes:
        if episode.confirmation_time is None or episode.confirmation_price is None:
            continue
        rows = snapshots_by_symbol[episode.symbol]
        future = [
            row
            for row in rows
            if _naive(row.snapshot_time) >= episode.confirmation_time
        ]
        if not future:
            continue
        entry = float(episode.confirmation_price)
        if episode.reversal_side == "SELL":
            favorable = entry - min(float(row.bar.low) for row in future)
            adverse = max(float(row.bar.high) for row in future) - entry
        elif episode.reversal_side == "BUY":
            favorable = max(float(row.bar.high) for row in future) - entry
            adverse = entry - min(float(row.bar.low) for row in future)
        else:
            raise ValueError(f"Unsupported reversal side: {episode.reversal_side}")
        episode.mfe_points = max(0.0, favorable)
        episode.mae_points = max(0.0, adverse)
        episode.mfe_atr = episode.mfe_points / episode.initiation_atr
        episode.mae_atr = episode.mae_points / episode.initiation_atr
        episode.mfe_pct = episode.mfe_points / entry
        episode.mae_pct = episode.mae_points / entry

        for horizon in config.outcomes.horizon_bars:
            key = f"move_{horizon}_bars_pct"
            if len(future) <= horizon:
                episode.horizon_outcomes[key] = None
                continue
            exit_close = float(future[horizon].close)
            if episode.reversal_side == "SELL":
                move_pct = (entry - exit_close) / entry
            else:
                move_pct = (exit_close - entry) / entry
            episode.horizon_outcomes[key] = move_pct


def _episode_rows(episodes: Sequence[ExhaustionEpisode]) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for episode in episodes:
        row: Dict[str, object] = {
            "episode_id": episode.episode_id,
            "symbol": episode.symbol,
            "exhausted_side": episode.exhausted_side,
            "reversal_side": episode.reversal_side,
            "gap_pct": episode.gap_pct,
            "large_gap": episode.large_gap,
            "initiation_time": _iso(episode.initiation_time),
            "initiation_close": episode.initiation_close,
            "initiation_atr": episode.initiation_atr,
            "initiation_vwap": episode.initiation_vwap,
            "initial_extreme": episode.initial_extreme,
            "final_pre_confirmation_extreme": episode.extreme_price,
            "extreme_time": _iso(episode.extreme_time),
            "initial_vwap_room_points": episode.initial_vwap_room_points,
            "initial_vwap_room_atr": episode.initial_vwap_room_atr,
            "initial_vwap_room_pct": episode.initial_vwap_room_pct,
            "initial_room_qualified": episode.initial_room_qualified,
            "confirmation_time": _iso(episode.confirmation_time),
            "confirmation_price": episode.confirmation_price,
            "confirmation_reason": episode.confirmation_reason,
            "confirmation_displacement_atr": episode.confirmation_displacement_atr,
            "reversal_failed_time": _iso(episode.reversal_failed_time),
            "continuation_unlocked_time": _iso(episode.continuation_unlocked_time),
            "baseline_first_same_side_signal_time": _iso(
                episode.baseline_first_same_side_signal_time
            ),
            "baseline_first_same_side_signal_setup": (
                episode.baseline_first_same_side_signal_setup
            ),
            "baseline_first_same_side_signal_price": (
                episode.baseline_first_same_side_signal_price
            ),
            "baseline_delay_minutes": episode.baseline_delay_minutes,
            "mfe_points": episode.mfe_points,
            "mae_points": episode.mae_points,
            "mfe_atr": episode.mfe_atr,
            "mae_atr": episode.mae_atr,
            "mfe_pct": episode.mfe_pct,
            "mae_pct": episode.mae_pct,
            "expires_at": _iso(episode.expires_at),
            "completed_time": _iso(episode.completed_time),
        }
        for key, value in episode.horizon_outcomes.items():
            row[key] = value
        rows.append(row)
    return rows


def _timeline_rows(
    timeline: Sequence[SnapshotPolicyState],
) -> List[Dict[str, object]]:
    return [
        {
            "symbol": state.symbol,
            "snapshot_time": _iso(state.snapshot_time),
            "close": state.close,
            "auction_state": state.auction_state,
            "gap_pct": state.gap_pct,
            "large_gap": state.large_gap,
            "range_locked": state.range_locked,
            "accepted_range_id": state.accepted_range_id,
            "exhaustion_active": state.exhaustion_active,
            "exhaustion_thesis_unresolved": state.exhaustion_thesis_unresolved,
            "exhausted_side": state.exhausted_side,
            "exhaustion_started_at": _iso(state.exhaustion_started_at),
            "active_episode_id": state.active_episode_id,
            "proposed_reversal_confirmed": state.proposed_reversal_confirmed,
            "reversal_failed": state.reversal_failed,
            "continuation_unlocked": state.continuation_unlocked,
        }
        for state in timeline
    ]


def _error_rows(errors: Sequence[ExperimentError]) -> List[Dict[str, object]]:
    return [
        {
            "symbol": error.symbol,
            "snapshot_time": _iso(error.snapshot_time),
            "stage": error.stage,
            "error_type": error.error_type,
            "message": error.message,
        }
        for error in errors
    ]


def _sum_known_package_pnl(rows: Sequence[Dict[str, object]]) -> float:
    total = 0.0
    for row in rows:
        value = row["package_pnl"]
        if value is not None:
            total += float(value)
    return total


def _summary(
    *,
    config: AuctionPolicyExperimentConfig,
    snapshots: Sequence[SnapshotSchema],
    episodes: Sequence[ExhaustionEpisode],
    baseline_rows: Sequence[Dict[str, object]],
    errors: Sequence[ExperimentError],
    output_files: Dict[str, str],
) -> Dict[str, object]:
    confirmed = [item for item in episodes if item.confirmation_time is not None]
    failed = [item for item in confirmed if item.reversal_failed_time is not None]
    qualified = [item for item in episodes if item.initial_room_qualified]
    blocked_range = [
        row for row in baseline_rows if row["classification"] == "BLOCK_RANGE_LOCKED"
    ]
    blocked_chase = [
        row
        for row in baseline_rows
        if row["classification"] == "BLOCK_UNRESOLVED_EXHAUSTION_CHASE"
    ]
    unchanged = [
        row
        for row in baseline_rows
        if row["classification"] == "UNCHANGED_BY_EXPERIMENT_POLICY"
    ]
    delayed = [
        item
        for item in confirmed
        if item.baseline_delay_minutes is not None
    ]

    return {
        "program": "replay_exhaustion_priority_experiment.py",
        "mode": config.mode,
        "production_behavior_changed": False,
        "database_writes": False,
        "trading_day": config.run.trading_day.isoformat(),
        "replay_userid": config.run.replay_userid,
        "symbols_filter": list(config.run.symbols),
        "snapshots_loaded": len(snapshots),
        "symbols_loaded": len({snapshot.symbol for snapshot in snapshots}),
        "episodes_detected": len(episodes),
        "episodes_initial_room_qualified": len(qualified),
        "proposed_reversals_confirmed": len(confirmed),
        "proposed_reversals_failed": len(failed),
        "baseline_signals": len(baseline_rows),
        "baseline_signals_blocked_by_range": len(blocked_range),
        "baseline_signals_blocked_as_chase": len(blocked_chase),
        "baseline_signals_unchanged": len(unchanged),
        "baseline_package_pnl_all_known": _sum_known_package_pnl(baseline_rows),
        "baseline_package_pnl_range_blocked_known": _sum_known_package_pnl(
            blocked_range
        ),
        "baseline_package_pnl_chase_blocked_known": _sum_known_package_pnl(
            blocked_chase
        ),
        "baseline_package_pnl_unchanged_known": _sum_known_package_pnl(unchanged),
        "confirmed_with_baseline_comparison": len(delayed),
        "median_baseline_delay_minutes": (
            sorted(float(item.baseline_delay_minutes) for item in delayed)[
                len(delayed) // 2
            ]
            if delayed
            else None
        ),
        "errors": len(errors),
        "config": config.model_dump(mode="json"),
        "output_files": output_files,
    }


def run(
    config: AuctionPolicyExperimentConfig = AUCTION_POLICY_EXPERIMENT_CONFIG,
) -> Dict[str, str]:
    if config.mode != "REPORT_ONLY":
        raise ValueError("Only REPORT_ONLY mode is permitted")

    snapshots = _load_snapshots(config)
    signals = _load_baseline_signals(config)
    episodes, timeline, errors, snapshots_by_symbol = _process_snapshots(
        snapshots,
        config,
    )
    _attach_baseline_comparison(episodes, signals)
    _attach_outcomes(episodes, snapshots_by_symbol, config)
    baseline_rows = _classify_baseline_signals(signals, timeline)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = Path(config.run.report_dir)
    prefix = (
        f"{config.run.report_prefix}_{config.run.trading_day.isoformat()}_{stamp}"
    )
    paths = {
        "episodes": root / f"{prefix}_episodes.csv",
        "baseline_policy": root / f"{prefix}_baseline_signal_policy.csv",
        "timeline": root / f"{prefix}_timeline.csv",
        "errors": root / f"{prefix}_errors.csv",
        "summary": root / f"{prefix}_summary.json",
    }
    output_files = {key: str(path) for key, path in paths.items()}

    _write_csv(paths["episodes"], _episode_rows(episodes))
    _write_csv(paths["baseline_policy"], baseline_rows)
    _write_csv(paths["timeline"], _timeline_rows(timeline))
    _write_csv(paths["errors"], _error_rows(errors))
    summary = _summary(
        config=config,
        snapshots=snapshots,
        episodes=episodes,
        baseline_rows=baseline_rows,
        errors=errors,
        output_files=output_files,
    )
    paths["summary"].parent.mkdir(parents=True, exist_ok=True)
    paths["summary"].write_text(
        json.dumps(sanitize_json(summary), indent=2, sort_keys=True),
        encoding="utf-8",
    )

    logger.info(
        "Experiment complete | day=%s snapshots=%s episodes=%s confirmed=%s "
        "range_blocks=%s chase_blocks=%s errors=%s",
        config.run.trading_day,
        len(snapshots),
        len(episodes),
        sum(1 for item in episodes if item.confirmation_time is not None),
        sum(
            1
            for row in baseline_rows
            if row["classification"] == "BLOCK_RANGE_LOCKED"
        ),
        sum(
            1
            for row in baseline_rows
            if row["classification"]
            == "BLOCK_UNRESOLVED_EXHAUSTION_CHASE"
        ),
        len(errors),
    )
    for key, path in output_files.items():
        logger.info("Report[%s]=%s", key, path)
    return output_files


def main() -> None:
    setup_logging()
    run()


if __name__ == "__main__":
    main()
