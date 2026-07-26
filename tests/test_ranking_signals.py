#!/usr/bin/env python3
"""
test_ranking_signals.py

Offline / shadow signal-ranking study for AutoTrades.

Purpose
-------
Read signals already persisted in MySQL for one trading date, reconstruct each
signal's creation-time context, calculate a transparent priority score, and
evaluate whether higher-ranked signals outperform lower-ranked signals.

Safety boundary
---------------
* READ-ONLY database access.
* Does not modify signals, snapshots, opportunities, trades, configuration, or live services.
* Does not import or call live signal-generation / trade-generation code.
* MFE, MAE, exit reason, exit price, later lifecycle state, and future candles are
  NEVER used as ranking inputs.
* Realized package P&L is joined only after scoring, exclusively for report evaluation.

Expected current AutoTrades fields
----------------------------------
signals:
    signal_id, symbol, setup, side, first_seen_time, actionable_time,
    created_price, meta_json

meta_json:
    entry_criteria_json  # immutable criteria captured when the signal was created

snapshots:
    symbol, snapshot_time, snapshot_json

user_trades:
    signal_id, exit_pnl, entry_exec_time, exit_exec_time,
    entry_status, exit_status

The table and column names are configurable below.

Typical usage
-------------
    cd /var/www/autotrades
    source venv/bin/activate

    # Edit TEST_DATE and DB settings below, then:
    python tests/test_ranking_signals.py

    # Optional date override:
    python tests/test_ranking_signals.py --date 2026-07-24

Output
------
reports/signal_ranking/<date>/<run_id>/
    signal_ranking_ranked.csv
    signal_ranking_quintiles.csv
    signal_ranking_top_percentiles.csv
    signal_ranking_setups.csv
    signal_ranking_summary.md
    signal_ranking_manifest.json
    signal_ranking_errors.csv
    test_ranking_signals.log

This is an evidence-generation program. A good result on one historical day is
not sufficient to deploy the score live. Freeze a ranking version and validate it
walk-forward on unseen dates.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import os
import re
import statistics
import sys
import traceback
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

import mysql.connector
from mysql.connector import Error as MySQLError


# =============================================================================
# USER CONFIGURATION — EDIT HERE
# =============================================================================

# ---- Run scope ---------------------------------------------------------------

TEST_DATE = "2026-07-24"
LOCAL_TIMEZONE = "Asia/Kolkata"

# Optional subset. Empty tuple means all signals created on TEST_DATE.
SYMBOLS: tuple[str, ...] = ()

# Optional setup subset. Empty tuple means all setup families.
SETUP_FAMILIES: tuple[str, ...] = ()

# Signals with no creation-time snapshot can either be excluded from ranking
# (recommended) or scored using only immutable entry_criteria_json fields.
REQUIRE_CREATION_SNAPSHOT = True

# A malformed individual record is written to errors.csv and processing continues.
# Startup/preflight failures still terminate the program.
FAIL_IF_ANY_RECORD_ERROR = False

# ---- Database ---------------------------------------------------------------

# Preferred: set AUTOTRADES_DATABASE_URL or DATABASE_URL:
# mysql://autotrades:password@127.0.0.1:3306/backtest
DATABASE_URL = (
    os.getenv("AUTOTRADES_DATABASE_URL")
    or os.getenv("DATABASE_URL")
    or ""
)

# Used when DATABASE_URL is empty.
DB_HOST = os.getenv("AUTOTRADES_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("AUTOTRADES_DB_PORT", "3306"))
DB_USER = os.getenv("AUTOTRADES_DB_USER", "autotrades")
DB_PASSWORD = os.getenv("AUTOTRADES_DB_PASSWORD", "")
DB_NAME = os.getenv("AUTOTRADES_DB_NAME", "backtest")
DB_CHARSET = "utf8mb4"
DB_CONNECTION_TIMEOUT_SECONDS = 20

# Table/column names are explicit so schema assumptions fail visibly.
SIGNALS_TABLE = "signals"
SIGNAL_ID_COLUMN = "signal_id"
SIGNAL_SYMBOL_COLUMN = "symbol"
SIGNAL_SETUP_COLUMN = "setup"
SIGNAL_SIDE_COLUMN = "side"
SIGNAL_FIRST_SEEN_COLUMN = "first_seen_time"
SIGNAL_ACTIONABLE_COLUMN = "actionable_time"
SIGNAL_CREATED_PRICE_COLUMN = "created_price"
SIGNAL_META_JSON_COLUMN = "meta_json"

SNAPSHOTS_TABLE = "snapshots"
SNAPSHOT_SYMBOL_COLUMN = "symbol"
SNAPSHOT_TIME_COLUMN = "snapshot_time"
SNAPSHOT_JSON_COLUMN = "snapshot_json"

TRADES_TABLE = "user_trades"
TRADE_SIGNAL_ID_COLUMN = "signal_id"
TRADE_EXIT_PNL_COLUMN = "exit_pnl"
TRADE_ENTRY_EXEC_TIME_COLUMN = "entry_exec_time"
TRADE_EXIT_EXEC_TIME_COLUMN = "exit_exec_time"
TRADE_ENTRY_STATUS_COLUMN = "entry_status"
TRADE_EXIT_STATUS_COLUMN = "exit_status"

# ---- Output -----------------------------------------------------------------

OUTPUT_ROOT = Path("reports/signal_ranking")
OUTPUT_PREFIX = "signal_ranking"
INCLUDE_REALIZED_OUTCOMES = True
WRITE_COMPONENT_JSON = True

# ---- Ranking identity --------------------------------------------------------

RANKING_VERSION = "SIGNAL_PRIORITY_V1_SHADOW"
RANKING_DESCRIPTION = (
    "Transparent decision-time score using Advisor, setup, location, "
    "directional alignment, range quality, extension, churn and timing."
)

TOP_PERCENTILES = (10, 20, 30)
QUINTILE_COUNT = 5

# Do not interpret these bands as live permission. They are report labels.
PRIORITY_BAND_PERCENTILES = {
    "A": 80.0,  # top 20%
    "B": 60.0,  # next 20%
    "C": 30.0,  # next 30%
    "D": 0.0,   # bottom 30%
}

# ---- Decision-time history ---------------------------------------------------

CHURN_LOOKBACK_MINUTES = 60
MAX_PREVIOUS_OPPOSITE_SIGNALS_COUNTED = 2
MAX_PREVIOUS_SAME_SIDE_SIGNALS_COUNTED = 2

# ---- Score weights -----------------------------------------------------------

ADVISOR_ACTION_POINTS = {
    "ALLOW": 10.0,
    "WATCH": 2.0,
    "BLOCK": -30.0,
    "NO_ACTION": 0.0,
    "UNKNOWN": -2.0,
}

SETUP_BASE_POINTS = {
    # Keep equal initially. Do not encode July 24 realized setup performance.
    "ACCEPTED_BREAKOUT": 0.0,
    "REVERSAL": 0.0,
    "FAILED_BREAKOUT": 0.0,
    "BREAKOUT_INITIATION": 0.0,
    "UNKNOWN": -5.0,
}

# Directional move windows. Values are maximum absolute points contributed by
# an aligned/opposed move. Reversal deliberately emphasizes the immediate 15m
# impulse more than the older 60m path.
WINDOW_MAX_POINTS = {
    "ACCEPTED_BREAKOUT": {"15m": 4.0, "30m": 4.0, "60m": 3.0},
    "REVERSAL": {"15m": 6.0, "30m": 3.0, "60m": 1.0},
    "FAILED_BREAKOUT": {"15m": 6.0, "30m": 4.0, "60m": 2.0},
    "BREAKOUT_INITIATION": {"15m": 5.0, "30m": 4.0, "60m": 2.0},
    "UNKNOWN": {"15m": 3.0, "30m": 2.0, "60m": 1.0},
}
WINDOW_FULL_SCORE_MOVE_ATR = {
    "15m": 1.5,
    "30m": 2.0,
    "60m": 3.0,
}

ADX_BANDS = (
    # upper_exclusive, points, label
    (12.0, -4.0, "VERY_WEAK"),
    (18.0, 0.0, "WEAK"),
    (25.0, 4.0, "DEVELOPING"),
    (40.0, 7.0, "STRONG"),
    (math.inf, 5.0, "VERY_STRONG_POSSIBLY_MATURE"),
)

SLOPE_ALIGNED_POINTS = 3.0
SLOPE_OPPOSED_POINTS = -4.0

VWAP_ALIGNED_POINTS = 3.0
VWAP_OPPOSED_POINTS = -2.0
VWAP_EXTENDED_DISTANCE_ATR = 3.0
VWAP_EXTENDED_PENALTY = -3.0

HMA_ALIGNED_POINTS = 3.0
HMA_OPPOSED_POINTS = {
    "REVERSAL": -1.0,
    "ACCEPTED_BREAKOUT": -3.0,
    "FAILED_BREAKOUT": -2.0,
    "BREAKOUT_INITIATION": -3.0,
    "UNKNOWN": -2.0,
}

ACCEPTED_RANGE_QUALITY_MAX_POINTS = 5.0
ACCEPTED_RANGE_QUALITY_FULL_SCORE = 85.0

# Accepted Breakout current outside-range displacement.
AB_OUTSIDE_ATR_RULES = (
    # upper_exclusive, points, label
    (0.0, -12.0, "NOT_OUTSIDE"),
    (0.15, -6.0, "MARGINAL_ESCAPE"),
    (0.35, 3.0, "EARLY_ESCAPE"),
    (1.20, 8.0, "HEALTHY_ESCAPE"),
    (2.00, 3.0, "EXTENDED_ESCAPE"),
    (math.inf, -6.0, "VERY_EXTENDED_ESCAPE"),
)

AB_INSIDE_RANGE_PENALTY = -12.0
AB_TRADE_SIDE_RANGE_EDGE_POINTS = 3.0

REVERSAL_RANGE_EDGE_POINTS = 4.0
REVERSAL_RANGE_INTERIOR_PENALTY = -4.0
REVERSAL_SESSION_EDGE_POINTS = 3.0
REVERSAL_SESSION_INTERIOR_PENALTY = -2.0

# Remaining room to the current same-direction session extreme.
# BUY: distance to session high; SELL: distance to session low.
REVERSAL_ROOM_RULES = (
    (0.25, -3.0, "VERY_LOW_ROOM"),
    (0.50, 0.0, "LOW_ROOM"),
    (1.00, 2.0, "MODERATE_ROOM"),
    (math.inf, 4.0, "GOOD_ROOM"),
)

# Same-direction travel already completed from the opposite session extreme.
DIRECTIONAL_EXTENSION_RULES = (
    (0.50, -2.0, "INSUFFICIENT_DEVELOPMENT"),
    (3.00, 4.0, "HEALTHY_DEVELOPMENT"),
    (5.00, 2.0, "MATURE_DEVELOPMENT"),
    (7.00, -2.0, "EXTENDED"),
    (math.inf, -7.0, "VERY_EXTENDED"),
)

PATH_EFFICIENCY_RULES = (
    (0.15, -3.0, "VERY_LOW_EFFICIENCY"),
    (0.30, -1.0, "LOW_EFFICIENCY"),
    (0.60, 1.0, "MODERATE_EFFICIENCY"),
    (math.inf, 3.0, "HIGH_EFFICIENCY"),
)

DIRECTIONAL_RATIO_RULES = (
    (0.45, -2.0, "OPPOSING_BARS_DOMINANT"),
    (0.55, 0.0, "MIXED"),
    (0.70, 2.0, "DIRECTIONAL"),
    (math.inf, 3.0, "STRONGLY_DIRECTIONAL"),
)

# Rotational accepted-range diagnostic, currently applied to Reversal only.
ROTATION_MAX_DIRECTIONAL_EFFICIENCY = 0.15
ROTATION_MIN_CLOSE_OCCUPANCY = 0.75
ROTATION_PENALTY = -5.0

# Candidate-to-deployment movement. This uses only information already known at
# creation; it does not use future price movement.
CANDIDATE_STALENESS_RULES = (
    (0.25, 0.0, "FRESH"),
    (0.50, -1.0, "SLIGHTLY_MOVED"),
    (1.00, -4.0, "STALE"),
    (math.inf, -8.0, "VERY_STALE"),
)

PREVIOUS_OPPOSITE_SIGNAL_PENALTY = -7.0
PREVIOUS_SAME_SIDE_SIGNAL_PENALTY = -2.0
SAME_ACCEPTED_RANGE_ADDITIONAL_PENALTY = -3.0
SAME_OPPORTUNITY_ADDITIONAL_PENALTY = -2.0

AUCTION_FLIP_COUNT_RULES = (
    (4, 0.0, "LOW"),
    (8, -2.0, "MODERATE"),
    (math.inf, -5.0, "HIGH"),
)
HMA_FLIP_COUNT_RULES = (
    (3, 0.0, "LOW"),
    (6, -1.0, "MODERATE"),
    (math.inf, -3.0, "HIGH"),
)
VWAP_FLIP_COUNT_RULES = (
    (3, 0.0, "LOW"),
    (6, -1.0, "MODERATE"),
    (math.inf, -2.0, "HIGH"),
)

TIME_OF_DAY_RULES = (
    # start_time_inclusive, points, label
    ("15:15", -25.0, "AFTER_INTRADAY_CUTOFF_ZONE"),
    ("14:45", -7.0, "VERY_LATE"),
    ("14:00", -3.0, "LATE"),
    ("09:15", 0.0, "NORMAL"),
)

# Optional experimental EMA-corridor diagnostic. Kept OUT of the score by
# default until multi-day evidence establishes a discriminating rule.
ENABLE_EXPERIMENTAL_EMA_CONTAINMENT_SCORE = False
EMA_ENVELOPE_COMPRESSED_THRESHOLD = 0.35
EMA_COMPRESSION_PENALTY = -3.0

# Context-coverage fields used to show whether a score was based on sufficient
# creation-time evidence. Missing fields are not silently substituted.
CORE_COVERAGE_FIELDS = (
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


# =============================================================================
# INTERNAL TYPES
# =============================================================================

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class ScoreComponent:
    name: str
    points: float
    value: Any
    detail: str
    category: str


@dataclass
class PackageOutcome:
    signal_id: str
    leg_count: int = 0
    package_pnl: Optional[float] = None
    entry_exec_time: Optional[datetime] = None
    exit_exec_time: Optional[datetime] = None
    fully_entered: bool = False
    fully_exited: bool = False


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
    advisor_reason_codes: list[str]
    snapshot_found: bool
    features: dict[str, Any]
    components: list[ScoreComponent]
    raw_score: float
    context_coverage_pct: float
    missing_core_features: list[str]
    previous_signal_count: int
    previous_opposite_signal_count: int
    previous_same_side_signal_count: int
    previous_same_range_count: int
    previous_same_opportunity_count: int
    outcome: Optional[PackageOutcome] = None
    global_rank: int = 0
    global_percentile: float = 0.0
    setup_rank: int = 0
    setup_percentile: float = 0.0
    quintile: int = 0
    priority_band: str = ""
    top_percentile_flags: dict[int, bool] = field(default_factory=dict)


# =============================================================================
# GENERIC HELPERS
# =============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline AutoTrades signal ranking study")
    parser.add_argument("--date", dest="test_date", default=TEST_DATE, help="YYYY-MM-DD")
    parser.add_argument("--db-name", dest="db_name", default=None)
    parser.add_argument("--output-root", dest="output_root", default=None)
    parser.add_argument(
        "--no-outcomes",
        action="store_true",
        help="Rank signals without joining user_trades outcome data",
    )
    return parser.parse_args()


def strict_identifier(value: str) -> str:
    if not IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return value


def qid(value: str) -> str:
    return f"`{strict_identifier(value)}`"


def parse_json(value: Any, *, field_name: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        raise ValueError(f"{field_name} is NULL")
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be JSON string/dict, got {type(value).__name__}")
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise TypeError(f"{field_name} root must be an object")
    return parsed


def get_path(root: Mapping[str, Any], *path: str) -> Any:
    current: Any = root
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def finite_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, Decimal):
        value = float(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def normalize_enum(value: Any, default: str = "UNKNOWN") -> str:
    if value is None:
        return default
    text = str(value).strip().upper()
    return text if text else default


def parse_datetime(value: Any, tz: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise TypeError(f"Unsupported datetime value: {value!r}")

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def db_naive(value: datetime, tz: ZoneInfo) -> datetime:
    return value.astimezone(tz).replace(tzinfo=None)


def iso_local(value: Optional[datetime], tz: ZoneInfo) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=tz)
    return value.astimezone(tz).isoformat()


def side_sign(side: str) -> float:
    normalized = normalize_enum(side)
    if normalized == "BUY":
        return 1.0
    if normalized == "SELL":
        return -1.0
    raise ValueError(f"Unsupported side: {side!r}")


def clip(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def band_lookup(value: Optional[float], rules: Sequence[tuple[float, float, str]]) -> tuple[float, str]:
    if value is None:
        return 0.0, "MISSING"
    for upper, points, label in rules:
        if value < upper:
            return float(points), label
    raise AssertionError("band rules must end with infinity")


def time_rule_points(decision_time: datetime) -> tuple[float, str]:
    local_t = decision_time.timetz().replace(tzinfo=None)
    for start_text, points, label in TIME_OF_DAY_RULES:
        start = time.fromisoformat(start_text)
        if local_t >= start:
            return float(points), label
    return 0.0, "UNCLASSIFIED"


def safe_mean(values: Sequence[float]) -> Optional[float]:
    return statistics.fmean(values) if values else None


def safe_median(values: Sequence[float]) -> Optional[float]:
    return statistics.median(values) if values else None


def profit_factor(values: Sequence[float]) -> Optional[float]:
    gains = sum(value for value in values if value > 0)
    losses = abs(sum(value for value in values if value < 0))
    if losses == 0:
        return math.inf if gains > 0 else None
    return gains / losses


def json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def config_payload(test_date: str, db_name: str) -> dict[str, Any]:
    return {
        "ranking_version": RANKING_VERSION,
        "ranking_description": RANKING_DESCRIPTION,
        "test_date": test_date,
        "timezone": LOCAL_TIMEZONE,
        "symbols": SYMBOLS,
        "setup_families": SETUP_FAMILIES,
        "require_creation_snapshot": REQUIRE_CREATION_SNAPSHOT,
        "include_realized_outcomes": INCLUDE_REALIZED_OUTCOMES,
        "database": db_name,
        "tables": {
            "signals": SIGNALS_TABLE,
            "snapshots": SNAPSHOTS_TABLE,
            "trades": TRADES_TABLE,
        },
        "top_percentiles": TOP_PERCENTILES,
        "quintile_count": QUINTILE_COUNT,
        "priority_band_percentiles": PRIORITY_BAND_PERCENTILES,
        "churn_lookback_minutes": CHURN_LOOKBACK_MINUTES,
        "weights": {
            "advisor_action_points": ADVISOR_ACTION_POINTS,
            "setup_base_points": SETUP_BASE_POINTS,
            "window_max_points": WINDOW_MAX_POINTS,
            "window_full_score_move_atr": WINDOW_FULL_SCORE_MOVE_ATR,
            "adx_bands": ADX_BANDS,
            "ab_outside_atr_rules": AB_OUTSIDE_ATR_RULES,
            "directional_extension_rules": DIRECTIONAL_EXTENSION_RULES,
            "path_efficiency_rules": PATH_EFFICIENCY_RULES,
            "directional_ratio_rules": DIRECTIONAL_RATIO_RULES,
            "candidate_staleness_rules": CANDIDATE_STALENESS_RULES,
            "time_of_day_rules": TIME_OF_DAY_RULES,
            "experimental_ema_containment_enabled": ENABLE_EXPERIMENTAL_EMA_CONTAINMENT_SCORE,
        },
    }


def config_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=json_default).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


# =============================================================================
# DATABASE
# =============================================================================

def connection_kwargs(db_name_override: Optional[str]) -> dict[str, Any]:
    db_name = db_name_override or DB_NAME

    if DATABASE_URL:
        parsed = urlparse(DATABASE_URL)
        if parsed.scheme not in {"mysql", "mysql+mysqlconnector"}:
            raise ValueError(
                "DATABASE_URL must use mysql:// or mysql+mysqlconnector://"
            )
        if not parsed.hostname or not parsed.username:
            raise ValueError("DATABASE_URL must include host and username")
        if parsed.path and parsed.path != "/":
            db_name = unquote(parsed.path.lstrip("/"))
        return {
            "host": parsed.hostname,
            "port": parsed.port or 3306,
            "user": unquote(parsed.username),
            "password": unquote(parsed.password or ""),
            "database": db_name,
            "charset": DB_CHARSET,
            "connection_timeout": DB_CONNECTION_TIMEOUT_SECONDS,
            "autocommit": False,
        }

    return {
        "host": DB_HOST,
        "port": DB_PORT,
        "user": DB_USER,
        "password": DB_PASSWORD,
        "database": db_name,
        "charset": DB_CHARSET,
        "connection_timeout": DB_CONNECTION_TIMEOUT_SECONDS,
        "autocommit": False,
    }


def open_read_only_connection(db_name_override: Optional[str]) -> mysql.connector.MySQLConnection:
    kwargs = connection_kwargs(db_name_override)
    connection = mysql.connector.connect(**kwargs)
    try:
        connection.start_transaction(readonly=True)
    except MySQLError:
        # Some server/connector combinations do not support readonly=True.
        # The program still issues SELECT/SHOW statements only.
        logging.warning(
            "Server did not accept explicit READ ONLY transaction; continuing with SELECT-only code",
            exc_info=True,
        )
        try:
            connection.rollback()
        except MySQLError:
            pass
        connection.start_transaction()
    return connection


def table_columns(connection: mysql.connector.MySQLConnection, table: str) -> set[str]:
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(f"SHOW COLUMNS FROM {qid(table)}")
        return {str(row["Field"]) for row in cursor.fetchall()}
    finally:
        cursor.close()


def require_columns(
    connection: mysql.connector.MySQLConnection,
    table: str,
    required: Iterable[str],
) -> None:
    existing = table_columns(connection, table)
    missing = sorted(set(required) - existing)
    if missing:
        raise RuntimeError(
            f"Table {table!r} is missing required columns: {', '.join(missing)}"
        )


def load_signals(
    connection: mysql.connector.MySQLConnection,
    start_naive: datetime,
    end_naive: datetime,
) -> list[dict[str, Any]]:
    require_columns(
        connection,
        SIGNALS_TABLE,
        (
            SIGNAL_ID_COLUMN,
            SIGNAL_SYMBOL_COLUMN,
            SIGNAL_SETUP_COLUMN,
            SIGNAL_SIDE_COLUMN,
            SIGNAL_FIRST_SEEN_COLUMN,
            SIGNAL_ACTIONABLE_COLUMN,
            SIGNAL_CREATED_PRICE_COLUMN,
            SIGNAL_META_JSON_COLUMN,
        ),
    )

    where = [
        f"{qid(SIGNAL_FIRST_SEEN_COLUMN)} >= %s",
        f"{qid(SIGNAL_FIRST_SEEN_COLUMN)} < %s",
    ]
    params: list[Any] = [start_naive, end_naive]

    if SYMBOLS:
        placeholders = ", ".join(["%s"] * len(SYMBOLS))
        where.append(f"{qid(SIGNAL_SYMBOL_COLUMN)} IN ({placeholders})")
        params.extend(SYMBOLS)

    if SETUP_FAMILIES:
        placeholders = ", ".join(["%s"] * len(SETUP_FAMILIES))
        where.append(f"{qid(SIGNAL_SETUP_COLUMN)} IN ({placeholders})")
        params.extend(SETUP_FAMILIES)

    sql = f"""
        SELECT
            {qid(SIGNAL_ID_COLUMN)} AS signal_id,
            {qid(SIGNAL_SYMBOL_COLUMN)} AS symbol,
            {qid(SIGNAL_SETUP_COLUMN)} AS setup_family,
            {qid(SIGNAL_SIDE_COLUMN)} AS side,
            {qid(SIGNAL_FIRST_SEEN_COLUMN)} AS first_seen_time,
            {qid(SIGNAL_ACTIONABLE_COLUMN)} AS actionable_time,
            {qid(SIGNAL_CREATED_PRICE_COLUMN)} AS created_price,
            {qid(SIGNAL_META_JSON_COLUMN)} AS meta_json
        FROM {qid(SIGNALS_TABLE)}
        WHERE {' AND '.join(where)}
        ORDER BY
            {qid(SIGNAL_FIRST_SEEN_COLUMN)},
            {qid(SIGNAL_SYMBOL_COLUMN)},
            {qid(SIGNAL_ID_COLUMN)}
    """

    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(sql, tuple(params))
        return list(cursor.fetchall())
    finally:
        cursor.close()


def load_creation_snapshots(
    connection: mysql.connector.MySQLConnection,
    start_naive: datetime,
    end_naive: datetime,
    symbols: Sequence[str],
) -> dict[tuple[str, datetime], dict[str, Any]]:
    require_columns(
        connection,
        SNAPSHOTS_TABLE,
        (SNAPSHOT_SYMBOL_COLUMN, SNAPSHOT_TIME_COLUMN, SNAPSHOT_JSON_COLUMN),
    )

    if not symbols:
        return {}

    result: dict[tuple[str, datetime], dict[str, Any]] = {}
    chunk_size = 200

    for offset in range(0, len(symbols), chunk_size):
        chunk = list(symbols[offset : offset + chunk_size])
        placeholders = ", ".join(["%s"] * len(chunk))
        sql = f"""
            SELECT
                {qid(SNAPSHOT_SYMBOL_COLUMN)} AS symbol,
                {qid(SNAPSHOT_TIME_COLUMN)} AS snapshot_time,
                {qid(SNAPSHOT_JSON_COLUMN)} AS snapshot_json
            FROM {qid(SNAPSHOTS_TABLE)}
            WHERE {qid(SNAPSHOT_TIME_COLUMN)} >= %s
              AND {qid(SNAPSHOT_TIME_COLUMN)} < %s
              AND {qid(SNAPSHOT_SYMBOL_COLUMN)} IN ({placeholders})
            ORDER BY {qid(SNAPSHOT_TIME_COLUMN)}, {qid(SNAPSHOT_SYMBOL_COLUMN)}
        """
        params: list[Any] = [start_naive, end_naive, *chunk]

        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(sql, tuple(params))
            for row in cursor.fetchall():
                symbol = str(row["symbol"])
                snapshot_time = row["snapshot_time"]
                if not isinstance(snapshot_time, datetime):
                    raise TypeError(
                        f"Unexpected snapshot_time type for {symbol}: "
                        f"{type(snapshot_time).__name__}"
                    )
                snapshot = parse_json(row["snapshot_json"], field_name="snapshot_json")
                key = (symbol, snapshot_time.replace(tzinfo=None))
                if key in result:
                    raise RuntimeError(
                        f"Duplicate snapshot row for {symbol} @ {snapshot_time}"
                    )
                result[key] = snapshot
        finally:
            cursor.close()

    return result


def load_package_outcomes(
    connection: mysql.connector.MySQLConnection,
    signal_ids: Sequence[str],
) -> dict[str, PackageOutcome]:
    if not signal_ids:
        return {}

    require_columns(
        connection,
        TRADES_TABLE,
        (
            TRADE_SIGNAL_ID_COLUMN,
            TRADE_EXIT_PNL_COLUMN,
            TRADE_ENTRY_EXEC_TIME_COLUMN,
            TRADE_EXIT_EXEC_TIME_COLUMN,
            TRADE_ENTRY_STATUS_COLUMN,
            TRADE_EXIT_STATUS_COLUMN,
        ),
    )

    rows_by_signal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    chunk_size = 500

    for offset in range(0, len(signal_ids), chunk_size):
        chunk = list(signal_ids[offset : offset + chunk_size])
        placeholders = ", ".join(["%s"] * len(chunk))
        sql = f"""
            SELECT
                {qid(TRADE_SIGNAL_ID_COLUMN)} AS signal_id,
                {qid(TRADE_EXIT_PNL_COLUMN)} AS exit_pnl,
                {qid(TRADE_ENTRY_EXEC_TIME_COLUMN)} AS entry_exec_time,
                {qid(TRADE_EXIT_EXEC_TIME_COLUMN)} AS exit_exec_time,
                {qid(TRADE_ENTRY_STATUS_COLUMN)} AS entry_status,
                {qid(TRADE_EXIT_STATUS_COLUMN)} AS exit_status
            FROM {qid(TRADES_TABLE)}
            WHERE {qid(TRADE_SIGNAL_ID_COLUMN)} IN ({placeholders})
            ORDER BY {qid(TRADE_SIGNAL_ID_COLUMN)}, {qid(TRADE_ENTRY_EXEC_TIME_COLUMN)}
        """
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(sql, tuple(chunk))
            for row in cursor.fetchall():
                rows_by_signal[str(row["signal_id"])].append(row)
        finally:
            cursor.close()

    outcomes: dict[str, PackageOutcome] = {}
    for signal_id, rows in rows_by_signal.items():
        pnl_values = [
            finite_float(row["exit_pnl"])
            for row in rows
            if finite_float(row["exit_pnl"]) is not None
        ]
        entry_times = [
            row["entry_exec_time"]
            for row in rows
            if isinstance(row["entry_exec_time"], datetime)
        ]
        exit_times = [
            row["exit_exec_time"]
            for row in rows
            if isinstance(row["exit_exec_time"], datetime)
        ]
        entry_statuses = {normalize_enum(row["entry_status"]) for row in rows}
        exit_statuses = {normalize_enum(row["exit_status"]) for row in rows}

        outcomes[signal_id] = PackageOutcome(
            signal_id=signal_id,
            leg_count=len(rows),
            package_pnl=sum(pnl_values) if pnl_values else None,
            entry_exec_time=min(entry_times) if entry_times else None,
            exit_exec_time=max(exit_times) if exit_times else None,
            fully_entered=bool(rows) and entry_statuses <= {"FILLED"},
            fully_exited=bool(rows) and exit_statuses <= {"FILLED"},
        )

    return outcomes


# =============================================================================
# ENTRY CONTEXT AND FEATURE EXTRACTION
# =============================================================================

def immutable_entry_criteria(signal_row: Mapping[str, Any]) -> dict[str, Any]:
    meta = parse_json(signal_row["meta_json"], field_name="signals.meta_json")
    entry = meta.get("entry_criteria_json")
    if not isinstance(entry, dict):
        raise ValueError(
            "meta_json.entry_criteria_json is required; current criteria are not "
            "accepted because they may contain later lifecycle information"
        )
    if normalize_enum(entry.get("signal_action")) != "CREATE":
        raise ValueError(
            "meta_json.entry_criteria_json.signal_action must be CREATE"
        )
    return entry


def extract_features(
    entry: Mapping[str, Any],
    snapshot: Optional[Mapping[str, Any]],
    side: str,
) -> dict[str, Any]:
    advisor = entry.get("advisor")
    advisor = advisor if isinstance(advisor, Mapping) else {}
    diagnostics = advisor.get("diagnostics")
    diagnostics = diagnostics if isinstance(diagnostics, Mapping) else {}

    stock = entry.get("stock_context")
    stock = stock if isinstance(stock, Mapping) else {}

    snapshot = snapshot or {}
    indicators = get_path(snapshot, "indicators") or {}
    market_windows = get_path(snapshot, "market_windows") or {}
    structure = get_path(snapshot, "structure") or {}
    accepted = structure.get("accepted") if isinstance(structure, Mapping) else {}
    accepted = accepted if isinstance(accepted, Mapping) else {}
    accepted_metrics = (
        accepted.get("metrics") if isinstance(accepted.get("metrics"), Mapping) else {}
    )
    accepted_range = (
        accepted.get("range") if isinstance(accepted.get("range"), Mapping) else {}
    )

    atr = finite_float(get_path(indicators, "atr", "value"))
    adx = finite_float(get_path(indicators, "adx", "value"))
    rsi = finite_float(get_path(indicators, "rsi", "value"))

    move_15m = finite_float(get_path(market_windows, "15m", "move_atr"))
    move_30m = finite_float(get_path(market_windows, "30m", "move_atr"))
    move_60m = finite_float(get_path(market_windows, "60m", "move_atr"))

    side_is_buy = normalize_enum(side) == "BUY"

    extension = finite_float(
        stock.get("rise_from_session_low_atr")
        if side_is_buy
        else stock.get("decline_from_session_high_atr")
    )
    path_efficiency = finite_float(
        stock.get("path_from_session_low_efficiency")
        if side_is_buy
        else stock.get("path_from_session_high_efficiency")
    )
    path_ratio = finite_float(
        stock.get("path_from_session_low_directional_ratio")
        if side_is_buy
        else stock.get("path_from_session_high_directional_ratio")
    )
    room_to_session_extreme = finite_float(
        stock.get("distance_to_session_high_atr")
        if side_is_buy
        else stock.get("distance_to_session_low_atr")
    )

    entry_price = finite_float(entry.get("entry_price"))
    candidate_price = finite_float(entry.get("candidate_entry_price"))
    candidate_staleness_atr: Optional[float] = None
    if (
        entry_price is not None
        and candidate_price is not None
        and atr is not None
        and atr > 0
    ):
        candidate_staleness_atr = abs(entry_price - candidate_price) / atr

    advisor_reason_codes = advisor.get("reason_codes")
    if not isinstance(advisor_reason_codes, list):
        advisor_reason_codes = []

    features: dict[str, Any] = {
        "advisor_action": normalize_enum(advisor.get("action")),
        "advisor_reason_codes": [str(code) for code in advisor_reason_codes],
        "advisor_triggered_rules": diagnostics.get("triggered_rules")
        if isinstance(diagnostics.get("triggered_rules"), list)
        else [],
        "session_position": finite_float(stock.get("session_position")),
        "accepted_range_inside": stock.get("accepted_range_inside"),
        "accepted_range_position": finite_float(stock.get("accepted_range_position")),
        "accepted_range_outside_atr": finite_float(
            stock.get("accepted_range_outside_atr")
        ),
        "accepted_range_id": str(stock.get("accepted_range_id") or ""),
        "accepted_range_source": str(stock.get("accepted_range_source") or ""),
        "accepted_range_quality": finite_float(accepted.get("quality")),
        "accepted_range_age_bars": finite_float(accepted.get("age_bars")),
        "accepted_range_width_atr": finite_float(accepted_range.get("width_atr")),
        "accepted_range_directional_efficiency": finite_float(
            accepted_metrics.get("directional_efficiency")
        ),
        "accepted_range_close_occupancy": finite_float(
            accepted_metrics.get("close_occupancy_ratio")
        ),
        "directional_extension_atr": extension,
        "room_to_session_extreme_atr": room_to_session_extreme,
        "path_efficiency": path_efficiency,
        "path_directional_ratio": path_ratio,
        "atr": atr,
        "adx": adx,
        "rsi": rsi,
        "move_15m_atr": move_15m,
        "move_30m_atr": move_30m,
        "move_60m_atr": move_60m,
        "vwap_side": normalize_enum(get_path(indicators, "vwap", "side")),
        "vwap_distance_atr": finite_float(
            get_path(indicators, "vwap", "distance_atr")
        ),
        "vwap_flip_count_today": finite_float(
            get_path(indicators, "vwap", "flip_count_today")
        ),
        "hma_state": normalize_enum(get_path(indicators, "hma", "state")),
        "hma_strength": normalize_enum(get_path(indicators, "hma", "strength")),
        "hma_flip_count_today": finite_float(
            get_path(indicators, "hma", "flip_count_today")
        ),
        "ema_ref": finite_float(get_path(indicators, "ema", "ref")),
        "ema_slow": finite_float(get_path(indicators, "ema", "slow")),
        "ema_envelope": finite_float(
            get_path(indicators, "envelopes", "ema_envelope")
        ),
        "price_slope_state": normalize_enum(
            get_path(snapshot, "price_action", "slope", "state")
        ),
        "price_slope_3_atr_per_bar": finite_float(
            get_path(snapshot, "price_action", "slope", "bars_3_atr_per_bar")
        ),
        "price_slope_5_atr_per_bar": finite_float(
            get_path(snapshot, "price_action", "slope", "bars_5_atr_per_bar")
        ),
        "auction_state": normalize_enum(entry.get("auction_state")),
        "auction_flip_count_today": finite_float(
            get_path(snapshot, "structure", "flip_count_today")
        ),
        "candidate_staleness_atr": candidate_staleness_atr,
        "snapshot_close": finite_float(
            get_path(snapshot, "bar", "close")
            if get_path(snapshot, "bar", "close") is not None
            else snapshot.get("close")
        ),
    }
    return features


# =============================================================================
# SCORING
# =============================================================================

def add_component(
    components: list[ScoreComponent],
    *,
    name: str,
    points: float,
    value: Any,
    detail: str,
    category: str,
) -> None:
    components.append(
        ScoreComponent(
            name=name,
            points=round(float(points), 6),
            value=value,
            detail=detail,
            category=category,
        )
    )


def directional_window_points(
    move_atr: Optional[float],
    side: str,
    max_points: float,
    full_score_move_atr: float,
) -> float:
    if move_atr is None:
        return 0.0
    aligned = side_sign(side) * move_atr
    return clip(aligned / full_score_move_atr, -1.0, 1.0) * max_points


def score_signal(
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
    advisor_reason_codes: list[str],
    snapshot_found: bool,
    features: dict[str, Any],
    history: Sequence[RankedSignal],
) -> RankedSignal:
    components: list[ScoreComponent] = []
    setup_family = normalize_enum(setup_family)
    side = normalize_enum(side)

    # Base/setup
    base = SETUP_BASE_POINTS.get(
        setup_family, SETUP_BASE_POINTS["UNKNOWN"]
    )
    add_component(
        components,
        name="setup_base",
        points=base,
        value=setup_family,
        detail=f"Base points for {setup_family}",
        category="SETUP",
    )

    # Advisor
    advisor_points = ADVISOR_ACTION_POINTS.get(
        advisor_action, ADVISOR_ACTION_POINTS["UNKNOWN"]
    )
    add_component(
        components,
        name="advisor_action",
        points=advisor_points,
        value=advisor_action,
        detail="Creation-time StockAdvisor action",
        category="ADVISOR",
    )

    # ADX
    adx = features.get("adx")
    adx_points, adx_label = band_lookup(adx, ADX_BANDS)
    add_component(
        components,
        name="adx_context",
        points=adx_points,
        value=adx,
        detail=adx_label,
        category="STRUCTURE",
    )

    # Directional windows
    setup_window_weights = WINDOW_MAX_POINTS.get(
        setup_family, WINDOW_MAX_POINTS["UNKNOWN"]
    )
    for window in ("15m", "30m", "60m"):
        move = features.get(f"move_{window}_atr")
        max_points = setup_window_weights[window]
        points = directional_window_points(
            move,
            side,
            max_points,
            WINDOW_FULL_SCORE_MOVE_ATR[window],
        )
        aligned_value = None if move is None else side_sign(side) * move
        add_component(
            components,
            name=f"{window}_directional_alignment",
            points=points,
            value=move,
            detail=f"side_aligned_move_atr={aligned_value}",
            category="IMPULSE",
        )

    # Price slope
    slope_state = normalize_enum(features.get("price_slope_state"))
    aligned_token = "UP" if side == "BUY" else "DOWN"
    opposite_token = "DOWN" if side == "BUY" else "UP"
    if aligned_token in slope_state:
        slope_points = SLOPE_ALIGNED_POINTS
        slope_detail = "ALIGNED"
    elif opposite_token in slope_state:
        slope_points = SLOPE_OPPOSED_POINTS
        slope_detail = "OPPOSED"
    else:
        slope_points = 0.0
        slope_detail = "NEUTRAL_OR_MISSING"
    add_component(
        components,
        name="price_slope_alignment",
        points=slope_points,
        value=slope_state,
        detail=slope_detail,
        category="IMPULSE",
    )

    # VWAP
    vwap_side = normalize_enum(features.get("vwap_side"))
    expected_vwap_side = "ABOVE" if side == "BUY" else "BELOW"
    if vwap_side == expected_vwap_side:
        vwap_points = VWAP_ALIGNED_POINTS
        vwap_detail = "ALIGNED"
    elif vwap_side in {"ABOVE", "BELOW"}:
        vwap_points = VWAP_OPPOSED_POINTS
        vwap_detail = "OPPOSED"
    else:
        vwap_points = 0.0
        vwap_detail = "MISSING_OR_NEUTRAL"
    add_component(
        components,
        name="vwap_alignment",
        points=vwap_points,
        value=vwap_side,
        detail=vwap_detail,
        category="STRUCTURE",
    )
    vwap_distance = features.get("vwap_distance_atr")
    vwap_extension_points = (
        VWAP_EXTENDED_PENALTY
        if vwap_distance is not None
        and abs(vwap_distance) > VWAP_EXTENDED_DISTANCE_ATR
        else 0.0
    )
    add_component(
        components,
        name="vwap_extension",
        points=vwap_extension_points,
        value=vwap_distance,
        detail=(
            "EXTENDED_FROM_VWAP"
            if vwap_extension_points < 0
            else "NOT_EXTENDED_OR_MISSING"
        ),
        category="EXTENSION",
    )

    # HMA
    hma_state = normalize_enum(features.get("hma_state"))
    if hma_state == side:
        hma_points = HMA_ALIGNED_POINTS
        hma_detail = "ALIGNED"
    elif hma_state in {"BUY", "SELL"}:
        hma_points = HMA_OPPOSED_POINTS.get(
            setup_family, HMA_OPPOSED_POINTS["UNKNOWN"]
        )
        hma_detail = "OPPOSED"
    else:
        hma_points = 0.0
        hma_detail = "NEUTRAL_OR_MISSING"
    add_component(
        components,
        name="hma_alignment",
        points=hma_points,
        value=hma_state,
        detail=hma_detail,
        category="STRUCTURE",
    )

    # Accepted range quality
    range_quality = features.get("accepted_range_quality")
    range_quality_points = 0.0
    if range_quality is not None:
        range_quality_points = (
            clip(range_quality / ACCEPTED_RANGE_QUALITY_FULL_SCORE, 0.0, 1.0)
            * ACCEPTED_RANGE_QUALITY_MAX_POINTS
        )
    add_component(
        components,
        name="accepted_range_quality",
        points=range_quality_points,
        value=range_quality,
        detail="Scaled creation-time accepted range quality",
        category="RANGE",
    )

    range_inside = features.get("accepted_range_inside")
    range_position = features.get("accepted_range_position")
    session_position = features.get("session_position")
    outside_atr = features.get("accepted_range_outside_atr")

    # Setup-specific range/location logic
    if setup_family == "ACCEPTED_BREAKOUT":
        if range_inside is True:
            add_component(
                components,
                name="accepted_breakout_inside_range",
                points=AB_INSIDE_RANGE_PENALTY,
                value=range_inside,
                detail="Accepted Breakout creation price remained inside accepted range",
                category="RANGE",
            )
        else:
            ab_points, ab_label = band_lookup(outside_atr, AB_OUTSIDE_ATR_RULES)
            add_component(
                components,
                name="accepted_breakout_outside_distance",
                points=ab_points,
                value=outside_atr,
                detail=ab_label,
                category="RANGE",
            )

        if range_position is not None:
            trade_side_position = (
                range_position if side == "BUY" else 1.0 - range_position
            )
            edge_points = (
                AB_TRADE_SIDE_RANGE_EDGE_POINTS
                if trade_side_position >= 0.80
                else 0.0
            )
            add_component(
                components,
                name="accepted_breakout_trade_side_edge",
                points=edge_points,
                value=trade_side_position,
                detail="Trade-side position within/relative to accepted range",
                category="RANGE",
            )

    elif setup_family == "REVERSAL":
        if range_position is not None:
            edge_distance = min(range_position, 1.0 - range_position)
            if edge_distance <= 0.20:
                points = REVERSAL_RANGE_EDGE_POINTS
                detail = "NEAR_ACCEPTED_RANGE_EDGE"
            elif 0.35 <= range_position <= 0.65:
                points = REVERSAL_RANGE_INTERIOR_PENALTY
                detail = "ACCEPTED_RANGE_INTERIOR"
            else:
                points = 0.0
                detail = "MID_EDGE_TRANSITION"
            add_component(
                components,
                name="reversal_accepted_range_location",
                points=points,
                value=range_position,
                detail=detail,
                category="RANGE",
            )

        if session_position is not None:
            session_edge_distance = min(session_position, 1.0 - session_position)
            if session_edge_distance <= 0.15:
                points = REVERSAL_SESSION_EDGE_POINTS
                detail = "NEAR_SESSION_EDGE"
            elif 0.35 <= session_position <= 0.65:
                points = REVERSAL_SESSION_INTERIOR_PENALTY
                detail = "SESSION_INTERIOR"
            else:
                points = 0.0
                detail = "MID_EDGE_TRANSITION"
            add_component(
                components,
                name="reversal_session_location",
                points=points,
                value=session_position,
                detail=detail,
                category="RANGE",
            )

        room = features.get("room_to_session_extreme_atr")
        room_points, room_label = band_lookup(room, REVERSAL_ROOM_RULES)
        add_component(
            components,
            name="reversal_remaining_session_room",
            points=room_points,
            value=room,
            detail=room_label,
            category="OPPORTUNITY",
        )

        directional_efficiency = features.get(
            "accepted_range_directional_efficiency"
        )
        close_occupancy = features.get("accepted_range_close_occupancy")
        rotational = (
            directional_efficiency is not None
            and close_occupancy is not None
            and directional_efficiency <= ROTATION_MAX_DIRECTIONAL_EFFICIENCY
            and close_occupancy >= ROTATION_MIN_CLOSE_OCCUPANCY
        )
        add_component(
            components,
            name="reversal_rotational_range",
            points=ROTATION_PENALTY if rotational else 0.0,
            value={
                "directional_efficiency": directional_efficiency,
                "close_occupancy": close_occupancy,
            },
            detail="ROTATIONAL" if rotational else "NOT_CONFIRMED_OR_MISSING",
            category="CHURN",
        )

    # Directional development / extension
    extension = features.get("directional_extension_atr")
    extension_points, extension_label = band_lookup(
        extension, DIRECTIONAL_EXTENSION_RULES
    )
    add_component(
        components,
        name="directional_extension",
        points=extension_points,
        value=extension,
        detail=extension_label,
        category="EXTENSION",
    )

    # Path quality
    efficiency = features.get("path_efficiency")
    efficiency_points, efficiency_label = band_lookup(
        efficiency, PATH_EFFICIENCY_RULES
    )
    add_component(
        components,
        name="path_efficiency",
        points=efficiency_points,
        value=efficiency,
        detail=efficiency_label,
        category="PATH",
    )

    directional_ratio = features.get("path_directional_ratio")
    ratio_points, ratio_label = band_lookup(
        directional_ratio, DIRECTIONAL_RATIO_RULES
    )
    add_component(
        components,
        name="path_directional_ratio",
        points=ratio_points,
        value=directional_ratio,
        detail=ratio_label,
        category="PATH",
    )

    # Candidate staleness
    staleness = features.get("candidate_staleness_atr")
    staleness_points, staleness_label = band_lookup(
        staleness, CANDIDATE_STALENESS_RULES
    )
    add_component(
        components,
        name="candidate_deployment_staleness",
        points=staleness_points,
        value=staleness,
        detail=staleness_label,
        category="FRESHNESS",
    )

    # Prior signals known at decision time
    cutoff = decision_time - timedelta(minutes=CHURN_LOOKBACK_MINUTES)
    prior = [
        item
        for item in history
        if item.symbol == symbol and cutoff <= item.decision_time < decision_time
    ]
    prior_opposite = [item for item in prior if item.side != side]
    prior_same = [item for item in prior if item.side == side]
    prior_same_range = [
        item
        for item in prior
        if accepted_range_id
        and item.accepted_range_id
        and item.accepted_range_id == accepted_range_id
    ]
    prior_same_opportunity = [
        item
        for item in prior
        if opportunity_key
        and item.opportunity_key
        and item.opportunity_key == opportunity_key
    ]

    opposite_count = min(
        len(prior_opposite), MAX_PREVIOUS_OPPOSITE_SIGNALS_COUNTED
    )
    same_count = min(len(prior_same), MAX_PREVIOUS_SAME_SIDE_SIGNALS_COUNTED)

    add_component(
        components,
        name="recent_opposite_signals",
        points=opposite_count * PREVIOUS_OPPOSITE_SIGNAL_PENALTY,
        value=len(prior_opposite),
        detail=f"{CHURN_LOOKBACK_MINUTES} minute lookback",
        category="CHURN",
    )
    add_component(
        components,
        name="recent_same_side_signals",
        points=same_count * PREVIOUS_SAME_SIDE_SIGNAL_PENALTY,
        value=len(prior_same),
        detail=f"{CHURN_LOOKBACK_MINUTES} minute lookback",
        category="CHURN",
    )
    add_component(
        components,
        name="same_accepted_range_history",
        points=(
            SAME_ACCEPTED_RANGE_ADDITIONAL_PENALTY
            if prior_same_range
            else 0.0
        ),
        value=len(prior_same_range),
        detail="Previous signal in same accepted range identity",
        category="CHURN",
    )
    add_component(
        components,
        name="same_opportunity_history",
        points=(
            SAME_OPPORTUNITY_ADDITIONAL_PENALTY
            if prior_same_opportunity
            else 0.0
        ),
        value=len(prior_same_opportunity),
        detail="Previous signal with same opportunity identity",
        category="CHURN",
    )

    # Flip counts
    for feature_name, component_name, rules in (
        (
            "auction_flip_count_today",
            "auction_flip_count",
            AUCTION_FLIP_COUNT_RULES,
        ),
        ("hma_flip_count_today", "hma_flip_count", HMA_FLIP_COUNT_RULES),
        ("vwap_flip_count_today", "vwap_flip_count", VWAP_FLIP_COUNT_RULES),
    ):
        value = features.get(feature_name)
        points, label = band_lookup(value, rules)
        add_component(
            components,
            name=component_name,
            points=points,
            value=value,
            detail=label,
            category="CHURN",
        )

    # Time of day
    tod_points, tod_label = time_rule_points(decision_time)
    add_component(
        components,
        name="time_of_day",
        points=tod_points,
        value=decision_time.timetz().isoformat(),
        detail=tod_label,
        category="TIME",
    )

    # Experimental EMA compression — output field always exists, score only when enabled.
    ema_envelope = features.get("ema_envelope")
    ema_compressed = (
        ema_envelope is not None
        and ema_envelope <= EMA_ENVELOPE_COMPRESSED_THRESHOLD
    )
    ema_points = (
        EMA_COMPRESSION_PENALTY
        if ENABLE_EXPERIMENTAL_EMA_CONTAINMENT_SCORE and ema_compressed
        else 0.0
    )
    add_component(
        components,
        name="experimental_ema_compression",
        points=ema_points,
        value=ema_envelope,
        detail=(
            "COMPRESSED"
            if ema_compressed
            else "NOT_COMPRESSED_OR_MISSING"
        )
        + (
            "_SCORED"
            if ENABLE_EXPERIMENTAL_EMA_CONTAINMENT_SCORE
            else "_DIAGNOSTIC_ONLY"
        ),
        category="EXPERIMENTAL",
    )

    raw_score = round(sum(component.points for component in components), 6)

    missing = [
        name
        for name in CORE_COVERAGE_FIELDS
        if features.get(name) is None
        or features.get(name) == "UNKNOWN"
    ]
    coverage = round(
        100.0 * (len(CORE_COVERAGE_FIELDS) - len(missing))
        / len(CORE_COVERAGE_FIELDS),
        2,
    )

    return RankedSignal(
        signal_id=signal_id,
        symbol=symbol,
        setup_family=setup_family,
        setup_subtype=setup_subtype,
        side=side,
        decision_time=decision_time,
        entry_price=entry_price,
        candidate_entry_price=candidate_entry_price,
        opportunity_key=opportunity_key,
        accepted_range_id=accepted_range_id,
        advisor_action=advisor_action,
        advisor_reason_codes=advisor_reason_codes,
        snapshot_found=snapshot_found,
        features=features,
        components=components,
        raw_score=raw_score,
        context_coverage_pct=coverage,
        missing_core_features=missing,
        previous_signal_count=len(prior),
        previous_opposite_signal_count=len(prior_opposite),
        previous_same_side_signal_count=len(prior_same),
        previous_same_range_count=len(prior_same_range),
        previous_same_opportunity_count=len(prior_same_opportunity),
    )


def assign_ranks(items: list[RankedSignal]) -> None:
    items.sort(
        key=lambda item: (
            -item.raw_score,
            item.decision_time,
            item.symbol,
            item.signal_id,
        )
    )
    total = len(items)

    for index, item in enumerate(items, start=1):
        item.global_rank = index
        item.global_percentile = round(
            100.0 * (total - index + 1) / total,
            4,
        )
        item.quintile = min(
            QUINTILE_COUNT,
            math.ceil(index * QUINTILE_COUNT / total),
        )
        item.top_percentile_flags = {
            percentile: index <= math.ceil(total * percentile / 100.0)
            for percentile in TOP_PERCENTILES
        }

        for band, minimum_percentile in PRIORITY_BAND_PERCENTILES.items():
            if item.global_percentile >= minimum_percentile:
                item.priority_band = band
                break

    grouped: dict[str, list[RankedSignal]] = defaultdict(list)
    for item in items:
        grouped[item.setup_family].append(item)

    for group in grouped.values():
        group.sort(
            key=lambda item: (
                -item.raw_score,
                item.decision_time,
                item.symbol,
                item.signal_id,
            )
        )
        count = len(group)
        for index, item in enumerate(group, start=1):
            item.setup_rank = index
            item.setup_percentile = round(
                100.0 * (count - index + 1) / count,
                4,
            )


# =============================================================================
# REPORTING
# =============================================================================

def outcome_values(items: Sequence[RankedSignal]) -> list[float]:
    return [
        item.outcome.package_pnl
        for item in items
        if item.outcome is not None and item.outcome.package_pnl is not None
    ]


def evaluation_metrics(items: Sequence[RankedSignal]) -> dict[str, Any]:
    values = outcome_values(items)
    wins = sum(1 for value in values if value > 0)
    losses = sum(1 for value in values if value < 0)
    flats = sum(1 for value in values if value == 0)
    nonflat = wins + losses

    return {
        "signals": len(items),
        "signals_with_package_outcome": len(values),
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "win_rate_pct": round(100.0 * wins / nonflat, 4) if nonflat else None,
        "total_pnl": round(sum(values), 4) if values else None,
        "average_pnl": round(safe_mean(values), 4) if values else None,
        "median_pnl": round(safe_median(values), 4) if values else None,
        "profit_factor": (
            round(profit_factor(values), 6)
            if values and profit_factor(values) not in (None, math.inf)
            else ("INF" if values and profit_factor(values) == math.inf else None)
        ),
        "best_package_pnl": round(max(values), 4) if values else None,
        "worst_package_pnl": round(min(values), 4) if values else None,
    }


def component_json(item: RankedSignal) -> str:
    return json.dumps(
        [
            {
                "name": component.name,
                "category": component.category,
                "points": component.points,
                "value": component.value,
                "detail": component.detail,
            }
            for component in item.components
        ],
        separators=(",", ":"),
        default=json_default,
    )


def positive_reasons(item: RankedSignal) -> str:
    positive = sorted(
        (component for component in item.components if component.points > 0),
        key=lambda component: -component.points,
    )
    return " | ".join(
        f"{component.name}:{component.points:+g}({component.detail})"
        for component in positive[:8]
    )


def penalty_reasons(item: RankedSignal) -> str:
    negative = sorted(
        (component for component in item.components if component.points < 0),
        key=lambda component: component.points,
    )
    return " | ".join(
        f"{component.name}:{component.points:+g}({component.detail})"
        for component in negative[:8]
    )


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def ranked_row(item: RankedSignal, tz: ZoneInfo) -> dict[str, Any]:
    outcome = item.outcome
    row: dict[str, Any] = {
        "ranking_version": RANKING_VERSION,
        "global_rank": item.global_rank,
        "global_percentile": item.global_percentile,
        "quintile": item.quintile,
        "priority_band": item.priority_band,
        "setup_rank": item.setup_rank,
        "setup_percentile": item.setup_percentile,
        "raw_priority_score": item.raw_score,
        "signal_id": item.signal_id,
        "symbol": item.symbol,
        "setup_family": item.setup_family,
        "setup_subtype": item.setup_subtype,
        "side": item.side,
        "decision_time": iso_local(item.decision_time, tz),
        "entry_price": item.entry_price,
        "candidate_entry_price": item.candidate_entry_price,
        "candidate_staleness_atr": item.features.get("candidate_staleness_atr"),
        "opportunity_key": item.opportunity_key,
        "accepted_range_id": item.accepted_range_id,
        "advisor_action": item.advisor_action,
        "advisor_reason_codes": "|".join(item.advisor_reason_codes),
        "snapshot_found": item.snapshot_found,
        "context_coverage_pct": item.context_coverage_pct,
        "missing_core_features": "|".join(item.missing_core_features),
        "previous_signal_count": item.previous_signal_count,
        "previous_opposite_signal_count": item.previous_opposite_signal_count,
        "previous_same_side_signal_count": item.previous_same_side_signal_count,
        "previous_same_range_count": item.previous_same_range_count,
        "previous_same_opportunity_count": item.previous_same_opportunity_count,
        "positive_score_reasons": positive_reasons(item),
        "penalty_score_reasons": penalty_reasons(item),
        "session_position": item.features.get("session_position"),
        "accepted_range_inside": item.features.get("accepted_range_inside"),
        "accepted_range_position": item.features.get("accepted_range_position"),
        "accepted_range_outside_atr": item.features.get(
            "accepted_range_outside_atr"
        ),
        "accepted_range_quality": item.features.get("accepted_range_quality"),
        "accepted_range_age_bars": item.features.get(
            "accepted_range_age_bars"
        ),
        "accepted_range_width_atr": item.features.get(
            "accepted_range_width_atr"
        ),
        "accepted_range_directional_efficiency": item.features.get(
            "accepted_range_directional_efficiency"
        ),
        "accepted_range_close_occupancy": item.features.get(
            "accepted_range_close_occupancy"
        ),
        "directional_extension_atr": item.features.get(
            "directional_extension_atr"
        ),
        "room_to_session_extreme_atr": item.features.get(
            "room_to_session_extreme_atr"
        ),
        "path_efficiency": item.features.get("path_efficiency"),
        "path_directional_ratio": item.features.get(
            "path_directional_ratio"
        ),
        "atr": item.features.get("atr"),
        "adx": item.features.get("adx"),
        "rsi": item.features.get("rsi"),
        "move_15m_atr": item.features.get("move_15m_atr"),
        "move_30m_atr": item.features.get("move_30m_atr"),
        "move_60m_atr": item.features.get("move_60m_atr"),
        "price_slope_state": item.features.get("price_slope_state"),
        "vwap_side": item.features.get("vwap_side"),
        "vwap_distance_atr": item.features.get("vwap_distance_atr"),
        "vwap_flip_count_today": item.features.get(
            "vwap_flip_count_today"
        ),
        "hma_state": item.features.get("hma_state"),
        "hma_strength": item.features.get("hma_strength"),
        "hma_flip_count_today": item.features.get(
            "hma_flip_count_today"
        ),
        "ema_ref": item.features.get("ema_ref"),
        "ema_slow": item.features.get("ema_slow"),
        "ema_envelope": item.features.get("ema_envelope"),
        "auction_state": item.features.get("auction_state"),
        "auction_flip_count_today": item.features.get(
            "auction_flip_count_today"
        ),
        "has_trade_package": outcome is not None,
        "trade_leg_count": outcome.leg_count if outcome else 0,
        "package_entry_exec_time": (
            iso_local(outcome.entry_exec_time, tz) if outcome else ""
        ),
        "package_exit_exec_time": (
            iso_local(outcome.exit_exec_time, tz) if outcome else ""
        ),
        "package_fully_entered": outcome.fully_entered if outcome else False,
        "package_fully_exited": outcome.fully_exited if outcome else False,
        "realized_package_pnl_evaluation_only": (
            outcome.package_pnl if outcome else None
        ),
        "realized_result_evaluation_only": (
            "WIN"
            if outcome and outcome.package_pnl is not None and outcome.package_pnl > 0
            else "LOSS"
            if outcome and outcome.package_pnl is not None and outcome.package_pnl < 0
            else "FLAT"
            if outcome and outcome.package_pnl == 0
            else "NO_OUTCOME"
        ),
    }
    for percentile in TOP_PERCENTILES:
        row[f"top_{percentile}_percent"] = item.top_percentile_flags[percentile]
    if WRITE_COMPONENT_JSON:
        row["score_components_json"] = component_json(item)
    return row


def summary_rows_by_key(
    items: Sequence[RankedSignal],
    key_name: str,
    key_func,
) -> list[dict[str, Any]]:
    grouped: dict[Any, list[RankedSignal]] = defaultdict(list)
    for item in items:
        grouped[key_func(item)].append(item)

    rows: list[dict[str, Any]] = []
    for key in sorted(grouped, key=lambda value: str(value)):
        row = {key_name: key}
        row.update(evaluation_metrics(grouped[key]))
        rows.append(row)
    return rows


def markdown_table(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> str:
    if not rows:
        return "_No rows._"
    header = "| " + " | ".join(columns) + " |"
    separator = "|" + "|".join(["---"] * len(columns)) + "|"
    body = []
    for row in rows:
        values = []
        for column in columns:
            value = row.get(column)
            if isinstance(value, float):
                value = f"{value:,.4f}".rstrip("0").rstrip(".")
            values.append("" if value is None else str(value))
        body.append("| " + " | ".join(values) + " |")
    return "\n".join([header, separator, *body])


def create_reports(
    *,
    output_dir: Path,
    items: list[RankedSignal],
    errors: list[dict[str, Any]],
    manifest: dict[str, Any],
    tz: ZoneInfo,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)

    ranked_rows = [ranked_row(item, tz) for item in items]
    ranked_fields = list(ranked_rows[0].keys()) if ranked_rows else [
        "ranking_version",
        "signal_id",
    ]

    quintile_rows = summary_rows_by_key(
        items, "quintile", lambda item: item.quintile
    )
    setup_rows = summary_rows_by_key(
        items, "setup_family", lambda item: item.setup_family
    )

    percentile_rows: list[dict[str, Any]] = []
    for percentile in TOP_PERCENTILES:
        selected = [
            item for item in items if item.top_percentile_flags[percentile]
        ]
        row = {"selection": f"TOP_{percentile}_PERCENT"}
        row.update(evaluation_metrics(selected))
        percentile_rows.append(row)
    all_row = {"selection": "ALL_SIGNALS"}
    all_row.update(evaluation_metrics(items))
    percentile_rows.append(all_row)

    paths = {
        "ranked": output_dir / f"{OUTPUT_PREFIX}_ranked.csv",
        "quintiles": output_dir / f"{OUTPUT_PREFIX}_quintiles.csv",
        "top_percentiles": output_dir / f"{OUTPUT_PREFIX}_top_percentiles.csv",
        "setups": output_dir / f"{OUTPUT_PREFIX}_setups.csv",
        "summary": output_dir / f"{OUTPUT_PREFIX}_summary.md",
        "manifest": output_dir / f"{OUTPUT_PREFIX}_manifest.json",
        "errors": output_dir / f"{OUTPUT_PREFIX}_errors.csv",
    }

    write_csv(paths["ranked"], ranked_rows, ranked_fields)
    write_csv(
        paths["quintiles"],
        quintile_rows,
        list(quintile_rows[0].keys()) if quintile_rows else ["quintile"],
    )
    write_csv(
        paths["top_percentiles"],
        percentile_rows,
        list(percentile_rows[0].keys()) if percentile_rows else ["selection"],
    )
    write_csv(
        paths["setups"],
        setup_rows,
        list(setup_rows[0].keys()) if setup_rows else ["setup_family"],
    )
    write_csv(
        paths["errors"],
        errors,
        (
            "signal_id",
            "symbol",
            "error_type",
            "error_message",
            "traceback",
        ),
    )

    manifest.update(
        {
            "signal_count_ranked": len(items),
            "record_error_count": len(errors),
            "overall_metrics": evaluation_metrics(items),
            "output_files": {key: str(path) for key, path in paths.items()},
        }
    )
    paths["manifest"].write_text(
        json.dumps(manifest, indent=2, sort_keys=True, default=json_default),
        encoding="utf-8",
    )

    top_items = items[: min(20, len(items))]
    top_rows = [
        {
            "rank": item.global_rank,
            "symbol": item.symbol,
            "setup": item.setup_family,
            "side": item.side,
            "score": item.raw_score,
            "band": item.priority_band,
            "pnl_eval_only": (
                item.outcome.package_pnl
                if item.outcome and item.outcome.package_pnl is not None
                else None
            ),
        }
        for item in top_items
    ]

    summary = f"""# AutoTrades Signal Ranking Study

**Ranking version:** `{RANKING_VERSION}`  
**Trading date:** `{manifest['test_date']}`  
**Run ID:** `{manifest['run_id']}`  
**Config hash:** `{manifest['config_hash']}`  
**Signals ranked:** {len(items)}  
**Record errors:** {len(errors)}

## Safety and leakage boundary

The score uses only immutable signal-creation criteria, the snapshot at the
creation timestamp, and earlier signals already known at that time.

The score does **not** use MFE, MAE, exit price, exit reason, later lifecycle
state, later Auction transitions, future candles or realized P&L.

`realized_package_pnl_evaluation_only` is joined after ranking and exists solely
to evaluate whether the ordering is useful.

## Top percentile evaluation

{markdown_table(percentile_rows, ('selection', 'signals', 'wins', 'losses', 'win_rate_pct', 'total_pnl', 'median_pnl', 'profit_factor'))}

## Quintile evaluation

Quintile 1 is the highest-scored 20%.

{markdown_table(quintile_rows, ('quintile', 'signals', 'wins', 'losses', 'win_rate_pct', 'total_pnl', 'median_pnl', 'profit_factor'))}

## Setup-family evaluation

{markdown_table(setup_rows, ('setup_family', 'signals', 'wins', 'losses', 'win_rate_pct', 'total_pnl', 'median_pnl', 'profit_factor'))}

## Highest-ranked signals

{markdown_table(top_rows, ('rank', 'symbol', 'setup', 'side', 'score', 'band', 'pnl_eval_only'))}

## Interpretation protocol

1. Do not tune weights until this run is archived.
2. Examine high-ranked losers and low-ranked winners.
3. Freeze the next ranking version before testing an unseen date.
4. Prefer monotonic quintile improvement over one-day top-group P&L.
5. Do not connect this score to live selection until several unseen dates show
   stable separation and winner controls are preserved.
"""
    paths["summary"].write_text(summary, encoding="utf-8")
    return paths


# =============================================================================
# MAIN
# =============================================================================

def main() -> int:
    args = parse_args()
    test_date_obj = date.fromisoformat(args.test_date)
    tz = ZoneInfo(LOCAL_TIMEZONE)
    start_local = datetime.combine(test_date_obj, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    start_naive = db_naive(start_local, tz)
    end_naive = db_naive(end_local, tz)

    db_kwargs = connection_kwargs(args.db_name)
    db_name = str(db_kwargs["database"])

    config = config_payload(args.test_date, db_name)
    config_digest = config_hash(config)
    run_id = (
        f"{RANKING_VERSION.lower()}_"
        f"{args.test_date}_"
        f"{datetime.now(tz).strftime('%Y%m%d_%H%M%S')}"
    )
    output_root = Path(args.output_root) if args.output_root else OUTPUT_ROOT
    output_dir = output_root / args.test_date / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    log_path = output_dir / "test_ranking_signals.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s:%(lineno)d] %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )

    logger = logging.getLogger("test_ranking_signals")
    logger.info(
        "Starting ranking study | version=%s date=%s db=%s config_hash=%s",
        RANKING_VERSION,
        args.test_date,
        db_name,
        config_digest,
    )

    manifest: dict[str, Any] = {
        "run_id": run_id,
        "created_at": datetime.now(tz).isoformat(),
        "test_date": args.test_date,
        "ranking_version": RANKING_VERSION,
        "ranking_description": RANKING_DESCRIPTION,
        "config_hash": config_digest,
        "config": config,
        "database": {
            "host": db_kwargs["host"],
            "port": db_kwargs["port"],
            "name": db_name,
            "user": db_kwargs["user"],
            # Password deliberately omitted.
        },
        "read_only": True,
        "ranking_input_excludes": [
            "MFE",
            "MAE",
            "exit_price",
            "exit_reason",
            "later_lifecycle_state",
            "future_auction_transitions",
            "future_candles",
            "realized_pnl",
        ],
    }

    connection: Optional[mysql.connector.MySQLConnection] = None
    errors: list[dict[str, Any]] = []

    try:
        connection = open_read_only_connection(args.db_name)

        signal_rows = load_signals(connection, start_naive, end_naive)
        logger.info("Loaded signals | count=%d", len(signal_rows))
        if not signal_rows:
            raise RuntimeError(
                f"No signals found for {args.test_date} in {SIGNALS_TABLE}"
            )

        # Parse immutable creation criteria first so snapshot requests are exact.
        prepared: list[tuple[dict[str, Any], dict[str, Any], datetime]] = []
        symbols: set[str] = set()

        for row in signal_rows:
            try:
                entry = immutable_entry_criteria(row)
                decision_time = parse_datetime(entry["snapshot_time"], tz)
                symbol = str(row["symbol"])
                symbols.add(symbol)
                prepared.append((row, entry, decision_time))
            except Exception as exc:
                errors.append(
                    {
                        "signal_id": str(row.get("signal_id") or ""),
                        "symbol": str(row.get("symbol") or ""),
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                        "traceback": traceback.format_exc(),
                    }
                )
                logger.exception(
                    "Signal preparation failed | signal_id=%s symbol=%s",
                    row.get("signal_id"),
                    row.get("symbol"),
                )

        snapshot_map = load_creation_snapshots(
            connection,
            start_naive,
            end_naive,
            sorted(symbols),
        )
        logger.info(
            "Loaded date snapshots | count=%d symbols=%d",
            len(snapshot_map),
            len(symbols),
        )

        outcomes: dict[str, PackageOutcome] = {}
        include_outcomes = INCLUDE_REALIZED_OUTCOMES and not args.no_outcomes
        if include_outcomes:
            outcomes = load_package_outcomes(
                connection,
                [str(row["signal_id"]) for row, _, _ in prepared],
            )
            logger.info("Loaded package outcomes | count=%d", len(outcomes))

        ranked: list[RankedSignal] = []
        chronological_history: list[RankedSignal] = []

        for row, entry, decision_time in sorted(
            prepared,
            key=lambda item: (
                item[2],
                str(item[0]["symbol"]),
                str(item[0]["signal_id"]),
            ),
        ):
            signal_id = str(row["signal_id"])
            symbol = str(row["symbol"])
            try:
                setup_family = normalize_enum(
                    entry.get("setup_family") or row["setup_family"]
                )
                setup_subtype = normalize_enum(entry.get("setup_subtype"))
                side = normalize_enum(entry.get("side") or row["side"])
                entry_price = finite_float(entry.get("entry_price"))
                if entry_price is None or entry_price <= 0:
                    raise ValueError("entry_criteria_json.entry_price must be positive")

                candidate_entry_price = finite_float(
                    entry.get("candidate_entry_price")
                )
                opportunity_key = str(entry.get("opportunity_key") or "")

                stock_context = entry.get("stock_context")
                stock_context = (
                    stock_context if isinstance(stock_context, Mapping) else {}
                )
                accepted_range_id = str(
                    stock_context.get("accepted_range_id") or ""
                )

                advisor = entry.get("advisor")
                advisor = advisor if isinstance(advisor, Mapping) else {}
                advisor_action = normalize_enum(advisor.get("action"))
                advisor_reason_codes = advisor.get("reason_codes")
                advisor_reason_codes = (
                    [str(code) for code in advisor_reason_codes]
                    if isinstance(advisor_reason_codes, list)
                    else []
                )

                snapshot_key = (
                    symbol,
                    db_naive(decision_time, tz),
                )
                snapshot = snapshot_map.get(snapshot_key)
                snapshot_found = snapshot is not None
                if REQUIRE_CREATION_SNAPSHOT and not snapshot_found:
                    raise ValueError(
                        f"Creation snapshot not found for {symbol} "
                        f"@ {decision_time.isoformat()}"
                    )

                features = extract_features(entry, snapshot, side)

                item = score_signal(
                    signal_id=signal_id,
                    symbol=symbol,
                    setup_family=setup_family,
                    setup_subtype=setup_subtype,
                    side=side,
                    decision_time=decision_time,
                    entry_price=entry_price,
                    candidate_entry_price=candidate_entry_price,
                    opportunity_key=opportunity_key,
                    accepted_range_id=accepted_range_id,
                    advisor_action=advisor_action,
                    advisor_reason_codes=advisor_reason_codes,
                    snapshot_found=snapshot_found,
                    features=features,
                    history=chronological_history,
                )
                item.outcome = outcomes.get(signal_id)
                ranked.append(item)
                chronological_history.append(item)

            except Exception as exc:
                errors.append(
                    {
                        "signal_id": signal_id,
                        "symbol": symbol,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                        "traceback": traceback.format_exc(),
                    }
                )
                logger.exception(
                    "Per-signal ranking failed | signal_id=%s symbol=%s",
                    signal_id,
                    symbol,
                )
                continue

        if not ranked:
            raise RuntimeError("No signals were successfully ranked")

        assign_ranks(ranked)

        manifest["database_counts"] = {
            "signal_rows_loaded": len(signal_rows),
            "signals_prepared": len(prepared),
            "date_snapshots_loaded": len(snapshot_map),
            "package_outcomes_loaded": len(outcomes),
        }

        paths = create_reports(
            output_dir=output_dir,
            items=ranked,
            errors=errors,
            manifest=manifest,
            tz=tz,
        )

        logger.info(
            "Ranking complete | ranked=%d errors=%d output_dir=%s",
            len(ranked),
            len(errors),
            output_dir,
        )
        for name, path in paths.items():
            logger.info("Output | %s=%s", name, path)

        overall = evaluation_metrics(ranked)
        print()
        print("Signal ranking completed")
        print(f"  Version: {RANKING_VERSION}")
        print(f"  Date: {args.test_date}")
        print(f"  Signals ranked: {len(ranked)}")
        print(f"  Record errors: {len(errors)}")
        print(f"  Total P&L (evaluation only): {overall.get('total_pnl')}")
        print(f"  Output: {output_dir}")

        if errors and FAIL_IF_ANY_RECORD_ERROR:
            return 2
        return 0

    except Exception:
        logger.exception("Ranking study failed during startup/preflight/run")
        return 1

    finally:
        if connection is not None:
            try:
                connection.rollback()
            except MySQLError:
                logger.exception("Rollback failed while closing read-only connection")
            try:
                connection.close()
            except MySQLError:
                logger.exception("Database connection close failed")


if __name__ == "__main__":
    raise SystemExit(main())
