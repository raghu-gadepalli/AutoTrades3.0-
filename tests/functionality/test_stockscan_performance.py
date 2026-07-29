#!/usr/bin/env python3
"""End-of-day performance audit for the current once-daily StockScan selector.

Purpose
-------
The live StockScan runs once at 09:16 IST, after the first completed one-minute
candle, and fixes the day's active basket.  This read-mostly test asks whether
that morning basket captured the stocks that subsequently produced the best
movement during the trading day.

The program:

1. Reads the complete EQ universe from the existing ``symbols`` table.
2. Freezes the actual morning selection from ``enabled`` + ``active`` flags,
   or optionally from a saved symbol/selection CSV.
3. Fetches full-day one-minute historical candles through the application's
   existing Kite client.
4. Aggregates those bars to three-minute OHLCV candles and optionally stores
   only missing rows in the existing ``candles`` table (frequency=3).
5. Ranks every equity by its largest chronologically valid move after 09:16.
6. Reports capture among the full universe, enabled universe, and actual
   StockScan candidate universe, plus missed movers and replaceable selections.

It does not change symbols.enabled, symbols.active, signals, opportunities,
snapshots, or trades.

Run from the project root:

    python tests/test_stockscan_performance.py

This file is configured for 2026-07-24 by default.  Edit only the TEST SETTINGS
section for other dates/runs.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import statistics
import sys
import time as time_module
import traceback
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

# Match existing test-program style: imports resolve from the project root.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config import AppConfig
from configs.scanner_config import SCANNER_CONFIG
from database.database import get_trades_db
from logconfig import setup_logging
from models.trade_models import Candle as CandleORM
from models.trade_models import Symbol as SymbolORM
from schemas.user import UserSchema
from services.zerodha.kiteconnect_service import KiteConnectService
from utils.datetime_utils import IST, to_ist_naive
from utils.universe_policy import universe_blacklist, universe_whitelist


# =============================================================================
# TEST SETTINGS
# =============================================================================

TEST_DATE: date = date(2026, 7, 24)

# The current live scanner runs at 09:16 after the 09:15 one-minute candle.
MARKET_OPEN_TIME: time = time(9, 15)
SELECTION_TIME: time = time(9, 16)
MARKET_CLOSE_TIME: time = time(15, 30)

# Selection source:
#   DATABASE_FLAGS  -> use current symbols.enabled and symbols.active.
#   CSV             -> overlay enabled/active from SELECTION_CSV_PATH.
#
# For 2026-07-24, DATABASE_FLAGS is valid only when the flags still represent
# that day's static basket.  A saved CSV is safer for older historical dates.
SELECTION_SOURCE: str = "DATABASE_FLAGS"
SELECTION_CSV_PATH: str = ""

# Rank individual equities.  The two index symbols can be retained in a
# separate report by setting this True, but are not directly comparable with
# stock movement ranks.
INCLUDE_INDEX_SYMBOLS_IN_RANKING: bool = False
INDEX_SYMBOLS: Set[str] = {"NIFTY 50", "NIFTY BANK"}

# Broker historical source.  One full-day minute request per symbol supplies
# the exact 09:16 anchor and the complete path.  The same data is aggregated to
# 3-minute candles for optional persistence.
FETCH_MINUTE_HISTORY_FROM_BROKER: bool = True
BROKER_INTERVAL: str = "minute"
BROKER_REQUEST_SLEEP_SEC: float = SCANNER_CONFIG.scan.historical_rate_sleep_sec

# Persist only missing 3-minute candles into the application's candles table.
PERSIST_THREE_MINUTE_CANDLES: bool = True
THREE_MINUTE_FREQUENCY: int = 3
OVERWRITE_EXISTING_THREE_MINUTE_CANDLES: bool = False

# A normal NSE session has 375 one-minute bars and 125 three-minute bars.
# A symbol can still be evaluated with fewer bars, but is flagged incomplete.
EXPECTED_MINUTE_BARS: int = 375
MIN_MINUTE_BARS_FOR_EVALUATION: int = 100
EXPECTED_THREE_MINUTE_BARS: int = 125

# Primary movement rank: the largest chronologically valid low->later-high or
# high->later-low move after 09:16, expressed as a percentage.  This captures
# trends and reversals without pretending that high-low order inside one bar is
# known.
PRIMARY_RANK_FIELD: str = "best_ordered_move_pct"

# Absolute threshold used only for the human-readable GOOD_MOVER label.
# Top-K capture reports remain threshold-free.
GOOD_MOVER_MIN_PCT: float = 1.50

# Capture cutoffs.  The actual selected-equity count is added automatically.
TOP_K_VALUES: Tuple[int, ...] = (10, 20, 30, 50, 75)

# Reports.
REPORT_DIR: Path = Path("reports")
REPORT_PREFIX: str = "test_stockscan_performance"
LOG_FILE: Path = REPORT_DIR / "test_stockscan_performance.log"

# Per-symbol failures are logged and processing continues.  Set True only when
# the shell should receive a non-zero status for partial data.
FAIL_IF_SYMBOL_ERRORS: bool = False


logger = logging.getLogger(__name__)
BLACKLIST = {str(value).strip().upper() for value in universe_blacklist()}
WHITELIST = {str(value).strip().upper() for value in universe_whitelist()}


# =============================================================================
# DATA TYPES
# =============================================================================

@dataclass(frozen=True)
class SymbolRecord:
    symbol: str
    token: Optional[str]
    name: Optional[str]
    type: str
    exchange: Optional[str]
    segment: Optional[str]
    enabled: bool
    active: bool
    source_enabled: bool
    source_active: bool
    blacklisted: bool
    whitelisted: bool
    scanner_candidate_eligible: bool
    selected: bool
    cohort: str


@dataclass(frozen=True)
class MinuteBar:
    candle_time: datetime  # naive IST, matching DB convention
    open: float
    high: float
    low: float
    close: float
    volume: float
    oi: float


@dataclass(frozen=True)
class ThreeMinuteBar:
    candle_time: datetime  # naive IST bin start
    open: float
    high: float
    low: float
    close: float
    volume: float
    oi: float


# =============================================================================
# GENERIC HELPERS
# =============================================================================

def _norm(value: Any) -> str:
    return str(value or "").strip().upper()


def _float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        value = float(value)
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float, Decimal)):
        return bool(int(value))
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off", ""}:
        return False
    raise ValueError(f"Cannot parse boolean value: {value!r}")


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def _market_dt(day: date, value: time) -> datetime:
    return datetime.combine(day, value, IST)


def _db_market_dt(day: date, value: time) -> datetime:
    return datetime.combine(day, value)


def _pct(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator * 100.0


def _percentile_rank(rank: int, count: int) -> float:
    if count <= 0 or rank <= 0:
        return 0.0
    return round(100.0 * (count - rank + 1) / count, 4)


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(rows[0].keys()),
            extrasaction="ignore",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    step = max(1, int(size))
    for index in range(0, len(items), step):
        yield items[index : index + step]


# =============================================================================
# SELECTION / UNIVERSE SNAPSHOT
# =============================================================================

def _read_selection_csv(path: str) -> Dict[str, Dict[str, bool]]:
    if not path:
        raise ValueError("SELECTION_CSV_PATH is required when SELECTION_SOURCE='CSV'")

    csv_path = Path(path)
    if not csv_path.exists():
        raise FileNotFoundError(f"Selection CSV not found: {csv_path}")

    result: Dict[str, Dict[str, bool]] = {}
    with csv_path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"symbol", "enabled", "active"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"Selection CSV missing columns {sorted(missing)}; "
                f"available={reader.fieldnames}"
            )
        for row in reader:
            symbol = _norm(row["symbol"])
            if not symbol:
                continue
            result[symbol] = {
                "enabled": _bool(row["enabled"]),
                "active": _bool(row["active"]),
            }
    if not result:
        raise ValueError(f"Selection CSV has no usable rows: {csv_path}")
    return result


def _cohort(
    *,
    enabled: bool,
    active: bool,
    blacklisted: bool,
    has_token: bool,
) -> str:
    # Important: disabled symbols are never treated as selected even when an
    # old/stale active flag remains true.
    if not enabled:
        return "DISABLED_UNIVERSE"
    # The current selector is static for the day; enabled+active is therefore
    # the actual selected basket and remains the primary historical fact.
    if active:
        return "SELECTED_ACTIVE"
    if blacklisted:
        return "ENABLED_BLACKLISTED"
    if not has_token:
        return "ENABLED_NO_TOKEN"
    return "ELIGIBLE_NOT_SELECTED"


def _load_symbol_universe() -> List[SymbolRecord]:
    source = _norm(SELECTION_SOURCE)
    csv_flags = _read_selection_csv(SELECTION_CSV_PATH) if source == "CSV" else {}
    if source not in {"DATABASE_FLAGS", "CSV"}:
        raise ValueError(
            "SELECTION_SOURCE must be DATABASE_FLAGS or CSV; "
            f"received {SELECTION_SOURCE!r}"
        )

    with get_trades_db() as db:
        rows = (
            db.query(SymbolORM)
            .filter(SymbolORM.type == "EQ")
            .order_by(SymbolORM.symbol.asc())
            .all()
        )
        raw_rows = [
            {
                "symbol": _norm(row.symbol),
                "token": str(row.token).strip() if row.token not in (None, "") else None,
                "name": row.name,
                "type": _norm(row.type),
                "exchange": row.exchange,
                "segment": row.segment,
                "db_enabled": bool(row.enabled),
                "db_active": bool(row.active),
            }
            for row in rows
        ]

    records: List[SymbolRecord] = []
    for raw in raw_rows:
        symbol = raw["symbol"]
        if not symbol:
            continue
        if not INCLUDE_INDEX_SYMBOLS_IN_RANKING and symbol in INDEX_SYMBOLS:
            continue

        if source == "CSV":
            if symbol not in csv_flags:
                raise KeyError(
                    f"Symbol {symbol} is in DB but missing from selection CSV"
                )
            enabled = csv_flags[symbol]["enabled"]
            active = csv_flags[symbol]["active"]
        else:
            enabled = raw["db_enabled"]
            active = raw["db_active"]

        blacklisted = symbol in BLACKLIST
        whitelisted = symbol in WHITELIST
        has_token = raw["token"] is not None
        scanner_eligible = enabled and not blacklisted and has_token
        selected = enabled and active
        cohort = _cohort(
            enabled=enabled,
            active=active,
            blacklisted=blacklisted,
            has_token=has_token,
        )

        records.append(
            SymbolRecord(
                symbol=symbol,
                token=raw["token"],
                name=raw["name"],
                type=raw["type"],
                exchange=raw["exchange"],
                segment=raw["segment"],
                enabled=enabled,
                active=active,
                source_enabled=raw["db_enabled"],
                source_active=raw["db_active"],
                blacklisted=blacklisted,
                whitelisted=whitelisted,
                scanner_candidate_eligible=scanner_eligible,
                selected=selected,
                cohort=cohort,
            )
        )

    if not records:
        raise RuntimeError("No EQ symbols found for StockScan performance audit")
    return records


# =============================================================================
# BROKER / CANDLES
# =============================================================================

def _kite() -> KiteConnectService:
    user = UserSchema.fetch_user(AppConfig.DATA_USER)
    if not user:
        raise RuntimeError(f"DATA_USER not found: {AppConfig.DATA_USER}")
    if not user.apikey or not user.access_token:
        raise RuntimeError(
            f"DATA_USER missing apikey/access_token: {AppConfig.DATA_USER}"
        )
    return KiteConnectService(api_key=user.apikey, access_token=user.access_token)


def _parse_minute_bars(raw_bars: Sequence[Mapping[str, Any]]) -> List[MinuteBar]:
    start = _db_market_dt(TEST_DATE, MARKET_OPEN_TIME)
    end = _db_market_dt(TEST_DATE, MARKET_CLOSE_TIME)
    parsed: List[MinuteBar] = []

    for raw in raw_bars:
        candle_time = to_ist_naive(raw.get("date"))
        if candle_time is None or candle_time < start or candle_time >= end:
            continue

        o = _float(raw.get("open"))
        h = _float(raw.get("high"))
        l = _float(raw.get("low"))
        c = _float(raw.get("close"))
        v = _float(raw.get("volume"))
        oi = _float(raw.get("oi"))
        if None in (o, h, l, c) or min(o, h, l, c) <= 0:
            raise ValueError(
                f"Invalid OHLC at {candle_time}: "
                f"open={o} high={h} low={l} close={c}"
            )
        if h < max(o, c) or l > min(o, c) or h < l:
            raise ValueError(
                f"Inconsistent OHLC at {candle_time}: "
                f"open={o} high={h} low={l} close={c}"
            )

        parsed.append(
            MinuteBar(
                candle_time=candle_time,
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
                volume=float(v or 0.0),
                oi=float(oi or 0.0),
            )
        )

    parsed.sort(key=lambda bar: bar.candle_time)

    # Keep one record per timestamp.  Exact duplicates are harmless; conflicting
    # duplicate timestamps fail loudly because the source is ambiguous.
    deduplicated: List[MinuteBar] = []
    by_time: Dict[datetime, MinuteBar] = {}
    for bar in parsed:
        previous = by_time.get(bar.candle_time)
        if previous is not None and previous != bar:
            raise ValueError(f"Conflicting minute bars at {bar.candle_time}")
        by_time[bar.candle_time] = bar
    deduplicated.extend(by_time[key] for key in sorted(by_time))
    return deduplicated


def _fetch_minute_bars(
    kite: KiteConnectService,
    symbol: SymbolRecord,
) -> List[MinuteBar]:
    if not symbol.token:
        raise ValueError(f"Missing instrument token for {symbol.symbol}")
    raw = kite.fetch_historical_data(
        instrument_token=int(symbol.token),
        from_date=_market_dt(TEST_DATE, MARKET_OPEN_TIME),
        # Add one minute so the broker includes the final 15:29 candle.
        to_date=_market_dt(TEST_DATE, MARKET_CLOSE_TIME) + timedelta(minutes=1),
        interval=BROKER_INTERVAL,
        oi=False,
    ) or []
    return _parse_minute_bars(raw)


def _three_minute_bin_start(candle_time: datetime) -> datetime:
    session_start = _db_market_dt(TEST_DATE, MARKET_OPEN_TIME)
    minutes = int((candle_time - session_start).total_seconds() // 60)
    if minutes < 0:
        raise ValueError(f"Bar is before market open: {candle_time}")
    return session_start + timedelta(minutes=(minutes // 3) * 3)


def _aggregate_three_minute(bars: Sequence[MinuteBar]) -> List[ThreeMinuteBar]:
    grouped: Dict[datetime, List[MinuteBar]] = {}
    for bar in bars:
        grouped.setdefault(_three_minute_bin_start(bar.candle_time), []).append(bar)

    result: List[ThreeMinuteBar] = []
    for bin_start in sorted(grouped):
        group = sorted(grouped[bin_start], key=lambda bar: bar.candle_time)
        result.append(
            ThreeMinuteBar(
                candle_time=bin_start,
                open=group[0].open,
                high=max(bar.high for bar in group),
                low=min(bar.low for bar in group),
                close=group[-1].close,
                volume=sum(bar.volume for bar in group),
                oi=group[-1].oi,
            )
        )
    return result


def _persist_three_minute_bars(
    symbol: str,
    bars: Sequence[ThreeMinuteBar],
) -> Dict[str, int]:
    if not PERSIST_THREE_MINUTE_CANDLES:
        return {"existing": 0, "inserted": 0, "updated": 0}

    start = _db_market_dt(TEST_DATE, MARKET_OPEN_TIME)
    end = _db_market_dt(TEST_DATE, MARKET_CLOSE_TIME)

    with get_trades_db() as db:
        existing_rows = (
            db.query(CandleORM)
            .filter(
                CandleORM.symbol == symbol,
                CandleORM.frequency == THREE_MINUTE_FREQUENCY,
                CandleORM.candle_time >= start,
                CandleORM.candle_time < end,
            )
            .all()
        )
        existing = {row.candle_time: row for row in existing_rows}
        inserted = 0
        updated = 0

        for bar in bars:
            row = existing.get(bar.candle_time)
            if row is None:
                db.add(
                    CandleORM(
                        symbol=symbol,
                        frequency=THREE_MINUTE_FREQUENCY,
                        candle_time=bar.candle_time,
                        open=bar.open,
                        high=bar.high,
                        low=bar.low,
                        close=bar.close,
                        volume=bar.volume,
                        oi=bar.oi,
                        active=True,
                    )
                )
                inserted += 1
            elif OVERWRITE_EXISTING_THREE_MINUTE_CANDLES:
                row.open = bar.open
                row.high = bar.high
                row.low = bar.low
                row.close = bar.close
                row.volume = bar.volume
                row.oi = bar.oi
                row.active = True
                updated += 1

        db.commit()
        return {
            "existing": len(existing_rows),
            "inserted": inserted,
            "updated": updated,
        }


# =============================================================================
# MOVEMENT OUTCOME
# =============================================================================

def _ordered_move(
    anchor_price: float,
    anchor_time: datetime,
    post_bars: Sequence[MinuteBar],
) -> Dict[str, Any]:
    min_price = anchor_price
    min_time = anchor_time
    max_price = anchor_price
    max_time = anchor_time

    best_up_pct = 0.0
    best_up_start = anchor_time
    best_up_end = anchor_time
    best_down_pct = 0.0
    best_down_start = anchor_time
    best_down_end = anchor_time

    for bar in post_bars:
        # Use only extrema known before the current bar.  This avoids assuming
        # whether this minute's high or low happened first.
        up_pct = _pct(bar.high - min_price, min_price)
        if up_pct > best_up_pct:
            best_up_pct = up_pct
            best_up_start = min_time
            best_up_end = bar.candle_time

        down_pct = _pct(max_price - bar.low, max_price)
        if down_pct > best_down_pct:
            best_down_pct = down_pct
            best_down_start = max_time
            best_down_end = bar.candle_time

        if bar.low < min_price:
            min_price = bar.low
            min_time = bar.candle_time
        if bar.high > max_price:
            max_price = bar.high
            max_time = bar.candle_time

    if best_up_pct >= best_down_pct:
        direction = "UP"
        best_pct = best_up_pct
        move_start = best_up_start
        move_end = best_up_end
    else:
        direction = "DOWN"
        best_pct = best_down_pct
        move_start = best_down_start
        move_end = best_down_end

    return {
        "best_up_move_pct": round(best_up_pct, 6),
        "best_down_move_pct": round(best_down_pct, 6),
        "best_ordered_move_pct": round(best_pct, 6),
        "best_ordered_move_direction": direction,
        "best_ordered_move_start": move_start,
        "best_ordered_move_end": move_end,
    }


def _outcome(
    symbol: SymbolRecord,
    bars: Sequence[MinuteBar],
    three_minute_count: int,
    persistence: Mapping[str, int],
) -> Dict[str, Any]:
    if len(bars) < MIN_MINUTE_BARS_FOR_EVALUATION:
        raise ValueError(
            f"Insufficient minute bars for {symbol.symbol}: "
            f"{len(bars)} < {MIN_MINUTE_BARS_FOR_EVALUATION}"
        )

    market_open = _db_market_dt(TEST_DATE, MARKET_OPEN_TIME)
    selection_dt = _db_market_dt(TEST_DATE, SELECTION_TIME)

    first = next((bar for bar in bars if bar.candle_time == market_open), None)
    if first is None:
        raise ValueError(
            f"Missing exact first minute candle for {symbol.symbol} at {market_open}"
        )

    post = [bar for bar in bars if bar.candle_time >= selection_dt]
    if not post:
        raise ValueError(f"No post-selection bars for {symbol.symbol}")

    day_open = first.open
    day_high = max(bar.high for bar in bars)
    day_low = min(bar.low for bar in bars)
    day_close = bars[-1].close
    day_volume = sum(bar.volume for bar in bars)
    anchor = first.close

    post_high_bar = max(post, key=lambda bar: bar.high)
    post_low_bar = min(post, key=lambda bar: bar.low)
    post_up_pct = _pct(post_high_bar.high - anchor, anchor)
    post_down_pct = _pct(anchor - post_low_bar.low, anchor)
    post_excursion_pct = max(post_up_pct, post_down_pct)

    ordered = _ordered_move(anchor, selection_dt, post)

    close_path = [anchor] + [bar.close for bar in post]
    total_close_path = sum(
        abs(close_path[index] - close_path[index - 1])
        for index in range(1, len(close_path))
    )
    path_efficiency = (
        abs(close_path[-1] - close_path[0]) / total_close_path
        if total_close_path > 0
        else 0.0
    )

    opening_move_pct = _pct(abs(first.close - first.open), first.open)
    opening_range_pct = _pct(first.high - first.low, first.open)
    day_range_pct = _pct(day_high - day_low, day_open)
    day_body_pct = _pct(abs(day_close - day_open), day_open)
    close_from_anchor_pct = _pct(abs(day_close - anchor), anchor)

    result: Dict[str, Any] = {
        "symbol": symbol.symbol,
        "name": symbol.name,
        "exchange": symbol.exchange,
        "segment": symbol.segment,
        "enabled": symbol.enabled,
        "active": symbol.active,
        "selected": symbol.selected,
        "cohort": symbol.cohort,
        "blacklisted": symbol.blacklisted,
        "whitelisted": symbol.whitelisted,
        "scanner_candidate_eligible": symbol.scanner_candidate_eligible,
        "token_present": bool(symbol.token),
        "selection_source": SELECTION_SOURCE,
        "minute_bar_count": len(bars),
        "minute_data_complete": len(bars) >= EXPECTED_MINUTE_BARS,
        "three_minute_bar_count": int(three_minute_count),
        "three_minute_expected_count": EXPECTED_THREE_MINUTE_BARS,
        "three_minute_existing_before": int(persistence.get("existing", 0)),
        "three_minute_inserted": int(persistence.get("inserted", 0)),
        "three_minute_updated": int(persistence.get("updated", 0)),
        "first_minute_time": first.candle_time,
        "first_minute_open": round(first.open, 6),
        "first_minute_high": round(first.high, 6),
        "first_minute_low": round(first.low, 6),
        "first_minute_close": round(first.close, 6),
        "first_minute_volume": round(first.volume, 6),
        "opening_1m_body_pct": round(opening_move_pct, 6),
        "opening_1m_range_pct": round(opening_range_pct, 6),
        "selection_anchor_price": round(anchor, 6),
        "day_open": round(day_open, 6),
        "day_high": round(day_high, 6),
        "day_low": round(day_low, 6),
        "day_close": round(day_close, 6),
        "day_volume": round(day_volume, 6),
        "day_range_pct": round(day_range_pct, 6),
        "abs_day_body_pct": round(day_body_pct, 6),
        "abs_close_from_0916_pct": round(close_from_anchor_pct, 6),
        "post_0916_high": round(post_high_bar.high, 6),
        "post_0916_high_time": post_high_bar.candle_time,
        "post_0916_low": round(post_low_bar.low, 6),
        "post_0916_low_time": post_low_bar.candle_time,
        "post_0916_up_from_anchor_pct": round(post_up_pct, 6),
        "post_0916_down_from_anchor_pct": round(post_down_pct, 6),
        "post_0916_best_excursion_pct": round(post_excursion_pct, 6),
        "close_path_efficiency": round(path_efficiency, 6),
    }
    result.update(ordered)
    result["movement_score"] = result[PRIMARY_RANK_FIELD]
    result["good_mover"] = result["movement_score"] >= GOOD_MOVER_MIN_PCT
    return result


# =============================================================================
# RANKING / ANALYSIS
# =============================================================================

def _assign_ranks(rows: List[Dict[str, Any]]) -> None:
    rows.sort(
        key=lambda row: (
            -float(row[PRIMARY_RANK_FIELD]),
            -float(row["day_range_pct"]),
            row["symbol"],
        )
    )

    total = len(rows)
    for index, row in enumerate(rows, start=1):
        row["overall_movement_rank"] = index
        row["overall_movement_percentile"] = _percentile_rank(index, total)

    enabled = [row for row in rows if row["enabled"]]
    enabled.sort(
        key=lambda row: (
            -float(row[PRIMARY_RANK_FIELD]),
            -float(row["day_range_pct"]),
            row["symbol"],
        )
    )
    for index, row in enumerate(enabled, start=1):
        row["enabled_universe_rank"] = index
        row["enabled_universe_percentile"] = _percentile_rank(index, len(enabled))

    candidates = [row for row in rows if row["scanner_candidate_eligible"]]
    candidates.sort(
        key=lambda row: (
            -float(row[PRIMARY_RANK_FIELD]),
            -float(row["day_range_pct"]),
            row["symbol"],
        )
    )
    for index, row in enumerate(candidates, start=1):
        row["scanner_candidate_rank"] = index
        row["scanner_candidate_percentile"] = _percentile_rank(
            index, len(candidates)
        )

    selected = [row for row in rows if row["selected"]]
    selected.sort(
        key=lambda row: (
            -float(row[PRIMARY_RANK_FIELD]),
            -float(row["day_range_pct"]),
            row["symbol"],
        )
    )
    for index, row in enumerate(selected, start=1):
        row["selected_basket_rank"] = index

    for row in rows:
        row.setdefault("enabled_universe_rank", "")
        row.setdefault("enabled_universe_percentile", "")
        row.setdefault("scanner_candidate_rank", "")
        row.setdefault("scanner_candidate_percentile", "")
        row.setdefault("selected_basket_rank", "")


def _scope_rows(rows: Sequence[Dict[str, Any]], scope: str) -> List[Dict[str, Any]]:
    if scope == "FULL_EQ_UNIVERSE":
        return list(rows)
    if scope == "ENABLED_EQ_UNIVERSE":
        return [row for row in rows if row["enabled"]]
    if scope == "SCANNER_CANDIDATE_UNIVERSE":
        return [row for row in rows if row["scanner_candidate_eligible"]]
    raise ValueError(f"Unknown scope: {scope}")


def _capture_rows(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    selected_count = sum(bool(row["selected"]) for row in rows)
    k_values = sorted(set(TOP_K_VALUES + ((selected_count,) if selected_count else ())))
    output: List[Dict[str, Any]] = []

    for scope in (
        "FULL_EQ_UNIVERSE",
        "ENABLED_EQ_UNIVERSE",
        "SCANNER_CANDIDATE_UNIVERSE",
    ):
        population = sorted(
            _scope_rows(rows, scope),
            key=lambda row: (
                -float(row[PRIMARY_RANK_FIELD]),
                -float(row["day_range_pct"]),
                row["symbol"],
            ),
        )
        for requested_k in k_values:
            actual_k = min(requested_k, len(population))
            if actual_k <= 0:
                continue
            top = population[:actual_k]
            selected = [row for row in top if row["selected"]]
            eligible_missed = [
                row for row in top if row["cohort"] == "ELIGIBLE_NOT_SELECTED"
            ]
            disabled = [
                row for row in top if row["cohort"] == "DISABLED_UNIVERSE"
            ]
            policy_excluded = [
                row
                for row in top
                if row["cohort"] in {"ENABLED_BLACKLISTED", "ENABLED_NO_TOKEN"}
            ]
            total_move = sum(float(row[PRIMARY_RANK_FIELD]) for row in top)
            selected_move = sum(float(row[PRIMARY_RANK_FIELD]) for row in selected)

            output.append(
                {
                    "scope": scope,
                    "requested_top_k": requested_k,
                    "actual_top_k": actual_k,
                    "population_size": len(population),
                    "selected_count_in_top_k": len(selected),
                    "capture_rate_pct": round(100.0 * len(selected) / actual_k, 4),
                    "eligible_not_selected_in_top_k": len(eligible_missed),
                    "disabled_in_top_k": len(disabled),
                    "policy_excluded_in_top_k": len(policy_excluded),
                    "selected_movement_share_pct": round(
                        100.0 * selected_move / total_move, 4
                    )
                    if total_move > 0
                    else 0.0,
                    "selected_symbols": "|".join(row["symbol"] for row in selected),
                    "eligible_missed_symbols": "|".join(
                        row["symbol"] for row in eligible_missed
                    ),
                    "disabled_symbols": "|".join(row["symbol"] for row in disabled),
                    "policy_excluded_symbols": "|".join(
                        row["symbol"] for row in policy_excluded
                    ),
                }
            )
    return output


def _replacement_rows(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    selected = [row for row in rows if row["selected"]]
    capacity = len(selected)
    if capacity <= 0:
        return []

    candidate_universe = sorted(
        [row for row in rows if row["scanner_candidate_eligible"]],
        key=lambda row: (
            -float(row[PRIMARY_RANK_FIELD]),
            -float(row["day_range_pct"]),
            row["symbol"],
        ),
    )
    objective_top = candidate_universe[:capacity]
    objective_top_symbols = {row["symbol"] for row in objective_top}

    missed = [row for row in objective_top if not row["selected"]]
    weak_selected = sorted(
        [row for row in selected if row["symbol"] not in objective_top_symbols],
        key=lambda row: (
            float(row[PRIMARY_RANK_FIELD]),
            float(row["day_range_pct"]),
            row["symbol"],
        ),
    )
    missed.sort(
        key=lambda row: (
            -float(row[PRIMARY_RANK_FIELD]),
            -float(row["day_range_pct"]),
            row["symbol"],
        )
    )

    output: List[Dict[str, Any]] = []
    for index in range(max(len(missed), len(weak_selected))):
        incoming = missed[index] if index < len(missed) else None
        outgoing = weak_selected[index] if index < len(weak_selected) else None
        output.append(
            {
                "pair_number": index + 1,
                "missed_symbol": incoming["symbol"] if incoming else "",
                "missed_candidate_rank": incoming["scanner_candidate_rank"]
                if incoming
                else "",
                "missed_move_pct": incoming[PRIMARY_RANK_FIELD] if incoming else "",
                "missed_direction": incoming["best_ordered_move_direction"]
                if incoming
                else "",
                "weak_selected_symbol": outgoing["symbol"] if outgoing else "",
                "weak_selected_candidate_rank": outgoing["scanner_candidate_rank"]
                if outgoing
                else "",
                "weak_selected_move_pct": outgoing[PRIMARY_RANK_FIELD]
                if outgoing
                else "",
                "movement_advantage_pct_points": round(
                    float(incoming[PRIMARY_RANK_FIELD])
                    - float(outgoing[PRIMARY_RANK_FIELD]),
                    6,
                )
                if incoming and outgoing
                else "",
            }
        )
    return output


def _distribution(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    values = [float(row[PRIMARY_RANK_FIELD]) for row in rows]
    if not values:
        return {
            "count": 0,
            "mean_move_pct": None,
            "median_move_pct": None,
            "p75_move_pct": None,
            "max_move_pct": None,
            "good_movers": 0,
        }
    values_sorted = sorted(values)
    p75_index = min(len(values_sorted) - 1, math.ceil(0.75 * len(values_sorted)) - 1)
    return {
        "count": len(values),
        "mean_move_pct": round(statistics.fmean(values), 6),
        "median_move_pct": round(statistics.median(values), 6),
        "p75_move_pct": round(values_sorted[p75_index], 6),
        "max_move_pct": round(max(values), 6),
        "good_movers": sum(value >= GOOD_MOVER_MIN_PCT for value in values),
    }


# =============================================================================
# REPORTS
# =============================================================================

def _report_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    ordered_fields = (
        "overall_movement_rank",
        "overall_movement_percentile",
        "enabled_universe_rank",
        "enabled_universe_percentile",
        "scanner_candidate_rank",
        "scanner_candidate_percentile",
        "selected_basket_rank",
        "symbol",
        "name",
        "cohort",
        "enabled",
        "active",
        "selected",
        "scanner_candidate_eligible",
        "blacklisted",
        "whitelisted",
        "movement_score",
        "best_ordered_move_pct",
        "best_ordered_move_direction",
        "best_ordered_move_start",
        "best_ordered_move_end",
        "best_up_move_pct",
        "best_down_move_pct",
        "post_0916_best_excursion_pct",
        "post_0916_up_from_anchor_pct",
        "post_0916_down_from_anchor_pct",
        "day_range_pct",
        "abs_day_body_pct",
        "abs_close_from_0916_pct",
        "close_path_efficiency",
        "good_mover",
        "selection_anchor_price",
        "first_minute_time",
        "first_minute_open",
        "first_minute_high",
        "first_minute_low",
        "first_minute_close",
        "first_minute_volume",
        "opening_1m_body_pct",
        "opening_1m_range_pct",
        "day_open",
        "day_high",
        "day_low",
        "day_close",
        "day_volume",
        "post_0916_high",
        "post_0916_high_time",
        "post_0916_low",
        "post_0916_low_time",
        "minute_bar_count",
        "minute_data_complete",
        "three_minute_bar_count",
        "three_minute_expected_count",
        "three_minute_existing_before",
        "three_minute_inserted",
        "three_minute_updated",
        "exchange",
        "segment",
        "token_present",
        "selection_source",
    )
    return {field: row.get(field, "") for field in ordered_fields}


def _write_reports(
    rows: List[Dict[str, Any]],
    errors: List[Dict[str, Any]],
    universe: Sequence[SymbolRecord],
    started_at: datetime,
) -> Dict[str, Path]:
    stamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
    stem = REPORT_DIR / f"{REPORT_PREFIX}_{TEST_DATE.isoformat()}_{stamp}"
    paths = {
        "ranked": stem.with_name(stem.name + "_ranked.csv"),
        "selected": stem.with_name(stem.name + "_selected.csv"),
        "missed": stem.with_name(stem.name + "_eligible_missed_top_movers.csv"),
        "full_universe_missed": stem.with_name(
            stem.name + "_full_universe_missed_top_movers.csv"
        ),
        "disabled_movers": stem.with_name(stem.name + "_disabled_movers.csv"),
        "weak_selected": stem.with_name(stem.name + "_weak_selected.csv"),
        "replacements": stem.with_name(stem.name + "_replacement_pairs.csv"),
        "capture": stem.with_name(stem.name + "_capture.csv"),
        "candle_completeness": stem.with_name(
            stem.name + "_candle_completeness.csv"
        ),
        "selection_manifest": stem.with_name(
            stem.name + "_selection_manifest.csv"
        ),
        "errors": stem.with_name(stem.name + "_errors.csv"),
        "summary": stem.with_name(stem.name + "_summary.json"),
    }

    ranked_rows = [_report_row(row) for row in rows]
    _write_csv(paths["ranked"], ranked_rows)

    selected = [row for row in rows if row["selected"]]
    selected.sort(key=lambda row: int(row["overall_movement_rank"]))
    _write_csv(paths["selected"], [_report_row(row) for row in selected])

    selected_count = len(selected)
    objective_top_symbols = {
        row["symbol"]
        for row in sorted(
            [row for row in rows if row["scanner_candidate_eligible"]],
            key=lambda row: (
                -float(row[PRIMARY_RANK_FIELD]),
                -float(row["day_range_pct"]),
                row["symbol"],
            ),
        )[:selected_count]
    }
    missed = [
        row
        for row in rows
        if row["symbol"] in objective_top_symbols and not row["selected"]
    ]
    missed.sort(key=lambda row: int(row["scanner_candidate_rank"]))
    _write_csv(paths["missed"], [_report_row(row) for row in missed])

    # Independent full-universe comparison requested by the audit: rank every
    # equity first, then overlay the morning selection.  A disabled symbol is
    # reported as a system-level universe miss, not as a StockScan ranking miss.
    full_objective_top = sorted(
        rows,
        key=lambda row: (
            -float(row[PRIMARY_RANK_FIELD]),
            -float(row["day_range_pct"]),
            row["symbol"],
        ),
    )[:selected_count]
    full_universe_missed = [row for row in full_objective_top if not row["selected"]]
    full_universe_missed.sort(key=lambda row: int(row["overall_movement_rank"]))
    _write_csv(
        paths["full_universe_missed"],
        [_report_row(row) for row in full_universe_missed],
    )

    disabled_movers = [
        row
        for row in rows
        if row["cohort"] == "DISABLED_UNIVERSE" and row["good_mover"]
    ]
    disabled_movers.sort(key=lambda row: int(row["overall_movement_rank"]))
    _write_csv(
        paths["disabled_movers"],
        [_report_row(row) for row in disabled_movers],
    )

    weak_selected = [
        row
        for row in rows
        if row["selected"] and row["symbol"] not in objective_top_symbols
    ]
    weak_selected.sort(
        key=lambda row: (
            float(row[PRIMARY_RANK_FIELD]),
            row["symbol"],
        )
    )
    _write_csv(
        paths["weak_selected"],
        [_report_row(row) for row in weak_selected],
    )

    replacements = _replacement_rows(rows)
    _write_csv(paths["replacements"], replacements)

    capture = _capture_rows(rows)
    _write_csv(paths["capture"], capture)

    completeness = [
        {
            "symbol": row["symbol"],
            "cohort": row["cohort"],
            "selected": row["selected"],
            "minute_bar_count": row["minute_bar_count"],
            "minute_data_complete": row["minute_data_complete"],
            "three_minute_bar_count": row["three_minute_bar_count"],
            "three_minute_expected_count": row["three_minute_expected_count"],
            "three_minute_existing_before": row["three_minute_existing_before"],
            "three_minute_inserted": row["three_minute_inserted"],
            "three_minute_updated": row["three_minute_updated"],
        }
        for row in sorted(rows, key=lambda item: item["symbol"])
    ]
    _write_csv(paths["candle_completeness"], completeness)

    manifest = [asdict(record) for record in universe]
    _write_csv(paths["selection_manifest"], manifest)
    _write_csv(paths["errors"], errors)

    config = {
        "test_date": TEST_DATE,
        "market_open_time": MARKET_OPEN_TIME,
        "selection_time": SELECTION_TIME,
        "market_close_time": MARKET_CLOSE_TIME,
        "selection_source": SELECTION_SOURCE,
        "selection_csv_path": SELECTION_CSV_PATH,
        "include_index_symbols_in_ranking": INCLUDE_INDEX_SYMBOLS_IN_RANKING,
        "index_symbols": INDEX_SYMBOLS,
        "broker_interval": BROKER_INTERVAL,
        "persist_three_minute_candles": PERSIST_THREE_MINUTE_CANDLES,
        "three_minute_frequency": THREE_MINUTE_FREQUENCY,
        "overwrite_existing_three_minute_candles": (
            OVERWRITE_EXISTING_THREE_MINUTE_CANDLES
        ),
        "primary_rank_field": PRIMARY_RANK_FIELD,
        "good_mover_min_pct": GOOD_MOVER_MIN_PCT,
        "top_k_values": TOP_K_VALUES,
    }
    config_hash = hashlib.sha256(
        json.dumps(
            config,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        ).encode("utf-8")
    ).hexdigest()[:16]

    by_cohort = {
        cohort: _distribution([row for row in rows if row["cohort"] == cohort])
        for cohort in sorted({row["cohort"] for row in rows})
    }
    selected_distribution = _distribution(selected)
    unselected_candidates = [
        row
        for row in rows
        if row["scanner_candidate_eligible"] and not row["selected"]
    ]

    summary = {
        "program": "test_stockscan_performance.py",
        "test_date": TEST_DATE,
        "started_at": started_at,
        "completed_at": datetime.now(IST),
        "config_hash": config_hash,
        "config": config,
        "read_only_except_missing_candle_backfill": True,
        "selection_interpretation": (
            "selected = enabled AND active; scanner eligibility is reported separately"
        ),
        "ranking_interpretation": (
            "largest chronologically valid post-09:16 ordered move"
        ),
        "universe_rows": len(universe),
        "successfully_ranked": len(rows),
        "symbol_errors": len(errors),
        "selected_count": len(selected),
        "scanner_candidate_count": sum(
            bool(row["scanner_candidate_eligible"]) for row in rows
        ),
        "enabled_count": sum(bool(row["enabled"]) for row in rows),
        "disabled_count": sum(not bool(row["enabled"]) for row in rows),
        "selected_distribution": selected_distribution,
        "eligible_not_selected_distribution": _distribution(unselected_candidates),
        "cohort_distributions": by_cohort,
        "scanner_candidate_top_capacity_overlap": {
            "capacity": selected_count,
            "selected_in_objective_top": selected_count - len(missed),
            "missed_from_objective_top": len(missed),
            "overlap_rate_pct": round(
                100.0 * (selected_count - len(missed)) / selected_count, 4
            )
            if selected_count
            else None,
            "missed_symbols": [row["symbol"] for row in missed],
            "weak_selected_symbols": [row["symbol"] for row in weak_selected],
        },
        "full_universe_top_capacity_overlap": {
            "capacity": selected_count,
            "selected_in_objective_top": selected_count - len(full_universe_missed),
            "missed_from_objective_top": len(full_universe_missed),
            "overlap_rate_pct": round(
                100.0 * (selected_count - len(full_universe_missed)) / selected_count, 4
            )
            if selected_count
            else None,
            "missed_symbols": [row["symbol"] for row in full_universe_missed],
            "disabled_movers_above_threshold": len(disabled_movers),
        },
        "capture": capture,
        "output_files": paths,
    }
    paths["summary"].write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=_json_default),
        encoding="utf-8",
    )
    return paths


# =============================================================================
# DRIVER
# =============================================================================

def run() -> int:
    started_at = datetime.now(IST)
    universe = _load_symbol_universe()
    selected_count = sum(record.selected for record in universe)

    logger.info(
        "StockScan EOD audit input | date=%s universe=%d selected=%d "
        "enabled=%d disabled=%d selection_source=%s",
        TEST_DATE,
        len(universe),
        selected_count,
        sum(record.enabled for record in universe),
        sum(not record.enabled for record in universe),
        SELECTION_SOURCE,
    )

    if TEST_DATE != datetime.now(IST).date() and SELECTION_SOURCE == "DATABASE_FLAGS":
        logger.warning(
            "Historical date %s is being evaluated using current DB flags. "
            "This is valid only if enabled/active still represent that day's "
            "static morning selection. Use SELECTION_SOURCE='CSV' otherwise.",
            TEST_DATE,
        )

    kite = _kite() if FETCH_MINUTE_HISTORY_FROM_BROKER else None
    outcomes: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for index, symbol in enumerate(universe, start=1):
        try:
            if not symbol.token:
                raise ValueError("Missing instrument token")
            if kite is None:
                raise RuntimeError(
                    "FETCH_MINUTE_HISTORY_FROM_BROKER=False is not supported "
                    "for the exact 09:16 outcome audit"
                )

            bars = _fetch_minute_bars(kite, symbol)
            three_minute = _aggregate_three_minute(bars)

            persistence = {"existing": 0, "inserted": 0, "updated": 0}
            try:
                persistence = _persist_three_minute_bars(
                    symbol.symbol,
                    three_minute,
                )
            except Exception as persist_error:
                # Candle persistence failure must not suppress the movement
                # evaluation because the broker data is already available.
                logger.exception(
                    "3-minute candle persistence failed; continuing analysis | "
                    "symbol=%s",
                    symbol.symbol,
                )
                errors.append(
                    {
                        "symbol": symbol.symbol,
                        "stage": "PERSIST_THREE_MINUTE_CANDLES",
                        "error_type": type(persist_error).__name__,
                        "error": str(persist_error),
                        "traceback": traceback.format_exc(),
                    }
                )

            outcome = _outcome(
                symbol,
                bars,
                len(three_minute),
                persistence,
            )
            outcomes.append(outcome)

            logger.info(
                "Evaluated %d/%d | symbol=%s cohort=%s selected=%s "
                "minute_bars=%d move=%.3f%% direction=%s inserted_3m=%d",
                index,
                len(universe),
                symbol.symbol,
                symbol.cohort,
                symbol.selected,
                len(bars),
                outcome[PRIMARY_RANK_FIELD],
                outcome["best_ordered_move_direction"],
                persistence.get("inserted", 0),
            )

            if BROKER_REQUEST_SLEEP_SEC > 0:
                time_module.sleep(BROKER_REQUEST_SLEEP_SEC)

        except Exception as exc:
            logger.exception(
                "Per-symbol StockScan performance evaluation failed; "
                "continuing | symbol=%s cohort=%s",
                symbol.symbol,
                symbol.cohort,
            )
            errors.append(
                {
                    "symbol": symbol.symbol,
                    "stage": "FETCH_OR_EVALUATE",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }
            )

    if not outcomes:
        raise RuntimeError("No symbols were successfully evaluated")

    _assign_ranks(outcomes)
    # Restore primary overall order after assigning scope-specific ranks.
    outcomes.sort(key=lambda row: int(row["overall_movement_rank"]))

    paths = _write_reports(outcomes, errors, universe, started_at)

    selected = [row for row in outcomes if row["selected"]]
    candidates = [row for row in outcomes if row["scanner_candidate_eligible"]]
    selected_count = len(selected)
    objective_top = sorted(
        candidates,
        key=lambda row: (
            -float(row[PRIMARY_RANK_FIELD]),
            -float(row["day_range_pct"]),
            row["symbol"],
        ),
    )[:selected_count]
    overlap = sum(row["selected"] for row in objective_top)

    logger.info(
        "StockScan EOD audit complete | ranked=%d errors=%d selected=%d "
        "candidate_universe=%d objective_top_overlap=%d/%d",
        len(outcomes),
        len(errors),
        selected_count,
        len(candidates),
        overlap,
        selected_count,
    )
    for name, path in paths.items():
        logger.info("Report | %s=%s", name, path)

    print("StockScan end-of-day performance audit complete")
    print(f"  date={TEST_DATE.isoformat()}")
    print(f"  ranked_symbols={len(outcomes)}")
    print(f"  symbol_errors={len(errors)}")
    print(f"  selected_equities={selected_count}")
    print(f"  scanner_candidate_universe={len(candidates)}")
    overlap_pct = (100.0 * overlap / selected_count) if selected_count else 0.0
    print(
        "  objective_top_capacity_overlap="
        f"{overlap}/{selected_count} ({overlap_pct:.2f}%)"
    )
    print(f"  ranked_report={paths['ranked']}")
    print(f"  capture_report={paths['capture']}")
    print(f"  missed_report={paths['missed']}")
    print(f"  summary={paths['summary']}")

    return 2 if errors and FAIL_IF_SYMBOL_ERRORS else 0


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    setup_logging(log_file=str(LOG_FILE))

    global logger
    logger = logging.getLogger(__name__)
    logger.info(
        "Starting test_stockscan_performance | date=%s selection_time=%s "
        "primary_rank=%s persist_3m=%s",
        TEST_DATE,
        SELECTION_TIME,
        PRIMARY_RANK_FIELD,
        PERSIST_THREE_MINUTE_CANDLES,
    )

    try:
        return run()
    except KeyboardInterrupt:
        logger.info("test_stockscan_performance interrupted")
        return 130
    except Exception:
        logger.exception(
            "test_stockscan_performance failed during startup/preflight"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
