#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
services/monitor/trade_monitor.py

Lifecycle-style trade monitor.

Responsibilities:
- monitor existing filled trades
- refresh live LTP
- update MTM / PnL / max-min
- update MTM / PnL / max-min
- manage one adaptive target and one adaptive stop via trade_management JSON
- set exit intent only: exit_status=READY
- keep executor responsible for actual order placement/fill finalization

Design:
- No eval
- Explicit ordered monitor checks
- trade_management JSON is the only active target/SL source
- Exact source signal context for every signal-managed trade
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time as dtime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from config import AppConfig
from configs.monitor_config import MONITOR_CONFIG
from configs.execution_config import EXECUTION_CONFIG
from enums.enums import TradeType, EntryStatus, ExitStatus, TradePosture

from schemas.signal import SignalSchema
from schemas.snapshot import SnapshotSchema
from schemas.user import UserSchema
from schemas.user_trade import UserTradeSchema

from services.zerodha.kiteconnect_service import KiteConnectService
from services.audit.auditlog import write_auditlog
from services.trade.monitor.trademon_helper import TradeMonHelper, extract_underlying_atr
from services.trade.monitor.signal_contract import AuctionTradeSignalContext

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

# user_trades.exit_rule is VARCHAR(100).  Keep it as one compact, canonical
# machine-readable code.  Human-readable causal detail belongs in audit payloads,
# not in colon-concatenated database values.
EXIT_RULE_MAX_LENGTH = 100
SIBLING_PRIMARY_EXIT_RULE = "close_sibling_legs_on_primary_exit"


# =============================================================================
# small helpers
# =============================================================================

def d(x: Any) -> Decimal:
    if isinstance(x, Decimal):
        return x
    if x is None:
        raise ValueError("decimal value is required")
    try:
        return Decimal(str(x))
    except Exception as exc:
        raise ValueError(f"invalid decimal value: {x!r}") from exc



def _to_ist_naive(ts: Optional[datetime]) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, str):
        raw = ts.strip()
        if not raw:
            raise ValueError("datetime text cannot be blank")
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception as exc:
            raise ValueError(f"invalid ISO datetime: {raw!r}") from exc
    if not isinstance(ts, datetime):
        raise TypeError(f"datetime required, got {type(ts).__name__}")
    if ts.tzinfo is not None:
        ts = ts.astimezone(IST)
    return ts.replace(tzinfo=None)


def _enum_str(x: Any) -> str:
    v = getattr(x, "value", x)
    return str(v or "").upper().strip()


def _canonical_exit_rule(value: Any, *, context: str) -> str:
    """Validate one exact machine-readable exit-rule code.

    Do not truncate or compose multiple reasons into this database column.  A
    too-long value indicates a programming error and must fail before SQL is
    attempted, while the detailed causal chain remains available in audit JSON.
    """
    rule = str(value or "").strip()
    if not rule:
        raise ValueError(f"{context} exit_rule cannot be blank")
    if len(rule) > EXIT_RULE_MAX_LENGTH:
        raise ValueError(
            f"{context} exit_rule exceeds {EXIT_RULE_MAX_LENGTH} characters: "
            f"length={len(rule)} value={rule!r}"
        )
    return rule


def _trade_side_str(v: Any) -> str:
    side = v.value if isinstance(v, TradeType) else _enum_str(v)
    if side not in {"BUY", "SELL"}:
        raise ValueError(f"trade side must be BUY or SELL, got {v!r}")
    return side


def _entry_status(ut: Any) -> str:
    return _enum_str(getattr(ut, "entry_status"))


def _exit_status(ut: Any) -> str:
    return _enum_str(getattr(ut, "exit_status"))


def _to_optional_price(x: Any) -> Optional[Decimal]:
    if x is None:
        return None
    v = d(x)
    return v if v > 0 else None


def _mapping(value: Any, path: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{path} must be a dict")
    return value


def _required(mapping: Dict[str, Any], key: str, path: str) -> Any:
    if key not in mapping:
        raise ValueError(f"{path}.{key} is required")
    return mapping[key]


def _optional(mapping: Dict[str, Any], key: str, default: Any = None) -> Any:
    return mapping[key] if key in mapping else default


def _normalize_snapshot_dict(snap: Any) -> Dict[str, Any]:
    if isinstance(snap, SnapshotSchema):
        payload = snap.model_dump(mode="python")
    elif isinstance(snap, dict):
        payload = dict(snap)
    else:
        raise TypeError(
            f"snapshot must be SnapshotSchema or dict, got {type(snap).__name__}"
        )
    if not isinstance(payload, dict):
        raise TypeError("snapshot serialization must produce a dict")
    return payload


def _infer_exchange_for_trade(ut: Any) -> str:
    inst = _enum_str(getattr(ut, "instrument_type", None))
    if inst in ("FUT", "CE", "PE"):
        return "NFO"
    return "NSE"


def _quote_key(ut: Any) -> str:
    exchange = _infer_exchange_for_trade(ut)
    symbol = str(getattr(ut, "symbol", "") or "").strip()
    if not symbol:
        raise ValueError("trade.symbol is required for quote lookup")
    entry_snapshot = getattr(ut, "entry_snapshot", None)
    if entry_snapshot is not None:
        snapshot_map = _mapping(entry_snapshot, "trade.entry_snapshot")
        if "exchange" in snapshot_map:
            declared = str(snapshot_map["exchange"]).upper().strip()
            if declared and declared != exchange:
                raise ValueError(
                    f"trade.entry_snapshot.exchange mismatch: {declared!r} != {exchange!r}"
                )
    return f"{exchange}:{symbol}"


def _audit_trade_monitor(ctx: "TradeMonitorContext", updates: Dict[str, Any]) -> None:
    """Persist every authoritative TradeMonitor update or fail visibly."""
    exit_status = _enum_str(updates["exit_status"]) if "exit_status" in updates else ""
    exit_reason = (
        str(updates["exit_reason"]).strip()
        if "exit_reason" in updates and updates["exit_reason"] is not None
        else ""
    )
    exit_rule = (
        str(updates["exit_rule"]).strip()
        if "exit_rule" in updates and updates["exit_rule"] is not None
        else ""
    )
    action = "EXIT_READY" if exit_status == ExitStatus.READY.value else "MONITOR"
    reason_code = exit_reason or "monitor_update"

    updated_management = updates["trade_management"] if "trade_management" in updates else None
    management = (
        _tm_as_dict(updated_management)
        if updated_management is not None
        else _tm_as_dict(ctx.trade_management)
    )
    management_keys = (
        "version",
        "mode",
        "posture",
        "target_expansion_allowed",
        "trail_mode",
        "exit_pressure",
        "management_posture",
        "management_reason_code",
        "signal_stage",
        "signal_status",
        "lifecycle_trade_action",
        "directional_alignment",
        "auction_action",
        "auction_state",
        "should_exit_signal",
        "current_target_price",
        "current_stop_price",
        "target_r_multiple",
        "stop_r_multiple",
        "current_profit_r",
        "mfe_profit_r",
        "profit_protection_applied",
        "expansion_count",
        "last_target_hit_price",
        "max_favorable_price",
        "last_update_reason",
    )
    compact_management = {
        key: management[key] for key in management_keys if key in management
    }
    contract = ctx.signal_contract
    contract_payload = None
    if contract is not None:
        contract_payload = {
            "contract_version": contract.contract_version,
            "signal_id": contract.signal_id,
            "stage": contract.stage,
            "status": contract.status,
            "management_posture": contract.management_posture,
            "management_reason_code": contract.management_reason_code,
            "lifecycle_trade_action": contract.lifecycle_trade_action,
            "should_exit_signal": contract.should_exit_signal,
            "auction_action": contract.auction_action,
            "auction_state": contract.auction_state,
            "directional_alignment": contract.directional_alignment,
            "opportunity_key": contract.opportunity_key,
            "candidate_id": contract.candidate_id,
            "boundary_event_key": contract.boundary_event_key,
        }

    persisted = write_auditlog(
        entity_type="TRADE",
        entity_id=getattr(ctx.trade, "id", None),
        symbol=getattr(ctx.trade, "symbol", None),
        userid=getattr(ctx.trade, "userid", None),
        evaluation_stage="TRADE_MONITOR",
        previous_state=_exit_status(ctx.trade),
        new_state=exit_status or _exit_status(ctx.trade),
        action=action,
        reason_code=reason_code,
        reason_text=exit_rule or reason_code,
        confidence=None,
        ts=ctx.last_time,
        payload_json={
            "signal_id": getattr(ctx.trade, "signal_id", None),
            "instrument_type": ctx.instrument_type,
            "side": ctx.side,
            "last_price": ctx.last_price,
            "entry_price": ctx.entry_price,
            "basis_price": ctx.basis_price,
            "pnl_per_unit": ctx.pnl_per_unit,
            "pnl_value": ctx.pnl_value,
            "management": compact_management,
            "signal_contract": contract_payload,
            "update_fields": sorted(str(key) for key in updates.keys()),
            "exit_status": exit_status or None,
            "exit_reason": exit_reason or None,
            "exit_rule": exit_rule or None,
        },
        strict=True,
        force_persist=True,
    )
    if not persisted:
        raise RuntimeError(
            f"strict TradeMonitor audit was not persisted for trade_id={getattr(ctx.trade, 'id', None)}"
        )


# =============================================================================
# monitor policy helpers
# =============================================================================

def _monitor_use_snapshot() -> bool:
    """Return True when replay/snapshot pricing is active.

    EXECUTION_CONFIG.use_snapshot is the single replay switch for the trade
    pipeline.  Replay_summary forces this flag before it runs generator,
    executor, and monitor.  Keeping one switch avoids drift where executor is
    in replay mode but monitor still reads live broker quote timestamps.
    """
    return bool(EXECUTION_CONFIG.use_snapshot)


def _monitor_use_live_quotes() -> bool:
    return bool(MONITOR_CONFIG.use_live_quotes)


def _instrument_type(ut: Any) -> str:
    inst = _enum_str(getattr(ut, "instrument_type", None))
    aliases = {
        "EQUITY": "EQ",
        "CASH": "EQ",
        "FUTURE": "FUT",
        "FUTURES": "FUT",
        "CALL": "CE",
        "PUT": "PE",
    }
    inst = aliases[inst] if inst in aliases else inst
    if inst not in {"EQ", "FUT", "CE", "PE"}:
        raise ValueError(f"unsupported trade instrument_type: {inst!r}")
    return inst


def _is_manual_trade(ut: Any) -> bool:
    signal_id = str(getattr(ut, "signal_id", "") or "").upper().strip()
    source = _enum_str(getattr(ut, "source", "") or "")
    return signal_id.startswith("MANUAL:") or source == "MANUAL"


def _trade_management_cfg() -> Any:
    return getattr(MONITOR_CONFIG, "trade_management", None)


def _tm_cfg_value(name: str) -> Any:
    cfg = _trade_management_cfg()
    if cfg is None or not hasattr(cfg, name):
        raise AttributeError(f"MONITOR_CONFIG.trade_management.{name} is required")
    return getattr(cfg, name)


def _tm_cfg_bool(name: str) -> bool:
    return bool(_tm_cfg_value(name))


def _group_management_cfg() -> Any:
    cfg = _trade_management_cfg()
    group_cfg = getattr(cfg, "group_management", None) if cfg is not None else None
    if group_cfg is None:
        raise AttributeError("MONITOR_CONFIG.trade_management.group_management is required")
    return group_cfg


def _group_management_enabled() -> bool:
    return bool(getattr(_group_management_cfg(), "enabled"))


def _group_key(ut: Any) -> Optional[Tuple[str, str]]:
    userid = str(getattr(ut, "userid", "") or "").strip()
    signal_id = str(getattr(ut, "signal_id", "") or "").strip()
    if not userid or not signal_id or signal_id.upper().startswith("MANUAL:"):
        return None
    return userid, signal_id


def _select_group_reference_ids(trades: List[Any]) -> Dict[Tuple[str, str], int]:
    if not _group_management_enabled():
        return {}
    priority = [str(x or "").upper().strip() for x in list(getattr(_group_management_cfg(), "reference_priority", []) or [])]
    if not priority:
        raise ValueError("group_management.reference_priority must not be empty")
    rank = {inst: idx for idx, inst in enumerate(priority)}
    grouped: Dict[Tuple[str, str], List[Any]] = {}
    for ut in trades:
        key = _group_key(ut)
        if key is not None:
            grouped.setdefault(key, []).append(ut)

    selected: Dict[Tuple[str, str], int] = {}
    for key, rows in grouped.items():
        candidates = [r for r in rows if _instrument_type(r) in rank]
        if not candidates:
            continue
        candidates.sort(key=lambda r: (rank[_instrument_type(r)], int(getattr(r, "id", 0) or 0)))
        ref_id = int(getattr(candidates[0], "id", 0) or 0)
        if ref_id > 0:
            selected[key] = ref_id
    return selected


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    val = _to_optional_price(value)
    return val if val is not None and val > 0 else None


def _tm_as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        obj = value.model_dump(mode="python", exclude_none=False)
        if not isinstance(obj, dict):
            raise TypeError("trade_management.model_dump() must return a dict")
        return dict(obj)
    raise TypeError(
        f"trade_management must be dict/model, got {type(value).__name__}"
    )


def _normalize_trade_management_for_monitor(
    *,
    raw: Any,
    side: str,
    basis_price: Decimal,
    snapshot_dict: Dict[str, Any],
    asof_time: datetime,
    instrument_type: str = "EQ",
) -> Dict[str, Any]:
    return TradeMonHelper.normalize_trade_management(
        raw=raw,
        side=side,
        instrument_type=instrument_type,
        entry_price=basis_price,
        underlying_atr=extract_underlying_atr(snapshot_dict),
        asof_time=asof_time,
    )


class PriceProvider:
    """Strict batch LTP fetcher using KiteConnect.quote()."""

    def __init__(self) -> None:
        self._svc: Optional[KiteConnectService] = None

    def _ensure(self) -> KiteConnectService:
        if self._svc is not None:
            return self._svc
        data_userid = str(AppConfig.DATA_USER or "").strip()
        if not data_userid:
            raise ValueError("AppConfig.DATA_USER is required for live quotes")
        user = UserSchema.fetch_user(data_userid)
        if user is None:
            raise LookupError(f"DATA_USER not found: {data_userid}")
        api_key = str(getattr(user, "apikey", "") or "").strip()
        access_token = str(getattr(user, "access_token", "") or "").strip()
        if not api_key or not access_token:
            raise ValueError("DATA_USER apikey and access_token are required")
        self._svc = KiteConnectService(api_key=api_key, access_token=access_token)
        return self._svc

    def fetch_ltps(self, quote_keys: List[str]) -> Dict[str, Tuple[Decimal, datetime]]:
        if not quote_keys:
            return {}
        if not _monitor_use_live_quotes():
            raise RuntimeError(
                "live quotes are disabled while snapshot replay mode is false"
            )
        data = self._ensure().fetch_quote(quote_keys)
        if not isinstance(data, dict):
            raise TypeError(
                f"quote response must be a dict, got {type(data).__name__}"
            )
        out: Dict[str, Tuple[Decimal, datetime]] = {}
        for key in quote_keys:
            if key not in data:
                raise LookupError(f"quote response missing key: {key}")
            rec = _mapping(data[key], f"quote[{key!r}]")
            ltp = _to_optional_price(_required(rec, "last_price", f"quote[{key!r}]"))
            if ltp is None:
                raise ValueError(f"quote[{key!r}].last_price must be positive")
            timestamp = (
                rec["timestamp"]
                if "timestamp" in rec
                else rec["last_trade_time"]
                if "last_trade_time" in rec
                else None
            )
            if not isinstance(timestamp, datetime):
                raise ValueError(
                    f"quote[{key!r}] requires datetime timestamp or last_trade_time"
                )
            out[key] = (ltp, timestamp)
        return out


# =============================================================================
# decision state
# =============================================================================

@dataclass
class TradeMonitorContext:
    trade: Any
    side: str
    instrument_type: str
    last_price: Decimal
    last_time: datetime
    entry_price: Decimal
    quantity: Decimal
    basis_price: Decimal
    pnl_per_unit: Decimal
    pnl_value: Decimal

    trade_management: Dict[str, Any]

    managed_stop_price: Optional[Decimal]
    managed_target_price: Optional[Decimal]

    max_price: Decimal
    min_price: Decimal

    intraday_only: bool
    intraday_exit_time: datetime

    source_signal: Optional[SignalSchema]
    snapshot: SnapshotSchema
    signal_contract: Optional[AuctionTradeSignalContext]

    group_role: str = "STANDALONE"
    group_reference_trade_id: Optional[int] = None
    group_reference_instrument: Optional[str] = None
    group_reference_symbol: Optional[str] = None


# =============================================================================
# monitor
# =============================================================================

class TradeMonitor:
    def __init__(self):
        self._cutoff_hms = self._load_intraday_cutoff_hms()
        self._pp = PriceProvider()
        self._pass_ltp_map: Dict[str, Tuple[Optional[Decimal], datetime]] = {}
        self._pass_group_reference_ids: Dict[Tuple[str, str], int] = {}
        # Per-pass item failures are recorded so long-running backend services
        # can continue with the next trade while callers/replays can still fail
        # the cycle explicitly after inspecting the complete result set.
        self.last_pass_errors: List[Dict[str, Any]] = []

        self._dt_keys = {
            "last_time",
            "max_time",
            "min_time",
            "exit_time",
            "exit_intent_time",
        }

        self._float_keys = {
            "last_price",
            "last_pnl",
            "last_pnl_value",
            "max_price",
            "min_price",
            "exit_price",
            "exit_pnl",
        }

    # -----------------------------------------------------------------
    # config
    # -----------------------------------------------------------------
    def _load_intraday_cutoff_hms(self) -> Tuple[int, int, int]:
        raw = str(MONITOR_CONFIG.intraday_cutoff_time).strip()
        if not raw:
            raise ValueError("MONITOR_CONFIG.intraday_cutoff_time is required")
        cutoff = dtime.fromisoformat(raw)
        return (cutoff.hour, cutoff.minute, cutoff.second)

    # -----------------------------------------------------------------
    # public
    # -----------------------------------------------------------------
    def run_once(self, snapshot_time: Optional[datetime] = None) -> int:
        return self.monitor(snapshot_time=snapshot_time)

    def monitor(self, snapshot_time: Optional[datetime] = None) -> int:
        """
        Monitor open filled positions.

        UserTradeSchema.fetch_open_positions() is expected to return:
        - entry_status = FILLED
        - exit_status not terminal/filled
        """
        self.last_pass_errors = []
        trades = UserTradeSchema.fetch_open_positions()

        if not trades:
            logger.info("TradeMonitor: no open positions to monitor.")
            return 0

        self._pass_group_reference_ids = _select_group_reference_ids(trades)

        # Reference FUT/EQ must be evaluated before its followers so any new
        # group stop/target state is available to the option/EQ projections in
        # this same monitor pass. Preserve package chronology using the earliest
        # entry time across all legs, not the individual leg entry timestamp.
        group_first_time: Dict[Tuple[str, str], datetime] = {}
        for row in trades:
            row_key = _group_key(row)
            if row_key is None:
                continue
            row_time = _to_ist_naive(getattr(row, "entry_time", None)) or datetime.min
            previous = group_first_time[row_key] if row_key in group_first_time else None
            if previous is None or row_time < previous:
                group_first_time[row_key] = row_time

        def _group_sort_key(ut: Any) -> Tuple[Any, str, int, int]:
            key = _group_key(ut)
            ref_id = self._pass_group_reference_ids[key] if key is not None and key in self._pass_group_reference_ids else None
            role_rank = 0 if ref_id and int(getattr(ut, "id", 0) or 0) == ref_id else 1
            entry_sort_time = group_first_time[key] if key is not None and key in group_first_time else None
            entry_sort_time = entry_sort_time or _to_ist_naive(getattr(ut, "entry_time", None)) or datetime.min
            group_label = f"{key[0]}:{key[1]}" if key is not None else f"STANDALONE:{getattr(ut, 'id', 0)}"
            return (entry_sort_time, group_label, role_rank, int(getattr(ut, "id", 0) or 0))

        trades = sorted(trades, key=_group_sort_key)
        quote_keys = [_quote_key(ut) for ut in trades]
        ltp_map = {} if _monitor_use_snapshot() else self._pp.fetch_ltps(quote_keys)
        self._pass_ltp_map = dict(ltp_map)

        updated_count = 0

        for ut, quote_key in zip(trades, quote_keys):
            try:
                # A previous primary-leg update in this same monitor pass may
                # have already marked this sibling leg READY for exit.  Re-read
                # the row before evaluating stale in-memory state so we do not
                # overwrite RELATED_PRIMARY_EXIT with an independent decision.
                current_ut = UserTradeSchema.fetch_user_trade_by_id_strict(int(getattr(ut, "id")))
                if current_ut is not None:
                    ut = current_ut
                if _exit_status(ut) in (ExitStatus.READY.value, ExitStatus.SUBMITTED.value, ExitStatus.FILLED.value):
                    logger.info(
                        "TradeMonitor: skip already exiting trade_id=%s symbol=%s exit_status=%s",
                        getattr(ut, "id", None),
                        getattr(ut, "symbol", None),
                        _exit_status(ut),
                    )
                    continue

                snapshot = self._fetch_snapshot(ut, asof_time=snapshot_time)
                if _monitor_use_snapshot():
                    ltp = self._price_from_snapshot_for_trade(ut, snapshot)
                    ltp_time = snapshot.snapshot_time
                    if not isinstance(ltp_time, datetime):
                        raise ValueError("snapshot.snapshot_time must be datetime")
                else:
                    if quote_key not in ltp_map:
                        raise LookupError(f"live LTP map missing quote key: {quote_key}")
                    ltp, ltp_time = ltp_map[quote_key]
                    if ltp <= 0:
                        raise ValueError(f"live LTP must be positive for {quote_key}")
                    if not isinstance(ltp_time, datetime):
                        raise ValueError(f"live LTP time must be datetime for {quote_key}")

                group_key = _group_key(ut)
                reference_trade_id = self._pass_group_reference_ids[group_key] if group_key is not None and group_key in self._pass_group_reference_ids else None
                trade_id = int(getattr(ut, "id", 0) or 0)
                group_role = (
                    "REFERENCE" if reference_trade_id and trade_id == reference_trade_id
                    else "FOLLOWER" if reference_trade_id
                    else "STANDALONE"
                )
                reference_trade = (
                    ut if group_role == "REFERENCE"
                    else UserTradeSchema.fetch_user_trade_by_id_strict(int(reference_trade_id)) if reference_trade_id
                    else None
                )

                source_signal, signal_contract = self._fetch_source_signal_context(ut)
                ctx = self._build_context(
                    ut=ut,
                    last_price=ltp,
                    last_time=ltp_time,
                    snapshot=snapshot,
                    source_signal=source_signal,
                    signal_contract=signal_contract,
                    group_role=group_role,
                    group_reference_trade=reference_trade,
                )

                updates = self._evaluate_trade(ctx)

                if updates:
                    payload = self._normalize_updates(updates)
                    persisted = UserTradeSchema.update_user_trade_by_id_strict(int(ut.id), payload)
                    if persisted is None:
                        raise RuntimeError(
                            f"trade update returned no row for trade_id={getattr(ut, 'id', None)}"
                        )

                    updated_count += 1
                    updated_count += self._mark_sibling_legs_on_primary_exit(ctx, payload)

                    # Audit follows authoritative trade persistence. A missing
                    # lifecycle audit is now fatal and visible to the caller.
                    _audit_trade_monitor(ctx, payload)

                    logger.info(
                        "TradeMonitor: updated trade_id=%s symbol=%s side=%s last=%s pnl=%s exit_status=%s reason=%s",
                        getattr(ut, "id", None),
                        getattr(ut, "symbol", None),
                        ctx.side,
                        ctx.last_price,
                        _optional(updates, "last_pnl_value"),
                        _optional(updates, "exit_status"),
                        _optional(updates, "exit_reason"),
                    )

            except Exception as exc:
                error = {
                    "trade_id": getattr(ut, "id", None),
                    "symbol": getattr(ut, "symbol", None),
                    "userid": getattr(ut, "userid", None),
                    "signal_id": getattr(ut, "signal_id", None),
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                }
                self.last_pass_errors.append(error)
                logger.exception(
                    "TradeMonitor: trade evaluation error; continuing with next trade "
                    "trade_id=%s symbol=%s error_type=%s",
                    error["trade_id"],
                    error["symbol"],
                    error["error_type"],
                )
                continue

        if self.last_pass_errors:
            logger.error(
                "TradeMonitor: pass completed with item errors updated=%d errors=%d",
                updated_count,
                len(self.last_pass_errors),
            )
        else:
            logger.info("TradeMonitor: pass updated %d positions", updated_count)
        return updated_count


    def _refresh_group_followers_before_exit(self, ctx: TradeMonitorContext) -> None:
        """Stamp every filled sibling with this same evaluation's own price.

        Reference legs are evaluated first.  Without this refresh, replay would
        close an option at its previous snapshot price.  Each sibling is updated
        from its own quote/snapshot before RELATED_PRIMARY_EXIT is persisted.
        """
        userid = str(getattr(ctx.trade, "userid", "") or "").strip()
        signal_id = str(getattr(ctx.trade, "signal_id", "") or "").strip()
        if not userid or not signal_id:
            return
        siblings = UserTradeSchema.fetch_active_trades_for_signal(userid=userid, signal_id=signal_id)
        for sib in siblings:
            if int(getattr(sib, "id", 0) or 0) == int(getattr(ctx.trade, "id", 0) or 0):
                continue
            if _entry_status(sib) != EntryStatus.FILLED.value:
                continue
            if _exit_status(sib) in (ExitStatus.READY.value, ExitStatus.SUBMITTED.value, ExitStatus.FILLED.value):
                continue

            try:
                snapshot = self._fetch_snapshot(sib, asof_time=ctx.last_time)
                if _monitor_use_snapshot():
                    price = self._price_from_snapshot_for_trade(sib, snapshot)
                    price_time = snapshot.snapshot_time
                    if not isinstance(price_time, datetime):
                        raise ValueError("follower snapshot_time must be datetime")
                else:
                    sibling_quote_key = _quote_key(sib)
                    if sibling_quote_key not in self._pass_ltp_map:
                        raise LookupError(
                            f"live LTP map missing follower key: {sibling_quote_key}"
                        )
                    price, price_time = self._pass_ltp_map[sibling_quote_key]
                    if price is None or price <= 0:
                        raise ValueError(
                            f"follower LTP must be positive: {sibling_quote_key}"
                        )
                    if not isinstance(price_time, datetime):
                        raise ValueError(
                            f"follower LTP time must be datetime: {sibling_quote_key}"
                        )

                follower_ctx = self._build_context(
                    ut=sib,
                    last_price=d(price),
                    last_time=price_time,
                    snapshot=snapshot,
                    source_signal=ctx.source_signal,
                    signal_contract=ctx.signal_contract,
                    group_role="FOLLOWER",
                    group_reference_trade=ctx.trade,
                )
                follower_updates = self._evaluate_group_follower(follower_ctx)
                payload = self._normalize_updates(follower_updates)
                if payload:
                    UserTradeSchema.update_user_trade_by_id_strict(int(getattr(sib, "id")), payload)
            except Exception:
                logger.exception(
                    "TradeMonitor: failed to refresh group follower before exit | primary_trade_id=%s sibling_trade_id=%s",
                    getattr(ctx.trade, "id", None),
                    getattr(sib, "id", None),
                )
                raise



    def _mark_sibling_legs_on_primary_exit(self, ctx: TradeMonitorContext, updates: Dict[str, Any]) -> int:
        """Close sibling legs when a primary EQ/FUT leg exits.

        Hedged trade sets are created under one signal_id.  If a primary
        EQ/FUT leg exits because the setup/adaptive stop failed, any remaining
        CE/PE/FUT/EQ sibling must be flattened too.  Option-only exits do not
        force primary-leg closure.
        """
        if "exit_status" not in updates or _enum_str(updates["exit_status"]) != ExitStatus.READY.value:
            return 0

        inst = _enum_str(getattr(ctx.trade, "instrument_type", None))
        if inst not in ("EQ", "FUT"):
            return 0
        if _group_management_enabled() and ctx.group_reference_trade_id and ctx.group_role != "REFERENCE":
            return 0

        userid = str(getattr(ctx.trade, "userid", "") or "").strip()
        signal_id = str(getattr(ctx.trade, "signal_id", "") or "").strip()
        if not userid or not signal_id:
            return 0

        reason = str(_required(updates, "exit_reason", "monitor updates")).strip()
        rule = _canonical_exit_rule(
            _required(updates, "exit_rule", "monitor updates"),
            context="primary monitor update",
        )
        sibling_reason = "RELATED_PRIMARY_EXIT"
        sibling_rule = _canonical_exit_rule(
            SIBLING_PRIMARY_EXIT_RULE,
            context="sibling primary-exit update",
        )
        sibling_reason_text = (
            f"{sibling_rule}; primary_exit_reason={reason}; "
            f"primary_exit_rule={rule}"
        )

        if _group_management_enabled() and ctx.group_role == "REFERENCE":
            self._refresh_group_followers_before_exit(ctx)

        try:
            marked = UserTradeSchema.mark_sibling_trades_exit_for_signal(
                userid=userid,
                signal_id=signal_id,
                exclude_trade_id=getattr(ctx.trade, "id", None),
                reason=sibling_reason,
                rule=sibling_rule,
                ts=ctx.last_time,
            )
        except Exception:
            logger.exception(
                "TradeMonitor: sibling-leg exit marking failed | trade_id=%s signal_id=%s",
                getattr(ctx.trade, "id", None),
                signal_id,
            )
            raise

        for sib in marked:
            try:
                persisted = write_auditlog(
                    entity_type="TRADE",
                    entity_id=getattr(sib, "id", None),
                    symbol=getattr(sib, "symbol", None),
                    userid=getattr(sib, "userid", None),
                    evaluation_stage="TRADE_MONITOR",
                    previous_state=ExitStatus.NONE.value,
                    new_state=ExitStatus.READY.value,
                    action="EXIT_READY",
                    reason_code=sibling_reason,
                    reason_text=sibling_reason_text,
                    confidence=None,
                    ts=ctx.last_time,
                    payload_json={
                        "signal_id": signal_id,
                        "primary_trade_id": getattr(ctx.trade, "id", None),
                        "primary_symbol": getattr(ctx.trade, "symbol", None),
                        "primary_instrument_type": inst,
                        "primary_exit_reason": reason,
                        "primary_exit_rule": rule,
                        "sibling_trade_id": getattr(sib, "id", None),
                        "sibling_symbol": getattr(sib, "symbol", None),
                        "sibling_instrument_type": getattr(sib, "instrument_type", None),
                        "sibling_observed_exit_price": getattr(sib, "exit_price", None),
                        "group_reference_mfe_r": _required(ctx.trade_management, "group_mfe_r", "trade_management"),
                        "group_reference_mae_r": _required(ctx.trade_management, "group_mae_r", "trade_management"),
                        "group_stop_profit_r": _required(ctx.trade_management, "group_stop_profit_r", "trade_management"),
                        "group_target_r": _required(ctx.trade_management, "group_target_r", "trade_management"),
                    },
                    strict=True,
                    force_persist=True,
                )
                if not persisted:
                    raise RuntimeError(
                        f"strict sibling TradeMonitor audit was not persisted for trade_id={getattr(sib, 'id', None)}"
                    )
            except Exception:
                logger.exception("TradeMonitor: sibling-leg audit failed")
                raise

        if marked:
            logger.info(
                "TradeMonitor: marked %d sibling legs for exit | primary_trade_id=%s signal_id=%s reason=%s",
                len(marked),
                getattr(ctx.trade, "id", None),
                signal_id,
                reason,
            )
        return len(marked)

    # -----------------------------------------------------------------
    # data fetch
    # -----------------------------------------------------------------
    def _fetch_snapshot(self, ut: Any, asof_time: Optional[datetime] = None) -> SnapshotSchema:
        equity_ref = str(getattr(ut, "equity_ref", "") or "").strip().upper()
        if not equity_ref:
            raise ValueError("trade.equity_ref is required for snapshot lookup")
        snapshot = (
            SnapshotSchema.fetch_latest_for_symbol_asof(equity_ref, asof_time)
            if asof_time is not None
            else SnapshotSchema.fetch_latest_for_symbol(equity_ref)
        )
        if snapshot is None:
            raise LookupError(
                f"No snapshot available for trade_id={getattr(ut, 'id', None)} "
                f"equity_ref={equity_ref} asof={asof_time}"
            )
        return snapshot

    def _price_from_snapshot_for_trade(self, ut: Any, snapshot: SnapshotSchema) -> Decimal:
        inst = _instrument_type(ut)
        symbol = str(getattr(ut, "symbol", "") or "").strip().upper()
        if not symbol:
            raise ValueError("trade.symbol is required for snapshot pricing")

        if inst == "EQ":
            value = _to_optional_price(snapshot.close)
            if value is None:
                raise ValueError("snapshot.close must be positive for EQ pricing")
            return value

        derivatives = snapshot.derivatives
        if inst == "FUT":
            future = derivatives.future
            if not isinstance(future, dict):
                raise TypeError("snapshot.derivatives.future must be an object")
            if "instrument" not in future or "last_price" not in future:
                raise ValueError("future.instrument and future.last_price are required")
            if str(future["instrument"]).strip().upper() != symbol:
                raise ValueError(
                    f"future instrument mismatch: {future['instrument']!r} != {symbol!r}"
                )
            value = _to_optional_price(future["last_price"])
            if value is None:
                raise ValueError("future.last_price must be positive")
            return value

        if inst in ("CE", "PE"):
            ladder = derivatives.option_ladder
            if not isinstance(ladder, dict):
                raise TypeError("snapshot.derivatives.option_ladder must be an object")
            side_key = "calls" if inst == "CE" else "puts"
            if side_key not in ladder or not isinstance(ladder[side_key], list):
                raise ValueError(f"option_ladder.{side_key} is required")
            matches = [
                row
                for row in ladder[side_key]
                if isinstance(row, dict)
                and "symbol" in row
                and str(row["symbol"]).strip().upper() == symbol
            ]
            if len(matches) != 1:
                raise ValueError(
                    f"Expected exactly one {inst} ladder row for {symbol}; found {len(matches)}"
                )
            row = matches[0]
            if "ltp" not in row:
                raise ValueError(f"option_ladder.{side_key} row ltp is required")
            value = _to_optional_price(row["ltp"])
            if value is None:
                raise ValueError("option ladder ltp must be positive")
            return value

        raise ValueError(f"Unsupported instrument_type for snapshot pricing: {inst}")

    def _fetch_source_signal_context(
        self,
        ut: Any,
    ) -> Tuple[Optional[SignalSchema], Optional[AuctionTradeSignalContext]]:
        if _is_manual_trade(ut):
            return None, None

        signal_id = str(getattr(ut, "signal_id", "") or "").strip()
        if not signal_id:
            raise ValueError("signal-managed trade requires trade.signal_id")
        signal = SignalSchema.fetch_by_signal_id_strict(signal_id)
        if signal is None:
            raise LookupError(f"source signal not found: {signal_id}")
        contract = AuctionTradeSignalContext.from_signal(signal)
        if contract.signal_id != signal_id:
            raise ValueError(
                f"trade/source signal mismatch: {signal_id!r} != {contract.signal_id!r}"
            )
        return signal, contract


    # -----------------------------------------------------------------
    # context
    # -----------------------------------------------------------------
    def _build_context(
        self,
        *,
        ut: Any,
        last_price: Decimal,
        last_time: datetime,
        snapshot: SnapshotSchema,
        source_signal: Optional[SignalSchema],
        signal_contract: Optional[AuctionTradeSignalContext],
        group_role: str = "STANDALONE",
        group_reference_trade: Optional[Any] = None,
    ) -> TradeMonitorContext:
        if not isinstance(snapshot, SnapshotSchema):
            raise TypeError("TradeMonitor context requires SnapshotSchema")
        if not isinstance(last_time, datetime):
            raise TypeError("TradeMonitor context requires datetime last_time")

        side = _trade_side_str(getattr(ut, "trade_type", None))
        instrument_type = _instrument_type(ut)
        planned_entry = d(getattr(ut, "entry_price", None))
        executed_entry = d(getattr(ut, "executed_entry_price", None))
        quantity = d(getattr(ut, "quantity", None))
        if planned_entry <= 0 or executed_entry <= 0 or quantity <= 0:
            raise ValueError(
                "filled trade requires positive planned entry, executed entry, and quantity"
            )
        if _entry_status(ut) != EntryStatus.FILLED.value:
            raise ValueError("TradeMonitor context only accepts FILLED trades")

        basis_price = executed_entry
        lp = d(last_price)
        if lp <= 0:
            raise ValueError("last_price must be positive")
        pnl_per_unit = lp - basis_price if side == "BUY" else basis_price - lp
        pnl_value = pnl_per_unit * quantity

        raw_management = getattr(ut, "trade_management", None)
        if raw_management is None:
            raise ValueError("filled trade requires trade_management")
        trade_management = _normalize_trade_management_for_monitor(
            raw=raw_management,
            side=side,
            basis_price=basis_price,
            snapshot_dict=_normalize_snapshot_dict(snapshot),
            asof_time=last_time,
            instrument_type=instrument_type,
        )
        managed_stop_price = _decimal_or_none(
            _required(trade_management, "current_stop_price", "trade_management")
        )
        managed_target_price = _decimal_or_none(
            _required(trade_management, "current_target_price", "trade_management")
        )
        if managed_stop_price is None or managed_target_price is None:
            raise ValueError("trade_management stop and target must be positive")

        max_price = d(getattr(ut, "max_price", None))
        min_price = d(getattr(ut, "min_price", None))
        if max_price <= 0 or min_price <= 0:
            raise ValueError("filled trade requires positive max_price and min_price")
        intraday_only = bool(getattr(ut, "intraday_only", False))
        cutoff = dtime(*self._cutoff_hms)
        intraday_exit_time = last_time.replace(
            hour=cutoff.hour, minute=cutoff.minute, second=cutoff.second, microsecond=0
        )

        manual = _is_manual_trade(ut)
        if manual:
            if source_signal is not None or signal_contract is not None:
                raise ValueError("manual trade cannot carry source signal context")
        elif source_signal is None or signal_contract is None:
            raise ValueError("signal-managed trade requires exact signal context")

        normalized_role = str(group_role).upper().strip()
        if normalized_role not in {"STANDALONE", "REFERENCE", "FOLLOWER"}:
            raise ValueError(f"unsupported group role: {normalized_role!r}")
        reference_id: Optional[int] = None
        reference_instrument: Optional[str] = None
        reference_symbol: Optional[str] = None
        if group_reference_trade is not None:
            reference_id = int(getattr(group_reference_trade, "id"))
            if reference_id <= 0:
                raise ValueError("group reference trade id must be positive")
            reference_instrument = _instrument_type(group_reference_trade)
            reference_symbol = str(getattr(group_reference_trade, "symbol", "") or "").strip()
            if not reference_symbol:
                raise ValueError("group reference symbol is required")
        elif normalized_role in {"REFERENCE", "FOLLOWER"}:
            raise ValueError(f"{normalized_role} group role requires reference trade")

        return TradeMonitorContext(
            trade=ut, side=side, instrument_type=instrument_type,
            last_price=lp, last_time=last_time, entry_price=planned_entry,
            quantity=quantity, basis_price=basis_price, pnl_per_unit=pnl_per_unit,
            pnl_value=pnl_value, trade_management=trade_management,
            managed_stop_price=managed_stop_price,
            managed_target_price=managed_target_price, max_price=max_price,
            min_price=min_price, intraday_only=intraday_only,
            intraday_exit_time=intraday_exit_time, source_signal=source_signal,
            snapshot=snapshot,
            signal_contract=signal_contract, group_role=normalized_role,
            group_reference_trade_id=reference_id,
            group_reference_instrument=reference_instrument,
            group_reference_symbol=reference_symbol,
        )

    def _trade_age_minutes(self, ctx: TradeMonitorContext) -> Decimal:
        entry_time = getattr(ctx.trade, "entry_exec_time", None)
        if not isinstance(entry_time, datetime):
            raise ValueError("filled trade requires datetime entry_exec_time")
        start = _to_ist_naive(entry_time)
        end = _to_ist_naive(ctx.last_time)
        if start is None or end is None:
            raise ValueError("unable to normalize trade age timestamps")
        return d((end - start).total_seconds() / 60.0)

    def _is_same_or_earlier_entry_tick(self, ctx: TradeMonitorContext) -> bool:
        """Prevent immediate same-tick exit after a trade is created/filled.

        The monitor should still update MTM/max-min/trade_management on the
        entry tick, but it should not set exit_status=READY from lifecycle,
        dynamic target, soft stop, or protective SL until the next snapshot/tick.
        This avoids draft/executed rows being created and closed at P&L=0.
        """
        return self._trade_age_minutes(ctx) <= Decimal("0")

    # -----------------------------------------------------------------
    # deterministic evaluator
    # -----------------------------------------------------------------
    def _intraday_cutoff_due(self, ctx: TradeMonitorContext) -> bool:
        """Return True when an intraday position must be flattened now.

        This check is intentionally independent of signal lifecycle and
        adaptive trade-management state.  It is an operational safety rule.
        """
        return bool(
            ctx.intraday_only
            and isinstance(ctx.last_time, datetime)
            and isinstance(ctx.intraday_exit_time, datetime)
            and ctx.last_time >= ctx.intraday_exit_time
        )

    def _evaluate_trade(self, ctx: TradeMonitorContext) -> Dict[str, Any]:
        # Intraday cutoff is fail-safe and must run before group projection,
        # signal inspection, or adaptive management.  This prevents malformed
        # optional context from keeping a live intraday position open.  The
        # reference leg still marks its siblings READY in the normal persistence
        # path; a follower can also flatten itself if its reference price/context
        # is unavailable at cutoff.
        if self._intraday_cutoff_due(ctx):
            updates: Dict[str, Any] = {}
            self._update_live_metrics(ctx, updates)
            self._update_extremes(ctx, updates)
            updates.update(
                self._exit_payload(
                    ctx,
                    reason="INTRADAY_CUTOFF",
                    rule="intraday_auto_exit_fail_safe",
                )
            )
            updates["last_snapshot"] = _normalize_snapshot_dict(ctx.snapshot)
            logger.warning(
                "TradeMonitor: fail-safe intraday cutoff | trade_id=%s symbol=%s role=%s last_time=%s cutoff=%s",
                getattr(ctx.trade, "id", None),
                getattr(ctx.trade, "symbol", None),
                ctx.group_role,
                ctx.last_time,
                ctx.intraday_exit_time,
            )
            return updates

        if _group_management_enabled() and ctx.group_role == "FOLLOWER":
            return self._evaluate_group_follower(ctx)

        updates: Dict[str, Any] = {}

        self._update_live_metrics(ctx, updates)
        self._update_extremes(ctx, updates)
        self._update_trade_management(ctx, updates)
        self._maybe_expand_after_adaptive_target(ctx, updates)
        exit_update = self._evaluate_exit_intent(ctx)

        if exit_update:
            updates.update(exit_update)

        updates["last_snapshot"] = _normalize_snapshot_dict(ctx.snapshot)

        return updates

    def _evaluate_group_follower(self, ctx: TradeMonitorContext) -> Dict[str, Any]:
        """Update a sibling from the exact reference-leg management state."""
        updates: Dict[str, Any] = {}
        self._update_live_metrics(ctx, updates)
        self._update_extremes(ctx, updates)

        reference_id = ctx.group_reference_trade_id
        if reference_id is None:
            raise ValueError("group follower requires reference trade id")
        reference_trade = UserTradeSchema.fetch_user_trade_by_id_strict(
            int(reference_id)
        )
        reference_tm = _tm_as_dict(
            getattr(reference_trade, "trade_management", None)
        )
        if not bool(getattr(_group_management_cfg(), "map_levels_to_siblings")):
            raise ValueError(
                "group follower projection cannot be disabled for an active follower"
            )
        if ctx.group_reference_instrument is None or ctx.group_reference_symbol is None:
            raise ValueError("group follower requires reference instrument and symbol")

        projected = TradeMonHelper.project_group_reference_to_follower(
            reference_trade_management=reference_tm,
            follower_trade_management=ctx.trade_management,
            side=ctx.side,
            instrument_type=ctx.instrument_type,
            entry_price=ctx.basis_price,
            underlying_atr=d(
                _required(ctx.trade_management, "atr_at_entry", "trade_management")
            ),
            reference_trade_id=reference_id,
            reference_instrument=ctx.group_reference_instrument,
            reference_symbol=ctx.group_reference_symbol,
            asof_time=ctx.last_time,
        )

        atr_unit = d(
            _required(projected, "instrument_atr", "projected trade_management")
        )
        if atr_unit <= 0:
            raise ValueError("projected instrument ATR must be positive")
        current_profit_r = (
            (ctx.last_price - ctx.basis_price) / atr_unit
            if ctx.side == "BUY"
            else (ctx.basis_price - ctx.last_price) / atr_unit
        )
        favorable = (
            max(ctx.max_price, ctx.last_price)
            if ctx.side == "BUY"
            else min(ctx.min_price, ctx.last_price)
        )
        follower_mfe_r = (
            (favorable - ctx.basis_price) / atr_unit
            if ctx.side == "BUY"
            else (ctx.basis_price - favorable) / atr_unit
        )
        projected["current_profit_r"] = float(current_profit_r)
        projected["mfe_profit_r"] = float(max(Decimal("0"), follower_mfe_r))
        projected["max_favorable_price"] = float(favorable)
        updates["trade_management"] = projected
        ctx.trade_management.clear()
        ctx.trade_management.update(projected)
        if ctx.snapshot is None:
            raise ValueError("group follower requires current snapshot")
        updates["last_snapshot"] = _normalize_snapshot_dict(ctx.snapshot)
        return updates


    def _update_live_metrics(self, ctx: TradeMonitorContext, updates: Dict[str, Any]) -> None:
        updates["last_time"] = ctx.last_time
        updates["last_price"] = ctx.last_price
        updates["last_pnl"] = ctx.pnl_per_unit
        updates["last_pnl_value"] = ctx.pnl_value

    def _update_extremes(self, ctx: TradeMonitorContext, updates: Dict[str, Any]) -> None:
        ut = ctx.trade

        if ctx.last_price > ctx.max_price:
            updates["max_price"] = ctx.last_price
            updates["max_time"] = ctx.last_time
        elif getattr(ut, "max_time", None) is None:
            updates["max_time"] = ctx.last_time

        if ctx.last_price < ctx.min_price:
            updates["min_price"] = ctx.last_price
            updates["min_time"] = ctx.last_time
        elif getattr(ut, "min_time", None) is None:
            updates["min_time"] = ctx.last_time

    def _update_trade_management(self, ctx: TradeMonitorContext, updates: Dict[str, Any]) -> None:
        """Evaluate lifecycle-driven adaptive posture and persist target/stop."""
        favorable_price = max(ctx.max_price, ctx.last_price) if ctx.side == "BUY" else min(ctx.min_price, ctx.last_price)
        adverse_price = min(ctx.min_price, ctx.last_price) if ctx.side == "BUY" else max(ctx.max_price, ctx.last_price)
        decision = TradeMonHelper.evaluate(
            trade=ctx.trade,
            signal_context=ctx.signal_contract,
            side=ctx.side,
            instrument_type=ctx.instrument_type,
            entry_price=ctx.basis_price,
            last_price=ctx.last_price,
            trade_management=ctx.trade_management,
            asof_time=ctx.last_time,
            max_favorable_price=favorable_price,
            manual_trade_context=_is_manual_trade(ctx.trade),
            max_adverse_price=adverse_price,
            group_role=ctx.group_role,
            group_reference_trade_id=ctx.group_reference_trade_id,
            group_reference_instrument=ctx.group_reference_instrument,
            group_reference_symbol=ctx.group_reference_symbol,
        )
        if decision.trade_management != (ctx.trade_management or {}):
            updates["trade_management"] = decision.trade_management
            ctx.trade_management.clear()
            ctx.trade_management.update(decision.trade_management)


    def _maybe_expand_after_adaptive_target(self, ctx: TradeMonitorContext, updates: Dict[str, Any]) -> None:
        """Treat target hit as a lifecycle decision point, not an unconditional exit.

        If current adaptive target is reached and the latest posture is EXPAND,
        lock most of the achieved target as the new stop and extend the target.
        If posture is not EXPAND, _evaluate_exit_intent will exit at target.
        """
        if not _tm_cfg_bool("exit_on_current_target"):
            return
        if not self._dynamic_target_hit(ctx):
            return
        if _required(ctx.trade_management, "posture", "trade_management") != TradePosture.EXPAND.value:
            return

        decision = TradeMonHelper.expand_after_target_hit(
            side=ctx.side,
            entry_price=ctx.basis_price,
            last_price=ctx.last_price,
            trade_management=ctx.trade_management,
            asof_time=ctx.last_time,
        )
        if decision.trade_management != (ctx.trade_management or {}):
            updates["trade_management"] = decision.trade_management
            ctx.trade_management.clear()
            ctx.trade_management.update(decision.trade_management)
            logger.info(
                "TradeMonitor: adaptive target expanded | trade_id=%s symbol=%s inst=%s side=%s reason=%s target=%s stop=%s",
                getattr(ctx.trade, "id", None),
                getattr(ctx.trade, "symbol", None),
                ctx.instrument_type,
                ctx.side,
                decision.reason,
                _required(ctx.trade_management, "current_target_price", "trade_management"),
                _required(ctx.trade_management, "current_stop_price", "trade_management"),
            )


    def _evaluate_exit_intent(self, ctx: TradeMonitorContext) -> Dict[str, Any]:
        """
        Return exit-intent updates if an exit condition is met.

        Phase-1 monitor policy:
        - no hedging
        - no partial exit
        - trade_management.current_target_price is the only actionable target
        - trade_management.current_stop_price is the only actionable stop
        - lifecycle deterioration exits derivatives faster than equity
        """
        ut = ctx.trade

        if _entry_status(ut) != EntryStatus.FILLED.value:
            return {}

        xs = _exit_status(ut)
        if xs in (
            ExitStatus.READY.value,
            ExitStatus.SUBMITTED.value,
            ExitStatus.FILLED.value,
        ):
            return {}

        # Keep a second cutoff guard here for direct evaluator callers.  It is
        # deliberately before the entry-tick skip and every adaptive/signal
        # branch: a position created or recovered after the cutoff must still be
        # flattened immediately.
        if self._intraday_cutoff_due(ctx):
            return self._exit_payload(
                ctx,
                reason="INTRADAY_CUTOFF",
                rule="intraday_auto_exit_fail_safe",
            )

        if _tm_cfg_bool("skip_exit_on_entry_tick") and self._is_same_or_earlier_entry_tick(ctx):
            logger.info(
                "TradeMonitor: skip entry-tick exit evaluation | trade_id=%s symbol=%s entry_time=%s last_time=%s last=%s pnl=%s",
                getattr(ut, "id", None),
                getattr(ut, "symbol", None),
                getattr(ut, "entry_time", None),
                ctx.last_time,
                ctx.last_price,
                ctx.pnl_value,
            )
            return {}

        if bool(_required(ctx.trade_management, "mae_risk_exit_required", "trade_management")):
            return self._exit_payload(
                ctx,
                reason="WEAKENING_RISK_EXIT",
                rule="exit_at_observed_price_when_new_mae_stop_is_already_breached",
            )

        # An exact Auction signal-lifecycle exit is authoritative and must keep
        # its own reason code. The adaptive posture may also be EXIT because it
        # was derived from the same contract, but it must not obscure the source.
        signal_exit = self._signal_exit_payload(ctx)
        if signal_exit:
            return signal_exit

        if _required(ctx.trade_management, "posture", "trade_management") == TradePosture.EXIT.value:
            return self._exit_payload(
                ctx,
                reason="ADAPTIVE_POSTURE_EXIT",
                rule="exit_on_adaptive_trade_posture",
            )

        if _tm_cfg_bool("exit_on_current_target") and self._dynamic_target_hit(ctx):
            return self._exit_payload(
                ctx,
                reason="ADAPTIVE_TARGET",
                rule="exit_on_current_adaptive_target",
            )

        # Protective SL is only a disaster/emergency guard. The preferred exit
        # path is target/signal/soft-profit protection above.
        if _tm_cfg_bool("exit_on_current_stop") and self._protective_sl_hit(ctx):
            return self._exit_payload(
                ctx,
                reason="ADAPTIVE_STOP",
                rule="exit_on_current_adaptive_stop",
            )

        return {}

    def _signal_exit_payload(self, ctx: TradeMonitorContext) -> Dict[str, Any]:
        contract = ctx.signal_contract
        if contract is None:
            return {}
        if not contract.requires_exit:
            return {}
        # Keep exit_rule as an exact machine-readable contract value. The
        # detailed lifecycle reason remains available in trade_management and
        # the TradeMonitor audit payload.
        return self._exit_payload(
            ctx,
            reason="SIGNAL_LIFECYCLE_EXIT",
            rule="exit_on_auction_signal_downstream_contract",
        )

    def _dynamic_target_hit(self, ctx: TradeMonitorContext) -> bool:
        target = _decimal_or_none(_required(ctx.trade_management, "current_target_price", "trade_management"))
        if target is None or target <= 0:
            return False
        return ctx.last_price >= target if ctx.side == "BUY" else ctx.last_price <= target

    def _protective_sl_hit(self, ctx: TradeMonitorContext) -> bool:
        stop = _decimal_or_none(_required(ctx.trade_management, "current_stop_price", "trade_management"))
        if stop is None or stop <= 0:
            return False
        return ctx.last_price <= stop if ctx.side == "BUY" else ctx.last_price >= stop

    def _exit_payload(
        self,
        ctx: TradeMonitorContext,
        *,
        reason: str,
        rule: str,
        extra_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        canonical_rule = _canonical_exit_rule(rule, context="exit payload")
        updates: Dict[str, Any] = {
            "exit_status": ExitStatus.READY.value,
            "exit_intent_time": ctx.last_time,
            "exit_time": ctx.last_time,
            "exit_price": ctx.last_price,
            "exit_pnl": ctx.pnl_value,
            "exit_reason": reason,
            "exit_rule": canonical_rule,
            "last_time": ctx.last_time,
            "last_price": ctx.last_price,
            "last_pnl": ctx.pnl_per_unit,
            "last_pnl_value": ctx.pnl_value,
        }

        if extra_reason is not None:
            detail = str(extra_reason).strip()
            if not detail:
                raise ValueError("extra exit reason cannot be blank")
            updates["exit_rule"] = f"{rule}:{detail}"
        return updates

    # -----------------------------------------------------------------
    # normalization
    # -----------------------------------------------------------------
    def _normalize_updates(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}

        for key, value in updates.items():
            if value is None:
                out[key] = None
                continue

            if key in self._dt_keys:
                out[key] = _to_ist_naive(value) if isinstance(value, datetime) else value
                continue

            if key == "exit_rule":
                out[key] = _canonical_exit_rule(
                    value,
                    context="normalized monitor update",
                )
                continue

            if isinstance(value, Decimal):
                out[key] = float(value) if key in self._float_keys else float(value)
                continue

            out[key] = value

        return out


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    logger.info("Starting Trade Monitor pass")

    try:
        count = TradeMonitor().monitor()
        logger.info("Monitor updated %d positions", count)
    except Exception:
        logger.exception("Fatal error in Trade Monitor")
        raise
