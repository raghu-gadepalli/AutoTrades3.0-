"""Replay-only price provider.

Replay runners do not choose or implement pricing.  They install this provider
for the duration of the replay and the provider owns the complete price-source
contract.

Configured source:
- ``snapshot``: preserve the existing snapshot replay pricing unchanged.
- ``1m_candle``: use one price everywhere -- the latest historical 1-minute
  candle CLOSE available at or before the replay clock.

The 1-minute path is a read-through cache:
1. If the exact replay-minute candle is already in ``candles``, use DB data.
2. If it is missing, fetch historical 1-minute data from the broker, persist
   only candle records that are not already present, then read the price from
   ``candles``.
3. If no causal candle exists after hydration, raise an explicit replay price
   error.  There is no fallback to snapshot/planned/live prices.

Production TradeExecutor and TradeMonitor code is not modified.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, time, timedelta
from decimal import Decimal
import logging
from typing import Any, Iterator, Optional

from configs.replay_config import REPLAY_CONFIG
from database.database import get_trades_db
from models.trade_models import Candle as CandleORM, Symbol as SymbolORM
from schemas.candle import CandleSchema
from services.trade.executor import trade_executor as executor_module
from services.trade.monitor.trade_monitor import TradeMonitor

logger = logging.getLogger(__name__)

PRICE_SOURCE_SNAPSHOT = "snapshot"
PRICE_SOURCE_1M_CANDLE = "1m_candle"
SUPPORTED_REPLAY_PRICE_SOURCES = (PRICE_SOURCE_SNAPSHOT, PRICE_SOURCE_1M_CANDLE)


class ReplayPriceProviderError(RuntimeError):
    """Raised when the configured replay price cannot be produced causally."""


def _replay_minute(value: Optional[datetime]) -> datetime:
    if not isinstance(value, datetime):
        raise ReplayPriceProviderError("REPLAY_PRICE_TIME_MISSING")
    if value.tzinfo is not None:
        value = value.astimezone(executor_module.IST).replace(tzinfo=None)
    return value.replace(second=0, microsecond=0)


def _exact_candle(symbol: str, replay_minute: datetime) -> Optional[CandleSchema]:
    return CandleSchema.fetch_candle(
        symbol=symbol,
        frequency=1,
        candle_time=replay_minute,
    )


def _latest_candle(symbol: str, replay_minute: datetime) -> Optional[CandleORM]:
    day_start = datetime.combine(replay_minute.date(), time(9, 15))
    with get_trades_db() as db:
        return (
            db.query(CandleORM)
            .filter(
                CandleORM.symbol == symbol,
                CandleORM.frequency == 1,
                CandleORM.active == True,  # noqa: E712
                CandleORM.candle_time >= day_start,
                CandleORM.candle_time <= replay_minute,
            )
            .order_by(CandleORM.candle_time.desc())
            .first()
        )


def _instrument_token(symbol: str) -> int:
    """Resolve the broker historical-data token from the validated symbol row.

    The current Kite historical-data service requires an integer token.  This
    is an exact symbol lookup only; ``enabled`` is deliberately not part of the
    replay pricing contract.
    """
    with get_trades_db() as db:
        row = db.query(SymbolORM.token).filter(SymbolORM.symbol == symbol).one_or_none()

    if row is None or row[0] is None:
        raise ReplayPriceProviderError(f"REPLAY_PRICE_TOKEN_MISSING symbol={symbol}")
    try:
        return int(row[0])
    except (TypeError, ValueError) as exc:
        raise ReplayPriceProviderError(
            f"REPLAY_PRICE_TOKEN_INVALID symbol={symbol} token={row[0]!r}"
        ) from exc


def _raw_candle_time(raw: dict) -> datetime:
    value = raw.get("date")
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return _replay_minute(value)


def _persist_missing_candles(symbol: str, records: Any, replay_minute: datetime) -> tuple[int, int]:
    if records is None:
        records = []
    if not isinstance(records, list):
        raise ReplayPriceProviderError(
            f"REPLAY_PRICE_API_RESPONSE_INVALID symbol={symbol} "
            f"type={type(records).__name__}"
        )

    written = 0
    skipped = 0
    for raw in records:
        if not isinstance(raw, dict):
            raise ReplayPriceProviderError(
                f"REPLAY_PRICE_API_RECORD_INVALID symbol={symbol}"
            )

        candle_time = _raw_candle_time(raw)
        if candle_time.date() != replay_minute.date() or candle_time > replay_minute:
            continue

        if _exact_candle(symbol, candle_time) is not None:
            skipped += 1
            continue

        CandleSchema.create_candle(
            {
                "symbol": symbol,
                "frequency": 1,
                "candle_time": candle_time,
                "open": float(raw["open"]),
                "high": float(raw["high"]),
                "low": float(raw["low"]),
                "close": float(raw["close"]),
                "volume": float(raw.get("volume") or 0),
                "oi": float(raw.get("oi") or 0),
                "active": True,
            }
        )
        written += 1

    return written, skipped


def _hydrate_from_historical_api(symbol: str, replay_minute: datetime) -> None:
    token = _instrument_token(symbol)
    broker = executor_module._get_data_user_broker()
    if broker is None:
        raise ReplayPriceProviderError(f"REPLAY_PRICE_BROKER_UNAVAILABLE symbol={symbol}")

    latest = _latest_candle(symbol, replay_minute)
    from_naive = (
        latest.candle_time
        if latest is not None
        else datetime.combine(replay_minute.date(), time(9, 15))
    )
    from_date = from_naive.replace(tzinfo=executor_module.IST)
    to_date = (replay_minute + timedelta(minutes=1)).replace(tzinfo=executor_module.IST)

    records = broker.svc.fetch_historical_data(
        instrument_token=token,
        from_date=from_date,
        to_date=to_date,
        interval="minute",
        oi=True,
    )
    written, skipped = _persist_missing_candles(symbol, records, replay_minute)
    logger.info(
        "REPLAY_PRICE_HYDRATE | symbol=%s through=%s received=%d written=%d skipped_existing=%d",
        symbol,
        replay_minute,
        len(records or []),
        written,
        skipped,
    )


def get_replay_price(symbol: str, replay_time: datetime) -> Decimal:
    """Return the replay equivalent of one live quote/LTP price."""
    symbol0 = str(symbol or "").strip().upper()
    if not symbol0:
        raise ReplayPriceProviderError("REPLAY_PRICE_SYMBOL_MISSING")

    replay_minute = _replay_minute(replay_time)

    # Exact minute already cached: no API work is needed.
    exact = _exact_candle(symbol0, replay_minute)
    if exact is None:
        _hydrate_from_historical_api(symbol0, replay_minute)

    # Sparse derivatives need not trade every minute.  After the provider has
    # hydrated through the replay clock, the latest causal close is the replay
    # equivalent of the live last-traded price.
    candle = _latest_candle(symbol0, replay_minute)
    if candle is None:
        message = (
            "REPLAY_PRICE_CANDLE_MISSING_AFTER_HYDRATION "
            f"symbol={symbol0} replay_time={replay_minute} frequency=1"
        )
        logger.error(message)
        raise ReplayPriceProviderError(message)

    price = Decimal(str(candle.close or 0))
    if price <= 0:
        message = (
            "REPLAY_PRICE_CANDLE_INVALID "
            f"symbol={symbol0} replay_time={replay_minute} "
            f"candle_time={candle.candle_time} close={candle.close}"
        )
        logger.error(message)
        raise ReplayPriceProviderError(message)

    logger.debug(
        "REPLAY_PRICE | symbol=%s replay_time=%s candle_time=%s close=%s",
        symbol0,
        replay_minute,
        candle.candle_time,
        price,
    )
    return price


def _executor_price_adapter(
    symbol: str,
    side: str,
    planned_price: Optional[Any],
    planned_time: Optional[datetime] = None,
    asof_time: Optional[datetime] = None,
    *,
    equity_ref: Optional[str] = None,
    instrument_type: Optional[Any] = None,
    last_known_price: Optional[Any] = None,
):
    del side, planned_price, planned_time, equity_ref, instrument_type, last_known_price
    replay_time = _replay_minute(asof_time)
    return get_replay_price(symbol, replay_time), replay_time


def _monitor_price_adapter(self: TradeMonitor, ut: Any, snapshot: Any) -> Decimal:
    del self
    return get_replay_price(
        getattr(ut, "symbol", None),
        getattr(snapshot, "snapshot_time", None),
    )


@contextmanager
def replay_price_provider() -> Iterator[None]:
    """Install the provider selected by replay config for one replay run."""
    source = str(REPLAY_CONFIG.execution_price_source or "").strip().lower()
    if source not in SUPPORTED_REPLAY_PRICE_SOURCES:
        raise ValueError(
            f"Unsupported replay price source {source!r}; "
            f"expected one of {SUPPORTED_REPLAY_PRICE_SOURCES}"
        )

    if source == PRICE_SOURCE_SNAPSHOT:
        logger.info("REPLAY_PRICE_PROVIDER | source=snapshot")
        yield
        return

    old_executor_price = executor_module._virtual_fill_price_time
    old_monitor_price = TradeMonitor._price_from_snapshot_for_trade
    executor_module._virtual_fill_price_time = _executor_price_adapter
    TradeMonitor._price_from_snapshot_for_trade = _monitor_price_adapter
    try:
        logger.info(
            "REPLAY_PRICE_PROVIDER | source=1m_candle field=close cache=read_through "
            "hydrate=historical_api fallback=NONE"
        )
        yield
    finally:
        TradeMonitor._price_from_snapshot_for_trade = old_monitor_price
        executor_module._virtual_fill_price_time = old_executor_price
