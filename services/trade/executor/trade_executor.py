#!/usr/bin/env python3
"""
services/execution/trade_executor.py

Rules
-----
- Persist ALL timestamps to DB as naive IST wall-clock datetimes.
- REAL fills must come only from broker truth (order history average_price).
- VIRTUAL fills use app pricing (LTP / snapshot / planned fallback).
- If user requests EXIT before entry is filled, interpret that as CANCEL ENTRY.
- LIMIT orders use live best bid/ask pricing and a modify-price retry ladder.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

from config import AppConfig
from configs.execution_config import EXECUTION_CONFIG
from configs.intraday_lifecycle_config import INTRADAY_LIFECYCLE_CONFIG
from database.database import get_trades_db
from models.trade_models import Snapshot as SnapshotORM
from models.trade_models import UserTrade as UserTradeORM

from enums.enums import (
    EntryStatus,
    ExitStatus,
    OrderStatus,
    OrderType,
    OrderVariety,
    TradeType,
)

from schemas.user_trade import UserTradeSchema
from schemas.user import UserSchema
from schemas.snapshot import SnapshotSchema
from schemas.signal import SignalSchema
from schemas.orderprofile import OrderProfileSchema
from services.trade.monitor.signal_contract import AuctionTradeSignalContext
from services.trade.monitor.trademon_helper import TradeMonHelper
from services.trade.generator.tradegen_helper import TradeGenHelper
from services.audit.auditlog import write_auditlog

from services.zerodha.kiteconnect_service import KiteConnectService
from utils.datetime_utils import IST
from utils.intraday_lifecycle import cutoff_due

logger = logging.getLogger(__name__)

MAX_RETRIES = int(EXECUTION_CONFIG.max_retries)
# Keep module-level defaults for compatibility, but read execution flags dynamically
# in the virtual/replay price path. Replay scripts may set EXECUTION_CONFIG.use_snapshot
# after this module is imported; frozen constants would ignore that and corrupt
# replay timestamps/prices with live/latest values.
USE_SNAPSHOT_FOR_VIRTUAL = bool(EXECUTION_CONFIG.use_snapshot)
USE_LIVE_PRICE_FOR_VIRTUAL = bool(EXECUTION_CONFIG.use_live_price_for_virtual)

def _execution_use_snapshot_for_virtual() -> bool:
    try:
        return bool(getattr(EXECUTION_CONFIG, "use_snapshot", USE_SNAPSHOT_FOR_VIRTUAL))
    except Exception:
        return bool(USE_SNAPSHOT_FOR_VIRTUAL)


def _execution_use_live_price_for_virtual() -> bool:
    try:
        return bool(getattr(EXECUTION_CONFIG, "use_live_price_for_virtual", USE_LIVE_PRICE_FOR_VIRTUAL))
    except Exception:
        return bool(USE_LIVE_PRICE_FOR_VIRTUAL)

ORDER_POLL_INTERVAL_SEC = float(EXECUTION_CONFIG.order_poll_interval_sec)
ORDER_POLL_TIMEOUT_SEC = float(EXECUTION_CONFIG.order_poll_timeout_sec)
MAX_REPRICES_PER_PASS = int(EXECUTION_CONFIG.max_reprices_per_pass)

TICK_SIZE = Decimal(str(EXECUTION_CONFIG.tick_size))
MAX_ENTRY_PRICE_DRIFT_OPTION = Decimal(
    str(EXECUTION_CONFIG.max_entry_price_drift_option_pct)
)
MAX_ENTRY_PRICE_DRIFT_DEFAULT = Decimal(
    str(EXECUTION_CONFIG.max_entry_price_drift_default_pct)
)

SIGNAL_LINKED_SOURCES = {
    "AUTOGEN",
    "TRADE_GENERATOR",
    "MANUAL_SIGNAL",
    "AUTOTRADES",
}
AUTO_SIGNAL_SOURCES = {"AUTOGEN", "TRADE_GENERATOR", "AUTOTRADES"}
NON_SIGNAL_SOURCES = {"WATCHLIST", "POSITION_ADD", "MANUAL"}

ENTRY_SIGNAL_GUARD_VALID = "VALID"
ENTRY_SIGNAL_GUARD_INVALID = "INVALID"
ENTRY_SIGNAL_GUARD_DEFER = "DEFER"
ENTRY_SIGNAL_GUARD_BYPASS = "BYPASS"

def _is_intraday_signal_cutoff_reason(reason: Any) -> bool:
    return str(reason or "").strip().upper().startswith(
        "ENTRY_CANCEL_INTRADAY_SIGNAL_CUTOFF"
    )


# -------------------------------------------------------------------
# time helpers
# -------------------------------------------------------------------

def _to_ist_naive(ts: Optional[datetime]) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, str):
        s = ts.strip()
        if not s:
            return None
        try:
            ts = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            try:
                ts = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                return None
    if not isinstance(ts, datetime):
        return None
    try:
        if ts.tzinfo is None:
            return ts.replace(tzinfo=None)
        return ts.astimezone(IST).replace(tzinfo=None)
    except Exception:
        return ts.replace(tzinfo=None)


def _now_ist_naive() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def _parse_kite_ts(x: Any) -> Optional[datetime]:
    if x is None:
        return None
    if isinstance(x, datetime):
        return _to_ist_naive(x)
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return _to_ist_naive(dt)
        except Exception:
            return None
    return None


def _broker_fill_time_from_history(hist: List[dict]) -> Optional[datetime]:
    if not hist:
        return None

    for row in reversed(hist):
        if not isinstance(row, dict):
            continue
        for k in ("exchange_timestamp", "order_timestamp", "timestamp", "exchange_time"):
            ts = _parse_kite_ts(row.get(k))
            if ts:
                return ts
    return None


# -------------------------------------------------------------------
# observability helpers
# -------------------------------------------------------------------

def _obs_checked_at_field(kind: str) -> str:
    return "reconcile_last_checked_at" if str(kind).lower() == "reconcile" else "exec_last_checked_at"


def _obs_status_field(kind: str) -> str:
    return "reconcile_status" if str(kind).lower() == "reconcile" else "exec_status"


def _obs_message_field(kind: str) -> str:
    return "reconcile_status_message" if str(kind).lower() == "reconcile" else "exec_status_message"


def _obs_update(kind: str, *, code: Optional[str], message: Optional[str], when: Optional[datetime] = None) -> Dict[str, Any]:
    ts = _to_ist_naive(when) or _now_ist_naive()
    return {
        _obs_checked_at_field(kind): ts,
        _obs_status_field(kind): code,
        _obs_message_field(kind): message,
    }


# -------------------------------------------------------------------
# tiny helpers
# -------------------------------------------------------------------

def d(x: Any) -> Decimal:
    if isinstance(x, Decimal):
        return x
    if x is None:
        return Decimal("0")
    return Decimal(str(x))


def _enum_str(x: Any) -> str:
    v = getattr(x, "value", x)
    return str(v).upper().strip()


def _is_real(ut: UserTradeSchema) -> bool:
    return _enum_str(getattr(ut, "execution_mode", "VIRTUAL")) == "REAL"


def _trade_source(ut: UserTradeSchema) -> str:
    return _enum_str(getattr(ut, "source", ""))


def _is_signal_linked_entry(ut: UserTradeSchema) -> bool:
    source = _trade_source(ut)
    signal_id = str(getattr(ut, "signal_id", "") or "").strip()
    if signal_id.upper().startswith("MANUAL:") or source in NON_SIGNAL_SOURCES:
        return False
    return source in SIGNAL_LINKED_SOURCES


def _entry_status(ut: UserTradeSchema) -> str:
    return _enum_str(getattr(ut, "entry_status", EntryStatus.CREATED.value))


def _exit_status(ut: UserTradeSchema) -> str:
    return _enum_str(getattr(ut, "exit_status", ExitStatus.NONE.value))


def _side(ut: UserTradeSchema) -> str:
    return _enum_str(getattr(ut, "trade_type", TradeType.BUY.value))


def _opposite_side(side: str) -> str:
    return TradeType.SELL.value if _enum_str(side) == TradeType.BUY.value else TradeType.BUY.value


def _try_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def _round_to_tick(px: Decimal) -> Decimal:
    if px <= 0:
        return Decimal("0")
    ticks = (px / TICK_SIZE).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return ticks * TICK_SIZE


def _extract_order_id(resp: Any) -> str:
    try:
        oid = KiteConnectService.extract_order_id(resp)
        return str(oid or "").strip()
    except Exception:
        return ""


def _entry_price_drift_limit(instrument_type: Any) -> Decimal:
    return (
        MAX_ENTRY_PRICE_DRIFT_OPTION
        if _enum_str(instrument_type) in ("CE", "PE")
        else MAX_ENTRY_PRICE_DRIFT_DEFAULT
    )


def _entry_price_drift_pct(
    planned_price: Any,
    executable_price: Any,
) -> Optional[Decimal]:
    planned = d(planned_price)
    executable = d(executable_price)
    if planned <= 0 or executable <= 0:
        return None
    return abs(executable - planned) / planned


def _extract_latest_order_price(hist: List[dict]) -> Optional[Decimal]:
    for row in reversed(hist or []):
        if not isinstance(row, dict):
            continue
        for key in ("price", "average_price", "trigger_price"):
            value = d(row.get(key))
            if value > 0:
                return value
    return None


def _extract_broker_status_message(hist: List[dict]) -> Optional[str]:
    for row in reversed(hist or []):
        if not isinstance(row, dict):
            continue
        for key in (
            "status_message_raw",
            "status_message",
            "rejection_reason",
            "message",
        ):
            value = str(row.get(key) or "").strip()
            if value:
                return value[:220]
    return None


def _json_safe_order_request(params: Dict[str, Any]) -> Dict[str, Any]:
    return {
        str(key): getattr(value, "value", value)
        for key, value in params.items()
    }


def _calc_exit_pnl_per_unit(ut: UserTradeSchema, exit_px: Any) -> Optional[Decimal]:
    entry_px = d(getattr(ut, "executed_entry_price", None) or getattr(ut, "entry_price", None) or 0)
    exit_px_d = d(exit_px or 0)

    if entry_px <= 0 or exit_px_d <= 0:
        return None

    if _side(ut) == TradeType.BUY.value:
        return exit_px_d - entry_px
    return entry_px - exit_px_d


def _calc_exit_pnl(ut: UserTradeSchema, exit_px: Any, qty: Any) -> Optional[Decimal]:
    per_unit = _calc_exit_pnl_per_unit(ut, exit_px)
    qty_i = _try_int(qty, 0)

    if per_unit is None or qty_i <= 0:
        return None

    return per_unit * Decimal(qty_i)


def _build_exit_fill_updates(
    ut: UserTradeSchema,
    *,
    exit_px: Any,
    qty: Any,
    when: Optional[datetime],
    status_code: Optional[str] = None,
    status_message: Optional[str] = None,
    obs_kind: str = "exec",
) -> Dict[str, Any]:
    when0 = _to_ist_naive(when) or _now_ist_naive()
    exit_px_d = d(exit_px or 0)
    if exit_px_d <= 0:
        raise ValueError("Exit fill price must be positive before marking FILLED")
    qty_i = _try_int(
        qty
        or getattr(ut, "executed_exit_qty", None)
        or getattr(ut, "executed_entry_qty", None)
        or getattr(ut, "quantity", 0)
        or 1,
        1,
    )
    if qty_i <= 0:
        raise ValueError("Exit fill quantity must be positive before marking FILLED")

    upd: Dict[str, Any] = {
        "exit_status": ExitStatus.FILLED.value,
        "executed_exit_price": float(exit_px_d),
        "executed_exit_qty": qty_i,
        "exit_exec_time": when0,
        "exit_time": _to_ist_naive(getattr(ut, "exit_time", None)) or when0,
        "last_time": when0,
        "last_price": float(exit_px_d),
    }
    upd.update(_obs_update(obs_kind, code=status_code, message=status_message, when=when0))

    exit_pnl = _calc_exit_pnl(ut, exit_px_d, qty_i)
    exit_pnl_per_unit = _calc_exit_pnl_per_unit(ut, exit_px_d)
    if exit_pnl is not None:
        upd["exit_pnl"] = float(exit_pnl)
        upd["last_pnl"] = float(exit_pnl_per_unit or Decimal("0"))
        upd["last_pnl_value"] = float(exit_pnl)

    return upd


# -------------------------------------------------------------------
# broker wrapper
# -------------------------------------------------------------------

_data_user_broker: Optional["ZerodhaBroker"] = None


class ZerodhaBroker:
    def __init__(self, user: UserSchema):
        self.user = user
        self.svc = KiteConnectService(api_key=user.apikey, access_token=user.access_token)

    def place_order(self, **params) -> dict:
        return self.svc.place_order(**params)

    def latest_status(self, order_id: str) -> Optional[OrderStatus]:
        return self.svc.fetch_latest_status_of_order(order_id)

    def history(self, order_id: str) -> List[dict]:
        return self.svc.fetch_order_history(order_id) or []


def _get_data_user_broker() -> Optional["ZerodhaBroker"]:
    global _data_user_broker

    if _data_user_broker is not None:
        return _data_user_broker

    data_userid = AppConfig.DATA_USER
    if not data_userid:
        return None

    u = UserSchema.fetch_user(str(data_userid))
    if not u:
        return None

    if not getattr(u, "apikey", None) or not getattr(u, "access_token", None):
        return None

    try:
        _data_user_broker = ZerodhaBroker(u)
        return _data_user_broker
    except Exception:
        logger.exception("TradeExecutor: failed to init DATA_USER broker for pricing")
        return None


# -------------------------------------------------------------------
# quote helpers
# -------------------------------------------------------------------

def _fetch_quote_record(symbol: str) -> Optional[dict]:
    if not symbol:
        return None

    broker = _get_data_user_broker()
    if not broker:
        return None

    svc = broker.svc
    keys = [f"NFO:{symbol}", f"NSE:{symbol}", symbol]

    for k in keys:
        try:
            q = svc.fetch_quote([k])
            if not q or not isinstance(q, dict):
                continue

            rec = q.get(k)
            if not isinstance(rec, dict):
                for _, v in q.items():
                    if isinstance(v, dict):
                        rec = v
                        break

            if isinstance(rec, dict):
                return rec
        except Exception:
            continue

    return None


def _quote_best_price(symbol: str, side: str) -> Optional[Decimal]:
    rec = _fetch_quote_record(symbol)
    if not rec:
        return None

    try:
        best_bid, best_ask, ltp = KiteConnectService.best_bid_ask_ltp_from_quote_record(rec)

        best_bid_d = d(best_bid) if best_bid is not None else None
        best_ask_d = d(best_ask) if best_ask is not None else None
        ltp_d = d(ltp) if ltp is not None else None

        if best_bid_d is not None and best_bid_d <= 0:
            best_bid_d = None
        if best_ask_d is not None and best_ask_d <= 0:
            best_ask_d = None
        if ltp_d is not None and ltp_d <= 0:
            ltp_d = None

        if _enum_str(side) == TradeType.BUY.value:
            return best_ask_d or ltp_d
        return best_bid_d or ltp_d
    except Exception:
        return None


def _best_limit_price(symbol: str, side: str, planned_price: Any) -> Decimal:
    px = _quote_best_price(symbol, side)
    if px is not None and px > 0:
        return _round_to_tick(px)

    p = d(planned_price)
    return _round_to_tick(p) if p > 0 else Decimal("0")


def _next_modified_limit_price(symbol: str, side: str, old_price: Any) -> Optional[Decimal]:
    live_px = _quote_best_price(symbol, side)
    if live_px is None or live_px <= 0:
        return None

    old_px = d(old_price)
    live_px = _round_to_tick(live_px)

    if _enum_str(side) == TradeType.BUY.value:
        target = max(live_px, _round_to_tick(old_px + TICK_SIZE) if old_px > 0 else live_px)
    else:
        target = min(live_px, _round_to_tick(old_px - TICK_SIZE) if old_px > 0 else live_px)

    if target <= 0:
        return None

    if old_px > 0 and target == _round_to_tick(old_px):
        return None

    return _round_to_tick(target)


def _latest_snapshot_record(symbol: str, *, asof_time: datetime) -> Optional[Any]:
    symbol0 = str(symbol or "").strip().upper()
    asof = _to_ist_naive(asof_time)
    if not symbol0 or asof is None:
        return None

    with get_trades_db() as db:
        return (
            db.query(SnapshotORM)
            .filter(SnapshotORM.symbol == symbol0)
            .filter(SnapshotORM.snapshot_time <= asof)
            .order_by(SnapshotORM.snapshot_time.desc())
            .first()
        )


def _latest_underlying_snapshot_price(
    symbol: str,
    *,
    asof_time: datetime,
) -> Tuple[Optional[Decimal], Optional[datetime], Optional[str]]:
    """Read the latest completed underlying snapshot price as-of executor time.

    Entry eligibility uses the persisted snapshot LTP first and falls back only
    to that same snapshot's close.  It never substitutes a trade-leg price.
    """
    rec = _latest_snapshot_record(symbol, asof_time=asof_time)
    if rec is None:
        return None, None, None

    snapshot_ts = _to_ist_naive(getattr(rec, "snapshot_time", None))
    ltp = d(getattr(rec, "ltp", None))
    if ltp > 0:
        return ltp, snapshot_ts, "LTP"

    payload = getattr(rec, "data", None)
    if not isinstance(payload, dict) or "close" not in payload:
        return None, snapshot_ts, None
    close = d(payload["close"])
    if close <= 0:
        return None, snapshot_ts, None
    return close, snapshot_ts, "CLOSE"


def _derivative_price_from_snapshot_payload(
    payload: Any,
    *,
    trade_symbol: str,
    instrument_type: Any,
) -> Optional[Decimal]:
    """Resolve one exact derivative quote from an underlying snapshot.

    The resolver never substitutes ATM/edge contracts or another strike.  The
    persisted trade symbol must match the snapshot derivative instrument.
    """
    if not isinstance(payload, dict):
        return None
    derivatives = payload.get("derivatives")
    if not isinstance(derivatives, dict):
        return None

    symbol0 = str(trade_symbol or "").strip().upper()
    inst = _enum_str(instrument_type)
    if not symbol0 or inst not in {"FUT", "CE", "PE"}:
        return None

    if inst == "FUT":
        future = derivatives.get("future")
        if not isinstance(future, dict):
            return None
        future_symbol = str(future.get("instrument") or future.get("symbol") or "").strip().upper()
        price = d(future.get("last_price"))
        return price if future_symbol == symbol0 and price > 0 else None

    ladder = derivatives.get("option_ladder")
    if isinstance(ladder, dict):
        bucket = "calls" if inst == "CE" else "puts"
        rows = ladder.get(bucket)
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                row_symbol = str(row.get("symbol") or "").strip().upper()
                price = d(row.get("ltp"))
                if row_symbol == symbol0 and price > 0:
                    return price

    lite = derivatives.get("options_lite")
    if isinstance(lite, dict):
        bucket = "top_calls" if inst == "CE" else "top_puts"
        rows = lite.get(bucket)
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                row_symbol = str(row.get("symbol") or "").strip().upper()
                price = d(row.get("ltp"))
                if row_symbol == symbol0 and price > 0:
                    return price
    return None


# -------------------------------------------------------------------
# VIRTUAL pricing helpers
# -------------------------------------------------------------------

def _virtual_fill_price_time(
    symbol: str,
    side: str,
    planned_price: Optional[Any],
    planned_time: Optional[datetime] = None,
    asof_time: Optional[datetime] = None,
    *,
    equity_ref: Optional[str] = None,
    instrument_type: Optional[Any] = None,
    last_known_price: Optional[Any] = None,
) -> Tuple[Decimal, datetime]:
    """Return deterministic fill price/time for virtual and replay execution.

    Replay/backtest sets EXECUTION_CONFIG.use_snapshot=True before running. That
    flag must be respected dynamically, because this module may already be
    imported. In snapshot mode we use the snapshot as-of the executor/replay
    clock, not the original intent time. This keeps delayed-intent validation
    honest while preserving deterministic market/replay timestamps.

    Live virtual mode may still use live quotes when use_snapshot=False and
    use_live_price_for_virtual=True.
    """
    execution_ts = _to_ist_naive(asof_time) or _now_ist_naive()
    pp = d(planned_price) if planned_price is not None else Decimal("0")

    if _execution_use_snapshot_for_virtual():
        snap = None
        try:
            snap = SnapshotSchema.fetch_latest_for_symbol_asof(symbol, execution_ts)
        except Exception:
            snap = None
        if snap:
            px = getattr(snap, "close", None)
            ts = getattr(snap, "snapshot_time", None)
            if px is not None and d(px) > 0:
                return d(px), (_to_ist_naive(ts) or execution_ts)

        # Derivative legs are priced from the exact symbol embedded in the
        # latest completed underlying snapshot.  They generally do not have a
        # separate snapshots row of their own.
        inst = _enum_str(instrument_type)
        underlying = str(equity_ref or "").strip().upper()
        if inst in {"FUT", "CE", "PE"} and underlying:
            rec = _latest_snapshot_record(underlying, asof_time=execution_ts)
            if rec is not None:
                px = _derivative_price_from_snapshot_payload(
                    getattr(rec, "data", None),
                    trade_symbol=symbol,
                    instrument_type=inst,
                )
                if px is not None and px > 0:
                    ts = _to_ist_naive(getattr(rec, "snapshot_time", None))
                    return px, (ts or execution_ts)

        # Planned and last-monitored prices are deterministic replay fallbacks.
        if pp > 0:
            return pp, execution_ts
        last_px = d(last_known_price)
        if last_px > 0:
            return last_px, execution_ts

    if _execution_use_live_price_for_virtual():
        px = _quote_best_price(symbol, side)
        if px is not None and px > 0:
            return px, _now_ist_naive()

        try:
            broker = _get_data_user_broker()
            if broker:
                ltp = broker.svc.fetch_latest_price(f"NSE:{symbol}")
                if ltp is not None and d(ltp) > 0:
                    return d(ltp), _now_ist_naive()
        except Exception:
            pass

    return (pp if pp > 0 else Decimal("0")), execution_ts


def _as_dict_payload(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if hasattr(raw, "model_dump"):
        value = raw.model_dump(mode="python")
        return value if isinstance(value, dict) else {}
    return {}


def _entry_fill_updates(
    ut: UserTradeSchema,
    *,
    fill_price: Any,
    fill_qty: Any,
    when: datetime,
    reconcile_message: str,
) -> Dict[str, Any]:
    px = d(fill_price)
    qty = _try_int(fill_qty, 0) or 1
    tm = TradeMonHelper.rebase_trade_management_after_fill(
        raw=getattr(ut, "trade_management", None),
        side=getattr(ut, "trade_type", None),
        instrument_type=getattr(ut, "instrument_type", None),
        planned_entry_price=getattr(ut, "entry_price", None),
        executed_entry_price=px,
        asof_time=when,
    )
    return {
        "entry_status": EntryStatus.FILLED.value,
        "executed_entry_price": px,
        "executed_entry_qty": qty,
        "entry_exec_time": when,
        "last_time": when,
        "last_price": px,
        "last_pnl": 0,
        "last_pnl_value": 0,
        "max_price": px,
        "min_price": px,
        "max_time": when,
        "min_time": when,
        "trade_management": tm,
        "entry_reconciled_at": when,
        **_obs_update(
            "reconcile",
            code="ENTRY_RECONCILED_FROM_EXECUTOR",
            message=reconcile_message,
            when=when,
        ),
        "exec_last_checked_at": when,
        "exec_status": None,
        "exec_status_message": None,
    }


def _executor_state(ut: Any) -> str:
    return f"ENTRY:{_entry_status(ut)}|EXIT:{_exit_status(ut)}"


def _executor_transition_action(ut: Any, updates: Dict[str, Any]) -> Optional[str]:
    old_entry = _entry_status(ut)
    old_exit = _exit_status(ut)
    new_entry = (
        _enum_str(updates.get("entry_status"))
        if updates.get("entry_status") is not None
        else old_entry
    )
    new_exit = (
        _enum_str(updates.get("exit_status"))
        if updates.get("exit_status") is not None
        else old_exit
    )

    if new_entry != old_entry:
        return f"ENTRY_{new_entry}"
    if new_exit != old_exit:
        return f"EXIT_{new_exit}"

    exec_status = (
        _enum_str(updates.get("exec_status"))
        if updates.get("exec_status") is not None
        else ""
    )
    if exec_status:
        return exec_status
    if "entry_order_id" in updates and updates.get("entry_order_id"):
        return "ENTRY_ORDER_UPDATED"
    if "exit_order_id" in updates and updates.get("exit_order_id"):
        return "EXIT_ORDER_UPDATED"
    if "entry_price" in updates or "exit_price" in updates:
        return "ORDER_PRICE_UPDATED"
    return None


def _persist_executor_update(ut: UserTradeSchema, updates: Dict[str, Any]) -> Optional[UserTradeSchema]:
    """Persist executor truth first, then emit a concise non-blocking event."""
    action = _executor_transition_action(ut, updates)
    previous_state = _executor_state(ut)
    persisted = UserTradeSchema.update_user_trade_by_id(int(ut.id), updates)
    if persisted is None or action is None:
        return persisted

    raw_exec_status = updates.get("exec_status")
    reason_code = _enum_str(raw_exec_status) if raw_exec_status is not None else action
    reason_text = str(updates.get("exec_status_message") or reason_code or action)
    event_ts = (
        _to_ist_naive(updates.get("entry_exec_time"))
        or _to_ist_naive(updates.get("exit_exec_time"))
        or _to_ist_naive(updates.get("exec_last_checked_at"))
    )
    if event_ts is None:
        logger.warning(
            "TradeExecutor audit skipped: persisted update has no source/snapshot timestamp "
            "trade_id=%s updated_fields=%s",
            getattr(persisted, "id", getattr(ut, "id", None)),
            sorted(str(key) for key in updates.keys()),
        )
        return persisted
    write_auditlog(
        entity_type="TRADE",
        entity_id=getattr(persisted, "id", getattr(ut, "id", None)),
        symbol=getattr(persisted, "symbol", getattr(ut, "symbol", None)),
        userid=getattr(persisted, "userid", getattr(ut, "userid", None)),
        evaluation_stage="TRADE_EXECUTOR",
        previous_state=previous_state,
        new_state=_executor_state(persisted),
        action=action,
        reason_code=reason_code,
        reason_text=reason_text,
        ts=event_ts,
        payload_json={
            "signal_id": getattr(persisted, "signal_id", None),
            "instrument_type": getattr(persisted, "instrument_type", None),
            "side": getattr(persisted, "trade_type", None),
            "execution_mode": getattr(persisted, "execution_mode", None),
            "planned_entry_price": getattr(persisted, "entry_price", None),
            "executed_entry_price": getattr(persisted, "executed_entry_price", None),
            "executed_entry_qty": getattr(persisted, "executed_entry_qty", None),
            "executed_exit_price": getattr(persisted, "executed_exit_price", None),
            "executed_exit_qty": getattr(persisted, "executed_exit_qty", None),
            "entry_order_id": getattr(persisted, "entry_order_id", None),
            "exit_order_id": getattr(persisted, "exit_order_id", None),
            "entry_status": getattr(persisted, "entry_status", None),
            "exit_status": getattr(persisted, "exit_status", None),
            "exit_reason": getattr(persisted, "exit_reason", None),
            "exit_rule": getattr(persisted, "exit_rule", None),
            "exec_status": updates.get("exec_status"),
            "updated_fields": sorted(str(key) for key in updates.keys()),
        },
    )
    return persisted


# -------------------------------------------------------------------
# DB fetchers
# -------------------------------------------------------------------

def _fetch_candidates_for_user(userid: str, limit: int = 500) -> List[UserTradeSchema]:
    with get_trades_db() as db:
        filt = (
            (UserTradeORM.entry_status.in_([EntryStatus.READY.value, EntryStatus.SUBMITTED.value]))
            | (UserTradeORM.exit_status.in_([ExitStatus.READY.value, ExitStatus.SUBMITTED.value]))
        )

        rows = (
            db.query(UserTradeORM)
            .filter(UserTradeORM.userid == userid)
            .filter(filt)
            .order_by(UserTradeORM.id.asc())
            .limit(int(limit))
            .all()
        )
    return [UserTradeSchema.model_validate(r) for r in rows]


def _fetch_due_userids(limit_users: int = 100) -> List[str]:
    with get_trades_db() as db:
        filt = (
            (UserTradeORM.entry_status.in_([EntryStatus.READY.value, EntryStatus.SUBMITTED.value]))
            | (UserTradeORM.exit_status.in_([ExitStatus.READY.value, ExitStatus.SUBMITTED.value]))
        )

        q = (
            db.query(UserTradeORM.userid)
            .filter(filt)
            .distinct()
            .order_by(UserTradeORM.userid.asc())
            .limit(int(limit_users))
        )
        rows = q.all()
    return [r[0] for r in rows if r and r[0]]


# -------------------------------------------------------------------
# order history helpers
# -------------------------------------------------------------------

def _extract_average_price_and_qty(hist: List[dict]) -> Tuple[Optional[Decimal], int]:
    if not hist:
        return None, 0

    avg_px: Optional[Decimal] = None
    filled_qty = 0

    for row in reversed(hist):
        if not isinstance(row, dict):
            continue

        ap = row.get("average_price")
        fq = row.get("filled_quantity")

        ap_d = d(ap) if ap is not None else Decimal("0")
        fq_i = _try_int(fq, 0)

        if avg_px is None and ap_d > 0:
            avg_px = ap_d
        if filled_qty <= 0 and fq_i > 0:
            filled_qty = fq_i

        if avg_px is not None and filled_qty > 0:
            break

    return avg_px, filled_qty


def _safe_modify_order_price(broker: ZerodhaBroker, *, order_id: str, price: Decimal, variety: Any) -> bool:
    if not order_id or price <= 0:
        return False
    try:
        broker.svc.modify_order_price(order_id=order_id, price=float(price), variety=variety)
        return True
    except Exception:
        return False


def _safe_cancel_order(broker: ZerodhaBroker, *, order_id: str, variety: Any) -> bool:
    if not order_id:
        return False
    try:
        response = broker.svc.cancel_order(order_id=order_id, variety=variety)
        return bool(response)
    except Exception:
        return False


# -------------------------------------------------------------------
# Executor core
# -------------------------------------------------------------------

class TradeExecutor:
    def execute_all(
        self,
        *,
        limit_users: int = 100,
        limit_per_user: int = 500,
        snapshot_time: Optional[datetime] = None,
    ) -> int:
        asof = _to_ist_naive(snapshot_time) or _now_ist_naive()
        userids = _fetch_due_userids(limit_users=limit_users)
        if not userids:
            logger.info("TradeExecutor: no due users")
            return 0

        total = 0
        for uid in userids:
            try:
                total += self.execute_user_once(userid=uid, limit=limit_per_user, snapshot_time=asof)
            except Exception:
                logger.exception("TradeExecutor: fatal error execute_user_once userid=%s", uid)

        logger.info("TradeExecutor: execute_all processed_total=%d users=%d", total, len(userids))
        return total

    def execute_user_once(
        self,
        *,
        userid: str,
        limit: int = 500,
        snapshot_time: Optional[datetime] = None,
    ) -> int:
        asof = _to_ist_naive(snapshot_time) or _now_ist_naive()
        user = UserSchema.fetch_user(userid)
        if not user:
            logger.warning("TradeExecutor: user not found userid=%s", userid)
            return 0

        trades = _fetch_candidates_for_user(userid, limit=limit)
        if not trades:
            return 0

        processed = 0
        for stale_ut in trades:
            try:
                # A prior sibling may have expired/cancelled the whole package.
                # Re-fetch so stale in-memory READY rows are never executed.
                ut = UserTradeSchema.fetch_user_trade_by_id(int(stale_ut.id))
                if ut and self._is_actionable(ut) and self._process_trade_for_user(ut, user, asof_time=asof):
                    processed += 1
            except Exception:
                logger.exception("TradeExecutor: error processing trade_id=%s userid=%s", getattr(stale_ut, "id", None), userid)

        return processed

    def _is_actionable(self, ut: UserTradeSchema) -> bool:
        es = _entry_status(ut)
        xs = _exit_status(ut)
        return (
            es in (EntryStatus.READY.value, EntryStatus.SUBMITTED.value)
            or xs in (ExitStatus.READY.value, ExitStatus.SUBMITTED.value)
        )

    def _entry_signal_guard(
        self,
        ut: UserTradeSchema,
    ) -> Tuple[str, Optional[SignalSchema], Optional[str]]:
        """Return VALID/INVALID/DEFER/BYPASS for the originating signal.

        Signal-linked entries must resolve the exact persisted signal.  Manual
        watchlist/position trades are intentionally outside this lifecycle.
        """
        if not _is_signal_linked_entry(ut):
            return ENTRY_SIGNAL_GUARD_BYPASS, None, None

        signal_id = str(getattr(ut, "signal_id", "") or "").strip()
        if not signal_id:
            return (
                ENTRY_SIGNAL_GUARD_INVALID,
                None,
                "ENTRY_CANCEL_SOURCE_SIGNAL_ID_MISSING",
            )

        try:
            signal = SignalSchema.fetch_by_signal_id_strict(signal_id)
        except Exception as exc:
            return (
                ENTRY_SIGNAL_GUARD_DEFER,
                None,
                f"ENTRY_DEFER_SIGNAL_REVALIDATION_FAILED {str(exc)[:190]}",
            )

        if signal is None:
            return (
                ENTRY_SIGNAL_GUARD_INVALID,
                None,
                "ENTRY_CANCEL_SOURCE_SIGNAL_NOT_FOUND",
            )

        status = _enum_str(getattr(signal, "status", ""))
        if status != "OPEN":
            return (
                ENTRY_SIGNAL_GUARD_INVALID,
                signal,
                f"ENTRY_CANCEL_SIGNAL_STATUS_{status or 'UNKNOWN'}",
            )

        try:
            context = AuctionTradeSignalContext.from_signal(signal)
        except Exception as exc:
            return (
                ENTRY_SIGNAL_GUARD_DEFER,
                signal,
                f"ENTRY_DEFER_SIGNAL_CONTRACT_INVALID {str(exc)[:190]}",
            )

        if context.requires_exit:
            return (
                ENTRY_SIGNAL_GUARD_INVALID,
                signal,
                (
                    "ENTRY_CANCEL_SIGNAL_EXIT_POSTURE "
                    f"stage={context.stage} action={context.lifecycle_trade_action}"
                ),
            )

        return ENTRY_SIGNAL_GUARD_VALID, signal, None

    def _entry_profitability_defer_reason(
        self,
        ut: UserTradeSchema,
        *,
        signal: Optional[SignalSchema],
        asof_time: datetime,
    ) -> Optional[str]:
        """Require strict favourable movement for automatic signal entries.

        Compare signal.created_price with the latest completed underlying
        snapshot LTP, falling back to that same snapshot's close.  Equality is
        deliberately a WAIT, not an entry permission.
        """
        if signal is None or _trade_source(ut) not in AUTO_SIGNAL_SOURCES:
            return None

        created = d(getattr(signal, "created_price", None))
        if created <= 0:
            return "ENTRY_DEFER_SIGNAL_CREATED_PRICE_MISSING"

        equity_ref = str(
            getattr(signal, "equity_ref", None)
            or getattr(ut, "equity_ref", None)
            or ""
        ).strip().upper()
        current, snapshot_ts, price_source = _latest_underlying_snapshot_price(
            equity_ref,
            asof_time=asof_time,
        )
        if current is None or current <= 0:
            return "ENTRY_DEFER_SNAPSHOT_PRICE_UNAVAILABLE"

        side = _enum_str(getattr(signal, "side", ""))
        favourable = (
            (side == TradeType.BUY.value and current > created)
            or (side == TradeType.SELL.value and current < created)
        )
        if favourable:
            return None

        relation = "current>created" if side == TradeType.BUY.value else "current<created"
        return (
            "ENTRY_DEFER_SIGNAL_NOT_STRICTLY_PROFITABLE "
            f"side={side} created={created} current={current} "
            f"required={relation} source={price_source} snapshot_time={snapshot_ts}"
        )

    def _process_trade_for_user(
        self,
        ut: UserTradeSchema,
        user: UserSchema,
        *,
        asof_time: datetime,
    ) -> bool:
        es = _entry_status(ut)
        xs = _exit_status(ut)

        entry_pending = es in (EntryStatus.READY.value, EntryStatus.SUBMITTED.value)
        entry_cutoff_due = bool(
            entry_pending
            and bool(getattr(ut, "intraday_only", False))
            and cutoff_due(
                asof_time,
                INTRADAY_LIFECYCLE_CONFIG.signal_cutoff_time,
            )
        )

        guard_state = ENTRY_SIGNAL_GUARD_BYPASS
        guard_reason: Optional[str] = None
        if entry_pending and not entry_cutoff_due:
            guard_state, _, guard_reason = self._entry_signal_guard(ut)

        if not _is_real(ut):
            if entry_cutoff_due:
                return self._cancel_invalidated_entry_package(
                    ut,
                    reason="ENTRY_CANCEL_INTRADAY_SIGNAL_CUTOFF",
                    asof_time=asof_time,
                    broker=None,
                )
            if guard_state == ENTRY_SIGNAL_GUARD_INVALID:
                return self._cancel_invalidated_entry_package(
                    ut,
                    reason=guard_reason or "ENTRY_CANCEL_SIGNAL_INVALIDATED",
                    asof_time=asof_time,
                    broker=None,
                )
            if guard_state == ENTRY_SIGNAL_GUARD_DEFER and es == EntryStatus.READY.value:
                return self._defer_ready_entry(
                    ut,
                    reason=guard_reason or "ENTRY_DEFER_SIGNAL_REVALIDATION_FAILED",
                    asof_time=asof_time,
                )
            return self._process_virtual(ut, asof_time=asof_time)

        broker_login_ok = _try_int(getattr(user, "broker_login", 0), 0) == 1
        broker = ZerodhaBroker(user) if broker_login_ok else None

        if entry_cutoff_due:
            return self._cancel_invalidated_entry_package(
                ut,
                reason="ENTRY_CANCEL_INTRADAY_SIGNAL_CUTOFF",
                asof_time=asof_time,
                broker=broker,
            )

        if guard_state == ENTRY_SIGNAL_GUARD_INVALID:
            return self._cancel_invalidated_entry_package(
                ut,
                reason=guard_reason or "ENTRY_CANCEL_SIGNAL_INVALIDATED",
                asof_time=asof_time,
                broker=broker,
            )

        if guard_state == ENTRY_SIGNAL_GUARD_DEFER and es == EntryStatus.READY.value:
            return self._defer_ready_entry(
                ut,
                reason=guard_reason or "ENTRY_DEFER_SIGNAL_REVALIDATION_FAILED",
                asof_time=asof_time,
            )

        if not broker_login_ok or broker is None:
            upd: Dict[str, Any] = {
                "exec_status": "BROKER_LOGIN_REQUIRED",
                "exec_status_message": "Cannot execute REAL trade: broker_login!=1",
                "exec_last_checked_at": _now_ist_naive(),
            }
            if es in (EntryStatus.READY.value, EntryStatus.SUBMITTED.value):
                upd["entry_status"] = EntryStatus.INVALID.value
            if xs in (ExitStatus.READY.value, ExitStatus.SUBMITTED.value):
                upd["exit_status"] = ExitStatus.FAILED.value
            _persist_executor_update(ut, upd)
            return True

        if xs in (ExitStatus.READY.value, ExitStatus.SUBMITTED.value) and es in (
            EntryStatus.READY.value,
            EntryStatus.SUBMITTED.value,
        ):
            return self._cancel_pending_entry(ut, broker)

        if xs in (ExitStatus.READY.value, ExitStatus.SUBMITTED.value):
            return self._process_exit_real_blocking(ut, broker)

        if es in (EntryStatus.READY.value, EntryStatus.SUBMITTED.value):
            return self._process_entry_real_blocking(ut, broker, asof_time=asof_time)

        return False

    def _mark_filled_entry_for_immediate_exit(
        self,
        ut: UserTradeSchema,
        *,
        reason: str,
        asof_time: datetime,
    ) -> bool:
        xs = _exit_status(ut)
        if xs in (ExitStatus.READY.value, ExitStatus.SUBMITTED.value, ExitStatus.FILLED.value):
            return True
        cutoff = _is_intraday_signal_cutoff_reason(reason)
        _persist_executor_update(ut, {
            "exit_status": ExitStatus.READY.value,
            "exit_reason": (
                "INTRADAY_SIGNAL_CUTOFF"
                if cutoff
                else "SIGNAL_INVALIDATED_BEFORE_ENTRY_COMPLETION"
            ),
            "exit_rule": (
                "trade_executor_intraday_signal_cutoff"
                if cutoff
                else "trade_executor_signal_revalidation"
            ),
            "exit_intent_time": asof_time,
            "exit_time": asof_time,
            "exec_status": (
                "EXIT_READY_INTRADAY_SIGNAL_CUTOFF_DURING_ENTRY"
                if cutoff
                else "EXIT_READY_SIGNAL_INVALIDATED_DURING_ENTRY"
            ),
            "exec_status_message": str(reason)[:250],
            "exec_last_checked_at": asof_time,
        })
        return True

    def _finalize_invalidated_broker_entry_fill(
        self,
        ut: UserTradeSchema,
        *,
        fill_price: Decimal,
        fill_qty: int,
        fill_time: datetime,
        reason: str,
    ) -> bool:
        cutoff = _is_intraday_signal_cutoff_reason(reason)
        updates = _entry_fill_updates(
            ut,
            fill_price=fill_price,
            fill_qty=fill_qty,
            when=fill_time,
            reconcile_message=(
                "Broker entry filled while entry eligibility was terminal; "
                f"immediate exit queued. reason={reason}"
            ),
        )
        updates.update({
            "exit_status": ExitStatus.READY.value,
            "exit_reason": (
                "INTRADAY_SIGNAL_CUTOFF"
                if cutoff
                else "SIGNAL_INVALIDATED_BEFORE_ENTRY_COMPLETION"
            ),
            "exit_rule": (
                "trade_executor_intraday_signal_cutoff"
                if cutoff
                else "trade_executor_signal_revalidation"
            ),
            "exit_intent_time": fill_time,
            "exit_time": fill_time,
            "exec_status": (
                "ENTRY_FILLED_DURING_INTRADAY_SIGNAL_CUTOFF"
                if cutoff
                else "ENTRY_FILLED_DURING_SIGNAL_INVALIDATION"
            ),
            "exec_status_message": str(reason)[:250],
            "exec_last_checked_at": fill_time,
        })
        _persist_executor_update(ut, updates)
        return True

    def _cancel_submitted_entry_for_signal_invalidation(
        self,
        ut: UserTradeSchema,
        broker: ZerodhaBroker,
        *,
        reason: str,
        asof_time: datetime,
    ) -> bool:
        oid = str(getattr(ut, "entry_order_id", "") or "").strip()
        terminal_suffix = (
            "INTRADAY_SIGNAL_CUTOFF"
            if _is_intraday_signal_cutoff_reason(reason)
            else "SIGNAL_INVALIDATED"
        )
        if not oid:
            _persist_executor_update(ut, {
                "entry_status": EntryStatus.INVALID.value,
                "exec_status": "ENTRY_CANCEL_FAILED_ORDER_ID_MISSING",
                "exec_status_message": str(reason)[:250],
                "exec_last_checked_at": asof_time,
            })
            return True

        mode = "intraday" if bool(getattr(ut, "intraday_only", False)) else "carryforward"
        op = OrderProfileSchema.fetch_order_profile(mode, getattr(ut, "instrument_type", "EQ"))
        variety = OrderVariety.from_string(getattr(op, "order_variety", OrderVariety.REGULAR.value))

        try:
            status = broker.latest_status(oid)
        except Exception as exc:
            _persist_executor_update(ut, {
                "exec_status": "ENTRY_CANCEL_STATUS_CHECK_FAILED",
                "exec_status_message": f"{reason}; {str(exc)[:160]}"[:250],
                "exec_last_checked_at": asof_time,
            })
            return True

        try:
            hist = broker.history(oid)
        except Exception:
            hist = []
        avg, filled_qty = _extract_average_price_and_qty(hist)
        fill_time = _broker_fill_time_from_history(hist) or asof_time

        if status == OrderStatus.COMPLETE:
            if avg is None or avg <= 0:
                _persist_executor_update(ut, {
                    "exec_status": f"ENTRY_{terminal_suffix}_FILL_PRICE_PENDING",
                    "exec_status_message": str(reason)[:250],
                    "exec_last_checked_at": asof_time,
                })
                return True
            return self._finalize_invalidated_broker_entry_fill(
                ut,
                fill_price=avg,
                fill_qty=filled_qty or (_try_int(getattr(ut, "quantity", 0), 0) or 1),
                fill_time=fill_time,
                reason=reason,
            )

        if status in (OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.INVALID):
            if filled_qty > 0:
                if avg is None or avg <= 0:
                    _persist_executor_update(ut, {
                        "exec_status": f"ENTRY_{terminal_suffix}_PARTIAL_FILL_PRICE_PENDING",
                        "exec_status_message": str(reason)[:250],
                        "exec_last_checked_at": asof_time,
                    })
                    return True
                return self._finalize_invalidated_broker_entry_fill(
                    ut,
                    fill_price=avg,
                    fill_qty=filled_qty,
                    fill_time=fill_time,
                    reason=reason,
                )

            terminal = (
                EntryStatus.CANCELLED.value
                if status == OrderStatus.CANCELLED
                else EntryStatus.REJECTED.value
                if status == OrderStatus.REJECTED
                else EntryStatus.INVALID.value
            )
            _persist_executor_update(ut, {
                "entry_status": terminal,
                "executed_entry_price": None,
                "executed_entry_qty": 0,
                "exec_status": f"ENTRY_{status.value}_{terminal_suffix}",
                "exec_status_message": str(reason)[:250],
                "exec_last_checked_at": asof_time,
            })
            return True

        requested_code = f"ENTRY_CANCEL_REQUESTED_{terminal_suffix}"
        failed_code = f"ENTRY_CANCEL_REQUEST_FAILED_{terminal_suffix}"
        already_requested = _enum_str(getattr(ut, "exec_status", "")) == requested_code
        requested = True if already_requested else _safe_cancel_order(
            broker,
            order_id=oid,
            variety=variety,
        )
        _persist_executor_update(ut, {
            "exec_status": requested_code if requested else failed_code,
            "exec_status_message": str(reason)[:250],
            "exec_last_checked_at": asof_time,
        })
        return True

    def _cancel_invalidated_entry_package(
        self,
        ut: UserTradeSchema,
        *,
        reason: str,
        asof_time: datetime,
        broker: Optional[ZerodhaBroker],
    ) -> bool:
        package = UserTradeSchema.fetch_active_trades_for_signal(
            userid=str(getattr(ut, "userid", "") or ""),
            signal_id=str(getattr(ut, "signal_id", "") or ""),
        )
        if not package:
            package = [ut]

        cutoff = _is_intraday_signal_cutoff_reason(reason)
        terminal_suffix = (
            "INTRADAY_SIGNAL_CUTOFF" if cutoff else "SIGNAL_INVALIDATED"
        )

        for leg in package:
            es = _entry_status(leg)
            if es in (EntryStatus.CREATED.value, EntryStatus.READY.value):
                _persist_executor_update(leg, {
                    "entry_status": EntryStatus.EXPIRED.value,
                    "exit_status": ExitStatus.NONE.value,
                    "exec_status": f"ENTRY_EXPIRED_{terminal_suffix}",
                    "exec_status_message": str(reason)[:250],
                    "exec_last_checked_at": asof_time,
                })
                continue

            if es == EntryStatus.SUBMITTED.value:
                if not _is_real(leg):
                    _persist_executor_update(leg, {
                        "entry_status": EntryStatus.CANCELLED.value,
                        "exit_status": ExitStatus.NONE.value,
                        "exec_status": f"VIRTUAL_ENTRY_CANCELLED_{terminal_suffix}",
                        "exec_status_message": str(reason)[:250],
                        "exec_last_checked_at": asof_time,
                    })
                elif broker is None:
                    _persist_executor_update(leg, {
                        "exec_status": (
                            "ENTRY_CANCEL_PENDING_BROKER_LOGIN_REQUIRED_"
                            f"{terminal_suffix}"
                        ),
                        "exec_status_message": str(reason)[:250],
                        "exec_last_checked_at": asof_time,
                    })
                else:
                    self._cancel_submitted_entry_for_signal_invalidation(
                        leg,
                        broker,
                        reason=reason,
                        asof_time=asof_time,
                    )
                continue

            if es == EntryStatus.FILLED.value:
                self._mark_filled_entry_for_immediate_exit(
                    leg,
                    reason=reason,
                    asof_time=asof_time,
                )

        logger.info(
            "TradeExecutor: terminal entry package handled | userid=%s signal_id=%s reason=%s legs=%d",
            getattr(ut, "userid", None),
            getattr(ut, "signal_id", None),
            reason,
            len(package),
        )
        return True

    def _expire_entry_package(self, ut: UserTradeSchema, *, reason: str, asof_time: datetime) -> bool:
        count = UserTradeSchema.expire_ready_entries_for_signal(
            userid=str(getattr(ut, "userid", "") or ""),
            signal_id=str(getattr(ut, "signal_id", "") or ""),
            reason=reason,
            ts=asof_time,
        )
        logger.info(
            "TradeExecutor: entry package expired | userid=%s signal_id=%s reason=%s rows=%d",
            getattr(ut, "userid", None),
            getattr(ut, "signal_id", None),
            reason,
            count,
        )
        if count:
            write_auditlog(
                entity_type="TRADE_PACKAGE",
                entity_id=getattr(ut, "signal_id", None),
                symbol=getattr(ut, "equity_ref", None) or getattr(ut, "symbol", None),
                userid=getattr(ut, "userid", None),
                evaluation_stage="TRADE_EXECUTOR",
                previous_state="ENTRY_READY",
                new_state="ENTRY_EXPIRED",
                action="ENTRY_EXPIRED",
                reason_code=str(reason or "ENTRY_EXPIRED").split(" ", 1)[0],
                reason_text=reason,
                ts=asof_time,
                payload_json={
                    "signal_id": getattr(ut, "signal_id", None),
                    "expired_trade_count": count,
                    "reason": reason,
                },
            )
        return True

    def _revalidate_ready_entry(
        self,
        ut: UserTradeSchema,
        *,
        candidate_price: Any,
        candidate_time: datetime,
        asof_time: datetime,
    ) -> Optional[str]:
        """Revalidate source signal and strict entry eligibility at execution.

        TradeGenerator remains the first eligibility gate.  Executor performs
        the final authoritative check immediately before a virtual fill or real
        broker submission so a stale READY package cannot execute after its
        signal has died.
        """
        if bool(getattr(ut, "intraday_only", False)) and cutoff_due(
            asof_time,
            INTRADAY_LIFECYCLE_CONFIG.signal_cutoff_time,
        ):
            return "ENTRY_CANCEL_INTRADAY_SIGNAL_CUTOFF"

        guard_state, signal, guard_reason = self._entry_signal_guard(ut)
        if guard_state == ENTRY_SIGNAL_GUARD_INVALID:
            return guard_reason or "ENTRY_CANCEL_SIGNAL_INVALIDATED"
        if guard_state == ENTRY_SIGNAL_GUARD_DEFER:
            return guard_reason or "ENTRY_DEFER_SIGNAL_REVALIDATION_FAILED"

        profitability_reason = self._entry_profitability_defer_reason(
            ut,
            signal=signal,
            asof_time=asof_time,
        )
        if profitability_reason:
            return profitability_reason

        px = d(candidate_price)
        if px <= 0:
            return "ENTRY_DEFER_QUOTE_UNAVAILABLE"

        quote_ts = _to_ist_naive(candidate_time)
        if quote_ts is None:
            return "ENTRY_DEFER_QUOTE_TIME_MISSING"

        return None

    def _defer_ready_entry(self, ut: UserTradeSchema, *, reason: str, asof_time: datetime) -> bool:
        """Keep a READY intent without emitting the same defer audit every tick."""
        code = str(reason or "ENTRY_DEFERRED").split(" ", 1)[0]
        message = str(reason or "ENTRY_DEFERRED")[:250]
        updates: Dict[str, Any] = {"exec_last_checked_at": asof_time}
        if (
            _enum_str(getattr(ut, "exec_status", "")) != _enum_str(code)
            or str(getattr(ut, "exec_status_message", "") or "") != message
        ):
            updates["exec_status"] = code
            updates["exec_status_message"] = message
        _persist_executor_update(ut, updates)
        return True

    # ---------------- cancel pending entry ----------------

    def _cancel_pending_entry(self, ut: UserTradeSchema, broker: ZerodhaBroker) -> bool:
        mode = "intraday" if bool(getattr(ut, "intraday_only", False)) else "carryforward"
        op = OrderProfileSchema.fetch_order_profile(mode, getattr(ut, "instrument_type", "EQ"))
        variety = OrderVariety.from_string(getattr(op, "order_variety", OrderVariety.REGULAR.value))
        oid = getattr(ut, "entry_order_id", None)

        cancelled_at_broker = False
        if oid:
            cancelled_at_broker = _safe_cancel_order(broker, order_id=oid, variety=variety)

        upd = {
            "entry_status": EntryStatus.CANCELLED.value,
            "exit_status": ExitStatus.NONE.value,
            "entry_order_id": oid or None,
            "exec_last_checked_at": _now_ist_naive(),
            "exec_status": "ENTRY_CANCELLED_BY_USER",
            "exec_status_message": (
                "Pending entry cancelled by user before fill"
                if cancelled_at_broker or oid
                else "Unsubmitted/ready entry cancelled by user before fill"
            ),
        }
        _persist_executor_update(ut, upd)
        return True

    # ---------------- VIRTUAL ----------------

    def _process_virtual(self, ut: UserTradeSchema, *, asof_time: datetime) -> bool:
        updates: Dict[str, Any] = {}

        es = _entry_status(ut)
        xs = _exit_status(ut)

        if es == EntryStatus.READY.value:
            px, ts = _virtual_fill_price_time(
                getattr(ut, "symbol", ""),
                _side(ut),
                getattr(ut, "entry_price", None),
                getattr(ut, "entry_intent_time", None) or getattr(ut, "entry_time", None),
                asof_time=asof_time,
                equity_ref=getattr(ut, "equity_ref", None),
                instrument_type=getattr(ut, "instrument_type", None),
                last_known_price=getattr(ut, "last_price", None),
            )
            expiry_reason = self._revalidate_ready_entry(
                ut,
                candidate_price=px,
                candidate_time=ts,
                asof_time=asof_time,
            )
            if expiry_reason:
                if expiry_reason.startswith("ENTRY_DEFER_"):
                    return self._defer_ready_entry(ut, reason=expiry_reason, asof_time=asof_time)
                return self._cancel_invalidated_entry_package(
                    ut,
                    reason=expiry_reason,
                    asof_time=asof_time,
                    broker=None,
                )
            qty = _try_int(getattr(ut, "quantity", 0), 0) or 1
            updates.update(_entry_fill_updates(
                ut,
                fill_price=px,
                fill_qty=qty,
                when=ts,
                reconcile_message="Virtual entry completed directly by executor.",
            ))

        if xs == ExitStatus.READY.value:
            px, ts = _virtual_fill_price_time(
                getattr(ut, "symbol", ""),
                _opposite_side(_side(ut)),
                getattr(ut, "exit_price", None),
                getattr(ut, "exit_intent_time", None) or getattr(ut, "exit_time", None) or getattr(ut, "last_time", None),
                asof_time=asof_time,
                equity_ref=getattr(ut, "equity_ref", None),
                instrument_type=getattr(ut, "instrument_type", None),
                last_known_price=getattr(ut, "last_price", None),
            )
            if px <= 0:
                _persist_executor_update(
                    ut,
                    {
                        "exec_last_checked_at": ts,
                        "exec_status": "EXIT_DEFER_PRICE_UNAVAILABLE",
                        "exec_status_message": (
                            "Virtual exit remains READY because no positive exact "
                            "instrument price was available."
                        ),
                    },
                )
                return True
            qty = _try_int(
                getattr(ut, "executed_exit_qty", None)
                or getattr(ut, "executed_entry_qty", None)
                or getattr(ut, "quantity", 0)
                or 1,
                1,
            )
            updates.update(
                _build_exit_fill_updates(
                    ut,
                    exit_px=px,
                    qty=qty,
                    when=ts,
                    status_code=None,
                    status_message=None,
                    obs_kind="exec",
                )
            )
            updates["exit_reconciled_at"] = ts
            updates.update(_obs_update(
                "reconcile",
                code="EXIT_RECONCILED_FROM_EXECUTOR",
                message="Virtual exit completed directly by executor.",
                when=ts,
            ))

        if updates:
            _persist_executor_update(ut, updates)
            return True

        return False

    # ---------------- REAL ENTRY ----------------

    def _process_entry_real_blocking(
        self,
        ut: UserTradeSchema,
        broker: ZerodhaBroker,
        *,
        asof_time: datetime,
    ) -> bool:
        es = _entry_status(ut)

        if es == EntryStatus.READY.value:
            try:
                plan_updates = TradeGenHelper.build_unsubmitted_plan_refresh(
                    ut,
                    asof_time=asof_time,
                    refresh_reason="AUTO_PRE_EXECUTION_PLAN_REFRESH",
                    force=False,
                )
                if plan_updates:
                    plan_updates.update({
                        "exec_status": "ENTRY_PLAN_REFRESHED",
                        "exec_status_message": (
                            "Entry plan rebuilt from the latest completed snapshot "
                            "before REAL execution."
                        ),
                        "exec_last_checked_at": asof_time,
                    })
                    refreshed = _persist_executor_update(ut, plan_updates)
                    if refreshed is None:
                        return True
                    ut = refreshed
            except Exception as exc:
                return self._defer_ready_entry(
                    ut,
                    reason=(
                        "ENTRY_DEFER_PLAN_REFRESH_FAILED "
                        f"{str(exc)[:210]}"
                    ),
                    asof_time=asof_time,
                )

            candidate_px = _quote_best_price(
                getattr(ut, "symbol", ""),
                _side(ut),
            )
            expiry_reason = self._revalidate_ready_entry(
                ut,
                candidate_price=candidate_px,
                candidate_time=asof_time,
                asof_time=asof_time,
            )
            if expiry_reason:
                if expiry_reason.startswith("ENTRY_DEFER_"):
                    return self._defer_ready_entry(
                        ut,
                        reason=expiry_reason,
                        asof_time=asof_time,
                    )
                return self._cancel_invalidated_entry_package(
                    ut,
                    reason=expiry_reason,
                    asof_time=asof_time,
                    broker=broker,
                )

            executable_px = _round_to_tick(d(candidate_px))
            drift = _entry_price_drift_pct(
                getattr(ut, "entry_price", None),
                executable_px,
            )
            drift_limit = _entry_price_drift_limit(
                getattr(ut, "instrument_type", None)
            )
            if drift is None:
                return self._defer_ready_entry(
                    ut,
                    reason="ENTRY_DEFER_PRICE_DRIFT_INPUT_INVALID",
                    asof_time=asof_time,
                )
            if drift > drift_limit:
                return self._defer_ready_entry(
                    ut,
                    reason=(
                        "ENTRY_DEFER_PRICE_DRIFT "
                        f"planned={d(getattr(ut, 'entry_price', None))} "
                        f"executable={executable_px} "
                        f"drift_pct={(drift * Decimal('100')):.2f} "
                        f"limit_pct={(drift_limit * Decimal('100')):.2f}"
                    ),
                    asof_time=asof_time,
                )

            placed = self._place_entry_real(
                ut,
                broker,
                executable_price=executable_px,
            )
            if not placed:
                return True
            return self._poll_entry_until_terminal(ut.id, broker)

        if es == EntryStatus.SUBMITTED.value:
            return self._poll_entry_until_terminal(ut.id, broker)

        return False

    def _place_entry_real(
        self,
        ut: UserTradeSchema,
        broker: ZerodhaBroker,
        *,
        executable_price: Any,
    ) -> bool:
        if hasattr(ut, "entry_retries") and getattr(ut, "entry_retries", None) is not None:
            if int(getattr(ut, "entry_retries") or 0) <= 0:
                _persist_executor_update(ut, {
                    "entry_status": EntryStatus.INVALID.value,
                    "exec_status": "ENTRY_RETRY_EXHAUSTED",
                    "exec_status_message": "Entry retries exhausted",
                    "exec_last_checked_at": _now_ist_naive(),
                })
                return False

        mode = "intraday" if bool(getattr(ut, "intraday_only", False)) else "carryforward"
        op = OrderProfileSchema.fetch_order_profile(mode, getattr(ut, "instrument_type", "EQ"))

        side = TradeType.from_string(_side(ut))
        order_type = OrderType.from_string(getattr(op, "order_type", OrderType.MARKET.value))
        variety = OrderVariety.from_string(getattr(op, "order_variety", OrderVariety.REGULAR.value))

        params: Dict[str, Any] = {
            "exchange": op.exchange,
            "tradingsymbol": ut.symbol,
            "transaction_type": side,
            "quantity": int(getattr(ut, "quantity", 1) or 1),
            "order_type": order_type,
            "product": getattr(op, "product_type"),
            "variety": variety,
        }

        entry_px = _round_to_tick(d(executable_price))
        if entry_px <= 0:
            raise ValueError("REAL entry executable_price must be positive")

        if order_type == OrderType.LIMIT:
            params["price"] = float(entry_px)
        elif order_type == OrderType.SL:
            params["trigger_price"] = float(entry_px)
            params["price"] = float(entry_px)
        elif order_type == OrderType.SLM:
            params["trigger_price"] = float(entry_px)

        try:
            resp = broker.place_order(**params)
            oid = _extract_order_id(resp)
            if not oid:
                raise ValueError("Broker place_order returned empty order_id")

            now = _now_ist_naive()
            upd: Dict[str, Any] = {
                "entry_status": EntryStatus.SUBMITTED.value,
                "entry_order_id": oid,
                "entry_order_response_json": json.dumps(
                    {
                        "request": _json_safe_order_request(params),
                        "guarded_reference_price": float(entry_px),
                        "broker_response": resp,
                    },
                    default=str,
                ),
                "entry_intent_time": _to_ist_naive(getattr(ut, "entry_intent_time", None)) or now,
                "exec_last_checked_at": now,
                "exec_status": None,
                "exec_status_message": None,
            }

            # Keep entry_price as the refreshed plan anchor. Broker request
            # price is stored in entry_order_response_json; executed fill truth
            # is stored separately and rebases management only after COMPLETE.
            _persist_executor_update(ut, upd)
            return True

        except Exception as e:
            logger.warning("REAL entry place failed trade_id=%s err=%s", ut.id, str(e)[:200])

            upd: Dict[str, Any] = {
                "exec_status": "ENTRY_PLACE_FAILED",
                "exec_status_message": str(e)[:500],
                "exec_last_checked_at": _now_ist_naive(),
            }

            if hasattr(ut, "entry_retries") and getattr(ut, "entry_retries", None) is not None:
                left = max(int(getattr(ut, "entry_retries", 1) or 1) - 1, 0)
                upd["entry_retries"] = left
                if left <= 0:
                    upd["entry_status"] = EntryStatus.INVALID.value

            _persist_executor_update(ut, upd)
            return False

    def _poll_entry_until_terminal(self, trade_id: int, broker: ZerodhaBroker) -> bool:
        t0 = time.time()
        reprices_done = 0

        while True:
            ut = UserTradeSchema.fetch_user_trade_by_id(trade_id)
            if not ut:
                return True

            es = _entry_status(ut)
            if es in (
                EntryStatus.FILLED.value,
                EntryStatus.EXPIRED.value,
                EntryStatus.CANCELLED.value,
                EntryStatus.REJECTED.value,
                EntryStatus.INVALID.value,
            ):
                return True

            now = _now_ist_naive()
            guard_state, _, guard_reason = self._entry_signal_guard(ut)
            allow_reprice = True

            if guard_state == ENTRY_SIGNAL_GUARD_INVALID:
                self._cancel_invalidated_entry_package(
                    ut,
                    reason=guard_reason or "ENTRY_CANCEL_SIGNAL_INVALIDATED",
                    asof_time=now,
                    broker=broker,
                )
                did_terminal = False
                allow_reprice = False
            elif guard_state == ENTRY_SIGNAL_GUARD_DEFER:
                _persist_executor_update(ut, {
                    "exec_status": "ENTRY_SIGNAL_REVALIDATION_PENDING",
                    "exec_status_message": str(guard_reason or "signal revalidation pending")[:250],
                    "exec_last_checked_at": now,
                })
                # Still reconcile broker truth, but never improve/reprice the
                # order until the source signal can be validated again.
                did_terminal = self._poll_entry_once(ut, broker)
                allow_reprice = False
            else:
                did_terminal = self._poll_entry_once(ut, broker)

            ut2 = UserTradeSchema.fetch_user_trade_by_id(trade_id)
            if not ut2:
                return True

            es2 = _entry_status(ut2)
            if es2 in (
                EntryStatus.FILLED.value,
                EntryStatus.EXPIRED.value,
                EntryStatus.CANCELLED.value,
                EntryStatus.REJECTED.value,
                EntryStatus.INVALID.value,
            ):
                return True

            if (time.time() - t0) >= ORDER_POLL_TIMEOUT_SEC:
                UserTradeSchema.update_user_trade_by_id(trade_id, {"exec_last_checked_at": _now_ist_naive()})
                return True

            if allow_reprice and not did_terminal and reprices_done < MAX_REPRICES_PER_PASS:
                if self._maybe_modify_entry_price(ut2, broker):
                    reprices_done += 1

            time.sleep(ORDER_POLL_INTERVAL_SEC)

    def _maybe_modify_entry_price(self, ut: UserTradeSchema, broker: ZerodhaBroker) -> bool:
        oid = getattr(ut, "entry_order_id", None)
        if not oid:
            return False

        mode = "intraday" if bool(getattr(ut, "intraday_only", False)) else "carryforward"
        op = OrderProfileSchema.fetch_order_profile(mode, getattr(ut, "instrument_type", "EQ"))
        order_type = OrderType.from_string(getattr(op, "order_type", OrderType.MARKET.value))
        variety = OrderVariety.from_string(getattr(op, "order_variety", OrderVariety.REGULAR.value))

        if order_type not in (OrderType.LIMIT, OrderType.SL):
            return False

        left = _try_int(getattr(ut, "entry_retries", None), MAX_RETRIES)
        if left <= 0:
            _persist_executor_update(ut, {
                "entry_status": EntryStatus.INVALID.value,
                "exec_status": "ENTRY_RETRY_EXHAUSTED",
                "exec_status_message": "Entry retries exhausted while waiting for fill",
                "exec_last_checked_at": _now_ist_naive(),
            })
            return False

        try:
            hist = broker.history(oid)
        except Exception:
            hist = []

        current_order_price = (
            _extract_latest_order_price(hist)
            or d(getattr(ut, "entry_price", None) or 0)
        )
        new_px = _next_modified_limit_price(
            ut.symbol,
            _side(ut),
            current_order_price,
        )
        if new_px is None or new_px <= 0:
            return False

        drift = _entry_price_drift_pct(
            getattr(ut, "entry_price", None),
            new_px,
        )
        drift_limit = _entry_price_drift_limit(
            getattr(ut, "instrument_type", None)
        )
        if drift is None or drift > drift_limit:
            _persist_executor_update(ut, {
                "exec_last_checked_at": _now_ist_naive(),
                "exec_status": "ENTRY_REPRICE_BLOCKED_PRICE_DRIFT",
                "exec_status_message": (
                    f"Refused reprice to {new_px}; "
                    f"planned={getattr(ut, 'entry_price', None)} "
                    f"limit_pct={(drift_limit * Decimal('100')):.2f}"
                )[:250],
            })
            return False

        if not _safe_modify_order_price(
            broker,
            order_id=oid,
            price=new_px,
            variety=variety,
        ):
            return False

        _persist_executor_update(ut, {
            # Do not mutate the plan anchor while changing a broker LIMIT.
            "entry_retries": max(left - 1, 0),
            "exec_last_checked_at": _now_ist_naive(),
            "exec_status": "ENTRY_REPRICED",
            "exec_status_message": f"Modified broker entry order to {new_px}",
        })
        return True

    def _poll_entry_once(self, ut: UserTradeSchema, broker: ZerodhaBroker) -> bool:
        oid = getattr(ut, "entry_order_id", None)
        if not oid:
            _persist_executor_update(ut, {
                "entry_status": EntryStatus.INVALID.value,
                "exec_status": "ENTRY_ORDER_ID_MISSING",
                "exec_status_message": "entry_status=SUBMITTED but entry_order_id is missing",
                "exec_last_checked_at": _now_ist_naive(),
            })
            return True

        try:
            st = broker.latest_status(oid)
            if st is None:
                _persist_executor_update(ut, {"exec_last_checked_at": _now_ist_naive()})
                return False

            if st == OrderStatus.COMPLETE:
                hist = broker.history(oid)
                avg, filled_qty = _extract_average_price_and_qty(hist)

                if avg is None or avg <= 0:
                    _persist_executor_update(ut, {
                        "exec_last_checked_at": _now_ist_naive(),
                        "exec_status": "ENTRY_PRICE_PENDING_RECONCILE",
                        "exec_status_message": "Broker fill complete but average_price missing; will retry history.",
                    })
                    return False

                when = _broker_fill_time_from_history(hist) or _now_ist_naive()

                updates = _entry_fill_updates(
                    ut,
                    fill_price=avg,
                    fill_qty=filled_qty or (_try_int(getattr(ut, "quantity", 0), 0) or 1),
                    when=when,
                    reconcile_message=f"Entry fill confirmed directly by executor via broker history order_id={oid}.",
                )
                updates["exec_last_checked_at"] = _now_ist_naive()
                _persist_executor_update(ut, updates)
                return True

            if st in (
                OrderStatus.CANCELLED,
                OrderStatus.REJECTED,
                OrderStatus.INVALID,
            ):
                try:
                    hist = broker.history(oid)
                except Exception:
                    hist = []

                broker_message = _extract_broker_status_message(hist)
                status_message = f"Broker status={st.value}"
                if broker_message:
                    status_message = f"{status_message}: {broker_message}"

                if st == OrderStatus.CANCELLED:
                    terminal_status = EntryStatus.CANCELLED.value
                elif st == OrderStatus.REJECTED:
                    terminal_status = EntryStatus.REJECTED.value
                else:
                    terminal_status = EntryStatus.INVALID.value

                _persist_executor_update(ut, {
                    "entry_status": terminal_status,
                    # Broker identity is immutable audit/reconciliation truth.
                    "entry_order_id": oid,
                    "executed_entry_price": None,
                    "executed_entry_qty": 0,
                    "last_pnl": 0,
                    "last_pnl_value": 0,
                    "exec_status": f"ENTRY_{st.value}",
                    "exec_status_message": status_message[:250],
                    "exec_last_checked_at": _now_ist_naive(),
                })
                return True

            _persist_executor_update(ut, {"exec_last_checked_at": _now_ist_naive()})
            return False

        except Exception as e:
            logger.warning("REAL entry poll failed trade_id=%s err=%s", ut.id, str(e)[:200])
            _persist_executor_update(ut, {
                "exec_status": "ENTRY_POLL_FAILED",
                "exec_status_message": str(e)[:500],
                "exec_last_checked_at": _now_ist_naive(),
            })
            return False

    # ---------------- REAL EXIT ----------------

    def _process_exit_real_blocking(self, ut: UserTradeSchema, broker: ZerodhaBroker) -> bool:
        xs = _exit_status(ut)

        if xs == ExitStatus.READY.value:
            placed = self._place_exit_real(ut, broker)
            if not placed:
                return True
            return self._poll_exit_until_terminal(ut.id, broker)

        if xs == ExitStatus.SUBMITTED.value:
            return self._poll_exit_until_terminal(ut.id, broker)

        return False

    def _place_exit_real(self, ut: UserTradeSchema, broker: ZerodhaBroker) -> bool:
        if hasattr(ut, "exit_retries") and getattr(ut, "exit_retries", None) is not None:
            if int(getattr(ut, "exit_retries") or 0) <= 0:
                _persist_executor_update(ut, {
                    "exit_status": ExitStatus.FAILED.value,
                    "exec_status": "EXIT_RETRY_EXHAUSTED",
                    "exec_status_message": "Exit retries exhausted",
                    "exec_last_checked_at": _now_ist_naive(),
                })
                return False

        mode = "intraday" if bool(getattr(ut, "intraday_only", False)) else "carryforward"
        op = OrderProfileSchema.fetch_order_profile(mode, getattr(ut, "instrument_type", "EQ"))

        exit_side = TradeType.from_string(_opposite_side(_side(ut)))
        order_type = OrderType.from_string(getattr(op, "order_type", OrderType.MARKET.value))
        variety = OrderVariety.from_string(getattr(op, "order_variety", OrderVariety.REGULAR.value))

        qty = int(
            getattr(ut, "executed_entry_qty", None)
            or getattr(ut, "quantity", 1)
            or 1
        )
        if qty <= 0:
            _persist_executor_update(ut, {
                "exit_status": ExitStatus.FAILED.value,
                "exec_status": "EXIT_QTY_ZERO",
                "exec_status_message": f"Computed exit qty={qty}",
                "exec_last_checked_at": _now_ist_naive(),
            })
            return False

        params: Dict[str, Any] = {
            "exchange": op.exchange,
            "tradingsymbol": ut.symbol,
            "transaction_type": exit_side,
            "quantity": qty,
            "order_type": order_type,
            "product": getattr(op, "product_type"),
            "variety": variety,
        }

        exit_px = Decimal("0")
        if order_type == OrderType.LIMIT:
            exit_px = _best_limit_price(ut.symbol, _opposite_side(_side(ut)), getattr(ut, "exit_price", None))
            params["price"] = float(exit_px)
        elif order_type == OrderType.SL:
            exit_px = _best_limit_price(ut.symbol, _opposite_side(_side(ut)), getattr(ut, "exit_price", None))
            params["trigger_price"] = float(exit_px)
            params["price"] = float(exit_px)
        elif order_type == OrderType.SLM:
            exit_px = _best_limit_price(ut.symbol, _opposite_side(_side(ut)), getattr(ut, "exit_price", None))
            params["trigger_price"] = float(exit_px)

        try:
            resp = broker.place_order(**params)
            oid = _extract_order_id(resp)
            if not oid:
                raise ValueError("Broker place_order returned empty exit order_id")

            now = _now_ist_naive()
            upd: Dict[str, Any] = {
                "exit_status": ExitStatus.SUBMITTED.value,
                "exit_order_id": oid,
                "exit_order_response_json": json.dumps(resp),
                "exit_intent_time": _to_ist_naive(getattr(ut, "exit_intent_time", None)) or now,
                "exec_last_checked_at": now,
                "exec_status": None,
                "exec_status_message": None,
            }

            if exit_px > 0 and order_type in (OrderType.LIMIT, OrderType.SL, OrderType.SLM):
                upd["exit_price"] = exit_px

            _persist_executor_update(ut, upd)
            return True

        except Exception as e:
            logger.warning("REAL exit place failed trade_id=%s err=%s", ut.id, str(e)[:200])

            upd: Dict[str, Any] = {
                "exec_status": "EXIT_PLACE_FAILED",
                "exec_status_message": str(e)[:500],
                "exec_last_checked_at": _now_ist_naive(),
            }

            if hasattr(ut, "exit_retries") and getattr(ut, "exit_retries", None) is not None:
                left = max(int(getattr(ut, "exit_retries", 1) or 1) - 1, 0)
                upd["exit_retries"] = left
                if left <= 0:
                    upd["exit_status"] = ExitStatus.FAILED.value

            _persist_executor_update(ut, upd)
            return False

    def _poll_exit_until_terminal(self, trade_id: int, broker: ZerodhaBroker) -> bool:
        t0 = time.time()
        reprices_done = 0

        while True:
            ut = UserTradeSchema.fetch_user_trade_by_id(trade_id)
            if not ut:
                return True

            xs = _exit_status(ut)
            if xs in (ExitStatus.FILLED.value, ExitStatus.CANCELLED.value, ExitStatus.FAILED.value):
                return True

            did_terminal = self._poll_exit_once(ut, broker)

            ut2 = UserTradeSchema.fetch_user_trade_by_id(trade_id)
            if not ut2:
                return True

            xs2 = _exit_status(ut2)
            if xs2 in (ExitStatus.FILLED.value, ExitStatus.CANCELLED.value, ExitStatus.FAILED.value):
                return True

            if (time.time() - t0) >= ORDER_POLL_TIMEOUT_SEC:
                UserTradeSchema.update_user_trade_by_id(trade_id, {"exec_last_checked_at": _now_ist_naive()})
                return True

            if not did_terminal and reprices_done < MAX_REPRICES_PER_PASS:
                if self._maybe_modify_exit_price(ut2, broker):
                    reprices_done += 1

            time.sleep(ORDER_POLL_INTERVAL_SEC)

    def _maybe_modify_exit_price(self, ut: UserTradeSchema, broker: ZerodhaBroker) -> bool:
        oid = getattr(ut, "exit_order_id", None)
        if not oid:
            return False

        mode = "intraday" if bool(getattr(ut, "intraday_only", False)) else "carryforward"
        op = OrderProfileSchema.fetch_order_profile(mode, getattr(ut, "instrument_type", "EQ"))
        order_type = OrderType.from_string(getattr(op, "order_type", OrderType.MARKET.value))
        variety = OrderVariety.from_string(getattr(op, "order_variety", OrderVariety.REGULAR.value))

        if order_type not in (OrderType.LIMIT, OrderType.SL):
            return False

        left = _try_int(getattr(ut, "exit_retries", None), MAX_RETRIES)
        if left <= 0:
            _persist_executor_update(ut, {
                "exit_status": ExitStatus.FAILED.value,
                "exec_status": "EXIT_RETRY_EXHAUSTED",
                "exec_status_message": "Exit retries exhausted while waiting for fill",
                "exec_last_checked_at": _now_ist_naive(),
            })
            return False

        exit_side = _opposite_side(_side(ut))
        current_price = d(getattr(ut, "exit_price", None) or 0)
        new_px = _next_modified_limit_price(ut.symbol, exit_side, current_price)
        if new_px is None or new_px <= 0:
            return False

        if not _safe_modify_order_price(broker, order_id=oid, price=new_px, variety=variety):
            return False

        _persist_executor_update(ut, {
            "exit_price": new_px,
            "exit_retries": max(left - 1, 0),
            "exec_last_checked_at": _now_ist_naive(),
            "exec_status": "EXIT_REPRICED",
            "exec_status_message": f"Modified exit order to {new_px}",
        })
        return True

    def _poll_exit_once(self, ut: UserTradeSchema, broker: ZerodhaBroker) -> bool:
        oid = getattr(ut, "exit_order_id", None)
        if not oid:
            _persist_executor_update(ut, {
                "exit_status": ExitStatus.FAILED.value,
                "exec_status": "EXIT_ORDER_ID_MISSING",
                "exec_status_message": "exit_status=SUBMITTED but exit_order_id is missing",
                "exec_last_checked_at": _now_ist_naive(),
            })
            return True

        try:
            st = broker.latest_status(oid)
            if st is None:
                _persist_executor_update(ut, {"exec_last_checked_at": _now_ist_naive()})
                return False

            if st == OrderStatus.COMPLETE:
                hist = broker.history(oid)
                avg, filled_qty = _extract_average_price_and_qty(hist)

                if avg is None or avg <= 0:
                    _persist_executor_update(ut, {
                        "exec_last_checked_at": _now_ist_naive(),
                        "exec_status": "EXIT_PRICE_PENDING_RECONCILE",
                        "exec_status_message": "Broker exit complete but average_price missing; will retry history.",
                    })
                    return False

                when = _broker_fill_time_from_history(hist) or _now_ist_naive()
                qty0 = filled_qty or (
                    _try_int(getattr(ut, "executed_entry_qty", None), 0)
                    or _try_int(getattr(ut, "quantity", 0), 0)
                    or 1
                )

                upd = _build_exit_fill_updates(
                    ut,
                    exit_px=avg,
                    qty=qty0,
                    when=when,
                    status_code=None,
                    status_message=None,
                    obs_kind="exec",
                )
                upd["exit_reconciled_at"] = when
                upd.update(_obs_update(
                    "reconcile",
                    code="EXIT_RECONCILED_FROM_EXECUTOR",
                    message=f"Exit fill confirmed directly by executor via broker history order_id={oid}.",
                    when=when,
                ))
                _persist_executor_update(ut, upd)
                return True

            if st in (OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.INVALID):
                left = _try_int(getattr(ut, "exit_retries", None), MAX_RETRIES)
                upd = {
                    "exec_status": f"EXIT_{st.value}",
                    "exec_status_message": f"Broker status={st.value}",
                    "exec_last_checked_at": _now_ist_naive(),
                }

                if left > 0:
                    upd["exit_status"] = ExitStatus.READY.value
                    upd["exit_retries"] = max(left - 1, 0)
                    upd["exit_order_id"] = None
                else:
                    upd["exit_status"] = ExitStatus.FAILED.value

                _persist_executor_update(ut, upd)
                return True

            _persist_executor_update(ut, {"exec_last_checked_at": _now_ist_naive()})
            return False

        except Exception as e:
            logger.warning("REAL exit poll failed trade_id=%s err=%s", ut.id, str(e)[:200])
            _persist_executor_update(ut, {
                "exec_status": "EXIT_POLL_FAILED",
                "exec_status_message": str(e)[:500],
                "exec_last_checked_at": _now_ist_naive(),
            })
            return False