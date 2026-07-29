#!/usr/bin/env python3
"""
tests/test_ranking_signals.py

Read-only shadow ranking study for signals already present in the AutoTrades DB.

This program follows the same application-integrated style as the existing
AutoTrades replay/test programs:

    project-root import bootstrap
    -> get_trades_db()
    -> existing SQLAlchemy ORM models
    -> SnapshotSchema.fetch_snapshot()
    -> hard-coded options at the top
    -> CSV/JSON reports under reports/

It does not maintain separate DB credentials, does not open a separate direct database connector, and does not write to
any database table.

Leakage boundary
----------------
Ranking inputs may use only:
- immutable meta_json.entry_criteria_json captured when the signal was created;
- the exact SnapshotSchema at entry_criteria_json.snapshot_time;
- earlier same-day signals that were already known by that timestamp.

The ranking score never uses:
- signal or package MFE/MAE;
- exit price, exit reason, exit time or realized P&L;
- later signal lifecycle state;
- later Auction transitions;
- future snapshots or candles.

Realized package P&L is loaded only after all scores/ranks have been assigned,
and is included strictly as an evaluation-only report field.
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
import traceback
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

# Match existing programs: allow imports from the project root.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from database.database import get_trades_db
from logconfig import setup_logging
from models.trade_models import Signal as SignalORM
from models.trade_models import UserTrade as TradeORM
from schemas.snapshot import SnapshotSchema


# =============================================================================
# HARD-CODED OPTIONS
# =============================================================================

IST = ZoneInfo("Asia/Kolkata")

TEST_DATE: date = date(2026, 7, 24)

# Empty lists mean all signals for TEST_DATE.
SYMBOLS: List[str] = []
SETUP_FAMILIES: List[str] = []

# None aggregates all users. Normally use the dedicated replay user.
TEST_USERID: Optional[str] = "DR1812"

RANKING_VERSION = "SIGNAL_PRIORITY_V1_1_WINDOW_ALIAS_FIX"
TOP_PERCENTILES: Tuple[int, ...] = (10, 20, 30)
QUINTILE_COUNT = 5

REPORT_DIR = Path("reports")
REPORT_PREFIX = "test_ranking_signals"
LOG_FILE = REPORT_DIR / "test_ranking_signals.log"

# Continue after individual signal/context errors and write them to errors.csv.
FAIL_IF_RECORD_ERRORS = False

# Require an exact DB snapshot at the immutable signal creation timestamp.
REQUIRE_CREATION_SNAPSHOT = True

# Signal-history window used only for creation-time churn diagnostics.
CHURN_LOOKBACK_MINUTES = 60
MAX_PREVIOUS_OPPOSITE_SIGNALS = 2
MAX_PREVIOUS_SAME_SIDE_SIGNALS = 2

# Report labels only. They do not affect live eligibility.
PRIORITY_BANDS: Tuple[Tuple[float, str], ...] = (
    (80.0, "A"),
    (60.0, "B"),
    (30.0, "C"),
    (0.0, "D"),
)

# =============================================================================
# CONFIGURABLE RANKING PARAMETERS
# =============================================================================

ADVISOR_POINTS: Dict[str, float] = {
    "ALLOW": 10.0,
    "WATCH": 2.0,
    "BLOCK": -30.0,
    "NO_ACTION": 0.0,
    "UNKNOWN": -2.0,
}

# Equal initial setup bases: do not encode one replay day's realized outcome.
SETUP_BASE_POINTS: Dict[str, float] = {
    "ACCEPTED_BREAKOUT": 0.0,
    "REVERSAL": 0.0,
    "FAILED_BREAKOUT": 0.0,
    "BREAKOUT_INITIATION": 0.0,
    "UNKNOWN": -5.0,
}

WINDOW_MAX_POINTS: Dict[str, Dict[str, float]] = {
    "ACCEPTED_BREAKOUT": {"15m": 4.0, "30m": 4.0, "60m": 3.0},
    "REVERSAL": {"15m": 6.0, "30m": 3.0, "60m": 1.0},
    "FAILED_BREAKOUT": {"15m": 6.0, "30m": 4.0, "60m": 2.0},
    "BREAKOUT_INITIATION": {"15m": 5.0, "30m": 4.0, "60m": 2.0},
    "UNKNOWN": {"15m": 3.0, "30m": 2.0, "60m": 1.0},
}

WINDOW_FULL_SCORE_ATR: Dict[str, float] = {
    "15m": 1.50,
    "30m": 2.00,
    "60m": 3.00,
}

ADX_RULES: Tuple[Tuple[float, float, str], ...] = (
    (12.0, -4.0, "VERY_WEAK"),
    (18.0, 0.0, "WEAK"),
    (25.0, 4.0, "DEVELOPING"),
    (40.0, 7.0, "STRONG"),
    (math.inf, 5.0, "VERY_STRONG_POSSIBLY_MATURE"),
)

PRICE_SLOPE_ALIGNED_POINTS = 3.0
PRICE_SLOPE_OPPOSED_POINTS = -4.0

VWAP_ALIGNED_POINTS = 3.0
VWAP_OPPOSED_POINTS = -2.0
VWAP_EXTENSION_ATR = 3.0
VWAP_EXTENSION_PENALTY = -3.0

HMA_ALIGNED_POINTS = 3.0
HMA_OPPOSED_POINTS: Dict[str, float] = {
    "ACCEPTED_BREAKOUT": -3.0,
    "REVERSAL": -1.0,
    "FAILED_BREAKOUT": -2.0,
    "BREAKOUT_INITIATION": -3.0,
    "UNKNOWN": -2.0,
}

ACCEPTED_RANGE_QUALITY_FULL_SCORE = 85.0
ACCEPTED_RANGE_QUALITY_MAX_POINTS = 5.0

ACCEPTED_BREAKOUT_OUTSIDE_RULES: Tuple[Tuple[float, float, str], ...] = (
    (0.0, -12.0, "NOT_OUTSIDE"),
    (0.15, -6.0, "MARGINAL_ESCAPE"),
    (0.35, 3.0, "EARLY_ESCAPE"),
    (1.20, 8.0, "HEALTHY_ESCAPE"),
    (2.00, 3.0, "EXTENDED_ESCAPE"),
    (math.inf, -6.0, "VERY_EXTENDED_ESCAPE"),
)
ACCEPTED_BREAKOUT_INSIDE_PENALTY = -12.0

REVERSAL_ACCEPTED_EDGE_POINTS = 4.0
REVERSAL_ACCEPTED_INTERIOR_PENALTY = -4.0
REVERSAL_SESSION_EDGE_POINTS = 3.0
REVERSAL_SESSION_INTERIOR_PENALTY = -2.0

REVERSAL_ROOM_RULES: Tuple[Tuple[float, float, str], ...] = (
    (0.25, -3.0, "VERY_LOW_ROOM"),
    (0.50, 0.0, "LOW_ROOM"),
    (1.00, 2.0, "MODERATE_ROOM"),
    (math.inf, 4.0, "GOOD_ROOM"),
)

DIRECTIONAL_EXTENSION_RULES: Tuple[Tuple[float, float, str], ...] = (
    (0.50, -2.0, "INSUFFICIENT_DEVELOPMENT"),
    (3.00, 4.0, "HEALTHY_DEVELOPMENT"),
    (5.00, 2.0, "MATURE_DEVELOPMENT"),
    (7.00, -2.0, "EXTENDED"),
    (math.inf, -7.0, "VERY_EXTENDED"),
)

PATH_EFFICIENCY_RULES: Tuple[Tuple[float, float, str], ...] = (
    (0.15, -3.0, "VERY_LOW"),
    (0.30, -1.0, "LOW"),
    (0.60, 1.0, "MODERATE"),
    (math.inf, 3.0, "HIGH"),
)

DIRECTIONAL_RATIO_RULES: Tuple[Tuple[float, float, str], ...] = (
    (0.45, -2.0, "OPPOSING_BARS_DOMINANT"),
    (0.55, 0.0, "MIXED"),
    (0.70, 2.0, "DIRECTIONAL"),
    (math.inf, 3.0, "STRONGLY_DIRECTIONAL"),
)

ROTATION_MAX_DIRECTIONAL_EFFICIENCY = 0.15
ROTATION_MIN_CLOSE_OCCUPANCY = 0.75
ROTATION_PENALTY = -5.0

CANDIDATE_STALENESS_RULES: Tuple[Tuple[float, float, str], ...] = (
    (0.25, 0.0, "FRESH"),
    (0.50, -1.0, "SLIGHTLY_MOVED"),
    (1.00, -4.0, "STALE"),
    (math.inf, -8.0, "VERY_STALE"),
)

PREVIOUS_OPPOSITE_SIGNAL_PENALTY = -7.0
PREVIOUS_SAME_SIDE_SIGNAL_PENALTY = -2.0
SAME_ACCEPTED_RANGE_PENALTY = -3.0
SAME_OPPORTUNITY_PENALTY = -2.0

AUCTION_FLIP_RULES: Tuple[Tuple[float, float, str], ...] = (
    (4.0, 0.0, "LOW"),
    (8.0, -2.0, "MODERATE"),
    (math.inf, -5.0, "HIGH"),
)
HMA_FLIP_RULES: Tuple[Tuple[float, float, str], ...] = (
    (3.0, 0.0, "LOW"),
    (6.0, -1.0, "MODERATE"),
    (math.inf, -3.0, "HIGH"),
)
VWAP_FLIP_RULES: Tuple[Tuple[float, float, str], ...] = (
    (3.0, 0.0, "LOW"),
    (6.0, -1.0, "MODERATE"),
    (math.inf, -2.0, "HIGH"),
)

TIME_RULES: Tuple[Tuple[time, float, str], ...] = (
    (time(15, 15), -25.0, "AFTER_CUTOFF_ZONE"),
    (time(14, 45), -7.0, "VERY_LATE"),
    (time(14, 0), -3.0, "LATE"),
    (time(9, 15), 0.0, "NORMAL"),
)

# Diagnostic only until multi-day evidence supports using it in the score.
ENABLE_EMA_ENVELOPE_SCORE = False
EMA_ENVELOPE_COMPRESSED_THRESHOLD = 0.35
EMA_ENVELOPE_COMPRESSED_PENALTY = -3.0

CORE_FEATURES: Tuple[str, ...] = (
    "advisor_action",
    "session_position",
    "accepted_range_inside",
    "accepted_range_position",
    "directional_extension_atr",
    "path_efficiency",
    "path_directional_ratio",
    "atr",
    "adx",
    "move_15m_atr",
    "move_30m_atr",
    "move_60m_atr",
    "vwap_side",
    "hma_state",
    "price_slope_state",
)


logger = logging.getLogger(__name__)


# =============================================================================
# RESULT TYPES
# =============================================================================

@dataclass(frozen=True)
class ScoreComponent:
    name: str
    category: str
    points: float
    value: Any
    detail: str


@dataclass
class PackageOutcome:
    leg_count: int
    package_pnl: Optional[float]
    entry_exec_time: Optional[datetime]
    exit_exec_time: Optional[datetime]
    fully_entered: bool
    fully_exited: bool


@dataclass
class RankedSignal:
    signal_id: str
    symbol: str
    setup_family: str
    setup_subtype: str
    side: str
    decision_time: datetime
    entry_price: float
    candidate_entry_price: Optional[float]
    opportunity_key: str
    accepted_range_id: str
    advisor_action: str
    advisor_reason_codes: List[str]
    features: Dict[str, Any]
    components: List[ScoreComponent]
    raw_score: float
    context_coverage_pct: float
    missing_features: List[str]
    previous_signal_count: int
    previous_opposite_count: int
    previous_same_side_count: int
    previous_same_range_count: int
    previous_same_opportunity_count: int
    outcome: Optional[PackageOutcome] = None
    global_rank: int = 0
    global_percentile: float = 0.0
    quintile: int = 0
    priority_band: str = ""
    setup_rank: int = 0
    setup_percentile: float = 0.0
    top_flags: Dict[int, bool] = field(default_factory=dict)


# =============================================================================
# GENERIC HELPERS
# =============================================================================

def _enum(value: Any, default: str = "UNKNOWN") -> str:
    text = str(getattr(value, "value", value) or "").strip().upper()
    return text if text else default


def _number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        value = float(value)
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _json_object(value: Any, path: str) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise TypeError(f"{path} must be a JSON object")


def _required_object(parent: Mapping[str, Any], key: str, path: str) -> Dict[str, Any]:
    if key not in parent:
        raise KeyError(f"{path}.{key} is required")
    value = parent[key]
    if not isinstance(value, dict):
        raise TypeError(f"{path}.{key} must be an object")
    return value


def _required_text(parent: Mapping[str, Any], key: str, path: str) -> str:
    if key not in parent:
        raise KeyError(f"{path}.{key} is required")
    text = str(parent[key] or "").strip()
    if not text:
        raise ValueError(f"{path}.{key} must be non-empty")
    return text


def _required_number(parent: Mapping[str, Any], key: str, path: str) -> float:
    if key not in parent:
        raise KeyError(f"{path}.{key} is required")
    value = _number(parent[key])
    if value is None:
        raise ValueError(f"{path}.{key} must be a finite number")
    return value


def _path(root: Mapping[str, Any], *parts: str) -> Any:
    current: Any = root
    for part in parts:
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _aware_ist(value: Any) -> datetime:
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise TypeError(f"Unsupported datetime value: {value!r}")

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def _db_naive(value: datetime) -> datetime:
    return value.astimezone(IST).replace(tzinfo=None)


def _side_sign(side: str) -> float:
    normalized = _enum(side)
    if normalized == "BUY":
        return 1.0
    if normalized == "SELL":
        return -1.0
    raise ValueError(f"Unsupported side: {side!r}")


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _band(
    value: Optional[float],
    rules: Sequence[Tuple[float, float, str]],
) -> Tuple[float, str]:
    if value is None:
        return 0.0, "MISSING"
    for upper, points, label in rules:
        if value < upper:
            return float(points), label
    raise AssertionError("Band rules must terminate with math.inf")


def _add(
    components: List[ScoreComponent],
    name: str,
    category: str,
    points: float,
    value: Any,
    detail: str,
) -> None:
    components.append(
        ScoreComponent(
            name=name,
            category=category,
            points=round(float(points), 6),
            value=value,
            detail=detail,
        )
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def _snapshot_dict(snapshot: SnapshotSchema) -> Dict[str, Any]:
    """Dump the typed snapshot using the persisted JSON aliases.

    ``MarketWindowsBlock`` uses valid Python field names internally
    (``m15``, ``m30``, ``m60``) with JSON aliases ``15m``, ``30m`` and
    ``60m``.  Ranking consumes the persisted snapshot contract, so the dump
    must use aliases exactly as the existing replay/report programs do.
    """
    if hasattr(snapshot, "model_dump"):
        payload = snapshot.model_dump(mode="python", by_alias=True)
    elif hasattr(snapshot, "dict"):
        payload = snapshot.dict(by_alias=True)
    else:
        raise TypeError("SnapshotSchema must provide model_dump() or dict()")

    if not isinstance(payload, dict):
        raise TypeError("SnapshotSchema dump must be an object")

    market_windows = _required_object(payload, "market_windows", "snapshot")
    missing_aliases = [
        key for key in ("15m", "30m", "60m", "sod")
        if key not in market_windows
    ]
    if missing_aliases:
        raise KeyError(
            "snapshot.market_windows alias dump is incomplete; "
            f"missing={missing_aliases} available={sorted(market_windows)}"
        )

    return payload


# =============================================================================
# READS THROUGH EXISTING APPLICATION MODELS / SESSION
# =============================================================================

def _load_signals() -> List[Dict[str, Any]]:
    start = datetime.combine(TEST_DATE, time.min)
    end = start + timedelta(days=1)

    with get_trades_db() as db:
        query = db.query(SignalORM).filter(
            SignalORM.first_seen_time >= start,
            SignalORM.first_seen_time < end,
        )
        if SYMBOLS:
            query = query.filter(SignalORM.symbol.in_([_enum(x) for x in SYMBOLS]))
        if SETUP_FAMILIES:
            query = query.filter(
                SignalORM.setup.in_([_enum(x) for x in SETUP_FAMILIES])
            )
        rows = query.order_by(
            SignalORM.first_seen_time.asc(),
            SignalORM.symbol.asc(),
            SignalORM.signal_id.asc(),
        ).all()

        return [
            {
                "signal_id": str(row.signal_id or "").strip(),
                "symbol": _enum(row.symbol),
                "setup": _enum(row.setup),
                "side": _enum(row.side),
                "first_seen_time": row.first_seen_time,
                "created_price": _number(row.created_price),
                "meta_json": row.meta_json,
            }
            for row in rows
        ]


def _load_outcomes(signal_ids: Sequence[str]) -> Dict[str, PackageOutcome]:
    if not signal_ids:
        return {}

    with get_trades_db() as db:
        query = db.query(TradeORM).filter(TradeORM.signal_id.in_(list(signal_ids)))
        if TEST_USERID is not None:
            query = query.filter(TradeORM.userid == TEST_USERID)
        rows = query.order_by(
            TradeORM.signal_id.asc(),
            TradeORM.entry_exec_time.asc(),
        ).all()

        extracted = [
            {
                "signal_id": str(row.signal_id or "").strip(),
                "exit_pnl": _number(row.exit_pnl),
                "entry_exec_time": row.entry_exec_time,
                "exit_exec_time": row.exit_exec_time,
                "entry_status": _enum(row.entry_status),
                "exit_status": _enum(row.exit_status),
            }
            for row in rows
        ]

    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in extracted:
        if row["signal_id"]:
            grouped[row["signal_id"]].append(row)

    outcomes: Dict[str, PackageOutcome] = {}
    for signal_id, package_rows in grouped.items():
        pnl_values = [
            row["exit_pnl"] for row in package_rows if row["exit_pnl"] is not None
        ]
        entry_times = [
            row["entry_exec_time"]
            for row in package_rows
            if isinstance(row["entry_exec_time"], datetime)
        ]
        exit_times = [
            row["exit_exec_time"]
            for row in package_rows
            if isinstance(row["exit_exec_time"], datetime)
        ]
        outcomes[signal_id] = PackageOutcome(
            leg_count=len(package_rows),
            package_pnl=sum(pnl_values) if pnl_values else None,
            entry_exec_time=min(entry_times) if entry_times else None,
            exit_exec_time=max(exit_times) if exit_times else None,
            fully_entered=bool(package_rows)
            and {row["entry_status"] for row in package_rows} == {"FILLED"},
            fully_exited=bool(package_rows)
            and {row["exit_status"] for row in package_rows} == {"FILLED"},
        )
    return outcomes


# =============================================================================
# CREATION-TIME CONTEXT
# =============================================================================

def _entry_criteria(signal: Mapping[str, Any]) -> Dict[str, Any]:
    meta = _json_object(signal["meta_json"], "signal.meta_json")
    entry = _required_object(meta, "entry_criteria_json", "signal.meta_json")
    if _enum(_required_text(entry, "signal_action", "entry_criteria_json")) != "CREATE":
        raise ValueError("entry_criteria_json.signal_action must be CREATE")
    return entry


def _load_creation_snapshot(symbol: str, decision_time: datetime) -> SnapshotSchema:
    snapshot = SnapshotSchema.fetch_snapshot(symbol, _db_naive(decision_time))
    if snapshot is None:
        raise LookupError(
            f"Creation snapshot not found: {symbol} @ {decision_time.isoformat()}"
        )

    actual_symbol = _enum(getattr(snapshot, "symbol", None))
    actual_time = _aware_ist(getattr(snapshot, "snapshot_time", None))
    if actual_symbol != symbol:
        raise ValueError(
            f"Creation snapshot symbol mismatch: expected={symbol} actual={actual_symbol}"
        )
    if actual_time != decision_time:
        raise ValueError(
            "Creation snapshot time mismatch: "
            f"expected={decision_time.isoformat()} actual={actual_time.isoformat()}"
        )
    return snapshot


def _features(
    entry: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    side: str,
) -> Dict[str, Any]:
    advisor = _required_object(entry, "advisor", "entry_criteria_json")
    advisor_diagnostics = _required_object(
        advisor, "diagnostics", "entry_criteria_json.advisor"
    )
    stock = _required_object(entry, "stock_context", "entry_criteria_json")

    indicators = _required_object(snapshot, "indicators", "snapshot")
    market_windows = _required_object(snapshot, "market_windows", "snapshot")
    structure = _required_object(snapshot, "structure", "snapshot")
    price_action = _required_object(snapshot, "price_action", "snapshot")

    accepted = _path(structure, "accepted")
    accepted = accepted if isinstance(accepted, Mapping) else {}
    accepted_range = _path(accepted, "range")
    accepted_range = accepted_range if isinstance(accepted_range, Mapping) else {}
    accepted_metrics = _path(accepted, "metrics")
    accepted_metrics = accepted_metrics if isinstance(accepted_metrics, Mapping) else {}

    normalized_side = _enum(side)
    if normalized_side == "BUY":
        extension = _number(stock["rise_from_session_low_atr"]) \
            if "rise_from_session_low_atr" in stock else None
        efficiency = _number(stock["path_from_session_low_efficiency"]) \
            if "path_from_session_low_efficiency" in stock else None
        directional_ratio = _number(stock["path_from_session_low_directional_ratio"]) \
            if "path_from_session_low_directional_ratio" in stock else None
        room = _number(stock["distance_to_session_high_atr"]) \
            if "distance_to_session_high_atr" in stock else None
    elif normalized_side == "SELL":
        extension = _number(stock["decline_from_session_high_atr"]) \
            if "decline_from_session_high_atr" in stock else None
        efficiency = _number(stock["path_from_session_high_efficiency"]) \
            if "path_from_session_high_efficiency" in stock else None
        directional_ratio = _number(stock["path_from_session_high_directional_ratio"]) \
            if "path_from_session_high_directional_ratio" in stock else None
        room = _number(stock["distance_to_session_low_atr"]) \
            if "distance_to_session_low_atr" in stock else None
    else:
        raise ValueError(f"Unsupported side: {side}")

    atr = _number(_path(indicators, "atr", "value"))
    entry_price = _required_number(entry, "entry_price", "entry_criteria_json")
    candidate_entry_price = _number(
        entry["candidate_entry_price"] if "candidate_entry_price" in entry else None
    )
    candidate_staleness_atr = None
    if candidate_entry_price is not None and atr is not None and atr > 0:
        candidate_staleness_atr = abs(entry_price - candidate_entry_price) / atr

    reason_codes = advisor["reason_codes"] if "reason_codes" in advisor else []
    if not isinstance(reason_codes, list):
        raise TypeError("entry_criteria_json.advisor.reason_codes must be a list")

    return {
        "advisor_action": _enum(
            _required_text(advisor, "action", "entry_criteria_json.advisor")
        ),
        "advisor_reason_codes": [str(value) for value in reason_codes],
        "advisor_triggered_rules": advisor_diagnostics["triggered_rules"]
        if "triggered_rules" in advisor_diagnostics
        and isinstance(advisor_diagnostics["triggered_rules"], list)
        else [],
        "session_position": _number(stock["session_position"])
        if "session_position" in stock else None,
        "accepted_range_inside": stock["accepted_range_inside"]
        if "accepted_range_inside" in stock else None,
        "accepted_range_position": _number(stock["accepted_range_position"])
        if "accepted_range_position" in stock else None,
        "accepted_range_outside_atr": _number(stock["accepted_range_outside_atr"])
        if "accepted_range_outside_atr" in stock else None,
        "accepted_range_id": str(stock["accepted_range_id"] or "")
        if "accepted_range_id" in stock else "",
        "accepted_range_quality": _number(accepted["quality"])
        if "quality" in accepted else None,
        "accepted_range_age_bars": _number(accepted["age_bars"])
        if "age_bars" in accepted else None,
        "accepted_range_width_atr": _number(accepted_range["width_atr"])
        if "width_atr" in accepted_range else None,
        "accepted_range_directional_efficiency": _number(
            accepted_metrics["directional_efficiency"]
        ) if "directional_efficiency" in accepted_metrics else None,
        "accepted_range_close_occupancy": _number(
            accepted_metrics["close_occupancy_ratio"]
        ) if "close_occupancy_ratio" in accepted_metrics else None,
        "directional_extension_atr": extension,
        "room_to_session_extreme_atr": room,
        "path_efficiency": efficiency,
        "path_directional_ratio": directional_ratio,
        "atr": atr,
        "adx": _number(_path(indicators, "adx", "value")),
        "rsi": _number(_path(indicators, "rsi", "value")),
        "market_window_15m_status": _enum(
            _path(market_windows, "15m", "status")
        ),
        "market_window_30m_status": _enum(
            _path(market_windows, "30m", "status")
        ),
        "market_window_60m_status": _enum(
            _path(market_windows, "60m", "status")
        ),
        "move_15m_atr": _number(_path(market_windows, "15m", "move_atr")),
        "move_30m_atr": _number(_path(market_windows, "30m", "move_atr")),
        "move_60m_atr": _number(_path(market_windows, "60m", "move_atr")),
        "vwap_side": _enum(_path(indicators, "vwap", "side")),
        "vwap_distance_atr": _number(
            _path(indicators, "vwap", "distance_atr")
        ),
        "vwap_flip_count_today": _number(
            _path(indicators, "vwap", "flip_count_today")
        ),
        "hma_state": _enum(_path(indicators, "hma", "state")),
        "hma_strength": _enum(_path(indicators, "hma", "strength")),
        "hma_flip_count_today": _number(
            _path(indicators, "hma", "flip_count_today")
        ),
        "ema_ref": _number(_path(indicators, "ema", "ref")),
        "ema_slow": _number(_path(indicators, "ema", "slow")),
        "ema_envelope": _number(
            _path(indicators, "envelopes", "ema_envelope")
        ),
        "price_slope_state": _enum(_path(price_action, "slope", "state")),
        "auction_state": _enum(
            _required_text(entry, "auction_state", "entry_criteria_json")
        ),
        "auction_flip_count_today": _number(
            structure["flip_count_today"]
            if "flip_count_today" in structure else None
        ),
        "candidate_staleness_atr": candidate_staleness_atr,
    }


# =============================================================================
# SCORE
# =============================================================================

def _directional_points(
    move_atr: Optional[float],
    side: str,
    maximum: float,
    full_score_atr: float,
) -> float:
    if move_atr is None:
        return 0.0
    aligned = _side_sign(side) * move_atr
    return _clip(aligned / full_score_atr, -1.0, 1.0) * maximum


def _time_points(decision_time: datetime) -> Tuple[float, str]:
    local = decision_time.astimezone(IST).time().replace(tzinfo=None)
    for start, points, label in TIME_RULES:
        if local >= start:
            return points, label
    return 0.0, "UNCLASSIFIED"


def _score(
    *,
    signal_id: str,
    symbol: str,
    setup_family: str,
    setup_subtype: str,
    side: str,
    decision_time: datetime,
    entry_price: float,
    candidate_entry_price: Optional[float],
    opportunity_key: str,
    accepted_range_id: str,
    advisor_action: str,
    advisor_reason_codes: List[str],
    features: Dict[str, Any],
    history: Sequence[RankedSignal],
) -> RankedSignal:
    components: List[ScoreComponent] = []
    setup = _enum(setup_family)
    signal_side = _enum(side)

    _add(
        components,
        "setup_base",
        "SETUP",
        SETUP_BASE_POINTS[setup]
        if setup in SETUP_BASE_POINTS
        else SETUP_BASE_POINTS["UNKNOWN"],
        setup,
        "Initial setup-family base",
    )

    _add(
        components,
        "advisor_action",
        "ADVISOR",
        ADVISOR_POINTS[advisor_action]
        if advisor_action in ADVISOR_POINTS
        else ADVISOR_POINTS["UNKNOWN"],
        advisor_action,
        "Creation-time Advisor action",
    )

    points, label = _band(features["adx"], ADX_RULES)
    _add(components, "adx_context", "STRUCTURE", points, features["adx"], label)

    window_weights = (
        WINDOW_MAX_POINTS[setup]
        if setup in WINDOW_MAX_POINTS
        else WINDOW_MAX_POINTS["UNKNOWN"]
    )
    for window in ("15m", "30m", "60m"):
        move = features[f"move_{window}_atr"]
        _add(
            components,
            f"{window}_directional_alignment",
            "IMPULSE",
            _directional_points(
                move,
                signal_side,
                window_weights[window],
                WINDOW_FULL_SCORE_ATR[window],
            ),
            move,
            "Side-aligned movement available at signal creation",
        )

    slope_state = features["price_slope_state"]
    aligned_token = "UP" if signal_side == "BUY" else "DOWN"
    opposite_token = "DOWN" if signal_side == "BUY" else "UP"
    if aligned_token in slope_state:
        points, detail = PRICE_SLOPE_ALIGNED_POINTS, "ALIGNED"
    elif opposite_token in slope_state:
        points, detail = PRICE_SLOPE_OPPOSED_POINTS, "OPPOSED"
    else:
        points, detail = 0.0, "NEUTRAL_OR_MISSING"
    _add(
        components,
        "price_slope_alignment",
        "IMPULSE",
        points,
        slope_state,
        detail,
    )

    vwap_side = features["vwap_side"]
    expected_vwap = "ABOVE" if signal_side == "BUY" else "BELOW"
    if vwap_side == expected_vwap:
        points, detail = VWAP_ALIGNED_POINTS, "ALIGNED"
    elif vwap_side in {"ABOVE", "BELOW"}:
        points, detail = VWAP_OPPOSED_POINTS, "OPPOSED"
    else:
        points, detail = 0.0, "NEUTRAL_OR_MISSING"
    _add(components, "vwap_alignment", "STRUCTURE", points, vwap_side, detail)

    vwap_distance = features["vwap_distance_atr"]
    vwap_extension_points = (
        VWAP_EXTENSION_PENALTY
        if vwap_distance is not None and abs(vwap_distance) > VWAP_EXTENSION_ATR
        else 0.0
    )
    _add(
        components,
        "vwap_extension",
        "EXTENSION",
        vwap_extension_points,
        vwap_distance,
        "EXTENDED" if vwap_extension_points else "NOT_EXTENDED_OR_MISSING",
    )

    hma_state = features["hma_state"]
    if hma_state == signal_side:
        points, detail = HMA_ALIGNED_POINTS, "ALIGNED"
    elif hma_state in {"BUY", "SELL"}:
        points = (
            HMA_OPPOSED_POINTS[setup]
            if setup in HMA_OPPOSED_POINTS
            else HMA_OPPOSED_POINTS["UNKNOWN"]
        )
        detail = "OPPOSED"
    else:
        points, detail = 0.0, "NEUTRAL_OR_MISSING"
    _add(components, "hma_alignment", "STRUCTURE", points, hma_state, detail)

    quality = features["accepted_range_quality"]
    quality_points = 0.0
    if quality is not None:
        quality_points = (
            _clip(quality / ACCEPTED_RANGE_QUALITY_FULL_SCORE, 0.0, 1.0)
            * ACCEPTED_RANGE_QUALITY_MAX_POINTS
        )
    _add(
        components,
        "accepted_range_quality",
        "RANGE",
        quality_points,
        quality,
        "Scaled accepted-range quality",
    )

    range_inside = features["accepted_range_inside"]
    range_position = features["accepted_range_position"]
    session_position = features["session_position"]

    if setup == "ACCEPTED_BREAKOUT":
        if range_inside is True:
            _add(
                components,
                "accepted_breakout_inside_range",
                "RANGE",
                ACCEPTED_BREAKOUT_INSIDE_PENALTY,
                range_inside,
                "Creation price remained inside accepted range",
            )
        else:
            points, label = _band(
                features["accepted_range_outside_atr"],
                ACCEPTED_BREAKOUT_OUTSIDE_RULES,
            )
            _add(
                components,
                "accepted_breakout_outside_distance",
                "RANGE",
                points,
                features["accepted_range_outside_atr"],
                label,
            )

    if setup == "REVERSAL":
        if range_position is not None:
            edge_distance = min(range_position, 1.0 - range_position)
            if edge_distance <= 0.20:
                points, detail = REVERSAL_ACCEPTED_EDGE_POINTS, "NEAR_RANGE_EDGE"
            elif 0.35 <= range_position <= 0.65:
                points, detail = (
                    REVERSAL_ACCEPTED_INTERIOR_PENALTY,
                    "RANGE_INTERIOR",
                )
            else:
                points, detail = 0.0, "MID_EDGE_TRANSITION"
            _add(
                components,
                "reversal_accepted_range_location",
                "RANGE",
                points,
                range_position,
                detail,
            )

        if session_position is not None:
            edge_distance = min(session_position, 1.0 - session_position)
            if edge_distance <= 0.15:
                points, detail = REVERSAL_SESSION_EDGE_POINTS, "NEAR_SESSION_EDGE"
            elif 0.35 <= session_position <= 0.65:
                points, detail = (
                    REVERSAL_SESSION_INTERIOR_PENALTY,
                    "SESSION_INTERIOR",
                )
            else:
                points, detail = 0.0, "MID_EDGE_TRANSITION"
            _add(
                components,
                "reversal_session_location",
                "RANGE",
                points,
                session_position,
                detail,
            )

        points, label = _band(
            features["room_to_session_extreme_atr"],
            REVERSAL_ROOM_RULES,
        )
        _add(
            components,
            "reversal_remaining_session_room",
            "OPPORTUNITY",
            points,
            features["room_to_session_extreme_atr"],
            label,
        )

        directional_efficiency = features[
            "accepted_range_directional_efficiency"
        ]
        close_occupancy = features["accepted_range_close_occupancy"]
        rotational = (
            directional_efficiency is not None
            and close_occupancy is not None
            and directional_efficiency <= ROTATION_MAX_DIRECTIONAL_EFFICIENCY
            and close_occupancy >= ROTATION_MIN_CLOSE_OCCUPANCY
        )
        _add(
            components,
            "reversal_rotational_range",
            "CHURN",
            ROTATION_PENALTY if rotational else 0.0,
            {
                "directional_efficiency": directional_efficiency,
                "close_occupancy": close_occupancy,
            },
            "ROTATIONAL" if rotational else "NOT_CONFIRMED_OR_MISSING",
        )

    points, label = _band(
        features["directional_extension_atr"],
        DIRECTIONAL_EXTENSION_RULES,
    )
    _add(
        components,
        "directional_extension",
        "EXTENSION",
        points,
        features["directional_extension_atr"],
        label,
    )

    points, label = _band(features["path_efficiency"], PATH_EFFICIENCY_RULES)
    _add(
        components,
        "path_efficiency",
        "PATH",
        points,
        features["path_efficiency"],
        label,
    )

    points, label = _band(
        features["path_directional_ratio"],
        DIRECTIONAL_RATIO_RULES,
    )
    _add(
        components,
        "path_directional_ratio",
        "PATH",
        points,
        features["path_directional_ratio"],
        label,
    )

    points, label = _band(
        features["candidate_staleness_atr"],
        CANDIDATE_STALENESS_RULES,
    )
    _add(
        components,
        "candidate_deployment_staleness",
        "FRESHNESS",
        points,
        features["candidate_staleness_atr"],
        label,
    )

    cutoff = decision_time - timedelta(minutes=CHURN_LOOKBACK_MINUTES)
    previous = [
        row
        for row in history
        if row.symbol == symbol and cutoff <= row.decision_time < decision_time
    ]
    opposite = [row for row in previous if row.side != signal_side]
    same_side = [row for row in previous if row.side == signal_side]
    same_range = [
        row
        for row in previous
        if accepted_range_id
        and row.accepted_range_id
        and row.accepted_range_id == accepted_range_id
    ]
    same_opportunity = [
        row
        for row in previous
        if opportunity_key
        and row.opportunity_key
        and row.opportunity_key == opportunity_key
    ]

    _add(
        components,
        "recent_opposite_signals",
        "CHURN",
        min(len(opposite), MAX_PREVIOUS_OPPOSITE_SIGNALS)
        * PREVIOUS_OPPOSITE_SIGNAL_PENALTY,
        len(opposite),
        f"{CHURN_LOOKBACK_MINUTES}-minute lookback",
    )
    _add(
        components,
        "recent_same_side_signals",
        "CHURN",
        min(len(same_side), MAX_PREVIOUS_SAME_SIDE_SIGNALS)
        * PREVIOUS_SAME_SIDE_SIGNAL_PENALTY,
        len(same_side),
        f"{CHURN_LOOKBACK_MINUTES}-minute lookback",
    )
    _add(
        components,
        "same_accepted_range_history",
        "CHURN",
        SAME_ACCEPTED_RANGE_PENALTY if same_range else 0.0,
        len(same_range),
        "Earlier signal in the same accepted-range identity",
    )
    _add(
        components,
        "same_opportunity_history",
        "CHURN",
        SAME_OPPORTUNITY_PENALTY if same_opportunity else 0.0,
        len(same_opportunity),
        "Earlier signal in the same opportunity identity",
    )

    for feature_name, component_name, rules in (
        ("auction_flip_count_today", "auction_flip_count", AUCTION_FLIP_RULES),
        ("hma_flip_count_today", "hma_flip_count", HMA_FLIP_RULES),
        ("vwap_flip_count_today", "vwap_flip_count", VWAP_FLIP_RULES),
    ):
        points, label = _band(features[feature_name], rules)
        _add(
            components,
            component_name,
            "CHURN",
            points,
            features[feature_name],
            label,
        )

    points, label = _time_points(decision_time)
    _add(
        components,
        "time_of_day",
        "TIME",
        points,
        decision_time.astimezone(IST).time().isoformat(),
        label,
    )

    ema_envelope = features["ema_envelope"]
    compressed = (
        ema_envelope is not None
        and ema_envelope <= EMA_ENVELOPE_COMPRESSED_THRESHOLD
    )
    _add(
        components,
        "ema_envelope_compression",
        "EXPERIMENTAL",
        EMA_ENVELOPE_COMPRESSED_PENALTY
        if ENABLE_EMA_ENVELOPE_SCORE and compressed
        else 0.0,
        ema_envelope,
        (
            "COMPRESSED" if compressed else "NOT_COMPRESSED_OR_MISSING"
        ) + (
            "_SCORED" if ENABLE_EMA_ENVELOPE_SCORE else "_DIAGNOSTIC_ONLY"
        ),
    )

    missing = [
        key
        for key in CORE_FEATURES
        if features[key] is None or features[key] == "UNKNOWN"
    ]
    coverage = round(
        100.0 * (len(CORE_FEATURES) - len(missing)) / len(CORE_FEATURES),
        2,
    )

    return RankedSignal(
        signal_id=signal_id,
        symbol=symbol,
        setup_family=setup,
        setup_subtype=setup_subtype,
        side=signal_side,
        decision_time=decision_time,
        entry_price=entry_price,
        candidate_entry_price=candidate_entry_price,
        opportunity_key=opportunity_key,
        accepted_range_id=accepted_range_id,
        advisor_action=advisor_action,
        advisor_reason_codes=advisor_reason_codes,
        features=features,
        components=components,
        raw_score=round(sum(component.points for component in components), 6),
        context_coverage_pct=coverage,
        missing_features=missing,
        previous_signal_count=len(previous),
        previous_opposite_count=len(opposite),
        previous_same_side_count=len(same_side),
        previous_same_range_count=len(same_range),
        previous_same_opportunity_count=len(same_opportunity),
    )


def _assign_ranks(rows: List[RankedSignal]) -> None:
    rows.sort(
        key=lambda row: (
            -row.raw_score,
            row.decision_time,
            row.symbol,
            row.signal_id,
        )
    )

    total = len(rows)
    for index, row in enumerate(rows, start=1):
        row.global_rank = index
        row.global_percentile = round(
            100.0 * (total - index + 1) / total,
            4,
        )
        row.quintile = min(
            QUINTILE_COUNT,
            math.ceil(index * QUINTILE_COUNT / total),
        )
        row.top_flags = {
            percentile: index <= math.ceil(total * percentile / 100.0)
            for percentile in TOP_PERCENTILES
        }
        for threshold, band in PRIORITY_BANDS:
            if row.global_percentile >= threshold:
                row.priority_band = band
                break

    by_setup: Dict[str, List[RankedSignal]] = defaultdict(list)
    for row in rows:
        by_setup[row.setup_family].append(row)

    for group in by_setup.values():
        group.sort(
            key=lambda row: (
                -row.raw_score,
                row.decision_time,
                row.symbol,
                row.signal_id,
            )
        )
        group_total = len(group)
        for index, row in enumerate(group, start=1):
            row.setup_rank = index
            row.setup_percentile = round(
                100.0 * (group_total - index + 1) / group_total,
                4,
            )


# =============================================================================
# REPORTING
# =============================================================================

def _outcome_values(rows: Sequence[RankedSignal]) -> List[float]:
    return [
        row.outcome.package_pnl
        for row in rows
        if row.outcome is not None and row.outcome.package_pnl is not None
    ]


def _profit_factor(values: Sequence[float]) -> Optional[float]:
    gross_profit = sum(value for value in values if value > 0)
    gross_loss = abs(sum(value for value in values if value < 0))
    if gross_loss == 0:
        return math.inf if gross_profit > 0 else None
    return gross_profit / gross_loss


def _metrics(rows: Sequence[RankedSignal]) -> Dict[str, Any]:
    values = _outcome_values(rows)
    wins = sum(value > 0 for value in values)
    losses = sum(value < 0 for value in values)
    flats = sum(value == 0 for value in values)
    nonflat = wins + losses
    pf = _profit_factor(values)

    return {
        "signals": len(rows),
        "signals_with_outcome": len(values),
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "win_rate_pct": round(100.0 * wins / nonflat, 4) if nonflat else None,
        "total_pnl": round(sum(values), 4) if values else None,
        "average_pnl": round(statistics.fmean(values), 4) if values else None,
        "median_pnl": round(statistics.median(values), 4) if values else None,
        "profit_factor": (
            "INF" if pf == math.inf else round(pf, 6) if pf is not None else None
        ),
        "best_package": round(max(values), 4) if values else None,
        "worst_package": round(min(values), 4) if values else None,
    }


def _positive_reasons(row: RankedSignal) -> str:
    values = sorted(
        [component for component in row.components if component.points > 0],
        key=lambda component: -component.points,
    )
    return " | ".join(
        f"{component.name}:{component.points:+g}({component.detail})"
        for component in values[:8]
    )


def _penalty_reasons(row: RankedSignal) -> str:
    values = sorted(
        [component for component in row.components if component.points < 0],
        key=lambda component: component.points,
    )
    return " | ".join(
        f"{component.name}:{component.points:+g}({component.detail})"
        for component in values[:8]
    )


def _ranked_row(row: RankedSignal) -> Dict[str, Any]:
    outcome = row.outcome
    result: Dict[str, Any] = {
        "ranking_version": RANKING_VERSION,
        "global_rank": row.global_rank,
        "global_percentile": row.global_percentile,
        "quintile": row.quintile,
        "priority_band": row.priority_band,
        "setup_rank": row.setup_rank,
        "setup_percentile": row.setup_percentile,
        "raw_priority_score": row.raw_score,
        "signal_id": row.signal_id,
        "symbol": row.symbol,
        "setup_family": row.setup_family,
        "setup_subtype": row.setup_subtype,
        "side": row.side,
        "decision_time": row.decision_time.isoformat(),
        "entry_price": row.entry_price,
        "candidate_entry_price": row.candidate_entry_price,
        "candidate_staleness_atr": row.features["candidate_staleness_atr"],
        "opportunity_key": row.opportunity_key,
        "accepted_range_id": row.accepted_range_id,
        "advisor_action": row.advisor_action,
        "advisor_reason_codes": "|".join(row.advisor_reason_codes),
        "context_coverage_pct": row.context_coverage_pct,
        "missing_features": "|".join(row.missing_features),
        "previous_signal_count": row.previous_signal_count,
        "previous_opposite_count": row.previous_opposite_count,
        "previous_same_side_count": row.previous_same_side_count,
        "previous_same_range_count": row.previous_same_range_count,
        "previous_same_opportunity_count": row.previous_same_opportunity_count,
        "positive_score_reasons": _positive_reasons(row),
        "penalty_score_reasons": _penalty_reasons(row),
    }

    for key in (
        "session_position",
        "accepted_range_inside",
        "accepted_range_position",
        "accepted_range_outside_atr",
        "accepted_range_quality",
        "accepted_range_age_bars",
        "accepted_range_width_atr",
        "accepted_range_directional_efficiency",
        "accepted_range_close_occupancy",
        "directional_extension_atr",
        "room_to_session_extreme_atr",
        "path_efficiency",
        "path_directional_ratio",
        "atr",
        "adx",
        "rsi",
        "market_window_15m_status",
        "market_window_30m_status",
        "market_window_60m_status",
        "move_15m_atr",
        "move_30m_atr",
        "move_60m_atr",
        "price_slope_state",
        "vwap_side",
        "vwap_distance_atr",
        "vwap_flip_count_today",
        "hma_state",
        "hma_strength",
        "hma_flip_count_today",
        "ema_ref",
        "ema_slow",
        "ema_envelope",
        "auction_state",
        "auction_flip_count_today",
    ):
        result[key] = row.features[key]

    for percentile in TOP_PERCENTILES:
        result[f"top_{percentile}_percent"] = row.top_flags[percentile]

    result.update(
        {
            "has_trade_package_evaluation_only": outcome is not None,
            "trade_leg_count_evaluation_only": outcome.leg_count if outcome else 0,
            "package_fully_entered_evaluation_only": outcome.fully_entered
            if outcome else False,
            "package_fully_exited_evaluation_only": outcome.fully_exited
            if outcome else False,
            "entry_exec_time_evaluation_only": outcome.entry_exec_time
            if outcome else None,
            "exit_exec_time_evaluation_only": outcome.exit_exec_time
            if outcome else None,
            "realized_package_pnl_evaluation_only": outcome.package_pnl
            if outcome else None,
            "realized_result_evaluation_only": (
                "WIN"
                if outcome and outcome.package_pnl is not None and outcome.package_pnl > 0
                else "LOSS"
                if outcome and outcome.package_pnl is not None and outcome.package_pnl < 0
                else "FLAT"
                if outcome and outcome.package_pnl == 0
                else "NO_OUTCOME"
            ),
            "score_components_json": json.dumps(
                [asdict(component) for component in row.components],
                separators=(",", ":"),
                default=_json_default,
            ),
        }
    )
    return result


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


def _config() -> Dict[str, Any]:
    return {
        "ranking_version": RANKING_VERSION,
        "test_date": TEST_DATE,
        "symbols": SYMBOLS,
        "setup_families": SETUP_FAMILIES,
        "test_userid": TEST_USERID,
        "top_percentiles": TOP_PERCENTILES,
        "quintile_count": QUINTILE_COUNT,
        "priority_bands": PRIORITY_BANDS,
        "churn_lookback_minutes": CHURN_LOOKBACK_MINUTES,
        "advisor_points": ADVISOR_POINTS,
        "setup_base_points": SETUP_BASE_POINTS,
        "window_max_points": WINDOW_MAX_POINTS,
        "window_full_score_atr": WINDOW_FULL_SCORE_ATR,
        "adx_rules": ADX_RULES,
        "accepted_breakout_outside_rules": ACCEPTED_BREAKOUT_OUTSIDE_RULES,
        "reversal_room_rules": REVERSAL_ROOM_RULES,
        "directional_extension_rules": DIRECTIONAL_EXTENSION_RULES,
        "path_efficiency_rules": PATH_EFFICIENCY_RULES,
        "directional_ratio_rules": DIRECTIONAL_RATIO_RULES,
        "candidate_staleness_rules": CANDIDATE_STALENESS_RULES,
        "time_rules": TIME_RULES,
        "ema_envelope_score_enabled": ENABLE_EMA_ENVELOPE_SCORE,
    }


def _write_reports(
    ranked: List[RankedSignal],
    errors: List[Dict[str, Any]],
    started_at: datetime,
) -> Dict[str, Path]:
    stamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
    stem = REPORT_DIR / f"{REPORT_PREFIX}_{TEST_DATE.isoformat()}_{stamp}"

    paths = {
        "ranked": stem.with_name(stem.name + "_ranked.csv"),
        "quintiles": stem.with_name(stem.name + "_quintiles.csv"),
        "top_percentiles": stem.with_name(stem.name + "_top_percentiles.csv"),
        "setups": stem.with_name(stem.name + "_setups.csv"),
        "errors": stem.with_name(stem.name + "_errors.csv"),
        "summary": stem.with_name(stem.name + "_summary.json"),
    }

    _write_csv(paths["ranked"], [_ranked_row(row) for row in ranked])

    quintile_rows: List[Dict[str, Any]] = []
    for quintile in range(1, QUINTILE_COUNT + 1):
        result = {"quintile": quintile}
        result.update(_metrics([row for row in ranked if row.quintile == quintile]))
        quintile_rows.append(result)
    _write_csv(paths["quintiles"], quintile_rows)

    percentile_rows: List[Dict[str, Any]] = []
    for percentile in TOP_PERCENTILES:
        result = {"selection": f"TOP_{percentile}_PERCENT"}
        result.update(
            _metrics([row for row in ranked if row.top_flags[percentile]])
        )
        percentile_rows.append(result)
    overall = {"selection": "ALL_SIGNALS"}
    overall.update(_metrics(ranked))
    percentile_rows.append(overall)
    _write_csv(paths["top_percentiles"], percentile_rows)

    setup_rows: List[Dict[str, Any]] = []
    for setup in sorted({row.setup_family for row in ranked}):
        result = {"setup_family": setup}
        result.update(
            _metrics([row for row in ranked if row.setup_family == setup])
        )
        setup_rows.append(result)
    _write_csv(paths["setups"], setup_rows)
    _write_csv(paths["errors"], errors)

    config = _config()
    config_hash = hashlib.sha256(
        json.dumps(
            config,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        ).encode("utf-8")
    ).hexdigest()[:16]

    summary = {
        "ranking_version": RANKING_VERSION,
        "test_date": TEST_DATE,
        "started_at": started_at,
        "completed_at": datetime.now(IST),
        "config_hash": config_hash,
        "config": config,
        "read_only": True,
        "database_access": "get_trades_db + existing ORM models",
        "snapshot_access": "SnapshotSchema.fetch_snapshot",
        "snapshot_dump": "SnapshotSchema.model_dump(by_alias=True)",
        "market_window_json_keys": ["15m", "30m", "60m", "sod"],
        "score_excludes": [
            "signal_mfe",
            "signal_mae",
            "package_mfe",
            "package_mae",
            "exit_price",
            "exit_reason",
            "exit_time",
            "later_signal_lifecycle",
            "later_auction_transitions",
            "future_snapshots",
            "realized_pnl",
        ],
        "overall": _metrics(ranked),
        "record_errors": len(errors),
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
    signal_rows = _load_signals()
    if not signal_rows:
        raise RuntimeError(
            f"No signals found for TEST_DATE={TEST_DATE.isoformat()}"
        )

    logger.info(
        "Ranking input | date=%s signals=%d symbols=%s setups=%s userid=%s",
        TEST_DATE,
        len(signal_rows),
        SYMBOLS or "ALL",
        SETUP_FAMILIES or "ALL",
        TEST_USERID or "ALL_USERS",
    )

    ranked: List[RankedSignal] = []
    history: List[RankedSignal] = []
    errors: List[Dict[str, Any]] = []

    for signal in signal_rows:
        signal_id = signal["signal_id"]
        symbol = signal["symbol"]
        try:
            if not signal_id:
                raise ValueError("signal_id is required")
            if not symbol or symbol == "UNKNOWN":
                raise ValueError("signal.symbol is required")

            entry = _entry_criteria(signal)
            decision_time = _aware_ist(
                _required_text(entry, "snapshot_time", "entry_criteria_json")
            )
            setup_family = _enum(
                _required_text(entry, "setup_family", "entry_criteria_json")
            )
            setup_subtype = _enum(
                _required_text(entry, "setup_subtype", "entry_criteria_json")
            )
            side = _enum(
                _required_text(entry, "side", "entry_criteria_json")
            )
            entry_price = _required_number(
                entry, "entry_price", "entry_criteria_json"
            )
            candidate_entry_price = _number(
                entry["candidate_entry_price"]
                if "candidate_entry_price" in entry else None
            )
            opportunity_key = _required_text(
                entry, "opportunity_key", "entry_criteria_json"
            )

            snapshot = _load_creation_snapshot(symbol, decision_time)
            feature_values = _features(
                entry,
                _snapshot_dict(snapshot),
                side,
            )

            row = _score(
                signal_id=signal_id,
                symbol=symbol,
                setup_family=setup_family,
                setup_subtype=setup_subtype,
                side=side,
                decision_time=decision_time,
                entry_price=entry_price,
                candidate_entry_price=candidate_entry_price,
                opportunity_key=opportunity_key,
                accepted_range_id=str(feature_values["accepted_range_id"] or ""),
                advisor_action=feature_values["advisor_action"],
                advisor_reason_codes=list(
                    feature_values["advisor_reason_codes"]
                ),
                features=feature_values,
                history=history,
            )
            ranked.append(row)
            history.append(row)

        except Exception as exc:
            logger.exception(
                "Per-signal ranking failed; continuing | signal_id=%s symbol=%s",
                signal_id,
                symbol,
            )
            errors.append(
                {
                    "signal_id": signal_id,
                    "symbol": symbol,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }
            )

    if not ranked:
        raise RuntimeError("No signals were successfully ranked")

    # Assign score/rank before outcome data is read.
    _assign_ranks(ranked)

    # Evaluation-only join after ranking.
    outcomes = _load_outcomes([row.signal_id for row in ranked])
    for row in ranked:
        row.outcome = outcomes[row.signal_id] if row.signal_id in outcomes else None

    paths = _write_reports(ranked, errors, started_at)

    logger.info(
        "Ranking complete | ranked=%d errors=%d outcomes=%d overall=%s",
        len(ranked),
        len(errors),
        len(outcomes),
        _metrics(ranked),
    )
    for name, path in paths.items():
        logger.info("Report | %s=%s", name, path)

    print("Signal ranking complete")
    print(f"  date={TEST_DATE.isoformat()}")
    print(f"  ranked={len(ranked)}")
    print(f"  errors={len(errors)}")
    print(f"  outcomes={len(outcomes)}")
    print(f"  overall={_metrics(ranked)}")
    print(f"  ranked_report={paths['ranked']}")
    print(f"  summary={paths['summary']}")

    return 2 if errors and FAIL_IF_RECORD_ERRORS else 0


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    setup_logging(log_file=LOG_FILE)

    global logger
    logger = logging.getLogger(__name__)
    logger.info(
        "Starting test_ranking_signals | date=%s version=%s "
        "read_only=True app_db=True future_data_in_score=False",
        TEST_DATE,
        RANKING_VERSION,
    )

    try:
        return run()
    except KeyboardInterrupt:
        logger.info("test_ranking_signals interrupted")
        return 130
    except Exception:
        logger.exception("test_ranking_signals failed during startup/preflight")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
