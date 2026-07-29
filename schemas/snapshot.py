# schemas/snapshot.py

from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, ConfigDict, model_validator, field_validator
from sqlalchemy import Text, cast, or_, and_
from sqlalchemy.exc import IntegrityError

from database.database import get_trades_db
from models.trade_models import Snapshot as SnapshotORM
from utils.json_utils import sanitize_json
from services.auction_engine.episode_contracts import (
    AuctionEpisodeMemory,
    AuctionLifecycleProjection,
    AuctionObservation,
)

logger = logging.getLogger(__name__)


STRICT_MODEL_CONFIG = ConfigDict(
    extra="forbid",
    arbitrary_types_allowed=True,
    populate_by_name=True,
    validate_assignment=True,
)


class StrictBaseModel(BaseModel):
    model_config = STRICT_MODEL_CONFIG


# -----------------------------
# Core market facts
# -----------------------------
class BarBlock(StrictBaseModel):
    open: float
    high: float
    low: float
    close: float
    volume: float

    @model_validator(mode="after")
    def validate_ohlcv(self) -> "BarBlock":
        values = (self.open, self.high, self.low, self.close, self.volume)
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("bar OHLCV values must be finite")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("bar prices must be positive")
        if self.volume < 0:
            raise ValueError("bar.volume cannot be negative")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("bar.high is inconsistent with OHLC")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("bar.low is inconsistent with OHLC")
        return self


class PrevDayBlock(StrictBaseModel):
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]


class TodayBlock(StrictBaseModel):
    open: Optional[float]


class OpeningRangeBlock(StrictBaseModel):
    window: str
    high: Optional[float]
    low: Optional[float]
    ready: bool

    @model_validator(mode="after")
    def validate_range(self) -> "OpeningRangeBlock":
        if self.ready and (self.high is None or self.low is None):
            raise ValueError("opening range high/low are required when ready=True")
        if self.high is not None and self.low is not None and self.high <= self.low:
            raise ValueError("opening range high must exceed low")
        return self


class LevelsBlock(StrictBaseModel):
    prev_day: PrevDayBlock
    today: TodayBlock
    opening_range: OpeningRangeBlock


# -----------------------------
# Indicator facts: one current value set only
# -----------------------------
class EMABlock(StrictBaseModel):
    fast: Optional[float]
    mid1: Optional[float]
    mid2: Optional[float]
    slow: Optional[float]
    ref: Optional[float]


class HMABlock(StrictBaseModel):
    fast: Optional[float]
    mid1: Optional[float]
    mid2: Optional[float]
    slow: Optional[float]
    state: str
    strength: str
    flip_count_today: int = Field(ge=0)


class VWAPBlock(StrictBaseModel):
    value: Optional[float]
    side: str
    distance_pct: Optional[float]
    distance_points: Optional[float]
    distance_atr: Optional[float]
    flip_count_today: int = Field(ge=0)


class RSIBlock(StrictBaseModel):
    value: Optional[float]
    zone: str


class ADXBlock(StrictBaseModel):
    value: Optional[float]
    band: str


class ATRBlock(StrictBaseModel):
    value: float = Field(gt=0)
    band: str
    pct: Optional[float]


class BollingerBlock(StrictBaseModel):
    upper: Optional[float]
    mid: Optional[float]
    lower: Optional[float]
    bb_width: Optional[float]
    position: Optional[float]
    zone: str


class EnvelopeBlock(StrictBaseModel):
    hma_envelope: Optional[float]
    ema_envelope: Optional[float]


class IndicatorsBlock(StrictBaseModel):
    ema: EMABlock
    hma: HMABlock
    vwap: VWAPBlock
    rsi: RSIBlock
    adx: ADXBlock
    atr: ATRBlock
    bollinger: BollingerBlock
    envelopes: EnvelopeBlock


class VolumeBlock(StrictBaseModel):
    bar_volume: Optional[float]
    bar_rvol: Optional[float]
    bar_rvol_pct: Optional[float]
    bar_rvol_band: str
    bar_volume_slope: Optional[float]
    today_cum: Optional[float]
    prev_day_total: Optional[float]
    today_vs_prev_ratio: Optional[float]
    periods: Dict[str, Any]




class DerivativesBlock(StrictBaseModel):
    """Existing display/option-selection derivative payload, shape retained."""
    spot_price: Any
    future: Any
    options_lite: Any
    option_ladder: Any
    option_sentiment_windows: Any
    future_sentiment_windows: Any


# -----------------------------
# Price-action windows
# -----------------------------
class MarketWindowBlock(StrictBaseModel):
    status: str
    bars: int = Field(ge=0)
    move_points: Optional[float]
    move_pct: Optional[float]
    move_atr: Optional[float]
    range_points: Optional[float]
    range_pct: Optional[float]
    close_position_in_range: Optional[float]


class MarketWindowsBlock(StrictBaseModel):
    m15: MarketWindowBlock = Field(alias="15m")
    m30: MarketWindowBlock = Field(alias="30m")
    m60: MarketWindowBlock = Field(alias="60m")
    sod: MarketWindowBlock


class PriceActionSlopeBlock(StrictBaseModel):
    status: str
    bars_3_atr: Optional[float]
    bars_5_atr: Optional[float]
    bars_3_atr_per_bar: Optional[float]
    bars_5_atr_per_bar: Optional[float]
    previous_3_atr_per_bar: Optional[float]
    state: str


class PriceActionBlock(StrictBaseModel):
    slope: PriceActionSlopeBlock


# -----------------------------
# Structure result
# -----------------------------
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
    side: Optional[str] = Field(default=None, exclude=True)
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

    # Internal construction details are accepted by the structure calculator
    # but are deliberately excluded from persisted/public snapshots.
    recent_closes: List[RecentCloseObservationBlock] = Field(default_factory=list, exclude=True)
    anchors: StructureAnchorBlock = Field(default_factory=lambda: StructureAnchorBlock(
        pdh=None, pdl=None, orb_high=None, orb_low=None, orb_ready=False,
        recent15_high=None, recent15_low=None, active_anchor="UNKNOWN"
    ), exclude=True)
    breakout_context: BreakoutContextBlock = Field(default_factory=lambda: BreakoutContextBlock(
        swing="UNKNOWN", orb="UNKNOWN", pdh_pdl="UNKNOWN", recent15="UNKNOWN"
    ), exclude=True)
    diagnostics: Dict[str, Any] = Field(default_factory=dict, exclude=True)
    previous_state: Optional[str] = Field(default=None, exclude=True)
    previous_side: Optional[str] = Field(default=None, exclude=True)
    count: int = Field(default=1, ge=0, exclude=True)
    reason: Optional[str] = Field(default=None, exclude=True)


# -----------------------------
# Private continuity memory
# -----------------------------
class StateMemoryEntry(StrictBaseModel):
    raw_state: str
    state: str
    count: int = Field(ge=0)
    previous_state: Optional[str]
    previous_count: int = Field(ge=0)
    candidate_state: Optional[str]
    candidate_count: int = Field(ge=0)
    flip_count_today: int = Field(ge=0)


class StructureMemoryBar(StrictBaseModel):
    date: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class StructureMemoryBlock(StrictBaseModel):
    snapshot_time: datetime
    bars_3m: List[StructureMemoryBar]
    bars_15m: List[StructureMemoryBar]
    state: Dict[str, StateMemoryEntry]


class AuctionMemoryBlock(AuctionEpisodeMemory):
    """Authoritative snapshot-carried Auction Episode continuity."""



class SnapshotMemoryBlock(StrictBaseModel):
    structure: StructureMemoryBlock
    auction: AuctionMemoryBlock


# -----------------------------
# Compact Auction public projection
# -----------------------------
class AuctionEngineIdentityProjection(StrictBaseModel):
    name: str
    version: str
    config_version: str
    config_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class AuctionSnapshotBlock(StrictBaseModel):
    status: str
    continuity_mode: str
    previous_snapshot_time: Optional[datetime]
    engine: Optional[AuctionEngineIdentityProjection]
    observation: Optional[AuctionObservation]
    lifecycle: Optional[AuctionLifecycleProjection]

    @model_validator(mode="after")
    def validate_status(self) -> "AuctionSnapshotBlock":
        if self.status == "NOT_RUN":
            if any(
                value is not None
                for value in (
                    self.engine,
                    self.observation,
                    self.lifecycle,
                )
            ):
                raise ValueError("NOT_RUN Auction block cannot contain evaluation output")
            return self
        if self.status != "OK":
            raise ValueError("Auction status must be NOT_RUN or OK")
        if self.engine is None or self.observation is None or self.lifecycle is None:
            raise ValueError("Authoritative Auction output is required when status=OK")
        if self.observation.symbol != self.lifecycle.symbol:
            raise ValueError("Auction observation/lifecycle symbol mismatch")
        if self.observation.snapshot_time != self.lifecycle.snapshot_time:
            raise ValueError("Auction observation/lifecycle time mismatch")
        if self.engine.name != self.lifecycle.engine_name:
            raise ValueError("Auction engine name mismatch")
        if self.engine.version != self.lifecycle.engine_version:
            raise ValueError("Auction engine version mismatch")
        if self.engine.config_version != self.lifecycle.config_version:
            raise ValueError("Auction config version mismatch")
        if self.engine.config_hash != self.lifecycle.config_hash:
            raise ValueError("Auction config hash mismatch")
        if self.continuity_mode == "COLD_START":
            if self.previous_snapshot_time is not None:
                raise ValueError("COLD_START cannot reference a previous Auction snapshot")
        elif self.continuity_mode == "INCREMENTAL_PREVIOUS_SNAPSHOT":
            if self.previous_snapshot_time is None:
                raise ValueError("Incremental Auction continuity requires previous_snapshot_time")
        else:
            raise ValueError("Unsupported Auction continuity_mode")
        return self


# -----------------------------
# Snapshot schema
# -----------------------------
class SnapshotSchema(StrictBaseModel):
    version: str
    symbol: str
    snapshot_time: datetime
    tf: str

    close: float
    bar: BarBlock

    # Normal DB columns. They are deliberately excluded from the JSON payload.
    ltp: Optional[float] = None
    ltp_time: Optional[datetime] = None
    gen_signals: bool

    levels: LevelsBlock
    indicators: IndicatorsBlock
    volume: VolumeBlock
    market_windows: MarketWindowsBlock
    price_action: PriceActionBlock
    structure: StructureBlock
    derivatives: DerivativesBlock
    auction: AuctionSnapshotBlock
    memory: SnapshotMemoryBlock

    @field_validator("version", "symbol", "tf")
    @classmethod
    def require_nonempty_text(cls, value: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("snapshot identity fields cannot be empty")
        return text

    @model_validator(mode="after")
    def validate_snapshot_contract(self) -> "SnapshotSchema":
        if not math.isfinite(float(self.close)) or self.close <= 0:
            raise ValueError("snapshot.close must be a positive finite value")
        tolerance = max(1e-9, abs(self.close) * 1e-9)
        if abs(self.close - self.bar.close) > tolerance:
            raise ValueError("snapshot.close must equal snapshot.bar.close")
        if self.memory.structure.snapshot_time != self.snapshot_time:
            raise ValueError("memory.structure.snapshot_time must equal snapshot_time")
        if self.auction.status == "OK":
            if self.memory.auction.symbol != self.symbol.strip().upper():
                raise ValueError("memory.auction.symbol must equal snapshot symbol")
            if self.memory.auction.trading_day != self.snapshot_time.date():
                raise ValueError("memory.auction.trading_day must match snapshot_time")
            if self.memory.auction.last_snapshot_time != self.snapshot_time:
                raise ValueError("memory.auction.last_snapshot_time must equal snapshot_time")
            if self.auction.observation is None or self.auction.lifecycle is None:
                raise ValueError("Validated Auction snapshot requires observation/lifecycle")
            if self.auction.observation.symbol != self.symbol.strip().upper():
                raise ValueError("auction.observation.symbol must equal snapshot symbol")
            if self.auction.lifecycle.snapshot_time != self.snapshot_time:
                raise ValueError("auction.lifecycle.snapshot_time must equal snapshot_time")
        if self.ltp is not None and (not math.isfinite(float(self.ltp)) or self.ltp <= 0):
            raise ValueError("snapshot.ltp must be positive when present")
        return self

    def to_db_dict(self) -> Dict[str, Any]:
        raw = self.model_dump(
            mode="python",
            by_alias=True,
            exclude={"ltp", "ltp_time"},
        )
        return sanitize_json(raw)

    @staticmethod
    def from_db_dict(dump: Dict[str, Any]) -> "SnapshotSchema":
        if not isinstance(dump, dict) or not dump:
            raise ValueError("Empty snapshot dump")
        return SnapshotSchema.model_validate(dump)

    @staticmethod
    def create_snapshot(snapshot: "SnapshotSchema") -> "SnapshotSchema":
        """Create or update a snapshot row idempotently.

        Replay/backfill runs often regenerate the same (symbol, snapshot_time).
        Do not rely only on a database IntegrityError because older deployed
        tables may not have the intended composite primary/unique key.  Check
        first, update the existing row when present, and only insert when no row
        exists.
        """
        raw = snapshot.to_db_dict()
        if snapshot.ltp is None or snapshot.ltp_time is None:
            raise ValueError("snapshot.ltp and snapshot.ltp_time are required for persistence")
        ltp_val = snapshot.ltp
        ltp_time_val = snapshot.ltp_time

        with get_trades_db() as db:
            existing = (
                db.query(SnapshotORM)
                .filter(SnapshotORM.symbol == snapshot.symbol)
                .filter(SnapshotORM.snapshot_time == snapshot.snapshot_time)
                .first()
            )

            if existing:
                logger.debug(
                    "Snapshot[%s @ %s] exists; updating current schema payload",
                    snapshot.symbol,
                    snapshot.snapshot_time,
                )
                existing.ltp = ltp_val
                existing.ltp_time = ltp_time_val
                existing.data = raw
                existing.processed = False
                db.commit()
                return snapshot

            orm = SnapshotORM(
                symbol=snapshot.symbol,
                snapshot_time=snapshot.snapshot_time,
                ltp=ltp_val,
                ltp_time=ltp_time_val,
                data=raw,
                processed=False,
            )
            db.add(orm)

            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                logger.warning(
                    "Snapshot[%s @ %s] hit unique constraint during insert; updating existing row",
                    snapshot.symbol,
                    snapshot.snapshot_time,
                )
                existing = (
                    db.query(SnapshotORM)
                    .filter(SnapshotORM.symbol == snapshot.symbol)
                    .filter(SnapshotORM.snapshot_time == snapshot.snapshot_time)
                    .first()
                )
                if existing:
                    existing.ltp = ltp_val
                    existing.ltp_time = ltp_time_val
                    existing.data = raw
                    existing.processed = False
                    db.commit()

        return snapshot

    @staticmethod
    def fetch_snapshot(symbol: str, snapshot_time: datetime) -> Optional["SnapshotSchema"]:
        # Use first() instead of one_or_none() so older tables that already
        # contain duplicate rows do not break replay. create_snapshot() is now
        # idempotent and truncating/cleaning snapshots removes the duplicates,
        # but fetch should remain defensive.
        with get_trades_db() as db:
            rec = (
                db.query(SnapshotORM)
                .filter(SnapshotORM.symbol == symbol)
                .filter(SnapshotORM.snapshot_time == snapshot_time)
                .order_by(SnapshotORM.snapshot_time.desc())
                .first()
            )
        return SnapshotSchema.from_db_dict(rec.data) if rec and rec.data else None

    @staticmethod
    def update_snapshot(
        symbol: str,
        snapshot_time: datetime,
        update_data: Dict[str, Any],
    ) -> Optional["SnapshotSchema"]:
        with get_trades_db() as db:
            rec = (
                db.query(SnapshotORM)
                .filter(SnapshotORM.symbol == symbol)
                .filter(SnapshotORM.snapshot_time == snapshot_time)
                .first()
            )
            if not rec:
                return None

            if not isinstance(rec.data, dict):
                raise ValueError("Existing snapshot payload must be an object")
            if not isinstance(update_data, dict):
                raise TypeError("Snapshot update_data must be an object")
            merged = dict(rec.data)
            merged.update(update_data)
            validated = SnapshotSchema.from_db_dict(merged)
            rec.data = validated.to_db_dict()
            db.commit()
            db.refresh(rec)
            data = rec.data

        return SnapshotSchema.from_db_dict(data) if data else None

    @staticmethod
    def delete_snapshot(symbol: str, snapshot_time: datetime) -> bool:
        with get_trades_db() as db:
            rows = (
                db.query(SnapshotORM)
                .filter(SnapshotORM.symbol == symbol)
                .filter(SnapshotORM.snapshot_time == snapshot_time)
                .all()
            )
            if not rows:
                return False

            for rec in rows:
                db.delete(rec)
            db.commit()

        return True

    @staticmethod
    def fetch_latest_for_symbol(symbol: str) -> Optional["SnapshotSchema"]:
        with get_trades_db() as db:
            rec = (
                db.query(SnapshotORM)
                .filter(SnapshotORM.symbol == symbol)
                .order_by(SnapshotORM.snapshot_time.desc())
                .first()
            )
        return SnapshotSchema.from_db_dict(rec.data) if rec and rec.data else None

    @staticmethod
    def fetch_latest_for_symbol_asof(symbol: str, asof_time: datetime) -> Optional["SnapshotSchema"]:
        with get_trades_db() as db:
            rec = (
                db.query(SnapshotORM)
                .filter(SnapshotORM.symbol == symbol)
                .filter(SnapshotORM.snapshot_time <= asof_time)
                .order_by(SnapshotORM.snapshot_time.desc())
                .first()
            )
        return SnapshotSchema.from_db_dict(rec.data) if rec and rec.data else None

    @staticmethod
    def fetch_latest_today_payload_before_time(
        symbol: str,
        ts: datetime,
    ) -> Optional[Dict[str, Any]]:
        """Return the raw previous snapshot payload for same-day continuity.

        This intentionally returns the JSON payload instead of validating through
        SnapshotSchema so older rows that do not yet contain state_memory can be
        inspected and rejected by the caller without raising.
        """
        if ts.tzinfo is None:
            start_of_day = ts.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            start_of_day = ts.astimezone(ts.tzinfo).replace(
                hour=0, minute=0, second=0, microsecond=0
            )

        with get_trades_db() as db:
            rec = (
                db.query(SnapshotORM)
                .filter(SnapshotORM.symbol == symbol)
                .filter(SnapshotORM.snapshot_time >= start_of_day)
                .filter(SnapshotORM.snapshot_time < ts)
                .order_by(SnapshotORM.snapshot_time.desc())
                .first()
            )

        if not rec or not rec.data:
            return None

        return rec.data if isinstance(rec.data, dict) else None

    @staticmethod
    def fetch_latest_ltp(symbol: str) -> Optional[Dict[str, Any]]:
        with get_trades_db() as db:
            rec = (
                db.query(SnapshotORM)
                .filter(SnapshotORM.symbol == symbol)
                .order_by(SnapshotORM.snapshot_time.desc())
                .first()
            )

            if not rec:
                return None

            ltp_val = None
            try:
                if getattr(rec, "ltp", None) is not None:
                    ltp_val = float(rec.ltp)
            except Exception:
                ltp_val = None

            return {
                "symbol": rec.symbol,
                "snapshot_time": rec.snapshot_time,
                "ltp": ltp_val,
                "ltp_time": getattr(rec, "ltp_time", None),
            }

    @staticmethod
    def fetch_symbols_for_day(trading_day: date) -> List[str]:
        day_start = datetime.combine(trading_day, dtime.min)
        day_end = day_start + timedelta(days=1)
        with get_trades_db() as db:
            rows = (
                db.query(SnapshotORM.symbol)
                .filter(SnapshotORM.snapshot_time >= day_start)
                .filter(SnapshotORM.snapshot_time < day_end)
                .distinct()
                .order_by(SnapshotORM.symbol.asc())
                .all()
            )
        return [str(symbol).strip().upper() for (symbol,) in rows if str(symbol).strip()]

    @staticmethod
    def fetch_day_replay_batch(
        *,
        trading_day: date,
        after_time: Optional[datetime] = None,
        after_symbol: str = "",
        symbols: Optional[List[str]] = None,
        limit: int = 500,
    ) -> List["SnapshotSchema"]:
        """Load one chronological historical batch without using processed."""
        day_start = datetime.combine(trading_day, dtime.min)
        day_end = day_start + timedelta(days=1)
        with get_trades_db() as db:
            query = (
                db.query(
                    SnapshotORM.symbol.label("symbol"),
                    SnapshotORM.snapshot_time.label("snapshot_time"),
                    SnapshotORM.ltp.label("ltp"),
                    SnapshotORM.ltp_time.label("ltp_time"),
                    cast(SnapshotORM.data, Text).label("data_text"),
                )
                .filter(SnapshotORM.snapshot_time >= day_start)
                .filter(SnapshotORM.snapshot_time < day_end)
            )
            if symbols:
                query = query.filter(SnapshotORM.symbol.in_([
                    str(symbol).strip().upper() for symbol in symbols
                ]))
            if after_time is not None:
                query = query.filter(or_(
                    SnapshotORM.snapshot_time > after_time,
                    and_(
                        SnapshotORM.snapshot_time == after_time,
                        SnapshotORM.symbol > str(after_symbol or ""),
                    ),
                ))
            rows = (
                query.order_by(
                    SnapshotORM.snapshot_time.asc(),
                    SnapshotORM.symbol.asc(),
                )
                .limit(max(1, int(limit)))
                .all()
            )

        snapshots: List[SnapshotSchema] = []
        for row in rows:
            try:
                raw = row.data_text
                payload = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
                if payload["symbol"] != str(row.symbol).strip().upper():
                    raise ValueError("Snapshot JSON symbol differs from DB symbol")
                payload_time = datetime.fromisoformat(payload["snapshot_time"]) if isinstance(payload["snapshot_time"], str) else payload["snapshot_time"]
                if payload_time.replace(tzinfo=None) != row.snapshot_time.replace(tzinfo=None):
                    raise ValueError("Snapshot JSON time differs from DB snapshot_time")
                payload["ltp"] = float(row.ltp) if row.ltp is not None else None
                payload["ltp_time"] = row.ltp_time
                snapshots.append(SnapshotSchema.from_db_dict(payload))
            except Exception as exc:
                raise ValueError(
                    f"Invalid snapshot payload for {row.symbol} @ {row.snapshot_time}"
                ) from exc
        return snapshots

    @staticmethod
    def fetch_unprocessed_day_batch(
        *,
        trading_day: date,
        after_time: Optional[datetime] = None,
        after_symbol: str = "",
        symbols: Optional[List[str]] = None,
        until_time: Optional[datetime] = None,
        limit: int = 500,
    ) -> List["SnapshotSchema"]:
        """Load one chronological batch of unprocessed snapshots for a day.

        This is the production/restart replay queue contract: processed rows are
        skipped, ordering is timestamp then symbol, and the cursor remains stable
        while the caller acknowledges rows after successful Auction processing.
        """
        day_start = datetime.combine(trading_day, dtime.min)
        day_end = day_start + timedelta(days=1)
        with get_trades_db() as db:
            query = (
                db.query(
                    SnapshotORM.symbol.label("symbol"),
                    SnapshotORM.snapshot_time.label("snapshot_time"),
                    SnapshotORM.ltp.label("ltp"),
                    SnapshotORM.ltp_time.label("ltp_time"),
                    cast(SnapshotORM.data, Text).label("data_text"),
                )
                .filter(SnapshotORM.processed == False)  # noqa: E712
                .filter(SnapshotORM.snapshot_time >= day_start)
                .filter(SnapshotORM.snapshot_time < day_end)
            )
            if symbols:
                query = query.filter(SnapshotORM.symbol.in_([
                    str(symbol).strip().upper() for symbol in symbols
                ]))
            if until_time is not None:
                query = query.filter(SnapshotORM.snapshot_time <= until_time)
            if after_time is not None:
                query = query.filter(or_(
                    SnapshotORM.snapshot_time > after_time,
                    and_(
                        SnapshotORM.snapshot_time == after_time,
                        SnapshotORM.symbol > str(after_symbol or ""),
                    ),
                ))
            rows = (
                query.order_by(
                    SnapshotORM.snapshot_time.asc(),
                    SnapshotORM.symbol.asc(),
                )
                .limit(max(1, int(limit)))
                .all()
            )

        snapshots: List[SnapshotSchema] = []
        for row in rows:
            try:
                raw = row.data_text
                payload = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
                if payload["symbol"] != str(row.symbol).strip().upper():
                    raise ValueError("Snapshot JSON symbol differs from DB symbol")
                payload_time = datetime.fromisoformat(payload["snapshot_time"]) if isinstance(payload["snapshot_time"], str) else payload["snapshot_time"]
                if payload_time.replace(tzinfo=None) != row.snapshot_time.replace(tzinfo=None):
                    raise ValueError("Snapshot JSON time differs from DB snapshot_time")
                payload["ltp"] = float(row.ltp) if row.ltp is not None else None
                payload["ltp_time"] = row.ltp_time
                snapshots.append(SnapshotSchema.from_db_dict(payload))
            except Exception as exc:
                raise ValueError(
                    f"Invalid unprocessed snapshot payload for {row.symbol} @ {row.snapshot_time}"
                ) from exc
        return snapshots

    @staticmethod
    def fetch_unprocessed(limit: Optional[int] = None) -> List["SnapshotSchema"]:
        with get_trades_db() as db:
            query = (
                db.query(SnapshotORM)
                .filter(SnapshotORM.processed == False)
                .order_by(
                    SnapshotORM.snapshot_time.asc(),
                    SnapshotORM.symbol.asc(),
                )
            )
            if limit is not None:
                query = query.limit(max(1, int(limit)))
            rows = query.all()

        out: List[SnapshotSchema] = []

        for r in rows:
            try:
                if r.data:
                    out.append(SnapshotSchema.from_db_dict(r.data))
            except Exception:
                logger.exception("Failed to load snapshot row: %s", getattr(r, "id", None))

        return out

    @staticmethod
    def fetch_unprocessed_keys_asof(
        snapshot_time: datetime,
        symbols: Optional[List[str]] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: Optional[int] = None,
    ) -> List[Tuple[str, datetime]]:
        """Return unprocessed snapshot keys available by as-of time in replay order.

        Snapshot replay must be chronological across symbols.  Ordering by
        symbol first causes one symbol to advance through the day while another
        symbol is still at the same earlier timestamp, which is not how the
        live pipeline sees snapshots.
        """
        with get_trades_db() as db:
            q = db.query(SnapshotORM.symbol, SnapshotORM.snapshot_time).filter(
                SnapshotORM.processed == False,  # noqa: E712
                SnapshotORM.snapshot_time <= snapshot_time,
            )
            if symbols:
                q = q.filter(SnapshotORM.symbol.in_(symbols))
            if start_time is not None:
                q = q.filter(SnapshotORM.snapshot_time >= start_time)
            if end_time is not None:
                q = q.filter(SnapshotORM.snapshot_time <= end_time)
            q = q.order_by(SnapshotORM.snapshot_time.asc(), SnapshotORM.symbol.asc())
            if limit is not None:
                q = q.limit(max(0, int(limit)))
            rows = q.all()

        return [(str(symbol), snapshot_time) for symbol, snapshot_time in rows]

    @staticmethod
    def mark_processed(symbol: str, snapshot_time: datetime) -> bool:
        try:
            with get_trades_db() as db:
                rows = (
                    db.query(SnapshotORM)
                    .filter(SnapshotORM.symbol == symbol)
                    .filter(SnapshotORM.snapshot_time == snapshot_time)
                    .all()
                )
                if not rows:
                    return False

                for rec in rows:
                    rec.processed = True
                db.commit()

            return True
        except Exception:
            logger.exception("Error marking snapshot processed")
            return False

    @staticmethod
    def fetch_recent_today_for_symbol_before_time(
        symbol: str,
        ts: datetime,
        limit: int = 10,
        ascending: bool = True,
    ) -> List["SnapshotSchema"]:
        start_of_day = ts.replace(hour=0, minute=0, second=0, microsecond=0)

        with get_trades_db() as db:
            rows = (
                db.query(SnapshotORM)
                .filter(SnapshotORM.symbol == symbol)
                .filter(SnapshotORM.snapshot_time >= start_of_day)
                .filter(SnapshotORM.snapshot_time <= ts)
                .order_by(SnapshotORM.snapshot_time.desc())
                .limit(int(limit))
                .all()
            )

        if ascending:
            rows = list(reversed(rows))

        out: List[SnapshotSchema] = []

        for r in rows:
            try:
                if r.data:
                    out.append(SnapshotSchema.from_db_dict(r.data))
            except Exception:
                logger.exception("Failed to load snapshot row: %s", getattr(r, "id", None))

        return out

    @staticmethod
    def update_latest_ltp_if_newer(
        symbol: str,
        ltp: float,
        ltp_time: datetime,
    ) -> bool:
        try:
            with get_trades_db() as db:
                rec = (
                    db.query(SnapshotORM)
                    .filter(SnapshotORM.symbol == symbol)
                    .order_by(SnapshotORM.snapshot_time.desc())
                    .first()
                )

                if not rec:
                    return False

                if ltp_time <= rec.snapshot_time:
                    return False

                rec.ltp = ltp
                rec.ltp_time = ltp_time
                db.commit()

                return True
        except Exception:
            logger.exception("Error updating latest ltp for %s", symbol)
            return False
