#!/usr/bin/env python3
"""15-minute StockMap generator.

The operational pattern intentionally mirrors Snapshot:

* strict schema
* one persisted state per completed cadence
* previous persisted state + new candle continuity
* explicit historical bootstrap only when no valid state exists
* per-candle causal replay for missed cadences and research days

StockMap is context only. It does not create signals or make ALLOW/DEFER decisions.
"""

from __future__ import annotations

import logging
import math
import os
import sys
import time
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from configs.stockmap_config import STOCKMAP_CONFIG
from schemas.stockmap import StockMapSchema
from services.stockmap.stockmap_helper import (
    compute_initial_accepted_range,
    compute_prev_day_ohlc,
    compute_structure_state_from_memory,
)

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 15)
TF_MINUTES = int(STOCKMAP_CONFIG.service.tick_minutes)

STRUCTURE_STATE_KEYS = (
    "hma.state",
    "vwap.side",
    "structure.accepted",
    "structure.raw.side",
    "structure.candidate",
    "structure.raw.state",
    "structure.session_phase",
    "structure.accepted.state",
    "structure.candidate.active",
)


def _as_ist(value: Any) -> datetime:
    ts = pd.to_datetime(value)
    if pd.isna(ts):
        raise ValueError("StockMap timestamp cannot be null")
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.tz_localize(IST)
    else:
        ts = ts.tz_convert(IST)
    return ts.to_pydatetime()


def _naive_ist(value: datetime) -> datetime:
    value = _as_ist(value)
    return value.replace(tzinfo=None)


def _normalise_candles(records: Sequence[Dict[str, Any]] | pd.DataFrame) -> pd.DataFrame:
    if isinstance(records, pd.DataFrame):
        d = records.copy()
    else:
        d = pd.DataFrame(list(records or []))
    if d.empty:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

    required = {"date", "open", "high", "low", "close", "volume"}
    missing = required.difference(d.columns)
    if missing:
        raise ValueError(f"Historical candle fields are missing: {sorted(missing)}")

    d = d[list(required)].copy()
    d["date"] = pd.to_datetime(d["date"])
    if d["date"].dt.tz is None:
        d["date"] = d["date"].dt.tz_localize(IST)
    else:
        d["date"] = d["date"].dt.tz_convert(IST)

    for column in ("open", "high", "low", "close", "volume"):
        d[column] = pd.to_numeric(d[column], errors="raise").astype(float)

    d = (
        d.sort_values("date")
        .drop_duplicates("date", keep="last")
        .reset_index(drop=True)
    )
    session_mask = (
        (d["date"].dt.time >= MARKET_OPEN)
        & (d["date"].dt.time < MARKET_CLOSE)
    )
    return d[session_mask].reset_index(drop=True)


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.astype(float).ewm(span=int(span), adjust=False).mean()


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            (high - low).abs(),
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(
        alpha=1.0 / int(period),
        adjust=False,
        min_periods=int(period),
    ).mean()


def _add_bootstrap_indicators(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    lengths = STOCKMAP_CONFIG.indicators.ema_lengths
    d["ema100"] = _ema(d["close"], int(lengths["ema100"]))
    d["ema200"] = _ema(d["close"], int(lengths["ema200"]))
    d["atr"] = _atr(d, int(STOCKMAP_CONFIG.indicators.atr_period))
    d["ema100_slope"] = d["ema100"].diff()
    d["ema200_slope"] = d["ema200"].diff()
    return d


def _float_or_none(value: Any) -> Optional[float]:
    try:
        if value is None or pd.isna(value):
            return None
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None




def _validate_candle_continuity(
    df: pd.DataFrame,
    *,
    previous_source_time: Optional[datetime] = None,
) -> None:
    if df is None or df.empty:
        raise ValueError("StockMap candle frame is empty")

    ordered = df.sort_values("date").reset_index(drop=True)
    times = [_as_ist(value) for value in ordered["date"].tolist()]
    if previous_source_time is not None:
        previous = _as_ist(previous_source_time)
        first = times[0]
        if first.date() == previous.date() and first - previous != timedelta(minutes=TF_MINUTES):
            raise ValueError(
                "Unsafe StockMap continuity gap: previous=%s first_new=%s"
                % (previous, first)
            )

    for prior, current in zip(times, times[1:]):
        if current <= prior:
            raise ValueError(
                f"StockMap candle times are not strictly increasing: {prior} -> {current}"
            )
        if current.date() == prior.date() and current - prior != timedelta(minutes=TF_MINUTES):
            raise ValueError(
                "Missing intraday 15-minute candle: %s -> %s" % (prior, current)
            )

def _latest_completed_boundary(end_date: datetime) -> Optional[datetime]:
    """Return the latest completed 15-minute candle start label."""

    end_date = _as_ist(end_date)
    open_dt = end_date.replace(hour=9, minute=15, second=0, microsecond=0)
    close_dt = end_date.replace(hour=15, minute=15, second=0, microsecond=0)

    if end_date < open_dt + timedelta(minutes=TF_MINUTES):
        return None
    effective = min(end_date, close_dt)
    elapsed = int((effective - open_dt).total_seconds() // 60)
    completed_candles = elapsed // TF_MINUTES
    if completed_candles <= 0:
        return None
    return open_dt + timedelta(minutes=(completed_candles - 1) * TF_MINUTES)


def _filter_completed_through(df: pd.DataFrame, candle_time: datetime) -> pd.DataFrame:
    if df.empty:
        return df
    candle_time = _as_ist(candle_time)
    return df[df["date"] <= candle_time].copy().reset_index(drop=True)


def _require_target_candle(df: pd.DataFrame, candle_time: datetime) -> None:
    candle_time = _as_ist(candle_time)
    if df is None or df.empty:
        raise ValueError(f"No completed 15-minute candles through {candle_time}")
    latest = _as_ist(df.iloc[-1]["date"])
    if latest != candle_time:
        raise ValueError(
            "Latest completed StockMap candle is missing: expected=%s actual=%s"
            % (candle_time, latest)
        )


def _require_anchor_candle(df: pd.DataFrame, candle_time: datetime) -> None:
    """Require the persisted StockMap timestamp in the fetched API frame.

    Continuation validates timestamp identity only. Candle values are not compared.
    """

    candle_time = _as_ist(candle_time)
    if df is None or df.empty:
        raise ValueError(f"StockMap continuation frame is empty for anchor {candle_time}")
    available = pd.to_datetime(df["date"])
    if available.dt.tz is None:
        available = available.dt.tz_localize(IST)
    else:
        available = available.dt.tz_convert(IST)
    if not bool((available == pd.Timestamp(candle_time)).any()):
        raise ValueError(
            f"Previous StockMap timestamp is missing from API frame: {candle_time}"
        )


def _compact_state_memory(state_memory: Dict[str, Any]) -> Dict[str, Any]:
    fields = (
        "raw_state",
        "state",
        "count",
        "previous_state",
        "previous_count",
        "candidate_state",
        "candidate_count",
        "flip_count_today",
    )
    compact: Dict[str, Any] = {}
    for key in STRUCTURE_STATE_KEYS:
        entry = state_memory.get(key)
        if entry is None:
            raise ValueError(f"StockMap structure memory key is missing: {key}")
        compact[key] = {field: entry.get(field) for field in fields}
    return compact


def _inflate_state_memory(previous: StockMapSchema) -> Dict[str, Any]:
    memory = {
        key: entry.model_dump(mode="python")
        for key, entry in previous.memory.state.items()
    }
    for key in STRUCTURE_STATE_KEYS:
        if key not in memory:
            raise ValueError(f"Previous StockMap continuity key is missing: {key}")
    memory["structure.accepted"]["value"] = previous.structure.accepted.model_dump(
        mode="python"
    )
    memory["structure.candidate"]["value"] = previous.structure.candidate.model_dump(
        mode="python"
    )
    return memory


def _reset_daily_flip_counts(memory: Dict[str, Any]) -> None:
    for entry in memory.values():
        if isinstance(entry, dict) and "flip_count_today" in entry:
            entry["flip_count_today"] = 0


def _dummy_structure_inputs() -> Dict[str, Dict[str, Any]]:
    return {
        "hma": {"state": "UNKNOWN", "strength": "UNKNOWN"},
        "context": {
            "rsi": {"zone": "NA"},
            "adx": {"band": "NA"},
            "atr": {"band": "NA"},
        },
        "bollinger": {"zone": "UNKNOWN"},
        "vwap": {"px_vs_vwap_pct": None},
        "volume": {"bar_rvol_band": "NA"},
    }


def _opening_range_from_15m(df: pd.DataFrame, source_time: datetime) -> Dict[str, Any]:
    # The first 15-minute candle is the completed 09:15-09:30 opening range.
    source_time = _as_ist(source_time)
    day_rows = df[df["date"].dt.date == source_time.date()].sort_values("date")
    first = day_rows[day_rows["date"].dt.time == MARKET_OPEN]
    ready = source_time >= source_time.replace(hour=9, minute=15, second=0, microsecond=0)
    if first.empty or not ready:
        return {"window": "09:15-09:29", "high": None, "low": None, "ready": False}
    row = first.iloc[0]
    return {
        "window": "09:15-09:29",
        "high": float(row["high"]),
        "low": float(row["low"]),
        "ready": True,
    }


def _today_block(df: pd.DataFrame, source_time: datetime) -> Dict[str, Optional[float]]:
    source_time = _as_ist(source_time)
    rows = df[
        (df["date"].dt.date == source_time.date())
        & (df["date"] <= source_time)
    ]
    if rows.empty:
        return {"open": None, "high": None, "low": None}
    return {
        "open": float(rows.iloc[0]["open"]),
        "high": float(rows["high"].max()),
        "low": float(rows["low"].min()),
    }


def _bootstrap_levels(df: pd.DataFrame, source_time: datetime) -> Dict[str, Any]:
    prev_day = compute_prev_day_ohlc(df, source_time) or {
        "open": None,
        "high": None,
        "low": None,
        "close": None,
    }
    return {
        "prev_day": prev_day,
        "initial_accepted_range": compute_initial_accepted_range(df, source_time),
        "today": _today_block(df, source_time),
        "opening_range": _opening_range_from_15m(df, source_time),
    }


def _advance_replay_levels(
    previous_levels: Dict[str, Any],
    row: pd.Series,
    *,
    previous_close: Optional[float],
    is_new_day: bool,
) -> Dict[str, Any]:
    """Advance replay/session levels without rescanning the full history."""
    source_time = _as_ist(row["date"])
    if is_new_day:
        prior_today = previous_levels.get("today") or {}
        prev_day = {
            "open": prior_today.get("open"),
            "high": prior_today.get("high"),
            "low": prior_today.get("low"),
            "close": previous_close,
        }
        today = {
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
        }
        opening_range = {
            "window": "09:15-09:29",
            "high": float(row["high"]) if source_time.time() == MARKET_OPEN else None,
            "low": float(row["low"]) if source_time.time() == MARKET_OPEN else None,
            "ready": source_time.time() == MARKET_OPEN,
        }
    else:
        prev_day = dict(previous_levels.get("prev_day") or {})
        today = dict(previous_levels.get("today") or {})
        today["high"] = (
            max(float(today["high"]), float(row["high"]))
            if today.get("high") is not None
            else float(row["high"])
        )
        today["low"] = (
            min(float(today["low"]), float(row["low"]))
            if today.get("low") is not None
            else float(row["low"])
        )
        opening_range = dict(previous_levels.get("opening_range") or {})

    return {
        "prev_day": prev_day,
        "initial_accepted_range": previous_levels.get("initial_accepted_range")
        if not is_new_day
        else prev_day,
        "today": today,
        "opening_range": opening_range,
    }


def _incremental_levels(previous: StockMapSchema, row: pd.Series) -> Dict[str, Any]:
    source_time = _as_ist(row["date"])
    previous_source = _as_ist(previous.stockmap_time)
    if source_time.date() == previous_source.date():
        prev_day = previous.levels.prev_day.model_dump(mode="python")
        today = previous.levels.today.model_dump(mode="python")
        today["high"] = max(float(today["high"]), float(row["high"])) if today.get("high") is not None else float(row["high"])
        today["low"] = min(float(today["low"]), float(row["low"])) if today.get("low") is not None else float(row["low"])
        opening_range = previous.levels.opening_range.model_dump(mode="python")
    else:
        prev_day = {
            "open": previous.levels.today.open,
            "high": previous.levels.today.high,
            "low": previous.levels.today.low,
            "close": previous.close,
        }
        today = {
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
        }
        opening_range = {
            "window": "09:15-09:29",
            "high": float(row["high"]) if source_time.time() == MARKET_OPEN else None,
            "low": float(row["low"]) if source_time.time() == MARKET_OPEN else None,
            "ready": source_time.time() == MARKET_OPEN,
        }
    return {
        "prev_day": prev_day,
        "initial_accepted_range": prev_day,
        "today": today,
        "opening_range": opening_range,
    }


def _indicator_context(
    *,
    close: float,
    ema100: Optional[float],
    ema200: Optional[float],
    ema100_slope: Optional[float],
    ema200_slope: Optional[float],
    atr: Optional[float],
) -> Dict[str, Any]:
    def relation(value: Optional[float]) -> str:
        if value is None:
            return "UNAVAILABLE"
        tolerance = abs(close) * 0.0003
        if abs(close - value) <= tolerance:
            return "TESTING"
        return "ABOVE" if close > value else "BELOW"

    if ema100 is None or ema200 is None:
        ordering = "UNAVAILABLE"
    elif ema100 > ema200:
        ordering = "EMA100_ABOVE_EMA200"
    elif ema100 < ema200:
        ordering = "EMA100_BELOW_EMA200"
    else:
        ordering = "EQUAL"

    if (
        ema100 is not None
        and ema200 is not None
        and close > ema100 > ema200
        and (ema100_slope is None or ema100_slope >= 0)
        and (ema200_slope is None or ema200_slope >= 0)
    ):
        regime = "BULLISH_STACKED"
    elif (
        ema100 is not None
        and ema200 is not None
        and close < ema100 < ema200
        and (ema100_slope is None or ema100_slope <= 0)
        and (ema200_slope is None or ema200_slope <= 0)
    ):
        regime = "BEARISH_STACKED"
    elif ema100 is None or ema200 is None:
        regime = "UNAVAILABLE"
    else:
        regime = "TRANSITIONAL"

    atr_pct = None if atr in (None, 0) else float(atr / close * 100.0)
    return {
        "ema": {
            "ema100": ema100,
            "ema200": ema200,
            "ema100_slope": ema100_slope,
            "ema200_slope": ema200_slope,
            "price_to_ema100": relation(ema100),
            "price_to_ema200": relation(ema200),
            "ordering": ordering,
            "regime": regime,
        },
        "atr": {"value": atr, "pct": atr_pct},
    }


def _candidate_levels(
    *,
    close: float,
    atr: Optional[float],
    levels: Dict[str, Any],
    structure: Any,
    indicators: Dict[str, Any],
) -> Dict[str, Any]:
    candidates: List[Tuple[str, float]] = []
    accepted = structure.accepted.range
    if accepted.low is not None:
        candidates.append(("ACCEPTED_RANGE_LOW", float(accepted.low)))
    if accepted.high is not None:
        candidates.append(("ACCEPTED_RANGE_HIGH", float(accepted.high)))

    prev_day = levels.get("prev_day") or {}
    if prev_day.get("low") is not None:
        candidates.append(("PDL", float(prev_day["low"])))
    if prev_day.get("high") is not None:
        candidates.append(("PDH", float(prev_day["high"])))

    opening = levels.get("opening_range") or {}
    if opening.get("ready"):
        candidates.append(("ORB_LOW", float(opening["low"])))
        candidates.append(("ORB_HIGH", float(opening["high"])))

    ema = indicators.get("ema") or {}
    if ema.get("ema100") is not None:
        candidates.append(("EMA100", float(ema["ema100"])))
    if ema.get("ema200") is not None:
        candidates.append(("EMA200", float(ema["ema200"])))

    supports = sorted(
        ((name, price) for name, price in candidates if price <= close),
        key=lambda item: item[1],
        reverse=True,
    )
    resistances = sorted(
        ((name, price) for name, price in candidates if price >= close),
        key=lambda item: item[1],
    )
    support = supports[0] if supports else (None, None)
    resistance = resistances[0] if resistances else (None, None)

    def distance_atr(price: Optional[float]) -> Optional[float]:
        if price is None or atr in (None, 0):
            return None
        return abs(float(price) - close) / float(atr)

    position = "UNKNOWN"
    position_pct = None
    if accepted.low is not None and accepted.high is not None and accepted.high > accepted.low:
        if close > accepted.high:
            position = "ABOVE"
        elif close < accepted.low:
            position = "BELOW"
        else:
            position = "INSIDE"
            position_pct = (close - accepted.low) / (accepted.high - accepted.low) * 100.0

    return {
        "accepted_range_position": position,
        "accepted_range_position_pct": position_pct,
        "nearest_support_type": support[0],
        "nearest_support_price": support[1],
        "nearest_support_distance_atr": distance_atr(support[1]),
        "nearest_resistance_type": resistance[0],
        "nearest_resistance_price": resistance[1],
        "nearest_resistance_distance_atr": distance_atr(resistance[1]),
        "room_up_atr": distance_atr(resistance[1]),
        "room_down_atr": distance_atr(support[1]),
    }


def _reason_codes(structure: Any, indicators: Dict[str, Any], location: Dict[str, Any]) -> List[str]:
    codes = [
        f"STRUCTURE_RAW_{str(structure.raw.state).upper()}",
        f"STRUCTURE_SIDE_{str(structure.raw.side).upper()}",
        f"ACCEPTED_{str(structure.accepted.state).upper()}",
        f"EMA_REGIME_{str(indicators['ema']['regime']).upper()}",
        f"RANGE_POSITION_{str(location['accepted_range_position']).upper()}",
    ]
    if structure.candidate.active:
        codes.append(f"CANDIDATE_{str(structure.candidate.status).upper()}")
    return list(dict.fromkeys(codes))


class StockMapFetcher:
    def __init__(self, api_key: str, access_token: str):
        from services.zerodha.kiteconnect_service import KiteConnectService

        self.kite = KiteConnectService(
            api_key=api_key,
            access_token=access_token,
        ).kite

    def fetch_range(
        self,
        token: int,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        started = time.perf_counter()
        logger.info(
            "StockMap API fetch token=%s start=%s end=%s interval=%s",
            token,
            _as_ist(start),
            _as_ist(end),
            STOCKMAP_CONFIG.indicators.frequency,
        )
        records = self.kite.historical_data(
            instrument_token=int(token),
            from_date=_naive_ist(start),
            to_date=_naive_ist(end),
            interval=str(STOCKMAP_CONFIG.indicators.frequency),
            continuous=False,
            oi=False,
        )
        frame = _normalise_candles(records)
        logger.info(
            "StockMap API fetch completed token=%s bars=%d elapsed=%.3fs",
            token,
            len(frame),
            time.perf_counter() - started,
        )
        return frame


class StockMapGenerator:
    def __init__(self, token: int, symbol: str, api_key: str, access_token: str):
        self.token = int(token)
        self.symbol = str(symbol).strip().upper()
        if not self.symbol:
            raise ValueError("StockMap symbol cannot be empty")
        self.fetcher = StockMapFetcher(api_key=api_key, access_token=access_token)

    def _build_map(
        self,
        *,
        row: pd.Series,
        working_frame: pd.DataFrame,
        state_memory: Dict[str, Any],
        levels: Dict[str, Any],
        calculation_mode: str,
        source_start_time: Optional[datetime],
        bars_used: int,
    ) -> Tuple[StockMapSchema, Dict[str, Any]]:
        source_time = _as_ist(row["date"])
        stockmap_time = source_time
        close = float(row["close"])
        atr = _float_or_none(row.get("atr"))
        ema100 = _float_or_none(row.get("ema100"))
        ema200 = _float_or_none(row.get("ema200"))
        ema100_slope = _float_or_none(row.get("ema100_slope"))
        ema200_slope = _float_or_none(row.get("ema200_slope"))

        base_lookback = max(
            int(STOCKMAP_CONFIG.structure.lookback_bars),
            int(STOCKMAP_CONFIG.structure.max_intraday_range_bars) + 1,
        )
        base_frame = working_frame[working_frame["date"] <= source_time].tail(base_lookback)

        structure, next_memory = compute_structure_state_from_memory(
            px=close,
            df3=base_frame,
            df15=None,
            levels=levels,
            atr=atr,
            curr_snapshot_like=_dummy_structure_inputs(),
            prev_state_memory=state_memory,
        )

        indicators = _indicator_context(
            close=close,
            ema100=ema100,
            ema200=ema200,
            ema100_slope=ema100_slope,
            ema200_slope=ema200_slope,
            atr=atr,
        )
        location = _candidate_levels(
            close=close,
            atr=atr,
            levels=levels,
            structure=structure,
            indicators=indicators,
        )

        stockmap = StockMapSchema.model_validate(
            {
                "symbol": self.symbol,
                "stockmap_time": stockmap_time,
                "tf": "15m",
                "close": close,
                "bar": {
                    "candle_time": source_time,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": close,
                    "volume": float(row["volume"]),
                },
                "levels": {
                    "prev_day": levels.get("prev_day") or {},
                    "today": levels.get("today") or {},
                    "opening_range": levels.get("opening_range") or {},
                },
                "indicators": indicators,
                "structure": structure.model_dump(mode="python"),
                "location": location,
                "memory": {
                    "stockmap_time": stockmap_time,
                    "state": _compact_state_memory(next_memory),
                },
                "diagnostics": {
                    "calculation_version": STOCKMAP_CONFIG.calculation_version,
                    "calculation_mode": calculation_mode,
                    "availability": "AVAILABLE",
                    "source_start_time": source_start_time,
                    "source_end_time": source_time,
                    "bars_used": int(bars_used),
                    "missing_bar_count": 0,
                    "reason_codes": _reason_codes(structure, indicators, location),
                },
            }
        )
        return stockmap, next_memory

    def _bootstrap_frame(self, candle_time: datetime) -> pd.DataFrame:
        candle_time = _as_ist(candle_time)
        fetch_end = candle_time + timedelta(minutes=TF_MINUTES)
        start = fetch_end - timedelta(
            days=int(STOCKMAP_CONFIG.indicators.bootstrap_calendar_days)
        )
        logger.info(
            "StockMap[%s] bootstrap start=%s target_candle=%s fetch_end=%s",
            self.symbol,
            start,
            candle_time,
            fetch_end,
        )
        df = self.fetcher.fetch_range(self.token, start, fetch_end)
        df = _filter_completed_through(df, candle_time)
        if df.empty:
            raise ValueError(
                f"No completed 15-minute candles returned for {self.symbol} through {candle_time}"
            )
        _require_target_candle(df, candle_time)
        _validate_candle_continuity(df)
        frame = _add_bootstrap_indicators(df)
        logger.info(
            "StockMap[%s] bootstrap ready bars=%d first=%s last=%s",
            self.symbol,
            len(frame),
            frame.iloc[0]["date"],
            frame.iloc[-1]["date"],
        )
        return frame

    def _replay_frame(
        self,
        frame: pd.DataFrame,
        *,
        persist: bool,
        persist_day: Optional[date] = None,
        persist_only_last: bool = False,
        skip_existing: bool = True,
        calculation_mode: str = "BOOTSTRAP",
    ) -> List[StockMapSchema]:
        state_memory: Dict[str, Any] = {}
        maps: List[StockMapSchema] = []
        source_start = _as_ist(frame.iloc[0]["date"])
        prior_day: Optional[date] = None
        previous_close: Optional[float] = None
        levels_state: Optional[Dict[str, Any]] = None
        total_bars = len(frame)
        progress_every = 100
        base_lookback = max(
            int(STOCKMAP_CONFIG.structure.lookback_bars),
            int(STOCKMAP_CONFIG.structure.max_intraday_range_bars) + 1,
        )
        started = time.perf_counter()

        logger.info(
            "StockMap[%s] replay start bars=%d target_day=%s mode=%s",
            self.symbol,
            total_bars,
            persist_day,
            calculation_mode,
        )

        for index, row in frame.iterrows():
            source_time = _as_ist(row["date"])
            is_new_day = prior_day is not None and source_time.date() != prior_day
            if is_new_day:
                _reset_daily_flip_counts(state_memory)
            prior_day = source_time.date()

            if levels_state is None or is_new_day:
                # Previous-day balance is recalculated once at each session boundary.
                # Within the session, levels advance incrementally.
                levels_state = _bootstrap_levels(frame.iloc[: index + 1], source_time)
            else:
                levels_state = _advance_replay_levels(
                    levels_state,
                    row,
                    previous_close=previous_close,
                    is_new_day=False,
                )

            start_index = max(0, index - base_lookback + 1)
            working = frame.iloc[start_index : index + 1]
            stockmap, state_memory = self._build_map(
                row=row,
                working_frame=working,
                state_memory=state_memory,
                levels=levels_state,
                calculation_mode=calculation_mode,
                source_start_time=source_start,
                bars_used=index + 1,
            )

            should_collect = persist_day is None or stockmap.stockmap_time.date() == persist_day
            if should_collect:
                maps.append(stockmap)

            previous_close = float(row["close"])
            processed = index + 1
            if processed == 1 or processed % progress_every == 0 or processed == total_bars:
                logger.info(
                    "StockMap[%s] replay progress %d/%d source=%s collected=%d elapsed=%.3fs",
                    self.symbol,
                    processed,
                    total_bars,
                    source_time,
                    len(maps),
                    time.perf_counter() - started,
                )

        if persist_only_last:
            maps = maps[-1:]

        if persist:
            logger.info(
                "StockMap[%s] persistence start rows=%d skip_existing=%s",
                self.symbol,
                len(maps),
                skip_existing,
            )
            for stockmap in maps:
                if skip_existing and StockMapSchema.stockmap_exists(
                    stockmap.symbol, stockmap.stockmap_time
                ):
                    continue
                StockMapSchema.create_stockmap(stockmap)
            logger.info(
                "StockMap[%s] persistence completed rows=%d",
                self.symbol,
                len(maps),
            )
        return maps

    def generate_stockmap(
        self,
        end_date: Optional[datetime] = None,
        persist_stockmap: bool = True,
        skip_existing: bool = True,
    ) -> Optional[StockMapSchema]:
        started = time.perf_counter()
        end_date = _as_ist(end_date or datetime.now(IST))
        target_candle_time = _latest_completed_boundary(end_date)
        if target_candle_time is None:
            logger.info(
                "StockMap[%s] has no completed 15-minute candle at %s",
                self.symbol,
                end_date,
            )
            return None

        def rebuild(calculation_mode: str) -> Optional[StockMapSchema]:
            frame = self._bootstrap_frame(target_candle_time)
            maps = self._replay_frame(
                frame,
                persist=persist_stockmap,
                persist_only_last=True,
                skip_existing=skip_existing,
                calculation_mode=calculation_mode,
            )
            return maps[-1] if maps else None

        if skip_existing:
            existing = StockMapSchema.fetch_stockmap(
                self.symbol,
                target_candle_time,
            )
            if existing is not None:
                return existing

        previous = StockMapSchema.fetch_previous_for_symbol(
            self.symbol,
            target_candle_time,
        )
        if previous is None:
            result = rebuild("INITIALIZED")
        elif previous.diagnostics.calculation_version != STOCKMAP_CONFIG.calculation_version:
            result = rebuild("REBUILT_VERSION")
        else:
            fetch_end = target_candle_time + timedelta(minutes=TF_MINUTES)
            fetch_start = fetch_end - timedelta(
                days=int(STOCKMAP_CONFIG.indicators.continuation_calendar_days)
            )

            if _as_ist(previous.stockmap_time) < fetch_start:
                logger.warning(
                    "StockMap[%s] previous state is outside the %d-day continuation window; "
                    "explicit rebuild previous=%s target=%s",
                    self.symbol,
                    int(STOCKMAP_CONFIG.indicators.continuation_calendar_days),
                    previous.stockmap_time,
                    target_candle_time,
                )
                result = rebuild("REBUILT_GAP")
            else:
                fetched = self.fetcher.fetch_range(
                    self.token,
                    fetch_start,
                    fetch_end,
                )
                fetched = _filter_completed_through(
                    fetched,
                    target_candle_time,
                )
                _require_target_candle(fetched, target_candle_time)
                _validate_candle_continuity(fetched)

                previous_time = _as_ist(previous.stockmap_time)
                _require_anchor_candle(fetched, previous_time)
                missing = fetched[fetched["date"] > previous_time].copy()
                if missing.empty:
                    logger.info(
                        "StockMap[%s] no new completed candles after %s",
                        self.symbol,
                        previous.stockmap_time,
                    )
                    return previous

                try:
                    state_memory = _inflate_state_memory(previous)
                    previous_ema100 = previous.indicators.ema.ema100
                    previous_ema200 = previous.indicators.ema.ema200
                    previous_atr = previous.indicators.atr.value
                    if (
                        previous_ema100 is None
                        or previous_ema200 is None
                        or previous_atr is None
                    ):
                        raise ValueError(
                            "Previous StockMap indicator seed is incomplete"
                        )
                except Exception as exc:
                    logger.warning(
                        "StockMap[%s] continuity restore failed at %s; explicit rebuild: %s",
                        self.symbol,
                        target_candle_time,
                        exc,
                    )
                    result = rebuild("REBUILT_CONTINUITY")
                else:
                    _validate_candle_continuity(
                        missing,
                        previous_source_time=previous.stockmap_time,
                    )
                    maps: List[StockMapSchema] = []
                    current_previous = previous
                    previous_close = float(previous.close)
                    alpha100 = 2.0 / (
                        int(STOCKMAP_CONFIG.indicators.ema_lengths["ema100"])
                        + 1.0
                    )
                    alpha200 = 2.0 / (
                        int(STOCKMAP_CONFIG.indicators.ema_lengths["ema200"])
                        + 1.0
                    )
                    atr_alpha = 1.0 / int(
                        STOCKMAP_CONFIG.indicators.atr_period
                    )
                    base_lookback = max(
                        int(STOCKMAP_CONFIG.structure.lookback_bars),
                        int(STOCKMAP_CONFIG.structure.max_intraday_range_bars)
                        + 1,
                    )
                    source_start_time = _as_ist(fetched.iloc[0]["date"])

                    for _, raw_row in missing.sort_values("date").iterrows():
                        row = raw_row.copy()
                        source_time = _as_ist(row["date"])
                        if source_time.date() != _as_ist(
                            current_previous.stockmap_time
                        ).date():
                            _reset_daily_flip_counts(state_memory)

                        close = float(row["close"])
                        ema100 = alpha100 * close + (
                            1.0 - alpha100
                        ) * float(previous_ema100)
                        ema200 = alpha200 * close + (
                            1.0 - alpha200
                        ) * float(previous_ema200)
                        true_range = max(
                            float(row["high"]) - float(row["low"]),
                            abs(float(row["high"]) - previous_close),
                            abs(float(row["low"]) - previous_close),
                        )
                        atr = atr_alpha * true_range + (
                            1.0 - atr_alpha
                        ) * float(previous_atr)
                        row["ema100"] = ema100
                        row["ema200"] = ema200
                        row["ema100_slope"] = ema100 - float(previous_ema100)
                        row["ema200_slope"] = ema200 - float(previous_ema200)
                        row["atr"] = atr

                        working = fetched[
                            fetched["date"] <= source_time
                        ].tail(base_lookback)
                        levels = _incremental_levels(current_previous, row)
                        stockmap, state_memory = self._build_map(
                            row=row,
                            working_frame=working,
                            state_memory=state_memory,
                            levels=levels,
                            calculation_mode="UPDATED",
                            source_start_time=source_start_time,
                            bars_used=len(working),
                        )
                        if persist_stockmap:
                            StockMapSchema.create_stockmap(stockmap)
                        maps.append(stockmap)
                        current_previous = stockmap
                        previous_close = close
                        previous_ema100 = ema100
                        previous_ema200 = ema200
                        previous_atr = atr

                    result = maps[-1] if maps else previous

        logger.info(
            "StockMap[%s] generated map_time=%s mode=%s elapsed_ms=%.3f",
            self.symbol,
            result.stockmap_time if result else None,
            result.diagnostics.calculation_mode if result else None,
            (time.perf_counter() - started) * 1000.0,
        )
        return result

    def generate_day(
        self,
        trading_day: date,
        *,
        persist_stockmaps: bool = True,
        skip_existing: bool = True,
    ) -> List[StockMapSchema]:
        """Build or resume one trading day of StockMaps.

        Default replay behaviour is restartable:

        * no prior compatible map -> 60-day bootstrap and full-day replay;
        * prior compatible map -> use it as the continuity anchor and fill only
          missing completed candles through the requested day's final candle;
        * completed requested day -> return the persisted day without rebuilding.

        ``skip_existing=False`` remains the explicit full-day rebuild/upsert mode.
        """

        market_close = datetime.combine(trading_day, MARKET_CLOSE, tzinfo=IST)
        target_candle_time = _latest_completed_boundary(market_close)
        if target_candle_time is None:
            raise ValueError(f"No completed StockMap candle for {trading_day}")

        day_start = datetime.combine(trading_day, MARKET_OPEN, tzinfo=IST)

        def full_day_rebuild(calculation_mode: str) -> List[StockMapSchema]:
            frame = self._bootstrap_frame(target_candle_time)
            return self._replay_frame(
                frame,
                persist=persist_stockmaps,
                persist_day=trading_day,
                persist_only_last=False,
                skip_existing=skip_existing,
                calculation_mode=calculation_mode,
            )

        # --force maps to skip_existing=False in replay_stockmaps.py. Preserve its
        # meaning as an explicit full-day rebuild/upsert rather than continuation.
        if not skip_existing:
            maps = full_day_rebuild("REPLAY_DAY_REBUILT")
            logger.info(
                "StockMap[%s] rebuilt %d maps for %s",
                self.symbol,
                len(maps),
                trading_day,
            )
            return maps

        existing_target = StockMapSchema.fetch_stockmap(
            self.symbol,
            target_candle_time,
        )
        if existing_target is not None:
            maps = StockMapSchema.fetch_range(
                self.symbol,
                day_start,
                target_candle_time,
            )
            logger.info(
                "StockMap[%s] replay day already complete day=%s rows=%d",
                self.symbol,
                trading_day,
                len(maps),
            )
            return maps

        previous = StockMapSchema.fetch_previous_for_symbol(
            self.symbol,
            target_candle_time,
        )
        fetch_end = target_candle_time + timedelta(minutes=TF_MINUTES)
        fetch_start = fetch_end - timedelta(
            days=int(STOCKMAP_CONFIG.indicators.continuation_calendar_days)
        )

        requires_bootstrap = (
            previous is None
            or previous.diagnostics.calculation_version
            != STOCKMAP_CONFIG.calculation_version
            or _as_ist(previous.stockmap_time) < fetch_start
        )
        if requires_bootstrap:
            mode = "REPLAY_DAY_INITIALIZED" if previous is None else "REPLAY_DAY_REBUILT"
            maps = full_day_rebuild(mode)
            logger.info(
                "StockMap[%s] generated %d maps for %s mode=%s",
                self.symbol,
                len(maps),
                trading_day,
                mode,
            )
            return maps

        logger.info(
            "StockMap[%s] replay resume day=%s previous=%s target=%s",
            self.symbol,
            trading_day,
            previous.stockmap_time,
            target_candle_time,
        )
        self.generate_stockmap(
            end_date=market_close,
            persist_stockmap=persist_stockmaps,
            skip_existing=True,
        )

        if not persist_stockmaps:
            raise ValueError(
                "Restartable generate_day continuation requires persistence; "
                "use explicit full replay for non-persisted controls"
            )

        maps = StockMapSchema.fetch_range(
            self.symbol,
            day_start,
            target_candle_time,
        )
        logger.info(
            "StockMap[%s] resumed %d maps for %s from previous=%s",
            self.symbol,
            len(maps),
            trading_day,
            previous.stockmap_time,
        )
        return maps
