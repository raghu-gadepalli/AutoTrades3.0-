#!/usr/bin/env python3
"""
services/trading_helper.py

Shared trade-planning / trade-creation helper.

Intent
------
- Keep dashboard routes thin.
- Keep backend AUTOGEN orchestrator thin.
- Keep trade planning / payload assembly / persistence outside routes and generator.

Ownership
---------
Routes own:
- manual/UI authorization
- target-user selection
- operator restrictions

Trade generator owns:
- AUTOGEN user eligibility
- AUTOGEN signal selection / stage policy

TradeGenHelper owns:
- object lookup
- integrity checks
- plan building
- instrument / pricing resolution
- persistence

Public entry points
-------------------
Signal / existing-signal flows
- TradeGenHelper.build_signal_plan(...)
- TradeGenHelper.create_trades_from_signal(...)

UI form flows
- TradeGenHelper.build_signal_trade_form(...)
- TradeGenHelper.build_watchlist_trade_form(...)
- TradeGenHelper.build_position_trade_form(...)

Manual flows
- TradeGenHelper.create_manual_trade(...)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from uuid import uuid4
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import or_

from configs.trade_config import TRADE_CONFIG
from configs.monitor_config import MONITOR_CONFIG
from configs.execution_config import EXECUTION_CONFIG
from enums.enums import EntryStatus, ExitStatus, SignalStatus
from schemas.signal import SignalSchema
from schemas.snapshot import SnapshotSchema
from schemas.symbol import SymbolSchema
from schemas.user import UserSchema
from schemas.user_trade import UserTradeSchema
from database.database import get_trades_db
from models.trade_models import UserTrade as UserTradeORM, Snapshot as SnapshotORM
from utils.datetime_utils import IST, business_now_naive
from utils.json_utils import sanitize_json
from services.account_governor.contracts import (
    AccountGovernorPackageLeg,
    AccountGovernorRequest,
)
from services.account_governor.service import AccountGovernorService
from services.audit.auditlog import write_auditlog
from services.trade.monitor.trademon_helper import TradeMonHelper, extract_underlying_atr
from services.trade.generator.tradegen_validator import (
    TradeDecisionHelper,
    MODE_AUTO,
    MODE_MANUAL_PREVIEW,
    MODE_MANUAL_CONFIRM,
)

logger = logging.getLogger(__name__)
_ACCOUNT_GOVERNOR = AccountGovernorService()


# =============================================================================
# time / tick helpers
# =============================================================================

def _to_ist_naive(ts: Optional[datetime]) -> Optional[datetime]:
    if ts is None or not isinstance(ts, datetime):
        return None
    try:
        if getattr(ts, "tzinfo", None) is not None:
            ts = ts.astimezone(IST)
        return ts.replace(tzinfo=None)
    except Exception:
        return ts.replace(tzinfo=None) if isinstance(ts, datetime) else None


DEFAULT_TICK = Decimal(str(EXECUTION_CONFIG.tick_size))


# =============================================================================
# config helpers
# =============================================================================

def _default_position_style() -> str:
    value = str(TRADE_CONFIG.defaults.default_position_style).strip().upper()
    if not value:
        raise ValueError("TRADE_CONFIG.defaults.default_position_style is required")
    return value


def _default_product_type() -> str:
    value = str(TRADE_CONFIG.defaults.default_product_type).strip().upper()
    if not value:
        raise ValueError("TRADE_CONFIG.defaults.default_product_type is required")
    return value


def _min_eq_amt() -> Decimal:
    """Minimum equity allocation used to derive default EQ quantity."""
    amt = Decimal(str(TRADE_CONFIG.defaults.min_eq_amt))
    if amt <= 0:
        raise ValueError("TRADE_CONFIG.defaults.min_eq_amt must be greater than zero")
    return amt


# =============================================================================
# policy helpers
# =============================================================================

def _terminal_statuses() -> List[str]:
    return [status.value for status in SignalStatus if status is not SignalStatus.OPEN]


# =============================================================================
# small helpers
# =============================================================================

def d(x: Any) -> Decimal:
    if isinstance(x, Decimal):
        return x
    if x is None:
        return Decimal("0")
    return Decimal(str(x))


def _enum_str(x: Any) -> str:
    v = getattr(x, "value", x)
    return str(v).upper().strip()


def _new_manual_signal_id() -> str:
    """Return a collision-safe synthetic signal id for a manual trade."""
    return f"MANUAL:{uuid4()}"


def _side(v: Any) -> str:
    s = _enum_str(v)
    return "BUY" if s == "BUY" else "SELL"


def _inst_code(v: Any) -> str:
    s = _enum_str(v)
    if s in ("EQ", "EQUITY"):
        return "EQ"
    if s in ("FUT", "FUTURE", "FUTURES"):
        return "FUT"
    if s in ("CE", "CALL"):
        return "CE"
    if s in ("PE", "PUT"):
        return "PE"
    return s


def _eq_sell_blocked(instrument_type: Any, side: Any) -> bool:
    # Equity SELL is allowed when equity is treated as intraday/MIS. Keep this
    # helper for older call sites, but do not apply a blanket app-wide block;
    # product-level validation handles non-intraday equity SELL.
    return False


def _option_sell_blocked(instrument_type: Any, side: Any) -> bool:
    return _inst_code(instrument_type) in ("CE", "PE") and _side(side) == "SELL"


def _trade_side_blocked(instrument_type: Any, side: Any, product: Any = None) -> Optional[str]:
    inst = _inst_code(instrument_type)
    side0 = _side(side)
    if inst == "EQ" and side0 == "SELL":
        # Equity SELL is valid only as an intraday/MIS order. When product is
        # absent (form preview / plan building), do not block; defaults resolve
        # EQ to MIS. When a caller explicitly asks for CNC/NRML, block that
        # specific trade rather than suppressing EQ SELL across the app.
        prod = str(product or "").strip().upper()
        if prod and prod not in ("MIS", "INTRADAY"):
            return "EQ_SELL_REQUIRES_INTRADAY_PRODUCT"
    if inst in ("CE", "PE") and side0 == "SELL":
        return "OPTION_SELL_NOT_ALLOWED"
    return None


def _instrument_entry_side(instrument_type: Any, signal_side: Any) -> str:
    # Options are directional long instruments in the current autotrades model:
    # BUY signal -> BUY CE, SELL signal -> BUY PE. We do not short options.
    inst = _inst_code(instrument_type)
    return "BUY" if inst in ("CE", "PE") else _side(signal_side)


def _instrument_applicable_sides(instrument_type: Any) -> List[str]:
    inst = _inst_code(instrument_type)
    if inst == "EQ":
        return ["BUY", "SELL"]
    if inst == "FUT":
        return ["BUY", "SELL"]
    if inst == "CE":
        return ["BUY"]
    if inst == "PE":
        return ["SELL"]
    return []


def _user_instrument_enabled(user: Any, instrument_type: Any) -> bool:
    inst = _inst_code(instrument_type)
    if inst == "EQ":
        return int(getattr(user, "equity", 0) or 0) == 1
    if inst == "FUT":
        return int(getattr(user, "futures", 0) or 0) == 1
    if inst in ("CE", "PE"):
        return int(getattr(user, "options", 0) or 0) == 1
    return False


def _instrument_allowed_order_sides(instrument_type: Any) -> List[str]:
    inst = _inst_code(instrument_type)
    if inst == "EQ":
        return ["BUY", "SELL"]
    if inst in ("CE", "PE"):
        return ["BUY"]
    if inst == "FUT":
        return ["BUY", "SELL"]
    return []


def _decorate_instrument_choice(choice: Optional[Dict[str, Any]], *, instrument_type: Any, side: Any) -> Optional[Dict[str, Any]]:
    if not choice:
        return None
    inst = _inst_code(instrument_type)
    out = dict(choice)
    out["instrument_type"] = inst
    out["entry_side"] = _instrument_entry_side(inst, side)
    out["applicable_sides"] = _instrument_applicable_sides(inst)
    out["allowed_sides"] = _instrument_allowed_order_sides(inst)
    if inst == "EQ":
        out["disabled_reason"] = None
        out["requires_intraday_for_sell"] = True
    elif inst == "CE":
        out["disabled_reason"] = "CE is used only for BUY direction"
    elif inst == "PE":
        out["disabled_reason"] = "PE is used only for SELL direction"
    return out


def _normalized_product_for_inst(instrument_type: Any, requested_product: Any = None) -> str:
    inst = _inst_code(instrument_type)
    prod = str(requested_product or _default_product_type()).strip().upper()
    if not prod:
        raise ValueError("trade product type is required")
    if inst == "EQ" and prod == "INTRADAY":
        return "MIS"
    return prod


def _intraday_for_inst_and_product(instrument_type: Any, product: Any) -> bool:
    prod = str(product or "").strip().upper()
    return prod in ("MIS", "INTRADAY")


def _round_to_tick(x: Optional[Decimal], tick: Decimal = DEFAULT_TICK) -> Optional[Decimal]:
    if x is None:
        return None
    if tick <= 0:
        return x
    units = (d(x) / tick).to_integral_value(rounding=ROUND_HALF_UP)
    return units * tick


def _safe_snapshot_dict(s: Any) -> Dict[str, Any]:
    if s is None:
        return {}
    if isinstance(s, dict):
        return s
    if hasattr(s, "model_dump"):
        return s.model_dump()
    if hasattr(s, "dict"):
        return s.dict()
    return dict(getattr(s, "__dict__", {}) or {})


def _as_dict_maybe_json(v: Any) -> Dict[str, Any]:
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            obj = json.loads(v)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    if hasattr(v, "model_dump"):
        try:
            obj = v.model_dump()
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}


def _safe_num(v: Any, default: Optional[Decimal] = None) -> Optional[Decimal]:
    try:
        if v is None or v == "":
            return default
        x = d(v)
        return x
    except Exception:
        return default


def _str_or_none(v: Any) -> Optional[str]:
    s = str(v).strip() if v is not None else ""
    return s or None


# =============================================================================
# adaptive trade-management helpers
# =============================================================================

def _extract_atr_from_snapshot_dict(snapshot_dict: Dict[str, Any]) -> Optional[Decimal]:
    """Extract the equity ATR value from the vNext snapshot payload."""
    snap = snapshot_dict or {}
    candidates = [
        (((snap.get("indicators") or {}).get("atr") or {}).get("value")),
    ]
    for value in candidates:
        val = _safe_num(value, None)
        if val is not None and val > 0:
            return val
    return None


def _cfg_trade_mgmt(name: str, default: Any = None) -> Any:
    cfg = getattr(MONITOR_CONFIG, "trade_management", None)
    return getattr(cfg, name, default) if cfg is not None else default


def _cfg_trade_mgmt_required(name: str) -> Any:
    cfg = getattr(MONITOR_CONFIG, "trade_management", None)
    if cfg is None or not hasattr(cfg, name):
        raise RuntimeError(f"MONITOR_CONFIG.trade_management.{name} is required")
    value = getattr(cfg, name)
    if value is None:
        raise RuntimeError(f"MONITOR_CONFIG.trade_management.{name} cannot be None")
    return value


def _snapshot_bar_high_low(snapshot_dict: Dict[str, Any]) -> Tuple[Optional[Decimal], Optional[Decimal]]:
    bar = _as_dict_maybe_json((snapshot_dict or {}).get("bar"))
    high = _safe_num(bar.get("high"), None)
    low = _safe_num(bar.get("low"), None)
    return high, low


def _signal_meta_dict(signal: SignalSchema) -> Dict[str, Any]:
    """Return signal.meta_json as a dict, tolerating JSON strings.

    Signal/setup discovery is the authoritative owner of setup reference levels.
    Trade generation should read that persisted metadata rather than rediscover
    breakout/reversal levels from the snapshot.
    """
    return _as_dict_maybe_json(getattr(signal, "meta_json", None))


def _signal_setup_levels(signal: SignalSchema) -> Dict[str, Any]:
    """Return the one canonical Auction setup-level handoff.

    Patch 5.2 established ``signal.meta_json.setup_levels`` as immutable.  Trade
    generation must not search duplicate metadata blocks or criteria JSON when
    that canonical contract is missing or malformed.
    """
    meta = _signal_meta_dict(signal)
    if "setup_levels" not in meta or not isinstance(meta["setup_levels"], dict):
        raise ValueError("signal.meta_json.setup_levels is required")
    levels = dict(meta["setup_levels"])
    required = (
        "entry_price",
        "initial_stop_reference_price",
        "reference_price",
        "reference_source",
        "opportunity_key",
        "candidate_id",
        "boundary_event_key",
        "setup_label",
        "side",
    )
    missing = [key for key in required if key not in levels or levels[key] in (None, "")]
    if missing:
        raise ValueError(f"signal.meta_json.setup_levels missing required fields: {missing}")
    for key in ("entry_price", "initial_stop_reference_price", "reference_price"):
        value = _safe_num(levels[key], None)
        if value is None or value <= 0:
            raise ValueError(f"signal.meta_json.setup_levels.{key} must be positive")
    setup_label = str(getattr(signal, "setup", "") or "").strip().upper()
    if str(levels["setup_label"]).strip().upper() != setup_label:
        raise ValueError("signal.meta_json.setup_levels setup identity mismatch")
    signal_side = _side(getattr(signal, "side", ""))
    if str(levels["side"]).strip().upper() != signal_side:
        raise ValueError("signal.meta_json.setup_levels side identity mismatch")
    return levels


def _setup_levels_initial_levels_for_leg(
    *,
    signal: SignalSchema,
    lifecycle_side: str,
    risk_side: str,
    instrument_type: str,
    instrument_entry_price: Decimal,
    equity_ref_price: Decimal,
) -> Dict[str, Any]:
    """Return trade-management handoff defaults for one trade leg.

    Signal/setup levels are signal-side evidence.  They must not be converted
    into trade_management stop/target/risk fields here, because
    TradeManagementSchema is the monitor-owned ATR-management state.

    Keep only the visible setup label for audit clarity.  Reference levels remain
    available on the originating signal metadata/setup_levels and can be wired
    into a future explicit setup-aware stop mode as a separate change.
    """
    setup_levels = _signal_setup_levels(signal)
    setup_label = str(getattr(signal, "setup", "") or "").upper().strip()
    level_setup_label = str(setup_levels.get("setup_label") or "").upper().strip() if setup_levels else ""
    if not setup_label:
        raise ValueError("SIGNAL_ORIGINATING_SETUP_MISSING_FOR_TRADE_MANAGEMENT")
    if level_setup_label and level_setup_label != setup_label:
        raise ValueError(
            "SIGNAL_SETUP_LEVEL_IDENTITY_MISMATCH "
            f"signal_id={getattr(signal, 'signal_id', None)} "
            f"signal_setup={setup_label} setup_levels={level_setup_label}"
        )

    return {
        # Leave active SL/target blank so TradeMonHelper initializes ATR_MULTIPLE.
        "initial_stop_price": None,
        "initial_stop_source": None,
        "initial_stop_reason": None,
        "initial_target_price": None,
        "initial_target_source": None,
        "initial_target_reason": None,
        "signal_setup_label": setup_label or None,
    }

def _setup_initial_levels_for_leg(
    *,
    signal: SignalSchema,
    snapshot_dict: Dict[str, Any],
    lifecycle_side: str,
    risk_side: str,
    instrument_type: str,
    instrument_entry_price: Decimal,
    equity_ref_price: Decimal,
    atr: Optional[Decimal],
) -> Dict[str, Any]:
    """Return signal setup-level-derived initial SL for one trade leg.

    Auction downstream V2 signals must persist setup_levels at CREATE time. Trade
    generation must not rediscover structure/reversal levels from snapshots.
    If setup_levels is absent or invalid, trade creation for the leg fails
    loudly so the signal layer can be fixed.

    Snapshot and ATR are accepted because the common leg-builder signature supplies them;
    this function intentionally does not use them to rediscover setup geometry.
    """
    return _setup_levels_initial_levels_for_leg(
        signal=signal,
        lifecycle_side=lifecycle_side,
        risk_side=risk_side,
        instrument_type=instrument_type,
        instrument_entry_price=instrument_entry_price,
        equity_ref_price=equity_ref_price,
    )


def _build_trade_management_payload(
    *,
    side: str,
    basis_price: Decimal,
    atr: Optional[Decimal],
    instrument_type: str = "EQ",
    initial_stop_price: Optional[Decimal] = None,
    initial_stop_source: Optional[str] = None,
    initial_stop_reason: Optional[str] = None,
    initial_target_price: Optional[Decimal] = None,
    initial_target_source: Optional[str] = None,
    initial_target_reason: Optional[str] = None,
    signal_setup_label: Optional[str] = None,
    asof_time: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build the Phase-1 trade_management payload.

    trade_management is the single active source for protective stop,
    adaptive target and expansion state. Legacy relational target/SL
    fields are intentionally not assembled here.
    """
    return TradeMonHelper.initialize_trade_management(
        side=side,
        instrument_type=instrument_type,
        entry_price=d(basis_price),
        underlying_atr=atr,
        asof_time=_to_ist_naive(asof_time) or business_now_naive(),
        initial_stop_price=initial_stop_price,
        initial_stop_source=initial_stop_source,
        initial_stop_reason=initial_stop_reason,
        initial_target_price=initial_target_price,
        initial_target_source=initial_target_source,
        initial_target_reason=initial_target_reason,
        signal_setup_label=signal_setup_label,
    )


# =============================================================================
# snapshot + derivatives extraction
# =============================================================================

def _get_signal_last_snapshot(signal: SignalSchema) -> Dict[str, Any]:
    for key in ("last_snapshot", "last_snapshot_json", "snapshot_json", "snapshot", "details"):
        if hasattr(signal, key):
            val = getattr(signal, key, None)
            dct = _as_dict_maybe_json(val)
            if dct:
                return dct
    return {}


def _resolve_future_symbol(signal: SignalSchema) -> Optional[str]:
    snap = _get_signal_last_snapshot(signal)
    deriv = snap.get("derivatives") or {}
    fut = deriv.get("future") or {}
    sym = fut.get("instrument") or fut.get("symbol")
    return str(sym).strip() if sym else None


def _resolve_future_symbol_from_snapshot(snapshot_dict: Dict[str, Any]) -> Optional[str]:
    deriv = snapshot_dict.get("derivatives") or {}
    fut = deriv.get("future") or {}
    sym = fut.get("instrument") or fut.get("symbol")
    return str(sym).strip() if sym else None


def _resolve_future_price_from_snapshot(snapshot_dict: Dict[str, Any]) -> Optional[Decimal]:
    deriv = snapshot_dict.get("derivatives") or {}
    fut = deriv.get("future") or {}
    px = fut.get("last_price") or fut.get("ltp") or None
    if px is not None and d(px) > 0:
        return d(px)
    return None


def _option_rows_from_snapshot(snapshot_dict: Dict[str, Any], is_call: bool) -> List[dict]:
    deriv = snapshot_dict.get("derivatives") or {}
    opt_lite = deriv.get("options_lite") or {}
    ladder = deriv.get("option_ladder") or {}

    rows: List[dict] = []
    if is_call:
        if isinstance(opt_lite.get("top_calls"), list):
            rows.extend([x for x in opt_lite["top_calls"] if isinstance(x, dict)])
        if isinstance(ladder.get("calls"), list):
            rows.extend([x for x in ladder["calls"] if isinstance(x, dict)])
    else:
        if isinstance(opt_lite.get("top_puts"), list):
            rows.extend([x for x in opt_lite["top_puts"] if isinstance(x, dict)])
        if isinstance(ladder.get("puts"), list):
            rows.extend([x for x in ladder["puts"] if isinstance(x, dict)])

    seen = set()
    out = []
    for r in rows:
        sym = str(r.get("symbol") or "").strip()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(r)
    return out


def _resolve_atm_option_symbol(signal: SignalSchema, *, is_call: bool) -> Optional[str]:
    snap = _get_signal_last_snapshot(signal)
    deriv = snap.get("derivatives") or {}

    opt_lite = deriv.get("options_lite") or {}
    atm = opt_lite.get("atm_strike", None)

    def _pick_from_list(rows: Any) -> Optional[str]:
        if not isinstance(rows, list) or not rows:
            return None
        if atm is not None:
            try:
                atm_f = float(atm)
                for r in rows:
                    if isinstance(r, dict) and float(r.get("strike", -1)) == atm_f and r.get("symbol"):
                        return str(r["symbol"]).strip()
            except Exception:
                pass
        for r in rows:
            if isinstance(r, dict) and r.get("symbol"):
                return str(r["symbol"]).strip()
        return None

    if is_call:
        s1 = _pick_from_list(opt_lite.get("top_calls"))
        if s1:
            return s1
    else:
        s1 = _pick_from_list(opt_lite.get("top_puts"))
        if s1:
            return s1

    ladder = deriv.get("option_ladder") or {}
    if is_call:
        s2 = _pick_from_list(ladder.get("calls"))
        if s2:
            return s2
    else:
        s2 = _pick_from_list(ladder.get("puts"))
        if s2:
            return s2

    return None


def _resolve_deriv_symbol_from_ladder(snapshot: dict, side: str, offset: int = 0) -> Optional[str]:
    try:
        ladder = (((snapshot or {}).get("derivatives") or {}).get("option_ladder") or {})
        calls = ladder.get("calls") or []
        puts = ladder.get("puts") or []
        atm = ladder.get("atm_strike")

        if not atm:
            return None

        side = _side(side)
        chain = calls if side == "BUY" else puts

        if not chain:
            return None

        chain_sorted = sorted(chain, key=lambda x: float(x.get("strike") or 0))

        atm_idx = min(
            range(len(chain_sorted)),
            key=lambda i: abs(float(chain_sorted[i].get("strike") or 0) - float(atm))
        )

        idx = atm_idx + offset
        if idx < 0 or idx >= len(chain_sorted):
            idx = atm_idx

        return chain_sorted[idx].get("symbol")

    except Exception:
        return None


def _resolve_deriv_entry_price(signal: SignalSchema, *, instrument_type: str, symbol: str) -> Optional[Decimal]:
    inst = _inst_code(instrument_type)
    snap = _get_signal_last_snapshot(signal)
    deriv = snap.get("derivatives") or {}

    if inst == "FUT":
        fut = deriv.get("future") or {}
        px = fut.get("last_price") or fut.get("ltp") or None
        if px is not None and d(px) > 0:
            return d(px)
        return None

    if inst in ("CE", "PE"):
        opt_lite = deriv.get("options_lite") or {}
        ladder = deriv.get("option_ladder") or {}

        candidates: List[dict] = []
        if inst == "CE":
            if isinstance(opt_lite.get("top_calls"), list):
                candidates.extend([x for x in opt_lite["top_calls"] if isinstance(x, dict)])
            if isinstance(ladder.get("calls"), list):
                candidates.extend([x for x in ladder["calls"] if isinstance(x, dict)])
        else:
            if isinstance(opt_lite.get("top_puts"), list):
                candidates.extend([x for x in opt_lite["top_puts"] if isinstance(x, dict)])
            if isinstance(ladder.get("puts"), list):
                candidates.extend([x for x in ladder["puts"] if isinstance(x, dict)])

        sym0 = str(symbol).strip()
        for r in candidates:
            if str(r.get("symbol", "")).strip() == sym0:
                px = r.get("ltp") or r.get("last_price") or None
                if px is not None and d(px) > 0:
                    return d(px)
        return None

    return None


def _resolve_option_choices_from_snapshot(
    snapshot_dict: Dict[str, Any],
    *,
    is_call: bool,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    rows = _option_rows_from_snapshot(snapshot_dict, is_call=is_call)
    deriv = snapshot_dict.get("derivatives") or {}
    opt_lite = deriv.get("options_lite") or {}
    atm = opt_lite.get("atm_strike", None)

    out: List[Dict[str, Any]] = []
    selected_symbol: Optional[str] = None

    for r in rows:
        sym = _str_or_none(r.get("symbol"))
        if not sym:
            continue

        strike = _safe_num(r.get("strike"))
        ltp = _safe_num(r.get("ltp"), _safe_num(r.get("last_price")))
        lotsize = TradeGenHelper._resolve_lotsize(sym)

        row = {
            "symbol": sym,
            "strike": float(strike) if strike is not None else None,
            "ltp": float(ltp) if ltp is not None else None,
            "lotsize": int(lotsize),
        }
        out.append(row)

        if selected_symbol is None and atm is not None and strike is not None:
            try:
                if float(strike) == float(atm):
                    selected_symbol = sym
            except Exception:
                pass

    if selected_symbol is None and out:
        selected_symbol = out[0]["symbol"]

    return out, selected_symbol


def _snapshot_close(snapshot: SnapshotSchema) -> Decimal:
    try:
        c = getattr(snapshot, "close", None)
        if c is not None:
            return d(c)
    except Exception:
        pass

    sd = _safe_snapshot_dict(snapshot)
    mclose = sd.get("frequencies_snapshot", {}).get("minute", {}).get("close", None)
    if mclose is None:
        raise ValueError("Snapshot missing close")
    return d(mclose)


# =============================================================================
# execution mode helpers
# =============================================================================

def _has_broker_login(user: UserSchema) -> bool:
    return int(getattr(user, "broker_login", 0) or 0) == 1


def _choose_execution_mode(user: UserSchema) -> str:
    requested = _enum_str(getattr(user, "execution_mode", "VIRTUAL") or "VIRTUAL")
    if requested == "REAL" and not _has_broker_login(user):
        return "VIRTUAL"
    return "REAL" if requested == "REAL" else "VIRTUAL"


def _pick_trade_time(snapshot: Optional[SnapshotSchema], signal: SignalSchema) -> datetime:
    for k in (
        "actionable_time",
        "qualified_time",
        "stage_changed_time",
        "last_snapshot_time",
        "last_eval_time",
        "first_seen_time",
    ):
        ts = getattr(signal, k, None)
        if ts is not None:
            return ts

    if snapshot is not None:
        ts = getattr(snapshot, "snapshot_time", None)
        if ts is not None:
            return ts

    raise ValueError("Cannot determine trade time")

def _fetch_snapshot_for_signal(signal: SignalSchema) -> Optional[SnapshotSchema]:
    """
    Fetch the snapshot that should be used for signal-based trade creation.

    For AUTOGEN/replay, prefer actionable_time because trade creation should be
    priced at the first actionable point, not the latest signal update.
    """
    symbol = str(getattr(signal, "symbol", "") or "").strip()
    if not symbol:
        return None

    lookup_time = None
    for k in (
        "actionable_time",
        "qualified_time",
        "stage_changed_time",
        "last_snapshot_time",
        "last_eval_time",
        "first_seen_time",
    ):
        ts = getattr(signal, k, None)
        if ts is not None:
            lookup_time = ts
            break

    if lookup_time is None:
        return SnapshotSchema.fetch_latest_for_symbol(symbol)

    lookup_time = _to_ist_naive(lookup_time)

    try:
        with get_trades_db() as db:
            row = (
                db.query(SnapshotORM)
                .filter(SnapshotORM.symbol == symbol)
                .filter(SnapshotORM.snapshot_time <= lookup_time)
                .order_by(SnapshotORM.snapshot_time.desc())
                .first()
            )

        if row and getattr(row, "data", None):
            return SnapshotSchema.from_db_dict(row.data)

    except Exception:
        logger.exception(
            "TradeGenHelper: failed to fetch signal-time snapshot | symbol=%s lookup_time=%s",
            symbol,
            lookup_time,
        )

    return SnapshotSchema.fetch_latest_for_symbol(symbol)

def _active_entry_status_values() -> List[str]:
    """Entry statuses that mean a trade already exists for a signal.

    Important for AUTOGEN: CREATED must count as active because REAL trades may
    intentionally remain unsubmitted when autotrade is off. Without this, the
    generator can keep creating fresh option strikes for the same signal on
    later ticks.
    """
    return [
        EntryStatus.CREATED.value,
        getattr(EntryStatus, "READY", EntryStatus.CREATED).value,
        EntryStatus.SUBMITTED.value,
        EntryStatus.FILLED.value,
    ]


def _existing_active_trades_for_signal(*, userid: str, signal_id: str) -> List[UserTradeSchema]:
    """Return active trades already linked to this user + signal.

    This is intentionally signal-level, not symbol-level. A later snapshot
    may resolve a different CE/PE strike for the same signal; that should
    not create another leg unless the earlier trade has been cancelled/invalidated
    or fully exited.
    """
    uid = str(userid or "").strip()
    opp = str(signal_id or "").strip()
    if not uid or not opp:
        return []

    terminal_entries = {EntryStatus.CANCELLED.value, EntryStatus.REJECTED.value, EntryStatus.INVALID.value}
    terminal_exits = {ExitStatus.FILLED.value, ExitStatus.CANCELLED.value}

    try:
        with get_trades_db() as db:
            rows = (
                db.query(UserTradeORM)
                .filter(UserTradeORM.userid == uid)
                .filter(UserTradeORM.signal_id == opp)
                .filter(UserTradeORM.entry_status.in_(_active_entry_status_values()))
                .filter(~UserTradeORM.entry_status.in_(terminal_entries))
                .filter(~UserTradeORM.exit_status.in_(terminal_exits))
                .order_by(UserTradeORM.id.asc())
                .all()
            )
        return [UserTradeSchema.model_validate(r) for r in rows]
    except Exception:
        logger.exception(
            "TradeGenHelper: active trade lookup failed userid=%s signal_id=%s",
            uid,
            opp,
        )
        # Fail closed for duplicate prevention. A DB lookup failure should not
        # create duplicate broker-facing rows.
        return [
            UserTradeSchema(
                userid=uid,
                signal_id=opp,
                symbol="LOOKUP_FAILED",
                equity_ref="LOOKUP_FAILED",
                instrument_type="EQ",
                trade_type="BUY",
                entry_snapshot={},
                entry_time=business_now_naive(),
                last_time=business_now_naive(),
                max_time=business_now_naive(),
                min_time=business_now_naive(),
            )
        ]


def _open_exit_status_filter():
    """SQLAlchemy filter for rows that are still open/pending exit.

    Important: ``NOT IN`` alone does not match NULL. Many live rows carry
    ``exit_status`` as NULL/NONE until an exit is intended, so include those
    explicitly.
    """
    terminal_exits = {
        ExitStatus.FILLED.value,
        ExitStatus.CANCELLED.value,
        "EXITED",
        "CLOSED",
        "REPLACED",
    }
    return or_(
        UserTradeORM.exit_status.is_(None),
        UserTradeORM.exit_status == "",
        UserTradeORM.exit_status == ExitStatus.NONE.value,
        ~UserTradeORM.exit_status.in_(terminal_exits),
    )


def _existing_open_trades_for_symbol(*, userid: str, symbol: str) -> List[UserTradeSchema]:
    """Return open rows for the exact traded instrument symbol.

    AUTOGEN duplicate protection should be instrument-name based, not broad
    signal/instrument-type based. This lets a later lifecycle continuation
    recreate a FUT/CE/PE/EQ leg after the earlier leg has exited, while still
    preventing duplicate open rows for the same broker instrument.
    """
    uid = str(userid or "").strip()
    sym = str(symbol or "").strip()
    if not uid or not sym:
        return []

    terminal_entries = {EntryStatus.CANCELLED.value, EntryStatus.REJECTED.value, EntryStatus.INVALID.value}

    try:
        with get_trades_db() as db:
            rows = (
                db.query(UserTradeORM)
                .filter(UserTradeORM.userid == uid)
                .filter(UserTradeORM.symbol == sym)
                .filter(UserTradeORM.entry_status.in_(_active_entry_status_values()))
                .filter(~UserTradeORM.entry_status.in_(terminal_entries))
                .filter(_open_exit_status_filter())
                .order_by(UserTradeORM.id.asc())
                .all()
            )
        return [UserTradeSchema.model_validate(r) for r in rows]
    except Exception:
        logger.exception(
            "TradeGenHelper: open symbol lookup failed userid=%s symbol=%s",
            uid,
            sym,
        )
        return [
            UserTradeSchema(
                userid=uid,
                signal_id="LOOKUP_FAILED",
                symbol=sym or "LOOKUP_FAILED",
                equity_ref="LOOKUP_FAILED",
                instrument_type="EQ",
                trade_type="BUY",
                entry_snapshot={},
                entry_time=business_now_naive(),
                last_time=business_now_naive(),
                max_time=business_now_naive(),
                min_time=business_now_naive(),
            )
        ]


def _trade_key_exists(*, userid: str, signal_id: str, symbol: str) -> bool:
    uid = str(userid or "").strip()
    opp = str(signal_id or "").strip()
    sym = str(symbol or "").strip()
    if not uid or not opp or not sym:
        return False
    try:
        with get_trades_db() as db:
            return bool(
                db.query(UserTradeORM.id)
                .filter(UserTradeORM.userid == uid)
                .filter(UserTradeORM.signal_id == opp)
                .filter(UserTradeORM.symbol == sym)
                .first()
            )
    except Exception:
        logger.exception(
            "TradeGenHelper: trade key lookup failed userid=%s signal_id=%s symbol=%s",
            uid, opp, sym,
        )
        # Fail closed for duplicate-key mutation; do not alter the key if unsure.
        return False


def _trade_instrument_key_exists(*, userid: str, signal_id: str, instrument_type: str) -> bool:
    """Return True if this signal already created this instrument family.

    This is intentionally broader than the exact symbol key. Option strikes can
    change as the underlying moves, but a single signal should not
    keep adding fresh CE/PE strikes while the same setup remains open.

    Re-entry/add-on policy should be explicit later. Until then, one signal can
    create at most one EQ, one FUT, and one option leg of the applicable type.
    """
    uid = str(userid or "").strip()
    opp = str(signal_id or "").strip()
    inst = _inst_code(instrument_type)
    if not uid or not opp or not inst:
        return False
    try:
        with get_trades_db() as db:
            return bool(
                db.query(UserTradeORM.id)
                .filter(UserTradeORM.userid == uid)
                .filter(UserTradeORM.signal_id == opp)
                .filter(UserTradeORM.instrument_type == inst)
                .first()
            )
    except Exception:
        logger.exception(
            "TradeGenHelper: trade instrument-key lookup failed userid=%s signal_id=%s instrument_type=%s",
            uid, opp, inst,
        )
        # Fail closed for broker-facing autogen safety.
        return True


def _existing_open_trades_for_signal_instrument(
    *,
    userid: str,
    signal_id: str,
    instrument_type: str,
) -> List[UserTradeSchema]:
    """Return currently open rows for this user + signal + instrument family."""
    uid = str(userid or "").strip()
    opp = str(signal_id or "").strip()
    inst = _inst_code(instrument_type)
    if not uid or not opp or not inst:
        return []

    terminal_entries = {EntryStatus.CANCELLED.value, EntryStatus.REJECTED.value, EntryStatus.INVALID.value}

    try:
        with get_trades_db() as db:
            rows = (
                db.query(UserTradeORM)
                .filter(UserTradeORM.userid == uid)
                .filter(UserTradeORM.signal_id == opp)
                .filter(UserTradeORM.instrument_type == inst)
                .filter(UserTradeORM.entry_status.in_(_active_entry_status_values()))
                .filter(~UserTradeORM.entry_status.in_(terminal_entries))
                .filter(_open_exit_status_filter())
                .order_by(UserTradeORM.id.asc())
                .all()
            )
        return [UserTradeSchema.model_validate(r) for r in rows]
    except Exception:
        logger.exception(
            "TradeGenHelper: open signal/instrument lookup failed userid=%s signal_id=%s inst=%s",
            uid, opp, inst,
        )
        return [
            UserTradeSchema(
                userid=uid,
                signal_id=opp or "LOOKUP_FAILED",
                symbol="LOOKUP_FAILED",
                equity_ref="LOOKUP_FAILED",
                instrument_type=inst or "EQ",
                trade_type="BUY",
                entry_snapshot={},
                entry_time=business_now_naive(),
                last_time=business_now_naive(),
                max_time=business_now_naive(),
                min_time=business_now_naive(),
            )
        ]


def _existing_trade_summary(rows: List[UserTradeSchema]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        out.append({
            "id": getattr(r, "id", None),
            "symbol": getattr(r, "symbol", None),
            "instrument_type": _inst_code(getattr(r, "instrument_type", None)),
            "entry_status": _enum_str(getattr(r, "entry_status", None)),
            "exit_status": _enum_str(getattr(r, "exit_status", None)),
            "execution_mode": getattr(r, "execution_mode", None),
        })
    return out


# =============================================================================
# stage/status gating
# =============================================================================

def _signal_stage(signal: SignalSchema) -> str:
    return _enum_str(getattr(signal, "stage", "") or "")


def _signal_status(signal: SignalSchema) -> str:
    return _enum_str(getattr(signal, "status", "") or "")


def _is_terminal(signal: SignalSchema) -> bool:
    return _signal_status(signal) in set(_terminal_statuses())


def _stage_allowed(signal: SignalSchema, allowed_stages: List[str]) -> bool:
    return _signal_stage(signal) in set([str(x).upper().strip() for x in (allowed_stages or [])])


# =============================================================================
# planning containers
# =============================================================================

@dataclass(frozen=True)
class TradeLegPlan:
    instrument_type: str
    trade_symbol: str
    lotsize: int
    entry_price_exec: Decimal
    risk_ref_price_eq: Decimal
    quantity_override: Optional[int] = None


@dataclass(frozen=True)
class TradePlan:
    user: UserSchema
    signal: SignalSchema
    snapshot: SnapshotSchema
    side: str
    equity_ref: str
    trade_time: datetime
    execution_mode: str
    product_type: str
    intraday_only: bool
    position_style: str
    legs: List[TradeLegPlan]
    source: str
    message: str


def _account_governor_quantity_for_leg(*, plan: TradePlan, leg: TradeLegPlan) -> Optional[int]:
    requested = int(getattr(leg, "quantity_override", 0) or 0)
    if requested > 0:
        return requested
    signal_quantity = int(getattr(plan.signal, "quantity", 0) or 0)
    if signal_quantity > 0:
        return signal_quantity
    if _inst_code(leg.instrument_type) != "EQ":
        return max(int(leg.lotsize or 1), 1)
    # The EQ quantity is derived from min_eq_amt during assembly. Keep it
    # explicitly unknown in the neutral contract rather than duplicate sizing
    # logic inside Account Governor.
    return None


def _account_governor_request_from_plan(plan: TradePlan) -> AccountGovernorRequest:
    return AccountGovernorRequest(
        userid=getattr(plan.user, "userid", None),
        as_of=plan.trade_time,
        source=plan.source,
        signal_id=getattr(plan.signal, "signal_id", None),
        equity_ref=plan.equity_ref,
        side=plan.side,
        execution_mode=plan.execution_mode,
        product_type=plan.product_type,
        position_style=plan.position_style,
        intraday_only=bool(plan.intraday_only),
        proposed_legs=tuple(
            AccountGovernorPackageLeg(
                instrument_type=_inst_code(leg.instrument_type),
                symbol=leg.trade_symbol,
                side=_instrument_entry_side(leg.instrument_type, plan.side),
                entry_price=leg.entry_price_exec,
                quantity=_account_governor_quantity_for_leg(plan=plan, leg=leg),
                lotsize=max(int(leg.lotsize or 1), 1),
            )
            for leg in plan.legs
        ),
    )


def _account_governor_request_from_manual_payload(
    *,
    payload: Dict[str, Any],
) -> AccountGovernorRequest:
    return AccountGovernorRequest(
        userid=payload["userid"],
        as_of=payload["entry_time"],
        source=payload["source"],
        signal_id=payload["signal_id"],
        equity_ref=payload["equity_ref"],
        side=payload["trade_type"],
        execution_mode=payload["execution_mode"],
        product_type=payload["product_type"],
        position_style=payload["position_style"],
        intraday_only=bool(payload["intraday_only"]),
        proposed_legs=(
            AccountGovernorPackageLeg(
                instrument_type=payload["instrument_type"],
                symbol=payload["symbol"],
                side=payload["trade_type"],
                entry_price=payload["entry_price"],
                quantity=int(payload["quantity"]),
                lotsize=max(int(payload["lotsize"] or 1), 1),
            ),
        ),
    )


def _assess_and_audit_account_governor(
    *,
    request: AccountGovernorRequest,
) -> Dict[str, Any]:
    assessment = _ACCOUNT_GOVERNOR.assess_package(request=request)
    request_json = request.model_dump(mode="json")
    assessment_json = assessment.model_dump(mode="json")
    audit_persisted = write_auditlog(
        entity_type="ACCOUNT_GOVERNOR",
        entity_id=request.signal_id,
        symbol=request.equity_ref,
        userid=request.userid,
        evaluation_stage="PACKAGE_AUTHORIZATION",
        previous_state=None,
        new_state=assessment.state.value,
        action="ASSESS_PACKAGE",
        reason_code=(
            assessment.reason_codes[0]
            if assessment.reason_codes
            else "ACCOUNT_GOVERNOR_ASSESSED"
        ),
        reason_text=", ".join(assessment.reason_codes),
        confidence=None,
        ts=request.as_of,
        payload_json={
            "snapshot_time": request.as_of,
            "request": request_json,
            "assessment": assessment_json,
        },
        force_persist=True,
    )
    logger.info(
        "ACCOUNT_GOVERNOR_ASSESSMENT | userid=%s signal_id=%s source=%s "
        "availability=%s state=%s influence=%s new_entry_allowed=%s "
        "force_flat=%s reasons=%s audit_persisted=%s",
        request.userid,
        request.signal_id,
        request.source,
        assessment.availability.value,
        assessment.state.value,
        assessment.influence.value,
        assessment.new_entry_allowed,
        assessment.force_flat,
        list(assessment.reason_codes),
        audit_persisted,
    )
    return {
        "request": request_json,
        "assessment": assessment_json,
        "audit_persisted": bool(audit_persisted),
    }


# =============================================================================
# assembler / persister
# =============================================================================


class UserTradeDataAssembler:
    @staticmethod
    def assemble_from_plan(*, plan: TradePlan, leg: TradeLegPlan) -> Dict[str, Any]:
        user = plan.user
        signal = plan.signal
        snapshot = plan.snapshot
        trade_time = plan.trade_time
        side_str = plan.side

        inst = _inst_code(leg.instrument_type)
        trade_symbol = (leg.trade_symbol or "").strip()
        if not trade_symbol:
            raise ValueError("Missing trade symbol")

        signal_id = getattr(signal, "signal_id", None) or getattr(signal, "signal_id", None)
        if not signal_id:
            raise ValueError("Signal missing signal_id/signal_id")

        entry_price_exec = d(leg.entry_price_exec)
        if entry_price_exec <= 0:
            raise ValueError("Entry price (exec) <= 0")

        risk_ref_eq = d(leg.risk_ref_price_eq)
        if risk_ref_eq <= 0:
            raise ValueError("Risk reference price <= 0")

        requested_qty = int(getattr(leg, "quantity_override", 0) or 0)
        signal_qty = int(getattr(signal, "quantity", 0) or 0)
        if requested_qty > 0:
            qty = requested_qty
        elif inst == "EQ":
            if signal_qty > 0:
                qty = signal_qty
            else:
                amt = _min_eq_amt()
                qty = int((amt / entry_price_exec).to_integral_value(rounding="ROUND_FLOOR"))
                qty = max(1, qty)
        else:
            qty = signal_qty if signal_qty > 0 else 0
            qty = max(int(leg.lotsize or 1), int(qty or 0))

        # Risk/target basis must be the traded instrument price, not always
        # the underlying equity reference price. Earlier logic used the equity
        # snapshot close for all legs, which produced unusable targets/SL for
        # options (for example, CE entry 430 with target around 13,000).
        #
        # EQ  : entry_price_exec is equity price
        # FUT : entry_price_exec is future price
        # CE/PE: entry_price_exec is option premium
        risk_basis = entry_price_exec

        # Options are bought in this version, even for bearish equity signals
        # (SELL setup -> BUY PE). Therefore option premium risk/targets must
        # be calculated as BUY-side movement. FUT/EQ follow lifecycle side.
        risk_side = "BUY" if inst in ("CE", "PE") else side_str

        # Phase-1 cleanup: legacy target/SL DB fields are no longer assembled here.
        # TradeMonHelper.initialize_trade_management(...) is the single source
        # for protective stop, adaptive target and expansion state.

        entry_snap = _safe_snapshot_dict(snapshot)
        last_snap = _safe_snapshot_dict(snapshot)
        entry_atr = _extract_atr_from_snapshot_dict(entry_snap)

        # Execution/order side can differ from lifecycle side.
        #
        # Lifecycle side:
        #   BUY  -> bullish setup
        #   SELL -> bearish setup
        #
        # Order side:
        #   EQ/FUT follows lifecycle side.
        #   CE/PE are always bought in the current autotrades model.
        #   Therefore:
        #       BUY signal  -> BUY CE
        #       SELL signal -> BUY PE
        #
        # We intentionally do not write SELL options unless option-writing is
        # explicitly introduced as a separate future feature.
        order_side = "BUY" if inst in ("CE", "PE") else side_str

        setup_levels = _setup_initial_levels_for_leg(
            signal=signal,
            snapshot_dict=entry_snap,
            lifecycle_side=side_str,
            risk_side=risk_side,
            instrument_type=inst,
            instrument_entry_price=entry_price_exec,
            equity_ref_price=risk_ref_eq,
            atr=entry_atr,
        )

        # AUTOGEN queueing policy:
        #   - Manual/UI created trades remain CREATED until user confirms/submits.
        #   - Trade-generator AUTOGEN trades move to READY only when the user's
        #     autotrade flag is enabled.
        #   - Executor owns READY -> SUBMITTED -> FILLED.
        source_key = str(plan.source or "").strip().upper()
        autotrade_enabled = int(getattr(user, "autotrade", 0) or 0) == 1
        queue_for_executor = source_key == "TRADE_GENERATOR" and autotrade_enabled

        ready_status = getattr(EntryStatus, "READY", None)
        ready_value = getattr(ready_status, "value", "READY")
        entry_status = ready_value if queue_for_executor else EntryStatus.CREATED.value

        # entry_time preserves the originating market/signal timestamp.
        # entry_intent_time records when the package became READY for execution.
        # The executor is mechanical and does not expire READY packages by age.
        queue_time = business_now_naive() if queue_for_executor else None
        entry_intent_time = queue_time
        exec_last_checked_at = queue_time

        return {
            "userid": user.userid,
            "signal_id": str(signal_id),
            "symbol": trade_symbol,
            "equity_ref": plan.equity_ref or trade_symbol,
            "instrument_type": inst,
            "trade_type": order_side,

            "position_style": plan.position_style,
            "hedged_symbol": getattr(signal, "hedged_symbol", None) or None,

            "source": plan.source,
            "message": plan.message or "",

            "entry_snapshot": entry_snap,
            "last_snapshot": last_snap,

            "entry_status": entry_status,
            "exit_status": ExitStatus.NONE.value,
            "execution_mode": plan.execution_mode,
            "intraday_only": bool(plan.intraday_only),

            "entry_time": trade_time,
            "entry_intent_time": entry_intent_time,
            "entry_exec_time": None,
            "entry_reconciled_at": None,
            "entry_price": entry_price_exec,
            "executed_entry_price": None,
            "executed_entry_qty": None,
            "quantity": int(qty),

            "entry_order_id": None,
            "entry_order_response_json": None,
            "entry_retries": 5,

            # Clean adaptive management is the only active target/SL source.
            "trade_management": _build_trade_management_payload(
                side=risk_side,
                basis_price=entry_price_exec,
                atr=entry_atr,
                instrument_type=inst,
                initial_stop_price=setup_levels.get("initial_stop_price"),
                initial_stop_source=setup_levels.get("initial_stop_source"),
                initial_stop_reason=setup_levels.get("initial_stop_reason"),
                initial_target_price=setup_levels.get("initial_target_price"),
                initial_target_source=setup_levels.get("initial_target_source"),
                initial_target_reason=setup_levels.get("initial_target_reason"),
                signal_setup_label=setup_levels.get("signal_setup_label"),
            ),

            "exit_reason": None,
            "exit_rule": None,
            "exit_time": None,
            "exit_intent_time": None,
            "exit_exec_time": None,
            "exit_reconciled_at": None,
            "exit_price": None,
            "executed_exit_price": None,
            "executed_exit_qty": 0,
            "exit_pnl": None,

            "exit_order_id": None,
            "exit_order_response_json": None,
            "exit_retries": 5,

            "last_time": trade_time,
            "last_price": entry_price_exec,
            "last_pnl": Decimal("0"),
            "last_pnl_value": Decimal("0"),
            "max_price": entry_price_exec,
            "min_price": entry_price_exec,
            "max_time": trade_time,
            "min_time": trade_time,

            "exec_last_checked_at": exec_last_checked_at,
            "exec_status": None,
            "exec_status_message": None,

            "reconcile_last_checked_at": None,
            "reconcile_status": None,
            "reconcile_status_message": None,

            "risk_ref_price": risk_basis,
            "product_type": _normalized_product_for_inst(inst, plan.product_type),
        }


class UserTradePersister:
    @staticmethod
    def persist(payload: Dict[str, Any]) -> Optional[UserTradeSchema]:
        return UserTradeSchema.create_user_trade(payload)


# =============================================================================
# helper
# =============================================================================

class TradeGenHelper:
    """
    Shared helper for trade planning / creation.

    Caller owns user-eligibility policy.
    Helper owns planning / integrity / persistence.
    """

    @staticmethod
    def _resolve_lotsize(symbol: str) -> int:
        try:
            rec = SymbolSchema.fetch_symbol(symbol)
            if rec and getattr(rec, "lotsize", None):
                val = int(rec.lotsize)
                return val if val >= 1 else 1
        except Exception:
            pass
        return 1

    @staticmethod
    def _resolve_signal(signal_id: str) -> Optional[SignalSchema]:
        try:
            return SignalSchema.fetch_by_signal_id(str(signal_id))
        except Exception:
            logger.exception("TradeGenHelper: failed to fetch signal_id=%s", signal_id)
            return None

    @staticmethod
    def build_unsubmitted_plan_refresh(
        trade: Any,
        *,
        asof_time: Optional[datetime],
        refresh_reason: str,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Rebuild one unsubmitted entry plan from the latest completed snapshot.

        The traded symbol and requested quantity are preserved. Entry price,
        snapshot context, ATR-based stop/target state, and mark fields are
        refreshed atomically. A caller must persist the returned mapping in the
        same transaction that queues or executes the trade.
        """
        status = _enum_str(
            getattr(trade, "entry_status", EntryStatus.CREATED.value)
        )
        allowed_statuses = {
            EntryStatus.CREATED.value,
            getattr(EntryStatus, "READY", EntryStatus.CREATED).value,
        }
        if status not in allowed_statuses:
            raise ValueError(
                "PLAN_REFRESH_REQUIRES_UNSUBMITTED_ENTRY "
                f"entry_status={status}"
            )

        if int(getattr(trade, "executed_entry_qty", 0) or 0) > 0:
            raise ValueError("PLAN_REFRESH_BLOCKED_AFTER_ENTRY_FILL")
        if str(getattr(trade, "entry_order_id", "") or "").strip():
            raise ValueError("PLAN_REFRESH_BLOCKED_AFTER_BROKER_SUBMISSION")

        equity_ref = str(
            getattr(trade, "equity_ref", "") or ""
        ).strip().upper()
        trade_symbol = str(
            getattr(trade, "symbol", "") or ""
        ).strip().upper()
        instrument_type = _inst_code(
            getattr(trade, "instrument_type", "")
        )

        if not equity_ref or not trade_symbol:
            raise ValueError("PLAN_REFRESH_REQUIRES_TRADE_SYMBOL_AND_EQUITY_REF")
        if instrument_type not in ("EQ", "FUT", "CE", "PE"):
            raise ValueError(
                f"PLAN_REFRESH_UNSUPPORTED_INSTRUMENT={instrument_type}"
            )

        asof = _to_ist_naive(asof_time) or business_now_naive()
        snapshot = SnapshotSchema.fetch_latest_for_symbol_asof(
            equity_ref,
            asof,
        )
        if snapshot is None:
            raise ValueError(
                f"PLAN_REFRESH_NO_COMPLETED_SNAPSHOT symbol={equity_ref}"
            )

        snapshot_time = _to_ist_naive(
            getattr(snapshot, "snapshot_time", None)
        )
        if snapshot_time is None:
            raise ValueError("PLAN_REFRESH_SNAPSHOT_TIME_MISSING")

        current_snapshot = _as_dict_maybe_json(
            getattr(trade, "last_snapshot", None)
        )
        current_snapshot_time = current_snapshot.get("snapshot_time")
        if isinstance(current_snapshot_time, str):
            try:
                current_snapshot_time = datetime.fromisoformat(
                    current_snapshot_time.replace("Z", "+00:00")
                )
            except Exception:
                current_snapshot_time = None
        current_snapshot_time = _to_ist_naive(current_snapshot_time)

        if (
            not force
            and current_snapshot_time is not None
            and snapshot_time <= current_snapshot_time
        ):
            return {}

        snapshot_dict = sanitize_json(snapshot.to_db_dict())
        equity_price = _snapshot_close(snapshot)

        refreshed_entry: Optional[Decimal] = None
        if instrument_type == "EQ":
            refreshed_entry = equity_price
        elif instrument_type == "FUT":
            latest_future_symbol = str(
                _resolve_future_symbol_from_snapshot(snapshot_dict) or ""
            ).strip().upper()
            if not latest_future_symbol or latest_future_symbol != trade_symbol:
                raise ValueError(
                    "PLAN_REFRESH_FUTURE_SYMBOL_UNAVAILABLE "
                    f"stored={trade_symbol} latest={latest_future_symbol or 'NONE'}"
                )
            refreshed_entry = _resolve_future_price_from_snapshot(snapshot_dict)
        else:
            option_rows = _option_rows_from_snapshot(
                snapshot_dict,
                is_call=(instrument_type == "CE"),
            )
            for row in option_rows:
                if str(row.get("symbol") or "").strip().upper() != trade_symbol:
                    continue
                refreshed_entry = _safe_num(
                    row.get("ltp"),
                    _safe_num(row.get("last_price"), None),
                )
                if refreshed_entry is not None and refreshed_entry > 0:
                    break

        refreshed_entry = _round_to_tick(d(refreshed_entry))
        if refreshed_entry <= 0:
            raise ValueError(
                "PLAN_REFRESH_INSTRUMENT_PRICE_UNAVAILABLE "
                f"instrument={instrument_type} symbol={trade_symbol}"
            )

        refreshed_atr = _extract_atr_from_snapshot_dict(snapshot_dict)
        if refreshed_atr is None or refreshed_atr <= 0:
            raise ValueError(
                f"PLAN_REFRESH_ATR_UNAVAILABLE symbol={equity_ref}"
            )

        old_management = _as_dict_maybe_json(
            getattr(trade, "trade_management", None)
        )
        if not old_management:
            raise ValueError("PLAN_REFRESH_TRADE_MANAGEMENT_MISSING")

        stop_source = str(
            old_management.get("initial_stop_source") or "ATR_MULTIPLE"
        ).upper().strip()
        target_source = str(
            old_management.get("initial_target_source") or "ATR_MULTIPLE"
        ).upper().strip()

        setup_stop = (
            old_management.get("initial_stop_price")
            if stop_source == "SIGNAL_SETUP_LEVEL"
            else None
        )
        setup_target = (
            old_management.get("initial_target_price")
            if target_source == "SIGNAL_SETUP_TARGET"
            else None
        )

        risk_side = (
            "BUY"
            if instrument_type in ("CE", "PE")
            else _side(getattr(trade, "trade_type", "BUY"))
        )
        refreshed_management = _build_trade_management_payload(
            side=risk_side,
            basis_price=refreshed_entry,
            atr=refreshed_atr,
            instrument_type=instrument_type,
            initial_stop_price=setup_stop,
            initial_stop_source=stop_source,
            initial_stop_reason=old_management.get("initial_stop_reason"),
            initial_target_price=setup_target,
            initial_target_source=target_source,
            initial_target_reason=old_management.get("initial_target_reason"),
            signal_setup_label=old_management.get("signal_setup_label"),
            asof_time=snapshot_time,
        )

        reason = str(refresh_reason or "PRE_ENTRY_PLAN_REFRESH").strip()[:80]
        refreshed_management["last_update_reason"] = reason
        refreshed_management["group_update_source"] = "PLAN_REFRESH"
        refreshed_management["group_update_reason"] = reason

        return {
            "entry_price": refreshed_entry,
            "trade_management": refreshed_management,
            "last_snapshot": snapshot_dict,
            "last_time": snapshot_time,
            "last_price": refreshed_entry,
            "last_pnl": Decimal("0"),
            "last_pnl_value": Decimal("0"),
            "max_price": refreshed_entry,
            "min_price": refreshed_entry,
            "max_time": snapshot_time,
            "min_time": snapshot_time,
        }

    @staticmethod
    def _default_qty_for_inst(inst: str, entry_price: Decimal, lotsize: int) -> int:
        inst = _inst_code(inst)
        px = d(entry_price)
        if inst == "EQ":
            if px <= 0:
                return 1
            amt = _min_eq_amt()
            qty = int((amt / px).to_integral_value(rounding="ROUND_FLOOR"))
            return max(1, qty)
        return max(int(lotsize or 1), 1)

    @staticmethod
    def _default_eq_base_qty(entry_price: Decimal) -> int:
        return TradeGenHelper._default_qty_for_inst("EQ", entry_price, 1)

    @staticmethod
    def _form_field_meta() -> Dict[str, Any]:
        return {
            "symbol_editable": False,
            "option_symbol_editable": True,
            "instrument_editable": True,
            "side_editable": True,
            "product_editable": True,
            "qty_editable": False,
            "lotsize_editable": False,
            "price_editable": False,
            "sl_editable": False,
            "target_editable": False,
        }

    @staticmethod
    def _build_eq_choice(*, trade_symbol: str, entry_price: Decimal, equity_ref: Optional[str] = None) -> Dict[str, Any]:
        qty = TradeGenHelper._default_eq_base_qty(entry_price)
        return {
            "instrument_type": "EQ",
            "trade_symbol": trade_symbol,
            "equity_ref": equity_ref or trade_symbol,
            "entry_price": str(entry_price),
            "lotsize": 1,
            "qty": qty,
            "base_qty": qty,
            "options": [
                {
                    "symbol": trade_symbol,
                    "display": trade_symbol,
                    "entry_price": str(entry_price),
                    "lotsize": 1,
                    "qty": qty,
                    "base_qty": qty,
                    "selected": True,
                }
            ],
        }

    @staticmethod
    def _build_fut_choice(*, trade_symbol: Optional[str], entry_price: Optional[Decimal], equity_ref: Optional[str] = None) -> Optional[Dict[str, Any]]:
        sym = _str_or_none(trade_symbol)
        if not sym:
            return None

        px = d(entry_price or 0)
        if px <= 0:
            return None

        lotsize = TradeGenHelper._resolve_lotsize(sym)
        qty = TradeGenHelper._default_qty_for_inst("FUT", px, lotsize)

        return {
            "instrument_type": "FUT",
            "trade_symbol": sym,
            "equity_ref": equity_ref,
            "entry_price": str(px),
            "lotsize": int(lotsize),
            "qty": int(qty),
            "base_qty": int(lotsize),
            "options": [
                {
                    "symbol": sym,
                    "display": sym,
                    "entry_price": str(px),
                    "lotsize": int(lotsize),
                    "qty": int(qty),
                    "base_qty": int(lotsize),
                    "selected": True,
                }
            ],
        }

    @staticmethod
    def _build_opt_choice_from_snapshot(
        *,
        snapshot_dict: Dict[str, Any],
        instrument_type: str,
        equity_ref: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        inst = _inst_code(instrument_type)
        if inst not in ("CE", "PE"):
            return None

        is_call = inst == "CE"
        rows, selected_symbol = _resolve_option_choices_from_snapshot(snapshot_dict, is_call=is_call)
        if not rows:
            return None

        options = []
        selected_entry_price = None
        selected_lotsize = None
        selected_qty = None

        for r in rows:
            sym = r["symbol"]
            lotsize = int(r.get("lotsize") or 1)
            px = _safe_num(r.get("ltp"), Decimal("0")) or Decimal("0")
            qty = TradeGenHelper._default_qty_for_inst(inst, px if px > 0 else Decimal("1"), lotsize)

            row = {
                "symbol": sym,
                "display": f"{sym} ({r.get('strike') if r.get('strike') is not None else 'NA'})",
                "strike": r.get("strike"),
                "entry_price": str(px),
                "lotsize": lotsize,
                "qty": qty,
                "base_qty": lotsize,
                "selected": bool(sym == selected_symbol),
            }
            options.append(row)

            if sym == selected_symbol:
                selected_entry_price = px
                selected_lotsize = lotsize
                selected_qty = qty

        if selected_symbol is None and options:
            options[0]["selected"] = True
            selected_symbol = options[0]["symbol"]
            selected_entry_price = d(options[0]["entry_price"])
            selected_lotsize = int(options[0]["lotsize"])
            selected_qty = int(options[0]["qty"])

        if not selected_symbol:
            return None

        return {
            "instrument_type": inst,
            "trade_symbol": selected_symbol,
            "equity_ref": equity_ref,
            "entry_price": str(selected_entry_price or Decimal("0")),
            "lotsize": int(selected_lotsize or 1),
            "qty": int(selected_qty or 1),
            "base_qty": int(selected_lotsize or 1),
            "options": options,
        }

    @staticmethod
    def _build_trade_form_payload(
        *,
        user: UserSchema,
        symbol: str,
        equity_ref: Optional[str],
        side: str,
        source: str,
        execution_mode: str,
        snapshot_dict: Dict[str, Any],
        eq_entry_price: Decimal,
        defaults: Dict[str, Any],
        signal_id: Optional[str] = None,
        force_instrument: Optional[str] = None,
    ) -> Dict[str, Any]:
        eq_trade_symbol = symbol
        eq_ref = equity_ref or symbol

        fut_symbol = _resolve_future_symbol_from_snapshot(snapshot_dict)
        fut_price = _resolve_future_price_from_snapshot(snapshot_dict)

        eq_choice = _decorate_instrument_choice(
            TradeGenHelper._build_eq_choice(
                trade_symbol=eq_trade_symbol,
                entry_price=eq_entry_price,
                equity_ref=eq_ref,
            ),
            instrument_type="EQ",
            side=side,
        )
        fut_choice = _decorate_instrument_choice(
            TradeGenHelper._build_fut_choice(
                trade_symbol=fut_symbol,
                entry_price=fut_price,
                equity_ref=eq_ref,
            ),
            instrument_type="FUT",
            side=side,
        )
        ce_choice = _decorate_instrument_choice(
            TradeGenHelper._build_opt_choice_from_snapshot(
                snapshot_dict=snapshot_dict,
                instrument_type="CE",
                equity_ref=eq_ref,
            ),
            instrument_type="CE",
            side=side,
        )
        pe_choice = _decorate_instrument_choice(
            TradeGenHelper._build_opt_choice_from_snapshot(
                snapshot_dict=snapshot_dict,
                instrument_type="PE",
                equity_ref=eq_ref,
            ),
            instrument_type="PE",
            side=side,
        )

        instruments: Dict[str, Any] = {}

        # Include instruments the user is enabled for. The UI will enable/disable
        # each choice based on applicable_sides for the selected direction.
        # This lets watchlist SELL still open the modal with EQ/CE disabled and
        # FUT/PE available instead of failing before the user sees the modal.
        if int(getattr(user, "equity", 0) or 0) == 1 and eq_choice:
            instruments["EQ"] = eq_choice

        if int(getattr(user, "futures", 0) or 0) == 1 and fut_choice:
            instruments["FUT"] = fut_choice

        if int(getattr(user, "options", 0) or 0) == 1 and ce_choice:
            instruments["CE"] = ce_choice

        if int(getattr(user, "options", 0) or 0) == 1 and pe_choice:
            instruments["PE"] = pe_choice

        available = [k for k in ("EQ", "FUT", "CE", "PE") if k in instruments]
        applicable_available = [
            k for k in available
            if _side(side) in (instruments.get(k, {}).get("applicable_sides") or [])
        ]

        selected_instrument = None
        if force_instrument:
            force_inst = _inst_code(force_instrument)
            if force_inst in instruments:
                selected_instrument = force_inst

        if selected_instrument is None:
            selected_instrument = applicable_available[0] if applicable_available else (available[0] if available else None)

        return {
            "userid": getattr(user, "userid", ""),
            "signal_id": signal_id,
            "source": source,
            "entry_origin": (
                "SIGNAL" if str(source or "").strip().upper() in ("AUTOGEN", "TRADE_GENERATOR", "MANUAL_SIGNAL")
                else "POSITION_ADD" if str(source or "").strip().upper() == "POSITION_ADD"
                else "WATCHLIST"
            ),
            "management_mode": (
                "SIGNAL_LIFECYCLE" if str(source or "").strip().upper() in ("AUTOGEN", "TRADE_GENERATOR", "MANUAL_SIGNAL")
                else "MANUAL_PRICE"
            ),
            "symbol": eq_trade_symbol,
            "equity_ref": eq_ref,
            "side": side,
            "product": _normalized_product_for_inst(selected_instrument or "EQ"),
            "execution_mode": execution_mode,
            "position_style": _default_position_style(),
            "fields": TradeGenHelper._form_field_meta(),
            "defaults": {
                "intraday_only": True,
            },
            "available_instruments": available,
            "selected_instrument": selected_instrument,
            "instruments": instruments,
            "meta": {
                "min_eq_amt": str(_min_eq_amt()),
                "tick_size": str(DEFAULT_TICK),
                "snapshot_time": snapshot_dict.get("snapshot_time"),
            }
        }

    @staticmethod
    def _build_plan_for_objects(
        *,
        user: UserSchema,
        signal: SignalSchema,
        snapshot: SnapshotSchema,
        source: str,
        message: str,
        instrument_choice: str = "MULTI",
        requested_product: Any = None,
        selected_trade_symbol: Optional[str] = None,
        selected_entry_price: Any = None,
        selected_quantity: Optional[int] = None,
        selected_lotsize: Optional[int] = None,
    ) -> TradePlan:
        side_str = _side(getattr(signal, "side", "BUY"))
        want_call = side_str == "BUY"
        opt_inst = "CE" if want_call else "PE"

        equity_ref = (getattr(signal, "equity_ref", None) or getattr(signal, "symbol", "") or "").strip()
        trade_time = _pick_trade_time(snapshot, signal)
        exec_mode = _choose_execution_mode(user)

        # User-specific target/SL preferences were removed in Phase-1.
        # Trade risk is managed from lifecycle config + trade_management JSON.
        product_type = _normalized_product_for_inst("EQ", requested_product)
        intraday_only = _intraday_for_inst_and_product("EQ", product_type)

        position_style = _default_position_style()
        if position_style not in ("NAKED", "HEDGED"):
            raise ValueError("TRADE_CONFIG.defaults.default_position_style must be NAKED or HEDGED")

        risk_ref_eq = _snapshot_close(snapshot)

        choice = str(instrument_choice).strip().upper()
        if choice not in {"EQ", "FUT", "CE", "PE", "MULTI"}:
            raise ValueError(f"Unsupported instrument_choice: {instrument_choice!r}")

        explicit_symbol = str(selected_trade_symbol or "").strip().upper()
        explicit_entry = _safe_num(selected_entry_price, None)
        explicit_qty = int(selected_quantity or 0) if selected_quantity is not None else 0
        explicit_lotsize = int(selected_lotsize or 0) if selected_lotsize is not None else 0

        legs: List[TradeLegPlan] = []
        plan_leg_keys: set[tuple[str, str]] = set()
        plan_option_leg_added = False

        def _add_leg(inst: str, sym: Optional[str]) -> None:
            nonlocal plan_option_leg_added
            inst = _inst_code(inst)

            order_side = _instrument_entry_side(inst, side_str)
            if _trade_side_blocked(inst, order_side, product_type):
                return

            sym0 = str(sym).strip().upper() if sym else ""
            if choice in ("EQ", "FUT", "CE", "PE") and inst == choice and explicit_symbol:
                sym0 = explicit_symbol
            if not sym0:
                return

            # One plan should never contain duplicate broker-symbol legs, and a
            # single signal should have at most one option leg in the current
            # AutoTrades model (BUY -> CE, SELL -> PE). This is deliberately
            # inside trade generation, not trade management/OMS.
            plan_key = (inst, sym0.upper())
            if plan_key in plan_leg_keys:
                return
            if inst in ("CE", "PE") and plan_option_leg_added:
                logger.warning(
                    "TradeGenHelper: duplicate option leg suppressed in plan | user=%s signal=%s side=%s inst=%s symbol=%s",
                    getattr(user, "userid", "?"),
                    getattr(signal, "signal_id", None),
                    side_str,
                    inst,
                    sym0,
                )
                return
            plan_leg_keys.add(plan_key)
            if inst in ("CE", "PE"):
                plan_option_leg_added = True

            use_explicit = choice in ("EQ", "FUT", "CE", "PE") and inst == choice and bool(explicit_symbol)
            lotsize = 1 if inst == "EQ" else TradeGenHelper._resolve_lotsize(sym0)
            if use_explicit and explicit_lotsize > 0:
                lotsize = explicit_lotsize

            entry_exec = explicit_entry if use_explicit and explicit_entry is not None and explicit_entry > 0 else None
            if entry_exec is None and inst in ("FUT", "CE", "PE"):
                entry_exec = _resolve_deriv_entry_price(signal, instrument_type=inst, symbol=sym0)
            if entry_exec is None or d(entry_exec) <= 0:
                if inst == "EQ":
                    entry_exec = risk_ref_eq
                else:
                    logger.warning(
                        "TradeGenHelper: derivative leg skipped because price is unavailable | "
                        "user=%s signal=%s inst=%s symbol=%s",
                        getattr(user, "userid", "?"),
                        getattr(signal, "signal_id", None),
                        inst,
                        sym0,
                    )
                    return

            legs.append(
                TradeLegPlan(
                    instrument_type=inst,
                    trade_symbol=sym0,
                    lotsize=lotsize,
                    entry_price_exec=d(entry_exec),
                    risk_ref_price_eq=d(risk_ref_eq),
                    quantity_override=explicit_qty if use_explicit and explicit_qty > 0 else None,
                )
            )

        if choice == "EQ":
            _add_leg("EQ", getattr(signal, "symbol", None))
        elif choice == "FUT":
            _add_leg("FUT", _resolve_future_symbol(signal))
        elif choice == "CE":
            _add_leg("CE", _resolve_atm_option_symbol(signal, is_call=True))
        elif choice == "PE":
            _add_leg("PE", _resolve_atm_option_symbol(signal, is_call=False))
        else:  # MULTI
            if int(getattr(user, "equity", 0) or 0) == 1:
                _add_leg("EQ", getattr(signal, "symbol", None))
            if int(getattr(user, "futures", 0) or 0) == 1:
                _add_leg("FUT", _resolve_future_symbol(signal))
            if int(getattr(user, "options", 0) or 0) == 1:
                if side_str == "BUY":
                    _add_leg("CE", _resolve_atm_option_symbol(signal, is_call=True))
                else:
                    _add_leg("PE", _resolve_atm_option_symbol(signal, is_call=False))

        return TradePlan(
            user=user,
            signal=signal,
            snapshot=snapshot,
            side=side_str,
            equity_ref=equity_ref or "",
            trade_time=trade_time,
            execution_mode=exec_mode,
            product_type=product_type,
            intraday_only=intraday_only,
            position_style=position_style,
            legs=legs,
            source=source,
            message=message,
        )

    @staticmethod
    def _persist_plan(plan: TradePlan) -> List[UserTradeSchema]:
        created: List[UserTradeSchema] = []
        for leg in plan.legs:
            signal_id = getattr(plan.signal, "signal_id", None)
            try:
                uid = str(getattr(plan.user, "userid", "") or "").strip()
                signal_ref = str(signal_id or "").strip()
                inst = _inst_code(leg.instrument_type)

                # First guard: a single signal may create at most one row per
                # user + instrument family for the day/lifetime of that signal.
                # This blocks duplicate option-leg churn even if the first
                # option leg was quickly exited before the next generator pass.
                if _trade_instrument_key_exists(
                    userid=uid,
                    signal_id=signal_ref,
                    instrument_type=inst,
                ):
                    logger.info(
                        "TradeGenHelper: duplicate historical signal/instrument skipped user=%s signal=%s inst=%s symbol=%s",
                        getattr(plan.user, "userid", "?"),
                        str(signal_id),
                        inst,
                        leg.trade_symbol,
                    )
                    continue

                # Second guard: do not add another currently-open row for the same signal and
                # same instrument family. This specifically prevents repeated
                # CE/PE strike migration rows such as 390PE, 392.5PE, 395PE
                # under one signal_id when the signal remains deployable.
                existing_inst = _existing_open_trades_for_signal_instrument(
                    userid=uid,
                    signal_id=signal_ref,
                    instrument_type=inst,
                )
                if existing_inst:
                    logger.info(
                        "TradeGenHelper: duplicate open signal/instrument skipped user=%s signal=%s inst=%s symbol=%s existing=%s",
                        getattr(plan.user, "userid", "?"),
                        str(signal_id),
                        inst,
                        leg.trade_symbol,
                        _existing_trade_summary(existing_inst),
                    )
                    continue

                # Third guard: exact broker symbol must not be duplicated while
                # still open, even if it came from another signal.
                existing = _existing_open_trades_for_symbol(
                    userid=uid,
                    symbol=leg.trade_symbol,
                )
                if existing:
                    logger.info(
                        "TradeGenHelper: duplicate open symbol skipped user=%s signal=%s inst=%s symbol=%s existing=%s",
                        getattr(plan.user, "userid", "?"),
                        str(signal_id),
                        inst,
                        leg.trade_symbol,
                        _existing_trade_summary(existing),
                    )
                    continue

                payload = UserTradeDataAssembler.assemble_from_plan(plan=plan, leg=leg)

                ut = UserTradePersister.persist(payload)
                if ut:
                    created.append(ut)
            except Exception:
                logger.exception(
                    "TradeGenHelper: assemble/persist failed user=%s signal=%s inst=%s symbol=%s",
                    getattr(plan.user, "userid", "?"),
                    str(signal_id),
                    leg.instrument_type,
                    leg.trade_symbol,
                )
        return created

    @staticmethod
    def build_watchlist_trade_form(
        *,
        userid: str,
        symbol: str,
        side: str = "BUY",
        source: str = "WATCHLIST",
    ) -> Dict[str, Any]:
        user = UserSchema.fetch_user(userid)
        if not user:
            return {"ok": False, "error": "USER_NOT_FOUND"}

        symbol = str(symbol or "").strip().upper()
        if not symbol:
            return {"ok": False, "error": "MISSING_SYMBOL"}

        snapshot = SnapshotSchema.fetch_latest_for_symbol(symbol)
        if not snapshot:
            return {"ok": False, "error": "NO_SNAPSHOT", "details": {"symbol": symbol}}

        snap_dict = _safe_snapshot_dict(snapshot)
        eq_price = _snapshot_close(snapshot)
        execution_mode = _choose_execution_mode(user)

        defaults = {"management_source": "TRADE_CONFIG", "intraday_only": True}

        form = TradeGenHelper._build_trade_form_payload(
            user=user,
            symbol=symbol,
            equity_ref=symbol,
            side=_side(side),
            source=source,
            execution_mode=execution_mode,
            snapshot_dict=snap_dict,
            eq_entry_price=eq_price,
            defaults=defaults,
            signal_id=None,
        )

        return {"ok": True, "trade_form": form}

    @staticmethod
    def build_position_trade_form(
        *,
        userid: str,
        symbol: str,
        equity_ref: str,
        instrument_type: str,
        side: str,
        source: str = "POSITION_ADD",
    ) -> Dict[str, Any]:
        user = UserSchema.fetch_user(userid)
        if not user:
            return {"ok": False, "error": "USER_NOT_FOUND"}

        trade_symbol = str(symbol or "").strip().upper()
        eq_ref = str(equity_ref or "").strip().upper()
        inst = _inst_code(instrument_type)
        trade_side = _side(side)

        if not trade_symbol:
            return {"ok": False, "error": "MISSING_SYMBOL"}
        if not eq_ref:
            return {"ok": False, "error": "MISSING_EQUITY_REF"}
        if inst not in ("EQ", "FUT", "CE", "PE"):
            return {"ok": False, "error": "INVALID_INSTRUMENT_TYPE", "details": {"instrument_type": inst}}

        blocked_reason = _trade_side_blocked(inst, trade_side)
        if blocked_reason:
            return {
                "ok": False,
                "error": blocked_reason,
                "details": {"instrument_type": inst, "side": trade_side},
            }

        snapshot = SnapshotSchema.fetch_latest_for_symbol(eq_ref)
        if not snapshot:
            return {"ok": False, "error": "NO_SNAPSHOT", "details": {"symbol": eq_ref}}

        snap_dict = _safe_snapshot_dict(snapshot)
        eq_price = _snapshot_close(snapshot)
        execution_mode = _choose_execution_mode(user)

        defaults = {"management_source": "TRADE_CONFIG", "intraday_only": True}

        form = TradeGenHelper._build_trade_form_payload(
            user=user,
            symbol=trade_symbol,
            equity_ref=eq_ref,
            side=trade_side,
            source=source,
            execution_mode=execution_mode,
            snapshot_dict=snap_dict,
            eq_entry_price=eq_price,
            defaults=defaults,
            signal_id=None,
            force_instrument=inst,
        )

        fields = form.get("fields", {})
        fields["side_editable"] = False
        fields["instrument_editable"] = False
        fields["option_symbol_editable"] = False if inst in ("CE", "PE") else fields.get("option_symbol_editable", True)
        form["fields"] = fields

        form["position_context"] = {
            "symbol": trade_symbol,
            "equity_ref": eq_ref,
            "instrument_type": inst,
            "side": trade_side,
        }

        return {"ok": True, "trade_form": form}

    @staticmethod
    def build_signal_trade_form(
        *,
        userid: str,
        signal_id: str,
        source: str = "AUTOGEN",
    ) -> Dict[str, Any]:
        """
        UI/modal form builder.

        Hard safety blocks stop the form. Soft price/delay conditions are shown
        as explicit manual warnings and can be confirmed by the operator.
        """
        user = UserSchema.fetch_user(userid)
        if not user:
            return {"ok": False, "error": "USER_NOT_FOUND"}

        signal = TradeGenHelper._resolve_signal(signal_id)
        if not signal:
            return {"ok": False, "error": "SIGNAL_NOT_FOUND", "details": {"signal_id": str(signal_id)}}

        # Shared validation path for manual signal-trade preview.
        # Hard blocks stop the form. Soft lifecycle/deployability WAIT decisions
        # are surfaced as warnings so the UI can ask for an explicit override.
        decision = TradeDecisionHelper.evaluate(
            user=user,
            signal=signal,
            mode=MODE_MANUAL_PREVIEW,
        )
        decision_dict = decision.to_dict()

        if decision.decision == "BLOCK":
            return {
                "ok": False,
                "error": "TRADE_VALIDATION_BLOCKED",
                "details": decision_dict,
            }

        snapshot = SnapshotSchema.fetch_latest_for_symbol(getattr(signal, "symbol", None))
        if not snapshot:
            return {"ok": False, "error": "NO_SNAPSHOT", "details": {"symbol": getattr(signal, "symbol", None)}}

        side = _side(getattr(signal, "side", "BUY"))
        eq_symbol = str(getattr(signal, "symbol", "") or "").strip().upper()
        eq_ref = str(getattr(signal, "equity_ref", None) or eq_symbol).strip().upper()
        eq_price = _snapshot_close(snapshot)
        execution_mode = _choose_execution_mode(user)
        snap_dict = _get_signal_last_snapshot(signal) or _safe_snapshot_dict(snapshot)

        defaults = {
            "management_source": "TRADE_CONFIG",
            "intraday_only": True,
        }

        form = TradeGenHelper._build_trade_form_payload(
            user=user,
            symbol=eq_symbol,
            equity_ref=eq_ref,
            side=side,
            source=source,
            execution_mode=execution_mode,
            snapshot_dict=snap_dict,
            eq_entry_price=eq_price,
            defaults=defaults,
            signal_id=str(signal_id),
        )

        allowed_for_signal = ["EQ", "FUT", "CE"] if side == "BUY" else ["EQ", "FUT", "PE"]

        instruments = form.get("instruments", {}) or {}
        instruments = {k: v for k, v in instruments.items() if k in allowed_for_signal}

        available = [k for k in ("EQ", "FUT", "CE", "PE") if k in instruments]
        selected = str(form.get("selected_instrument") or "").strip().upper()

        if selected not in available:
            if side == "BUY":
                for pref in ("EQ", "FUT", "CE"):
                    if pref in available:
                        selected = pref
                        break
            else:
                for pref in ("EQ", "FUT", "PE"):
                    if pref in available:
                        selected = pref
                        break

        form["instruments"] = instruments
        form["available_instruments"] = available
        form["selected_instrument"] = selected if selected in available else (available[0] if available else None)

        form["fields"]["side_editable"] = False
        form["fields"]["product_editable"] = True
        form["product"] = _normalized_product_for_inst(form.get("selected_instrument") or "EQ")
        form["entry_eligibility"] = decision_dict
        decision_details = decision_dict.get("details") if isinstance(decision_dict.get("details"), dict) else {}
        form["entry_context"] = {
            "origin": "SIGNAL",
            "setup": decision_details.get("setup_family") or getattr(signal, "setup", None),
            "signal_stage": decision_details.get("signal_stage") or getattr(signal, "stage", None),
            "signal_status": decision_details.get("signal_status") or getattr(signal, "status", None),
            "management_posture": decision_details.get("management_posture"),
            "directional_alignment": decision_details.get("directional_alignment"),
            "auction_state": decision_details.get("auction_state"),
            "auction_action": decision_details.get("auction_action"),
            "current_trade_instruction": decision_details.get("lifecycle_trade_action"),
            "lifecycle_reason": decision_details.get("lifecycle_reason"),
            "should_exit_signal": decision_details.get("should_exit_signal"),
        }
        if decision.decision == "WAIT":
            form["requires_confirmation"] = True
            form["entry_warning"] = ", ".join(decision.reasons or [])
            form["validation_warning"] = form["entry_warning"]

        form["defaults"]["intraday_only"] = True

        return {"ok": True, "trade_form": form}

    @staticmethod
    def _resolve_signal_form_selection(
        *,
        user: UserSchema,
        signal: SignalSchema,
        snapshot: SnapshotSchema,
        instrument_choice: str,
        selected_trade_symbol: Optional[str],
        selected_quantity: Optional[int],
        selected_lots: Optional[int],
    ) -> Dict[str, Any]:
        """Validate a signal-modal selection against the backend form data.

        Browser values are treated as a selection request, not as authoritative
        market data.  Symbol, price and lot-size are resolved from the current
        backend form payload; only the requested positive quantity is carried into
        the trade plan after lot-size validation.
        """
        choice = _inst_code(instrument_choice)
        symbol = str(selected_trade_symbol or "").strip().upper()
        if not symbol:
            return {"ok": True, "selection": {}}
        if choice not in ("EQ", "FUT", "CE", "PE"):
            return {
                "ok": False,
                "error": "SELECTED_LEG_REQUIRES_SINGLE_INSTRUMENT",
                "details": {"instrument_choice": instrument_choice},
            }
        if not _user_instrument_enabled(user, choice):
            return {
                "ok": False,
                "error": "USER_INSTRUMENT_NOT_ENABLED",
                "details": {"userid": user.userid, "instrument_type": choice},
            }

        signal_side = _side(getattr(signal, "side", "BUY"))
        allowed = {"EQ", "FUT", "CE"} if signal_side == "BUY" else {"EQ", "FUT", "PE"}
        if choice not in allowed:
            return {
                "ok": False,
                "error": "INSTRUMENT_NOT_APPLICABLE_TO_SIGNAL_SIDE",
                "details": {"instrument_type": choice, "signal_side": signal_side},
            }

        snap_dict = _get_signal_last_snapshot(signal) or _safe_snapshot_dict(snapshot)
        eq_price = _snapshot_close(snapshot)
        form = TradeGenHelper._build_trade_form_payload(
            user=user,
            symbol=str(getattr(signal, "symbol", "") or "").strip().upper(),
            equity_ref=str(getattr(signal, "equity_ref", None) or getattr(signal, "symbol", "") or "").strip().upper(),
            side=signal_side,
            source="MANUAL_SIGNAL",
            execution_mode=_choose_execution_mode(user),
            snapshot_dict=snap_dict,
            eq_entry_price=eq_price,
            defaults={"management_source": "TRADE_CONFIG", "intraday_only": True},
            signal_id=str(getattr(signal, "signal_id", "") or ""),
        )
        block = (form.get("instruments") or {}).get(choice) or {}
        options = [x for x in (block.get("options") or []) if isinstance(x, dict)]
        selected = next(
            (x for x in options if str(x.get("symbol") or "").strip().upper() == symbol),
            None,
        )
        if selected is None:
            return {
                "ok": False,
                "error": "SELECTED_TRADE_SYMBOL_NOT_AVAILABLE",
                "details": {
                    "instrument_type": choice,
                    "trade_symbol": symbol,
                    "available_symbols": [str(x.get("symbol") or "") for x in options],
                },
            }

        entry_price = _safe_num(selected.get("entry_price"), None)
        lotsize = max(int(selected.get("lotsize") or 1), 1)
        base_qty = max(int(selected.get("base_qty") or selected.get("qty") or lotsize), 1)
        quantity = int(selected_quantity or 0) if selected_quantity is not None else 0
        lots = max(int(selected_lots or 0), 0) if selected_lots is not None else 0
        if quantity <= 0 and lots > 0:
            quantity = base_qty * lots
        if quantity <= 0:
            quantity = max(int(selected.get("qty") or base_qty), 1)

        if choice in ("FUT", "CE", "PE") and quantity % lotsize != 0:
            return {
                "ok": False,
                "error": "QUANTITY_NOT_MULTIPLE_OF_LOT_SIZE",
                "details": {
                    "instrument_type": choice,
                    "quantity": quantity,
                    "lotsize": lotsize,
                },
            }

        if entry_price is None or entry_price <= 0:
            return {
                "ok": False,
                "error": "SELECTED_TRADE_SYMBOL_PRICE_UNAVAILABLE",
                "details": {"instrument_type": choice, "trade_symbol": symbol},
            }

        return {
            "ok": True,
            "selection": {
                "trade_symbol": symbol,
                "entry_price": entry_price,
                "quantity": quantity,
                "lotsize": lotsize,
            },
        }


    @staticmethod
    def build_signal_plan(
        *,
        userid: str,
        signal_id: str,
        instrument_choice: str = "MULTI",
        source: str = "AUTOGEN",
        requested_product: Any = None,
        selected_trade_symbol: Optional[str] = None,
        selected_quantity: Optional[int] = None,
        selected_lots: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Build a trade plan for a specific user + signal.

        Caller owns flow policy:
        - routes own manual/UI authorization and signal eligibility rules
        - trade_generator owns AUTOGEN user/signal eligibility rules

        Helper owns:
        - object lookup
        - integrity checks
        - planning / pricing / leg resolution
        """
        user = UserSchema.fetch_user(userid)
        if not user:
            return {"ok": False, "error": "USER_NOT_FOUND"}

        signal = TradeGenHelper._resolve_signal(signal_id)
        if not signal:
            return {"ok": False, "error": "SIGNAL_NOT_FOUND", "details": {"signal_id": str(signal_id)}}

        if _is_terminal(signal):
            return {
                "ok": False,
                "error": "SIGNAL_TERMINAL_STATUS",
                "details": {"signal_id": str(signal_id), "status": _signal_status(signal)},
            }

        snapshot = _fetch_snapshot_for_signal(signal)
        if not snapshot:
            return {"ok": False, "error": "NO_SNAPSHOT", "details": {"symbol": getattr(signal, "symbol", None)}}

        signal_setup = str(getattr(signal, "setup", "") or "").strip().upper()
        if not signal_setup:
            return {"ok": False, "error": "MISSING_SIGNAL_SETUP", "details": {"signal_id": getattr(signal, "signal_id", None)}}

        selection_result = TradeGenHelper._resolve_signal_form_selection(
            user=user,
            signal=signal,
            snapshot=snapshot,
            instrument_choice=instrument_choice,
            selected_trade_symbol=selected_trade_symbol,
            selected_quantity=selected_quantity,
            selected_lots=selected_lots,
        )
        if not selection_result.get("ok"):
            return selection_result
        selection = selection_result.get("selection") or {}

        plan = TradeGenHelper._build_plan_for_objects(
            user=user,
            signal=signal,
            snapshot=snapshot,
            source=source,
            # Trades do not persist a dedicated setup column. Keep message as the
            # human setup label. Missing setup should fail upstream, not fall back
            # to the backend lifecycle grouping.
            message=signal_setup,
            instrument_choice=instrument_choice,
            requested_product=requested_product,
            selected_trade_symbol=selection.get("trade_symbol"),
            selected_entry_price=selection.get("entry_price"),
            selected_quantity=selection.get("quantity"),
            selected_lotsize=selection.get("lotsize"),
        )

        if not plan.legs:
            return {
                "ok": False,
                "error": "NO_LEGS_RESOLVED",
                "details": {
                    "signal_id": str(signal_id),
                    "instrument_choice": instrument_choice,
                    "side": plan.side,
                },
            }

        return {
            "ok": True,
            "plan": {
                "userid": userid,
                "signal_id": str(signal_id),
                "instrument_choice": instrument_choice,
                "source": source,
                "side": plan.side,
                "equity_ref": plan.equity_ref,
                "execution_mode": plan.execution_mode,
                "product_type": plan.product_type,
                "intraday_only": plan.intraday_only,
                "position_style": plan.position_style,
                "legs": [
                    {
                        "instrument_type": leg.instrument_type,
                        "trade_symbol": leg.trade_symbol,
                        "lotsize": leg.lotsize,
                        "quantity": leg.quantity_override,
                        "entry_price_exec": str(leg.entry_price_exec),
                        "risk_ref_price_eq": str(leg.risk_ref_price_eq),
                    }
                    for leg in plan.legs
                ],
            },
            "_plan_obj": plan,
        }

    @staticmethod
    def create_manual_trade(
        *,
        userid: str,
        payload: Dict[str, Any],
        source: str = "MANUAL",
    ) -> Dict[str, Any]:
        user = UserSchema.fetch_user(userid)
        if not user:
            return {"ok": False, "error": "USER_NOT_FOUND"}

        p = payload or {}

        raw_symbol = str(p.get("symbol") or "").strip().upper()
        raw_equity_ref = str(p.get("equity_ref") or raw_symbol).strip().upper()

        if not raw_symbol:
            return {"ok": False, "error": "MISSING_SYMBOL"}
        if not raw_equity_ref:
            return {"ok": False, "error": "MISSING_EQUITY_REF"}

        instrument_type = _inst_code(p.get("instrument_type") or "EQ")
        if instrument_type not in ("EQ", "FUT", "CE", "PE"):
            return {
                "ok": False,
                "error": "INVALID_INSTRUMENT_TYPE",
                "details": {"instrument_type": instrument_type},
            }

        trade_type = _side(p.get("trade_type") or p.get("side") or "BUY")

        if not _user_instrument_enabled(user, instrument_type):
            return {
                "ok": False,
                "error": "USER_INSTRUMENT_NOT_ENABLED",
                "details": {"userid": user.userid, "instrument_type": instrument_type},
            }

        trade_symbol_default = raw_symbol
        if instrument_type == "EQ":
            trade_symbol_default = raw_symbol

        trade_symbol = str(p.get("trade_symbol") or trade_symbol_default).strip().upper()
        if not trade_symbol:
            return {"ok": False, "error": "MISSING_TRADE_SYMBOL"}

        blocked_reason = _trade_side_blocked(instrument_type, trade_type, p.get("product"))
        if blocked_reason:
            return {
                "ok": False,
                "error": blocked_reason,
                "details": {
                    "instrument_type": instrument_type,
                    "trade_type": trade_type,
                    "symbol": trade_symbol,
                    "product": str(p.get("product") or "").strip().upper(),
                },
            }

        equity_ref = raw_equity_ref

        requested_exec_mode = _enum_str(p.get("execution_mode") or getattr(user, "execution_mode", "VIRTUAL"))
        effective_exec_mode = "REAL" if requested_exec_mode == "REAL" and _has_broker_login(user) else "VIRTUAL"

        snapshot = SnapshotSchema.fetch_latest_for_symbol(equity_ref)
        if not snapshot:
            return {"ok": False, "error": "NO_SNAPSHOT", "details": {"symbol": equity_ref}}

        snap_dict = _safe_snapshot_dict(snapshot)
        trade_time = getattr(snapshot, "snapshot_time", None)
        trade_time = _to_ist_naive(trade_time) or business_now_naive()

        # Watchlist/position manual trade risk is anchored to the completed
        # snapshot close. Browser-submitted prices are display hints only and are
        # never authoritative for persistence.
        try:
            risk_ref_price = _snapshot_close(snapshot)
        except Exception as exc:
            return {
                "ok": False,
                "error": "INVALID_RISK_REF_PRICE",
                "details": {"symbol": equity_ref, "reason": str(exc)},
            }
        if risk_ref_price <= 0:
            return {"ok": False, "error": "INVALID_RISK_REF_PRICE"}

        entry_price: Optional[Decimal] = None
        if instrument_type == "EQ":
            entry_price = risk_ref_price
        elif instrument_type == "FUT":
            entry_price = _resolve_future_price_from_snapshot(snap_dict)
        elif instrument_type in ("CE", "PE"):
            rows = _option_rows_from_snapshot(snap_dict, is_call=(instrument_type == "CE"))
            sym0 = trade_symbol.strip().upper()
            for row in rows:
                if str(row.get("symbol") or "").strip().upper() != sym0:
                    continue
                entry_price = _safe_num(row.get("ltp"), _safe_num(row.get("last_price"), None))
                if entry_price is not None and entry_price > 0:
                    break

        if entry_price is None or entry_price <= 0:
            return {
                "ok": False,
                "error": "DERIVATIVE_PRICE_UNAVAILABLE" if instrument_type != "EQ" else "INVALID_ENTRY_PRICE",
                "details": {"instrument_type": instrument_type, "trade_symbol": trade_symbol},
            }

        lots = _safe_num(p.get("lots"), None)
        if lots is None or int(lots) <= 0:
            lots = Decimal("1")
        lots_i = max(int(lots), 1)

        lotsize = _safe_num(p.get("lotsize"), None)
        if lotsize is None or int(lotsize) <= 0:
            lotsize = Decimal(str(TradeGenHelper._resolve_lotsize(trade_symbol) if instrument_type != "EQ" else 1))
        lotsize_i = max(int(lotsize), 1)

        quantity = _safe_num(p.get("quantity"), None)
        if quantity is None or int(quantity) <= 0:
            quantity = _safe_num(p.get("qty"), None)

        if quantity is None or int(quantity) <= 0:
            if instrument_type == "EQ":
                base_qty = TradeGenHelper._default_eq_base_qty(entry_price)
                quantity_i = max(1, base_qty * lots_i)
            else:
                quantity_i = max(1, lotsize_i * lots_i)
        else:
            quantity_i = max(int(quantity), 1)

        if instrument_type in ("FUT", "CE", "PE") and quantity_i % lotsize_i != 0:
            return {
                "ok": False,
                "error": "QUANTITY_NOT_MULTIPLE_OF_LOT_SIZE",
                "details": {
                    "instrument_type": instrument_type,
                    "quantity": quantity_i,
                    "lotsize": lotsize_i,
                },
            }

        manual_message = str(p.get("message") or "MANUAL").strip()
        product = _normalized_product_for_inst(instrument_type, p.get("product"))
        position_style = _default_position_style()
        intraday_only = _intraday_for_inst_and_product(instrument_type, product)

        # Snapshot timestamps repeat for every manual click made within the same
        # market snapshot.  Use a UUID for identity and keep entry_time/snapshot
        # separately as the source context.
        manual_signal_id = _new_manual_signal_id()

        create_payload = {
            "userid": user.userid,
            "signal_id": manual_signal_id,
            "symbol": trade_symbol,
            "equity_ref": equity_ref,
            "instrument_type": instrument_type,
            "trade_type": trade_type,

            "position_style": position_style,
            "hedged_symbol": None,

            "source": source,
            "message": manual_message,

            "entry_snapshot": snap_dict,
            "last_snapshot": snap_dict,

            "entry_status": EntryStatus.CREATED.value,
            "exit_status": ExitStatus.NONE.value,
            "execution_mode": effective_exec_mode,
            "intraday_only": bool(intraday_only),

            "entry_time": trade_time,
            "entry_intent_time": None,
            "entry_exec_time": None,
            "entry_reconciled_at": None,
            "entry_price": d(entry_price),
            "executed_entry_price": None,
            "executed_entry_qty": None,
            "quantity": int(quantity_i),

            "entry_order_id": None,
            "entry_order_response_json": None,
            "entry_retries": 5,

            # Clean adaptive management is the only active target/SL source.
            "trade_management": _build_trade_management_payload(
                side=trade_type,
                basis_price=d(entry_price),
                atr=_extract_atr_from_snapshot_dict(snap_dict),
                instrument_type=instrument_type,
            ),

            "exit_reason": None,
            "exit_rule": None,
            "exit_time": None,
            "exit_intent_time": None,
            "exit_exec_time": None,
            "exit_reconciled_at": None,
            "exit_price": None,
            "executed_exit_price": None,
            "executed_exit_qty": 0,
            "exit_pnl": None,

            "exit_order_id": None,
            "exit_order_response_json": None,
            "exit_retries": 5,

            "last_time": trade_time,
            "last_price": d(entry_price),
            "last_pnl": Decimal("0"),
            "last_pnl_value": Decimal("0"),
            "max_price": d(entry_price),
            "min_price": d(entry_price),
            "max_time": trade_time,
            "min_time": trade_time,

            "exec_last_checked_at": None,
            "exec_status": None,
            "exec_status_message": None,

            "reconcile_last_checked_at": None,
            "reconcile_status": None,
            "reconcile_status_message": None,

            "risk_ref_price": d(risk_ref_price),

            "product_type": product,
            "lotsize": lotsize_i,
        }

        account_governor = _assess_and_audit_account_governor(
            request=_account_governor_request_from_manual_payload(
                payload=create_payload,
            )
        )

        try:
            created = UserTradePersister.persist(create_payload)
        except Exception:
            logger.exception(
                "TradeGenHelper.create_manual_trade failed userid=%s equity_ref=%s inst=%s trade_symbol=%s",
                userid, equity_ref, instrument_type, trade_symbol
            )
            return {"ok": False, "error": "CREATE_MANUAL_TRADE_FAILED"}

        if not created:
            return {"ok": False, "error": "NO_TRADE_CREATED"}

        return {
            "ok": True,
            "created_count": 1,
            "created": [f"{instrument_type}:{trade_symbol}"],
            "trade_ids": [getattr(created, "id", None)],
            "account_governor": account_governor,
        }

    @staticmethod
    def create_trades_from_signal(
        *,
        userid: str,
        signal_id: str,
        instrument_choice: str = "MULTI",
        source: str = "AUTOGEN",
        confirm_entry_warning: bool = False,
        requested_product: Any = None,
        selected_trade_symbol: Optional[str] = None,
        selected_quantity: Optional[int] = None,
        selected_lots: Optional[int] = None,
    ) -> Dict[str, Any]:
        # One deployment per user/signal is the authoritative policy.  The DB
        # unique constraint remains the final race-safe per-instrument guard.

        # Use the same validation path for AUTO and manual signal-based trade
        # creation. AUTO must be strictly allowed. Manual creation may proceed
        # through a defensive-but-nonterminal posture only after an explicit
        # ``confirm_entry_warning``. Hard lifecycle exit postures cannot be overridden.
        user = UserSchema.fetch_user(userid)
        signal = TradeGenHelper._resolve_signal(signal_id)

        if not user:
            return {"ok": False, "error": "USER_NOT_FOUND"}

        if not signal:
            return {"ok": False, "error": "SIGNAL_NOT_FOUND", "details": {"signal_id": str(signal_id)}}

        if UserTradeSchema.has_any_trade_for_signal(userid=userid, signal_id=str(signal_id)):
            return {
                "ok": False,
                "error": "SIGNAL_ALREADY_DEPLOYED",
                "details": {"userid": userid, "signal_id": str(signal_id)},
            }

        src = str(source or "").strip().upper()
        if src in ("AUTOGEN", "TRADE_GENERATOR"):
            validation_mode = MODE_AUTO
        elif confirm_entry_warning:
            validation_mode = MODE_MANUAL_CONFIRM
        else:
            validation_mode = MODE_MANUAL_PREVIEW

        decision = TradeDecisionHelper.evaluate(
            user=user,
            signal=signal,
            mode=validation_mode,
        )

        if not decision.allowed:
            err = "MANUAL_CONFIRMATION_REQUIRED" if decision.decision == "WAIT" else "TRADE_VALIDATION_BLOCKED"
            return {
                "ok": False,
                "error": err,
                "details": decision.to_dict(),
            }

        built = TradeGenHelper.build_signal_plan(
            userid=userid,
            signal_id=signal_id,
            instrument_choice=instrument_choice,
            source=source,
            requested_product=requested_product,
            selected_trade_symbol=selected_trade_symbol,
            selected_quantity=selected_quantity,
            selected_lots=selected_lots,
        )
        if not built.get("ok"):
            built.setdefault("validation", decision.to_dict())
            return built

        plan = built.get("_plan_obj")
        if not isinstance(plan, TradePlan):
            return {"ok": False, "error": "PLAN_BUILD_FAILED"}

        account_governor = _assess_and_audit_account_governor(
            request=_account_governor_request_from_plan(plan)
        )
        created_trades = TradeGenHelper._persist_plan(plan)
        if not created_trades:
            return {
                "ok": False,
                "error": "NO_TRADES_CREATED",
                "details": {
                    "signal_id": str(signal_id),
                    "instrument_choice": instrument_choice,
                    "legs": [
                        {"inst": x.instrument_type, "symbol": x.trade_symbol}
                        for x in plan.legs
                    ],
                },
            }

        return {
            "ok": True,
            "created_count": len(created_trades),
            "created": [
                f"{getattr(t, 'instrument_type', '?')}:{getattr(t, 'symbol', '?')}"
                for t in created_trades
            ],
            "trade_ids": [getattr(t, "id", None) for t in created_trades],
            "validation": decision.to_dict(),
            "account_governor": account_governor,
        }