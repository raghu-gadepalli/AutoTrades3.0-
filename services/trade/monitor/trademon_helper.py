#!/usr/bin/env python3
"""
services/trade/monitor/trademon_helper.py

Clean Phase-1 adaptive trade-management helper.

Inputs are an existing trade, current instrument LTP, underlying/equity ATR,
and current signal metadata. Output is a compact decision/state update:
EXPAND, HOLD, PROTECT, or EXIT.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
from typing import Any, Dict, Optional

from configs.monitor_config import MONITOR_CONFIG
from enums.enums import TradePosture
from services.trade.monitor.signal_contract import AuctionTradeSignalContext


def d(x: Any) -> Decimal:
    if isinstance(x, Decimal):
        return x
    if x is None:
        raise ValueError("decimal value is required")
    try:
        return Decimal(str(x))
    except Exception as exc:
        raise ValueError(f"invalid decimal value: {x!r}") from exc


def _required(mapping: Dict[str, Any], key: str, path: str = "trade_management") -> Any:
    if key not in mapping:
        raise ValueError(f"{path}.{key} is required")
    return mapping[key]


def _optional(mapping: Dict[str, Any], key: str, default: Any = None) -> Any:
    return mapping[key] if key in mapping else default


def _cfg(name: str) -> Any:
    cfg = getattr(MONITOR_CONFIG, "trade_management", None)
    if cfg is None or not hasattr(cfg, name):
        raise AttributeError(f"MONITOR_CONFIG.trade_management.{name} is required")
    return getattr(cfg, name)


def _cfg_dec(name: str) -> Decimal:
    return d(_cfg(name))


def _json_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    return float(d(value))


def _side(v: Any) -> str:
    s = str(getattr(v, "value", v) or "").upper().strip()
    if s not in {"BUY", "SELL"}:
        raise ValueError(f"trade side must be BUY or SELL, got {v!r}")
    return s


def _inst(v: Any) -> str:
    s = str(getattr(v, "value", v) or "").upper().strip()
    aliases = {
        "EQUITY": "EQ",
        "CASH": "EQ",
        "FUTURE": "FUT",
        "FUTURES": "FUT",
        "CALL": "CE",
        "PUT": "PE",
    }
    s = aliases[s] if s in aliases else s
    if s not in {"EQ", "FUT", "CE", "PE"}:
        raise ValueError(f"unsupported instrument type: {v!r}")
    return s


def _target_price_from_r(*, side: str, entry_price: Decimal, atr_unit: Decimal, r_multiple: Decimal) -> Optional[Decimal]:
    if entry_price <= 0 or atr_unit <= 0 or r_multiple <= 0:
        return None
    return entry_price + (atr_unit * r_multiple) if side == "BUY" else entry_price - (atr_unit * r_multiple)


def _stop_price_from_r(*, side: str, entry_price: Decimal, atr_unit: Decimal, r_multiple: Decimal) -> Optional[Decimal]:
    if entry_price <= 0 or atr_unit <= 0 or r_multiple < 0:
        return None
    return entry_price - (atr_unit * r_multiple) if side == "BUY" else entry_price + (atr_unit * r_multiple)


def _stop_price_from_profit_r(
    *,
    side: str,
    entry_price: Decimal,
    atr_unit: Decimal,
    stop_profit_r: Decimal,
) -> Optional[Decimal]:
    """Return a stop from signed profit-space R relative to executed entry.

    Positive R locks profit, zero is cost, and negative R leaves controlled
    adverse risk.  This is separate from ``_stop_price_from_r`` whose positive
    multiple always represents an adverse-side disaster stop.
    """
    if entry_price <= 0 or atr_unit <= 0:
        return None
    return (
        entry_price + (atr_unit * stop_profit_r)
        if side == "BUY"
        else entry_price - (atr_unit * stop_profit_r)
    )


def _profit_r_from_price(
    *,
    side: str,
    entry_price: Decimal,
    price: Decimal,
    atr_unit: Decimal,
) -> Optional[Decimal]:
    if entry_price <= 0 or price <= 0 or atr_unit <= 0:
        return None
    return (price - entry_price) / atr_unit if side == "BUY" else (entry_price - price) / atr_unit


def _profit_protection_cfg(name: str) -> Any:
    cfg = _cfg("profit_protection")
    if cfg is None or not hasattr(cfg, name):
        raise AttributeError(f"MONITOR_CONFIG.trade_management.profit_protection.{name} is required")
    return getattr(cfg, name)


def _group_cfg(name: str) -> Any:
    cfg = _cfg("group_management")
    if cfg is None or not hasattr(cfg, name):
        raise AttributeError(f"MONITOR_CONFIG.trade_management.group_management.{name} is required")
    return getattr(cfg, name)


def _group_cfg_dec(name: str) -> Decimal:
    return d(_group_cfg(name))


def _floor_bucket(value: Decimal, step: Decimal) -> Decimal:
    if value <= 0 or step <= 0:
        return Decimal("0")
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _stop_is_breached(*, side: str, last_price: Decimal, stop_price: Optional[Decimal]) -> bool:
    if stop_price is None or stop_price <= 0 or last_price <= 0:
        return False
    return last_price <= stop_price if side == "BUY" else last_price >= stop_price


def _profit_r(*, side: str, entry_price: Decimal, last_price: Decimal, atr_unit: Decimal) -> Decimal:
    if entry_price <= 0 or atr_unit <= 0:
        return Decimal("0")
    profit = last_price - entry_price if side == "BUY" else entry_price - last_price
    return profit / atr_unit


def _price_reached_target(*, side: str, last_price: Decimal, target_price: Decimal) -> bool:
    if last_price <= 0 or target_price <= 0:
        return False
    return last_price >= target_price if side == "BUY" else last_price <= target_price


def _lock_stop_from_target(*, side: str, previous_target: Decimal, atr_unit: Decimal) -> Optional[Decimal]:
    if previous_target <= 0 or atr_unit <= 0:
        return None
    buffer_r = _cfg_dec("target_lock_buffer_r")
    buffer = atr_unit * max(buffer_r, Decimal("0"))
    return previous_target - buffer if side == "BUY" else previous_target + buffer


def _is_better_stop(*, side: str, new_stop: Optional[Decimal], old_stop: Optional[Decimal]) -> bool:
    if new_stop is None or new_stop <= 0:
        return False
    if old_stop is None or old_stop <= 0:
        return True
    return new_stop > old_stop if side == "BUY" else new_stop < old_stop


def _is_better_target(*, side: str, new_target: Optional[Decimal], old_target: Optional[Decimal]) -> bool:
    """Return True only when the active target is improved farther in profit direction."""
    if new_target is None or new_target <= 0:
        return False
    if old_target is None or old_target <= 0:
        return True
    return new_target > old_target if side == "BUY" else new_target < old_target


def _ratchet_trade_management_levels(
    *,
    side: str,
    tm: Dict[str, Any],
    previous_tm: Dict[str, Any],
) -> Dict[str, Any]:
    """Keep stop, target, and expansion count monotonic."""
    _validate_trade_management_shape(previous_tm)
    _validate_trade_management_shape(tm)
    out = dict(tm)

    prev_stop = d(_required(previous_tm, "current_stop_price"))
    new_stop = d(_required(out, "current_stop_price"))
    if prev_stop > 0 and new_stop > 0 and not _is_better_stop(
        side=side, new_stop=new_stop, old_stop=prev_stop
    ):
        out["current_stop_price"] = _json_number(prev_stop)

    prev_target = d(_required(previous_tm, "current_target_price"))
    new_target = d(_required(out, "current_target_price"))
    if prev_target > 0 and new_target > 0 and not _is_better_target(
        side=side, new_target=new_target, old_target=prev_target
    ):
        out["current_target_price"] = _json_number(prev_target)

    prev_count = int(_required(previous_tm, "expansion_count"))
    new_count = int(_required(out, "expansion_count"))
    if prev_count < 0 or new_count < 0:
        raise ValueError("trade_management.expansion_count cannot be negative")
    out["expansion_count"] = max(prev_count, new_count)
    return out


def _as_dict_payload(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if hasattr(raw, "model_dump"):
        obj = raw.model_dump(mode="python")
        if not isinstance(obj, dict):
            raise TypeError("model_dump() must return a dict")
        return obj
    raise TypeError(f"trade_management must be dict/model, got {type(raw).__name__}")


def extract_underlying_atr(snapshot_dict: Dict[str, Any]) -> Decimal:
    if not isinstance(snapshot_dict, dict):
        raise TypeError("snapshot payload must be a dict")
    indicators = _required(snapshot_dict, "indicators", "snapshot")
    if not isinstance(indicators, dict):
        raise TypeError("snapshot.indicators must be an object")
    atr = _required(indicators, "atr", "snapshot.indicators")
    if not isinstance(atr, dict):
        raise TypeError("snapshot.indicators.atr must be an object")
    value = d(_required(atr, "value", "snapshot.indicators.atr"))
    if value <= 0:
        raise ValueError("snapshot.indicators.atr.value must be positive")
    return value


def instrument_atr_unit(*, instrument_type: Any, underlying_atr: Optional[Decimal]) -> Optional[Decimal]:
    atr = d(underlying_atr or 0)
    if atr <= 0:
        return None
    if _inst(instrument_type) in ("CE", "PE"):
        factor = _cfg_dec("option_atr_factor")
    else:
        factor = Decimal("1")
    unit = atr * factor
    return unit if unit > 0 else None


def initial_stop_r_for_instrument(instrument_type: Any) -> Decimal:
    """Return initial stop R by instrument, keeping option premium noise wider.

    EQ/FUT use the base stop multiple. ATM options use a wider multiple
    because premiums can retrace more than the underlying during normal
    consolidation even when the lifecycle setup remains intact.
    """
    if _inst(instrument_type) in ("CE", "PE"):
        return _cfg_dec("initial_option_stop_r_multiple")
    return _cfg_dec("initial_stop_r_multiple")


def _validate_trade_management_shape(tm: Dict[str, Any]) -> None:
    required_keys = (
        "version",
        "mode",
        "price_basis",
        "posture",
        "entry_price",
        "planned_entry_price",
        "atr_at_entry",
        "instrument_atr",
        "instrument_atr_factor",
        "target_r_multiple",
        "stop_r_multiple",
        "current_target_price",
        "current_stop_price",
        "initial_target_price",
        "initial_stop_price",
        "initial_stop_source",
        "initial_stop_reason",
        "initial_target_source",
        "initial_target_reason",
        "target_expansion_allowed",
        "trail_mode",
        "exit_pressure",
        "profit_protection_applied",
        "profit_protection_trigger_mfe_r",
        "profit_protection_stop_profit_r",
        "group_management_enabled",
        "group_role",
        "mae_risk_exit_required",
        "expansion_count",
        "last_managed_price",
        "last_update_reason",
    )
    missing = [key for key in required_keys if key not in tm]
    if missing:
        raise ValueError(
            "trade_management missing required keys: " + ", ".join(missing)
        )
    if int(tm["version"]) != 2:
        raise ValueError(f"trade_management.version must be 2, got {tm['version']!r}")
    if str(tm["price_basis"]).strip().upper() != "INSTRUMENT":
        raise ValueError("trade_management.price_basis must be INSTRUMENT")


@dataclass(frozen=True)
class TradeManagementDecision:
    posture: str
    reason: str
    trade_management: Dict[str, Any]


class TradeMonHelper:
    @staticmethod
    def initialize_trade_management(
        *,
        side: Any,
        instrument_type: Any,
        entry_price: Any,
        underlying_atr: Optional[Decimal],
        asof_time: Optional[datetime] = None,
        initial_stop_price: Optional[Any] = None,
        initial_stop_source: Optional[str] = None,
        initial_stop_reason: Optional[str] = None,
        initial_target_price: Optional[Any] = None,
        initial_target_source: Optional[str] = None,
        initial_target_reason: Optional[str] = None,
        signal_setup_label: Optional[str] = None,
    ) -> Dict[str, Any]:
        side_s = _side(side)
        inst_s = _inst(instrument_type)
        entry = d(entry_price)
        atr0 = d(underlying_atr or 0)
        factor = _cfg_dec("option_atr_factor") if inst_s in ("CE", "PE") else Decimal("1")
        atr_unit = atr0 * factor if atr0 > 0 else Decimal("0")

        target_r = _cfg_dec("initial_target_r_multiple")
        stop_r = initial_stop_r_for_instrument(inst_s)

        target = _target_price_from_r(side=side_s, entry_price=entry, atr_unit=atr_unit, r_multiple=target_r)
        stop = _stop_price_from_r(side=side_s, entry_price=entry, atr_unit=atr_unit, r_multiple=stop_r)

        stop_source = str(initial_stop_source or "ATR_MULTIPLE").upper().strip()
        stop_reason = str(initial_stop_reason or "initial_stop_from_atr_multiple").strip()
        target_source = str(initial_target_source or "ATR_MULTIPLE").upper().strip()
        target_reason = str(initial_target_reason or "initial_target_from_atr_multiple").strip()

        override_stop = d(initial_stop_price) if initial_stop_price is not None else Decimal("0")
        if stop_source == "SIGNAL_SETUP_LEVEL" and override_stop <= 0:
            raise ValueError("SIGNAL_SETUP_LEVEL_STOP_MISSING_IN_TRADE_MANAGEMENT_INIT")

        if override_stop > 0:
            # Signal/setup reference level is authoritative. If the signal handoff is
            # malformed, fail loudly instead of silently falling back to ATR.
            if (side_s == "BUY" and override_stop < entry) or (side_s == "SELL" and override_stop > entry):
                stop = override_stop
                if atr_unit > 0:
                    stop_r = abs(entry - stop) / atr_unit
            else:
                raise ValueError("initial stop price is invalid for trade side")

        override_target = d(initial_target_price) if initial_target_price is not None else Decimal("0")
        if target_source == "SIGNAL_SETUP_TARGET" and override_target <= 0:
            raise ValueError("SIGNAL_SETUP_TARGET_MISSING_IN_TRADE_MANAGEMENT_INIT")

        if override_target > 0:
            # Signal/setup target is authoritative. If the handoff is malformed,
            # fail loudly instead of silently falling back to an ATR target.
            if (side_s == "BUY" and override_target > entry) or (side_s == "SELL" and override_target < entry):
                target = override_target
                if atr_unit > 0:
                    target_r = abs(target - entry) / atr_unit
            else:
                raise ValueError("initial target price is invalid for trade side")

        # Bought low-premium options can have an ATR-derived stop below zero
        # because option ATR is approximated from the underlying ATR.  A
        # negative premium is impossible, so cap only that exceptional risk
        # distance to the configured fraction of premium and preserve the
        # resulting smaller R multiple.  This is a generic option-premium
        # safeguard, not a symbol-specific repair.
        if inst_s in ("CE", "PE") and side_s == "BUY" and (stop is None or stop <= 0):
            option_risk_cap_pct = _cfg_dec("setup_option_risk_cap_pct")
            if option_risk_cap_pct <= 0 or option_risk_cap_pct >= 1:
                raise ValueError(
                    "trade_management.setup_option_risk_cap_pct must be between 0 and 1"
                )
            stop = entry * (Decimal("1") - option_risk_cap_pct)
            if stop <= 0 or stop >= entry or atr_unit <= 0:
                raise ValueError("unable to construct positive low-premium option stop")
            stop_r = abs(entry - stop) / atr_unit
            stop_source = "OPTION_PREMIUM_RISK_CAP"
            stop_reason = "initial_stop_capped_to_positive_option_premium"

        if target is None or stop is None or target <= 0 or stop <= 0:
            raise ValueError("initial target and stop must be positive")
        if side_s == "BUY" and not (stop < entry < target):
            raise ValueError("BUY initial levels must satisfy stop < entry < target")
        if side_s == "SELL" and not (target < entry < stop):
            raise ValueError("SELL initial levels must satisfy target < entry < stop")

        return {
            "version": 2,
            "mode": str(_cfg("mode")),
            "price_basis": "INSTRUMENT",
            "posture": TradePosture.HOLD.value,
            "entry_price": _json_number(entry),
            "planned_entry_price": _json_number(entry),
            "atr_at_entry": _json_number(atr0) if atr0 > 0 else None,
            "instrument_atr": _json_number(atr_unit) if atr_unit > 0 else None,
            "instrument_atr_factor": _json_number(factor),
            "target_r_multiple": _json_number(target_r),
            "stop_r_multiple": _json_number(stop_r),
            "current_target_price": _json_number(target),
            "current_stop_price": _json_number(stop),
            "initial_target_price": _json_number(target),
            "initial_stop_price": _json_number(stop),
            "initial_stop_source": stop_source,
            "initial_stop_reason": stop_reason,
            "initial_target_source": target_source,
            "initial_target_reason": target_reason,
            "signal_setup_label": str(signal_setup_label or "").upper().strip() or None,
            "target_expansion_allowed": False,
            "trail_mode": "NORMAL",
            "exit_pressure": "LOW",
            "management_context": None,
            "signal_context_available": None,
            "management_posture": None,
            "management_reason_code": None,
            "signal_stage": None,
            "signal_status": None,
            "lifecycle_trade_action": None,
            "directional_alignment": None,
            "auction_action": None,
            "auction_state": None,
            "should_exit_signal": False,
            "profit_protection_applied": False,
            "profit_protection_trigger_mfe_r": _json_number(d(_profit_protection_cfg("trigger_mfe_r"))),
            "profit_protection_stop_profit_r": _json_number(d(_profit_protection_cfg("stop_profit_r"))),
            "current_stop_profit_r": _json_number(-stop_r),
            "group_management_enabled": bool(_group_cfg("enabled")),
            "group_role": "STANDALONE",
            "group_reference_trade_id": None,
            "group_reference_instrument": None,
            "group_reference_symbol": None,
            "group_reference_entry_price": None,
            "group_reference_atr": None,
            "group_reference_current_price": None,
            "group_mfe_r": None,
            "group_mae_r": None,
            "last_processed_group_mfe_bucket_r": None,
            "last_processed_group_mae_bucket_r": None,
            "group_stop_profit_r": _json_number(-stop_r),
            "group_target_r": _json_number(target_r),
            "group_projected_stop_price": _json_number(stop),
            "group_projected_target_price": _json_number(target),
            "group_update_source": "CREATE",
            "group_update_reason": "CREATE",
            "mae_risk_exit_required": False,
            "mae_risk_exit_observed_price": None,
            "expansion_count": 0,
            "last_target_hit_price": None,
            "last_managed_price": _json_number(entry),
            "last_updated_at": asof_time.isoformat() if hasattr(asof_time, "isoformat") else None,
            "last_update_reason": "CREATE",
        }

    @staticmethod
    def rebase_trade_management_after_fill(
        *,
        raw: Any,
        side: Any,
        instrument_type: Any,
        planned_entry_price: Any,
        executed_entry_price: Any,
        asof_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Rebase one strict Auction trade-management record to fill truth."""
        old = _as_dict_payload(raw)
        _validate_trade_management_shape(old)
        requested_planned = d(planned_entry_price)
        stored_planned = d(_required(old, "planned_entry_price"))
        if stored_planned <= 0:
            raise ValueError("trade_management.planned_entry_price must be positive")
        if requested_planned > 0 and requested_planned != stored_planned:
            raise ValueError(
                "planned entry mismatch between trade row and trade_management"
            )
        executed = d(executed_entry_price)
        if executed <= 0:
            raise ValueError("TRADE_MANAGEMENT_REBASE_REQUIRES_EXECUTED_ENTRY_PRICE")

        atr0 = d(_required(old, "atr_at_entry"))
        if atr0 <= 0:
            raise ValueError("trade_management.atr_at_entry must be positive")
        stop_source = str(_required(old, "initial_stop_source")).upper().strip()
        target_source = str(_required(old, "initial_target_source")).upper().strip()

        setup_stop = (
            _required(old, "initial_stop_price")
            if stop_source == "SIGNAL_SETUP_LEVEL"
            else None
        )
        setup_target = (
            _required(old, "initial_target_price")
            if target_source == "SIGNAL_SETUP_TARGET"
            else None
        )

        tm = TradeMonHelper.initialize_trade_management(
            side=side,
            instrument_type=instrument_type,
            entry_price=executed,
            underlying_atr=atr0,
            asof_time=asof_time,
            initial_stop_price=setup_stop,
            initial_stop_source=stop_source,
            initial_stop_reason=_required(old, "initial_stop_reason"),
            initial_target_price=setup_target,
            initial_target_source=target_source,
            initial_target_reason=_required(old, "initial_target_reason"),
            signal_setup_label=_optional(old, "signal_setup_label"),
        )

        for key in (
            "target_expansion_allowed",
            "trail_mode",
            "exit_pressure",
            "management_context",
            "signal_context_available",
            "management_posture",
            "management_reason_code",
            "signal_stage",
            "signal_status",
            "lifecycle_trade_action",
            "directional_alignment",
            "auction_action",
            "auction_state",
            "should_exit_signal",
        ):
            if key in old:
                tm[key] = old[key]

        adverse_slippage = (
            executed - stored_planned
            if _side(side) == "BUY"
            else stored_planned - executed
        )
        atr_unit = d(_required(tm, "instrument_atr"))
        if atr_unit <= 0:
            raise ValueError("trade_management.instrument_atr must be positive")

        tm.update({
            "planned_entry_price": _json_number(stored_planned),
            "entry_price": _json_number(executed),
            "entry_slippage": _json_number(adverse_slippage),
            "entry_slippage_r": _json_number(adverse_slippage / atr_unit),
            "entry_rebased_at": asof_time.isoformat() if asof_time is not None else None,
            "entry_rebase_reason": "EXECUTED_ENTRY_PRICE",
            "last_managed_price": _json_number(executed),
            "last_update_reason": "ENTRY_REBASED_TO_EXECUTED_FILL",
        })
        _validate_trade_management_shape(tm)
        return tm

    @staticmethod
    def normalize_trade_management(
        *,
        raw: Any,
        side: Any,
        instrument_type: Any,
        entry_price: Any,
        underlying_atr: Optional[Decimal],
        asof_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Validate current trade-management state; never migrate or rebuild it."""
        tm = _as_dict_payload(raw)
        _validate_trade_management_shape(tm)
        if str(_required(tm, "mode")).strip() != str(_cfg("mode")):
            raise ValueError(
                f"unsupported trade_management.mode={tm['mode']!r}; "
                f"expected {_cfg('mode')!r}"
            )

        requested_entry = d(entry_price)
        stored_entry = d(_required(tm, "entry_price"))
        if requested_entry <= 0 or stored_entry <= 0:
            raise ValueError("entry prices must be positive")
        if requested_entry != stored_entry:
            raise ValueError(
                f"trade entry/trade_management entry mismatch: "
                f"{requested_entry} != {stored_entry}"
            )

        # ATR is frozen at trade creation. Current snapshots may legitimately
        # carry a different ATR and must not rewrite or invalidate that basis.
        current_snapshot_atr = d(underlying_atr)
        stored_atr = d(_required(tm, "atr_at_entry"))
        if current_snapshot_atr <= 0:
            raise ValueError("current snapshot ATR must be positive")
        if stored_atr <= 0:
            raise ValueError("trade_management.atr_at_entry must be positive")

        instrument_atr = d(_required(tm, "instrument_atr"))
        if instrument_atr <= 0:
            raise ValueError("trade_management.instrument_atr must be positive")
        target = d(_required(tm, "current_target_price"))
        stop = d(_required(tm, "current_stop_price"))
        if target <= 0 or stop <= 0:
            raise ValueError("current target and stop must be positive")

        normalized = dict(tm)
        normalized["mae_risk_exit_required"] = False
        normalized["mae_risk_exit_observed_price"] = None
        normalized["last_updated_at"] = (
            asof_time.isoformat() if asof_time is not None else _optional(tm, "last_updated_at")
        )
        return normalized

    @staticmethod
    def evaluate(
        *,
        trade: Any,
        signal_context: Optional[AuctionTradeSignalContext],
        side: Any,
        instrument_type: Any,
        entry_price: Any,
        last_price: Any,
        trade_management: Dict[str, Any],
        asof_time: Optional[datetime] = None,
        max_favorable_price: Any = None,
        max_adverse_price: Any = None,
        manual_trade_context: bool = False,
        group_role: str = "STANDALONE",
        group_reference_trade_id: Optional[int] = None,
        group_reference_instrument: Optional[str] = None,
        group_reference_symbol: Optional[str] = None,
    ) -> TradeManagementDecision:
        del trade  # state is carried explicitly in the arguments and strict contract
        side_s = _side(side)
        last = d(last_price)
        tm = dict(trade_management)
        _validate_trade_management_shape(tm)
        previous_tm = dict(tm)

        entry = d(_required(tm, "entry_price"))
        requested_entry = d(entry_price)
        if entry <= 0 or requested_entry <= 0 or entry != requested_entry:
            raise ValueError(
                f"trade-management entry mismatch: stored={entry} requested={requested_entry}"
            )
        atr_unit = d(_required(tm, "instrument_atr"))
        target_r = d(_required(tm, "target_r_multiple"))
        stop_r = d(_required(tm, "stop_r_multiple"))

        # Adaptive management may legitimately move the stop exactly to the
        # executed entry price. In that state stop_r_multiple is 0.0R
        # (breakeven), which must remain valid on subsequent monitor cycles.
        # Negative stop multiples are not part of the strict V2 contract.
        if stop_r < 0:
            raise ValueError("stop R multiple must not be negative")
        if atr_unit <= 0 or target_r <= 0 or last <= 0:
            raise ValueError("entry, last price, ATR and target R must be positive")

        normalized_group_role = str(group_role).upper().strip()
        if normalized_group_role not in {"STANDALONE", "REFERENCE", "FOLLOWER"}:
            raise ValueError(f"unsupported group role: {normalized_group_role}")
        group_reference_enabled = bool(_group_cfg("enabled")) and normalized_group_role == "REFERENCE"
        tm["group_management_enabled"] = bool(_group_cfg("enabled"))
        tm["group_role"] = normalized_group_role
        tm["group_reference_trade_id"] = group_reference_trade_id
        tm["group_reference_instrument"] = (
            str(group_reference_instrument).upper().strip()
            if group_reference_instrument is not None
            else None
        )
        tm["group_reference_symbol"] = (
            str(group_reference_symbol).strip()
            if group_reference_symbol is not None
            else None
        )
        if group_reference_enabled:
            tm["group_reference_entry_price"] = _json_number(entry)
            tm["group_reference_atr"] = _json_number(atr_unit)
            tm["group_reference_current_price"] = _json_number(last)
        tm["mae_risk_exit_required"] = False
        tm["mae_risk_exit_observed_price"] = None

        manual_context = bool(manual_trade_context)
        if manual_context and signal_context is not None:
            raise ValueError("manual trade must not receive a signal context")
        if not manual_context and signal_context is None:
            raise ValueError("signal-managed trade requires Auction signal context")

        if manual_context:
            stage = "MANUAL"
            management_posture = "MANUAL_PRICE_ONLY"
            management_reason = "MANUAL_PRICE_ONLY"
            tm["management_context"] = "MANUAL_PRICE_ONLY"
            tm["signal_context_available"] = False
            tm["management_posture"] = None
            tm["management_reason_code"] = None
            tm["signal_stage"] = None
            tm["signal_status"] = None
            tm["lifecycle_trade_action"] = None
            tm["directional_alignment"] = None
            tm["auction_action"] = None
            tm["auction_state"] = None
            tm["should_exit_signal"] = False
            tm["target_expansion_allowed"] = False
            tm["trail_mode"] = "NORMAL"
            tm["exit_pressure"] = "LOW"
            hard_exit = False
            defensive = False
            strengthening = False
        else:
            assert signal_context is not None
            stage = signal_context.stage
            management_posture = signal_context.management_posture
            management_reason = signal_context.management_reason_code
            tm["management_context"] = "AUCTION_SIGNAL_LIFECYCLE"
            tm["signal_context_available"] = True
            tm["management_posture"] = management_posture
            tm["management_reason_code"] = management_reason
            tm["signal_stage"] = stage
            tm["signal_status"] = signal_context.status
            tm["lifecycle_trade_action"] = signal_context.lifecycle_trade_action
            tm["directional_alignment"] = signal_context.directional_alignment
            tm["auction_action"] = signal_context.auction_action
            tm["auction_state"] = signal_context.auction_state
            tm["should_exit_signal"] = signal_context.should_exit_signal
            tm["target_expansion_allowed"] = signal_context.target_expansion_allowed
            tm["trail_mode"] = signal_context.trail_mode
            tm["exit_pressure"] = signal_context.exit_pressure
            hard_exit = signal_context.requires_exit
            defensive = signal_context.is_defensive
            strengthening = signal_context.is_strengthening

        profit_r = _profit_r(
            side=side_s,
            entry_price=entry,
            last_price=last,
            atr_unit=atr_unit,
        )
        favorable = d(max_favorable_price if max_favorable_price is not None else last)
        adverse = d(max_adverse_price if max_adverse_price is not None else last)
        mfe_profit_r = max(
            Decimal("0"),
            _profit_r(
                side=side_s,
                entry_price=entry,
                last_price=favorable,
                atr_unit=atr_unit,
            ),
        )
        adverse_profit_r = _profit_r(
            side=side_s,
            entry_price=entry,
            last_price=adverse,
            atr_unit=atr_unit,
        )
        mae_r = max(Decimal("0"), -adverse_profit_r)

        protection_reason: Optional[str] = None
        adverse_reason: Optional[str] = None
        protection_enabled = bool(_profit_protection_cfg("enabled"))
        protection_trigger_r = max(
            d(_profit_protection_cfg("trigger_mfe_r")),
            Decimal("0"),
        )
        protection_stop_profit_r = d(_profit_protection_cfg("stop_profit_r"))
        protection_applied = bool(_required(tm, "profit_protection_applied"))

        if group_reference_enabled:
            step_r = max(_group_cfg_dec("step_r"), Decimal("0"))
            tm["group_mfe_r"] = _json_number(mfe_profit_r)
            tm["group_mae_r"] = _json_number(mae_r)
            tm["group_reference_current_price"] = _json_number(last)
            tm["group_target_r"] = _json_number(target_r)

            current_stop_price = d(_required(tm, "current_stop_price"))
            current_stop_profit_r = _profit_r_from_price(
                side=side_s,
                entry_price=entry,
                price=current_stop_price,
                atr_unit=atr_unit,
            )
            best_stop_profit_r = current_stop_profit_r
            best_stop_source: Optional[str] = None
            best_stop_reason: Optional[str] = None

            if protection_enabled and mfe_profit_r >= protection_trigger_r:
                completed = Decimal("0")
                if bool(_group_cfg("mfe_ratchet_enabled")) and step_r > 0:
                    completed = _floor_bucket(
                        mfe_profit_r - protection_trigger_r,
                        step_r,
                    )
                mfe_stop_profit_r = protection_stop_profit_r + completed
                if (
                    best_stop_profit_r is None
                    or mfe_stop_profit_r > best_stop_profit_r
                ):
                    best_stop_profit_r = mfe_stop_profit_r
                    best_stop_source = "GROUP_MFE_RATCHET"
                    best_stop_reason = (
                        f"GROUP_MFE_RATCHET_MFE_{mfe_profit_r:.2f}R_"
                        f"STOP_{mfe_stop_profit_r:+.2f}R"
                    )
                tm["last_processed_group_mfe_bucket_r"] = _json_number(
                    protection_trigger_r + completed
                )
                tm["profit_protection_applied"] = True

            mae_requires_defensive = bool(_group_cfg("mae_requires_weakening"))
            unproven_mfe_max = d(_group_cfg("unproven_mfe_max_r"))
            allow_mae_ratchet = (
                not mae_requires_defensive or defensive
            ) and mfe_profit_r <= unproven_mfe_max
            if allow_mae_ratchet:
                chosen_step = None
                for step in _group_cfg("mae_steps"):
                    if mae_r >= d(step.mae_r):
                        if chosen_step is None or d(step.mae_r) > d(chosen_step.mae_r):
                            chosen_step = step
                if chosen_step is not None:
                    mae_stop_profit_r = d(chosen_step.stop_profit_r)
                    if (
                        best_stop_profit_r is None
                        or mae_stop_profit_r > best_stop_profit_r
                    ):
                        best_stop_profit_r = mae_stop_profit_r
                        best_stop_source = "GROUP_MAE_RATCHET"
                        best_stop_reason = (
                            f"GROUP_MAE_RATCHET_MAE_{mae_r:.2f}R_"
                            f"STOP_{mae_stop_profit_r:+.2f}R_"
                            f"POSTURE_{management_posture}"
                        )
                    tm["last_processed_group_mae_bucket_r"] = _json_number(
                        d(chosen_step.mae_r)
                    )

            if best_stop_profit_r is not None and best_stop_source is not None:
                candidate_stop = _stop_price_from_profit_r(
                    side=side_s,
                    entry_price=entry,
                    atr_unit=atr_unit,
                    stop_profit_r=best_stop_profit_r,
                )
                old_stop = d(_required(tm, "current_stop_price"))
                if _is_better_stop(
                    side=side_s,
                    new_stop=candidate_stop,
                    old_stop=old_stop,
                ):
                    tm["current_stop_price"] = _json_number(candidate_stop)
                    tm["current_stop_profit_r"] = _json_number(best_stop_profit_r)
                    tm["stop_r_multiple"] = _json_number(abs(best_stop_profit_r))
                    tm["group_stop_profit_r"] = _json_number(best_stop_profit_r)
                    tm["group_update_source"] = best_stop_source
                    tm["group_update_reason"] = best_stop_reason
                    stop_r = abs(best_stop_profit_r)
                    if best_stop_source == "GROUP_MFE_RATCHET":
                        protection_reason = best_stop_reason
                    else:
                        adverse_reason = best_stop_reason
                        if _stop_is_breached(
                            side=side_s,
                            last_price=last,
                            stop_price=candidate_stop,
                        ):
                            tm["mae_risk_exit_required"] = True
                            tm["mae_risk_exit_observed_price"] = _json_number(last)
                            adverse_reason = (
                                f"{adverse_reason};"
                                "EXIT_AT_OBSERVED_PRICE_NEW_STOP_ALREADY_BREACHED"
                            )
                elif best_stop_source == "GROUP_MFE_RATCHET":
                    protection_reason = f"{best_stop_reason};ALREADY_SATISFIED"
                else:
                    adverse_reason = f"{best_stop_reason};ALREADY_SATISFIED"
        else:
            if (
                protection_enabled
                and mfe_profit_r >= protection_trigger_r
                and not protection_applied
            ):
                new_stop = _stop_price_from_profit_r(
                    side=side_s,
                    entry_price=entry,
                    atr_unit=atr_unit,
                    stop_profit_r=protection_stop_profit_r,
                )
                old_stop = d(_required(tm, "current_stop_price"))
                if _is_better_stop(
                    side=side_s,
                    new_stop=new_stop,
                    old_stop=old_stop,
                ):
                    tm["current_stop_price"] = _json_number(new_stop)
                    tm["current_stop_profit_r"] = _json_number(
                        protection_stop_profit_r
                    )
                    tm["stop_r_multiple"] = _json_number(
                        abs(protection_stop_profit_r)
                    )
                    stop_r = abs(protection_stop_profit_r)
                    protection_reason = (
                        f"PROFIT_PROTECTION_MFE_{mfe_profit_r:.2f}R_"
                        f"STOP_{protection_stop_profit_r:+.2f}R"
                    )
                else:
                    protection_reason = "PROFIT_PROTECTION_ALREADY_SATISFIED"
                tm["profit_protection_applied"] = True

            adverse_threshold = -abs(_cfg_dec("adverse_tighten_profit_r"))
            adverse_stop_r = _cfg_dec("adverse_tighten_stop_r_multiple")
            if (
                defensive
                and profit_r <= adverse_threshold
                and adverse_stop_r > 0
                and adverse_stop_r < stop_r
            ):
                new_stop = _stop_price_from_r(
                    side=side_s,
                    entry_price=entry,
                    atr_unit=atr_unit,
                    r_multiple=adverse_stop_r,
                )
                old_stop = d(_required(tm, "current_stop_price"))
                if _is_better_stop(
                    side=side_s,
                    new_stop=new_stop,
                    old_stop=old_stop,
                ):
                    stop_r = adverse_stop_r
                    tm["stop_r_multiple"] = _json_number(stop_r)
                    tm["current_stop_price"] = _json_number(new_stop)
                    adverse_reason = (
                        f"ADVERSE_TIGHTEN_{profit_r:.2f}R_"
                        f"STAGE_{stage}_POSTURE_{management_posture}"
                    )

        if hard_exit:
            posture = TradePosture.EXIT.value
            reason = f"EXIT_AUCTION_SIGNAL_{management_reason}"
        elif adverse_reason is not None:
            posture = TradePosture.PROTECT.value
            reason = adverse_reason
        elif defensive:
            posture = TradePosture.PROTECT.value
            if group_reference_enabled:
                reason = (
                    f"PROTECT_GROUP_REFERENCE_STAGE_{stage}_"
                    f"POSTURE_{management_posture}"
                )
            elif profit_r > 0:
                desired_stop_r = max(
                    Decimal("0"),
                    profit_r - _cfg_dec("protect_buffer_r"),
                )
                stepped_stop_r = stop_r + _cfg_dec("stop_tighten_r_step")
                new_stop_r = max(stop_r, min(desired_stop_r, stepped_stop_r))
                candidate_stop = _stop_price_from_r(
                    side=side_s,
                    entry_price=entry,
                    atr_unit=atr_unit,
                    r_multiple=new_stop_r,
                )
                old_stop = d(_required(tm, "current_stop_price"))
                if _is_better_stop(
                    side=side_s,
                    new_stop=candidate_stop,
                    old_stop=old_stop,
                ):
                    tm["stop_r_multiple"] = _json_number(new_stop_r)
                    tm["current_stop_price"] = _json_number(candidate_stop)
                reason = (
                    f"PROTECT_PROFIT_STAGE_{stage}_POSTURE_{management_posture}"
                )
            else:
                reason = (
                    f"PROTECT_SIGNAL_STAGE_{stage}_POSTURE_{management_posture}"
                )
        elif (
            strengthening
            and bool(_required(tm, "target_expansion_allowed"))
            and profit_r >= _cfg_dec("expand_ready_profit_r")
        ):
            posture = TradePosture.EXPAND.value
            reason = f"EXPAND_READY_STAGE_{stage}_PROFIT_R_{profit_r:.2f}"
        else:
            posture = TradePosture.HOLD.value
            reason = "HOLD_MANUAL_PRICE_ONLY" if manual_context else (
                f"HOLD_AUCTION_SIGNAL_STAGE_{stage}_POSTURE_{management_posture}"
            )

        if protection_reason is not None and protection_reason not in reason:
            reason = f"{reason};{protection_reason}"

        tm["posture"] = posture
        tm["mfe_profit_r"] = _json_number(mfe_profit_r)
        tm["current_profit_r"] = _json_number(profit_r)
        tm["max_favorable_price"] = _json_number(favorable)
        current_stop = d(_required(tm, "current_stop_price"))
        current_stop_profit_r = _profit_r_from_price(
            side=side_s,
            entry_price=entry,
            price=current_stop,
            atr_unit=atr_unit,
        )
        if current_stop_profit_r is None:
            raise ValueError("unable to derive current_stop_profit_r")
        tm["current_stop_profit_r"] = _json_number(current_stop_profit_r)
        if group_reference_enabled:
            tm["group_stop_profit_r"] = _json_number(current_stop_profit_r)
            tm["group_target_r"] = _required(tm, "target_r_multiple")
            tm["group_projected_stop_price"] = _required(
                tm,
                "current_stop_price",
            )
            tm["group_projected_target_price"] = _required(
                tm,
                "current_target_price",
            )
        tm["last_managed_price"] = _json_number(last)
        tm["last_updated_at"] = asof_time.isoformat() if asof_time is not None else None
        tm["last_update_reason"] = reason
        tm = _ratchet_trade_management_levels(
            side=side_s,
            tm=tm,
            previous_tm=previous_tm,
        )
        _validate_trade_management_shape(tm)
        return TradeManagementDecision(
            posture=posture,
            reason=reason,
            trade_management=tm,
        )


    @staticmethod
    def project_group_reference_to_follower(
        *,
        reference_trade_management: Dict[str, Any],
        follower_trade_management: Dict[str, Any],
        side: Any,
        instrument_type: Any,
        entry_price: Any,
        underlying_atr: Optional[Decimal],
        reference_trade_id: Optional[int],
        reference_instrument: str,
        reference_symbol: str,
        asof_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Project the exact reference-leg group state onto one follower."""
        ref = dict(reference_trade_management)
        tm = dict(follower_trade_management)
        _validate_trade_management_shape(ref)
        _validate_trade_management_shape(tm)

        side_s = _side(side)
        _inst(instrument_type)
        entry = d(_required(tm, "entry_price"))
        requested_entry = d(entry_price)
        if requested_entry <= 0 or requested_entry != entry:
            raise ValueError("follower entry does not match trade_management.entry_price")
        atr0 = d(_required(tm, "atr_at_entry"))
        if underlying_atr is None or d(underlying_atr) != atr0:
            raise ValueError("follower ATR does not match frozen trade_management ATR")
        atr_unit = d(_required(tm, "instrument_atr"))
        if entry <= 0 or atr0 <= 0 or atr_unit <= 0:
            raise ValueError("group follower requires positive entry and frozen ATR")

        stop_profit_r = d(_required(ref, "group_stop_profit_r", "reference_trade_management"))
        target_r = d(_required(ref, "group_target_r", "reference_trade_management"))
        if target_r <= 0:
            raise ValueError("reference group_target_r must be positive")

        mapped_stop = _stop_price_from_profit_r(
            side=side_s, entry_price=entry, atr_unit=atr_unit, stop_profit_r=stop_profit_r
        )
        mapped_target = _target_price_from_r(
            side=side_s, entry_price=entry, atr_unit=atr_unit, r_multiple=target_r
        )
        if mapped_stop is None or mapped_target is None:
            raise ValueError("unable to map reference stop/target to follower")

        old_stop = d(_required(tm, "current_stop_price"))
        if _is_better_stop(side=side_s, new_stop=mapped_stop, old_stop=old_stop):
            tm["current_stop_price"] = _json_number(mapped_stop)
            tm["current_stop_profit_r"] = _json_number(stop_profit_r)
            tm["stop_r_multiple"] = _json_number(abs(stop_profit_r))

        tm["current_target_price"] = _json_number(mapped_target)
        tm["target_r_multiple"] = _json_number(target_r)
        tm["posture"] = str(_required(ref, "posture", "reference_trade_management"))
        tm["target_expansion_allowed"] = bool(
            _required(ref, "target_expansion_allowed", "reference_trade_management")
        )
        tm["group_management_enabled"] = True
        tm["group_role"] = "FOLLOWER"
        if reference_trade_id is None:
            raise ValueError("reference_trade_id is required for follower projection")
        tm["group_reference_trade_id"] = int(reference_trade_id)
        ref_inst = str(reference_instrument).upper().strip()
        if ref_inst not in {"EQ", "FUT"}:
            raise ValueError("reference_instrument must be EQ or FUT")
        tm["group_reference_instrument"] = ref_inst
        ref_symbol = str(reference_symbol).strip()
        if not ref_symbol:
            raise ValueError("reference_symbol is required")
        tm["group_reference_symbol"] = ref_symbol
        tm["group_reference_entry_price"] = _required(ref, "group_reference_entry_price", "reference_trade_management")
        tm["group_reference_atr"] = _required(ref, "group_reference_atr", "reference_trade_management")
        tm["group_reference_current_price"] = _required(ref, "group_reference_current_price", "reference_trade_management")
        tm["group_mfe_r"] = _required(ref, "group_mfe_r", "reference_trade_management")
        tm["group_mae_r"] = _required(ref, "group_mae_r", "reference_trade_management")
        tm["last_processed_group_mfe_bucket_r"] = _required(ref, "last_processed_group_mfe_bucket_r", "reference_trade_management")
        tm["last_processed_group_mae_bucket_r"] = _required(ref, "last_processed_group_mae_bucket_r", "reference_trade_management")
        tm["group_stop_profit_r"] = _json_number(stop_profit_r)
        tm["group_target_r"] = _json_number(target_r)
        tm["group_projected_stop_price"] = _json_number(mapped_stop)
        tm["group_projected_target_price"] = _json_number(mapped_target)
        tm["profit_protection_applied"] = bool(
            _required(ref, "profit_protection_applied", "reference_trade_management")
        )
        tm["expansion_count"] = int(_required(ref, "expansion_count", "reference_trade_management"))
        tm["last_target_hit_price"] = _required(ref, "last_target_hit_price", "reference_trade_management")
        tm["group_update_source"] = "REFERENCE_PROJECTION"
        tm["group_update_reason"] = str(_required(ref, "group_update_reason", "reference_trade_management"))
        tm["mae_risk_exit_required"] = False
        tm["mae_risk_exit_observed_price"] = None
        tm["last_updated_at"] = asof_time.isoformat() if asof_time is not None else None
        tm["last_update_reason"] = (
            f"FOLLOW_GROUP_REFERENCE_{ref_inst}_{reference_trade_id}:"
            f"{tm['group_update_reason']}"
        )
        tm = _ratchet_trade_management_levels(
            side=side_s, tm=tm, previous_tm=follower_trade_management
        )
        _validate_trade_management_shape(tm)
        return tm


    @staticmethod
    def expand_after_target_hit(
        *,
        side: Any,
        entry_price: Any,
        last_price: Any,
        trade_management: Dict[str, Any],
        asof_time: Optional[datetime] = None,
    ) -> TradeManagementDecision:
        """Expand an enabled target after the current target is reached."""
        side_s = _side(side)
        tm = dict(trade_management)
        _validate_trade_management_shape(tm)
        previous_tm = dict(tm)

        if not bool(_required(tm, "target_expansion_allowed")):
            raise ValueError("target expansion called while disabled by signal contract")

        entry = d(_required(tm, "entry_price"))
        requested_entry = d(entry_price)
        if requested_entry <= 0 or requested_entry != entry:
            raise ValueError("entry price does not match trade_management.entry_price")
        last = d(last_price)
        atr_unit = d(_required(tm, "instrument_atr"))
        current_target = d(_required(tm, "current_target_price"))
        target_r = d(_required(tm, "target_r_multiple"))
        if entry <= 0 or last <= 0 or atr_unit <= 0 or current_target <= 0 or target_r <= 0:
            raise ValueError("target expansion requires positive entry, LTP, ATR, target, and target R")
        if not _price_reached_target(
            side=side_s, last_price=last, target_price=current_target
        ):
            raise ValueError("target expansion called before current target was reached")

        step = _cfg_dec("target_expand_r_step")
        if step <= 0:
            raise ValueError("target_expand_r_step must be positive")

        old_stop = d(_required(tm, "current_stop_price"))
        completed_target = current_target
        new_stop = _lock_stop_from_target(
            side=side_s, previous_target=completed_target, atr_unit=atr_unit
        )
        if new_stop is None:
            raise ValueError("unable to derive target-lock stop")
        expansions_this_tick = 0

        for _ in range(50):
            new_target_r = target_r + step
            new_target = _target_price_from_r(
                side=side_s, entry_price=entry, atr_unit=atr_unit, r_multiple=new_target_r
            )
            if new_target is None or new_target <= 0 or new_target == current_target:
                raise ValueError("target expansion produced an invalid target")
            target_r = new_target_r
            current_target = new_target
            expansions_this_tick += 1
            if _price_reached_target(
                side=side_s, last_price=last, target_price=current_target
            ):
                completed_target = current_target
                candidate_stop = _lock_stop_from_target(
                    side=side_s, previous_target=completed_target, atr_unit=atr_unit
                )
                if candidate_stop is None:
                    raise ValueError("unable to derive repeated target-lock stop")
                if _is_better_stop(
                    side=side_s, new_stop=candidate_stop, old_stop=new_stop
                ):
                    new_stop = candidate_stop
                continue
            break
        else:
            raise RuntimeError("target expansion exceeded 50 deterministic steps")

        tm["target_r_multiple"] = _json_number(target_r)
        tm["current_target_price"] = _json_number(current_target)
        if _is_better_stop(side=side_s, new_stop=new_stop, old_stop=old_stop):
            tm["current_stop_price"] = _json_number(new_stop)
            locked_r = abs((new_stop - entry) / atr_unit)
            tm["stop_r_multiple"] = _json_number(locked_r)

        tm["expansion_count"] = int(_required(tm, "expansion_count")) + expansions_this_tick
        tm["last_target_hit_price"] = _json_number(completed_target)
        tm["group_target_r"] = _json_number(target_r)
        locked_stop = d(_required(tm, "current_stop_price"))
        locked_stop_profit_r = _profit_r_from_price(
            side=side_s, entry_price=entry, price=locked_stop, atr_unit=atr_unit
        )
        if locked_stop_profit_r is None:
            raise ValueError("unable to derive locked stop profit R")
        tm["current_stop_profit_r"] = _json_number(locked_stop_profit_r)
        tm["group_stop_profit_r"] = _json_number(locked_stop_profit_r)
        tm["group_projected_stop_price"] = _required(tm, "current_stop_price")
        tm["group_projected_target_price"] = _required(tm, "current_target_price")
        tm["group_update_source"] = "TARGET_EXPANSION"
        tm["last_managed_price"] = _json_number(last)
        tm["last_updated_at"] = asof_time.isoformat() if asof_time is not None else None
        reason = (
            f"TARGET_HIT_EXPANDED_{expansions_this_tick}_STEP"
            f"_TO_{target_r}R_LOCK_STOP_AT_COMPLETED_TARGET_BUFFER"
        )
        tm["last_update_reason"] = reason
        tm["group_update_reason"] = reason
        tm["posture"] = TradePosture.EXPAND.value
        tm = _ratchet_trade_management_levels(
            side=side_s, tm=tm, previous_tm=previous_tm
        )
        _validate_trade_management_shape(tm)
        return TradeManagementDecision(
            posture=TradePosture.EXPAND.value, reason=reason, trade_management=tm
        )

