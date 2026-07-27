#!/usr/bin/env python3
"""V2 replay-only experiment for selective Exhaustion Reversal priority.

The program reads persisted snapshots and current-day signals/trades, then
reports how a stricter policy would have behaved:

1. One qualified episode is preserved through a continuous mature leg.
2. Confirmation cannot occur on the initiation/extreme candle.
3. Initiation-time VWAP room is mandatory and is never recalculated as a
   current-price blocker.
4. Reversal confirmation requires meaningful displacement plus a local
   structure break.
5. Failure requires buffered acceptance beyond the frozen extreme.
6. Range blocking is setup-aware: Failed Breakout remains permitted.
7. Same-direction chase blocking applies only while a qualified episode is
   unresolved.
8. Baseline signal comparisons are one-to-one.

This program is REPORT ONLY. It does not create, update, close, or delete any
snapshot, opportunity, signal, trade, or audit record.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta
import csv
import json
import logging
import os
from pathlib import Path
import sys
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

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
    maturity_qualified,
    range_blocks_setup,
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
    accepted_range_inside: bool
    exhaustion_active: bool
    qualified_episode_unresolved: bool
    exhausted_side: str
    exhaustion_started_at: Optional[datetime]
    active_episode_id: Optional[str]
    episode_status: str
    episode_initial_room_qualified: bool
    episode_maturity_qualified: bool
    proposed_reversal_confirmed: bool
    reversal_failed: bool
    continuation_unlocked: bool
    chase_block_eligible: bool


@dataclass(frozen=True)
class ExperimentError:
    symbol: str
    snapshot_time: Optional[datetime]
    stage: str
    error_type: str
    message: str


@dataclass
class ProcessStats:
    excluded_snapshots: int = 0
    maturity_rejected_observations: int = 0
    room_rejected_observations: int = 0
    rearm_rejected_observations: int = 0
    range_locked_snapshots: int = 0
    chase_block_eligible_snapshots: int = 0


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
) -> Tuple[List[SnapshotSchema], int]:
    loaded: List[SnapshotSchema] = []
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
        loaded.extend(batch)
        last = batch[-1]
        after_time = _naive(last.snapshot_time)
        after_symbol = last.symbol
        if len(batch) < config.run.batch_size:
            break

    if not loaded:
        raise ValueError(
            f"No snapshots found for {config.run.trading_day.isoformat()}"
        )

    excluded = {value.strip().upper() for value in config.run.excluded_symbols}
    included = [
        snapshot
        for snapshot in loaded
        if snapshot.symbol.strip().upper() not in excluded
    ]
    excluded_count = len(loaded) - len(included)
    if not included:
        raise ValueError("All loaded snapshots were excluded by strict configuration")
    return included, excluded_count


def _load_baseline_signals(
    config: AuctionPolicyExperimentConfig,
) -> List[BaselineSignal]:
    day_start = datetime.combine(config.run.trading_day, time.min)
    day_end = day_start + timedelta(days=1)
    symbols = tuple(config.run.symbols)
    excluded = {value.strip().upper() for value in config.run.excluded_symbols}

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
        equity_ref = str(trade.equity_ref).strip().upper()
        if equity_ref in excluded:
            continue
        signal_id = str(trade.signal_id).strip()
        if not signal_id:
            raise ValueError("user_trades.signal_id cannot be empty")
        if trade.exit_pnl is None:
            incomplete_trade_pnl[signal_id] = True
        else:
            pnl_by_signal[signal_id] += float(trade.exit_pnl)

    output: List[BaselineSignal] = []
    for signal in signal_rows:
        symbol = str(signal.symbol).strip().upper()
        if symbol in excluded:
            continue
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
                symbol=symbol,
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


def _current_extreme(snapshot: SnapshotSchema, exhausted_side: str) -> float:
    if exhausted_side == "UP":
        return float(snapshot.bar.high)
    if exhausted_side == "DOWN":
        return float(snapshot.bar.low)
    raise ValueError(f"Unsupported exhausted side: {exhausted_side}")


def _new_episode(
    *,
    snapshot: SnapshotSchema,
    config: AuctionPolicyExperimentConfig,
    gap_pct: float,
    sequence_number: int,
    bar_index: int,
) -> Tuple[Optional[ExhaustionEpisode], str]:
    context = snapshot.auction.stock_context
    if context is None:
        raise ValueError("auction.stock_context is required")
    if not context.exhaustion_active:
        raise ValueError("Cannot create an episode while exhaustion_active=False")
    if context.exhaustion_started_at is None:
        raise ValueError("exhaustion_started_at is required when exhaustion is active")

    reason_codes = tuple(str(value).strip().upper() for value in context.reason_codes)
    mature = maturity_qualified(reason_codes=reason_codes, config=config)
    if not mature:
        return None, "MATURITY_NOT_QUALIFIED"

    exhausted_side = str(context.exhausted_side).strip().upper()
    if exhausted_side == "UP":
        reversal_side = "SELL"
    elif exhausted_side == "DOWN":
        reversal_side = "BUY"
    else:
        raise ValueError(f"Unsupported exhausted side: {exhausted_side}")

    vwap = snapshot.indicators.vwap.value
    if vwap is None or vwap <= 0:
        raise ValueError("indicators.vwap.value is required and must be positive")
    atr = float(snapshot.indicators.atr.value)
    if atr <= 0:
        raise ValueError("indicators.atr.value must be positive")
    extreme = _current_extreme(snapshot, exhausted_side)
    room_points, room_atr, room_pct = room_from_initiation(
        exhausted_side=exhausted_side,
        extreme=extreme,
        vwap=float(vwap),
        atr=atr,
    )
    room_qualified = bool(
        room_atr >= config.exhaustion.minimum_initial_vwap_room_atr
        and room_pct >= config.exhaustion.minimum_initial_vwap_room_pct
    )
    if (
        config.exhaustion.require_initial_vwap_room_for_confirmation
        and not room_qualified
    ):
        return None, "INITIAL_VWAP_ROOM_NOT_QUALIFIED"

    initiation_time = _naive(snapshot.snapshot_time)
    auction_started_at = _naive(context.exhaustion_started_at)
    episode_id = (
        f"EXPERIMENT_V2:{snapshot.symbol}:{sequence_number}:"
        f"{initiation_time.isoformat()}:{exhausted_side}"
    )
    large_gap = abs(gap_pct) >= config.gap.large_gap_threshold_pct
    return (
        ExhaustionEpisode(
            episode_id=episode_id,
            symbol=snapshot.symbol,
            sequence_number=sequence_number,
            exhausted_side=exhausted_side,
            reversal_side=reversal_side,
            initiation_time=initiation_time,
            auction_exhaustion_started_at=auction_started_at,
            initiation_close=float(snapshot.close),
            initiation_atr=atr,
            initiation_vwap=float(vwap),
            initiation_reason_codes=reason_codes,
            maturity_qualified=mature,
            initial_extreme=extreme,
            extreme_price=extreme,
            extreme_time=initiation_time,
            extreme_bar_index=bar_index,
            gap_pct=gap_pct,
            large_gap=large_gap,
            initial_vwap_room_points=room_points,
            initial_vwap_room_atr=room_atr,
            initial_vwap_room_pct=room_pct,
            initial_room_qualified=room_qualified,
            expires_at=initiation_time
            + timedelta(minutes=config.exhaustion.episode_expiry_minutes),
        ),
        "STARTED",
    )


def _update_extreme(
    episode: ExhaustionEpisode,
    snapshot: SnapshotSchema,
    bar_index: int,
) -> None:
    if episode.confirmation_time is not None:
        return
    current_extreme = _current_extreme(snapshot, episode.exhausted_side)
    if episode.exhausted_side == "UP" and current_extreme > episode.extreme_price:
        episode.extreme_price = current_extreme
        episode.extreme_time = _naive(snapshot.snapshot_time)
        episode.extreme_bar_index = bar_index
    elif (
        episode.exhausted_side == "DOWN"
        and current_extreme < episode.extreme_price
    ):
        episode.extreme_price = current_extreme
        episode.extreme_time = _naive(snapshot.snapshot_time)
        episode.extreme_bar_index = bar_index


def _rearm_allowed(
    *,
    last_episode: Optional[ExhaustionEpisode],
    snapshot: SnapshotSchema,
    exhausted_side: str,
    bars_since_terminal: int,
    config: AuctionPolicyExperimentConfig,
) -> bool:
    if last_episode is None:
        return True
    if bars_since_terminal < config.exhaustion.minimum_rearm_bars:
        return False
    if exhausted_side != last_episode.exhausted_side:
        return True
    atr = float(snapshot.indicators.atr.value)
    if atr <= 0:
        raise ValueError("indicators.atr.value must be positive")
    current_extreme = _current_extreme(snapshot, exhausted_side)
    required_extension = config.exhaustion.minimum_rearm_extreme_atr * atr
    if exhausted_side == "UP":
        return current_extreme >= last_episode.extreme_price + required_extension
    if exhausted_side == "DOWN":
        return current_extreme <= last_episode.extreme_price - required_extension
    raise ValueError(f"Unsupported exhausted side: {exhausted_side}")


def _episode_status(episode: Optional[ExhaustionEpisode]) -> str:
    if episode is None:
        return "NONE"
    if episode.continuation_unlocked_time is not None:
        return "CONTINUATION_UNLOCKED"
    if episode.reversal_failed_time is not None:
        return "REVERSAL_FAILED"
    if episode.confirmation_time is not None:
        return "REVERSAL_CONFIRMED"
    return "WATCH"


def _process_snapshots(
    snapshots: Sequence[SnapshotSchema],
    config: AuctionPolicyExperimentConfig,
    excluded_snapshot_count: int,
) -> Tuple[
    List[ExhaustionEpisode],
    List[SnapshotPolicyState],
    List[ExperimentError],
    Dict[str, List[SnapshotSchema]],
    ProcessStats,
]:
    by_symbol: Dict[str, List[SnapshotSchema]] = defaultdict(list)
    for snapshot in snapshots:
        by_symbol[snapshot.symbol].append(snapshot)
    for symbol in by_symbol:
        by_symbol[symbol].sort(key=lambda item: _naive(item.snapshot_time))

    episodes: List[ExhaustionEpisode] = []
    timeline: List[SnapshotPolicyState] = []
    errors: List[ExperimentError] = []
    stats = ProcessStats(excluded_snapshots=excluded_snapshot_count)

    for symbol, rows in sorted(by_symbol.items()):
        active_episode: Optional[ExhaustionEpisode] = None
        last_episode: Optional[ExhaustionEpisode] = None
        bars_since_terminal = config.exhaustion.minimum_rearm_bars
        sequence_number = 0
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

        for bar_index, snapshot in enumerate(rows):
            snapshot_time = _naive(snapshot.snapshot_time)
            try:
                context = snapshot.auction.stock_context
                if context is None:
                    raise ValueError("auction.stock_context is required")

                if active_episode is not None:
                    context_side = str(context.exhausted_side).strip().upper()
                    same_active_context = bool(
                        context.exhaustion_active
                        and context_side == active_episode.exhausted_side
                    )
                    if same_active_context:
                        active_episode.context_inactive_bars = 0
                        _update_extreme(active_episode, snapshot, bar_index)
                    else:
                        active_episode.context_inactive_bars += 1

                    if (
                        active_episode.confirmation_time is None
                        and snapshot_time <= active_episode.expires_at
                    ):
                        lookback = config.exhaustion.local_structure_lookback_bars
                        prior_bars = rows[max(0, bar_index - lookback):bar_index]
                        if prior_bars:
                            confirmed, reason, displacement_atr = confirmation(
                                episode=active_episode,
                                prior_bars=prior_bars,
                                current=snapshot,
                                current_bar_index=bar_index,
                                config=config,
                            )
                            if confirmed:
                                active_episode.confirmation_time = snapshot_time
                                active_episode.confirmation_price = float(snapshot.close)
                                active_episode.confirmation_reason = reason
                                active_episode.confirmation_displacement_atr = (
                                    displacement_atr
                                )

                    update_failure(active_episode, snapshot, config)

                    if (
                        active_episode.completed_time is None
                        and snapshot_time > active_episode.expires_at
                    ):
                        active_episode.completed_time = snapshot_time
                        active_episode.completion_reason = (
                            "CONFIRMED_EXPIRED_UNFAILED"
                            if active_episode.confirmation_time is not None
                            else "UNCONFIRMED_EXPIRED"
                        )
                    if (
                        active_episode.completed_time is None
                        and active_episode.confirmation_time is None
                        and active_episode.context_inactive_bars
                        >= config.exhaustion.inactive_completion_bars
                    ):
                        active_episode.completed_time = snapshot_time
                        active_episode.completion_reason = (
                            "UNCONFIRMED_CONTEXT_INACTIVE"
                        )

                    if active_episode.completed_time is not None:
                        last_episode = active_episode
                        active_episode = None
                        bars_since_terminal = 0
                    else:
                        bars_since_terminal += 1
                else:
                    bars_since_terminal += 1

                if (
                    active_episode is None
                    and config.exhaustion.enabled
                    and context.exhaustion_active
                ):
                    exhausted_side = str(context.exhausted_side).strip().upper()
                    if _rearm_allowed(
                        last_episode=last_episode,
                        snapshot=snapshot,
                        exhausted_side=exhausted_side,
                        bars_since_terminal=bars_since_terminal,
                        config=config,
                    ):
                        sequence_number += 1
                        candidate, reason = _new_episode(
                            snapshot=snapshot,
                            config=config,
                            gap_pct=symbol_gap_pct,
                            sequence_number=sequence_number,
                            bar_index=bar_index,
                        )
                        if candidate is not None:
                            active_episode = candidate
                            episodes.append(candidate)
                        elif reason == "MATURITY_NOT_QUALIFIED":
                            stats.maturity_rejected_observations += 1
                        elif reason == "INITIAL_VWAP_ROOM_NOT_QUALIFIED":
                            stats.room_rejected_observations += 1
                        else:
                            raise ValueError(
                                f"Unsupported episode-start decision: {reason}"
                            )
                    else:
                        stats.rearm_rejected_observations += 1

                locked = is_range_locked(snapshot, config)
                if locked:
                    stats.range_locked_snapshots += 1

                unresolved = bool(
                    active_episode is not None
                    and active_episode.fully_qualified
                    and active_episode.unresolved
                    and snapshot_time <= active_episode.expires_at
                )
                continuation_unlocked = bool(
                    active_episode is not None
                    and active_episode.continuation_unlocked_time is not None
                    and active_episode.continuation_unlocked_time <= snapshot_time
                )
                chase_block_eligible = bool(
                    unresolved and not continuation_unlocked
                )
                if chase_block_eligible:
                    stats.chase_block_eligible_snapshots += 1

                if snapshot.auction.state is None:
                    raise ValueError("auction.state is required")
                timeline.append(
                    SnapshotPolicyState(
                        symbol=symbol,
                        snapshot_time=snapshot_time,
                        close=float(snapshot.close),
                        auction_state=snapshot.auction.state.current,
                        gap_pct=symbol_gap_pct,
                        large_gap=(
                            abs(symbol_gap_pct)
                            >= config.gap.large_gap_threshold_pct
                        ),
                        range_locked=locked,
                        accepted_range_id=context.accepted_range_id,
                        accepted_range_inside=context.accepted_range_inside,
                        exhaustion_active=context.exhaustion_active,
                        qualified_episode_unresolved=unresolved,
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
                        episode_status=_episode_status(active_episode),
                        episode_initial_room_qualified=bool(
                            active_episode is not None
                            and active_episode.initial_room_qualified
                        ),
                        episode_maturity_qualified=bool(
                            active_episode is not None
                            and active_episode.maturity_qualified
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
                        chase_block_eligible=chase_block_eligible,
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

    return episodes, timeline, errors, by_symbol, stats


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
    if not state.chase_block_eligible or state.continuation_unlocked:
        return False
    if state.exhausted_side == "UP":
        return signal.side == "BUY"
    if state.exhausted_side == "DOWN":
        return signal.side == "SELL"
    return False


def _classify_baseline_signals(
    signals: Sequence[BaselineSignal],
    timeline: Sequence[SnapshotPolicyState],
    config: AuctionPolicyExperimentConfig,
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
                    "range_policy_exempt": None,
                    "same_direction_chase": None,
                    "would_block": None,
                    "package_pnl": signal.package_pnl,
                    "signal_last_pnl_pct": signal.signal_last_pnl_pct,
                    "signal_last_pnl_value": signal.signal_last_pnl_value,
                }
            )
            continue

        range_policy_exempt = bool(
            signal.setup
            in config.range_abstention.allowed_range_resolution_setups
        )
        range_block = range_blocks_setup(
            setup=signal.setup,
            locked=state.range_locked,
            config=config,
        )
        chase_block = _same_direction_chase(signal, state)
        if range_block:
            classification = "BLOCK_RANGE_LOCKED"
        elif chase_block:
            classification = "BLOCK_QUALIFIED_EXHAUSTION_CHASE"
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
                "range_policy_exempt": range_policy_exempt,
                "accepted_range_id": state.accepted_range_id,
                "accepted_range_inside": state.accepted_range_inside,
                "exhaustion_active": state.exhaustion_active,
                "qualified_episode_unresolved": state.qualified_episode_unresolved,
                "exhausted_side": state.exhausted_side,
                "active_episode_id": state.active_episode_id,
                "episode_status": state.episode_status,
                "episode_initial_room_qualified": (
                    state.episode_initial_room_qualified
                ),
                "episode_maturity_qualified": state.episode_maturity_qualified,
                "proposed_reversal_confirmed": (
                    state.proposed_reversal_confirmed
                ),
                "reversal_failed": state.reversal_failed,
                "continuation_unlocked": state.continuation_unlocked,
                "chase_block_eligible": state.chase_block_eligible,
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
    config: AuctionPolicyExperimentConfig,
) -> None:
    signals_by_symbol: Dict[str, List[BaselineSignal]] = defaultdict(list)
    for signal in signals:
        signals_by_symbol[signal.symbol].append(signal)
    for symbol in signals_by_symbol:
        signals_by_symbol[symbol].sort(key=lambda item: item.signal_time)

    used_signal_ids: Set[str] = set()
    confirmed_episodes = sorted(
        (episode for episode in episodes if episode.confirmation_time is not None),
        key=lambda item: (item.confirmation_time, item.symbol, item.sequence_number),
    )
    for episode in confirmed_episodes:
        if episode.confirmation_time is None:
            raise ValueError("Confirmed episode is missing confirmation_time")
        match_deadline = episode.confirmation_time + timedelta(
            minutes=config.exhaustion.baseline_match_window_minutes
        )
        for signal in signals_by_symbol[episode.symbol]:
            if signal.signal_id in used_signal_ids:
                continue
            if signal.side != episode.reversal_side:
                continue
            if signal.signal_time < episode.confirmation_time:
                continue
            if signal.signal_time > match_deadline:
                break
            used_signal_ids.add(signal.signal_id)
            episode.baseline_signal_id = signal.signal_id
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
            "sequence_number": episode.sequence_number,
            "exhausted_side": episode.exhausted_side,
            "reversal_side": episode.reversal_side,
            "gap_pct": episode.gap_pct,
            "large_gap": episode.large_gap,
            "initiation_time": _iso(episode.initiation_time),
            "auction_exhaustion_started_at": _iso(
                episode.auction_exhaustion_started_at
            ),
            "initiation_close": episode.initiation_close,
            "initiation_atr": episode.initiation_atr,
            "initiation_vwap": episode.initiation_vwap,
            "initiation_reason_codes": "|".join(episode.initiation_reason_codes),
            "maturity_qualified": episode.maturity_qualified,
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
            "confirmation_displacement_atr": (
                episode.confirmation_displacement_atr
            ),
            "failure_candidate_time": _iso(episode.failure_candidate_time),
            "reversal_failed_time": _iso(episode.reversal_failed_time),
            "continuation_unlocked_time": _iso(
                episode.continuation_unlocked_time
            ),
            "baseline_signal_id": episode.baseline_signal_id,
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
            "completion_reason": episode.completion_reason,
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
            "accepted_range_inside": state.accepted_range_inside,
            "exhaustion_active": state.exhaustion_active,
            "qualified_episode_unresolved": state.qualified_episode_unresolved,
            "exhausted_side": state.exhausted_side,
            "exhaustion_started_at": _iso(state.exhaustion_started_at),
            "active_episode_id": state.active_episode_id,
            "episode_status": state.episode_status,
            "episode_initial_room_qualified": (
                state.episode_initial_room_qualified
            ),
            "episode_maturity_qualified": state.episode_maturity_qualified,
            "proposed_reversal_confirmed": state.proposed_reversal_confirmed,
            "reversal_failed": state.reversal_failed,
            "continuation_unlocked": state.continuation_unlocked,
            "chase_block_eligible": state.chase_block_eligible,
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


def _median(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2.0


def _summary(
    *,
    config: AuctionPolicyExperimentConfig,
    snapshots: Sequence[SnapshotSchema],
    episodes: Sequence[ExhaustionEpisode],
    timeline: Sequence[SnapshotPolicyState],
    baseline_rows: Sequence[Dict[str, object]],
    errors: Sequence[ExperimentError],
    stats: ProcessStats,
    output_files: Dict[str, str],
) -> Dict[str, object]:
    confirmed = [item for item in episodes if item.confirmation_time is not None]
    failed = [item for item in confirmed if item.reversal_failed_time is not None]
    blocked_range = [
        row for row in baseline_rows if row["classification"] == "BLOCK_RANGE_LOCKED"
    ]
    blocked_chase = [
        row
        for row in baseline_rows
        if row["classification"] == "BLOCK_QUALIFIED_EXHAUSTION_CHASE"
    ]
    unchanged = [
        row
        for row in baseline_rows
        if row["classification"] == "UNCHANGED_BY_EXPERIMENT_POLICY"
    ]
    delayed = [
        float(item.baseline_delay_minutes)
        for item in confirmed
        if item.baseline_delay_minutes is not None
    ]
    same_bar_confirmations = [
        item
        for item in confirmed
        if item.confirmation_time == item.initiation_time
    ]
    episodes_by_symbol: Dict[str, int] = defaultdict(int)
    for episode in episodes:
        episodes_by_symbol[episode.symbol] += 1
    episode_counts = list(episodes_by_symbol.values())
    failed_breakout_exempt = [
        row
        for row in baseline_rows
        if row["setup"] == "FAILED_BREAKOUT"
        and row["range_locked"] is True
        and row["range_policy_exempt"] is True
    ]

    return {
        "program": "replay_exhaustion_priority_experiment.py",
        "experiment_version": config.version,
        "mode": config.mode,
        "production_behavior_changed": False,
        "database_writes": False,
        "trading_day": config.run.trading_day.isoformat(),
        "replay_userid": config.run.replay_userid,
        "symbols_filter": list(config.run.symbols),
        "excluded_symbols": list(config.run.excluded_symbols),
        "excluded_snapshots": stats.excluded_snapshots,
        "snapshots_loaded": len(snapshots),
        "symbols_loaded": len({snapshot.symbol for snapshot in snapshots}),
        "episodes_detected": len(episodes),
        "episodes_confirmed": len(confirmed),
        "episodes_failed": len(failed),
        "same_bar_confirmations": len(same_bar_confirmations),
        "symbols_with_episodes": len(episodes_by_symbol),
        "maximum_episodes_per_symbol": max(episode_counts) if episode_counts else 0,
        "median_episodes_per_symbol": _median(
            [float(value) for value in episode_counts]
        ),
        "maturity_rejected_snapshot_observations": (
            stats.maturity_rejected_observations
        ),
        "room_rejected_snapshot_observations": stats.room_rejected_observations,
        "rearm_rejected_snapshot_observations": stats.rearm_rejected_observations,
        "range_locked_snapshots": stats.range_locked_snapshots,
        "range_locked_snapshot_pct": (
            (stats.range_locked_snapshots / len(timeline)) * 100.0
            if timeline
            else None
        ),
        "chase_block_eligible_snapshots": stats.chase_block_eligible_snapshots,
        "chase_block_eligible_snapshot_pct": (
            (stats.chase_block_eligible_snapshots / len(timeline)) * 100.0
            if timeline
            else None
        ),
        "baseline_signals": len(baseline_rows),
        "baseline_signals_blocked_by_range": len(blocked_range),
        "baseline_signals_blocked_as_chase": len(blocked_chase),
        "baseline_signals_unchanged": len(unchanged),
        "failed_breakout_signals_exempted_inside_range": len(
            failed_breakout_exempt
        ),
        "baseline_package_pnl_all_known": _sum_known_package_pnl(baseline_rows),
        "baseline_package_pnl_range_blocked_known": _sum_known_package_pnl(
            blocked_range
        ),
        "baseline_package_pnl_chase_blocked_known": _sum_known_package_pnl(
            blocked_chase
        ),
        "baseline_package_pnl_unchanged_known": _sum_known_package_pnl(unchanged),
        "unique_confirmed_with_baseline_comparison": len(delayed),
        "median_baseline_delay_minutes": _median(delayed),
        "errors": len(errors),
        "config": config.model_dump(mode="json"),
        "output_files": output_files,
    }


def run(
    config: AuctionPolicyExperimentConfig = AUCTION_POLICY_EXPERIMENT_CONFIG,
) -> Dict[str, str]:
    if config.mode != "REPORT_ONLY":
        raise ValueError("Only REPORT_ONLY mode is permitted")

    snapshots, excluded_snapshot_count = _load_snapshots(config)
    signals = _load_baseline_signals(config)
    episodes, timeline, errors, snapshots_by_symbol, stats = _process_snapshots(
        snapshots,
        config,
        excluded_snapshot_count,
    )
    _attach_baseline_comparison(episodes, signals, config)
    _attach_outcomes(episodes, snapshots_by_symbol, config)
    baseline_rows = _classify_baseline_signals(signals, timeline, config)

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
        timeline=timeline,
        baseline_rows=baseline_rows,
        errors=errors,
        stats=stats,
        output_files=output_files,
    )
    paths["summary"].parent.mkdir(parents=True, exist_ok=True)
    paths["summary"].write_text(
        json.dumps(sanitize_json(summary), indent=2, sort_keys=True),
        encoding="utf-8",
    )

    logger.info(
        "V2 experiment complete | day=%s snapshots=%s episodes=%s confirmed=%s "
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
            if row["classification"] == "BLOCK_QUALIFIED_EXHAUSTION_CHASE"
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
