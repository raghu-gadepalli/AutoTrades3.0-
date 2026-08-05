#!/usr/bin/env python3
"""
services/generator/snapshot_generator.py

Snapshot generator with structure block.

Strict snapshot generator with embedded Auction continuity.

The persisted payload is validated by SnapshotSchema before Auction consumes it
and again after Auction enrichment. Schema-defined fields never use alternate
paths or silent default substitutions.
"""

from __future__ import annotations

import json
import os
import sys
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any, Tuple

import numpy as np
import pandas as pd
from kiteconnect.exceptions import TokenException, InputException

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from configs.snapshot_config import SNAPSHOT_CONFIG
from schemas.snapshot import SnapshotSchema
from schemas.symbol import SymbolSchema
from schemas.derivatives import DerivativesChainSchema

from services.auction_engine.snapshot_adapter import (
    empty_auction_block,
    initial_auction_memory,
    enrich_snapshot_with_auction,
)

from services.snapshot.snapshot_helper import (
    aggregate_ohlcv,
    compute_prev_day_ohlc,
    compute_initial_accepted_range,
    compute_today_open,
    compute_envelopes,
    compute_px_vs_vwap_pct,
    compute_opening_range_15m,
    classify_rsi_zone,
    classify_adx_band,
    classify_atr_band,
    compute_volume_metrics,
    compute_bollinger_position_zone,
    compute_structure_state_from_memory,
    build_price_action_block,
    build_market_windows_block,
)

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

BASE_INTERVAL = SNAPSHOT_CONFIG.indicators.frequency
LOOKBACK_DAYS = SNAPSHOT_CONFIG.indicators.lookback_days
STRUCTURE_REPLAY_BARS = SNAPSHOT_CONFIG.structure.structure_replay_bars
STRUCTURE_REPLAY_15M_BARS = max(50, int(STRUCTURE_REPLAY_BARS / 5))

HMA_LENGTHS = SNAPSHOT_CONFIG.indicators.hma_lengths
EMA_LENGTHS = SNAPSHOT_CONFIG.indicators.ema_lengths

RSI_PERIOD = SNAPSHOT_CONFIG.indicators.rsi_period
ATR_PERIOD = SNAPSHOT_CONFIG.indicators.atr_period
ADX_PERIOD = SNAPSHOT_CONFIG.indicators.adx_period

BB_PERIOD = SNAPSHOT_CONFIG.indicators.bb_period
BB_STD_MULT = SNAPSHOT_CONFIG.indicators.bb_std_mult

MARKET_OPEN_HHMM: Tuple[int, int] = (9, 15)
MARKET_CLOSE_HHMM: Tuple[int, int] = (15, 30)

TF_PRIMARY_MIN = 3
TF_15_MIN = 15


def _ema(series: pd.Series, span: int) -> pd.Series:
    span = int(span)
    if span <= 0:
        return pd.Series(index=series.index, dtype=float)
    return series.ewm(span=span, adjust=False).mean()


def _wma(series: pd.Series, length: int) -> pd.Series:
    length = int(length)
    if length <= 0:
        return pd.Series(index=series.index, dtype=float)

    weights = np.arange(1, length + 1, dtype=float)

    def _calc(x: np.ndarray) -> float:
        return float(np.dot(x, weights) / weights.sum())

    return series.rolling(length, min_periods=length).apply(_calc, raw=True)


def _hma(series: pd.Series, length: int) -> pd.Series:
    n = int(length)
    if n <= 1:
        return series.astype(float)

    half = max(1, n // 2)
    root = max(1, int(np.sqrt(n)))

    wma_half = _wma(series, half)
    wma_full = _wma(series, n)

    return _wma(2.0 * wma_half - wma_full, root)


def _rsi(close: pd.Series, period: int) -> pd.Series:
    period = int(period)
    if period <= 0:
        return pd.Series(index=close.index, dtype=float)

    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    period = int(period)
    if period <= 0:
        return pd.Series(index=df.index, dtype=float)

    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    prev_close = close.shift(1)

    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def _adx(df: pd.DataFrame, period: int) -> pd.Series:
    period = int(period)
    if period <= 0:
        return pd.Series(index=df.index, dtype=float)

    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    prev_close = close.shift(1)

    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    plus_dm_sm = (
        pd.Series(plus_dm, index=df.index)
        .ewm(alpha=1 / period, adjust=False, min_periods=period)
        .mean()
    )

    minus_dm_sm = (
        pd.Series(minus_dm, index=df.index)
        .ewm(alpha=1 / period, adjust=False, min_periods=period)
        .mean()
    )

    plus_di = 100.0 * (plus_dm_sm / atr.replace(0, np.nan))
    minus_di = 100.0 * (minus_dm_sm / atr.replace(0, np.nan))

    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)

    return dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def _vwap_session(df: pd.DataFrame) -> pd.Series:
    d = df.copy()

    tp = (
        d["high"].astype(float)
        + d["low"].astype(float)
        + d["close"].astype(float)
    ) / 3.0

    vol = d["volume"].astype(float)

    out = pd.Series(index=d.index, dtype=float)

    for _, g in d.groupby(d["date"].dt.date):
        pv = (tp.loc[g.index] * vol.loc[g.index]).cumsum()
        vv = vol.loc[g.index].cumsum().replace(0, np.nan)
        out.loc[g.index] = pv / vv

    return out


def _bollinger(close: pd.Series, period: int, std_mult: float = 2.0) -> pd.DataFrame:
    period = int(period)

    if period <= 0:
        return pd.DataFrame(
            index=close.index,
            data={
                "upper": np.nan,
                "mid": np.nan,
                "lower": np.nan,
                "bb_width": np.nan,
            },
        )

    mid = close.rolling(period, min_periods=period).mean()
    std = close.rolling(period, min_periods=period).std(ddof=0)

    upper = mid + std_mult * std
    lower = mid - std_mult * std
    bb_width = (upper - lower) / mid.replace(0, np.nan) * 100.0

    return pd.DataFrame(
        {
            "upper": upper,
            "mid": mid,
            "lower": lower,
            "bb_width": bb_width,
        }
    )


class SnapshotFetcher:
    def __init__(self, api_key: str, access_token: str):
        from services.zerodha.kiteconnect_service import KiteConnectService

        self.kite = KiteConnectService(
            api_key=api_key,
            access_token=access_token,
        ).kite

    def fetch(self, token: int, end: datetime, interval: str, lookback_days: int):
        start = end - timedelta(days=lookback_days)

        return self.kite.historical_data(
            instrument_token=token,
            from_date=start,
            to_date=end,
            interval=interval,
            continuous=False,
            oi=False,
        )


class IndicatorComputer:
    @staticmethod
    def add_indicators(df_tf: pd.DataFrame) -> pd.DataFrame:
        d = df_tf.copy()

        d["date"] = pd.to_datetime(d["date"])

        if d["date"].dt.tz is None:
            d["date"] = d["date"].dt.tz_localize(IST)
        else:
            d["date"] = d["date"].dt.tz_convert(IST)

        d["hmafast"] = _hma(d["close"], HMA_LENGTHS["hmafast"])
        d["hmamid1"] = _hma(d["close"], HMA_LENGTHS["hmamid1"])
        d["hmamid2"] = _hma(d["close"], HMA_LENGTHS["hmamid2"])
        d["hmaslow"] = _hma(d["close"], HMA_LENGTHS["hmaslow"])

        d["ema_fast"] = _ema(d["close"], EMA_LENGTHS["ema_fast"])
        d["ema_mid1"] = _ema(d["close"], EMA_LENGTHS["ema_mid1"])
        d["ema_mid2"] = _ema(d["close"], EMA_LENGTHS["ema_mid2"])
        d["ema_slow"] = _ema(d["close"], EMA_LENGTHS["ema_slow"])
        d["ema_ref"] = _ema(d["close"], EMA_LENGTHS["ema_ref"])

        d["vwap"] = _vwap_session(d)

        d["atr"] = _atr(d, ATR_PERIOD)
        d["adx"] = _adx(d, ADX_PERIOD)
        d["rsi"] = _rsi(d["close"], RSI_PERIOD)

        bb = _bollinger(d["close"], period=BB_PERIOD, std_mult=BB_STD_MULT)

        d["bb_upper"] = bb["upper"]
        d["bb_mid"] = bb["mid"]
        d["bb_lower"] = bb["lower"]
        d["bb_width"] = bb["bb_width"]

        return d


def _select_latest_stable(
    df_tf: pd.DataFrame,
    end: datetime,
    *,
    period_minutes: int,
    market_open_hhmm: Tuple[int, int] = MARKET_OPEN_HHMM,
    market_close_hhmm: Tuple[int, int] = MARKET_CLOSE_HHMM,
) -> Optional[pd.Series]:
    """Return the latest completed candle as of ``end``.

    AutoTrades stores candle timestamps as candle *start* times.  A candle that
    starts at 15:27 is available at 15:30, and there should be no separate
    15:30 candle.  Therefore, at exact 3-minute boundaries we select the
    previous candle-start timestamp; at/after market close we cap the stable
    timestamp at close - period.
    """
    if df_tf is None or df_tf.empty:
        return None

    d = df_tf.copy()
    d["date"] = pd.to_datetime(d["date"])

    if d["date"].dt.tz is None:
        d["date"] = d["date"].dt.tz_localize(IST)
    else:
        d["date"] = d["date"].dt.tz_convert(IST)

    d = d.sort_values("date")

    if end.tzinfo is None:
        end = end.replace(tzinfo=IST)
    else:
        end = end.astimezone(IST)

    open_ = end.replace(
        hour=market_open_hhmm[0],
        minute=market_open_hhmm[1],
        second=0,
        microsecond=0,
    )

    close_ = end.replace(
        hour=market_close_hhmm[0],
        minute=market_close_hhmm[1],
        second=0,
        microsecond=0,
    )

    period_minutes = max(1, int(period_minutes))

    if end <= open_:
        subset = d[d["date"] <= open_]
        return subset.iloc[-1] if not subset.empty else None

    if end >= close_:
        stable_end = close_ - timedelta(minutes=period_minutes)
    else:
        minutes_since_open = int((end - open_).total_seconds() // 60)
        if minutes_since_open % period_minutes == 0:
            minutes_since_open -= period_minutes
        else:
            minutes_since_open = (minutes_since_open // period_minutes) * period_minutes
        stable_end = open_ + timedelta(minutes=minutes_since_open)

    if stable_end < open_:
        return None

    subset = d[d["date"] <= stable_end]
    return subset.iloc[-1] if not subset.empty else None

def _derive_hma_state_strength(row: pd.Series) -> Dict[str, str]:
    fast = row["hmafast"]
    mid1 = row["hmamid1"]
    mid2 = row["hmamid2"]
    slow = row["hmaslow"]

    if any(pd.isna(x) for x in [fast, mid1, mid2, slow]):
        return {"state": "NO_TREND", "strength": "NA"}

    if fast > mid1 and fast > mid2 and fast > slow:
        return {"state": "BUY", "strength": "STRONG_BUY"}
    if fast > mid1 and fast > mid2:
        return {"state": "BUY", "strength": "MEDIUM_BUY"}
    if fast > mid1:
        return {"state": "BUY", "strength": "WEAK_BUY"}

    if fast < mid1 and fast < mid2 and fast < slow:
        return {"state": "SELL", "strength": "STRONG_SELL"}
    if fast < mid1 and fast < mid2:
        return {"state": "SELL", "strength": "MEDIUM_SELL"}
    if fast < mid1:
        return {"state": "SELL", "strength": "WEAK_SELL"}

    return {"state": "NO_TREND", "strength": "NA"}


def _optional_float(row: Any, key: str) -> Optional[float]:
    """Read one known column directly; null is explicit, missing is an error."""
    value = row[key]
    return None if pd.isna(value) else float(value)


def _normalise_prev_day(prev_day: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if prev_day is None:
        return {"open": None, "high": None, "low": None, "close": None}
    return {
        "open": prev_day["open"],
        "high": prev_day["high"],
        "low": prev_day["low"],
        "close": prev_day["close"],
    }


def _normalise_opening_range(opening_range: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "window": opening_range["window"],
        "high": opening_range["high"],
        "low": opening_range["low"],
        "ready": bool(opening_range["ready"]),
    }


def _compact_market_windows(windows: Dict[str, Any]) -> Dict[str, Any]:
    fields = (
        "status",
        "bars",
        "move_points",
        "move_pct",
        "move_atr",
        "range_points",
        "range_pct",
        "close_position_in_range",
    )
    compact: Dict[str, Any] = {}
    for key in ("15m", "30m", "60m", "sod"):
        if key not in windows:
            raise ValueError(f"Required market window is missing: {key}")
        row = windows[key]
        compact[key] = {field: row[field] for field in fields}
    return compact


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


def _snapshot_payload_mapping(value: Any) -> Optional[Dict[str, Any]]:
    """Decode a persisted snapshot payload without interpreting field values."""
    if value is None or value == "":
        return None
    if isinstance(value, SnapshotSchema):
        return value.model_dump(mode="python", by_alias=True)
    if isinstance(value, str):
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("Snapshot JSON root must be an object")
        return parsed
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        parsed = value.model_dump(mode="python", by_alias=True)
        if not isinstance(parsed, dict):
            raise ValueError("Snapshot model_dump root must be an object")
        return parsed
    raise TypeError(f"Unsupported snapshot payload type: {type(value)!r}")


def _parse_snapshot_payload(value: Any) -> Optional[SnapshotSchema]:
    """Strictly validate one complete persisted snapshot payload."""
    if isinstance(value, SnapshotSchema):
        return value
    parsed = _snapshot_payload_mapping(value)
    if parsed is None:
        return None
    return SnapshotSchema.from_db_dict(parsed)


def _to_ist_datetime(value: Any) -> datetime:
    ts = pd.to_datetime(value)
    if pd.isna(ts):
        raise ValueError("Snapshot time cannot be null")
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.tz_localize(IST)
    else:
        ts = ts.tz_convert(IST)
    return ts.to_pydatetime()


def _symbol_last_snapshot(
    sym: Any,
    snap_time: datetime,
) -> Optional[SnapshotSchema]:
    """Return the cache only when it can be the immediate continuity input.

    Replay commonly starts at the beginning of a session while ``symbols`` still
    carries the final snapshot from an earlier run. That future or stale cache
    entry is not a candidate continuity record and must not be schema-validated.
    A temporally eligible cache entry is always validated strictly; an old or
    malformed immediately-previous payload still fails rather than cold-starting
    silently.
    """
    if sym is None:
        return None

    raw = getattr(sym, "last_snapshot")
    if isinstance(raw, SnapshotSchema):
        previous_time = _to_ist_datetime(raw.snapshot_time)
        payload = None
    else:
        payload = _snapshot_payload_mapping(raw)
        if payload is None:
            return None
        if "snapshot_time" not in payload:
            raise ValueError("Cached snapshot is missing required snapshot_time")
        previous_time = _to_ist_datetime(payload["snapshot_time"])

    current_time = _to_ist_datetime(snap_time)
    if previous_time.date() != current_time.date() or previous_time >= current_time:
        return None

    max_gap_minutes = float(SNAPSHOT_CONFIG.service.tick_minutes) + 0.5
    gap_minutes = (current_time - previous_time).total_seconds() / 60.0
    if gap_minutes > max_gap_minutes:
        logger.warning(
            "symbol_snapshot_cache_not_immediate symbol=%s previous=%s current=%s "
            "gap_minutes=%.3f; cache ignored before schema validation",
            getattr(sym, "symbol"),
            previous_time,
            current_time,
            gap_minutes,
        )
        return None

    if isinstance(raw, SnapshotSchema):
        return raw
    return SnapshotSchema.from_db_dict(payload)


def _inflate_structure_state_memory(previous: SnapshotSchema) -> Dict[str, Any]:
    """Reattach public accepted/candidate values for the structure calculator.

    The persisted private state intentionally excludes duplicate copies of the
    current accepted and candidate structures. The calculator receives them
    from the canonical public ``structure`` block.
    """
    memory = {
        key: entry.model_dump(mode="python")
        for key, entry in previous.memory.structure.state.items()
    }
    for key in STRUCTURE_STATE_KEYS:
        if key not in memory:
            raise ValueError(f"Required structure continuity key is missing: {key}")
    memory["structure.accepted"]["value"] = previous.structure.accepted.model_dump(
        mode="python"
    )
    memory["structure.candidate"]["value"] = previous.structure.candidate.model_dump(
        mode="python"
    )
    return memory


def _compact_structure_state_memory(state_memory: Dict[str, Any]) -> Dict[str, Any]:
    """Persist only structure continuity and the two Auction flip counters."""
    compact: Dict[str, Any] = {}
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
    for key in STRUCTURE_STATE_KEYS:
        if key not in state_memory:
            raise ValueError(f"Structure calculator did not produce required memory key: {key}")
        entry = state_memory[key]
        compact[key] = {field: entry[field] for field in fields}
    return compact


def _frame_to_structure_rows(frame: pd.DataFrame, *, limit: int) -> list[Dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    d = frame.copy()
    d["date"] = pd.to_datetime(d["date"])
    if d["date"].dt.tz is None:
        d["date"] = d["date"].dt.tz_localize(IST)
    else:
        d["date"] = d["date"].dt.tz_convert(IST)
    rows: list[Dict[str, Any]] = []
    for _, row in d.sort_values("date").drop_duplicates("date", keep="last").tail(limit).iterrows():
        rows.append({
            "date": pd.to_datetime(row["date"]).to_pydatetime(),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
        })
    return rows


def _structure_rows_to_frame(rows: Any) -> pd.DataFrame:
    if rows is None:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    normalised = [
        row.model_dump(mode="python") if hasattr(row, "model_dump") else dict(row)
        for row in rows
    ]
    if not normalised:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    d = pd.DataFrame(normalised)
    required = {"date", "open", "high", "low", "close", "volume"}
    missing = required.difference(d.columns)
    if missing:
        raise ValueError(f"Structure memory bar fields are missing: {sorted(missing)}")
    d["date"] = pd.to_datetime(d["date"])
    if d["date"].dt.tz is None:
        d["date"] = d["date"].dt.tz_localize(IST)
    else:
        d["date"] = d["date"].dt.tz_convert(IST)
    return d.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)


def _append_structure_current(
    previous_rows: Any,
    current_frame: pd.DataFrame,
    *,
    snap_time: datetime,
    limit: int,
) -> pd.DataFrame:
    previous = _structure_rows_to_frame(previous_rows)
    current = pd.DataFrame()
    if current_frame is not None and not current_frame.empty:
        current = current_frame.copy()
        current["date"] = pd.to_datetime(current["date"])
        if current["date"].dt.tz is None:
            current["date"] = current["date"].dt.tz_localize(IST)
        else:
            current["date"] = current["date"].dt.tz_convert(IST)
        current = current[current["date"] <= snap_time].tail(1)
    combined = pd.concat([previous, current], ignore_index=True)
    if combined.empty:
        return combined
    return (
        combined.sort_values("date")
        .drop_duplicates("date", keep="last")
        .tail(limit)
        .reset_index(drop=True)
    )


def _build_structure_memory(
    df3: pd.DataFrame,
    df15: pd.DataFrame,
    *,
    snapshot_time: datetime,
    lookback_bars: int,
    state_memory: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "snapshot_time": snapshot_time,
        "bars_3m": _frame_to_structure_rows(df3, limit=lookback_bars),
        "bars_15m": _frame_to_structure_rows(df15, limit=3),
        "state": _compact_structure_state_memory(state_memory),
    }


def _usable_previous_snapshot_for_structure(
    previous: Optional[SnapshotSchema],
    snap_time: datetime,
) -> bool:
    if not bool(SNAPSHOT_CONFIG.structure.use_symbol_last_snapshot_for_structure):
        return False
    if previous is None:
        return False

    prev_time = _to_ist_datetime(previous.snapshot_time)
    curr_time = _to_ist_datetime(snap_time)
    if prev_time.date() != curr_time.date() or prev_time >= curr_time:
        return False
    if not previous.memory.structure.bars_3m:
        raise ValueError("Previous snapshot has no structure bars")

    max_gap_minutes = float(SNAPSHOT_CONFIG.service.tick_minutes) + 0.5
    gap_minutes = (curr_time - prev_time).total_seconds() / 60.0
    if gap_minutes <= 0:
        raise ValueError("Previous structure snapshot must be earlier than current snapshot")
    if gap_minutes > max_gap_minutes:
        logger.warning(
            "structure_continuity_gap symbol=%s previous=%s current=%s gap_minutes=%.3f; "
            "using explicit full-session replay",
            previous.symbol,
            previous.snapshot_time,
            snap_time,
            gap_minutes,
        )
        return False
    return True


def _load_previous_structure_snapshot(
    *,
    symbol: str,
    snap_time: datetime,
    symbol_snapshot: Optional[SnapshotSchema],
) -> Optional[SnapshotSchema]:
    """Load and validate the immediately previous same-day snapshot."""
    if _usable_previous_snapshot_for_structure(symbol_snapshot, snap_time):
        return symbol_snapshot

    db_payload = SnapshotSchema.fetch_latest_today_payload_before_time(
        symbol,
        snap_time.replace(tzinfo=None) if snap_time.tzinfo is not None else snap_time,
    )
    db_snapshot = _parse_snapshot_payload(db_payload)
    if _usable_previous_snapshot_for_structure(db_snapshot, snap_time):
        return db_snapshot
    return None


class SnapshotGenerator:
    def __init__(self, token: int, symbol: str, api_key: str, access_token: str):
        self.token = token
        self.symbol = symbol
        self.fetcher = SnapshotFetcher(
            api_key=api_key,
            access_token=access_token,
        )

    def generate_snapshot(
        self,
        end_date: Optional[datetime] = None,
        persist_snapshot: bool = True,
    ) -> Optional[SnapshotSchema]:
        total_started = time.perf_counter()
        if end_date is None:
            end_date = datetime.now(IST)

        assemble_started = time.perf_counter()
        snap_dict = self._assemble(end_date)
        assemble_ms = (time.perf_counter() - assemble_started) * 1000.0

        if not snap_dict:
            return None

        validation_started = time.perf_counter()
        snapshot = SnapshotSchema.model_validate(snap_dict)
        validation_ms = (time.perf_counter() - validation_started) * 1000.0

        snapshot_write_ms = 0.0
        symbol_cache_write_ms = 0.0
        if persist_snapshot:
            last_snap_payload = snapshot.model_dump(mode="json", by_alias=True)

            write_started = time.perf_counter()
            # Single idempotent persistence path. Persistence failures are
            # fatal; the service must not continue with a cache-only snapshot.
            SnapshotSchema.create_snapshot(snapshot)
            snapshot_write_ms = (time.perf_counter() - write_started) * 1000.0

            if snapshot.ltp is None or snapshot.ltp_time is None:
                raise ValueError("Validated snapshot is missing ltp/ltp_time DB fields")
            cache_started = time.perf_counter()
            SymbolSchema.update_symbol(
                snapshot.symbol,
                {
                    "price": snapshot.ltp,
                    "last_time": snapshot.ltp_time,
                    "last_snapshot": last_snap_payload,
                },
            )
            symbol_cache_write_ms = (time.perf_counter() - cache_started) * 1000.0

        total_ms = (time.perf_counter() - total_started) * 1000.0
        logger.debug(
            "snapshot_timing symbol=%s snapshot_time=%s total_ms=%.3f "
            "assemble_ms=%.3f validation_ms=%.3f snapshot_write_ms=%.3f "
            "symbol_cache_write_ms=%.3f",
            snapshot.symbol,
            snapshot.snapshot_time,
            total_ms,
            assemble_ms,
            validation_ms,
            snapshot_write_ms,
            symbol_cache_write_ms,
        )
        return snapshot

    def _assemble(self, end: datetime) -> Optional[Dict[str, Any]]:
        assemble_started = time.perf_counter()
        generation_timings: Dict[str, Any] = {}
        fetch_started = time.perf_counter()
        try:
            raw = self.fetcher.fetch(
                self.token,
                end,
                BASE_INTERVAL,
                lookback_days=LOOKBACK_DAYS,
            )
        except (TokenException, InputException):
            raise
        except Exception:
            logger.exception("Historical fetch failed for %s", self.symbol)
            return None
        generation_timings["historical_fetch_ms"] = round(
            (time.perf_counter() - fetch_started) * 1000.0, 3
        )

        market_started = time.perf_counter()
        df1 = pd.DataFrame(raw)

        if df1.empty or "date" not in df1.columns:
            return None

        df1["date"] = pd.to_datetime(df1["date"])

        if df1["date"].dt.tz is None:
            df1["date"] = df1["date"].dt.tz_localize(IST)
        else:
            df1["date"] = df1["date"].dt.tz_convert(IST)

        df1 = df1.sort_values("date")
        base_ohlcv = df1[["date", "open", "high", "low", "close", "volume"]].copy()

        df3 = aggregate_ohlcv(base_ohlcv, minutes=TF_PRIMARY_MIN)
        df15 = aggregate_ohlcv(base_ohlcv, minutes=TF_15_MIN)

        if df3.empty:
            return None

        df3i = IndicatorComputer.add_indicators(df3)
        df3i = df3i.sort_values("date").reset_index(drop=True)

        last = _select_latest_stable(df3i, end, period_minutes=TF_PRIMARY_MIN)

        if last is None:
            return None

        close_now = float(last["close"])

        snap_ts = pd.to_datetime(last["date"])
        if getattr(snap_ts, "tzinfo", None) is None:
            snap_ts = snap_ts.tz_localize(IST)
        else:
            snap_ts = snap_ts.tz_convert(IST)

        snap_time = snap_ts.to_pydatetime()

        prev_day = compute_prev_day_ohlc(base_ohlcv, snap_time)
        initial_accepted_range = compute_initial_accepted_range(base_ohlcv, snap_time)
        day_open = compute_today_open(base_ohlcv, snap_time)
        opening_range = compute_opening_range_15m(base_ohlcv, snap_time)
        hma_ss = _derive_hma_state_strength(last)

        vwap_val = _optional_float(last, "vwap")
        px_vs_vwap_pct = compute_px_vs_vwap_pct(close_now, vwap_val)

        boll = {
            "upper": _optional_float(last, "bb_upper"),
            "mid": _optional_float(last, "bb_mid"),
            "lower": _optional_float(last, "bb_lower"),
            "bb_width": _optional_float(last, "bb_width"),
        }

        bb_extra = compute_bollinger_position_zone(
            close_now,
            boll["upper"],
            boll["lower"],
        )

        boll["position"] = bb_extra["position"]
        boll["zone"] = bb_extra["zone"]

        envelopes = compute_envelopes(last.to_dict())

        df3_stable = (
            df3[df3["date"] <= snap_time].copy()
            if not df3.empty
            else pd.DataFrame()
        )

        df15_stable = (
            df15[df15["date"] <= snap_time].copy()
            if not df15.empty
            else pd.DataFrame()
        )

        levels = {
            "prev_day": _normalise_prev_day(prev_day),
            "today": {"open": day_open},
            "opening_range": _normalise_opening_range(opening_range),
        }
        if initial_accepted_range is not None:
            initial_structure_range = initial_accepted_range
        elif prev_day is not None:
            initial_structure_range = prev_day
        else:
            initial_structure_range = {}
        structure_levels = {
            **levels,
            "initial_accepted_range": initial_structure_range,
        }

        rsi_val = _optional_float(last, "rsi")
        adx_val = _optional_float(last, "adx")
        atr_val = _optional_float(last, "atr")

        context = {
            "rsi": {"value": rsi_val, "zone": classify_rsi_zone(rsi_val)},
            "adx": {"value": adx_val, "band": classify_adx_band(adx_val)},
            "atr": {"value": atr_val, "band": classify_atr_band(atr_val, close_now)},
        }

        hma = {
            "fast": _optional_float(last, "hmafast"),
            "mid1": _optional_float(last, "hmamid1"),
            "mid2": _optional_float(last, "hmamid2"),
            "slow": _optional_float(last, "hmaslow"),
            "state": hma_ss["state"],
            "strength": hma_ss["strength"],
            "flip_count_today": 0,
        }

        ema = {
            "fast": _optional_float(last, "ema_fast"),
            "mid1": _optional_float(last, "ema_mid1"),
            "mid2": _optional_float(last, "ema_mid2"),
            "slow": _optional_float(last, "ema_slow"),
            "ref": _optional_float(last, "ema_ref"),
        }

        vwap = {
            "value": vwap_val,
            "px_vs_vwap_pct": px_vs_vwap_pct,
        }

        vol_block = compute_volume_metrics(
            df_tf=df3_stable if not df3_stable.empty else df3,
            df_1m=base_ohlcv,
            asof=snap_time,
        )
        if not isinstance(vol_block, dict):
            raise ValueError("Volume calculation did not return a mapping")

        volume = {
            "bar_volume": vol_block["bar_volume"],
            "bar_rvol": vol_block["bar_rvol"],
            "bar_rvol_pct": vol_block["bar_rvol_pct"],
            "bar_rvol_band": vol_block["bar_rvol_band"],
            "bar_volume_slope": vol_block["bar_volume_slope"],
            "today_cum": vol_block["today_cum"],
            "prev_day_total": vol_block["prev_day_total"],
            "today_vs_prev_ratio": vol_block["today_vs_prev_ratio"],
            "periods": vol_block["periods"],
        }

        bar = {
            "open": float(last["open"]),
            "high": float(last["high"]),
            "low": float(last["low"]),
            "close": close_now,
            "volume": float(last["volume"]),
        }

        price_action = build_price_action_block(
            df_tf=df3_stable if not df3_stable.empty else df3,
            asof=snap_time,
            close=close_now,
            atr=atr_val,
            today_open=day_open,
            opening_range=opening_range,
            vwap_value=vwap_val,
        )

        vwap_distance_points = None if vwap_val is None else close_now - vwap_val
        vwap_distance_atr = (
            None
            if vwap_distance_points is None or atr_val in (None, 0)
            else float(vwap_distance_points / atr_val)
        )
        if px_vs_vwap_pct is None or px_vs_vwap_pct == 0:
            vwap_side = "AT"
        elif px_vs_vwap_pct > 0:
            vwap_side = "ABOVE"
        else:
            vwap_side = "BELOW"
        atr_pct = None if atr_val is None or atr_val == 0 else float(atr_val / close_now * 100.0)

        indicators = {
            "ema": ema,
            "hma": hma,
            "vwap": {
                "value": vwap_val,
                "side": vwap_side,
                "distance_pct": px_vs_vwap_pct,
                "distance_points": vwap_distance_points,
                "distance_atr": vwap_distance_atr,
                "flip_count_today": 0,
            },
            "rsi": context["rsi"],
            "adx": context["adx"],
            "atr": {**context["atr"], "pct": atr_pct},
            "bollinger": boll,
            "envelopes": envelopes,
        }

        market_windows_full = build_market_windows_block(
            df3_stable if not df3_stable.empty else df3,
            snap_time,
            atr=atr_val,
        )
        market_windows = _compact_market_windows(market_windows_full)
        price_action = {"slope": price_action["slope"]}
        generation_timings["market_assembly_ms"] = round(
            (time.perf_counter() - market_started) * 1000.0, 3
        )

        structure_started = time.perf_counter()
        sym = SymbolSchema.fetch_symbol(self.symbol)
        if sym is None:
            raise ValueError(f"Symbol configuration is missing: {self.symbol}")
        symbol_snapshot = _symbol_last_snapshot(sym, snap_time)

        # Structure uses one explicit continuity mode. A valid immediately
        # previous snapshot advances incrementally; otherwise the current
        # session candles are replayed and the mode is logged. No field value is
        # substituted from an alternate JSON path.
        state_memory: Dict[str, Any] = {}
        structure = None

        # Range discovery excludes the latest completed candle, so retain one
        # additional row beyond the largest configured historical window.
        lookback_bars = max(
            int(SNAPSHOT_CONFIG.structure.lookback_bars),
            int(SNAPSHOT_CONFIG.structure.max_intraday_range_bars) + 1,
        )

        previous_structure_snapshot = _load_previous_structure_snapshot(
            symbol=self.symbol,
            snap_time=snap_time,
            symbol_snapshot=symbol_snapshot,
        )
        previous_state_memory = (
            _inflate_structure_state_memory(previous_structure_snapshot)
            if previous_structure_snapshot is not None
            else {}
        )
        previous_structure_memory = (
            previous_structure_snapshot.memory.structure
            if previous_structure_snapshot is not None
            else None
        )
        use_cached_structure = previous_structure_memory is not None
        structure_df3_used = pd.DataFrame()
        structure_df15_used = pd.DataFrame()

        if use_cached_structure:
            # Advance the bounded candle ring carried by the immediately
            # previous snapshot.  Structure no longer rediscovers its working
            # frame from the freshly fetched session history on every tick.
            structure_df3_used = _append_structure_current(
                previous_structure_memory.bars_3m,
                df3,
                snap_time=snap_time,
                limit=lookback_bars,
            )
            structure_df15_used = _append_structure_current(
                previous_structure_memory.bars_15m,
                df15,
                snap_time=snap_time,
                limit=3,
            )

            current_df3_stable = structure_df3_used
            current_df15_stable = structure_df15_used

            structure, state_memory = compute_structure_state_from_memory(
                px=close_now,
                df3=current_df3_stable if not current_df3_stable.empty else df3_stable,
                df15=current_df15_stable if not current_df15_stable.empty else df15_stable,
                levels=structure_levels,
                atr=atr_val,
                curr_snapshot_like={
                    "hma": hma,
                    "context": context,
                    "bollinger": boll,
                    "vwap": vwap,
                    "volume": vol_block,
                },
                prev_state_memory=previous_state_memory,
            )

            logger.debug(
                "structure_update symbol=%s snapshot_time=%s mode=INCREMENTAL_PREVIOUS_SNAPSHOT previous=%s",
                self.symbol,
                snap_time,
                previous_structure_snapshot.snapshot_time,
            )

        else:
            logger.debug(
                "structure_update symbol=%s snapshot_time=%s mode=FULL_SESSION_REPLAY",
                self.symbol,
                snap_time,
            )

            replay_df3 = (
                df3i[
                    (df3i["date"] <= snap_time)
                    & (df3i["date"].dt.date == snap_time.date())
                ]
                .copy()
                .reset_index(drop=True)
            )

            replay_df3_base = (
                df3[
                    (df3["date"] <= snap_time)
                    & (df3["date"].dt.date == snap_time.date())
                ]
                .copy()
                .reset_index(drop=True)
                if not df3.empty
                else pd.DataFrame()
            )

            replay_df15_base = (
                df15[
                    (df15["date"] <= snap_time)
                    & (df15["date"].dt.date == snap_time.date())
                ]
                .copy()
                .reset_index(drop=True)
                if not df15.empty
                else pd.DataFrame()
            )

            for i, (_, row) in enumerate(replay_df3.iterrows()):
                row_close = float(row["close"])

                row_date = pd.to_datetime(row["date"])
                if getattr(row_date, "tzinfo", None) is None:
                    row_date = row_date.tz_localize(IST)
                else:
                    row_date = row_date.tz_convert(IST)
                row_time = row_date.to_pydatetime()

                row_hma_ss = _derive_hma_state_strength(row)
                row_hma = {
                    "fast": _optional_float(row, "hmafast"),
                    "mid1": _optional_float(row, "hmamid1"),
                    "mid2": _optional_float(row, "hmamid2"),
                    "slow": _optional_float(row, "hmaslow"),
                    "state": row_hma_ss["state"],
                    "strength": row_hma_ss["strength"],
                }

                row_vwap_val = _optional_float(row, "vwap")
                row_vwap = {
                    "value": row_vwap_val,
                    "px_vs_vwap_pct": compute_px_vs_vwap_pct(row_close, row_vwap_val),
                }

                row_boll = {
                    "upper": _optional_float(row, "bb_upper"),
                    "mid": _optional_float(row, "bb_mid"),
                    "lower": _optional_float(row, "bb_lower"),
                    "bb_width": _optional_float(row, "bb_width"),
                }
                row_bb_extra = compute_bollinger_position_zone(
                    row_close,
                    row_boll["upper"],
                    row_boll["lower"],
                )
                row_boll["position"] = row_bb_extra["position"]
                row_boll["zone"] = row_bb_extra["zone"]

                row_rsi = _optional_float(row, "rsi")
                row_adx = _optional_float(row, "adx")
                row_atr = _optional_float(row, "atr")
                row_context = {
                    "rsi": {"value": row_rsi, "zone": classify_rsi_zone(row_rsi)},
                    "adx": {"value": row_adx, "band": classify_adx_band(row_adx)},
                    "atr": {"value": row_atr, "band": classify_atr_band(row_atr, row_close)},
                }

                bounded_start = max(0, i + 1 - lookback_bars)
                replay_df3_stable = (
                    replay_df3_base.iloc[bounded_start : i + 1]
                    if not replay_df3_base.empty
                    else df3_stable
                )
                replay_df15_stable = (
                    replay_df15_base[replay_df15_base["date"] <= row_time].tail(3)
                    if not replay_df15_base.empty and "date" in replay_df15_base.columns
                    else df15_stable
                )

                row_opening_range = compute_opening_range_15m(base_ohlcv, row_time)
                row_levels = {
                    "prev_day": _normalise_prev_day(prev_day),
                    "initial_accepted_range": initial_structure_range,
                    "today": {"open": day_open},
                    "opening_range": _normalise_opening_range(row_opening_range),
                }
                row_vol_block = {
                    "bar_volume": _optional_float(row, "volume"),
                    "bar_rvol": None,
                    "bar_rvol_pct": None,
                    "bar_rvol_band": "NA",
                    "bar_volume_slope": None,
                    "today_cum": None,
                    "prev_day_total": None,
                    "today_vs_prev_ratio": None,
                    "periods": {},
                }

                structure, state_memory = compute_structure_state_from_memory(
                    px=row_close,
                    df3=replay_df3_stable if not replay_df3_stable.empty else df3_stable,
                    df15=replay_df15_stable if not replay_df15_stable.empty else df15_stable,
                    levels=row_levels,
                    atr=row_atr,
                    curr_snapshot_like={
                        "hma": row_hma,
                        "context": row_context,
                        "bollinger": row_boll,
                        "vwap": row_vwap,
                        "volume": row_vol_block,
                    },
                    prev_state_memory=state_memory,
                )

        if structure is None:
            structure, state_memory = compute_structure_state_from_memory(
                px=close_now,
                df3=df3_stable if not df3_stable.empty else df3,
                df15=df15_stable if not df15_stable.empty else df15,
                levels=structure_levels,
                atr=atr_val,
                curr_snapshot_like={
                    "hma": hma,
                    "context": context,
                    "bollinger": boll,
                    "vwap": vwap,
                    "volume": vol_block,
                },
                prev_state_memory={},
            )

        if structure_df3_used.empty:
            structure_df3_used = (
                df3[
                    (df3["date"] <= snap_time)
                    & (df3["date"].dt.date == snap_time.date())
                ]
                .tail(lookback_bars)
                .reset_index(drop=True)
                if not df3.empty
                else df3_stable
            )
        if structure_df15_used.empty:
            structure_df15_used = (
                df15[
                    (df15["date"] <= snap_time)
                    & (df15["date"].dt.date == snap_time.date())
                ]
                .tail(3)
                .reset_index(drop=True)
                if not df15.empty
                else df15_stable
            )
        structure_memory = _build_structure_memory(
            structure_df3_used,
            structure_df15_used,
            snapshot_time=snap_time,
            lookback_bars=lookback_bars,
            state_memory=state_memory,
        )

        # Transition counters are projected directly into the domains that
        # own them. The generic state_context block is no longer persisted or
        # constructed.
        indicators["hma"]["flip_count_today"] = int(
            state_memory["hma.state"]["flip_count_today"]
        )
        indicators["vwap"]["flip_count_today"] = int(
            state_memory["vwap.side"]["flip_count_today"]
        )
        generation_timings["structure_ms"] = round(
            (time.perf_counter() - structure_started) * 1000.0, 3
        )

        # Derivatives are intentionally retained as-is in this patch because
        # strike-level display and option-selection consumers depend on them.
        derivatives_started = time.perf_counter()
        derivatives_spot_price = None
        derivatives_future = None
        derivatives_options_lite = None
        derivatives_option_ladder = None
        options_sentiment_windows = None
        future_sentiment_windows = None

        def _decoded_mapping(value: Any, *, field_name: str) -> Dict[str, Any]:
            if value is None or value == "":
                return {}
            if isinstance(value, str):
                parsed = json.loads(value)
                if not isinstance(parsed, dict):
                    raise ValueError(f"{field_name} must decode to an object")
                return parsed
            if isinstance(value, dict):
                return value
            raise TypeError(f"{field_name} must be an object or JSON object string")

        equity_ref = (
            self.symbol
            if getattr(sym, "type") == "EQ"
            else getattr(sym, "equity_ref")
        )
        if equity_ref:
            asof = snap_time.astimezone(IST).replace(
                tzinfo=None,
                second=0,
                microsecond=0,
            )
            chain_obj = DerivativesChainSchema.fetch_latest_today_for_symbol_before_time(
                equity_ref,
                asof,
            )
            if chain_obj is not None:
                raw_chain = _decoded_mapping(
                    getattr(chain_obj, "raw"),
                    field_name="derivatives.raw",
                )
                derived = _decoded_mapping(
                    getattr(chain_obj, "derived"),
                    field_name="derivatives.derived",
                )
                derivatives_spot_price = (
                    raw_chain["spot_price"] if "spot_price" in raw_chain else None
                )
                future_value = raw_chain["future"] if "future" in raw_chain else None
                derivatives_future = (
                    future_value if isinstance(future_value, dict) else None
                )
                options_value = (
                    derived["options_lite"] if "options_lite" in derived else None
                )
                derivatives_options_lite = (
                    options_value if isinstance(options_value, dict) else None
                )
                ladder_value = (
                    derived["option_ladder"] if "option_ladder" in derived else None
                )
                derivatives_option_ladder = (
                    ladder_value if isinstance(ladder_value, dict) else None
                )
                option_sentiment_value = (
                    derived["option_sentiment_windows"]
                    if "option_sentiment_windows" in derived
                    else None
                )
                options_sentiment_windows = (
                    option_sentiment_value
                    if isinstance(option_sentiment_value, dict)
                    else None
                )
                future_sentiment_value = (
                    derived["future_sentiment_windows"]
                    if "future_sentiment_windows" in derived
                    else None
                )
                future_sentiment_windows = (
                    future_sentiment_value
                    if isinstance(future_sentiment_value, dict)
                    else None
                )
        generation_timings["derivatives_ms"] = round(
            (time.perf_counter() - derivatives_started) * 1000.0, 3
        )

        gen_signals = bool(getattr(sym, "generate_signals"))
        derivatives = {
            "spot_price": derivatives_spot_price,
            "future": derivatives_future,
            "options_lite": derivatives_options_lite,
            "option_ladder": derivatives_option_ladder,
            "option_sentiment_windows": options_sentiment_windows,
            "future_sentiment_windows": future_sentiment_windows,
        }

        pre_auction_payload = {
            "symbol": self.symbol,
            "snapshot_time": snap_time,
            "tf": "3m",
            "close": close_now,
            "bar": bar,
            "ltp": close_now,
            "ltp_time": snap_time,
            "gen_signals": gen_signals,
            "levels": levels,
            "indicators": indicators,
            "volume": volume,
            "market_windows": market_windows,
            "price_action": price_action,
            "structure": structure.model_dump(mode="python"),
            "derivatives": derivatives,
            "auction": empty_auction_block().model_dump(mode="python", by_alias=True),
            "memory": {
                "structure": structure_memory,
                "auction": initial_auction_memory(
                    self.symbol, snap_time
                ).model_dump(mode="python"),
            },
        }
        pre_auction_snapshot = SnapshotSchema.model_validate(pre_auction_payload)

        auction_started = time.perf_counter()
        auction_block, auction_memory = enrich_snapshot_with_auction(
            pre_auction_snapshot,
            previous_snapshot=previous_structure_snapshot,
        )
        generation_timings["auction_adapter_ms"] = round(
            (time.perf_counter() - auction_started) * 1000.0, 3
        )
        generation_timings["assemble_total_ms"] = round(
            (time.perf_counter() - assemble_started) * 1000.0, 3
        )
        logger.debug(
            "snapshot_component_timing symbol=%s snapshot_time=%s timings=%s",
            self.symbol,
            snap_time,
            generation_timings,
        )

        final_payload = pre_auction_snapshot.model_dump(mode="python", by_alias=True)
        final_payload["auction"] = auction_block.model_dump(mode="python", by_alias=True)
        final_payload["memory"]["auction"] = auction_memory.model_dump(mode="python")
        final_snapshot = SnapshotSchema.model_validate(final_payload)
        return final_snapshot.model_dump(mode="python", by_alias=True)

