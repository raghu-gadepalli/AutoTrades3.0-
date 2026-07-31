"""Persistence contract for diagnostic stock movement rankings."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from database.database import get_trades_db
from models.trade_models import StockRank as StockRankORM
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
        if len(rank_times) != 1:
            raise ValueError("StockRank bulk upsert requires one common rank_time")
        symbols = [row.symbol for row in payloads]
        if len(symbols) != len(set(symbols)):
            raise ValueError("StockRank bulk upsert contains duplicate symbols")

        with get_trades_db() as db:
            for row in payloads:
                rec = (
                    db.query(StockRankORM)
                    .filter(StockRankORM.symbol == row.symbol)
                    .filter(StockRankORM.rank_time == row.rank_time)
                    .one_or_none()
                )
                values = row.orm_values()
                if rec is None:
                    db.add(StockRankORM(**values))
                else:
                    for name, value in values.items():
                        setattr(rec, name, value)
            db.commit()

            persisted = (
                db.query(StockRankORM)
                .filter(StockRankORM.rank_time == payloads[0].rank_time)
                .filter(StockRankORM.symbol.in_(symbols))
                .order_by(StockRankORM.rank_position.asc())
                .all()
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

    def report_row(self) -> Dict[str, Any]:
        row = self.model_dump(exclude={"id", "created_at", "updated_at"})
        row["metrics_json"] = sanitize_json(row["metrics_json"])
        return row


__all__ = ["StockRankSchema"]
