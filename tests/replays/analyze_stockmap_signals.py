#!/usr/bin/env python3
"""Diagnostic one-day StockMap/signal join.

This script does not change historical decisions. It attaches the latest causal
StockMap available at each signal's first_seen_time and exports transparent
alignment/room cohorts for review.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd

# ---------------------------------------------------------------------------
# Hard-coded research defaults. CLI arguments override them.
# ---------------------------------------------------------------------------
ANALYSIS_DAY = date(2026, 8, 5)
STOCKMAP_CSV = ""
SIGNALS_CSV = ""
OUTPUT_DIR = "."


def _latest_matching(pattern: str) -> Optional[Path]:
    matches = sorted(Path(".").glob(pattern), key=lambda path: path.stat().st_mtime)
    return matches[-1] if matches else None


def _resolve_path(value: str, *, pattern: str, label: str) -> Path:
    if str(value or "").strip():
        path = Path(value).expanduser().resolve()
    else:
        match = _latest_matching(pattern)
        if match is None:
            raise RuntimeError(
                f"No {label} path was supplied and no file matched {pattern!r}"
            )
        path = match.resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    return path




def _to_utc_series(series: pd.Series, *, assume_ist_when_naive: bool) -> pd.Series:
    values = []
    for raw in series:
        ts = pd.Timestamp(raw)
        if ts.tzinfo is None:
            ts = ts.tz_localize("Asia/Kolkata" if assume_ist_when_naive else "UTC")
        values.append(ts.tz_convert("UTC"))
    return pd.Series(values, index=series.index, dtype="datetime64[ns, UTC]")

def _alignment(side: str, bullish_label: str, bearish_label: str, value: str) -> str:
    side = str(side or "").strip().upper()
    value = str(value or "").strip().upper()
    if side == "BUY":
        if value == bullish_label:
            return "ALIGNED"
        if value == bearish_label:
            return "OPPOSED"
    elif side == "SELL":
        if value == bearish_label:
            return "ALIGNED"
        if value == bullish_label:
            return "OPPOSED"
    return "MIXED"


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Join one-day signals to the latest causal StockMap"
    )
    parser.add_argument("--day", default=None, help="YYYY-MM-DD")
    parser.add_argument("--stockmaps", default=None)
    parser.add_argument("--signals", default=None)
    parser.add_argument("--output-dir", default=None)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    day = date.fromisoformat(args.day) if args.day else ANALYSIS_DAY
    stockmap_path = _resolve_path(
        args.stockmaps or STOCKMAP_CSV,
        pattern=f"stockmap_replay_{day.isoformat()}_*.csv",
        label="StockMap CSV",
    )
    signal_path = _resolve_path(
        args.signals or SIGNALS_CSV,
        pattern=f"signal_replay_{day.isoformat()}_*_signals.csv",
        label="signal CSV",
    )
    output_dir = Path(args.output_dir or OUTPUT_DIR).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    maps = pd.read_csv(stockmap_path)
    signals = pd.read_csv(signal_path)
    required_map = {"symbol", "stockmap_time", "raw_side", "ema_regime", "room_up_atr", "room_down_atr"}
    required_signal = {"symbol", "side", "first_seen_time", "signal_id", "setup"}
    missing_map = required_map.difference(maps.columns)
    missing_signal = required_signal.difference(signals.columns)
    if missing_map:
        raise ValueError(f"StockMap CSV missing columns: {sorted(missing_map)}")
    if missing_signal:
        raise ValueError(f"Signal CSV missing columns: {sorted(missing_signal)}")

    maps["symbol"] = maps["symbol"].astype(str).str.strip().str.upper()
    signals["symbol"] = signals["symbol"].astype(str).str.strip().str.upper()
    maps["stockmap_time"] = _to_utc_series(
        maps["stockmap_time"],
        assume_ist_when_naive=True,
    )
    maps["stockmap_available_time"] = maps["stockmap_time"] + pd.Timedelta(
        minutes=15
    )
    signals["signal_time"] = _to_utc_series(
        signals["first_seen_time"],
        assume_ist_when_naive=True,
    )

    maps = maps.sort_values(["stockmap_available_time", "symbol"])
    signals = signals.sort_values(["signal_time", "symbol"])
    joined = pd.merge_asof(
        signals,
        maps,
        left_on="signal_time",
        right_on="stockmap_available_time",
        by="symbol",
        direction="backward",
        allow_exact_matches=True,
        suffixes=("_signal", "_map"),
    )

    joined["ema_alignment"] = joined.apply(
        lambda row: _alignment(
            row.get("side"),
            "BULLISH_STACKED",
            "BEARISH_STACKED",
            row.get("ema_regime"),
        ),
        axis=1,
    )
    joined["structure_alignment"] = joined.apply(
        lambda row: _alignment(
            row.get("side"),
            "BUY",
            "SELL",
            row.get("raw_side"),
        ),
        axis=1,
    )
    joined["map_alignment"] = joined.apply(
        lambda row: (
            "ALIGNED"
            if row["ema_alignment"] == "ALIGNED"
            and row["structure_alignment"] != "OPPOSED"
            else "OPPOSED"
            if row["ema_alignment"] == "OPPOSED"
            and row["structure_alignment"] != "ALIGNED"
            else "MIXED"
        ),
        axis=1,
    )
    joined["directional_room_atr"] = joined.apply(
        lambda row: row.get("room_up_atr")
        if str(row.get("side") or "").upper() == "BUY"
        else row.get("room_down_atr"),
        axis=1,
    )
    joined["stockmap_available"] = joined["stockmap_time"].notna()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    joined_path = output_dir / f"stockmap_signal_context_{day.isoformat()}_{stamp}.csv"
    joined.to_csv(joined_path, index=False)

    outcome_column = "max_pnl" if "max_pnl" in joined.columns else None
    group_columns = ["side", "setup", "map_alignment", "ema_regime", "raw_side"]
    aggregations = {"signal_id": "count", "directional_room_atr": "median"}
    if outcome_column:
        joined[outcome_column] = pd.to_numeric(joined[outcome_column], errors="coerce")
        joined["positive_mfe"] = joined[outcome_column] > 0
        aggregations[outcome_column] = "median"
        aggregations["positive_mfe"] = "mean"

    summary = (
        joined.groupby(group_columns, dropna=False)
        .agg(aggregations)
        .reset_index()
        .rename(
            columns={
                "signal_id": "signals",
                "directional_room_atr": "median_directional_room_atr",
                outcome_column: "median_max_pnl" if outcome_column else outcome_column,
                "positive_mfe": "positive_mfe_rate",
            }
        )
    )
    summary_path = output_dir / f"stockmap_signal_summary_{day.isoformat()}_{stamp}.csv"
    summary.to_csv(summary_path, index=False)

    print(f"Joined context: {joined_path}")
    print(f"Cohort summary: {summary_path}")
    print(
        "Coverage: %d/%d signals have a causal StockMap"
        % (int(joined["stockmap_available"].sum()), len(joined))
    )


if __name__ == "__main__":
    main()
