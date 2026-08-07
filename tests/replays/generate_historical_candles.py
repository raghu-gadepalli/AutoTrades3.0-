#!/usr/bin/env python3
"""Populate persisted historical candles for deterministic replay execution.

This is an ad-hoc replay preparation utility.  It may call the broker historical
API, but the causal replay itself must not.  Instrument sourcing is controlled
by the source-level ``GET_SYMBOLS_FROM_TRADES`` flag.  When enabled, the script
loads every distinct symbol currently present in ``user_trades`` with no date
filter.  When disabled, instruments come from ``DEFAULT_INSTRUMENTS`` or the
existing ``--instruments`` CLI override.

PowerShell example
------------------

With ``GET_SYMBOLS_FROM_TRADES=True``:

    python tests/replays/generate_historical_candles.py \
        --day 2026-08-07 \
        --timeframe 1m \
        --mode replay

Set ``GET_SYMBOLS_FROM_TRADES=False`` to use ``DEFAULT_INSTRUMENTS`` or the
existing ``--instruments`` CLI override instead.

Defaults are visible below and every operational value can be overridden from
CLI.  Existing rows are preserved.  Conflicting existing rows are logged as
errors and are never silently overwritten.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
import logging
import os
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config import AppConfig
from database.database import get_trades_db
from logconfig import setup_logging
from models.trade_models import Candle as CandleORM, UserTrade as UserTradeORM
from schemas.instrument import InstrumentSchema
from schemas.user import UserSchema
from services.zerodha.kiteconnect_service import KiteConnectService

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

# =============================================================================
# SOURCE DEFAULTS - edit these for the usual ad-hoc run; CLI values override.
# =============================================================================
DEFAULT_DAY = "2026-08-07"
DEFAULT_TIMEFRAME = "1m"
DEFAULT_MODE = "replay"
DEFAULT_INSTRUMENTS: List[str] = []
# Source-level choice only.  This is intentionally NOT exposed as a CLI flag.
# True  -> use all distinct user_trades.symbol values, with no date filter.
# False -> use DEFAULT_INSTRUMENTS or the existing --instruments CLI override.
GET_SYMBOLS_FROM_TRADES = True
DEFAULT_DATA_USER_ID = AppConfig.DATA_USER
DEFAULT_API_KEY = ""
DEFAULT_ACCESS_TOKEN = ""
DEFAULT_INCLUDE_OI = True
DEFAULT_LOG_FILE = "reports/generate_historical_candles.log"

# CandleSchema/CandleORM use integer minute frequencies.
TIMEFRAME_MAP: Dict[str, Tuple[int, str]] = {
    "1m": (1, "minute"),
    "3m": (3, "3minute"),
    "5m": (5, "5minute"),
    "10m": (10, "10minute"),
    "15m": (15, "15minute"),
    "30m": (30, "30minute"),
    "60m": (60, "60minute"),
}


def _args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch historical candles from the configured broker and persist them "
            "for deterministic replay. Defaults live in this file; CLI overrides them."
        )
    )
    parser.add_argument(
        "--day",
        default=DEFAULT_DAY,
        help=f"Trading day YYYY-MM-DD (default: {DEFAULT_DAY})",
    )
    parser.add_argument(
        "--timeframe",
        default=DEFAULT_TIMEFRAME,
        choices=tuple(TIMEFRAME_MAP),
        help=f"Candle timeframe (default: {DEFAULT_TIMEFRAME})",
    )
    parser.add_argument(
        "--mode",
        default=DEFAULT_MODE,
        choices=("replay",),
        help=f"Preparation mode (default: {DEFAULT_MODE})",
    )
    parser.add_argument(
        "--instruments",
        nargs="+",
        default=None,
        help=(
            "Instrument symbols. Used only when GET_SYMBOLS_FROM_TRADES=False; "
            "overrides DEFAULT_INSTRUMENTS. Items may also be comma-separated."
        ),
    )
    parser.add_argument(
        "--data-user",
        default=DEFAULT_DATA_USER_ID,
        help=f"User containing broker credentials (default: {DEFAULT_DATA_USER_ID})",
    )
    parser.add_argument("--api-key", default=DEFAULT_API_KEY)
    parser.add_argument("--access-token", default=DEFAULT_ACCESS_TOKEN)
    parser.add_argument(
        "--include-oi",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_INCLUDE_OI,
        help=f"Request OI from broker historical API (default: {DEFAULT_INCLUDE_OI})",
    )
    parser.add_argument("--log-file", default=DEFAULT_LOG_FILE)
    return parser.parse_args(argv)


def _normalise_instruments(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for raw in values:
        for item in str(raw or "").split(","):
            symbol = item.strip().upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            out.append(symbol)
    if not out:
        raise ValueError(
            "No instruments configured. Populate DEFAULT_INSTRUMENTS or pass --instruments."
        )
    return out


def _get_distinct_trade_symbols() -> List[str]:
    """Return every distinct symbol currently present in user_trades.

    Deliberately has no date/user/status filter.  This is an operator-selected
    data-preparation mode controlled only by GET_SYMBOLS_FROM_TRADES.
    """
    with get_trades_db() as db:
        rows = (
            db.query(UserTradeORM.symbol)
            .filter(UserTradeORM.symbol.isnot(None))
            .distinct()
            .order_by(UserTradeORM.symbol.asc())
            .all()
        )

    raw_symbols = [row[0] for row in rows if row and row[0]]
    if not raw_symbols:
        raise RuntimeError(
            "Historical-candle preflight failed: user_trades contains no symbols"
        )
    symbols = _normalise_instruments(raw_symbols)
    logger.info(
        "HISTORICAL_CANDLE_SYMBOLS_FROM_TRADES | symbols=%d",
        len(symbols),
    )
    return symbols


def _resolve_credentials(
    *,
    data_user: str,
    api_key_override: str,
    access_token_override: str,
) -> Tuple[str, str, str]:
    userid = str(data_user or "").strip()
    user = UserSchema.fetch_user(userid) if userid else None
    db_api_key = str(getattr(user, "apikey", "") or "").strip() if user else ""
    db_access_token = (
        str(getattr(user, "access_token", "") or "").strip() if user else ""
    )

    api_key = str(api_key_override or db_api_key).strip()
    access_token = str(access_token_override or db_access_token).strip()
    if not api_key or not access_token:
        raise RuntimeError(
            "Historical-candle preflight failed: broker API key/access token unavailable"
        )
    source = "CLI" if api_key_override or access_token_override else f"USER:{userid}"
    return api_key, access_token, source


def _day_bounds(day: date) -> Tuple[datetime, datetime, datetime, datetime]:
    start_aware = datetime.combine(day, time.min, tzinfo=IST)
    end_aware = start_aware + timedelta(days=1) - timedelta(seconds=1)
    start_naive = start_aware.replace(tzinfo=None)
    next_day_naive = (start_aware + timedelta(days=1)).replace(tzinfo=None)
    return start_aware, end_aware, start_naive, next_day_naive


def _naive_ist(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise TypeError(f"Historical candle date must be datetime/string, got {type(value)!r}")
    if value.tzinfo is not None:
        return value.astimezone(IST).replace(tzinfo=None)
    return value.replace(tzinfo=None)


def _number(value: Any, field: str) -> float:
    if value is None:
        if field in {"volume", "oi"}:
            return 0.0
        raise ValueError(f"missing required field {field}")
    return float(value)


def _same_existing(row: CandleORM, payload: Dict[str, Any]) -> bool:
    checks = ("open", "high", "low", "close", "volume", "oi")
    for field in checks:
        if abs(float(getattr(row, field) or 0) - float(payload[field])) > 1e-9:
            return False
    return True


def _persist_instrument_day(
    *,
    symbol: str,
    frequency: int,
    records: Sequence[dict],
    day_start: datetime,
    next_day: datetime,
) -> Dict[str, int]:
    stats = {"new": 0, "existing": 0, "conflicts": 0, "invalid": 0, "out_of_day": 0}

    with get_trades_db() as db:
        existing_rows = (
            db.query(CandleORM)
            .filter(
                CandleORM.symbol == symbol,
                CandleORM.frequency == frequency,
                CandleORM.candle_time >= day_start,
                CandleORM.candle_time < next_day,
            )
            .all()
        )
        existing = {row.candle_time: row for row in existing_rows}

        for index, raw in enumerate(records):
            try:
                candle_time = _naive_ist(raw.get("date"))
                if not (day_start <= candle_time < next_day):
                    stats["out_of_day"] += 1
                    logger.warning(
                        "HISTORICAL_CANDLE_OUT_OF_DAY | symbol=%s index=%d candle_time=%s",
                        symbol,
                        index,
                        candle_time,
                    )
                    continue

                payload = {
                    "symbol": symbol,
                    "frequency": frequency,
                    "candle_time": candle_time,
                    "open": _number(raw.get("open"), "open"),
                    "high": _number(raw.get("high"), "high"),
                    "low": _number(raw.get("low"), "low"),
                    "close": _number(raw.get("close"), "close"),
                    "volume": _number(raw.get("volume"), "volume"),
                    "oi": _number(raw.get("oi"), "oi"),
                    "active": True,
                }

                prior = existing.get(candle_time)
                if prior is not None:
                    if _same_existing(prior, payload):
                        stats["existing"] += 1
                    else:
                        stats["conflicts"] += 1
                        logger.error(
                            "HISTORICAL_CANDLE_CONFLICT | symbol=%s frequency=%d "
                            "candle_time=%s action=KEEP_EXISTING",
                            symbol,
                            frequency,
                            candle_time,
                        )
                    continue

                orm = CandleORM(**payload)
                db.add(orm)
                existing[candle_time] = orm
                stats["new"] += 1
            except Exception:
                stats["invalid"] += 1
                logger.exception(
                    "HISTORICAL_CANDLE_RECORD_FAILED | symbol=%s index=%d action=CONTINUE",
                    symbol,
                    index,
                )

        try:
            db.commit()
        except Exception:
            db.rollback()
            logger.exception(
                "HISTORICAL_CANDLE_PERSIST_FAILED | symbol=%s frequency=%d action=CONTINUE",
                symbol,
                frequency,
            )
            # The whole symbol transaction failed; do not report attempted rows as written.
            stats["new"] = 0
            stats["invalid"] += 1

    return stats


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _args(argv)
    setup_logging(log_file=args.log_file)
    global logger
    logger = logging.getLogger(__name__)

    # Startup/preflight failures are allowed to terminate this ad-hoc preparation job.
    trading_day = date.fromisoformat(args.day)
    frequency, broker_interval = TIMEFRAME_MAP[args.timeframe]
    if GET_SYMBOLS_FROM_TRADES:
        if args.instruments is not None:
            logger.warning(
                "HISTORICAL_CANDLE_INSTRUMENTS_CLI_IGNORED | "
                "reason=GET_SYMBOLS_FROM_TRADES_ENABLED"
            )
        instruments = _get_distinct_trade_symbols()
        instrument_source = "USER_TRADES_DISTINCT"
    else:
        instruments = _normalise_instruments(
            args.instruments if args.instruments is not None else DEFAULT_INSTRUMENTS
        )
        instrument_source = (
            "CLI" if args.instruments is not None else "DEFAULT_INSTRUMENTS"
        )
    api_key, access_token, credential_source = _resolve_credentials(
        data_user=args.data_user,
        api_key_override=args.api_key,
        access_token_override=args.access_token,
    )
    from_date, to_date, day_start, next_day = _day_bounds(trading_day)
    broker = KiteConnectService(api_key=api_key, access_token=access_token)

    logger.info(
        "HISTORICAL_CANDLE_LOAD_START | day=%s timeframe=%s frequency=%d mode=%s "
        "instruments=%d instrument_source=%s credentials=%s",
        trading_day,
        args.timeframe,
        frequency,
        args.mode,
        len(instruments),
        instrument_source,
        credential_source,
    )

    totals = {
        "requested": len(instruments),
        "resolved": 0,
        "api_empty": 0,
        "instrument_missing": 0,
        "api_failed": 0,
        "new": 0,
        "existing": 0,
        "conflicts": 0,
        "invalid": 0,
        "out_of_day": 0,
    }

    for symbol in instruments:
        instrument = InstrumentSchema.fetch_instrument(symbol)
        if instrument is None:
            totals["instrument_missing"] += 1
            logger.error(
                "HISTORICAL_CANDLE_INSTRUMENT_MISSING | symbol=%s action=CONTINUE",
                symbol,
            )
            continue
        totals["resolved"] += 1

        try:
            records = broker.fetch_historical_data(
                instrument_token=instrument.instrument_token,
                from_date=from_date,
                to_date=to_date,
                interval=broker_interval,
                oi=bool(args.include_oi),
            )
        except Exception:
            totals["api_failed"] += 1
            logger.exception(
                "HISTORICAL_CANDLE_API_FAILED | symbol=%s token=%s action=CONTINUE",
                symbol,
                instrument.instrument_token,
            )
            continue

        if not records:
            totals["api_empty"] += 1
            logger.error(
                "HISTORICAL_CANDLE_API_EMPTY | symbol=%s token=%s interval=%s action=CONTINUE",
                symbol,
                instrument.instrument_token,
                broker_interval,
            )
            continue

        stats = _persist_instrument_day(
            symbol=symbol,
            frequency=frequency,
            records=records,
            day_start=day_start,
            next_day=next_day,
        )
        for key in ("new", "existing", "conflicts", "invalid", "out_of_day"):
            totals[key] += stats[key]
        logger.info(
            "HISTORICAL_CANDLE_INSTRUMENT_DONE | symbol=%s rows=%d new=%d existing=%d "
            "conflicts=%d invalid=%d out_of_day=%d",
            symbol,
            len(records),
            stats["new"],
            stats["existing"],
            stats["conflicts"],
            stats["invalid"],
            stats["out_of_day"],
        )

    has_errors = any(
        totals[key] > 0
        for key in ("api_empty", "instrument_missing", "api_failed", "conflicts", "invalid")
    )
    logger.info(
        "HISTORICAL_CANDLE_LOAD_DONE | status=%s totals=%s",
        "COMPLETED_WITH_ERRORS" if has_errors else "COMPLETED",
        totals,
    )
    # Per-instrument/per-record failures do not abort backend-style processing.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
