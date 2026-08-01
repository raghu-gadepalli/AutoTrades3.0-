"""Persistence contract for diagnostic stock movement rankings."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import and_

from database.database import get_trades_db
from models.trade_models import (
    StockRank as StockRankORM,
    StockRankHistory as StockRankHistoryORM,
)
from utils.datetime_utils import to_ist_naive
from utils.json_utils import sanitize_json


class StockRankSchema(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        extra="forbid",
        validate_assignment=True,
    )

    id: Optional[int] = None

    run_id: str = Field(min_length=1, max_length=64)
    trading_day: date
    rank_time: datetime
    symbol: str = Field(min_length=1, max_length=32)
    rank_position: int = Field(ge=1)
    universe_size: int = Field(ge=1)

    direction: str
    classification: str
    attention_tier: Literal["PRIORITY", "SECONDARY", "SUPPRESSED"]

    total_score: float = Field(ge=0.0, le=100.0)
    movement_score: float = Field(ge=0.0, le=100.0)
    quality_score: float = Field(ge=0.0, le=100.0)
    range_penalty: float = Field(ge=0.0, le=100.0)
    stall_penalty: float = Field(ge=0.0, le=100.0)

    close_price: float = Field(gt=0.0)
    previous_close: Optional[float] = Field(default=None, gt=0.0)
    today_open: Optional[float] = Field(default=None, gt=0.0)
    gap_pct: Optional[float] = None
    session_move_pct: Optional[float] = None
    post_open_move_pct: Optional[float] = None

    move_15m_pct: Optional[float] = None
    move_30m_pct: Optional[float] = None
    move_60m_pct: Optional[float] = None
    move_15m_atr: Optional[float] = None
    move_30m_atr: Optional[float] = None
    move_60m_atr: Optional[float] = None

    atr_value: float = Field(gt=0.0)
    atr_pct: Optional[float] = None
    directional_efficiency: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    recent_efficiency: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    direction_consistency: float = Field(ge=0.0, le=1.0)
    acceleration_score: float = Field(ge=0.0, le=1.0)
    volume_ratio: Optional[float] = Field(default=None, ge=0.0)
    freshness_score: float = Field(ge=0.0, le=1.0)
    bars_since_extreme: int = Field(ge=0)

    range_active: bool
    range_episode_id: Optional[str] = None
    range_id: Optional[str] = None
    range_age_bars: int = Field(ge=0)
    range_width_pct: Optional[float] = Field(default=None, ge=0.0)
    containment_ratio: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    midpoint_crossings: int = Field(ge=0)
    vwap_crossings: int = Field(ge=0)
    failed_escape_count: int = Field(ge=0)
    rearm_required: bool
    attempt_limit_reached: bool

    metrics_json: Dict[str, Any]

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @field_validator("symbol", "direction", "classification")
    @classmethod
    def normalise_text(cls, value: str) -> str:
        clean = str(value or "").strip().upper()
        if not clean:
            raise ValueError("StockRank text fields cannot be empty")
        return clean

    @model_validator(mode="after")
    def validate_identity(self) -> "StockRankSchema":
        if self.rank_position > self.universe_size:
            raise ValueError("rank_position cannot exceed universe_size")
        if self.rank_time.date() != self.trading_day:
            raise ValueError("rank_time must belong to trading_day")
        return self

    def orm_values(self) -> Dict[str, Any]:
        values = self.model_dump(exclude={"id", "created_at", "updated_at"})
        values["metrics_json"] = sanitize_json(values["metrics_json"])
        return values

    @staticmethod
    def upsert_many(rows: Iterable["StockRankSchema"]) -> List["StockRankSchema"]:
        payloads = [
            row if isinstance(row, StockRankSchema) else StockRankSchema.model_validate(row)
            for row in rows
        ]
        if not payloads:
            return []

        rank_times = {row.rank_time for row in payloads}
        run_ids = {row.run_id for row in payloads}
        trading_days = {row.trading_day for row in payloads}
        universe_sizes = {row.universe_size for row in payloads}
        if len(rank_times) != 1:
            raise ValueError("StockRank bulk upsert requires one common rank_time")
        if len(run_ids) != 1 or len(trading_days) != 1 or len(universe_sizes) != 1:
            raise ValueError("StockRank bulk upsert requires one coherent run")
        symbols = [row.symbol for row in payloads]
        positions = [row.rank_position for row in payloads]
        if len(symbols) != len(set(symbols)):
            raise ValueError("StockRank bulk upsert contains duplicate symbols")
        if sorted(positions) != list(range(1, len(payloads) + 1)):
            raise ValueError("StockRank bulk upsert requires contiguous rank positions")
        if next(iter(universe_sizes)) != len(payloads):
            raise ValueError("StockRank universe_size must match payload count")

        rank_time = payloads[0].rank_time
        with get_trades_db() as db:
            existing_rows = (
                db.query(StockRankORM)
                .filter(StockRankORM.rank_time == rank_time)
                .all()
            )
            existing_by_symbol = {row.symbol: row for row in existing_rows}
            for stale in existing_rows:
                if stale.symbol not in symbols:
                    db.delete(stale)
            for row in payloads:
                rec = existing_by_symbol.get(row.symbol)
                values = row.orm_values()
                if rec is None:
                    db.add(StockRankORM(**values))
                else:
                    for name, value in values.items():
                        setattr(rec, name, value)
            db.commit()

            persisted = (
                db.query(StockRankORM)
                .filter(StockRankORM.rank_time == rank_time)
                .order_by(StockRankORM.rank_position.asc())
                .all()
            )
        if len(persisted) != len(payloads):
            raise RuntimeError(
                "StockRank persistence verification failed "
                f"expected={len(payloads)} actual={len(persisted)}"
            )
        return [StockRankSchema.model_validate(row) for row in persisted]

    @staticmethod
    def fetch_for_run(run_id: str) -> List["StockRankSchema"]:
        clean = str(run_id or "").strip()
        if not clean:
            raise ValueError("run_id is required")
        with get_trades_db() as db:
            rows = (
                db.query(StockRankORM)
                .filter(StockRankORM.run_id == clean)
                .order_by(StockRankORM.rank_position.asc())
                .all()
            )
        return [StockRankSchema.model_validate(row) for row in rows]

    @staticmethod
    def fetch_latest_rank_time(trading_day: date) -> Optional[datetime]:
        with get_trades_db() as db:
            value = (
                db.query(StockRankORM.rank_time)
                .filter(StockRankORM.trading_day == trading_day)
                .order_by(StockRankORM.rank_time.desc())
                .limit(1)
                .scalar()
            )
        return value

    @staticmethod
    def fetch_for_time(rank_time: datetime) -> List["StockRankSchema"]:
        with get_trades_db() as db:
            rows = (
                db.query(StockRankORM)
                .filter(StockRankORM.rank_time == rank_time)
                .order_by(StockRankORM.rank_position.asc())
                .all()
            )
        return [StockRankSchema.model_validate(row) for row in rows]

    @staticmethod
    def fetch_latest_for_symbol_at_or_before(
        *,
        symbol: str,
        through_time: datetime,
    ) -> Optional["StockRankSchema"]:
        """Return one causal current-day rank row for Advisor context."""
        clean_symbol = str(symbol or "").strip().upper()
        as_of = to_ist_naive(through_time)
        if not clean_symbol:
            raise ValueError("StockRank causal lookup requires symbol")
        if as_of is None:
            raise ValueError("StockRank causal lookup requires valid through_time")
        with get_trades_db() as db:
            row = (
                db.query(StockRankORM)
                .filter(StockRankORM.symbol == clean_symbol)
                .filter(StockRankORM.trading_day == as_of.date())
                .filter(StockRankORM.rank_time <= as_of)
                .order_by(StockRankORM.rank_time.desc())
                .limit(1)
                .one_or_none()
            )
        return StockRankSchema.model_validate(row) if row is not None else None

    def report_row(self) -> Dict[str, Any]:
        row = self.model_dump(exclude={"id", "created_at", "updated_at"})
        row["metrics_json"] = sanitize_json(row["metrics_json"])
        return row


    @staticmethod
    def archive_current_rows():
        """Archive all current ranks and verify symbol/cadence identity."""
        from schemas.archive import ArchiveSpec, archive_rows

        return archive_rows(
            ArchiveSpec(
                name="stock_rank",
                source_model=StockRankORM,
                history_model=StockRankHistoryORM,
                target_to_source={"stock_rank_id": "id"},
                excluded_target_columns=frozenset(
                    {"history_id", "archived_on"}
                ),
                verification_condition=lambda source, target: and_(
                    target.c.symbol == source.c.symbol,
                    target.c.rank_time == source.c.rank_time,
                ),
            )
        )



__all__ = ["StockRankSchema"]
