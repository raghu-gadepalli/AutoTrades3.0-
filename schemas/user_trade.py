# schemas/user_trade.py
#
# vNext UserTrade schema (DB-backed) — single source of truth for:
# - trade_generator (creates rows)
# - UI (edits rows, submits for execution)
# - trade_monitor (updates live/exit intent + MTM)
# - trade_executor (places orders + immediate polling)
# - trade_backfill_from_oms (reconciles OMS truth back into user_trades)
#
# NOTE:
# - Signal-originated trades are managed by the exact source-signal Auction contract.
# - Manual trades remain independent and use price-only management.
# - entry_status is ENTRY lifecycle; exit_status is EXIT lifecycle (separate).
# - Stop-loss / targets are soft (monitor-managed). No broker SL/OCO fields here.

import logging
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Optional, List, Dict, Any, Literal, Set

from pydantic import BaseModel, Field
from sqlalchemy import and_, bindparam, func, text, or_
from sqlalchemy.exc import IntegrityError

from database.database import get_trades_db
from models.trade_models import (
    UserTrade as UserTradeORM,
    UserTradeHistory as UserTradeHistoryORM,
)

from utils.json_utils import sanitize_json
from utils.datetime_utils import IST

from enums.enums import (
    SymbolType,
    TradeType,
    EntryStatus,
    ExitStatus,
    PositionStyle,
)

logger = logging.getLogger(__name__)


def _to_ist_naive(ts: Optional[datetime]) -> Optional[datetime]:
    """Normalize any datetime to naive IST for DB storage/UI consistency."""
    if ts is None or not isinstance(ts, datetime):
        return None
    try:
        if getattr(ts, "tzinfo", None) is not None:
            ts = ts.astimezone(IST)
        return ts.replace(tzinfo=None)
    except Exception:
        return ts.replace(tzinfo=None) if isinstance(ts, datetime) else None


class TradeManagementSchema(BaseModel):
    # Fail loudly if monitor/generator starts writing a trade-management key
    # that has not been added to the schema. Pydantic's default extra=ignore
    # silently drops unknown keys, which hides handoff/persistence bugs.
    model_config = {"extra": "forbid"}

    """Strict Auction-driven adaptive trade-management state.

    Stored in user_trades.trade_management. Version 2 is intentionally not
    backward-compatible: an open trade carrying an older management payload
    must fail visibly and be handled explicitly.
    """
    version: Literal[2]
    mode: Literal["AUCTION_ADAPTIVE_V2"]
    price_basis: Literal["INSTRUMENT"]

    posture: str = "HOLD"

    entry_price: Decimal = Decimal("0.00")
    planned_entry_price: Optional[Decimal] = None
    entry_slippage: Optional[Decimal] = None
    entry_slippage_r: Optional[Decimal] = None
    entry_rebased_at: Optional[datetime] = None
    entry_rebase_reason: Optional[str] = None
    atr_at_entry: Optional[Decimal] = None
    instrument_atr: Optional[Decimal] = None
    instrument_atr_factor: Decimal = Decimal("1.0000")

    target_r_multiple: Decimal = Decimal("2.0000")
    stop_r_multiple: Decimal = Decimal("1.0000")

    current_target_price: Optional[Decimal] = None
    current_stop_price: Optional[Decimal] = None

    # Immutable initial levels for audit/replay clarity. These should be set
    # at CREATE from setup handoff and should not be rewritten by adaptive
    # expansion/contraction; the monitor mutates current_* levels from there.
    initial_target_price: Optional[Decimal] = None
    initial_stop_price: Optional[Decimal] = None

    # Initial setup handoff/audit fields. The monitor should use the current_*
    # levels above as its active source of truth and must not rediscover setup
    # invalidation from snapshots. These fields explain where the initial levels
    # came from.
    initial_stop_source: Optional[str] = None
    initial_stop_reason: Optional[str] = None
    initial_target_source: Optional[str] = None
    initial_target_reason: Optional[str] = None

    # Signal/setup handoff in trade_management is intentionally limited to a
    # label for audit/UI clarity.  Setup levels and invalidation references stay
    # on the signal metadata/setup_levels; the trade monitor owns ATR stops,
    # ATR targets, trailing, profit protection, and exits.
    signal_setup_label: Optional[str] = None

    # Runtime context written by TradeMonHelper.evaluate().  These fields are
    # part of the strict persistence contract: manual trades are managed from
    # price only, while signal-originated trades retain lifecycle context.
    # Keeping them explicit preserves extra="forbid" without rejecting valid
    # monitor updates.
    management_context: Optional[str] = None
    signal_context_available: Optional[bool] = None

    # Exact source-signal contract projected by TradeMonitor. Manual trades
    # keep these nullable and are managed from price only.
    target_expansion_allowed: bool = False
    trail_mode: str = "NORMAL"
    exit_pressure: str = "LOW"
    management_posture: Optional[str] = None
    management_reason_code: Optional[str] = None
    signal_stage: Optional[str] = None
    signal_status: Optional[str] = None
    lifecycle_trade_action: Optional[str] = None
    directional_alignment: Optional[str] = None
    auction_action: Optional[str] = None
    auction_state: Optional[str] = None
    should_exit_signal: bool = False

    # MFE-triggered profit protection. The stop is expressed in signed
    # profit-space R: negative retains risk, zero is cost, positive locks
    # profit. These fields are initialized by TradeMonHelper and must be part
    # of this strict persistence contract (extra=forbid).
    profit_protection_applied: bool = False
    profit_protection_trigger_mfe_r: Optional[Decimal] = None
    profit_protection_stop_profit_r: Optional[Decimal] = None
    current_stop_profit_r: Optional[Decimal] = None

    # FUT/EQ-first group management.  These fields are JSON-only; no relational
    # migration is required.  The reference leg owns MFE/MAE/target/stop and
    # followers store proportional projections for audit and replay clarity.
    group_management_enabled: bool = False
    group_role: str = "STANDALONE"
    group_reference_trade_id: Optional[int] = None
    group_reference_instrument: Optional[str] = None
    group_reference_symbol: Optional[str] = None
    group_reference_entry_price: Optional[Decimal] = None
    group_reference_atr: Optional[Decimal] = None
    group_reference_current_price: Optional[Decimal] = None
    group_mfe_r: Optional[Decimal] = None
    group_mae_r: Optional[Decimal] = None
    last_processed_group_mfe_bucket_r: Optional[Decimal] = None
    last_processed_group_mae_bucket_r: Optional[Decimal] = None
    group_stop_profit_r: Optional[Decimal] = None
    group_target_r: Optional[Decimal] = None
    group_projected_stop_price: Optional[Decimal] = None
    group_projected_target_price: Optional[Decimal] = None
    group_update_source: Optional[str] = None
    group_update_reason: Optional[str] = None

    # True only when a newly calculated MAE stop is already breached at the
    # current observation.  The monitor exits at observed price; executor must
    # not claim a retrospective trigger-level fill.
    mae_risk_exit_required: bool = False
    mae_risk_exit_observed_price: Optional[Decimal] = None

    expansion_count: int = 0
    last_target_hit_price: Optional[Decimal] = None

    # Runtime R-state written by the monitor. These are useful in audit/replay
    # and must persist instead of being silently dropped by schema validation.
    mfe_profit_r: Optional[Decimal] = None
    current_profit_r: Optional[Decimal] = None
    max_favorable_price: Optional[Decimal] = None

    last_managed_price: Optional[Decimal] = None

    last_updated_at: Optional[datetime] = None
    last_update_reason: Optional[str] = None

    def to_json_dict(self) -> Dict[str, Any]:
        """Return a DB JSON-safe dict."""
        return sanitize_json(self.model_dump(mode="json", exclude_none=False))

def _normalize_trade_management(value: Any) -> Optional[Dict[str, Any]]:
    """
    Normalize trade_management into a DB JSON-safe dict.

    Accepts:
    - None
    - TradeManagementSchema
    - dict payload
    """
    if value is None:
        return None

    if isinstance(value, TradeManagementSchema):
        return value.to_json_dict()

    if isinstance(value, dict):
        # Strict validation by design: trade_management is owned by the trade
        # monitor and should contain only ATR/trailing/exit-management state.
        # Signal/setup reference levels must remain on the signal metadata
        # (setup_levels) and must not be silently accepted or cleaned here.
        return TradeManagementSchema.model_validate(value).to_json_dict()

    raise TypeError(
        f"unsupported trade_management payload type: {type(value).__name__}"
    )


class UserTradeActiveContextRow(BaseModel):
    """Narrow trade projection for causal active-context replay."""

    model_config = {"from_attributes": True}

    id: Optional[int] = None
    signal_id: str
    symbol: str
    equity_ref: str
    trade_type: TradeType
    entry_status: EntryStatus
    exit_status: ExitStatus
    entry_time: datetime
    exit_time: Optional[datetime] = None


class UserTradeSchema(BaseModel):
    model_config = {"from_attributes": True}

    # ---------------------------------------------------------------------
    # 1) Core Identification / linkage
    # ---------------------------------------------------------------------
    id: Optional[int] = None

    userid: str
    signal_id: str = Field(..., description="Reference to originating signal (not a FK)")
    source: str = "autotrades"
    message: Optional[str] = None

    symbol: str
    equity_ref: str
    instrument_type: SymbolType
    trade_type: TradeType

    position_style: PositionStyle = PositionStyle.NAKED
    hedged_symbol: Optional[str] = None

    # ---------------------------------------------------------------------
    # 2) Lifecycle control
    # ---------------------------------------------------------------------
    entry_status: EntryStatus = EntryStatus.CREATED
    exit_status: ExitStatus = ExitStatus.NONE

    execution_mode: Literal["REAL", "VIRTUAL"] = "VIRTUAL"
    intraday_only: bool = False

    # ---------------------------------------------------------------------
    # 3) Snapshots (context for UI + monitoring)
    # ---------------------------------------------------------------------
    entry_snapshot: Dict[str, Any]
    last_snapshot: Optional[Dict[str, Any]] = None

    # ---------------------------------------------------------------------
    # 4) Entry fields
    # ---------------------------------------------------------------------
    entry_time: datetime
    entry_intent_time: Optional[datetime] = None
    entry_exec_time: Optional[datetime] = None
    entry_reconciled_at: Optional[datetime] = None

    entry_price: Decimal = Decimal("0.00")
    executed_entry_price: Optional[Decimal] = None
    executed_entry_qty: Optional[int] = None

    quantity: int = 1

    entry_order_id: Optional[str] = None
    entry_order_response_json: Optional[str] = None
    entry_retries: int = 0

    # ---------------------------------------------------------------------
    # 5) Adaptive trade management JSON
    # ---------------------------------------------------------------------
    trade_management: Optional[TradeManagementSchema] = None

    # ---------------------------------------------------------------------
    # 6) Exit fields
    # ---------------------------------------------------------------------
    exit_reason: Optional[str] = None
    exit_rule: Optional[str] = None

    exit_time: Optional[datetime] = None
    exit_intent_time: Optional[datetime] = None
    exit_exec_time: Optional[datetime] = None
    exit_reconciled_at: Optional[datetime] = None

    exit_price: Optional[Decimal] = None
    executed_exit_price: Optional[Decimal] = None
    executed_exit_qty: int = 0

    exit_pnl: Optional[Decimal] = None

    exit_order_id: Optional[str] = None
    exit_order_response_json: Optional[str] = None
    exit_retries: int = 0

    # ---------------------------------------------------------------------
    # 10) Live monitoring fields
    # ---------------------------------------------------------------------
    last_time: datetime
    last_price: Decimal = Decimal("0.00")
    last_pnl: Decimal = Decimal("0.00")
    last_pnl_value: Decimal = Decimal("0.00")
    max_price: Decimal = Decimal("0.00")
    min_price: Decimal = Decimal("0.00")
    max_time: datetime
    min_time: datetime

    # ---------------------------------------------------------------------
    # 11) Execution observability (executor-owned)
    # ---------------------------------------------------------------------
    exec_last_checked_at: Optional[datetime] = None
    exec_status: Optional[str] = None
    exec_status_message: Optional[str] = None

    # ---------------------------------------------------------------------
    # 12) Reconciliation observability (backfill-owned)
    # ---------------------------------------------------------------------
    reconcile_last_checked_at: Optional[datetime] = None
    reconcile_status: Optional[str] = None
    reconcile_status_message: Optional[str] = None

    # ---------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------
    def to_db_dict(self) -> Dict[str, Any]:
        data = self.model_dump(exclude_none=True)
        for k, v in list(data.items()):
            if isinstance(
                v,
                (
                    SymbolType,
                    TradeType,
                    EntryStatus,
                    ExitStatus,
                    PositionStyle,
                ),
            ):
                data[k] = v.value

        if "trade_management" in data:
            data["trade_management"] = _normalize_trade_management(data.get("trade_management"))

        return data

    # ---------------------------------------------------------------------
    # CRUD + Query helpers
    # ---------------------------------------------------------------------
    @staticmethod
    def create_user_trade(data: dict) -> Optional["UserTradeSchema"]:
        valid: Set[str] = {
            # identity
            "userid", "signal_id", "source", "message",
            "symbol", "equity_ref", "instrument_type", "trade_type",
            "position_style", "hedged_symbol",

            # lifecycle control
            "entry_status", "exit_status", "execution_mode", "intraday_only",

            # snapshots
            "entry_snapshot", "last_snapshot",

            # entry
            "entry_time", "entry_intent_time", "entry_exec_time", "entry_reconciled_at",
            "entry_price", "executed_entry_price", "executed_entry_qty",
            "quantity",
            "entry_order_id", "entry_order_response_json", "entry_retries",

            # adaptive trade management JSON
            "trade_management",

            # exit
            "exit_reason", "exit_rule",
            "exit_time", "exit_intent_time", "exit_exec_time", "exit_reconciled_at",
            "exit_price", "executed_exit_price", "executed_exit_qty", "exit_pnl",
            "exit_order_id", "exit_order_response_json", "exit_retries",

            # live
            "last_time", "last_price", "last_pnl", "last_pnl_value",
            "max_price", "min_price", "max_time", "min_time",

            # executor observability
            "exec_last_checked_at", "exec_status", "exec_status_message",

            # reconciliation observability
            "reconcile_last_checked_at", "reconcile_status", "reconcile_status_message",
        }

        payload = {k: v for k, v in (data or {}).items() if k in valid}

        payload["entry_snapshot"] = sanitize_json(payload.get("entry_snapshot") or {})
        if "last_snapshot" in payload:
            payload["last_snapshot"] = sanitize_json(payload.get("last_snapshot"))
        if "trade_management" in payload:
            payload["trade_management"] = _normalize_trade_management(payload.get("trade_management"))

        for k, v in list(payload.items()):
            if isinstance(v, datetime):
                payload[k] = _to_ist_naive(v)

        for k in (
            "instrument_type", "trade_type",
            "entry_status", "exit_status",
            "position_style",
        ):
            if k in payload and hasattr(payload[k], "value"):
                payload[k] = payload[k].value

        with get_trades_db() as db:
            orm = UserTradeORM(**payload)
            db.add(orm)
            try:
                db.commit()
                db.refresh(orm)
                return UserTradeSchema.model_validate(orm)
            except IntegrityError:
                db.rollback()
                # Do not return the existing row as if a new trade was created.
                # The trade generator counts returned rows as created rows;
                # returning an existing duplicate caused misleading
                # "created_count" logs and replay summaries. Duplicate/open
                # prevention should happen before insert in trading_helper.py.
                logger.warning(
                    "UserTrade duplicate; insert skipped (userid=%s signal_id=%s symbol=%s)",
                    payload.get("userid"), payload.get("signal_id"), payload.get("symbol"),
                )
                return None
            except Exception as e:
                db.rollback()
                logger.error("Error inserting UserTrade: %s", e, exc_info=True)
                return None

    @staticmethod
    def fetch_for_signal_ids(signal_ids: List[str]) -> List["UserTradeSchema"]:
        """Fetch trade rows linked to the given signal ids for review/reporting."""
        clean_ids = [str(x).strip() for x in (signal_ids or []) if str(x).strip()]
        if not clean_ids:
            return []

        with get_trades_db() as db:
            rows = (
                db.query(UserTradeORM)
                .filter(UserTradeORM.signal_id.in_(clean_ids))
                .order_by(UserTradeORM.signal_id.asc(), UserTradeORM.entry_time.asc(), UserTradeORM.id.asc())
                .all()
            )
        return [UserTradeSchema.model_validate(r) for r in rows]

    @staticmethod
    def fetch_for_signal_id(signal_id: str) -> List["UserTradeSchema"]:
        """Fetch trade rows for one signal id.

        Review scripts use this single-signal fetch path to avoid large IN-list
        queries and full-row sorts on low-memory VPS instances.
        """
        clean_id = str(signal_id or "").strip()
        if not clean_id:
            return []

        with get_trades_db() as db:
            rows = (
                db.query(UserTradeORM)
                .filter(UserTradeORM.signal_id == clean_id)
                .order_by(UserTradeORM.entry_time.asc(), UserTradeORM.id.asc())
                .all()
            )
        return [UserTradeSchema.model_validate(r) for r in rows]

    @staticmethod
    def has_any_trade_for_signal(*, userid: str, signal_id: str) -> bool:
        """Return True once a signal has ever deployed for this user.

        Historical terminal rows deliberately count.  AutoTrades currently has
        no re-entry policy; closing the original package must not make the same
        signal deployable again.
        """
        userid = str(userid or "").strip()
        signal_id = str(signal_id or "").strip()
        if not userid or not signal_id:
            return False
        with get_trades_db() as db:
            row = (
                db.query(UserTradeORM.id)
                .filter(UserTradeORM.userid == userid)
                .filter(UserTradeORM.signal_id == signal_id)
                .limit(1)
                .first()
            )
        return row is not None

    @staticmethod
    def fetch_deployed_signal_ids(*, userid: str, signal_ids: List[str]) -> Set[str]:
        userid = str(userid or "").strip()
        clean_ids = sorted({str(x or "").strip() for x in (signal_ids or []) if str(x or "").strip()})
        if not userid or not clean_ids:
            return set()
        with get_trades_db() as db:
            rows = (
                db.query(UserTradeORM.signal_id)
                .filter(UserTradeORM.userid == userid)
                .filter(UserTradeORM.signal_id.in_(clean_ids))
                .distinct()
                .all()
            )
        return {str(row[0]) for row in rows if row and row[0]}

    @staticmethod
    def expire_ready_entries_for_signal(
        *,
        userid: str,
        signal_id: str,
        reason: str,
        ts: datetime,
    ) -> int:
        """Expire all not-yet-submitted READY/CREATED sibling legs atomically."""
        userid = str(userid or "").strip()
        signal_id = str(signal_id or "").strip()
        when = _to_ist_naive(ts)
        if not userid or not signal_id or when is None:
            return 0
        with get_trades_db() as db:
            rows = (
                db.query(UserTradeORM)
                .filter(UserTradeORM.userid == userid)
                .filter(UserTradeORM.signal_id == signal_id)
                .filter(UserTradeORM.entry_status.in_([
                    EntryStatus.CREATED.value,
                    EntryStatus.READY.value,
                ]))
                .all()
            )
            for row in rows:
                row.entry_status = EntryStatus.EXPIRED.value
                row.exec_last_checked_at = when
                row.exec_status = "ENTRY_EXPIRED"
                row.exec_status_message = str(reason or "ENTRY_EXPIRED")[:255]
            db.commit()
            return len(rows)

    @staticmethod
    def fetch_user_trade_by_id(id: int) -> Optional["UserTradeSchema"]:
        with get_trades_db() as db:
            rec = db.get(UserTradeORM, id)
        return UserTradeSchema.model_validate(rec) if rec else None

    @staticmethod
    def fetch_user_trade_by_id_strict(id: int) -> "UserTradeSchema":
        trade_id = int(id)
        if trade_id <= 0:
            raise ValueError("user_trade id must be positive")
        with get_trades_db() as db:
            rec = db.get(UserTradeORM, trade_id)
        if rec is None:
            raise LookupError(f"UserTrade[id={trade_id}] not found")
        return UserTradeSchema.model_validate(rec)

    @staticmethod
    def update_user_trade_by_id_strict(id: int, updates: dict) -> "UserTradeSchema":
        trade_id = int(id)
        if trade_id <= 0:
            raise ValueError("user_trade id must be positive")
        if not isinstance(updates, dict) or not updates:
            raise ValueError("non-empty user_trade updates dict is required")
        clean = dict(updates)
        for key in (
            "instrument_type", "trade_type", "entry_status", "exit_status", "position_style"
        ):
            if key in clean and hasattr(clean[key], "value"):
                clean[key] = clean[key].value
        if "entry_snapshot" in clean:
            clean["entry_snapshot"] = sanitize_json(clean["entry_snapshot"])
        if "last_snapshot" in clean:
            clean["last_snapshot"] = sanitize_json(clean["last_snapshot"])
        if "trade_management" in clean:
            clean["trade_management"] = _normalize_trade_management(clean["trade_management"])
        with get_trades_db() as db:
            rec = db.get(UserTradeORM, trade_id)
            if rec is None:
                raise LookupError(f"UserTrade[id={trade_id}] not found")
            for key, value in list(clean.items()):
                if isinstance(value, datetime):
                    clean[key] = _to_ist_naive(value)
            for key, value in clean.items():
                if not hasattr(rec, key):
                    raise AttributeError(f"UserTradeORM has no field {key!r}")
                setattr(rec, key, value)
            try:
                db.commit()
                db.refresh(rec)
            except Exception:
                db.rollback()
                raise
        return UserTradeSchema.model_validate(rec)

    @staticmethod
    def fetch_user_trades_by_user(userid: str) -> List["UserTradeSchema"]:
        with get_trades_db() as db:
            rows = (
                db.query(UserTradeORM)
                .filter(UserTradeORM.userid == userid)
                .order_by(UserTradeORM.entry_time.desc(), UserTradeORM.id.desc())
                .all()
            )
        return [UserTradeSchema.model_validate(r) for r in rows]

    @staticmethod
    def update_user_trade_by_id(id: int, updates: dict) -> Optional["UserTradeSchema"]:
        for k in (
            "instrument_type", "trade_type",
            "entry_status", "exit_status",
            "position_style",
        ):
            if k in updates and hasattr(updates[k], "value"):
                updates[k] = updates[k].value

        if "entry_snapshot" in updates:
            updates["entry_snapshot"] = sanitize_json(updates.get("entry_snapshot") or {})
        if "last_snapshot" in updates:
            updates["last_snapshot"] = sanitize_json(updates.get("last_snapshot"))
        if "trade_management" in updates:
            updates["trade_management"] = _normalize_trade_management(updates.get("trade_management"))

        with get_trades_db() as db:
            rec = db.get(UserTradeORM, id)
            if not rec:
                logger.info("UserTrade[id=%s] not found for update", id)
                return None

            for k, v in list(updates.items()):
                if isinstance(v, datetime):
                    updates[k] = _to_ist_naive(v)

            for k, v in updates.items():
                setattr(rec, k, v)

            try:
                db.commit()
                db.refresh(rec)
                return UserTradeSchema.model_validate(rec)
            except Exception as e:
                db.rollback()
                logger.error("Error updating UserTrade[id=%s]: %s", id, e, exc_info=True)
                return None

    @staticmethod
    def cancel_user_trade_by_id(
        id: int,
        *,
        reason: str = "MANUAL",
        ts: datetime,
        rule: str = "cancel_user_trade_by_id",
    ) -> bool:
        with get_trades_db() as db:
            rec = db.get(UserTradeORM, id)
            if not rec:
                return False

            rec.entry_status = EntryStatus.CANCELLED.value
            rec.exit_reason = reason
            rec.exit_rule = rule
            rec.exit_intent_time = _to_ist_naive(ts)
            rec.exit_time = _to_ist_naive(ts)

            db.commit()
            return True

    # ---------------------------------------------------------------------
    # Queries for services
    # ---------------------------------------------------------------------
    @staticmethod
    def fetch_for_executor_pickup(*, execution_mode: str = "REAL", limit: int = 200) -> List["UserTradeSchema"]:
        exec_mode = (execution_mode or "").upper()
        if exec_mode not in ("REAL", "VIRTUAL"):
            raise ValueError(f"invalid execution_mode: {execution_mode!r}")

        with get_trades_db() as db:
            rows = (
                db.query(UserTradeORM)
                .filter(UserTradeORM.execution_mode == exec_mode)
                .filter(UserTradeORM.entry_status == EntryStatus.READY.value)
                .order_by(UserTradeORM.entry_time.asc(), UserTradeORM.id.asc())
                .limit(int(limit))
                .all()
            )
        return [UserTradeSchema.model_validate(r) for r in rows]

    @staticmethod
    def fetch_open_positions(*, userid: Optional[str] = None, symbol: Optional[str] = None) -> List["UserTradeSchema"]:
        with get_trades_db() as db:
            q = (
                db.query(UserTradeORM)
                .filter(UserTradeORM.entry_status == EntryStatus.FILLED.value)
                .filter(or_(UserTradeORM.exit_status.is_(None), UserTradeORM.exit_status != ExitStatus.FILLED.value))
            )
            if userid:
                q = q.filter(UserTradeORM.userid == userid)
            if symbol:
                q = q.filter(UserTradeORM.symbol == symbol)

            rows = q.order_by(UserTradeORM.entry_time.asc(), UserTradeORM.id.asc()).all()

        return [UserTradeSchema.model_validate(r) for r in rows]

    @staticmethod

    @staticmethod
    def fetch_active_trades_for_signal(*, userid: str, signal_id: str) -> List["UserTradeSchema"]:
        """Return all non-terminal trades for the same user + signal_id.

        Used by trade monitor to keep hedged/sibling legs in sync when a
        primary EQ/FUT leg exits by adaptive/lifecycle logic.  This includes
        CREATED/READY/SUBMITTED/FILLED entries and excludes only terminal exit
        states.
        """
        userid = str(userid or "").strip()
        signal_id = str(signal_id or "").strip()
        if not userid or not signal_id:
            return []

        active_entry_statuses = [
            EntryStatus.CREATED.value,
            EntryStatus.READY.value,
            EntryStatus.SUBMITTED.value,
            EntryStatus.FILLED.value,
        ]
        terminal_exit_statuses = [
            ExitStatus.FILLED.value,
            ExitStatus.CANCELLED.value,
        ]
        open_exit_filter = or_(
            UserTradeORM.exit_status.is_(None),
            UserTradeORM.exit_status == "",
            UserTradeORM.exit_status == ExitStatus.NONE.value,
            ~UserTradeORM.exit_status.in_(terminal_exit_statuses),
        )

        with get_trades_db() as db:
            rows = (
                db.query(UserTradeORM)
                .filter(UserTradeORM.userid == userid)
                .filter(UserTradeORM.signal_id == signal_id)
                .filter(UserTradeORM.entry_status.in_(active_entry_statuses))
                .filter(open_exit_filter)
                .order_by(UserTradeORM.entry_time.asc(), UserTradeORM.id.asc())
                .all()
            )

        return [UserTradeSchema.model_validate(r) for r in rows]

    @staticmethod
    def mark_sibling_trades_exit_for_signal(
        *,
        userid: str,
        signal_id: str,
        exclude_trade_id: Optional[int],
        reason: str,
        rule: str,
        ts: Optional[datetime] = None,
    ) -> List["UserTradeSchema"]:
        """Mark open sibling legs of an signal for exit.

        This is intentionally signal-scoped, not equity_ref-scoped, so an
        adaptive stop on one hedged trade set does not flatten unrelated signals
        for the same underlying.  It is used when a primary EQ/FUT leg exits and
        the remaining option/derivative hedge legs must not be orphaned.
        """
        userid = str(userid or "").strip()
        signal_id = str(signal_id or "").strip()
        if not userid or not signal_id:
            return []

        active_entry_statuses = [
            EntryStatus.CREATED.value,
            EntryStatus.READY.value,
            EntryStatus.SUBMITTED.value,
            EntryStatus.FILLED.value,
        ]
        terminal_exit_statuses = [
            ExitStatus.FILLED.value,
            ExitStatus.CANCELLED.value,
        ]
        open_exit_filter = or_(
            UserTradeORM.exit_status.is_(None),
            UserTradeORM.exit_status == "",
            UserTradeORM.exit_status == ExitStatus.NONE.value,
            ~UserTradeORM.exit_status.in_(terminal_exit_statuses),
        )
        mark_ts = _to_ist_naive(ts or datetime.now(IST))

        with get_trades_db() as db:
            q = (
                db.query(UserTradeORM)
                .filter(UserTradeORM.userid == userid)
                .filter(UserTradeORM.signal_id == signal_id)
                .filter(UserTradeORM.entry_status.in_(active_entry_statuses))
                .filter(open_exit_filter)
            )
            if exclude_trade_id is not None:
                q = q.filter(UserTradeORM.id != exclude_trade_id)

            rows = q.order_by(UserTradeORM.entry_time.asc(), UserTradeORM.id.asc()).all()

            updated = []
            for rec in rows:
                if rec.exit_status not in (ExitStatus.READY.value, ExitStatus.SUBMITTED.value):
                    rec.exit_status = ExitStatus.READY.value
                rec.exit_reason = reason
                rec.exit_rule = rule
                rec.exit_intent_time = mark_ts
                rec.exit_time = mark_ts
                # Do not invent a primary-leg price for a sibling derivative.
                # Prefer the sibling's own last observed price if available;
                # otherwise executor/reconcile will determine the fill price.
                if getattr(rec, "exit_price", None) in (None, 0, ""):
                    last_price = getattr(rec, "last_price", None)
                    if last_price not in (None, 0, ""):
                        rec.exit_price = last_price
                updated.append(rec)

            db.commit()
            for rec in updated:
                db.refresh(rec)

            return [UserTradeSchema.model_validate(r) for r in updated]

    def fetch_active_trades_for_user_equity_ref(*, userid: str, equity_ref: str) -> List["UserTradeSchema"]:
        """
        Return all non-terminal trades for the same user + underlying/equity_ref.

        This intentionally includes CREATED/READY/SUBMITTED/FILLED entries so
        auto generation does not create another lifecycle/side while an earlier
        position is still pending, filled, or waiting for exit execution.
        """
        userid = str(userid or "").strip()
        equity_ref = str(equity_ref or "").strip().upper()
        if not userid or not equity_ref:
            return []

        active_entry_statuses = [
            EntryStatus.CREATED.value,
            EntryStatus.READY.value,
            EntryStatus.SUBMITTED.value,
            EntryStatus.FILLED.value,
        ]
        terminal_exit_statuses = [
            ExitStatus.FILLED.value,
            ExitStatus.CANCELLED.value,
        ]

        open_exit_filter = or_(
            UserTradeORM.exit_status.is_(None),
            UserTradeORM.exit_status == "",
            UserTradeORM.exit_status == ExitStatus.NONE.value,
            ~UserTradeORM.exit_status.in_(terminal_exit_statuses),
        )

        with get_trades_db() as db:
            rows = (
                db.query(UserTradeORM)
                .filter(UserTradeORM.userid == userid)
                .filter(UserTradeORM.entry_status.in_(active_entry_statuses))
                .filter(open_exit_filter)
                .filter(
                    or_(
                        UserTradeORM.equity_ref == equity_ref,
                        UserTradeORM.symbol == equity_ref,
                    )
                )
                .order_by(UserTradeORM.entry_time.asc(), UserTradeORM.id.asc())
                .all()
            )

        return [UserTradeSchema.model_validate(r) for r in rows]


    @staticmethod
    def fetch_for_active_context_day(
        *,
        equity_ref: str,
        trading_day: date,
    ) -> List[UserTradeActiveContextRow]:
        """Load narrow trade projections whose lifetime can overlap one day.

        Entry/exit activity is resolved causally by the caller.  Large snapshot
        and trade-management JSON columns are intentionally excluded, and SQL
        ordering is avoided because the provider sorts the small cached result
        in memory.
        """
        equity_ref = str(equity_ref or "").strip().upper()
        if not equity_ref:
            return []
        day_start = datetime.combine(trading_day, time.min)
        day_end = datetime.combine(trading_day + timedelta(days=1), time.min)
        try:
            with get_trades_db() as db:
                rows = (
                    db.query(
                        UserTradeORM.id,
                        UserTradeORM.signal_id,
                        UserTradeORM.symbol,
                        UserTradeORM.equity_ref,
                        UserTradeORM.trade_type,
                        UserTradeORM.entry_status,
                        UserTradeORM.exit_status,
                        UserTradeORM.entry_time,
                        UserTradeORM.exit_time,
                    )
                    .filter(
                        or_(
                            UserTradeORM.equity_ref == equity_ref,
                            UserTradeORM.symbol == equity_ref,
                        )
                    )
                    .filter(UserTradeORM.entry_time < day_end)
                    .filter(
                        or_(
                            UserTradeORM.exit_time.is_(None),
                            UserTradeORM.exit_time > day_start,
                        )
                    )
                    .all()
                )
            return [
                UserTradeActiveContextRow.model_validate(dict(row._mapping))
                for row in rows
            ]
        except Exception:
            logger.exception(
                "fetch_for_active_context_day failed | equity_ref=%s day=%s",
                equity_ref, trading_day,
            )
            return []

    @staticmethod
    def fetch_active_trades_for_equity_ref(*, equity_ref: str) -> List["UserTradeSchema"]:
        """
        Return all non-terminal trades for an underlying/equity_ref across users.

        Signal generation is symbol-level, not user-level. This helper lets the
        lifecycle engine know that a trade is already active for the same
        underlying so signal replacement/invalidation can be trade-aware.
        """
        equity_ref = str(equity_ref or "").strip().upper()
        if not equity_ref:
            return []

        active_entry_statuses = [
            EntryStatus.CREATED.value,
            EntryStatus.READY.value,
            EntryStatus.SUBMITTED.value,
            EntryStatus.FILLED.value,
        ]
        terminal_exit_statuses = [
            ExitStatus.FILLED.value,
            ExitStatus.CANCELLED.value,
        ]

        open_exit_filter = or_(
            UserTradeORM.exit_status.is_(None),
            UserTradeORM.exit_status == "",
            UserTradeORM.exit_status == ExitStatus.NONE.value,
            ~UserTradeORM.exit_status.in_(terminal_exit_statuses),
        )

        with get_trades_db() as db:
            rows = (
                db.query(UserTradeORM)
                .filter(UserTradeORM.entry_status.in_(active_entry_statuses))
                .filter(open_exit_filter)
                .filter(
                    or_(
                        UserTradeORM.equity_ref == equity_ref,
                        UserTradeORM.symbol == equity_ref,
                    )
                )
                .order_by(UserTradeORM.entry_time.asc(), UserTradeORM.id.asc())
                .all()
            )

        return [UserTradeSchema.model_validate(r) for r in rows]

    @staticmethod
    def mark_active_trades_exit_for_user_equity_ref(
        *,
        userid: str,
        equity_ref: str,
        reason: str,
        rule: str = "opposite_signal",
        ts: Optional[datetime] = None,
    ) -> List["UserTradeSchema"]:
        """Mark every active/pending trade for this user + underlying for exit.

        Used by AUTOGEN when an opposite-side signal appears. The new trade
        must not be created in the same generator pass; executor/monitor gets
        a chance to flatten the old EQ/FUT/CE/PE family first.
        """
        userid = str(userid or "").strip()
        equity_ref = str(equity_ref or "").strip().upper()
        if not userid or not equity_ref:
            return []

        active_entry_statuses = [
            EntryStatus.CREATED.value,
            EntryStatus.READY.value,
            EntryStatus.SUBMITTED.value,
            EntryStatus.FILLED.value,
        ]
        terminal_exit_statuses = [
            ExitStatus.FILLED.value,
            ExitStatus.CANCELLED.value,
        ]
        open_exit_filter = or_(
            UserTradeORM.exit_status.is_(None),
            UserTradeORM.exit_status == "",
            UserTradeORM.exit_status == ExitStatus.NONE.value,
            ~UserTradeORM.exit_status.in_(terminal_exit_statuses),
        )
        mark_ts = _to_ist_naive(ts or datetime.now(IST))

        with get_trades_db() as db:
            rows = (
                db.query(UserTradeORM)
                .filter(UserTradeORM.userid == userid)
                .filter(UserTradeORM.entry_status.in_(active_entry_statuses))
                .filter(open_exit_filter)
                .filter(
                    or_(
                        UserTradeORM.equity_ref == equity_ref,
                        UserTradeORM.symbol == equity_ref,
                    )
                )
                .order_by(UserTradeORM.entry_time.asc(), UserTradeORM.id.asc())
                .all()
            )

            updated = []
            for rec in rows:
                if rec.exit_status not in (ExitStatus.READY.value, ExitStatus.SUBMITTED.value):
                    rec.exit_status = ExitStatus.READY.value
                rec.exit_reason = reason
                rec.exit_rule = rule
                if hasattr(rec, "exit_intent_time"):
                    rec.exit_intent_time = mark_ts
                updated.append(rec)

            db.commit()
            for rec in updated:
                db.refresh(rec)

            return [UserTradeSchema.model_validate(r) for r in updated]

    @staticmethod
    def exists_for_signal_user_symbol(userid: str, signal_id: str, symbol: str) -> bool:
        with get_trades_db() as db:
            return (
                db.query(UserTradeORM)
                .filter_by(userid=userid, signal_id=signal_id, symbol=symbol)
                .first()
                is not None
            )

    @staticmethod
    def get_perf_summary_for_users(
        userids: List[str],
        execution_mode: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: int = 5000,
        offset: int = 0,
    ) -> List[Dict]:
        exec_mode = (execution_mode or "").upper()
        if exec_mode and exec_mode not in ("REAL", "VIRTUAL"):
            raise ValueError(f"invalid execution_mode: {execution_mode!r}")

        clean_userids = [
            str(u).strip()
            for u in (userids or [])
            if str(u or "").strip()
        ]

        where = [
            "trade_type IN ('BUY','SELL')",
            "entry_status = 'FILLED'",
        ]

        params = {
            "limit": int(limit),
            "offset": int(offset),
        }

        bind_params = []

        if exec_mode:
            where.insert(0, "execution_mode = :exec_mode")
            params["exec_mode"] = exec_mode

        if clean_userids:
            where.insert(0, "userid IN :userids")
            params["userids"] = clean_userids
            bind_params.append(bindparam("userids", expanding=True))

        if date_from and date_to:
            where.append("trading_date BETWEEN :date_from AND :date_to")
            params["date_from"] = date_from
            params["date_to"] = date_to
        elif date_from:
            where.append("trading_date >= :date_from")
            params["date_from"] = date_from
        elif date_to:
            where.append("trading_date <= :date_to")
            params["date_to"] = date_to

        sql = text(f"""
            SELECT
                id, userid, symbol, equity_ref, instrument_type, trade_type,
                signal_id, source, execution_mode,
                entry_status, exit_status,
                entry_time, entry_price, executed_entry_price, executed_entry_qty,
                last_time, last_price, last_pnl, last_pnl_value,
                exit_time, exit_price, executed_exit_price, executed_exit_qty, exit_pnl,
                quantity, message AS setup
            FROM user_trades_history
            WHERE {" AND ".join(where)}
            ORDER BY userid ASC, entry_time DESC, id DESC
            LIMIT :limit OFFSET :offset
        """)

        if bind_params:
            sql = sql.bindparams(*bind_params)

        def _f(x):
            try:
                return float(x) if x is not None else None
            except Exception:
                return None

        def _i(x):
            try:
                return int(x) if x is not None else 0
            except Exception:
                return 0

        def _s(x):
            return x.isoformat() if hasattr(x, "isoformat") else (str(x) if x is not None else None)

        with get_trades_db() as db:
            rows = db.execute(sql, params).mappings().all()

        out: List[Dict] = []

        for r in rows:
            entry_status = str(r.get("entry_status") or "").upper()
            exit_status = str(r.get("exit_status") or "").upper()
            trade_status = exit_status if exit_status and exit_status != "NONE" else entry_status

            pnl = _f(r.get("exit_pnl"))
            if pnl is None:
                pnl = _f(r.get("last_pnl_value"))
            if pnl is None:
                pnl = _f(r.get("last_pnl"))
            if pnl is None:
                pnl = 0.0

            out.append({
                "id": r.get("id"),
                "userid": r.get("userid"),

                "symbol": r.get("symbol"),
                "equity_ref": r.get("equity_ref"),
                "instrument_type": r.get("instrument_type"),
                "trade_type": r.get("trade_type"),

                "signal_id": r.get("signal_id"),
                "source": r.get("source"),
                "execution_mode": r.get("execution_mode"),

                "entry_status": entry_status,
                "exit_status": exit_status,
                "trade_status": trade_status,

                "entry_time": _s(r.get("entry_time")),
                "entry_price": _f(r.get("entry_price")),
                "executed_entry_price": _f(r.get("executed_entry_price")),
                "executed_entry_qty": _i(r.get("executed_entry_qty")),

                "last_time": _s(r.get("last_time")),
                "last_price": _f(r.get("last_price")),
                "last_pnl": _f(r.get("last_pnl")),
                "last_pnl_value": _f(r.get("last_pnl_value")),

                "exit_time": _s(r.get("exit_time")),
                "exit_price": _f(r.get("exit_price")),
                "executed_exit_price": _f(r.get("executed_exit_price")),
                "executed_exit_qty": _i(r.get("executed_exit_qty")),
                "exit_pnl": _f(r.get("exit_pnl")),

                "pnl": pnl,
                "pnl_value": pnl,

                "quantity": _i(r.get("quantity")),
                "qty": _i(r.get("quantity")),

                "setup": r.get("setup"),
            })

        return out

    @staticmethod
    def archive_current_rows():
        """Archive all current trades and verify their history identity."""
        from schemas.archive import ArchiveSpec, archive_rows

        return archive_rows(
            ArchiveSpec(
                name="user_trades",
                source_model=UserTradeORM,
                history_model=UserTradeHistoryORM,
                excluded_target_columns=frozenset(
                    {"hist_id", "archived_on", "trading_date"}
                ),
                verification_condition=lambda source, target: and_(
                    target.c.id == source.c.id,
                    target.c.trading_date == func.date(source.c.entry_time),
                ),
            )
        )
