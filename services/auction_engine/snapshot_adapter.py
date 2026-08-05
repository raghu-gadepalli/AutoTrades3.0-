"""Snapshot adapter for the Auction directional core."""
from __future__ import annotations

from datetime import datetime
import logging
import time
from typing import Optional

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG
from configs.snapshot_config import SNAPSHOT_CONFIG
from schemas.snapshot import AuctionMemoryBlock, AuctionSnapshotBlock, SnapshotSchema
from services.auction_engine.engine import AuctionEngine

logger = logging.getLogger(__name__)


def initial_auction_memory(symbol: str, snapshot_time: datetime) -> AuctionMemoryBlock:
    return AuctionMemoryBlock.model_validate(
        AuctionEngine.initial_memory(symbol, snapshot_time).model_dump(mode="python")
    )


def empty_auction_block() -> AuctionSnapshotBlock:
    return AuctionSnapshotBlock(
        status="NOT_RUN",
        continuity_mode="COLD_START",
        previous_snapshot_time=None,
        evidence=None,
        directional=None,
        balance=None,
        events=(),
        permissions=(),
        diagnostics={},
    )


def enrich_snapshot_with_auction(
    snapshot: SnapshotSchema,
    *,
    previous_snapshot: Optional[SnapshotSchema] = None,
) -> tuple[AuctionSnapshotBlock, AuctionMemoryBlock]:
    symbol = snapshot.symbol.strip().upper()
    snapshot_time = snapshot.snapshot_time
    engine = AuctionEngine(AUCTION_ENGINE_CONFIG)
    previous_time: Optional[datetime] = None
    continuity_mode = "COLD_START"
    started = time.perf_counter()

    if previous_snapshot is not None:
        previous_time = previous_snapshot.snapshot_time
        if previous_snapshot.symbol.strip().upper() != symbol:
            raise ValueError(
                f"Previous snapshot symbol mismatch: {previous_snapshot.symbol} != {symbol}"
            )
        if previous_time >= snapshot_time:
            raise ValueError(
                f"Previous snapshot time must precede current snapshot: "
                f"{previous_time} >= {snapshot_time}"
            )
        if previous_time.date() == snapshot_time.date():
            gap_minutes = (snapshot_time - previous_time).total_seconds() / 60.0
            max_gap = float(SNAPSHOT_CONFIG.auction.max_incremental_gap_minutes)
            if gap_minutes > max_gap:
                raise ValueError(
                    f"Auction continuity gap is {gap_minutes:.3f} minutes; "
                    f"maximum is {max_gap:.3f}"
                )
            if previous_snapshot.auction.status != "OK":
                raise ValueError("Previous same-day Auction block is not OK")
            previous_memory = previous_snapshot.memory.auction
            if previous_memory.last_snapshot_time != previous_time:
                raise ValueError(
                    "Previous Auction memory last_snapshot_time does not match snapshot"
                )
            history_limit = int(AUCTION_ENGINE_CONFIG.state.history_bars)
            query_time = (
                previous_time.replace(tzinfo=None)
                if previous_time.tzinfo is not None
                else previous_time
            )
            history_snapshots = SnapshotSchema.fetch_recent_today_for_symbol_before_time(
                symbol,
                query_time,
                limit=history_limit,
                ascending=True,
            )
            by_time = {item.snapshot_time: item for item in history_snapshots}
            by_time[previous_snapshot.snapshot_time] = previous_snapshot
            history_snapshots = [by_time[key] for key in sorted(by_time)]
            engine.restore_incremental_state(
                symbol,
                previous_memory,
                history_snapshots=history_snapshots[-history_limit:],
            )
            continuity_mode = "INCREMENTAL_PREVIOUS_SNAPSHOT"

    result = engine.evaluate_snapshot(snapshot, equity_ref=symbol)
    memory = AuctionMemoryBlock.model_validate(
        engine.export_incremental_state(symbol).model_dump(mode="python")
    )
    block = AuctionSnapshotBlock(
        status="OK",
        continuity_mode=continuity_mode,
        previous_snapshot_time=previous_time,
        evidence=result.fresh_direction,
        directional=result.directional,
        balance=result.balance,
        events=result.events,
        permissions=result.permissions,
        diagnostics=result.diagnostics,
    )

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    logger.info(
        "auction_snapshot symbol=%s snapshot_time=%s continuity_mode=%s "
        "elapsed_ms=%.3f fresh_direction=%s active_episode=%s balance=%s events=%d",
        symbol,
        snapshot_time,
        continuity_mode,
        elapsed_ms,
        result.fresh_direction.side.value,
        result.directional.active_episode_id,
        result.balance.current_state.value,
        len(result.events),
    )
    return block, memory


__all__ = [
    "empty_auction_block",
    "initial_auction_memory",
    "enrich_snapshot_with_auction",
]
