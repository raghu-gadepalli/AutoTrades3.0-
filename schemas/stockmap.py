from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.exc import IntegrityError

from database.database import get_trades_db
from models.trade_models import StockMap as StockMapORM
from utils.json_utils import sanitize_json

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
STOCKMAP_PERIOD_MINUTES = 15
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 15
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MINUTE = 15


def _as_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=IST)
    return value.astimezone(IST)


def _db_time(value: datetime) -> datetime:
    return _as_ist(value).replace(tzinfo=None)


def expected_stockmap_time_for_asof(asof_time: datetime) -> Optional[datetime]:
    """Return the latest completed 15-minute candle start label.

    StockMap follows Snapshot's candle-start convention. For example, an
    as-of time of 10:07 maps to the completed 09:45-10:00 candle and therefore
    returns 09:45. At and after the EQ close, the final key is 15:00.
    """

    asof = _as_ist(asof_time)
    market_open = asof.replace(
        hour=MARKET_OPEN_HOUR,
        minute=MARKET_OPEN_MINUTE,
        second=0,
        microsecond=0,
    )
    market_close = asof.replace(
        hour=MARKET_CLOSE_HOUR,
        minute=MARKET_CLOSE_MINUTE,
        second=0,
        microsecond=0,
    )
    effective = min(asof, market_close)
    elapsed_minutes = int((effective - market_open).total_seconds() // 60)
    completed_candles = elapsed_minutes // STOCKMAP_PERIOD_MINUTES
    if completed_candles <= 0:
        return None
    return market_open + timedelta(
        minutes=(completed_candles - 1) * STOCKMAP_PERIOD_MINUTES
    )

STRICT_MODEL_CONFIG = ConfigDict(
    extra="forbid",
    arbitrary_types_allowed=True,
    populate_by_name=True,
    validate_assignment=True,
)


class StrictBaseModel(BaseModel):
    model_config = STRICT_MODEL_CONFIG


class StockMapBarBlock(StrictBaseModel):
    candle_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    @model_validator(mode="after")
    def validate_ohlcv(self) -> "StockMapBarBlock":
        values = (self.open, self.high, self.low, self.close, self.volume)
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("stockmap bar OHLCV values must be finite")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("stockmap bar prices must be positive")
        if self.volume < 0:
            raise ValueError("stockmap bar volume cannot be negative")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("stockmap bar.high is inconsistent with OHLC")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("stockmap bar.low is inconsistent with OHLC")
        return self


class PrevDayBlock(StrictBaseModel):
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None


class TodayBlock(StrictBaseModel):
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None


class OpeningRangeBlock(StrictBaseModel):
    window: str
    high: Optional[float] = None
    low: Optional[float] = None
    ready: bool = False

    @model_validator(mode="after")
    def validate_range(self) -> "OpeningRangeBlock":
        if self.ready and (self.high is None or self.low is None):
            raise ValueError("opening range high/low are required when ready=True")
        if self.high is not None and self.low is not None and self.high <= self.low:
            raise ValueError("opening range high must exceed low")
        return self


class StockMapLevelsBlock(StrictBaseModel):
    prev_day: PrevDayBlock
    today: TodayBlock
    opening_range: OpeningRangeBlock


class StockMapEMAContextBlock(StrictBaseModel):
    ema100: Optional[float] = None
    ema200: Optional[float] = None
    ema100_slope: Optional[float] = None
    ema200_slope: Optional[float] = None
    price_to_ema100: str = "UNAVAILABLE"
    price_to_ema200: str = "UNAVAILABLE"
    ordering: str = "UNAVAILABLE"
    regime: str = "UNAVAILABLE"


class StockMapATRBlock(StrictBaseModel):
    value: Optional[float] = None
    pct: Optional[float] = None


class StockMapIndicatorsBlock(StrictBaseModel):
    ema: StockMapEMAContextBlock
    atr: StockMapATRBlock


# ---------------------------------------------------------------------------
# Structure contracts intentionally mirror schemas.snapshot for V1.
# ---------------------------------------------------------------------------
class StructureRangeBlock(StrictBaseModel):
    range_id: Optional[str] = None
    version: int = Field(default=0, ge=0)
    high: Optional[float] = None
    low: Optional[float] = None
    width_pct: Optional[float] = None
    width_atr: Optional[float] = None
    source: str = "UNKNOWN"
    range_type: str = "UNKNOWN"
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    established_at: Optional[datetime] = None
    evidence_cutoff: Optional[datetime] = None
    bars: int = Field(default=0, ge=0)
    provisional: bool = False
    breakout_eligible: bool = False


class BalanceMetricsBlock(StrictBaseModel):
    adjacent_overlap_ratio: Optional[float] = None
    directional_efficiency: Optional[float] = None
    net_displacement_fraction: Optional[float] = None
    close_occupancy_ratio: Optional[float] = None
    midpoint_drift_atr: Optional[float] = None
    upper_boundary_drift_atr: Optional[float] = None
    lower_boundary_drift_atr: Optional[float] = None
    upper_interactions: int = Field(default=0, ge=0)
    lower_interactions: int = Field(default=0, ge=0)
    quality: Optional[float] = None
    classification: str = "UNKNOWN"
    reason: Optional[str] = None


class RawStructureBlock(StrictBaseModel):
    state: str = "UNKNOWN"
    side: str = "NEUTRAL"
    range: StructureRangeBlock = Field(default_factory=StructureRangeBlock)
    metrics: BalanceMetricsBlock = Field(default_factory=BalanceMetricsBlock)
    recent_swing_high: Optional[float] = None
    recent_swing_low: Optional[float] = None
    reason: Optional[str] = None


class AcceptedStructureBlock(StrictBaseModel):
    state: str = "RANGE_ACCEPTED"
    side: Optional[str] = None
    range: StructureRangeBlock = Field(default_factory=StructureRangeBlock)
    metrics: BalanceMetricsBlock = Field(default_factory=BalanceMetricsBlock)
    age_bars: int = Field(default=0, ge=0)
    frozen: bool = True
    promoted_time: Optional[datetime] = None
    quality: Optional[float] = None
    reason: Optional[str] = None


class CandidateStructureBlock(StrictBaseModel):
    active: bool = False
    status: str = "NONE"
    side: str = "NEUTRAL"
    range: StructureRangeBlock = Field(default_factory=StructureRangeBlock)
    metrics: BalanceMetricsBlock = Field(default_factory=BalanceMetricsBlock)
    bars_confirmed: int = Field(default=0, ge=0)
    first_seen_time: Optional[datetime] = None
    quality: Optional[float] = None
    reason: Optional[str] = None


class RecentCloseObservationBlock(StrictBaseModel):
    time: datetime
    close: float


class StructureAnchorBlock(StrictBaseModel):
    pdh: Optional[float] = None
    pdl: Optional[float] = None
    orb_high: Optional[float] = None
    orb_low: Optional[float] = None
    orb_ready: bool = False
    recent15_high: Optional[float] = None
    recent15_low: Optional[float] = None
    active_anchor: str = "UNKNOWN"


class BreakoutContextBlock(StrictBaseModel):
    swing: str = "UNKNOWN"
    orb: str = "UNKNOWN"
    pdh_pdl: str = "UNKNOWN"
    recent15: str = "UNKNOWN"


class StructureBlock(StrictBaseModel):
    raw: RawStructureBlock
    accepted: AcceptedStructureBlock
    candidate: CandidateStructureBlock
    session_phase: str
    flip_count_today: int = Field(ge=0)
    recent_closes: List[RecentCloseObservationBlock] = Field(default_factory=list)
    anchors: StructureAnchorBlock = Field(default_factory=StructureAnchorBlock)
    breakout_context: BreakoutContextBlock = Field(default_factory=BreakoutContextBlock)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
    previous_state: Optional[str] = None
    previous_side: Optional[str] = None
    count: int = Field(default=1, ge=0)
    reason: Optional[str] = None


class StateMemoryEntry(StrictBaseModel):
    raw_state: str
    state: str
    count: int = Field(ge=0)
    previous_state: Optional[str] = None
    previous_count: int = Field(ge=0)
    candidate_state: Optional[str] = None
    candidate_count: int = Field(ge=0)
    flip_count_today: int = Field(ge=0)


class StockMapMemoryBlock(StrictBaseModel):
    stockmap_time: datetime
    state: Dict[str, StateMemoryEntry]


class StockMapLocationBlock(StrictBaseModel):
    accepted_range_position: str = "UNKNOWN"
    accepted_range_position_pct: Optional[float] = None
    nearest_support_type: Optional[str] = None
    nearest_support_price: Optional[float] = None
    nearest_support_distance_atr: Optional[float] = None
    nearest_resistance_type: Optional[str] = None
    nearest_resistance_price: Optional[float] = None
    nearest_resistance_distance_atr: Optional[float] = None
    room_up_atr: Optional[float] = None
    room_down_atr: Optional[float] = None


class StockMapDiagnosticsBlock(StrictBaseModel):
    calculation_version: str
    calculation_mode: str
    availability: str
    source_start_time: Optional[datetime] = None
    source_end_time: datetime
    bars_used: int = Field(ge=0)
    missing_bar_count: int = Field(default=0, ge=0)
    reason_codes: List[str] = Field(default_factory=list)


class StockMapSchema(StrictBaseModel):
    symbol: str
    stockmap_time: datetime
    tf: str = "15m"
    close: float
    bar: StockMapBarBlock
    levels: StockMapLevelsBlock
    indicators: StockMapIndicatorsBlock
    structure: StructureBlock
    location: StockMapLocationBlock
    memory: StockMapMemoryBlock
    diagnostics: StockMapDiagnosticsBlock

    @field_validator("symbol", "tf")
    @classmethod
    def require_nonempty_text(cls, value: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("stockmap identity fields cannot be empty")
        return text

    @model_validator(mode="after")
    def validate_stockmap_contract(self) -> "StockMapSchema":
        if not math.isfinite(float(self.close)) or self.close <= 0:
            raise ValueError("stockmap.close must be a positive finite value")
        tolerance = max(1e-9, abs(self.close) * 1e-9)
        if abs(self.close - self.bar.close) > tolerance:
            raise ValueError("stockmap.close must equal stockmap.bar.close")
        if self.bar.candle_time != self.stockmap_time:
            raise ValueError("bar.candle_time must equal stockmap_time")
        if self.memory.stockmap_time != self.stockmap_time:
            raise ValueError("memory.stockmap_time must equal stockmap_time")
        if self.diagnostics.source_end_time > self.stockmap_time:
            raise ValueError("diagnostics.source_end_time cannot exceed stockmap_time")
        return self

    def to_db_dict(self) -> Dict[str, Any]:
        return sanitize_json(self.model_dump(mode="python"))

    @staticmethod
    def from_db_dict(dump: Dict[str, Any]) -> "StockMapSchema":
        if not isinstance(dump, dict) or not dump:
            raise ValueError("Empty stockmap dump")
        return StockMapSchema.model_validate(dump)

    @staticmethod
    def create_stockmap(stockmap: "StockMapSchema") -> "StockMapSchema":
        raw = stockmap.to_db_dict()
        db_time = _db_time(stockmap.stockmap_time)

        with get_trades_db() as db:
            existing = (
                db.query(StockMapORM)
                .filter(StockMapORM.symbol == stockmap.symbol)
                .filter(StockMapORM.stockmap_time == db_time)
                .first()
            )
            if existing:
                existing.data = raw
                db.commit()
                return stockmap

            db.add(
                StockMapORM(
                    symbol=stockmap.symbol,
                    stockmap_time=db_time,
                    data=raw,
                )
            )
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = (
                    db.query(StockMapORM)
                    .filter(StockMapORM.symbol == stockmap.symbol)
                    .filter(StockMapORM.stockmap_time == db_time)
                    .first()
                )
                if existing is None:
                    raise
                existing.data = raw
                db.commit()
        return stockmap

    @staticmethod
    def fetch_stockmap(symbol: str, stockmap_time: datetime) -> Optional["StockMapSchema"]:
        db_time = _db_time(stockmap_time)
        with get_trades_db() as db:
            rec = (
                db.query(StockMapORM)
                .filter(StockMapORM.symbol == str(symbol).strip().upper())
                .filter(StockMapORM.stockmap_time == db_time)
                .first()
            )
        return StockMapSchema.from_db_dict(rec.data) if rec and rec.data else None

    @staticmethod
    def fetch_latest_for_symbol(symbol: str) -> Optional["StockMapSchema"]:
        with get_trades_db() as db:
            rec = (
                db.query(StockMapORM)
                .filter(StockMapORM.symbol == str(symbol).strip().upper())
                .order_by(StockMapORM.stockmap_time.desc())
                .first()
            )
        return StockMapSchema.from_db_dict(rec.data) if rec and rec.data else None

    @staticmethod
    def fetch_latest_for_symbol_asof(symbol: str, asof_time: datetime) -> Optional["StockMapSchema"]:
        expected = expected_stockmap_time_for_asof(asof_time)
        if expected is None:
            return None
        return StockMapSchema.fetch_stockmap(symbol, expected)

    @staticmethod
    def fetch_range(
        symbol: str,
        start_time: datetime,
        end_time: datetime,
    ) -> List["StockMapSchema"]:
        start_db = _db_time(start_time)
        end_db = _db_time(end_time)
        with get_trades_db() as db:
            records = (
                db.query(StockMapORM)
                .filter(StockMapORM.symbol == str(symbol).strip().upper())
                .filter(StockMapORM.stockmap_time >= start_db)
                .filter(StockMapORM.stockmap_time <= end_db)
                .order_by(StockMapORM.stockmap_time.asc())
                .all()
            )
        return [
            StockMapSchema.from_db_dict(record.data)
            for record in records
            if record and record.data
        ]

    @staticmethod
    def fetch_previous_for_symbol(symbol: str, before_time: datetime) -> Optional["StockMapSchema"]:
        db_time = _db_time(before_time)
        with get_trades_db() as db:
            rec = (
                db.query(StockMapORM)
                .filter(StockMapORM.symbol == str(symbol).strip().upper())
                .filter(StockMapORM.stockmap_time < db_time)
                .order_by(StockMapORM.stockmap_time.desc())
                .first()
            )
        return StockMapSchema.from_db_dict(rec.data) if rec and rec.data else None

    @staticmethod
    def stockmap_exists(symbol: str, stockmap_time: datetime) -> bool:
        db_time = _db_time(stockmap_time)
        with get_trades_db() as db:
            return (
                db.query(StockMapORM.symbol)
                .filter(StockMapORM.symbol == str(symbol).strip().upper())
                .filter(StockMapORM.stockmap_time == db_time)
                .first()
                is not None
            )
