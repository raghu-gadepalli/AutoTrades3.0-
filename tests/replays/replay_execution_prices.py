"""Strict replay-only execution pricing from persisted historical candles.

This module is intentionally confined to ``tests/replays``.  Production trade
execution remains unchanged.  Replay runners may temporarily replace the
virtual fill resolver with a deterministic candle-backed resolver.

Contract
--------
- ``snapshot`` keeps the existing replay behavior.
- ``1m_candle`` uses the OPEN of the exact first one-minute candle that starts
  at or after the executor's causal ``asof_time``.
- If that exact candle is missing or invalid, the execution intent is blocked
  for the remainder of the process.  A later candle is never substituted.
- There is no API access and no fallback to snapshot/planned/live prices in
  ``1m_candle`` mode.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
import logging
from typing import Any, Iterator, Optional, Set, Tuple

from configs.replay_config import REPLAY_CONFIG
from schemas.candle import CandleSchema
from services.trade.executor import trade_executor as executor_module

logger = logging.getLogger(__name__)

PRICE_SOURCE_SNAPSHOT = "snapshot"
PRICE_SOURCE_1M_CANDLE = "1m_candle"
SUPPORTED_REPLAY_PRICE_SOURCES = (
    PRICE_SOURCE_SNAPSHOT,
    PRICE_SOURCE_1M_CANDLE,
)
DEFAULT_REPLAY_EXECUTION_PRICE_SOURCE = REPLAY_CONFIG.execution_price_source


class ReplayExecutionCandleError(RuntimeError):
    """Raised when strict persisted-candle execution pricing cannot proceed."""


def _naive_ist(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    # AutoTrades persists replay market timestamps as naive IST wall-clock time.
    if value.tzinfo is not None:
        return value.astimezone(executor_module.IST).replace(tzinfo=None)
    return value.replace(tzinfo=None)


def _first_executable_minute(asof_time: datetime) -> datetime:
    """Return the first one-minute candle start causally executable at ``asof``."""
    asof = _naive_ist(asof_time)
    if asof is None:
        raise ReplayExecutionCandleError("REPLAY_EXECUTION_ASOF_TIME_MISSING")

    minute = asof.replace(second=0, microsecond=0)
    if asof.second or asof.microsecond:
        minute += timedelta(minutes=1)
    return minute


def _intent_key(
    symbol: str,
    side: str,
    planned_time: Optional[datetime],
) -> Tuple[str, str, datetime]:
    planned = _naive_ist(planned_time)
    if planned is None:
        raise ReplayExecutionCandleError(
            "REPLAY_EXECUTION_INTENT_TIME_MISSING "
            f"symbol={str(symbol or '').strip().upper()} side={str(side or '').strip().upper()}"
        )
    return (
        str(symbol or "").strip().upper(),
        str(side or "").strip().upper(),
        planned,
    )


def _strict_1m_fill_resolver(blocked_intents: Set[Tuple[str, str, datetime]]):
    def _resolve(
        symbol: str,
        side: str,
        planned_price: Optional[Any],
        planned_time: Optional[datetime] = None,
        asof_time: Optional[datetime] = None,
        *,
        equity_ref: Optional[str] = None,
        instrument_type: Optional[Any] = None,
        last_known_price: Optional[Any] = None,
    ) -> Tuple[Decimal, datetime]:
        del planned_price, equity_ref, instrument_type, last_known_price

        symbol0 = str(symbol or "").strip().upper()
        side0 = str(side or "").strip().upper()
        if not symbol0:
            raise ReplayExecutionCandleError("REPLAY_EXECUTION_SYMBOL_MISSING")

        key = _intent_key(symbol0, side0, planned_time)
        if key in blocked_intents:
            raise ReplayExecutionCandleError(
                "REPLAY_EXECUTION_INTENT_BLOCKED_AFTER_CANDLE_ERROR "
                f"symbol={symbol0} side={side0} intent_time={key[2]}"
            )

        asof = _naive_ist(asof_time)
        if asof is None:
            blocked_intents.add(key)
            raise ReplayExecutionCandleError(
                "REPLAY_EXECUTION_ASOF_TIME_MISSING "
                f"symbol={symbol0} side={side0} intent_time={key[2]}"
            )

        required_time = _first_executable_minute(asof)
        candle = CandleSchema.fetch_candle(
            symbol=symbol0,
            frequency=1,
            candle_time=required_time,
        )
        if candle is None:
            blocked_intents.add(key)
            logger.error(
                "REPLAY_EXECUTION_CANDLE_MISSING | symbol=%s side=%s "
                "intent_time=%s asof_time=%s required_candle_time=%s frequency=1",
                symbol0,
                side0,
                key[2],
                asof,
                required_time,
            )
            raise ReplayExecutionCandleError(
                "REPLAY_EXECUTION_CANDLE_MISSING "
                f"symbol={symbol0} side={side0} intent_time={key[2]} "
                f"asof_time={asof} required_candle_time={required_time} frequency=1"
            )

        px = Decimal(str(candle.open or 0))
        if px <= 0:
            blocked_intents.add(key)
            logger.error(
                "REPLAY_EXECUTION_CANDLE_INVALID | symbol=%s side=%s "
                "intent_time=%s asof_time=%s candle_time=%s open=%s",
                symbol0,
                side0,
                key[2],
                asof,
                required_time,
                candle.open,
            )
            raise ReplayExecutionCandleError(
                "REPLAY_EXECUTION_CANDLE_INVALID "
                f"symbol={symbol0} side={side0} intent_time={key[2]} "
                f"candle_time={required_time} open={candle.open}"
            )

        return px, required_time

    return _resolve


@contextmanager
def replay_execution_price_source(source: str) -> Iterator[None]:
    """Temporarily select the virtual execution price source for one replay."""
    source0 = str(source or "").strip().lower()
    if source0 not in SUPPORTED_REPLAY_PRICE_SOURCES:
        raise ValueError(
            f"Unsupported replay execution price source {source!r}; "
            f"expected one of {SUPPORTED_REPLAY_PRICE_SOURCES}"
        )

    if source0 == PRICE_SOURCE_SNAPSHOT:
        yield
        return

    old_resolver = executor_module._virtual_fill_price_time
    blocked_intents: Set[Tuple[str, str, datetime]] = set()
    executor_module._virtual_fill_price_time = _strict_1m_fill_resolver(blocked_intents)
    try:
        logger.info(
            "REPLAY_EXECUTION_PRICE_SOURCE | source=1m_candle frequency=1 field=open "
            "fallback=NONE"
        )
        yield
    finally:
        executor_module._virtual_fill_price_time = old_resolver
