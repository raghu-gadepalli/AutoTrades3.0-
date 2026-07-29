"""Strict authoritative Auction snapshot adapter.

The adapter restores only Persistent Episode memory from the immediately
previous same-day snapshot, evaluates objective evidence and lifecycle once,
and writes the final Auction observation/lifecycle projection.  It contains no
legacy state, boundary, setup, opportunity, decision or fallback path.
"""
from __future__ import annotations

from datetime import datetime
import logging
import time
from typing import Optional

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG
from configs.snapshot_config import SNAPSHOT_CONFIG
from schemas.snapshot import (
    AuctionEngineIdentityProjection,
    AuctionMemoryBlock,
    AuctionSnapshotBlock,
    SnapshotSchema,
)
from services.auction_engine.engine import AuctionEngine

logger = logging.getLogger(__name__)


def initial_auction_memory(symbol: str, snapshot_time: datetime) -> AuctionMemoryBlock:
    """Return the explicit first-snapshot authoritative memory state."""
    return AuctionMemoryBlock.model_validate(
        AuctionEngine.initial_memory(symbol, snapshot_time).model_dump(mode="python")
    )


def empty_auction_block() -> AuctionSnapshotBlock:
    """Explicit pre-evaluation placeholder used only during snapshot assembly."""
    return AuctionSnapshotBlock(
        status="NOT_RUN",
        continuity_mode="COLD_START",
        previous_snapshot_time=None,
        engine=None,
        observation=None,
        lifecycle=None,
    )


def enrich_snapshot_with_auction(
    snapshot: SnapshotSchema,
    *,
    previous_snapshot: Optional[SnapshotSchema] = None,
) -> tuple[AuctionSnapshotBlock, AuctionMemoryBlock]:
    """Evaluate one snapshot through the sole authoritative Auction lifecycle."""
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
                raise ValueError("Previous same-day authoritative Auction block is not OK")
            previous_memory = previous_snapshot.memory.auction
            if previous_memory.last_snapshot_time != previous_time:
                raise ValueError(
                    "Previous Auction memory last_snapshot_time does not match snapshot"
                )
            engine.restore_incremental_state(symbol, previous_memory)
            continuity_mode = "INCREMENTAL_PREVIOUS_SNAPSHOT"

    result = engine.evaluate_snapshot(snapshot, equity_ref=symbol)
    memory = AuctionMemoryBlock.model_validate(
        engine.export_incremental_state(symbol).model_dump(mode="python")
    )
    lifecycle = result.lifecycle

    block = AuctionSnapshotBlock(
        status="OK",
        continuity_mode=continuity_mode,
        previous_snapshot_time=previous_time,
        engine=AuctionEngineIdentityProjection(
            name=AUCTION_ENGINE_CONFIG.engine.engine_name,
            version=AUCTION_ENGINE_CONFIG.engine.engine_version,
            config_version=AUCTION_ENGINE_CONFIG.engine.config_version,
            config_hash=AUCTION_ENGINE_CONFIG.stable_hash(),
        ),
        observation=result.observation,
        lifecycle=lifecycle,
    )

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    logger.info(
        "auction_authority_snapshot symbol=%s snapshot_time=%s continuity_mode=%s "
        "elapsed_ms=%.3f directional=%s balance=%s events=%d permissions=%d",
        symbol,
        snapshot_time,
        continuity_mode,
        elapsed_ms,
        lifecycle.directional.current_state.value,
        lifecycle.balance.current_state.value,
        len(lifecycle.events),
        len(lifecycle.permissions),
    )
    return block, memory


__all__ = [
    "empty_auction_block",
    "initial_auction_memory",
    "enrich_snapshot_with_auction",
]
