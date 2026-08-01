# schemas/signal.py
#
# Signal lifecycle schema
# - Table is still named signals for now
# - Conceptually each row represents an signal instance
# - stage  = LifecycleStage
# - status = OPEN / CLOSED / INVALIDATED / EXPIRED / REPLACED / BLOCKED / CANCELLED

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, func, or_

from database.database import get_trades_db
from enums.enums import SignalSide, LifecycleStage, SignalStatus
from models.trade_models import (
    Signal as SignalORM,
    SignalHistory as SignalHistoryORM,
)
from utils.json_utils import sanitize_json
from utils.datetime_utils import IST

logger = logging.getLogger(__name__)


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def _now_ist() -> datetime:
    """Naive IST timestamp for DB/UI consistency."""
    return datetime.now(IST).replace(tzinfo=None)


def _to_ist_naive(ts: Optional[datetime]) -> Optional[datetime]:
    """Normalize any datetime to naive IST."""
    if ts is None or not isinstance(ts, datetime):
        return None
    try:
        if getattr(ts, "tzinfo", None) is not None:
            ts = ts.astimezone(IST)
        return ts.replace(tzinfo=None)
    except Exception:
        return ts.replace(tzinfo=None)


def _require_ist_naive_datetime(ts: Optional[datetime], *, source: str) -> datetime:
    out = _to_ist_naive(ts)
    if out is None:
        raise ValueError(f"Missing required datetime from {source}")
    return out


def _dec_or_none(x: Any) -> Optional[Decimal]:
    if x is None:
        return None
    try:
        return Decimal(str(x))
    except Exception:
        return None


def _dec_or_zero(x: Any) -> Decimal:
    v = _dec_or_none(x)
    return v if v is not None else Decimal("0")


def _enum_to_str(x: Any) -> str:
    return x.value if hasattr(x, "value") else str(x)


def _fmt_dt(ts: Any) -> str:
    if not isinstance(ts, datetime):
        return "N/A"
    try:
        return ts.strftime("%d-%m-%Y %H:%M")
    except Exception:
        return "N/A"


def _age_minutes(first_seen: Any, last_eval: Any) -> Optional[float]:
    if not isinstance(first_seen, datetime) or not isinstance(last_eval, datetime):
        return None
    try:
        if first_seen.tzinfo and not last_eval.tzinfo:
            last_eval = last_eval.replace(tzinfo=first_seen.tzinfo)
        if last_eval.tzinfo and not first_seen.tzinfo:
            first_seen = first_seen.replace(tzinfo=last_eval.tzinfo)
        return round((last_eval - first_seen).total_seconds() / 60.0, 1)
    except Exception:
        return None


def _num(x: Any, nd: int = 4) -> Optional[float]:
    try:
        if x is None:
            return None
        return round(float(x), nd)
    except Exception:
        return None


def _get_path(d: Any, path: str) -> Any:
    if not isinstance(d, dict) or not path:
        return None
    cur: Any = d
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur




DOWNSTREAM_CONTRACT_VERSION = "AUCTION_SIGNAL_DOWNSTREAM_V2"


def _require_setup_label(value: Any, *, source: str) -> str:
    """Return a normalized setup label, or fail loudly."""
    label = str(value or "").strip().upper()
    if not label:
        raise ValueError(f"Missing required signal setup from {source}")
    return label


def _require_meta_block(meta_json: Any, key: str, *, source: str) -> Dict[str, Any]:
    if not isinstance(meta_json, dict):
        raise ValueError(f"{source}.meta_json must be an object")
    if key not in meta_json:
        raise ValueError(f"{source}.meta_json.{key} is required")
    block = meta_json[key]
    if not isinstance(block, dict):
        raise ValueError(f"{source}.meta_json.{key} must be an object")
    return block


def _assert_setup_identity(
    *,
    persisted_setup: Any,
    requested_setup: Any = None,
    meta_json: Any = None,
    source: str,
) -> str:
    """Validate immutable setup identity through the strict Auction V2 contract."""
    persisted = _require_setup_label(persisted_setup, source=f"{source}.persisted_setup")
    requested = (
        _require_setup_label(requested_setup, source=f"{source}.requested_setup")
        if requested_setup is not None
        else persisted
    )
    if requested != persisted:
        raise ValueError(
            f"SIGNAL_SETUP_IMMUTABLE_MISMATCH source={source} "
            f"persisted={persisted} requested={requested}"
        )
    if meta_json is None:
        return persisted

    contract = _require_meta_block(meta_json, "downstream_contract", source=source)
    if "version" not in contract:
        raise ValueError(f"{source}.meta_json.downstream_contract.version is required")
    if str(contract["version"]).strip() != DOWNSTREAM_CONTRACT_VERSION:
        raise ValueError(
            f"{source} requires {DOWNSTREAM_CONTRACT_VERSION}; "
            f"actual={contract['version']}"
        )
    setup_levels = _require_meta_block(meta_json, "setup_levels", source=source)
    auction_signal = _require_meta_block(meta_json, "auction_signal", source=source)
    for path, block, key in (
        ("setup_levels.setup_label", setup_levels, "setup_label"),
        ("auction_signal.setup_family", auction_signal, "setup_family"),
    ):
        if key not in block:
            raise ValueError(f"{source}.meta_json.{path} is required")
        label = _require_setup_label(block[key], source=f"{source}.meta_json.{path}")
        if label != persisted:
            raise ValueError(
                f"SIGNAL_SETUP_METADATA_MISMATCH source={source} "
                f"persisted={persisted} metadata={label} path={path}"
            )
    return persisted


def _is_actionable_stage(stage_value: str) -> bool:
    return stage_value in (
        LifecycleStage.ACTIVE.value,
        LifecycleStage.EXPAND.value,
    )


def _is_qualified_stage(stage_value: str) -> bool:
    return stage_value in (
        LifecycleStage.BUILDING.value,
        LifecycleStage.ACTIVE.value,
        LifecycleStage.EXPAND.value,
        LifecycleStage.PROTECT.value,
    )


# -------------------------------------------------------------------
# Lightweight read model used only by narrow signal queries.
# It intentionally excludes large JSON payloads so one day-context lookup
# cannot trigger a MySQL filesort over wide signal rows.
# -------------------------------------------------------------------
class SignalActiveContextRow(BaseModel):
    model_config = {"from_attributes": True}

    id: Optional[int] = None
    signal_id: str
    equity_ref: str
    symbol: str
    lifecycle: str
    setup: str
    side: SignalSide
    stage: LifecycleStage
    status: SignalStatus
    first_seen_time: Optional[datetime] = None
    qualified_time: Optional[datetime] = None
    actionable_time: Optional[datetime] = None
    last_snapshot_time: datetime
    closed_time: Optional[datetime] = None


# -------------------------------------------------------------------
# Schema
# -------------------------------------------------------------------
class SignalSchema(BaseModel):
    """
    Persisted lifecycle-driven signal instance.

    Notes:
    - Table remains signals for now.
    - Each row is an signal instance.
    - Only one OPEN row per (equity_ref, lifecycle) should exist at a time.
    - This uniqueness is enforced in code, not DB.
    """

    model_config = {"from_attributes": True, "extra": "allow"}

    # -----------------------------
    # Identity / grouping
    # -----------------------------
    id: Optional[int] = None
    signal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))

    equity_ref: str
    symbol: str
    lifecycle: str
    # Immutable originating setup.  Mutable current/opposite evidence remains
    # in meta_json and must not change this field.
    setup: str
    side: SignalSide

    # -----------------------------
    # Lifecycle
    # -----------------------------
    stage: LifecycleStage = LifecycleStage.DISCOVERY
    status: SignalStatus = SignalStatus.OPEN
    status_reason: Optional[str] = None

    # -----------------------------
    # Timing
    # -----------------------------
    first_seen_time: Optional[datetime] = None
    created_price: Optional[Decimal] = None

    last_eval_time: datetime
    last_snapshot_time: datetime

    stage_changed_time: Optional[datetime] = None
    status_changed_time: Optional[datetime] = None

    qualified_time: Optional[datetime] = None
    actionable_time: Optional[datetime] = None

    closed_time: Optional[datetime] = None
    closed_price: Optional[Decimal] = None

    # -----------------------------
    # Prices
    # -----------------------------
    last_price: Optional[Decimal] = None
    ltp: Optional[Decimal] = None
    ltp_time: Optional[datetime] = None

    # -----------------------------
    # Signal analytics / excursion
    # -----------------------------
    last_pnl: Decimal = Decimal("0")
    last_pnl_value: Decimal = Decimal("0")

    max_price: Decimal = Decimal("0")
    min_price: Decimal = Decimal("0")

    max_time: Optional[datetime] = None
    min_time: Optional[datetime] = None

    max_pnl: Decimal = Decimal("0")
    min_pnl: Decimal = Decimal("0")

    max_pnl_value: Decimal = Decimal("0")
    min_pnl_value: Decimal = Decimal("0")

    # -----------------------------
    # Payloads
    # -----------------------------
    criteria_json: Dict[str, Any] = Field(default_factory=dict)
    snapshot_json: Dict[str, Any] = Field(default_factory=dict)
    meta_json: Optional[Dict[str, Any]] = None

    # -----------------------------
    # Validators
    # -----------------------------
    @field_validator("equity_ref", "symbol", "lifecycle", mode="before")
    @classmethod
    def _strip_strings(cls, v):
        if v is None:
            return v
        return str(v).strip()

    @field_validator("setup", mode="before")
    @classmethod
    def _require_setup(cls, v):
        return _require_setup_label(v, source="SignalSchema.setup")

    @field_validator("criteria_json", "snapshot_json", mode="before")
    @classmethod
    def _ensure_dict(cls, v):
        if v is None:
            return {}
        return v if isinstance(v, dict) else {}

    @field_validator("meta_json", mode="before")
    @classmethod
    def _ensure_meta(cls, v):
        if v is None:
            return None
        return v if isinstance(v, dict) else None

    # -----------------------------
    # UI projection
    # -----------------------------
    def to_ui_row(self, registry: Any = None) -> Dict[str, Any]:
        criteria = self.criteria_json or {}
        snap = self.snapshot_json or {}
        meta = self.meta_json or {}

        vars_ = criteria.get("vars") if isinstance(criteria.get("vars"), dict) else {}

        rsi = _num(vars_.get("rsi"), 2)
        if rsi is None:
            rsi = _num(_get_path(snap, "indicators.rsi.value"), 2)

        bb_zone = _get_path(snap, "indicators.bollinger.zone")
        vwap = _num(_get_path(snap, "indicators.vwap.value"), 2)

        reason = None
        if isinstance(meta, dict):
            reason = meta.get("reason")
        if not reason:
            reason = self.status_reason
        reason = str(reason).strip().lower() if reason is not None else None

        setup_label = _assert_setup_identity(
            persisted_setup=self.setup,
            meta_json=meta,
            source="SignalSchema.to_ui_row",
        )
        signal_block = _require_meta_block(meta, "signal", source="SignalSchema.to_ui_row")
        lifecycle_block = _require_meta_block(meta, "lifecycle", source="SignalSchema.to_ui_row")
        management = _require_meta_block(meta, "management", source="SignalSchema.to_ui_row")
        setup_levels = _require_meta_block(meta, "setup_levels", source="SignalSchema.to_ui_row")
        downstream_contract = _require_meta_block(
            meta,
            "downstream_contract",
            source="SignalSchema.to_ui_row",
        )
        version = str(downstream_contract["version"]).strip() if "version" in downstream_contract else ""
        if version != DOWNSTREAM_CONTRACT_VERSION:
            raise ValueError(
                "Signal UI requires current downstream contract: "
                f"expected={DOWNSTREAM_CONTRACT_VERSION} actual={version}"
            )

        management_posture = str(management["action"]).strip().upper()
        current_setup = str(management["setup_family"]).strip().upper()
        lifecycle_trade_action = str(lifecycle_block["trade_action"]).strip().upper()

        return {
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "equity_ref": self.equity_ref,
            "lifecycle": self.lifecycle,
            "setup": setup_label,
            "current_setup": current_setup,
            "setup_transitioned": current_setup != setup_label,
            "management_posture": management_posture,
            "management_reason_code": str(management["reason_code"]).strip().upper(),
            "lifecycle_reason": str(signal_block["signal_reason"]).strip(),
            "lifecycle_trade_action": lifecycle_trade_action,
            "should_exit_signal": bool(management["should_exit_signal"]),
            "auction_action": str(management["auction_action"]).strip().upper(),
            "auction_state": str(management["auction_state"]).strip().upper(),
            "directional_alignment": str(management["directional_alignment"]).strip().upper(),
            "downstream_contract_version": version,
            "opportunity_key": setup_levels["opportunity_key"],
            "candidate_id": setup_levels["candidate_id"],
            "boundary_event_key": setup_levels["boundary_event_key"],
            "setup_reference_price": _num(setup_levels["reference_price"], 2),
            "setup_reference_source": setup_levels["reference_source"],
            "confidence": None,
            "quality": None,
            "side": _enum_to_str(self.side).upper(),

            "stage": _enum_to_str(self.stage).upper(),
            "status": _enum_to_str(self.status).upper(),
            "reason": reason,

            "first_seen_time": _fmt_dt(self.first_seen_time),
            "created_price": _num(self.created_price, 2),

            "last_eval_time": _fmt_dt(self.last_eval_time),
            "last_snapshot_time": _fmt_dt(self.last_snapshot_time),
            "last_price": _num(self.last_price, 2),

            "last_pnl": _num(self.last_pnl, 2),
            "last_pnl_value": _num(self.last_pnl_value, 2),

            "max_price": _num(self.max_price, 2),
            "min_price": _num(self.min_price, 2),

            "max_pnl": _num(self.max_pnl, 2),
            "min_pnl": _num(self.min_pnl, 2),

            "max_pnl_value": _num(self.max_pnl_value, 2),
            "min_pnl_value": _num(self.min_pnl_value, 2),

            "max_time": _fmt_dt(self.max_time),
            "min_time": _fmt_dt(self.min_time),

            "qualified_time": _fmt_dt(self.qualified_time),
            "actionable_time": _fmt_dt(self.actionable_time),

            "closed_time": _fmt_dt(self.closed_time),
            "closed_price": _num(self.closed_price, 2),

            "vwap": vwap,
            "rsi": rsi,
            "bb_zone": bb_zone,

            "criteria": criteria,
            "meta": meta,
            "snapshot": snap,
        }

    # -----------------------------
    # DB conversion
    # -----------------------------
    def to_db_dict(self) -> Dict[str, Any]:
        d = self.model_dump(exclude_none=True)

        d["side"] = _enum_to_str(self.side)
        d["stage"] = _enum_to_str(self.stage)
        d["status"] = _enum_to_str(self.status)

        d["criteria_json"] = sanitize_json(d.get("criteria_json") or {})
        d["snapshot_json"] = sanitize_json(d.get("snapshot_json") or {})
        if "meta_json" in d:
            d["meta_json"] = sanitize_json(d.get("meta_json"))

        for fld in (
            "created_price",
            "closed_price",
            "last_price",
            "ltp",
            "last_pnl",
            "last_pnl_value",
            "max_price",
            "min_price",
            "max_pnl",
            "min_pnl",
            "max_pnl_value",
            "min_pnl_value",
        ):
            if fld in d and d[fld] is not None:
                d[fld] = float(_dec_or_none(d[fld]))

        return d

    # -----------------------------------------------------------------
    # Fetchers
    # -----------------------------------------------------------------
    @staticmethod
    def fetch_by_signal_id_strict(signal_id: str) -> Optional["SignalSchema"]:
        """Read one signal by identity and propagate database/validation failures."""
        with get_trades_db() as db:
            rec = db.query(SignalORM).filter(SignalORM.signal_id == signal_id).one_or_none()
        return SignalSchema.model_validate(rec) if rec is not None else None

    @staticmethod
    def fetch_by_signal_id(signal_id: str) -> Optional["SignalSchema"]:
        try:
            with get_trades_db() as db:
                rec = db.query(SignalORM).filter(SignalORM.signal_id == signal_id).one_or_none()
            return SignalSchema.model_validate(rec) if rec else None
        except Exception:
            logger.exception("fetch_by_signal_id failed | signal_id=%s", signal_id)
            return None

    @staticmethod
    def fetch_active_signal_strict(equity_ref: str, lifecycle: str) -> Optional["SignalSchema"]:
        """Read the OPEN signal and propagate database/validation failures."""
        with get_trades_db() as db:
            rec = (
                db.query(SignalORM)
                .filter(
                    SignalORM.equity_ref == equity_ref,
                    SignalORM.lifecycle == lifecycle,
                    SignalORM.status == SignalStatus.OPEN.value,
                )
                .order_by(SignalORM.last_eval_time.desc(), SignalORM.id.desc())
                .first()
            )
        return SignalSchema.model_validate(rec) if rec is not None else None

    @staticmethod
    def fetch_active_signal(equity_ref: str, lifecycle: str) -> Optional["SignalSchema"]:
        """
        Load the currently OPEN signal for (equity_ref, lifecycle), if any.
        """
        try:
            with get_trades_db() as db:
                rec = (
                    db.query(SignalORM)
                    .filter(
                        SignalORM.equity_ref == equity_ref,
                        SignalORM.lifecycle == lifecycle,
                        SignalORM.status == SignalStatus.OPEN.value,
                    )
                    .order_by(SignalORM.last_eval_time.desc(), SignalORM.id.desc())
                    .first()
                )
            return SignalSchema.model_validate(rec) if rec else None
        except Exception:
            logger.exception(
                "fetch_active_signal failed | equity_ref=%s lifecycle=%s",
                equity_ref,
                lifecycle,
            )
            return None


    @staticmethod
    def fetch_for_active_context_day(
        *,
        equity_ref: str,
        lifecycle: str,
        trading_day: date,
    ) -> List[SignalActiveContextRow]:
        """Load narrow signal projections whose lifetime can overlap one day.

        Narrow consumers need only identity, side, lifecycle and timing.
        Querying full ORM rows would also select large JSON payloads; combining
        that with SQL ORDER BY can exhaust MySQL's per-thread sort buffer on
        the VPS.  This projection deliberately performs no database sort.  The
        provider applies deterministic in-memory ordering after causal filtering.
        """
        equity_ref = str(equity_ref or "").strip().upper()
        lifecycle = str(lifecycle or "").strip().upper()
        if not equity_ref or not lifecycle:
            return []

        day_start = datetime.combine(trading_day, time.min)
        day_end = datetime.combine(trading_day + timedelta(days=1), time.min)
        start_time = func.coalesce(
            SignalORM.first_seen_time,
            SignalORM.actionable_time,
            SignalORM.qualified_time,
            SignalORM.last_snapshot_time,
        )

        try:
            with get_trades_db() as db:
                rows = (
                    db.query(
                        SignalORM.id,
                        SignalORM.signal_id,
                        SignalORM.equity_ref,
                        SignalORM.symbol,
                        SignalORM.lifecycle,
                        SignalORM.setup,
                        SignalORM.side,
                        SignalORM.stage,
                        SignalORM.status,
                        SignalORM.first_seen_time,
                        SignalORM.qualified_time,
                        SignalORM.actionable_time,
                        SignalORM.last_snapshot_time,
                        SignalORM.closed_time,
                    )
                    .filter(SignalORM.equity_ref == equity_ref)
                    .filter(SignalORM.lifecycle == lifecycle)
                    .filter(start_time < day_end)
                    .filter(
                        or_(
                            SignalORM.closed_time.is_(None),
                            SignalORM.closed_time > day_start,
                        )
                    )
                    .all()
                )
            return [
                SignalActiveContextRow.model_validate(dict(row._mapping))
                for row in rows
            ]
        except Exception:
            logger.exception(
                "fetch_for_active_context_day failed | equity_ref=%s lifecycle=%s day=%s",
                equity_ref, lifecycle, trading_day,
            )
            return []

    @staticmethod
    def fetch_latest_signal_snapshot_time() -> Optional[datetime]:
        """Return the latest signal snapshot timestamp in the live signals table."""
        try:
            with get_trades_db() as db:
                rec = (
                    db.query(SignalORM.last_snapshot_time)
                    .filter(SignalORM.last_snapshot_time.isnot(None))
                    .order_by(SignalORM.last_snapshot_time.desc(), SignalORM.id.desc())
                    .first()
                )
            return rec[0] if rec else None
        except Exception:
            logger.exception("fetch_latest_signal_snapshot_time failed")
            return None

    @staticmethod
    def fetch_for_signal_review(
        *,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        symbols: Optional[List[str]] = None,
        setups: Optional[List[str]] = None,
        limit: Optional[int] = None,
    ) -> List["SignalSchema"]:
        """Fetch signal rows for after-market signal review.

        This is intentionally DB-backed so review scripts do not depend on wide
        exported CSVs.  Filtering uses last_snapshot_time because that is the
        timestamp at which the signal was evaluated/created in replay.

        The first query fetches only ordered primary keys, then rows are loaded
        one at a time by id.  Avoid sorting the full signal rows because signal
        payload columns can be large enough to hit MySQL sort-buffer limits on
        the VPS during review/report scripts.
        """
        clean_symbols = sorted({str(x).strip().upper() for x in (symbols or []) if str(x).strip()})
        clean_setups = sorted({str(x).strip().upper() for x in (setups or []) if str(x).strip()})
        start_db = _to_ist_naive(start_time) if start_time else None
        end_db = _to_ist_naive(end_time) if end_time else None

        try:
            with get_trades_db() as db:
                q = db.query(SignalORM.id)

                if start_db is not None:
                    q = q.filter(SignalORM.last_snapshot_time >= start_db)
                if end_db is not None:
                    q = q.filter(SignalORM.last_snapshot_time < end_db)
                if clean_symbols:
                    q = q.filter(
                        or_(
                            SignalORM.equity_ref.in_(clean_symbols),
                            SignalORM.symbol.in_(clean_symbols),
                        )
                    )
                if clean_setups:
                    q = q.filter(SignalORM.setup.in_(clean_setups))

                q = q.order_by(SignalORM.last_snapshot_time.asc(), SignalORM.id.asc())
                if limit and int(limit) > 0:
                    q = q.limit(int(limit))

                signal_ids = [int(row[0]) for row in q.all() if row and row[0] is not None]
                rows = []
                for signal_db_id in signal_ids:
                    rec = db.get(SignalORM, signal_db_id)
                    if rec is not None:
                        rows.append(SignalSchema.model_validate(rec))

            return rows
        except Exception:
            logger.exception(
                "fetch_for_signal_review failed | start=%s end=%s symbols=%s setups=%s limit=%s",
                start_db,
                end_db,
                clean_symbols,
                clean_setups,
                limit,
            )
            raise

    @staticmethod
    def list_for_ui(
        *,
        lifecycle: Optional[str] = None,
        setup: Optional[str] = None,
        equity_ref: Optional[str] = None,
        side: Optional[str] = None,
        stages: Optional[List[str]] = None,
        statuses: Optional[List[str]] = None,
        limit: int = 500,
    ) -> List["SignalSchema"]:
        try:
            with get_trades_db() as db:
                q = db.query(SignalORM)

                if lifecycle:
                    q = q.filter(SignalORM.lifecycle == lifecycle)
                if setup:
                    q = q.filter(SignalORM.setup == str(setup).strip().upper())
                if equity_ref:
                    q = q.filter(SignalORM.equity_ref == equity_ref)
                if side:
                    q = q.filter(SignalORM.side == SignalSide.from_string(side).value)
                if stages:
                    st = [LifecycleStage.from_string(x).value for x in stages]
                    q = q.filter(SignalORM.stage.in_(st))
                if statuses:
                    ss = [SignalStatus.from_string(x).value for x in statuses]
                    q = q.filter(SignalORM.status.in_(ss))

                # Keep the ORDER BY query narrow. Selecting the full SignalORM row here
                # includes large JSON columns (criteria_json/snapshot_json/meta_json), and
                # MySQL may need to filesort those wide rows when there is no covering
                # index. That can fail with:
                #   1038 (HY001): Out of sort memory
                #
                # Two-step loading avoids that silent UI/trade-generator failure:
                #   1. sort/limit only the integer primary key,
                #   2. fetch the selected rows by primary key,
                #   3. restore the sorted order in Python.
                #
                # This is intentional. If the DB query itself fails, the
                # exception is still logged by the outer handler.
                safe_limit = max(1, min(int(limit), 2000))
                ordered_ids = [
                    row_id
                    for (row_id,) in (
                        q.with_entities(SignalORM.id)
                        .order_by(SignalORM.last_eval_time.desc(), SignalORM.id.desc())
                        .limit(safe_limit)
                        .all()
                    )
                ]

                if not ordered_ids:
                    rows = []
                else:
                    fetched = db.query(SignalORM).filter(SignalORM.id.in_(ordered_ids)).all()
                    by_id = {int(r.id): r for r in fetched}
                    rows = [by_id[x] for x in ordered_ids if x in by_id]

            return [SignalSchema.model_validate(r) for r in rows]
        except Exception:
            logger.exception("list_for_ui failed")
            raise

    # -----------------------------------------------------------------
    # Lifecycle write methods
    # -----------------------------------------------------------------
    @staticmethod
    def create_signal(
        *,
        equity_ref: str,
        symbol: str,
        lifecycle: str,
        setup: str,
        side: str | SignalSide,
        stage: str | LifecycleStage,
        status: str | SignalStatus = SignalStatus.OPEN,
        status_reason: Optional[str] = None,
        last_eval_time: datetime,
        last_snapshot_time: datetime,
        criteria_json: Dict[str, Any],
        snapshot_json: Dict[str, Any],
        meta_json: Optional[Dict[str, Any]] = None,
        last_price: Optional[Decimal] = None,
        ltp: Optional[Decimal] = None,
        ltp_time: Optional[datetime] = None,

        last_pnl: Optional[Decimal] = None,
        last_pnl_value: Optional[Decimal] = None,
        max_price: Optional[Decimal] = None,
        min_price: Optional[Decimal] = None,
        max_time: Optional[datetime] = None,
        min_time: Optional[datetime] = None,
        max_pnl: Optional[Decimal] = None,
        min_pnl: Optional[Decimal] = None,
        max_pnl_value: Optional[Decimal] = None,
        min_pnl_value: Optional[Decimal] = None,

        signal_id: Optional[str] = None,
    ) -> "SignalSchema":
        side_e = SignalSide.from_string(side)
        stage_e = LifecycleStage.from_string(stage)
        status_e = SignalStatus.from_string(status)

        last_eval_time = _to_ist_naive(last_eval_time) or last_eval_time
        last_snapshot_time = _to_ist_naive(last_snapshot_time) or last_snapshot_time
        ltp_time = _to_ist_naive(ltp_time) if ltp_time else None

        created_ts = _require_ist_naive_datetime(last_eval_time, source="create_signal.last_eval_time")
        created_px = _dec_or_none(last_price)

        setup_label = _require_setup_label(setup, source="create_signal.setup")
        _assert_setup_identity(
            persisted_setup=setup_label,
            meta_json=meta_json,
            source="create_signal",
        )

        try:
            with get_trades_db() as db:
                rec = SignalORM(
                    signal_id=signal_id or str(uuid.uuid4()),
                    equity_ref=equity_ref,
                    symbol=symbol,
                    lifecycle=lifecycle,
                    setup=setup_label,
                    side=side_e.value,
                    stage=stage_e.value,
                    status=status_e.value,
                    status_reason=status_reason,
                    first_seen_time=created_ts,
                    created_price=float(created_px) if created_px is not None else None,

                    last_eval_time=last_eval_time,
                    last_snapshot_time=last_snapshot_time,

                    stage_changed_time=last_eval_time,
                    status_changed_time=last_eval_time,

                    qualified_time=last_eval_time if _is_qualified_stage(stage_e.value) else None,
                    actionable_time=last_eval_time if _is_actionable_stage(stage_e.value) else None,

                    closed_time=last_eval_time if status_e.value != SignalStatus.OPEN.value else None,
                    closed_price=float(created_px) if (status_e.value != SignalStatus.OPEN.value and created_px is not None) else None,

                    last_price=float(_dec_or_none(last_price)) if last_price is not None else None,
                    ltp=float(_dec_or_none(ltp)) if ltp is not None else None,
                    ltp_time=ltp_time,

                    last_pnl=float(_dec_or_zero(last_pnl)),
                    last_pnl_value=float(_dec_or_zero(last_pnl_value)),

                    max_price=float(_dec_or_zero(max_price)),
                    min_price=float(_dec_or_zero(min_price)),

                    max_time=_to_ist_naive(max_time) if max_time else None,
                    min_time=_to_ist_naive(min_time) if min_time else None,

                    max_pnl=float(_dec_or_zero(max_pnl)),
                    min_pnl=float(_dec_or_zero(min_pnl)),

                    max_pnl_value=float(_dec_or_zero(max_pnl_value)),
                    min_pnl_value=float(_dec_or_zero(min_pnl_value)),

                    criteria_json=sanitize_json(criteria_json or {}),
                    snapshot_json=sanitize_json(snapshot_json or {}),
                    meta_json=sanitize_json(meta_json) if isinstance(meta_json, dict) else None,
                )
                db.add(rec)
                db.commit()
                db.refresh(rec)
                return SignalSchema.model_validate(rec)

        except Exception:
            logger.exception("create_signal failed | %s %s %s", equity_ref, lifecycle, side_e.value)
            raise

    @staticmethod
    def update_signal(
        *,
        signal_id: str,
        stage: Optional[str | LifecycleStage] = None,
        status: Optional[str | SignalStatus] = None,
        setup: Optional[str] = None,
        status_reason: Optional[str] = None,
        last_eval_time: Optional[datetime] = None,
        last_snapshot_time: Optional[datetime] = None,
        criteria_json: Optional[Dict[str, Any]] = None,
        snapshot_json: Optional[Dict[str, Any]] = None,
        meta_json: Optional[Dict[str, Any]] = None,
        last_price: Optional[Decimal] = None,
        ltp: Optional[Decimal] = None,
        ltp_time: Optional[datetime] = None,

        last_pnl: Optional[Decimal] = None,
        last_pnl_value: Optional[Decimal] = None,
        max_price: Optional[Decimal] = None,
        min_price: Optional[Decimal] = None,
        max_time: Optional[datetime] = None,
        min_time: Optional[datetime] = None,
        max_pnl: Optional[Decimal] = None,
        min_pnl: Optional[Decimal] = None,
        max_pnl_value: Optional[Decimal] = None,
        min_pnl_value: Optional[Decimal] = None,
    ) -> Optional["SignalSchema"]:
        try:
            with get_trades_db() as db:
                rec = db.query(SignalORM).filter(SignalORM.signal_id == signal_id).one_or_none()
                if not rec:
                    return None

                ts_eval = _to_ist_naive(last_eval_time) if last_eval_time else None
                ts_snap = _to_ist_naive(last_snapshot_time) if last_snapshot_time else None
                ts_ltp = _to_ist_naive(ltp_time) if ltp_time else None

                if ts_eval is not None:
                    rec.last_eval_time = ts_eval
                if ts_snap is not None:
                    rec.last_snapshot_time = ts_snap

                if criteria_json is not None:
                    rec.criteria_json = sanitize_json(criteria_json)
                if snapshot_json is not None:
                    rec.snapshot_json = sanitize_json(snapshot_json)
                if meta_json is not None or setup is not None:
                    _assert_setup_identity(
                        persisted_setup=rec.setup,
                        requested_setup=setup,
                        meta_json=meta_json,
                        source="update_signal",
                    )
                if meta_json is not None:
                    rec.meta_json = sanitize_json(meta_json)

                if last_price is not None:
                    rec.last_price = float(_dec_or_none(last_price))
                if ltp is not None:
                    rec.ltp = float(_dec_or_none(ltp))
                if ts_ltp is not None:
                    rec.ltp_time = ts_ltp

                if last_pnl is not None:
                    rec.last_pnl = float(_dec_or_zero(last_pnl))
                if last_pnl_value is not None:
                    rec.last_pnl_value = float(_dec_or_zero(last_pnl_value))

                if max_price is not None:
                    rec.max_price = float(_dec_or_zero(max_price))
                if min_price is not None:
                    rec.min_price = float(_dec_or_zero(min_price))

                if max_time is not None:
                    rec.max_time = _to_ist_naive(max_time)
                if min_time is not None:
                    rec.min_time = _to_ist_naive(min_time)

                if max_pnl is not None:
                    rec.max_pnl = float(_dec_or_zero(max_pnl))
                if min_pnl is not None:
                    rec.min_pnl = float(_dec_or_zero(min_pnl))

                if max_pnl_value is not None:
                    rec.max_pnl_value = float(_dec_or_zero(max_pnl_value))
                if min_pnl_value is not None:
                    rec.min_pnl_value = float(_dec_or_zero(min_pnl_value))

                if stage is not None:
                    new_stage = LifecycleStage.from_string(stage).value
                    prev_stage = (rec.stage or "").upper()
                    if prev_stage != new_stage:
                        rec.stage = new_stage
                        rec.stage_changed_time = ts_eval or _now_ist()

                        if _is_qualified_stage(new_stage) and rec.qualified_time is None:
                            rec.qualified_time = ts_eval or _now_ist()

                        if _is_actionable_stage(new_stage) and rec.actionable_time is None:
                            rec.actionable_time = ts_eval or _now_ist()

                if status is not None:
                    new_status = SignalStatus.from_string(status).value
                    prev_status = (rec.status or "").upper()
                    if prev_status != new_status:
                        rec.status = new_status
                        rec.status_changed_time = ts_eval or _now_ist()

                        if new_status != SignalStatus.OPEN.value and rec.closed_time is None:
                            rec.closed_time = ts_eval or _now_ist()

                        if new_status != SignalStatus.OPEN.value and rec.closed_price is None:
                            px = _dec_or_none(last_price)
                            rec.closed_price = float(px) if px is not None else None

                if status_reason is not None:
                    rec.status_reason = status_reason

                db.commit()
                db.refresh(rec)
                return SignalSchema.model_validate(rec)

        except Exception:
            logger.exception("update_signal failed | signal_id=%s", signal_id)
            raise

    @staticmethod
    def close_signal(
        *,
        signal_id: str,
        status: str | SignalStatus,
        stage: Optional[str | LifecycleStage] = None,
        setup: Optional[str] = None,
        reason: Optional[str] = None,
        ts: Optional[datetime] = None,
        last_eval_time: Optional[datetime] = None,
        last_snapshot_time: Optional[datetime] = None,
        criteria_json: Optional[Dict[str, Any]] = None,
        snapshot_json: Optional[Dict[str, Any]] = None,
        meta_json: Optional[Dict[str, Any]] = None,
        last_price: Optional[Decimal] = None,
        ltp: Optional[Decimal] = None,
        ltp_time: Optional[datetime] = None,
        meta_patch: Optional[Dict[str, Any]] = None,

        last_pnl: Optional[Decimal] = None,
        last_pnl_value: Optional[Decimal] = None,
        max_price: Optional[Decimal] = None,
        min_price: Optional[Decimal] = None,
        max_time: Optional[datetime] = None,
        min_time: Optional[datetime] = None,
        max_pnl: Optional[Decimal] = None,
        min_pnl: Optional[Decimal] = None,
        max_pnl_value: Optional[Decimal] = None,
        min_pnl_value: Optional[Decimal] = None,
    ) -> Optional["SignalSchema"]:
        """
        Close an existing signal with a terminal status.
        Also persists the final evaluation payload/time/price from the closing snapshot.
        """
        status_e = SignalStatus.from_string(status)
        if status_e == SignalStatus.OPEN:
            raise ValueError("close_signal cannot use OPEN status")

        ts = _require_ist_naive_datetime(ts, source="close_signal.ts")
        last_eval_time = _to_ist_naive(last_eval_time) or ts
        last_snapshot_time = _to_ist_naive(last_snapshot_time) or last_eval_time
        ltp_time = _to_ist_naive(ltp_time) if ltp_time else None

        try:
            with get_trades_db() as db:
                rec = db.query(SignalORM).filter(SignalORM.signal_id == signal_id).one_or_none()
                if not rec:
                    return None

                rec.last_eval_time = last_eval_time
                rec.last_snapshot_time = last_snapshot_time

                if criteria_json is not None:
                    rec.criteria_json = sanitize_json(criteria_json)

                if snapshot_json is not None:
                    rec.snapshot_json = sanitize_json(snapshot_json)

                next_meta = meta_json
                if next_meta is None and isinstance(meta_patch, dict) and meta_patch:
                    next_meta = dict(rec.meta_json) if isinstance(rec.meta_json, dict) else {}
                    next_meta.update(meta_patch)

                if next_meta is not None or setup is not None:
                    _assert_setup_identity(
                        persisted_setup=rec.setup,
                        requested_setup=setup,
                        meta_json=next_meta,
                        source="close_signal",
                    )
                if next_meta is not None:
                    rec.meta_json = sanitize_json(next_meta)

                px = _dec_or_none(last_price)
                if last_price is not None:
                    rec.last_price = float(px) if px is not None else None

                if ltp is not None:
                    rec.ltp = float(_dec_or_none(ltp))

                if ltp_time is not None:
                    rec.ltp_time = ltp_time

                if last_pnl is not None:
                    rec.last_pnl = float(_dec_or_zero(last_pnl))
                if last_pnl_value is not None:
                    rec.last_pnl_value = float(_dec_or_zero(last_pnl_value))

                if max_price is not None:
                    rec.max_price = float(_dec_or_zero(max_price))
                if min_price is not None:
                    rec.min_price = float(_dec_or_zero(min_price))

                if max_time is not None:
                    rec.max_time = _to_ist_naive(max_time)
                if min_time is not None:
                    rec.min_time = _to_ist_naive(min_time)

                if max_pnl is not None:
                    rec.max_pnl = float(_dec_or_zero(max_pnl))
                if min_pnl is not None:
                    rec.min_pnl = float(_dec_or_zero(min_pnl))

                if max_pnl_value is not None:
                    rec.max_pnl_value = float(_dec_or_zero(max_pnl_value))
                if min_pnl_value is not None:
                    rec.min_pnl_value = float(_dec_or_zero(min_pnl_value))

                rec.status = status_e.value
                rec.status_reason = reason
                rec.status_changed_time = ts

                stage_e = LifecycleStage.from_string(stage) if stage is not None else None
                if stage_e is None and status_e in {
                    SignalStatus.CLOSED,
                    SignalStatus.INVALIDATED,
                    SignalStatus.EXPIRED,
                    SignalStatus.REPLACED,
                    SignalStatus.CANCELLED,
                }:
                    stage_e = LifecycleStage.FORCE_EXIT
                if stage_e is not None:
                    rec.stage = stage_e.value
                    rec.stage_changed_time = ts

                if rec.closed_time is None:
                    rec.closed_time = ts

                if px is not None:
                    rec.closed_price = float(px)

                db.commit()
                db.refresh(rec)
                return SignalSchema.model_validate(rec)

        except Exception:
            logger.exception("close_signal failed | signal_id=%s", signal_id)
            raise

    @staticmethod
    def replace_signal(
        *,
        existing_signal_id: str,
        new_side: str | SignalSide,
        new_stage: str | LifecycleStage,
        equity_ref: str,
        symbol: str,
        lifecycle: str,
        setup: str,
        last_eval_time: datetime,
        last_snapshot_time: datetime,
        criteria_json: Dict[str, Any],
        snapshot_json: Dict[str, Any],
        meta_json: Optional[Dict[str, Any]] = None,
        last_price: Optional[Decimal] = None,
        ltp: Optional[Decimal] = None,
        ltp_time: Optional[datetime] = None,
        reason: str = "opposite_actionable",
    ) -> tuple[Optional["SignalSchema"], "SignalSchema"]:
        """
        Close current signal as REPLACED and create a new opposite-side OPEN signal.
        """
        old_signal = SignalSchema.close_signal(
            signal_id=existing_signal_id,
            status=SignalStatus.REPLACED,
            reason=reason,
            ts=last_eval_time,
            last_eval_time=last_eval_time,
            last_snapshot_time=last_snapshot_time,
            criteria_json=criteria_json,
            snapshot_json=snapshot_json,
            meta_json=meta_json,
            last_price=last_price,
            ltp=ltp,
            ltp_time=ltp_time,
            meta_patch={"replaced": True, "replaced_reason": reason},
        )

        new_signal = SignalSchema.create_signal(
            equity_ref=equity_ref,
            symbol=symbol,
            lifecycle=lifecycle,
            setup=setup,
            side=new_side,
            stage=new_stage,
            status=SignalStatus.OPEN,
            status_reason=None,
            last_eval_time=last_eval_time,
            last_snapshot_time=last_snapshot_time,
            criteria_json=criteria_json,
            snapshot_json=snapshot_json,
            meta_json=meta_json,
            last_price=last_price,
            ltp=ltp,
            ltp_time=ltp_time,
        )

        return old_signal, new_signal

    @staticmethod
    def touch_signal(
        *,
        signal_id: str,
        last_eval_time: datetime,
        last_snapshot_time: datetime,
        criteria_json: Dict[str, Any],
        snapshot_json: Dict[str, Any],
        last_price: Optional[Decimal] = None,
        ltp: Optional[Decimal] = None,
        ltp_time: Optional[datetime] = None,
        meta_json: Optional[Dict[str, Any]] = None,

        last_pnl: Optional[Decimal] = None,
        last_pnl_value: Optional[Decimal] = None,
        max_price: Optional[Decimal] = None,
        min_price: Optional[Decimal] = None,
        max_time: Optional[datetime] = None,
        min_time: Optional[datetime] = None,
        max_pnl: Optional[Decimal] = None,
        min_pnl: Optional[Decimal] = None,
        max_pnl_value: Optional[Decimal] = None,
        min_pnl_value: Optional[Decimal] = None,
    ) -> Optional["SignalSchema"]:
        """
        Update evaluation payloads/timestamps without changing stage/status.
        """
        return SignalSchema.update_signal(
            signal_id=signal_id,
            last_eval_time=last_eval_time,
            last_snapshot_time=last_snapshot_time,
            criteria_json=criteria_json,
            snapshot_json=snapshot_json,
            meta_json=meta_json,
            last_price=last_price,
            ltp=ltp,
            ltp_time=ltp_time,

            last_pnl=last_pnl,
            last_pnl_value=last_pnl_value,
            max_price=max_price,
            min_price=min_price,
            max_time=max_time,
            min_time=min_time,
            max_pnl=max_pnl,
            min_pnl=min_pnl,
            max_pnl_value=max_pnl_value,
            min_pnl_value=min_pnl_value,
        )

    @staticmethod
    def archive_current_rows():
        """Archive all current signals and verify their history identity."""
        from schemas.archive import ArchiveSpec, archive_rows

        return archive_rows(
            ArchiveSpec(
                name="signals",
                source_model=SignalORM,
                history_model=SignalHistoryORM,
                excluded_target_columns=frozenset(
                    {"hist_id", "archived_on", "trading_date"}
                ),
                verification_condition=lambda source, target: and_(
                    target.c.id == source.c.id,
                    target.c.trading_date == func.date(source.c.last_eval_time),
                ),
            )
        )
