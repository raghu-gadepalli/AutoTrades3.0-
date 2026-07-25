# services/snapshot_helper.py
# Snapshot helpers (3m primary, derived from 1m canonical series)

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from configs.snapshot_config import SNAPSHOT_CONFIG
from schemas.snapshot import (
    StructureBlock,
    StructureRangeBlock,
    BalanceMetricsBlock,
    RawStructureBlock,
    AcceptedStructureBlock,
    CandidateStructureBlock,
    RecentCloseObservationBlock,
    StructureAnchorBlock,
    BreakoutContextBlock,
)


# =============================================================================
# Config -> internal constants
# =============================================================================
IST = ZoneInfo("Asia/Kolkata")

VOL_SMA_N = SNAPSHOT_CONFIG.indicators.volume_period
VOL_SLOPE_N = SNAPSHOT_CONFIG.indicators.volume_slope_period

ORB_START_HHMM = SNAPSHOT_CONFIG.indicators.orb_start_hhmm
ORB_END_HHMM = SNAPSHOT_CONFIG.indicators.orb_end_hhmm
ORB_READY_HHMM = SNAPSHOT_CONFIG.indicators.orb_ready_hhmm


# -----------------------------
# Index helpers
# -----------------------------
def _ensure_dt_index(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    if "date" in df.columns:
        d = df.copy()
        d["date"] = pd.to_datetime(d["date"])

        if getattr(d["date"].dt, "tz", None) is None:
            d["date"] = d["date"].dt.tz_localize(IST)
        else:
            d["date"] = d["date"].dt.tz_convert(IST)

        return d.sort_values("date").set_index("date")

    if isinstance(df.index, pd.DatetimeIndex):
        d = df.copy().sort_index()
        if d.index.tz is None:
            d.index = d.index.tz_localize(IST)
        else:
            d.index = d.index.tz_convert(IST)
        return d

    raise ValueError("DataFrame must have 'date' column or DatetimeIndex")


def _ensure_asof(asof: datetime | None) -> datetime | None:
    if asof is None:
        return None
    if asof.tzinfo is None:
        return asof.replace(tzinfo=IST)
    return asof.astimezone(IST)


def _df_last_time(df: pd.DataFrame | None) -> datetime | None:
    if df is None or df.empty:
        return None
    try:
        d = _ensure_dt_index(df)
        if d.empty:
            return None
        ts = d.index[-1]
        return ts.to_pydatetime()
    except Exception:
        return None


# -----------------------------
# Candle aggregation
# -----------------------------
def aggregate_ohlcv(df_1m: pd.DataFrame, minutes: int) -> pd.DataFrame:
    if df_1m is None or df_1m.empty:
        return pd.DataFrame()

    d = _ensure_dt_index(df_1m)

    out_parts = []
    for day, g in d.groupby(d.index.date):
        day_start = pd.Timestamp(
            datetime.combine(day, datetime.min.time()),
            tz=IST,
        ).replace(hour=9, minute=15)

        # Candle timestamps in AutoTrades are treated as the *start* of the
        # completed candle.  For a 3-minute day, the last tradable candle starts
        # at 15:27 and becomes available at 15:30; there should not be a 15:30
        # candle row.  Filter the regular session to [09:15, 15:30) and resample
        # with left labels/left-closed buckets so timestamps remain candle-start
        # aligned.
        session_start = day_start
        session_end = day_start.replace(hour=15, minute=30)
        g_session = g[(g.index >= session_start) & (g.index < session_end)]

        if g_session.empty:
            continue

        r = (
            g_session.resample(
                f"{int(minutes)}min",
                origin=day_start,
                label="left",
                closed="left",
            )
            .agg(
                {
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum",
                }
            )
            .dropna(subset=["open", "high", "low", "close"])
        )

        out_parts.append(r)

    if not out_parts:
        return pd.DataFrame()

    return pd.concat(out_parts).sort_index().reset_index().rename(columns={"index": "date"})








# -----------------------------
# Strategy-neutral price action evidence
# -----------------------------
def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or pd.isna(v):
            return None
        return float(v)
    except Exception:
        return None


def _safe_atr(atr: Any) -> Optional[float]:
    x = _safe_float(atr)
    if x is None or x <= 0:
        return None
    return x


def _pct_move(points: Optional[float], ref: Optional[float]) -> Optional[float]:
    if points is None or ref is None or ref == 0:
        return None
    return (points / ref) * 100.0


def _atr_move(points: Optional[float], atr: Optional[float]) -> Optional[float]:
    if points is None or atr is None or atr <= 0:
        return None
    return points / atr


def _position_distance_from_band(
    px: Optional[float],
    low: Optional[float],
    high: Optional[float],
    atr: Optional[float],
) -> Dict[str, Any]:
    out = {"position": "UNKNOWN", "distance_points": None, "distance_atr": None}
    if px is None or low is None or high is None:
        return out

    if px > high:
        pts = px - high
        pos = "ABOVE"
    elif px < low:
        pts = low - px
        pos = "BELOW"
    else:
        pts = 0.0
        pos = "INSIDE"

    out["position"] = pos
    out["distance_points"] = pts
    out["distance_atr"] = _atr_move(pts, atr)
    return out


def _position_distance_from_level(
    px: Optional[float],
    level: Optional[float],
    atr: Optional[float],
) -> Dict[str, Any]:
    out = {"position": "UNKNOWN", "distance_points": None, "distance_atr": None}
    if px is None or level is None:
        return out

    pts = px - level
    out["position"] = "ABOVE" if pts > 0 else "BELOW" if pts < 0 else "AT_LEVEL"
    out["distance_points"] = pts
    out["distance_atr"] = _atr_move(pts, atr)
    return out


def _as_dt_indexed_tf(df_tf: pd.DataFrame | None, asof: datetime | None) -> pd.DataFrame:
    if df_tf is None or df_tf.empty or asof is None:
        return pd.DataFrame()
    try:
        d = _ensure_dt_index(df_tf)
        asof = _ensure_asof(asof)
        d = d[d.index <= asof].copy()
        if d.empty:
            return pd.DataFrame()
        # For price-action windows, use the current trading session only.
        return d[d.index.date == asof.date()].copy()
    except Exception:
        return pd.DataFrame()


def _window_move_block(
    d: pd.DataFrame,
    *,
    asof: datetime,
    close_now: Optional[float],
    atr: Optional[float],
    window_minutes: Optional[int],
    day_open: Optional[float] = None,
) -> Dict[str, Any]:
    out = {
        "status": "na",
        "session_elapsed_minutes": None,
        "points": None,
        "pct": None,
        "atr": None,
    }
    if d is None or d.empty or close_now is None:
        return out

    first_ts = d.index[0].to_pydatetime()
    available = max(0.0, (asof - first_ts).total_seconds() / 60.0)
    out["session_elapsed_minutes"] = available

    if window_minutes is None:
        if day_open is None:
            return out
        ref_price = day_open
        out["status"] = "ok"
    else:
        if available < float(window_minutes):
            out["status"] = "warming_up"
            # Use earliest available close during warm-up so the field remains
            # informative without pretending a full window exists.
            ref_price = _safe_float(d.iloc[0].get("close"))
        else:
            target = asof - pd.Timedelta(minutes=int(window_minutes))
            prior = d[d.index <= target]
            if prior.empty:
                out["status"] = "warming_up"
                ref_price = _safe_float(d.iloc[0].get("close"))
            else:
                out["status"] = "ok"
                ref_price = _safe_float(prior.iloc[-1].get("close"))

    pts = None if ref_price is None else close_now - ref_price
    out["points"] = pts
    out["pct"] = _pct_move(pts, ref_price)
    out["atr"] = _atr_move(pts, atr)
    return out


def _net_slope_atr_per_bar(closes: list[float], atr: Optional[float]) -> tuple[Optional[float], Optional[float]]:
    if len(closes) < 2 or atr is None or atr <= 0:
        return None, None
    net_atr = (closes[-1] - closes[0]) / atr
    return net_atr, net_atr / float(len(closes) - 1)


def _slope_state(
    s3: Optional[float],
    s5: Optional[float],
    prev3: Optional[float],
    *,
    flat_epsilon: float,
) -> str:
    if s3 is None or s5 is None:
        return "UNKNOWN"

    # Use sign and relative gradient, not strategy-specific thresholds.
    eps = abs(float(flat_epsilon or 0.0))
    if abs(s3) < eps and abs(s5) < eps:
        return "FLAT"

    if s3 > 0 and s5 > 0:
        return "UP_ACCELERATING" if s3 > s5 else "UP_SLOWING"
    if s3 < 0 and s5 < 0:
        # More negative means downside acceleration.
        return "DOWN_ACCELERATING" if s3 < s5 else "DOWN_SLOWING"
    if s3 > 0 and s5 <= 0:
        return "TURNING_UP"
    if s3 < 0 and s5 >= 0:
        return "TURNING_DOWN"

    if prev3 is not None:
        if s3 > prev3:
            return "UP_ACCELERATING" if s3 > 0 else "DOWN_SLOWING"
        if s3 < prev3:
            return "DOWN_ACCELERATING" if s3 < 0 else "UP_SLOWING"
    return "FLAT"


def _slope_block(d: pd.DataFrame, *, atr: Optional[float]) -> Dict[str, Any]:
    out = {
        "status": "na",
        "bars_3_atr": None,
        "bars_5_atr": None,
        "bars_3_atr_per_bar": None,
        "bars_5_atr_per_bar": None,
        "previous_3_atr_per_bar": None,
        "state": "UNKNOWN",
    }
    if d is None or d.empty or atr is None:
        return out

    pa_cfg = SNAPSHOT_CONFIG.price_action
    fast_bars = max(2, int(pa_cfg.slope_fast_bars or 3))
    slow_bars = max(fast_bars, int(pa_cfg.slope_slow_bars or 5))
    prev_bars = max(2, int(pa_cfg.slope_previous_bars or fast_bars))
    needed_bars = max(slow_bars, fast_bars + prev_bars)

    closes = [_safe_float(x) for x in d["close"].tail(needed_bars).tolist()]
    closes = [x for x in closes if x is not None]
    if len(closes) < fast_bars:
        out["status"] = "warming_up"
        return out

    out["status"] = "ok" if len(closes) >= slow_bars else "warming_up"

    net3, per3 = _net_slope_atr_per_bar(closes[-fast_bars:], atr)
    out["bars_3_atr"] = net3
    out["bars_3_atr_per_bar"] = per3

    if len(closes) >= slow_bars:
        net5, per5 = _net_slope_atr_per_bar(closes[-slow_bars:], atr)
        out["bars_5_atr"] = net5
        out["bars_5_atr_per_bar"] = per5
    else:
        per5 = None

    if len(closes) >= fast_bars + prev_bars:
        prev_start = -(fast_bars + prev_bars)
        prev_end = -fast_bars
        _, prev3 = _net_slope_atr_per_bar(closes[prev_start:prev_end], atr)
        out["previous_3_atr_per_bar"] = prev3
    else:
        prev3 = None

    out["state"] = _slope_state(per3, per5, prev3, flat_epsilon=pa_cfg.slope_flat_epsilon)
    return out


def build_price_action_block(
    df_tf: pd.DataFrame,
    asof: datetime,
    *,
    close: Any,
    atr: Any,
    today_open: Any = None,
    opening_range: Optional[Dict[str, Any]] = None,
    vwap_value: Any = None,
) -> Dict[str, Any]:
    """Build strategy-neutral price-action evidence for the snapshot.

    This is intentionally not a breakout/reversal decision helper.  It stores
    reusable facts: location vs ORB/VWAP, movement windows, and recent slope.
    Thesis helpers can consume these facts differently.
    """
    asof = _ensure_asof(asof)
    close_now = _safe_float(close)
    atr_val = _safe_atr(atr)
    day_open = _safe_float(today_open)
    vwap_val = _safe_float(vwap_value)
    opening_range = opening_range or {}

    d = _as_dt_indexed_tf(df_tf, asof)

    orb_low = _safe_float(opening_range.get("low"))
    orb_high = _safe_float(opening_range.get("high"))
    orb_ready = bool(opening_range.get("ready"))

    pa_cfg = SNAPSHOT_CONFIG.price_action
    windows = dict(pa_cfg.movement_windows_minutes or {})

    out: Dict[str, Any] = {
        "orb": {"position": "UNKNOWN", "distance_points": None, "distance_atr": None},
        "vwap": {"position": "UNKNOWN", "distance_points": None, "distance_atr": None},
        "moves": {"sod": {"status": "na", "session_elapsed_minutes": None, "points": None, "pct": None, "atr": None}},
        "slope": {"status": "na", "bars_3_atr": None, "bars_5_atr": None, "bars_3_atr_per_bar": None, "bars_5_atr_per_bar": None, "previous_3_atr_per_bar": None, "state": "UNKNOWN"},
    }
    for name in windows.keys():
        out["moves"][str(name)] = {"status": "na", "session_elapsed_minutes": None, "points": None, "pct": None, "atr": None}

    if close_now is None or asof is None:
        return out

    if orb_ready:
        out["orb"] = _position_distance_from_band(close_now, orb_low, orb_high, atr_val)

    out["vwap"] = _position_distance_from_level(close_now, vwap_val, atr_val)

    move_blocks = {
        "sod": _window_move_block(d, asof=asof, close_now=close_now, atr=atr_val, window_minutes=None, day_open=day_open),
    }
    for name, minutes in windows.items():
        move_blocks[str(name)] = _window_move_block(
            d,
            asof=asof,
            close_now=close_now,
            atr=atr_val,
            window_minutes=int(minutes),
        )
    out["moves"] = move_blocks
    out["slope"] = _slope_block(d, atr=atr_val)

    return out


# -----------------------------
# Day / opening range levels
# -----------------------------
def compute_prev_day_ohlc(df_1m: pd.DataFrame, asof: datetime) -> dict | None:
    if df_1m is None or df_1m.empty:
        return None

    d = _ensure_dt_index(df_1m)
    asof = _ensure_asof(asof)
    today = asof.date()

    days = sorted({ts.date() for ts in d.index if ts.date() < today})
    if not days:
        return None

    prev_day = days[-1]
    g = d[d.index.date == prev_day]
    if g.empty:
        return None

    return {
        "open": float(g["open"].iloc[0]),
        "high": float(g["high"].max()),
        "low": float(g["low"].min()),
        "close": float(g["close"].iloc[-1]),
    }


def compute_initial_accepted_range(df_1m: pd.DataFrame, asof: datetime) -> dict | None:
    """
    Compute the initial accepted range for today's structure engine.

    Full previous-day high/low is the fallback. Recent prior-day windows are
    checked shortest-first to approximate the previous day's final balance
    without replaying yesterday's structure state.

    Optional config knobs:
      SNAPSHOT_CONFIG.structure.prev_balance_threshold_pct = 10.0
      SNAPSHOT_CONFIG.structure.prev_balance_windows_minutes = [45, 90, 180]
    """
    if df_1m is None or df_1m.empty:
        return None

    d = _ensure_dt_index(df_1m)
    asof = _ensure_asof(asof)
    if asof is None:
        return None

    today = asof.date()
    days = sorted({ts.date() for ts in d.index if ts.date() < today})
    if not days:
        return None

    prev_day = days[-1]
    g = d[d.index.date == prev_day].sort_index()
    if g.empty:
        return None

    full_high = _safe_float(g["high"].max())
    full_low = _safe_float(g["low"].min())
    full_close = _safe_float(g["close"].iloc[-1])

    if full_high is None or full_low is None:
        return None

    full_width = float(full_high - full_low)
    fallback = {
        "high": full_high,
        "low": full_low,
        "source": "PDH_PDL",
        "start_time": g.index[0].to_pydatetime(),
        "end_time": g.index[-1].to_pydatetime(),
        "bars": int(len(g)),
        "narrowing_pct": 0.0,
        "window_minutes": None,
        "full_high": full_high,
        "full_low": full_low,
        "full_close": full_close,
    }

    if full_width <= 0:
        return fallback

    threshold_pct = float(SNAPSHOT_CONFIG.structure.prev_balance_threshold_pct)
    windows = SNAPSHOT_CONFIG.structure.prev_balance_windows_minutes

    try:
        windows = [int(x) for x in windows]
    except Exception:
        windows = [45, 90, 180]

    for minutes in windows:
        if minutes <= 0:
            continue

        window_start = g.index[-1] - pd.Timedelta(minutes=minutes)
        wg = g[g.index >= window_start]
        if wg.empty:
            continue

        high = _safe_float(wg["high"].max())
        low = _safe_float(wg["low"].min())
        if high is None or low is None:
            continue

        width = float(high - low)
        narrowing_pct = ((full_width - width) / full_width) * 100.0

        if narrowing_pct >= threshold_pct:
            return {
                "high": high,
                "low": low,
                "source": "PREV_BALANCE",
                "start_time": wg.index[0].to_pydatetime(),
                "end_time": wg.index[-1].to_pydatetime(),
                "bars": int(len(wg)),
                "narrowing_pct": float(narrowing_pct),
                "window_minutes": int(minutes),
                "full_high": full_high,
                "full_low": full_low,
                "full_close": full_close,
            }

    return fallback


def compute_today_open(df_1m: pd.DataFrame, asof: datetime) -> float | None:
    if df_1m is None or df_1m.empty:
        return None

    d = _ensure_dt_index(df_1m)
    asof = _ensure_asof(asof)
    g = d[d.index.date == asof.date()]

    if g.empty:
        return None

    return float(g["open"].iloc[0])


def compute_opening_range_15m(df_1m: pd.DataFrame, asof: datetime) -> dict:
    asof = _ensure_asof(asof)

    out = {
        "window": f"{ORB_START_HHMM[0]:02d}:{ORB_START_HHMM[1]:02d}-"
                  f"{ORB_END_HHMM[0]:02d}:{ORB_END_HHMM[1]:02d}",
        "high": None,
        "low": None,
        "ready": False,
    }

    if df_1m is None or df_1m.empty:
        return out

    d = _ensure_dt_index(df_1m)
    day = asof.date()

    start = asof.replace(
        hour=ORB_START_HHMM[0],
        minute=ORB_START_HHMM[1],
        second=0,
        microsecond=0,
    )
    end = asof.replace(
        hour=ORB_END_HHMM[0],
        minute=ORB_END_HHMM[1],
        second=0,
        microsecond=0,
    )

    g = d[(d.index >= start) & (d.index <= end) & (d.index.date == day)]
    if g.empty:
        return out

    ready_time = asof.replace(
        hour=ORB_READY_HHMM[0],
        minute=ORB_READY_HHMM[1],
        second=0,
        microsecond=0,
    )

    # Snapshot timestamps are candle-start labels.  The 09:27 three-minute
    # candle is completed and persisted at 09:30, when the 09:15-09:29 ORB is
    # fully known.  Use the candle completion time for readiness so ORB does
    # not become available one snapshot late.
    completion_time = asof + pd.Timedelta(minutes=int(SNAPSHOT_CONFIG.service.tick_minutes))
    out["ready"] = completion_time >= ready_time

    if out["ready"]:
        out["high"] = float(g["high"].max())
        out["low"] = float(g["low"].min())

    return out


# -----------------------------
# Moves / VWAP
# -----------------------------
def compute_moves(close: float, prev_close: float | None, day_open: float | None) -> dict:
    def pct(a, b):
        if a is None or b in (None, 0):
            return None
        return float((a - b) / b * 100.0)

    return {
        "pct_vs_prev_close": pct(close, prev_close),
        "pct_vs_day_open": pct(close, day_open),
    }


def compute_px_vs_vwap_pct(close: float | None, vwap: float | None) -> float | None:
    if close is None or vwap in (None, 0):
        return None
    return float((close - vwap) / vwap * 100.0)


# -----------------------------
# Envelopes
# -----------------------------
def compute_envelopes(last_row: dict) -> dict:
    close = last_row.get("close")

    def pct_abs(a, b):
        if close in (None, 0) or a is None or b is None:
            return None
        return float(abs(a - b) / close * 100.0)

    return {
        "hma_envelope": pct_abs(last_row.get("hmafast"), last_row.get("hmaslow")),
        "ema_envelope": pct_abs(last_row.get("ema_fast"), last_row.get("ema_ref")),
    }


# -----------------------------
# Strength / band classifiers
# -----------------------------
def classify_rsi_zone(val: float | None) -> str:
    if val is None or np.isnan(val):
        return "NA"

    z = SNAPSHOT_CONFIG.thresholds.rsi_zone or {}
    os_extreme = float(z.get("os_extreme", 20.0))
    os_ = float(z.get("os", 30.0))
    ob_ = float(z.get("ob", 70.0))
    ob_extreme = float(z.get("ob_extreme", 80.0))

    if val >= ob_extreme:
        return "OB_EXTREME"
    if val >= ob_:
        return "OB"
    if val <= os_extreme:
        return "OS_EXTREME"
    if val <= os_:
        return "OS"
    return "MID"


def classify_adx_band(val: float | None) -> str:
    if val is None or np.isnan(val):
        return "NA"

    b = SNAPSHOT_CONFIG.thresholds.adx_band or {}
    medium = float(b.get("medium", 20.0))
    strong = float(b.get("strong", 30.0))

    if val >= strong:
        return "STRONG"
    if val >= medium:
        return "MEDIUM"
    return "WEAK"


def classify_atr_band(atr: float | None, close: float | None) -> str:
    if atr is None or close in (None, 0) or np.isnan(atr):
        return "NA"

    atr_pct = (atr / close) * 100.0
    b = SNAPSHOT_CONFIG.thresholds.atr_pct_band or {}
    medium = float(b.get("medium", 0.70))
    strong = float(b.get("strong", 1.20))

    if atr_pct >= strong:
        return "STRONG"
    if atr_pct >= medium:
        return "MEDIUM"
    return "WEAK"


def classify_rvol_pct_band(rvol_pct: float | None) -> str:
    if rvol_pct is None or (isinstance(rvol_pct, float) and np.isnan(rvol_pct)):
        return "NA"

    b = SNAPSHOT_CONFIG.thresholds.rvol_pct_band or {}
    low = float(b.get("low", 60.0))
    high = float(b.get("high", 125.0))

    if rvol_pct < low:
        return "LOW"
    if rvol_pct > high:
        return "HIGH"
    return "MEDIUM"


# -----------------------------
# Bollinger
# -----------------------------
def compute_bb_position(px: float | None, upper: float | None, lower: float | None) -> float | None:
    if px is None or upper is None or lower is None:
        return None

    span = float(upper) - float(lower)
    if span == 0:
        return None

    return float((float(px) - float(lower)) / span)


def classify_bollinger_zone(bb_pos: float | None) -> str:
    if bb_pos is None or (isinstance(bb_pos, float) and np.isnan(bb_pos)):
        return "UNKNOWN"

    z = SNAPSHOT_CONFIG.thresholds.bollinger_pos_zone or {}
    near_lower = float(z.get("near_lower", 0.20))
    near_upper = float(z.get("near_upper", 0.80))

    if bb_pos > 1.0:
        return "ABOVE_BAND"
    if bb_pos < 0.0:
        return "BELOW_BAND"
    if bb_pos >= near_upper:
        return "NEAR_UPPER"
    if bb_pos <= near_lower:
        return "NEAR_LOWER"

    return "MID_RANGE"


def compute_bollinger_position_zone(px: float | None, upper: float | None, lower: float | None) -> dict:
    pos = compute_bb_position(px, upper, lower)
    return {
        "position": pos,
        "zone": classify_bollinger_zone(pos),
    }


# -----------------------------
# Volume helpers
# -----------------------------
def _linear_slope(y: np.ndarray) -> float | None:
    if y is None or len(y) < 2:
        return None

    x = np.arange(len(y), dtype=float)
    x_mean = x.mean()
    y_mean = np.nanmean(y)
    denom = ((x - x_mean) ** 2).sum()

    if denom == 0:
        return 0.0

    num = np.nansum((x - x_mean) * (y - y_mean))
    return float(num / denom)


def compute_volume_metrics(df_tf: pd.DataFrame, df_1m: pd.DataFrame, asof: datetime) -> dict:
    out = {
        "bar_volume": None,
        "bar_rvol": None,
        "bar_rvol_pct": None,
        "bar_rvol_band": "NA",
        "bar_volume_slope": None,
        "today_cum": None,
        "prev_day_total": None,
        "today_vs_prev_ratio": None,
        "periods": {"rvol_sma_n": VOL_SMA_N, "slope_n": VOL_SLOPE_N},
    }

    if df_tf is None or df_tf.empty:
        return out

    v = df_tf["volume"].to_numpy(dtype=float)
    out["bar_volume"] = float(v[-1]) if len(v) else None

    if len(v) >= VOL_SMA_N:
        sma = float(np.nanmean(v[-VOL_SMA_N:]))
        if sma not in (0.0, None) and not np.isnan(sma):
            rvol = float(v[-1] / sma)
            out["bar_rvol"] = rvol
            out["bar_rvol_pct"] = float(rvol * 100.0)
            out["bar_rvol_band"] = classify_rvol_pct_band(out["bar_rvol_pct"])

    if len(v) >= VOL_SLOPE_N:
        out["bar_volume_slope"] = _linear_slope(v[-VOL_SLOPE_N:])

    if df_1m is not None and not df_1m.empty:
        d1 = _ensure_dt_index(df_1m)
        asof = _ensure_asof(asof)
        day = asof.date()
        g_today = d1[d1.index.date == day]

        if not g_today.empty:
            out["today_cum"] = float(g_today["volume"].sum())

        days = sorted({ts.date() for ts in d1.index if ts.date() < day})
        if days:
            prev_day = days[-1]
            g_prev = d1[d1.index.date == prev_day]
            if not g_prev.empty:
                out["prev_day_total"] = float(g_prev["volume"].sum())

        if out["today_cum"] is not None and out["prev_day_total"] not in (None, 0):
            out["today_vs_prev_ratio"] = float(out["today_cum"] / out["prev_day_total"])

    return out


# =============================================================================
# Structure engine + generic state memory
# =============================================================================
def _safe_float(v):
    try:
        if v is None or pd.isna(v):
            return None
        return float(v)
    except Exception:
        return None


def pct_distance(a, b):
    a = _safe_float(a)
    b = _safe_float(b)

    if a is None or b in (None, 0):
        return None

    return float((a - b) / b * 100.0)


def classify_level_position(px, low=None, high=None) -> str:
    px = _safe_float(px)
    low = _safe_float(low)
    high = _safe_float(high)

    if px is None:
        return "UNKNOWN"
    if high is not None and px > high:
        return "ABOVE"
    if low is not None and px < low:
        return "BELOW"
    if high is None and low is None:
        return "UNKNOWN"

    return "INSIDE"


def compute_range_width_pct(high, low, ref_price):
    high = _safe_float(high)
    low = _safe_float(low)
    ref_price = _safe_float(ref_price)

    if high is None or low is None or ref_price in (None, 0):
        return None

    return float((high - low) / ref_price * 100.0)


def _recent_high_low(df_tf: pd.DataFrame, lookback: int) -> tuple[float | None, float | None]:
    if df_tf is None or df_tf.empty:
        return None, None

    d = df_tf.tail(max(1, int(lookback)))
    if d.empty:
        return None, None

    return (
        _safe_float(d["high"].max()),
        _safe_float(d["low"].min()),
    )


def _side_from_position(px, low, high) -> str:
    px = _safe_float(px)
    low = _safe_float(low)
    high = _safe_float(high)

    if px is None or low is None or high is None:
        return "NEUTRAL"

    mid = (high + low) / 2.0
    if px > mid:
        return "BUY"
    if px < mid:
        return "SELL"
    return "NEUTRAL"


def _as_dict_for_memory(x):
    if not x:
        return {}
    if isinstance(x, dict):
        return x
    if hasattr(x, "model_dump"):
        return x.model_dump(mode="python")
    if hasattr(x, "dict"):
        return x.dict()
    return {}


def _get_path(d: dict, path: str, default=None):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(part)
        if cur is None:
            return default
    return cur


def _vwap_side(v):
    try:
        if v is None:
            return "UNKNOWN"
        v = float(v)
        if v > 0:
            return "ABOVE"
        if v < 0:
            return "BELOW"
        return "AT"
    except Exception:
        return "UNKNOWN"


def _parse_hhmm(value: str, default: time) -> time:
    try:
        hh, mm = str(value).split(":")[:2]
        return time(int(hh), int(mm))
    except Exception:
        return default


def _session_phase(asof: datetime | None) -> str:
    asof = _ensure_asof(asof)
    if asof is None:
        return "UNKNOWN"

    # ``asof`` is the completed candle's start label.  Session policy applies
    # when that candle becomes observable to SignalGenerator.
    observation_time = asof + pd.Timedelta(minutes=int(SNAPSHOT_CONFIG.service.tick_minutes))
    t = observation_time.time()
    opening_start = _parse_hhmm(SNAPSHOT_CONFIG.structure.opening_start, time(9, 15))
    opening_end = _parse_hhmm(SNAPSHOT_CONFIG.structure.opening_end, time(9, 30))
    late_start = _parse_hhmm(SNAPSHOT_CONFIG.structure.late_start, time(14, 45))

    if opening_start <= t < opening_end:
        return "OPENING"
    if t >= late_start:
        return "LATE"
    return "ACTIVE"


def _today_slice(df: pd.DataFrame | None, asof: datetime | None) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    d = _ensure_dt_index(df)
    asof = _ensure_asof(asof) or _df_last_time(d)
    if asof is None:
        return d.reset_index().rename(columns={"index": "date"})
    out = d[(d.index.date == asof.date()) & (d.index <= asof)]
    return out.reset_index().rename(columns={"index": "date"})


def _range_block(
    *,
    high: float | None,
    low: float | None,
    ref_price: float | None,
    source: str,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    bars: int = 0,
    atr: float | None = None,
    range_id: str | None = None,
    version: int = 0,
    range_type: str = "UNKNOWN",
    established_at: datetime | None = None,
    evidence_cutoff: datetime | None = None,
    provisional: bool = False,
    breakout_eligible: bool = False,
) -> StructureRangeBlock:
    high_f = _safe_float(high)
    low_f = _safe_float(low)
    atr_f = _safe_atr(atr)
    width_atr = None
    if high_f is not None and low_f is not None and atr_f is not None:
        width_atr = max(0.0, high_f - low_f) / atr_f

    return StructureRangeBlock(
        range_id=range_id,
        version=max(0, int(version or 0)),
        high=high_f,
        low=low_f,
        width_pct=compute_range_width_pct(high_f, low_f, ref_price),
        width_atr=width_atr,
        source=source,
        range_type=range_type,
        start_time=start_time,
        end_time=end_time,
        established_at=established_at,
        evidence_cutoff=evidence_cutoff,
        bars=int(bars or 0),
        provisional=bool(provisional),
        breakout_eligible=bool(breakout_eligible),
    )


def _range_from_df(
    df: pd.DataFrame | None,
    ref_price: float | None,
    source: str,
    *,
    atr: float | None = None,
    range_id: str | None = None,
    version: int = 0,
    range_type: str = "UNKNOWN",
    established_at: datetime | None = None,
    evidence_cutoff: datetime | None = None,
    provisional: bool = False,
    breakout_eligible: bool = False,
) -> StructureRangeBlock:
    if df is None or df.empty:
        return _range_block(
            high=None,
            low=None,
            ref_price=ref_price,
            source=source,
            atr=atr,
            range_id=range_id,
            version=version,
            range_type=range_type,
            established_at=established_at,
            evidence_cutoff=evidence_cutoff,
            provisional=provisional,
            breakout_eligible=breakout_eligible,
        )

    d = _ensure_dt_index(df)
    return _range_block(
        high=_safe_float(d["high"].max()),
        low=_safe_float(d["low"].min()),
        ref_price=ref_price,
        source=source,
        start_time=d.index[0].to_pydatetime(),
        end_time=d.index[-1].to_pydatetime(),
        bars=len(d),
        atr=atr,
        range_id=range_id,
        version=version,
        range_type=range_type,
        established_at=established_at,
        evidence_cutoff=evidence_cutoff,
        provisional=provisional,
        breakout_eligible=breakout_eligible,
    )


def _dict_to_range_block(d: dict | None) -> StructureRangeBlock:
    d = d or {}
    return StructureRangeBlock(
        range_id=d.get("range_id"),
        version=int(d.get("version") or 0),
        high=_safe_float(d.get("high")),
        low=_safe_float(d.get("low")),
        width_pct=_safe_float(d.get("width_pct")),
        width_atr=_safe_float(d.get("width_atr")),
        source=str(d.get("source") or "UNKNOWN"),
        range_type=str(d.get("range_type") or "UNKNOWN"),
        start_time=d.get("start_time"),
        end_time=d.get("end_time"),
        established_at=d.get("established_at"),
        evidence_cutoff=d.get("evidence_cutoff"),
        bars=int(d.get("bars") or 0),
        provisional=bool(d.get("provisional", False)),
        breakout_eligible=bool(d.get("breakout_eligible", False)),
    )


def _dict_to_balance_metrics(d: dict | None) -> BalanceMetricsBlock:
    d = d or {}
    return BalanceMetricsBlock(
        adjacent_overlap_ratio=_safe_float(d.get("adjacent_overlap_ratio")),
        directional_efficiency=_safe_float(d.get("directional_efficiency")),
        net_displacement_fraction=_safe_float(d.get("net_displacement_fraction")),
        close_occupancy_ratio=_safe_float(d.get("close_occupancy_ratio")),
        midpoint_drift_atr=_safe_float(d.get("midpoint_drift_atr")),
        upper_boundary_drift_atr=_safe_float(d.get("upper_boundary_drift_atr")),
        lower_boundary_drift_atr=_safe_float(d.get("lower_boundary_drift_atr")),
        upper_interactions=int(d.get("upper_interactions") or 0),
        lower_interactions=int(d.get("lower_interactions") or 0),
        quality=_safe_float(d.get("quality")),
        classification=str(d.get("classification") or "UNKNOWN"),
        reason=d.get("reason"),
    )


def _previous_accepted(prev_memory: dict | None) -> AcceptedStructureBlock | None:
    d = ((prev_memory or {}).get("structure.accepted") or {}).get("value")
    if not isinstance(d, dict):
        return None
    try:
        return AcceptedStructureBlock(
            state=str(d.get("state") or "RANGE_ACCEPTED"),
            range=_dict_to_range_block(d.get("range") or {}),
            metrics=_dict_to_balance_metrics(d.get("metrics") or {}),
            age_bars=int(d.get("age_bars") or 0),
            frozen=bool(d.get("frozen", True)),
            promoted_time=d.get("promoted_time"),
            quality=_safe_float(d.get("quality")),
            reason=d.get("reason"),
        )
    except Exception:
        return None


def _previous_candidate(prev_memory: dict | None) -> CandidateStructureBlock | None:
    d = ((prev_memory or {}).get("structure.candidate") or {}).get("value")
    if not isinstance(d, dict):
        return None
    try:
        return CandidateStructureBlock(
            active=bool(d.get("active", False)),
            status=str(d.get("status") or ("QUALIFIED" if d.get("active") else "NONE")),
            side=str(d.get("side") or "NEUTRAL"),
            range=_dict_to_range_block(d.get("range") or {}),
            metrics=_dict_to_balance_metrics(d.get("metrics") or {}),
            bars_confirmed=int(d.get("bars_confirmed") or 0),
            first_seen_time=d.get("first_seen_time"),
            quality=_safe_float(d.get("quality")),
            reason=d.get("reason"),
        )
    except Exception:
        return None


def _memory_entry(raw_state: Any, prev_entry: dict | None = None, *, value: Any | None = None, confirm_count: int | None = None) -> dict:
    raw_state = str(raw_state) if raw_state is not None else "UNKNOWN"
    prev_entry = prev_entry or {}

    required = int(
        confirm_count
        if confirm_count is not None
        else SNAPSHOT_CONFIG.structure.candidate_state_required
    )

    stable_state = prev_entry.get("state")
    stable_count = int(prev_entry.get("count") or 0)
    candidate_state = prev_entry.get("candidate_state")
    candidate_count = int(prev_entry.get("candidate_count") or 0)
    previous_state = prev_entry.get("previous_state")
    previous_count = int(prev_entry.get("previous_count") or 0)
    flip_count_today = int(prev_entry.get("flip_count_today") or 0)

    if not stable_state:
        out = {
            "raw_state": raw_state,
            "state": raw_state,
            "count": 1,
            "previous_state": None,
            "previous_count": 0,
            "candidate_state": None,
            "candidate_count": 0,
            "flip_count_today": flip_count_today,
        }
    elif raw_state == stable_state:
        out = {
            "raw_state": raw_state,
            "state": stable_state,
            "count": stable_count + 1,
            "previous_state": previous_state,
            "previous_count": previous_count,
            "candidate_state": None,
            "candidate_count": 0,
            "flip_count_today": flip_count_today,
        }
    else:
        if raw_state == candidate_state:
            candidate_count += 1
        else:
            candidate_state = raw_state
            candidate_count = 1

        if candidate_count >= required:
            out = {
                "raw_state": raw_state,
                "state": raw_state,
                "count": candidate_count,
                "previous_state": stable_state,
                "previous_count": stable_count,
                "candidate_state": None,
                "candidate_count": 0,
                "flip_count_today": flip_count_today + 1,
            }
        else:
            out = {
                "raw_state": raw_state,
                "state": stable_state,
                "count": stable_count,
                "previous_state": previous_state,
                "previous_count": previous_count,
                "candidate_state": candidate_state,
                "candidate_count": candidate_count,
                "flip_count_today": flip_count_today,
            }

    if value is not None:
        if hasattr(value, "model_dump"):
            out["value"] = value.model_dump(mode="python")
        elif isinstance(value, dict):
            out["value"] = value
        else:
            out["value"] = value

    return out


def build_state_memory(
    curr_snapshot_like: dict,
    prev_snapshot_like: dict | None = None,
    *,
    confirm_count: int | None = None,
) -> dict:
    curr = _as_dict_for_memory(curr_snapshot_like)
    prev = _as_dict_for_memory(prev_snapshot_like)
    prev_memory = prev.get("state_memory") or {}

    raw_labels = {
        "structure.raw.state": _get_path(curr, "structure.raw.state"),
        "structure.raw.side": _get_path(curr, "structure.raw.side"),
        "structure.accepted.state": _get_path(curr, "structure.accepted.state"),
        "structure.candidate.active": _get_path(curr, "structure.candidate.active"),
        "structure.session_phase": _get_path(curr, "structure.session_phase"),

        "hma.state": _get_path(curr, "hma.state"),
        "hma.strength": _get_path(curr, "hma.strength"),

        "rsi.zone": _get_path(curr, "context.rsi.zone"),
        "adx.band": _get_path(curr, "context.adx.band"),
        "atr.band": _get_path(curr, "context.atr.band"),

        "bollinger.zone": _get_path(curr, "bollinger.zone"),
        "vwap.side": _vwap_side(_get_path(curr, "vwap.px_vs_vwap_pct")),
    }

    vol_band = _get_path(curr, "volume.bar_rvol_band")
    if vol_band is not None:
        raw_labels["volume.rvol_band"] = vol_band

    value_keys = {
        "structure.accepted": _get_path(curr, "structure.accepted"),
        "structure.candidate": _get_path(curr, "structure.candidate"),
    }

    memory: dict = {}
    for key, raw_state in raw_labels.items():
        if raw_state is None:
            continue
        memory[key] = _memory_entry(
            raw_state,
            prev_memory.get(key) or {},
            confirm_count=confirm_count,
        )

    for key, value in value_keys.items():
        if value is None:
            continue
        if key == "structure.candidate":
            raw = str(value.get("status") or ("ACTIVE" if bool(value.get("active")) else "INACTIVE"))
        else:
            raw = value.get("state") or "UNKNOWN"
        memory[key] = _memory_entry(
            raw,
            prev_memory.get(key) or {},
            value=value,
            confirm_count=1,
        )

    return memory


def _build_anchors(
    *,
    levels: dict,
    recent_swing_high: float | None,
    recent_swing_low: float | None,
    recent15_high: float | None,
    recent15_low: float | None,
    active_anchor: str,
) -> StructureAnchorBlock:
    prev_day = levels.get("prev_day") or {}
    opening_range = levels.get("opening_range") or {}

    orb_ready = bool(opening_range.get("ready"))

    return StructureAnchorBlock(
        pdh=_safe_float(prev_day.get("high")),
        pdl=_safe_float(prev_day.get("low")),
        orb_high=_safe_float(opening_range.get("high")) if orb_ready else None,
        orb_low=_safe_float(opening_range.get("low")) if orb_ready else None,
        orb_ready=orb_ready,
        recent15_high=recent15_high,
        recent15_low=recent15_low,
        active_anchor=active_anchor,
    )


def _range_overlap_ratio(
    low_a: float | None,
    high_a: float | None,
    low_b: float | None,
    high_b: float | None,
) -> float:
    vals = [_safe_float(x) for x in (low_a, high_a, low_b, high_b)]
    if any(x is None for x in vals):
        return 0.0
    la, ha, lb, hb = vals
    width_a = max(0.0, ha - la)
    width_b = max(0.0, hb - lb)
    denom = min(width_a, width_b)
    if denom <= 0:
        return 0.0
    overlap = max(0.0, min(ha, hb) - max(la, lb))
    return float(max(0.0, min(1.0, overlap / denom)))


def _adjacent_overlap_ratio(d: pd.DataFrame) -> float:
    if d is None or len(d) < 2:
        return 0.0
    overlaps = 0
    pairs = 0
    rows = d[["high", "low"]].astype(float).to_numpy()
    for i in range(1, len(rows)):
        prev_high, prev_low = rows[i - 1]
        high, low = rows[i]
        pairs += 1
        if min(prev_high, high) > max(prev_low, low):
            overlaps += 1
    return float(overlaps / pairs) if pairs else 0.0


def _classify_range_type(width_atr: float | None) -> str:
    if width_atr is None:
        return "UNKNOWN"
    cfg = SNAPSHOT_CONFIG.structure
    if width_atr < float(cfg.preferred_min_range_width_atr):
        return "MICRO_COMPRESSION"
    if width_atr <= float(cfg.preferred_max_range_width_atr):
        return "NORMAL_BALANCE"
    return "BROAD_BALANCE"


def _balance_metrics_for_window(d: pd.DataFrame, atr: float | None) -> BalanceMetricsBlock:
    cfg = SNAPSHOT_CONFIG.structure
    atr_f = _safe_atr(atr)
    if d is None or d.empty or len(d) < 2:
        return BalanceMetricsBlock(
            classification="INSUFFICIENT_DATA",
            reason="balance_window_has_fewer_than_two_bars",
            quality=0.0,
        )

    x = _ensure_dt_index(d)
    highs = x["high"].astype(float)
    lows = x["low"].astype(float)
    closes = x["close"].astype(float)

    high = float(highs.max())
    low = float(lows.min())
    width = max(0.0, high - low)
    width_atr = (width / atr_f) if atr_f else None

    overlap = _adjacent_overlap_ratio(x)
    close_changes = closes.diff().abs().dropna()
    path = float(close_changes.sum()) if not close_changes.empty else 0.0
    net = abs(float(closes.iloc[-1] - closes.iloc[0]))
    efficiency = (net / path) if path > 0 else 0.0
    displacement = (net / width) if width > 0 else 0.0

    zone_fraction = max(0.0, min(0.45, float(cfg.boundary_interaction_zone_fraction)))
    inner_low = low + width * zone_fraction
    inner_high = high - width * zone_fraction
    if width <= 0:
        occupancy = 0.0
        upper_interactions = 0
        lower_interactions = 0
    else:
        occupancy = float(((closes >= inner_low) & (closes <= inner_high)).mean())
        upper_zone = high - width * zone_fraction
        lower_zone = low + width * zone_fraction
        upper_interactions = int((highs >= upper_zone).sum())
        lower_interactions = int((lows <= lower_zone).sum())

    split = max(1, len(x) // 2)
    first = x.iloc[:split]
    second = x.iloc[split:]
    if second.empty:
        second = x.iloc[-1:]

    first_high = float(first["high"].max())
    first_low = float(first["low"].min())
    second_high = float(second["high"].max())
    second_low = float(second["low"].min())
    first_mid = (first_high + first_low) / 2.0
    second_mid = (second_high + second_low) / 2.0

    midpoint_drift = abs(second_mid - first_mid) / atr_f if atr_f else None
    upper_drift = abs(second_high - first_high) / atr_f if atr_f else None
    lower_drift = abs(second_low - first_low) / atr_f if atr_f else None

    min_width = float(cfg.min_range_width_atr)
    enough_width = width_atr is None or width_atr >= min_width
    overlap_ok = overlap >= float(cfg.min_adjacent_overlap_ratio)
    efficiency_ok = efficiency <= float(cfg.max_directional_efficiency)
    displacement_ok = displacement <= float(cfg.max_net_displacement_fraction)
    occupancy_ok = occupancy >= float(cfg.min_close_occupancy_ratio)
    interactions_ok = (
        upper_interactions >= int(cfg.min_boundary_interactions)
        and lower_interactions >= int(cfg.min_boundary_interactions)
    )
    midpoint_ok = midpoint_drift is None or midpoint_drift <= float(cfg.max_midpoint_drift_atr)
    boundaries_ok = (
        upper_drift is None
        or lower_drift is None
        or (
            upper_drift <= float(cfg.max_boundary_drift_atr)
            and lower_drift <= float(cfg.max_boundary_drift_atr)
        )
    )

    if not enough_width:
        classification = "NOISE"
        reason = "candidate_width_below_atr_noise_floor"
    elif efficiency > float(cfg.max_directional_efficiency) or displacement > float(cfg.max_net_displacement_fraction):
        direction = "UP" if closes.iloc[-1] > closes.iloc[0] else "DOWN" if closes.iloc[-1] < closes.iloc[0] else "FLAT"
        classification = f"TRENDING_{direction}" if direction != "FLAT" else "TRANSITION"
        reason = "candidate_has_directional_efficiency_or_displacement"
    elif not midpoint_ok or not boundaries_ok:
        classification = "EXPANDING_RANGE"
        reason = "candidate_boundaries_or_midpoint_still_drifting"
    elif all((overlap_ok, occupancy_ok, interactions_ok)):
        classification = "BALANCE_QUALIFIED"
        reason = "candidate_passed_balance_metrics"
    else:
        classification = "BALANCE_FORMING"
        reason = "candidate_has_partial_balance_characteristics"

    stability_score = 1.0
    if midpoint_drift is not None:
        stability_score *= max(0.0, 1.0 - midpoint_drift / max(float(cfg.max_midpoint_drift_atr), 1e-9))
    if upper_drift is not None and lower_drift is not None:
        avg_boundary_drift = (upper_drift + lower_drift) / 2.0
        stability_score *= max(0.0, 1.0 - avg_boundary_drift / max(float(cfg.max_boundary_drift_atr), 1e-9))

    interaction_target = max(2, int(cfg.min_boundary_interactions) * 2)
    interaction_score = min(1.0, (upper_interactions + lower_interactions) / float(interaction_target))

    quality = (
        25.0 * max(0.0, min(1.0, overlap))
        + 20.0 * max(0.0, min(1.0, 1.0 - efficiency))
        + 15.0 * max(0.0, min(1.0, 1.0 - displacement))
        + 15.0 * max(0.0, min(1.0, occupancy))
        + 15.0 * max(0.0, min(1.0, stability_score))
        + 10.0 * max(0.0, min(1.0, interaction_score))
    )

    if width_atr is not None:
        preferred_min = float(cfg.preferred_min_range_width_atr)
        preferred_max = float(cfg.preferred_max_range_width_atr)
        if width_atr < preferred_min:
            quality -= min(10.0, (preferred_min - width_atr) / max(preferred_min, 1e-9) * 10.0)
        elif width_atr > preferred_max:
            quality -= min(15.0, (width_atr - preferred_max) / max(preferred_max, 1e-9) * 10.0)

    return BalanceMetricsBlock(
        adjacent_overlap_ratio=float(overlap),
        directional_efficiency=float(efficiency),
        net_displacement_fraction=float(displacement),
        close_occupancy_ratio=float(occupancy),
        midpoint_drift_atr=midpoint_drift,
        upper_boundary_drift_atr=upper_drift,
        lower_boundary_drift_atr=lower_drift,
        upper_interactions=upper_interactions,
        lower_interactions=lower_interactions,
        quality=float(max(0.0, min(100.0, quality))),
        classification=classification,
        reason=reason,
    )


def _candidate_from_window(
    d: pd.DataFrame,
    *,
    px: float | None,
    atr: float | None,
    evidence_cutoff: datetime | None,
) -> CandidateStructureBlock:
    if d is None or d.empty:
        return CandidateStructureBlock(status="NONE", reason="no_candidate_window")
    x = _ensure_dt_index(d)
    metrics = _balance_metrics_for_window(x, atr)
    high = _safe_float(x["high"].max())
    low = _safe_float(x["low"].min())
    range_type = _classify_range_type((high - low) / atr if high is not None and low is not None and atr else None)
    start_time = x.index[0].to_pydatetime()
    end_time = x.index[-1].to_pydatetime()
    range_id = f"DYNAMIC:{start_time.isoformat()}:{end_time.isoformat()}"
    r = _range_block(
        high=high,
        low=low,
        ref_price=px,
        source="INTRADAY_BALANCE",
        start_time=start_time,
        end_time=end_time,
        bars=len(x),
        atr=atr,
        range_id=range_id,
        range_type=range_type,
        established_at=None,
        evidence_cutoff=evidence_cutoff,
        provisional=False,
        breakout_eligible=False,
    )
    return CandidateStructureBlock(
        active=False,
        status=metrics.classification,
        side=_side_from_position(px, low, high),
        range=r,
        metrics=metrics,
        bars_confirmed=0,
        first_seen_time=start_time,
        quality=metrics.quality,
        reason=metrics.reason,
    )


def _best_candidate_ending_at(
    history: pd.DataFrame,
    *,
    end_pos: int,
    px: float | None,
    atr: float | None,
) -> CandidateStructureBlock:
    cfg = SNAPSHOT_CONFIG.structure
    if history is None or history.empty or end_pos < 0:
        return CandidateStructureBlock(status="NONE", reason="no_history_for_candidate")

    x = _ensure_dt_index(history)
    end_pos = min(end_pos, len(x) - 1)
    min_bars = max(2, int(cfg.min_intraday_range_bars))
    max_bars = max(min_bars, int(cfg.max_intraday_range_bars))
    available = end_pos + 1
    if available < min_bars:
        return CandidateStructureBlock(
            status="INSUFFICIENT_DATA",
            bars_confirmed=0,
            reason="not_enough_historical_bars_for_balance",
        )

    candidates: list[CandidateStructureBlock] = []
    for bars in range(min_bars, min(max_bars, available) + 1):
        start_pos = end_pos - bars + 1
        window = x.iloc[start_pos : end_pos + 1]
        cand = _candidate_from_window(
            window,
            px=px,
            atr=atr,
            evidence_cutoff=x.index[end_pos].to_pydatetime(),
        )
        candidates.append(cand)

    qualified = [c for c in candidates if c.status == "BALANCE_QUALIFIED"]
    pool = qualified or candidates
    return max(
        pool,
        key=lambda c: (
            float(c.quality or 0.0),
            int(c.range.bars or 0),
            c.range.start_time or datetime.min.replace(tzinfo=IST),
        ),
    )


def _same_balance(a: CandidateStructureBlock | StructureRangeBlock | None, b: CandidateStructureBlock | StructureRangeBlock | None, atr: float | None) -> bool:
    if a is None or b is None:
        return False
    ra = a.range if isinstance(a, CandidateStructureBlock) else a
    rb = b.range if isinstance(b, CandidateStructureBlock) else b
    if ra.high is None or ra.low is None or rb.high is None or rb.low is None:
        return False
    atr_f = _safe_atr(atr)
    if atr_f is None:
        tolerance = max(abs(float(ra.high)) * 0.0005, 1e-9)
        midpoint_tolerance = tolerance
    else:
        tolerance = float(SNAPSHOT_CONFIG.structure.boundary_tolerance_atr) * atr_f
        midpoint_tolerance = float(SNAPSHOT_CONFIG.structure.midpoint_tolerance_atr) * atr_f
    mid_a = (float(ra.high) + float(ra.low)) / 2.0
    mid_b = (float(rb.high) + float(rb.low)) / 2.0
    overlap = _range_overlap_ratio(ra.low, ra.high, rb.low, rb.high)
    return (
        abs(float(ra.high) - float(rb.high)) <= tolerance
        and abs(float(ra.low) - float(rb.low)) <= tolerance
        and abs(mid_a - mid_b) <= midpoint_tolerance
        and overlap >= float(SNAPSHOT_CONFIG.structure.min_range_overlap_for_same_balance)
    )


def _find_latest_balance(
    history: pd.DataFrame,
    *,
    px: float | None,
    atr: float | None,
) -> CandidateStructureBlock:
    if history is None or history.empty:
        return CandidateStructureBlock(status="NONE", reason="no_previous_candles_for_dynamic_balance")
    x = _ensure_dt_index(history)
    current = _best_candidate_ending_at(x, end_pos=len(x) - 1, px=px, atr=atr)
    required = max(1, int(SNAPSHOT_CONFIG.structure.balance_stable_evaluations))

    if current.status != "BALANCE_QUALIFIED":
        current.active = False
        current.bars_confirmed = 0
        return current

    confirmations = 1
    first_seen = current.range.start_time
    for offset in range(1, required):
        prev_pos = len(x) - 1 - offset
        if prev_pos < 0:
            break
        previous = _best_candidate_ending_at(x, end_pos=prev_pos, px=px, atr=atr)
        if previous.status != "BALANCE_QUALIFIED" or not _same_balance(current, previous, atr):
            break
        confirmations += 1
        first_seen = min(
            [dt for dt in (first_seen, previous.range.start_time) if dt is not None],
            default=first_seen,
        )

    current.bars_confirmed = confirmations
    current.first_seen_time = first_seen
    if confirmations >= required:
        established_at = current.range.evidence_cutoff
        current.active = True
        current.status = "QUALIFIED"
        current.range.established_at = established_at
        current.range.breakout_eligible = True
        current.range.range_id = (
            f"DYNAMIC:{current.range.start_time.isoformat() if current.range.start_time else 'NA'}:"
            f"{established_at.isoformat() if established_at else 'NA'}"
        )
        current.reason = "dynamic_balance_stable_across_historical_cutoffs"
    else:
        current.active = False
        current.status = "STABILIZING"
        current.reason = "dynamic_balance_waiting_for_stable_historical_cutoff"
    return current


def _build_raw_structure(
    *,
    px: float,
    history_df3: pd.DataFrame,
    levels: dict,
    asof: datetime | None,
    atr: float | None,
) -> tuple[RawStructureBlock, CandidateStructureBlock, StructureAnchorBlock, BreakoutContextBlock]:
    cfg = SNAPSHOT_CONFIG.structure
    swing_lookback = int(cfg.swing_lookback)
    recent15_lookback = int(cfg.recent15_lookback or 5)

    today_history = _today_slice(history_df3, asof)
    base = today_history if not today_history.empty else history_df3
    candidate = _find_latest_balance(base, px=px, atr=atr)

    recent_swing_high, recent_swing_low = _recent_high_low(base, swing_lookback)
    recent15_high, recent15_low = _recent_high_low(base, recent15_lookback)

    raw_range = candidate.range
    raw_metrics = candidate.metrics
    state = raw_metrics.classification or candidate.status or "UNKNOWN"
    if candidate.status == "QUALIFIED":
        state = "BALANCE_QUALIFIED"
    side = _side_from_position(px, raw_range.low, raw_range.high)

    prev_day = levels.get("prev_day") or {}
    opening_range = levels.get("opening_range") or {}
    pdh = _safe_float(prev_day.get("high"))
    pdl = _safe_float(prev_day.get("low"))
    orb_ready = bool(opening_range.get("ready"))
    orb_high = _safe_float(opening_range.get("high")) if orb_ready else None
    orb_low = _safe_float(opening_range.get("low")) if orb_ready else None

    swing_pos = classify_level_position(px, recent_swing_low, recent_swing_high)
    orb_pos = classify_level_position(px, orb_low, orb_high) if orb_ready else "UNKNOWN"
    pdh_pdl_pos = classify_level_position(px, pdl, pdh)
    recent15_pos = classify_level_position(px, recent15_low, recent15_high)

    breakout_context = BreakoutContextBlock(
        swing=swing_pos,
        orb=orb_pos,
        pdh_pdl=pdh_pdl_pos,
        recent15=recent15_pos,
    )

    active_anchor = "DYNAMIC_RANGE" if candidate.active else "SWING"
    if pdh_pdl_pos in ("ABOVE", "BELOW"):
        active_anchor = "PDH_PDL"
    elif orb_pos in ("ABOVE", "BELOW"):
        active_anchor = "ORB"

    anchors = _build_anchors(
        levels=levels,
        recent_swing_high=recent_swing_high,
        recent_swing_low=recent_swing_low,
        recent15_high=recent15_high,
        recent15_low=recent15_low,
        active_anchor=active_anchor,
    )

    raw = RawStructureBlock(
        state=state,
        side=side,
        range=raw_range,
        metrics=raw_metrics,
        recent_swing_high=recent_swing_high,
        recent_swing_low=recent_swing_low,
        reason=candidate.reason,
    )
    return raw, candidate, anchors, breakout_context


def _opening_range_seed(
    *,
    px: float,
    levels: dict,
    session_df3: pd.DataFrame,
    asof: datetime | None,
    atr: float | None,
    version: int = 1,
) -> AcceptedStructureBlock | None:
    opening_range = levels.get("opening_range") or {}
    if not bool(opening_range.get("ready")):
        return None
    high = _safe_float(opening_range.get("high"))
    low = _safe_float(opening_range.get("low"))
    if high is None or low is None or high <= low:
        return None
    day = asof.date().isoformat() if asof else "UNKNOWN"
    session = _ensure_dt_index(session_df3)
    if asof is not None and not session.empty:
        session = session[(session.index.date == asof.date()) & (session.index <= asof)]
    orb_rows = session
    if not session.empty:
        start_h, start_m = SNAPSHOT_CONFIG.indicators.orb_start_hhmm
        end_h, end_m = SNAPSHOT_CONFIG.indicators.orb_end_hhmm
        times = session.index.time
        orb_rows = session[
            (times >= time(int(start_h), int(start_m)))
            & (times <= time(int(end_h), int(end_m)))
        ]
    start_time = orb_rows.index[0].to_pydatetime() if not orb_rows.empty else None
    end_time = orb_rows.index[-1].to_pydatetime() if not orb_rows.empty else asof
    bars = int(len(orb_rows))
    r = _range_block(
        high=high,
        low=low,
        ref_price=px,
        source="ORB",
        start_time=start_time,
        end_time=end_time,
        bars=bars,
        atr=atr,
        range_id=f"ORB:{day}",
        version=version,
        range_type="OPENING_RANGE",
        established_at=asof,
        evidence_cutoff=asof,
        provisional=False,
        breakout_eligible=bool(
            SNAPSHOT_CONFIG.structure.initial_accepted_seed_breakout_eligible
        ),
    )
    quality = _range_quality(r, None)
    return AcceptedStructureBlock(
        state="RANGE_ACCEPTED",
        range=r,
        metrics=BalanceMetricsBlock(classification="OPENING_RANGE", quality=quality),
        age_bars=1,
        frozen=True,
        promoted_time=asof,
        quality=quality,
        reason="seeded_from_completed_opening_range",
    )


def _provisional_session_seed(
    *,
    px: float,
    session_df3: pd.DataFrame,
    asof: datetime | None,
    atr: float | None,
) -> AcceptedStructureBlock:
    d = _today_slice(session_df3, asof)
    day = asof.date().isoformat() if asof else "UNKNOWN"
    r = _range_from_df(
        d,
        px,
        "PROVISIONAL_SESSION",
        atr=atr,
        range_id=f"PROVISIONAL:{day}",
        version=0,
        range_type="PROVISIONAL_SESSION",
        established_at=None,
        evidence_cutoff=asof,
        provisional=True,
        breakout_eligible=False,
    )
    return AcceptedStructureBlock(
        state="RANGE_PROVISIONAL",
        range=r,
        metrics=BalanceMetricsBlock(classification="PROVISIONAL_SESSION", quality=0.0),
        age_bars=1,
        frozen=False,
        promoted_time=None,
        quality=0.0,
        reason="running_session_range_until_orb_is_complete",
    )


def _initial_accepted(
    *,
    px: float,
    levels: dict,
    session_df3: pd.DataFrame,
    asof: datetime | None,
    atr: float | None,
) -> AcceptedStructureBlock:
    if SNAPSHOT_CONFIG.structure.initial_accepted_seed_source.upper() == "ORB":
        opening_seed = _opening_range_seed(
            px=px,
            levels=levels,
            session_df3=session_df3,
            asof=asof,
            atr=atr,
        )
        if opening_seed is not None:
            return opening_seed
    return _provisional_session_seed(px=px, session_df3=session_df3, asof=asof, atr=atr)


def _range_quality(r: StructureRangeBlock | None, metrics: BalanceMetricsBlock | None) -> float | None:
    if metrics is not None and metrics.quality is not None:
        return float(metrics.quality)
    if r is None or r.high is None or r.low is None:
        return None
    bars = max(0, int(r.bars or 0))
    width_atr = _safe_float(r.width_atr)
    bar_score = min(45.0, bars * 3.0)
    width_score = 25.0
    if width_atr is not None:
        if width_atr < float(SNAPSHOT_CONFIG.structure.min_range_width_atr):
            width_score = 0.0
        elif width_atr < float(SNAPSHOT_CONFIG.structure.preferred_min_range_width_atr):
            width_score = 20.0
        elif width_atr <= float(SNAPSHOT_CONFIG.structure.preferred_max_range_width_atr):
            width_score = 35.0
        else:
            width_score = 15.0
    return float(max(0.0, min(100.0, bar_score + width_score)))


def _post_establishment_observations(
    history: pd.DataFrame,
    r: StructureRangeBlock,
) -> pd.DataFrame:
    """Return causal observations completed after an accepted range existed.

    The previous implementation sliced the original range-formation window and
    therefore reported occupancy close to 1.0 by construction.  Replacement
    must instead use candles completed after ``established_at`` (or, for older
    persisted rows, after ``end_time``).
    """
    if history is None or history.empty:
        return pd.DataFrame()
    d = _ensure_dt_index(history)
    anchor = _ensure_asof(r.established_at or r.end_time)
    if anchor is None:
        return pd.DataFrame()
    d = d[d.index > anchor]
    lookback = max(1, int(SNAPSHOT_CONFIG.structure.replacement_recent_lookback_bars))
    return d.tail(lookback).copy()


def _close_occupancy_for_frame(d: pd.DataFrame, r: StructureRangeBlock) -> float | None:
    if d is None or d.empty or r.high is None or r.low is None:
        return None
    closes = d["close"].astype(float)
    return float(((closes >= float(r.low)) & (closes <= float(r.high))).mean())


def _range_contained_with_tolerance(
    inner: StructureRangeBlock,
    outer: StructureRangeBlock,
    atr: float | None,
) -> bool:
    if inner.high is None or inner.low is None or outer.high is None or outer.low is None:
        return False
    atr_f = _safe_atr(atr)
    if atr_f is None:
        tolerance = max(abs(float(outer.high)) * 0.0001, 1e-9)
    else:
        tolerance = float(SNAPSHOT_CONFIG.structure.nested_containment_tolerance_atr) * atr_f
    return bool(
        float(inner.low) >= float(outer.low) - tolerance
        and float(inner.high) <= float(outer.high) + tolerance
    )


def _candidate_replacement_allowed(
    candidate: CandidateStructureBlock,
    accepted: AcceptedStructureBlock,
    history: pd.DataFrame,
    atr: float | None,
) -> tuple[bool, dict[str, Any]]:
    diagnostics: dict[str, Any] = {
        "candidate_qualified": bool(candidate.active and candidate.status == "QUALIFIED"),
        "same_as_accepted": False,
        "post_establishment_observations": 0,
        "old_range_close_occupancy": None,
        "candidate_range_close_occupancy": None,
        "candidate_occupancy_advantage": None,
        "quality_delta": None,
        "range_overlap_ratio": None,
        "candidate_contained_in_accepted": False,
        "nested_refinement": False,
        "overlapping_evolution": False,
        "relocation_replacement": False,
        "replacement_reason": None,
    }
    if not candidate.active or candidate.status != "QUALIFIED":
        return False, diagnostics
    if _same_balance(candidate, accepted.range, atr):
        diagnostics["same_as_accepted"] = True
        return False, diagnostics
    if accepted.range.provisional:
        diagnostics["replacement_reason"] = "replace_provisional_range"
        return True, diagnostics

    candidate_quality = float(candidate.quality or 0.0)
    accepted_quality = float(accepted.quality or 0.0)
    delta = candidate_quality - accepted_quality
    diagnostics["quality_delta"] = delta

    accepted_width = None
    candidate_width = None
    if accepted.range.high is not None and accepted.range.low is not None:
        accepted_width = max(0.0, float(accepted.range.high) - float(accepted.range.low))
    if candidate.range.high is not None and candidate.range.low is not None:
        candidate_width = max(0.0, float(candidate.range.high) - float(candidate.range.low))

    width_ratio = candidate_width / accepted_width if accepted_width and candidate_width is not None else None
    overlap = _range_overlap_ratio(
        candidate.range.low,
        candidate.range.high,
        accepted.range.low,
        accepted.range.high,
    )
    contained = _range_contained_with_tolerance(candidate.range, accepted.range, atr)
    diagnostics["candidate_to_accepted_width_ratio"] = width_ratio
    diagnostics["range_overlap_ratio"] = overlap
    diagnostics["candidate_contained_in_accepted"] = contained

    quality_ok = delta >= float(SNAPSHOT_CONFIG.structure.quality_replacement_margin)
    nested_refinement = bool(
        contained
        and width_ratio is not None
        and width_ratio <= float(SNAPSHOT_CONFIG.structure.nested_range_max_width_ratio)
        and quality_ok
    )
    diagnostics["nested_refinement"] = nested_refinement
    if nested_refinement:
        diagnostics["replacement_reason"] = "contained_nested_refinement"
        return True, diagnostics

    recent = _post_establishment_observations(history, accepted.range)
    observation_count = int(len(recent))
    min_observations = max(1, int(SNAPSHOT_CONFIG.structure.replacement_min_observations))
    diagnostics["post_establishment_observations"] = observation_count
    if observation_count < min_observations:
        diagnostics["replacement_reason"] = "insufficient_post_establishment_observations"
        return False, diagnostics

    old_occ = _close_occupancy_for_frame(recent, accepted.range)
    candidate_occ = _close_occupancy_for_frame(recent, candidate.range)
    advantage = (candidate_occ - old_occ) if old_occ is not None and candidate_occ is not None else None
    diagnostics["old_range_close_occupancy"] = old_occ
    diagnostics["candidate_range_close_occupancy"] = candidate_occ
    diagnostics["candidate_occupancy_advantage"] = advantage

    if old_occ is None or candidate_occ is None:
        diagnostics["replacement_reason"] = "occupancy_unavailable"
        return False, diagnostics

    relocation_ok = old_occ <= float(SNAPSHOT_CONFIG.structure.max_old_range_close_occupancy)
    candidate_occupied = candidate_occ >= float(SNAPSHOT_CONFIG.structure.min_close_occupancy_ratio)
    strong_relocation = old_occ <= 0.10 and candidate_quality >= 60.0
    relocation_replacement = bool(
        relocation_ok
        and candidate_occupied
        and (quality_ok or strong_relocation)
    )
    diagnostics["relocation_replacement"] = relocation_replacement
    if relocation_replacement:
        diagnostics["replacement_reason"] = "post_establishment_relocation"
        return True, diagnostics

    quality_not_materially_worse = delta >= -float(
        SNAPSHOT_CONFIG.structure.overlap_evolution_quality_tolerance
    )
    occupancy_advantage_ok = advantage >= float(
        SNAPSHOT_CONFIG.structure.overlap_evolution_min_occupancy_advantage
    )
    overlapping_evolution = bool(
        overlap >= float(SNAPSHOT_CONFIG.structure.overlap_evolution_min_overlap_ratio)
        and quality_not_materially_worse
        and candidate_occupied
        and occupancy_advantage_ok
    )
    diagnostics["overlapping_evolution"] = overlapping_evolution
    if overlapping_evolution:
        diagnostics["replacement_reason"] = "overlapping_balance_evolution"
        return True, diagnostics

    diagnostics["replacement_reason"] = "replacement_conditions_not_met"
    return False, diagnostics


def _recent_close_observations(
    df3: pd.DataFrame | None,
    *,
    count: int | None = None,
) -> list[RecentCloseObservationBlock]:
    """Return compact, strategy-neutral recent close history.

    Evidence can compare these closes with PDH/PDL, ORB and the active range
    using its own configured ATR buffers. Snapshot generation does not label
    attempts, acceptance, failure or re-arm state.
    """
    if count is None:
        count = int(SNAPSHOT_CONFIG.structure.recent_close_observation_bars)
    if df3 is None or df3.empty or count <= 0:
        return []
    d = _ensure_dt_index(df3)
    if d.empty or "close" not in d.columns:
        return []
    out: list[RecentCloseObservationBlock] = []
    for ts, row in d.tail(int(count)).iterrows():
        close = _safe_float(row.get("close"))
        if close is None:
            continue
        dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        out.append(RecentCloseObservationBlock(time=dt, close=float(close)))
    return out


def _split_structure_frames(df3: pd.DataFrame | None) -> tuple[pd.DataFrame, pd.DataFrame, datetime | None]:
    if df3 is None or df3.empty:
        return pd.DataFrame(), pd.DataFrame(), None
    d = _ensure_dt_index(df3)
    if d.empty:
        return pd.DataFrame(), pd.DataFrame(), None
    current = d.iloc[-1:].copy()
    history = d.iloc[:-1].copy()
    asof = current.index[-1].to_pydatetime()
    return history, current, asof


def _promote_candidate(
    candidate: CandidateStructureBlock,
    accepted: AcceptedStructureBlock,
    *,
    asof: datetime | None,
) -> AcceptedStructureBlock:
    version = max(1, int(accepted.range.version or 0) + 1)
    promoted_range = candidate.range.model_copy(deep=True)
    promoted_range.version = version
    promoted_range.breakout_eligible = True
    promoted_range.provisional = False
    promoted_range.established_at = candidate.range.established_at or candidate.range.evidence_cutoff
    return AcceptedStructureBlock(
        state="RANGE_ACCEPTED",
        range=promoted_range,
        metrics=candidate.metrics,
        age_bars=1,
        frozen=True,
        promoted_time=asof,
        quality=candidate.quality,
        reason="promoted_qualified_intraday_balance",
    )


def _intraday_range_trust_ready(asof: datetime | None) -> bool:
    asof = _ensure_asof(asof)
    if asof is None:
        return False
    session_start = asof.replace(hour=9, minute=15, second=0, microsecond=0)
    completion_time = asof + pd.Timedelta(minutes=int(SNAPSHOT_CONFIG.service.tick_minutes))
    elapsed = (completion_time - session_start).total_seconds() / 60.0
    return elapsed >= float(SNAPSHOT_CONFIG.structure.trust_intraday_after_minutes)


def compute_raw_structure_state(
    *,
    px: float,
    df3: pd.DataFrame,
    df15: pd.DataFrame | None = None,
    levels: dict | None = None,
    atr: float | None = None,
) -> StructureBlock:
    """Build structure without persisted continuity.

    Dynamic range discovery uses every completed candle except the latest one.
    The latest candle may only evaluate a range that was already established by
    the prior historical cut-off.
    """
    px = _safe_float(px)
    atr = _safe_atr(atr)
    levels = levels or {}
    history, current, asof = _split_structure_frames(df3)

    raw, candidate, anchors, breakout_context = _build_raw_structure(
        px=px,
        history_df3=history,
        levels=levels,
        asof=asof,
        atr=atr,
    )
    accepted = _initial_accepted(
        px=px,
        levels=levels,
        session_df3=df3,
        asof=asof,
        atr=atr,
    )

    promoted_dynamic = False
    replacement_diag: dict[str, Any] = {}
    allowed, replacement_diag = _candidate_replacement_allowed(candidate, accepted, history, atr)
    trust_ready = _intraday_range_trust_ready(asof)
    replacement_diag["intraday_trust_ready"] = trust_ready
    allowed = bool(allowed and trust_ready)
    if bool(SNAPSHOT_CONFIG.structure.allow_intraday_accepted_promotion) and allowed:
        accepted = _promote_candidate(candidate, accepted, asof=asof)
        promoted_dynamic = True

    recent_closes = _recent_close_observations(df3)
    anchors.active_anchor = str(accepted.range.source or "UNKNOWN")

    return StructureBlock(
        raw=raw,
        accepted=accepted,
        candidate=candidate,
        recent_closes=recent_closes,
        anchors=anchors,
        breakout_context=breakout_context,
        diagnostics={
            "phase": "CAUSAL_DYNAMIC_BALANCE_V3",
            "current_candle_time": asof,
            "range_evidence_cutoff": candidate.range.evidence_cutoff,
            "latest_candle_excluded_from_range_discovery": True,
            "accepted_source": accepted.range.source,
            "accepted_range_id": accepted.range.range_id,
            "accepted_range_version": accepted.range.version,
            "accepted_high": accepted.range.high,
            "accepted_low": accepted.range.low,
            "accepted_breakout_eligible": accepted.range.breakout_eligible,
            "candidate_status": candidate.status,
            "candidate_reason": candidate.reason,
            "candidate_quality": candidate.quality,
            "candidate_bars_confirmed": candidate.bars_confirmed,
            "dynamic_accepted_replacement": promoted_dynamic,
            "replacement": replacement_diag,
            "recent_close_count": len(recent_closes),
        },
        session_phase=_session_phase(asof),
        previous_state=None,
        previous_side=None,
        count=1,
        flip_count_today=0,
        reason="causal_structure_without_previous_memory",
    )


def compute_structure_state(
    *,
    px: float,
    df3: pd.DataFrame,
    df15: pd.DataFrame | None = None,
    levels: dict | None = None,
    curr_snapshot_like: dict | None = None,
    prev_snapshot: dict | None = None,
    atr: float | None = None,
) -> tuple[StructureBlock, dict]:
    levels = levels or {}
    prev_snapshot = prev_snapshot or {}
    prev_memory = prev_snapshot.get("state_memory") or {}
    px = _safe_float(px)
    atr = _safe_atr(atr)
    history, current, asof = _split_structure_frames(df3)

    raw, candidate, anchors, breakout_context = _build_raw_structure(
        px=px,
        history_df3=history,
        levels=levels,
        asof=asof,
        atr=atr,
    )

    prev_accepted = _previous_accepted(prev_memory)
    legacy_state_reset = bool(prev_accepted and not prev_accepted.range.range_id)
    if legacy_state_reset:
        # The pre-V3 state has no versioned range identity and may contain the
        # first-candle ROLLING_60M seed.  Re-seed once from the current causal
        # lifecycle rather than carrying an un-auditable legacy reference.
        prev_accepted = None
    accepted = prev_accepted or _initial_accepted(
        px=px,
        levels=levels,
        session_df3=df3,
        asof=asof,
        atr=atr,
    )

    orb_replaced_provisional = False
    if accepted.range.provisional:
        orb_seed = _opening_range_seed(
            px=px,
            levels=levels,
            session_df3=df3,
            asof=asof,
            atr=atr,
            version=max(1, int(accepted.range.version or 0) + 1),
        )
        if orb_seed is not None:
            accepted = orb_seed
            orb_replaced_provisional = True

    accepted.age_bars = int(accepted.age_bars or 0) + (1 if prev_accepted and not orb_replaced_provisional else 0)

    allow_intraday_promotion = bool(SNAPSHOT_CONFIG.structure.allow_intraday_accepted_promotion)
    allowed, replacement_diag = _candidate_replacement_allowed(candidate, accepted, history, atr)
    trust_ready = _intraday_range_trust_ready(asof)
    replacement_diag["intraday_trust_ready"] = trust_ready
    allowed = bool(allowed and trust_ready)
    promoted_dynamic = False
    if allow_intraday_promotion and allowed:
        accepted = _promote_candidate(candidate, accepted, asof=asof)
        promoted_dynamic = True

    recent_closes = _recent_close_observations(df3)

    anchors.active_anchor = str(accepted.range.source or "UNKNOWN")
    curr_like = dict(curr_snapshot_like or {})
    structure = StructureBlock(
        raw=raw,
        accepted=accepted,
        candidate=candidate,
        recent_closes=recent_closes,
        anchors=anchors,
        breakout_context=breakout_context,
        diagnostics={
            "phase": "CAUSAL_DYNAMIC_BALANCE_V3",
            "current_candle_time": asof,
            "history_bar_count": len(history),
            "range_evidence_cutoff": candidate.range.evidence_cutoff,
            "latest_candle_excluded_from_range_discovery": True,
            "accepted_source": accepted.range.source,
            "accepted_range_id": accepted.range.range_id,
            "accepted_range_version": accepted.range.version,
            "accepted_high": accepted.range.high,
            "accepted_low": accepted.range.low,
            "accepted_range_type": accepted.range.range_type,
            "accepted_breakout_eligible": accepted.range.breakout_eligible,
            "accepted_quality": accepted.quality,
            "candidate_status": candidate.status,
            "candidate_reason": candidate.reason,
            "candidate_quality": candidate.quality,
            "candidate_bars_confirmed": candidate.bars_confirmed,
            "candidate_classification": candidate.metrics.classification,
            "candidate_overlap": candidate.metrics.adjacent_overlap_ratio,
            "candidate_efficiency": candidate.metrics.directional_efficiency,
            "candidate_displacement": candidate.metrics.net_displacement_fraction,
            "candidate_occupancy": candidate.metrics.close_occupancy_ratio,
            "orb_replaced_provisional": orb_replaced_provisional,
            "legacy_structure_state_reset": legacy_state_reset,
            "dynamic_accepted_replacement": promoted_dynamic,
            "replacement": replacement_diag,
            "recent_close_count": len(recent_closes),
        },
        session_phase=_session_phase(asof),
        previous_state=(prev_memory.get("structure.accepted.state") or {}).get("previous_state"),
        previous_side=(prev_memory.get("structure.raw.side") or {}).get("previous_state"),
        count=int((prev_memory.get("structure.accepted.state") or {}).get("count") or 1),
        flip_count_today=int((prev_memory.get("structure.raw.side") or {}).get("flip_count_today") or 0),
        reason="causal_structure_memory_updated",
    )

    curr_like["structure"] = structure.model_dump(mode="python")
    memory = build_state_memory(curr_like, prev_snapshot)
    accepted_state_mem = memory.get("structure.accepted.state") or {}
    raw_side_mem = memory.get("structure.raw.side") or {}
    structure.count = int(accepted_state_mem.get("count") or 1)
    structure.flip_count_today = int(raw_side_mem.get("flip_count_today") or 0)
    structure.previous_state = accepted_state_mem.get("previous_state")
    structure.previous_side = raw_side_mem.get("previous_state")
    return structure, memory


def make_state_memory_baseline(
    *,
    state_memory: dict | None = None,
    structure: StructureBlock | dict | None = None,
    hma: dict | None = None,
    context: dict | None = None,
    bollinger: dict | None = None,
    vwap: dict | None = None,
    volume: dict | None = None,
) -> dict:
    """
    Creates a snapshot-like baseline used only for in-memory state_memory replay.
    """
    out: dict = {
        "state_memory": state_memory or {},
    }

    if structure is not None:
        if hasattr(structure, "model_dump"):
            out["structure"] = structure.model_dump(mode="python")
        elif isinstance(structure, dict):
            out["structure"] = structure

    if hma is not None:
        out["hma"] = hma
    if context is not None:
        out["context"] = context
    if bollinger is not None:
        out["bollinger"] = bollinger
    if vwap is not None:
        out["vwap"] = vwap
    if volume is not None:
        out["volume"] = volume

    return out


def compute_structure_state_from_memory(
    *,
    px: float,
    df3: pd.DataFrame,
    df15: pd.DataFrame | None = None,
    levels: dict | None = None,
    curr_snapshot_like: dict | None = None,
    prev_state_memory: dict | None = None,
    atr: float | None = None,
) -> tuple[StructureBlock, dict]:
    """
    In-memory version of compute_structure_state().

    The caller should feed candles candle-by-candle through the current session
    replay. The memory stores accepted/candidate structure state so no DB read is
    required for range continuity. Strategy setup state belongs to Evidence.
    """
    prev_snapshot_like = {
        "state_memory": prev_state_memory or {},
    }

    return compute_structure_state(
        px=px,
        df3=df3,
        df15=df15,
        levels=levels,
        curr_snapshot_like=curr_snapshot_like,
        prev_snapshot=prev_snapshot_like,
        atr=atr,
    )


# -----------------------------
# Events
# -----------------------------
def _as_dict(x: Any) -> Dict[str, Any]:
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if hasattr(x, "model_dump"):
        return x.model_dump(mode="python")
    if hasattr(x, "dict"):
        return x.dict()
    return {}


def _get(d: Dict[str, Any], path: str, default=None):
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(part)
        if cur is None:
            return default
    return cur


def build_events(curr: dict, prev: dict | None) -> list[dict]:
    currd = _as_dict(curr)
    prevd = _as_dict(prev)

    if not prevd:
        return []

    events: list[dict] = []

    def add(k: str, prev_val: Any, curr_val: Any):
        if prev_val is None:
            return
        if curr_val is None:
            return
        if prev_val == curr_val:
            return
        events.append({"k": k, "from_": prev_val, "to": curr_val})

    # Raw indicator events use explicit raw.* paths because debounced/confirmed
    # state_context can intentionally lag raw indicator changes.
    add("raw.indicators.hma.state", _get(prevd, "indicators.hma.state"), _get(currd, "indicators.hma.state"))
    add("raw.indicators.hma.strength", _get(prevd, "indicators.hma.strength"), _get(currd, "indicators.hma.strength"))

    add("raw.indicators.atr.band", _get(prevd, "indicators.atr.band"), _get(currd, "indicators.atr.band"))
    add("raw.indicators.adx.band", _get(prevd, "indicators.adx.band"), _get(currd, "indicators.adx.band"))
    add("raw.indicators.rsi.zone", _get(prevd, "indicators.rsi.zone"), _get(currd, "indicators.rsi.zone"))
    add("raw.indicators.bollinger.zone", _get(prevd, "indicators.bollinger.zone"), _get(currd, "indicators.bollinger.zone"))
    add("raw.indicators.vwap.side", _get(prevd, "indicators.vwap.side"), _get(currd, "indicators.vwap.side"))
    add("raw.volume.bar_rvol_band", _get(prevd, "volume.bar_rvol_band"), _get(currd, "volume.bar_rvol_band"))

    # Confirmed state events are separate from raw changes.
    add("state_context.hma.confirmed_state", _get(prevd, "state_context.hma.confirmed_state"), _get(currd, "state_context.hma.confirmed_state"))
    add("state_context.rsi.confirmed_state", _get(prevd, "state_context.rsi.confirmed_state"), _get(currd, "state_context.rsi.confirmed_state"))
    add("state_context.bollinger.confirmed_state", _get(prevd, "state_context.bollinger.confirmed_state"), _get(currd, "state_context.bollinger.confirmed_state"))
    add("state_context.volume.confirmed_state", _get(prevd, "state_context.volume.confirmed_state"), _get(currd, "state_context.volume.confirmed_state"))

    add("structure.raw.state", _get(prevd, "structure.raw.state"), _get(currd, "structure.raw.state"))
    add("structure.raw.side", _get(prevd, "structure.raw.side"), _get(currd, "structure.raw.side"))
    add("structure.accepted.confirmed_state", _get(prevd, "structure.accepted.state"), _get(currd, "structure.accepted.state"))
    add("structure.breakout.swing", _get(prevd, "structure.breakout_context.swing"), _get(currd, "structure.breakout_context.swing"))
    add("structure.breakout.orb", _get(prevd, "structure.breakout_context.orb"), _get(currd, "structure.breakout_context.orb"))
    add("structure.breakout.pdh_pdl", _get(prevd, "structure.breakout_context.pdh_pdl"), _get(currd, "structure.breakout_context.pdh_pdl"))
    add("structure.breakout.recent15", _get(prevd, "structure.breakout_context.recent15"), _get(currd, "structure.breakout_context.recent15"))

    return events



# =============================================================================
# AutoTrades Snapshot V1 window/state helpers
# =============================================================================
WINDOW_MINUTES = {
    "15m": 15,
    "30m": 30,
    "60m": 60,
}


def _window_slice_for_minutes(d: pd.DataFrame, asof: datetime, minutes: int | None) -> pd.DataFrame:
    if d is None or d.empty:
        return pd.DataFrame()
    if minutes is None:
        return d.copy()
    start = asof - pd.Timedelta(minutes=int(minutes))
    return d[(d.index > start) & (d.index <= asof)].copy()


def _window_status(d_session: pd.DataFrame, w: pd.DataFrame, asof: datetime, minutes: int | None) -> tuple[str, float | None]:
    if d_session is None or d_session.empty or w is None or w.empty:
        return "na", None
    first_ts = d_session.index[0].to_pydatetime()
    available = max(0.0, (asof - first_ts).total_seconds() / 60.0)
    if minutes is None:
        return "ok", available
    return ("ok" if available >= float(minutes) else "warming_up"), available


def _close_position_in_range(close: Optional[float], low: Optional[float], high: Optional[float]) -> Optional[float]:
    if close is None or low is None or high is None:
        return None
    span = float(high) - float(low)
    if span == 0:
        # Flat candle/window: high == low. Treat the close as neutral inside
        # the range instead of writing null and breaking strict snapshot replay.
        return 0.5
    pos = float((float(close) - float(low)) / span)
    # Defensive clamp for occasional vendor rounding / bad ticks.
    return max(0.0, min(1.0, pos))


def _market_window_from_df(
    d_session: pd.DataFrame,
    w: pd.DataFrame,
    *,
    asof: datetime,
    minutes: int | None,
    atr: Optional[float],
) -> Dict[str, Any]:
    status, available = _window_status(d_session, w, asof, minutes)
    out: Dict[str, Any] = {
        "status": status,
        "bars": int(len(w)) if w is not None else 0,
        "minutes": (int(minutes) if minutes is not None else (3 if len(w) == 1 else (int(available) if available is not None else None))),
        "session_elapsed_minutes": available,
        "open": None,
        "high": None,
        "low": None,
        "close": None,
        "volume_sum": None,
        "avg_volume": None,
        "move_points": None,
        "move_pct": None,
        "move_atr": None,
        "range_points": None,
        "range_pct": None,
        "slope_points_per_bar": None,
        "slope_atr_per_bar": None,
        "close_position_in_range": None,
    }
    if w is None or w.empty:
        return out

    opn = _safe_float(w.iloc[0].get("open"))
    high = _safe_float(w["high"].max())
    low = _safe_float(w["low"].min())
    close = _safe_float(w.iloc[-1].get("close"))
    volume_sum = _safe_float(w["volume"].sum()) if "volume" in w.columns else None
    bars = int(len(w))

    move_points = None if opn is None or close is None else float(close - opn)
    range_points = None if high is None or low is None else float(high - low)
    slope_points = None
    slope_atr = None
    if bars >= 2 and move_points is not None:
        slope_points = float(move_points / (bars - 1))
        slope_atr = _atr_move(slope_points, atr)

    out.update(
        {
            "open": opn,
            "high": high,
            "low": low,
            "close": close,
            "volume_sum": volume_sum,
            "avg_volume": None if volume_sum is None or bars <= 0 else float(volume_sum / bars),
            "move_points": move_points,
            "move_pct": _pct_move(move_points, opn),
            "move_atr": _atr_move(move_points, atr),
            "range_points": range_points,
            "range_pct": _pct_move(range_points, opn),
            "slope_points_per_bar": slope_points,
            "slope_atr_per_bar": slope_atr,
            "close_position_in_range": _close_position_in_range(close, low, high),
        }
    )
    return out


def build_market_windows_block(df_tf: pd.DataFrame, asof: datetime, *, atr: Any = None) -> Dict[str, Any]:
    asof = _ensure_asof(asof)
    d = _as_dt_indexed_tf(df_tf, asof)
    atr_val = _safe_atr(atr)
    out: Dict[str, Any] = {
        "current": _market_window_from_df(d, d.tail(1), asof=asof, minutes=None, atr=atr_val),
        "15m": _market_window_from_df(d, _window_slice_for_minutes(d, asof, 15), asof=asof, minutes=15, atr=atr_val),
        "30m": _market_window_from_df(d, _window_slice_for_minutes(d, asof, 30), asof=asof, minutes=30, atr=atr_val),
        "60m": _market_window_from_df(d, _window_slice_for_minutes(d, asof, 60), asof=asof, minutes=60, atr=atr_val),
        "sod": _market_window_from_df(d, d, asof=asof, minutes=None, atr=atr_val),
    }
    out["current"]["minutes"] = 3
    return out


def _series_values(w: pd.DataFrame, col: str) -> list[float]:
    if w is None or w.empty or col not in w.columns:
        return []
    vals = [_safe_float(x) for x in w[col].tolist()]
    return [x for x in vals if x is not None]


def _derive_hma_state_strength_from_values(fast: Any, mid1: Any, mid2: Any, slow: Any) -> Dict[str, str]:
    vals = [_safe_float(fast), _safe_float(mid1), _safe_float(mid2), _safe_float(slow)]
    if any(x is None for x in vals):
        return {"state": "NO_TREND", "strength": "NA"}
    fast, mid1, mid2, slow = vals
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


def _numeric_current(d: pd.DataFrame, col: str, classifier=None) -> Dict[str, Any]:
    val = None if d is None or d.empty else _safe_float(d.iloc[-1].get(col))
    out = {"value": val}
    if classifier:
        out["state"] = classifier(val)
    return out


def _numeric_window(
    d_session: pd.DataFrame,
    w: pd.DataFrame,
    *,
    asof: datetime,
    minutes: int | None,
    col: str,
    classifier=None,
    upper_state: str | None = None,
    lower_state: str | None = None,
) -> Dict[str, Any]:
    status, _ = _window_status(d_session, w, asof, minutes)
    vals = _series_values(w, col)
    out: Dict[str, Any] = {
        "status": status,
        "bars": int(len(vals)),
        "minutes": minutes,
        "start_value": None,
        "end_value": None,
        "high": None,
        "low": None,
        "delta": None,
        "slope_per_bar": None,
        "start_state": None,
        "end_state": None,
        "state_changed": False,
        "touched_upper": False,
        "touched_lower": False,
    }
    if not vals:
        return out
    start = vals[0]
    end = vals[-1]
    start_state = classifier(start) if classifier else None
    end_state = classifier(end) if classifier else None
    states = [classifier(v) for v in vals] if classifier else []
    out.update(
        {
            "start_value": start,
            "end_value": end,
            "high": float(max(vals)),
            "low": float(min(vals)),
            "delta": float(end - start),
            "slope_per_bar": None if len(vals) < 2 else float((end - start) / (len(vals) - 1)),
            "start_state": start_state,
            "end_state": end_state,
            "state_changed": bool(start_state != end_state) if classifier else False,
            "touched_upper": bool(upper_state in states) if upper_state else False,
            "touched_lower": bool(lower_state in states) if lower_state else False,
        }
    )
    return out


def _window_set(d: pd.DataFrame, asof: datetime, fn) -> Dict[str, Any]:
    current = fn(d.tail(1), 3)
    m15 = fn(_window_slice_for_minutes(d, asof, 15), 15)
    m30 = fn(_window_slice_for_minutes(d, asof, 30), 30)
    m60 = fn(_window_slice_for_minutes(d, asof, 60), 60)
    sod = fn(d, None)
    status, elapsed = _window_status(d, d, asof, None)
    if isinstance(sod, dict):
        sod["status"] = status
        sod["minutes"] = int(round(elapsed)) if elapsed is not None else None
    return {
        "current": current,
        "15m": m15,
        "30m": m30,
        "60m": m60,
        "sod": sod,
    }


def _hma_window(d_session: pd.DataFrame, w: pd.DataFrame, asof: datetime, minutes: int | None) -> Dict[str, Any]:
    status, _ = _window_status(d_session, w, asof, minutes)
    out = {
        "status": status,
        "bars": int(len(w)) if w is not None else 0,
        "minutes": minutes,
        "start_state": "UNKNOWN",
        "end_state": "UNKNOWN",
        "start_strength": "UNKNOWN",
        "end_strength": "UNKNOWN",
        "state_changed": False,
        "strength_changed": False,
        "flip_count": 0,
        "bars_in_current_state": 0,
    }
    if w is None or w.empty:
        return out
    states = []
    strengths = []
    for _, row in w.iterrows():
        ss = _derive_hma_state_strength_from_values(
            row.get("hmafast"), row.get("hmamid1"), row.get("hmamid2"), row.get("hmaslow")
        )
        states.append(ss["state"])
        strengths.append(ss["strength"])
    flips = sum(1 for i in range(1, len(states)) if states[i] != states[i - 1])
    bars_current = 0
    if states:
        last = states[-1]
        for st in reversed(states):
            if st == last:
                bars_current += 1
            else:
                break
        out.update(
            {
                "start_state": states[0],
                "end_state": states[-1],
                "start_strength": strengths[0],
                "end_strength": strengths[-1],
                "state_changed": states[0] != states[-1],
                "strength_changed": strengths[0] != strengths[-1],
                "flip_count": int(flips),
                "bars_in_current_state": int(bars_current),
            }
        )
    return out


def _vwap_window(d_session: pd.DataFrame, w: pd.DataFrame, asof: datetime, minutes: int | None) -> Dict[str, Any]:
    status, _ = _window_status(d_session, w, asof, minutes)
    out = {
        "status": status,
        "bars": int(len(w)) if w is not None else 0,
        "minutes": minutes,
        "start_side": "UNKNOWN",
        "end_side": "UNKNOWN",
        "crossed": False,
        "min_distance_pct": None,
        "max_distance_pct": None,
        "end_distance_pct": None,
        "bars_in_current_side": 0,
    }
    if w is None or w.empty or "vwap" not in w.columns:
        return out
    distances = []
    sides = []
    for _, row in w.iterrows():
        close = _safe_float(row.get("close"))
        vwap = _safe_float(row.get("vwap"))
        dist = compute_px_vs_vwap_pct(close, vwap)
        distances.append(dist)
        sides.append(_vwap_side(dist))
    valid_d = [x for x in distances if x is not None]
    bars_current = 0
    if sides:
        last = sides[-1]
        for st in reversed(sides):
            if st == last:
                bars_current += 1
            else:
                break
        out.update(
            {
                "start_side": sides[0],
                "end_side": sides[-1],
                "crossed": any(sides[i] != sides[i - 1] for i in range(1, len(sides))),
                "min_distance_pct": None if not valid_d else float(min(valid_d)),
                "max_distance_pct": None if not valid_d else float(max(valid_d)),
                "end_distance_pct": distances[-1],
                "bars_in_current_side": int(bars_current),
            }
        )
    return out


def _bollinger_window(d_session: pd.DataFrame, w: pd.DataFrame, asof: datetime, minutes: int | None) -> Dict[str, Any]:
    status, _ = _window_status(d_session, w, asof, minutes)
    out = {
        "status": status,
        "bars": int(len(w)) if w is not None else 0,
        "minutes": minutes,
        "start_zone": "UNKNOWN",
        "end_zone": "UNKNOWN",
        "zone_changed": False,
        "touch_upper_count": 0,
        "touch_lower_count": 0,
        "min_position": None,
        "max_position": None,
        "width_change_pct": None,
    }
    if w is None or w.empty:
        return out
    positions = []
    zones = []
    widths = []
    for _, row in w.iterrows():
        pos = compute_bb_position(_safe_float(row.get("close")), _safe_float(row.get("bb_upper")), _safe_float(row.get("bb_lower")))
        zone = classify_bollinger_zone(pos)
        positions.append(pos)
        zones.append(zone)
        widths.append(_safe_float(row.get("bb_width")))
    valid_pos = [x for x in positions if x is not None]
    valid_widths = [x for x in widths if x is not None]
    width_change_pct = None
    if len(valid_widths) >= 2 and valid_widths[0] not in (None, 0):
        width_change_pct = float((valid_widths[-1] - valid_widths[0]) / valid_widths[0] * 100.0)
    out.update(
        {
            "start_zone": zones[0] if zones else "UNKNOWN",
            "end_zone": zones[-1] if zones else "UNKNOWN",
            "zone_changed": bool(zones and zones[0] != zones[-1]),
            "touch_upper_count": int(sum(1 for z in zones if z in {"NEAR_UPPER", "ABOVE_BAND"})),
            "touch_lower_count": int(sum(1 for z in zones if z in {"NEAR_LOWER", "BELOW_BAND"})),
            "min_position": None if not valid_pos else float(min(valid_pos)),
            "max_position": None if not valid_pos else float(max(valid_pos)),
            "width_change_pct": width_change_pct,
        }
    )
    return out



def _volume_rvol_frame(d: pd.DataFrame) -> pd.DataFrame:
    out = d.copy()
    if out.empty or "volume" not in out.columns:
        out["rvol_pct"] = np.nan
        return out
    vol = pd.to_numeric(out["volume"], errors="coerce")
    sma = vol.rolling(int(VOL_SMA_N), min_periods=int(VOL_SMA_N)).mean()
    out["rvol_pct"] = np.where(sma > 0, (vol / sma) * 100.0, np.nan)
    return out


def _volume_window(d_session: pd.DataFrame, w: pd.DataFrame, asof: datetime, minutes: int | None) -> Dict[str, Any]:
    return _numeric_window(
        d_session,
        w,
        asof=asof,
        minutes=minutes,
        col="rvol_pct",
        classifier=classify_rvol_pct_band,
    )

def build_indicator_windows_block(df_tf: pd.DataFrame, asof: datetime) -> Dict[str, Any]:
    asof = _ensure_asof(asof)
    d = _as_dt_indexed_tf(df_tf, asof)
    if d.empty:
        return {}
    d_vol = _volume_rvol_frame(d)

    def numeric_set(col: str, classifier=None, upper_state=None, lower_state=None):
        return _window_set(
            d,
            asof,
            lambda w, minutes: _numeric_window(
                d,
                w,
                asof=asof,
                minutes=minutes,
                col=col,
                classifier=classifier,
                upper_state=upper_state,
                lower_state=lower_state,
            ),
        )

    return {
        "rsi": numeric_set("rsi", classify_rsi_zone, upper_state="OB", lower_state="OS"),
        "adx": numeric_set("adx", classify_adx_band),
        "atr": numeric_set("atr", lambda v: classify_atr_band(v, _safe_float(d.iloc[-1].get("close")))),
        "hma": _window_set(d, asof, lambda w, minutes: _hma_window(d, w, asof, minutes)),
        "vwap": _window_set(d, asof, lambda w, minutes: _vwap_window(d, w, asof, minutes)),
        "bollinger": _window_set(d, asof, lambda w, minutes: _bollinger_window(d, w, asof, minutes)),
        "volume": _window_set(d_vol, asof, lambda w, minutes: _volume_window(d_vol, w, asof, minutes)),
    }


def _state_metric(memory: Dict[str, Any], key: str, *, override_state: Any = None) -> Dict[str, Any]:
    entry = memory.get(key) or {}
    state = override_state if override_state is not None else entry.get("state")
    previous = entry.get("previous_state")
    raw = override_state if override_state is not None else entry.get("raw_state")
    age = int(entry.get("count") or 0)
    if override_state is not None and age <= 0:
        age = 1
    return {
        "confirmed_state": None if state is None else str(state),
        "raw_state": None if raw is None else str(raw),
        "previous_state": None if previous is None else str(previous),
        "age_bars": age,
        "previous_age_bars": int(entry.get("previous_count") or 0),
        "candidate_state": None if entry.get("candidate_state") is None else str(entry.get("candidate_state")),
        "candidate_age_bars": int(entry.get("candidate_count") or 0),
        "changed": bool(previous is not None and state != previous and age <= 1),
        "flip_count_today": int(entry.get("flip_count_today") or 0),
    }


def build_state_context_block(
    state_memory: Dict[str, Any],
    structure: StructureBlock | Dict[str, Any],
    *,
    current_volume_band: Any = None,
) -> Dict[str, Any]:
    memory = state_memory or {}
    struct = structure.model_dump(mode="python") if hasattr(structure, "model_dump") else (structure or {})
    return {
        "hma": _state_metric(memory, "hma.state"),
        "hma_strength": _state_metric(memory, "hma.strength"),
        "vwap": _state_metric(memory, "vwap.side"),
        "rsi": _state_metric(memory, "rsi.zone"),
        "adx": _state_metric(memory, "adx.band"),
        "atr": _state_metric(memory, "atr.band"),
        "bollinger": _state_metric(memory, "bollinger.zone"),
        "volume": _state_metric(memory, "volume.rvol_band", override_state=current_volume_band),
        "structure": {
            "confirmed_state": _get_path(struct, "accepted.state"),
            "raw_state": _get_path(struct, "raw.state"),
            "raw_side": _get_path(struct, "raw.side"),
            "previous_state": struct.get("previous_state"),
            "previous_side": struct.get("previous_side"),
            "age_bars": int(_get_path(struct, "accepted.age_bars", 0) or 0),
            "changed": bool(struct.get("previous_state") and struct.get("previous_state") != _get_path(struct, "accepted.state")),
            "flip_count_today": int(struct.get("flip_count_today") or 0),
        },
    }
