#!/usr/bin/env python3
"""
services/trade/generator/trade_generator.py

AUTOGEN trade orchestrator.

Responsibilities:
- select eligible backend users
- select eligible lifecycle signals
- delegate trade eligibility to TradeGenValidator
- invoke TradeGenHelper for planning/persistence

TradeGenHelper remains responsible for:
- signal lookup
- integrity checks
- instrument resolution
- pricing defaults
- persistence
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from configs.trade_config import TRADE_CONFIG
from enums.enums import SignalStatus
from schemas.signal import SignalSchema
from schemas.user import UserSchema
from schemas.user_trade import UserTradeSchema
from services.trade.generator.tradegen_validator import TradeDecisionHelper, MODE_AUTO
from services.trade.generator.tradegen_helper import TradeGenHelper
from services.audit.auditlog import write_auditlog

logger = logging.getLogger(__name__)


# =============================================================================
# constants / helpers
# =============================================================================

AUTOGEN_SOURCE = "TRADE_GENERATOR"

def _audit_trade_decision(
    *,
    userid: str,
    signal: SignalSchema,
    decision: Any,
    action: str,
    reason_code: str,
    result: Optional[Dict[str, Any]] = None,
) -> None:
    try:
        details = decision.to_dict() if hasattr(decision, "to_dict") else {}
        audit_ts = _signal_audit_ts(signal, details)
        audit_reason = _signal_audit_reason(signal, details, reason_code)
        write_auditlog(
            entity_type="TRADE_DECISION",
            entity_id=getattr(signal, "signal_id", None),
            symbol=getattr(signal, "symbol", None) or getattr(signal, "equity_ref", None),
            userid=userid,
            evaluation_stage="TRADE_GENERATOR",
            previous_state=getattr(signal, "status", None),
            new_state=getattr(signal, "stage", None),
            action=action,
            reason_code=reason_code,
            reason_text=(
                ", ".join(details.get("reasons") or [])
                if isinstance(details, dict) and details.get("reasons")
                else audit_reason
            ),
            confidence=_signal_confidence(signal),
            ts=audit_ts,
            payload_json={
                "snapshot_time": audit_ts,
                "signal": {
                    "signal_id": getattr(signal, "signal_id", None),
                    "last_eval_time": getattr(signal, "last_eval_time", None),
                    "last_snapshot_time": getattr(signal, "last_snapshot_time", None),
                    "first_seen_time": getattr(signal, "first_seen_time", None),
                },
                "decision": details,
                "result": result or {},
                "lifecycle_name": getattr(signal, "lifecycle", None),
                "originating_setup": getattr(signal, "setup", None),
                "side": getattr(signal, "side", None),
            },
        )
    except Exception:
        logger.warning("trade decision audit failed", exc_info=True)


def _audit_trade_created(
    *,
    userid: str,
    signal: SignalSchema,
    trade_ids: List[Any],
    result: Dict[str, Any],
) -> None:
    try:
        audit_ts = _signal_audit_ts(signal, {})
        audit_reason = _signal_audit_reason(signal, {}, "TradeGenerator created trade rows from signal.")
        write_auditlog(
            entity_type="TRADE",
            entity_id=",".join(str(x) for x in trade_ids if x),
            symbol=getattr(signal, "symbol", None) or getattr(signal, "equity_ref", None),
            userid=userid,
            evaluation_stage="TRADE_GENERATOR",
            previous_state="SIGNAL_OPEN",
            new_state="TRADE_CREATED",
            action="CREATE_TRADE",
            reason_code="trade_generator_allowed",
            reason_text=audit_reason,
            confidence=_signal_confidence(signal),
            ts=audit_ts,
            payload_json={
                "snapshot_time": audit_ts,
                "signal_id": getattr(signal, "signal_id", None),
                "signal": {
                    "signal_id": getattr(signal, "signal_id", None),
                    "last_eval_time": getattr(signal, "last_eval_time", None),
                    "last_snapshot_time": getattr(signal, "last_snapshot_time", None),
                    "first_seen_time": getattr(signal, "first_seen_time", None),
                },
                "trade_ids": trade_ids,
                "result": result,
                "lifecycle_name": getattr(signal, "lifecycle", None),
                "originating_setup": getattr(signal, "setup", None),
                "side": getattr(signal, "side", None),
            },
        )
    except Exception:
        logger.warning("trade create audit failed", exc_info=True)


def _enum_str(x: Any) -> str:
    v = getattr(x, "value", x)
    return str(v or "").strip().upper()


def _safe_userid(user: UserSchema) -> str:
    return str(getattr(user, "userid", "") or "").strip()


def _as_dict(v: Any) -> Dict[str, Any]:
    if isinstance(v, dict):
        return v
    return {}


def _signal_meta(signal: SignalSchema) -> Dict[str, Any]:
    meta = _as_dict(getattr(signal, "meta_json", None))
    signal_meta = meta.get("signal")
    if not isinstance(signal_meta, dict):
        raise ValueError("signal.meta_json['signal'] is required for trade generation")
    return signal_meta


def _signal_confidence(signal: SignalSchema) -> Optional[float]:
    """Return signal confidence from signal.meta_json['signal']."""
    value = _signal_meta(signal).get("confidence")
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _signal_side(signal: SignalSchema) -> str:
    side = _enum_str(getattr(signal, "side", ""))
    return "BUY" if side == "BUY" else "SELL" if side == "SELL" else ""


def _symbol_family(signal: SignalSchema) -> str:
    return str(
        getattr(signal, "equity_ref", "")
        or getattr(signal, "underlying", "")
        or getattr(signal, "symbol", "")
        or ""
    ).strip().upper()


def _trade_exposure_side(trade: UserTradeSchema) -> str:
    """Map an existing trade row to market exposure side.

    Current version uses options for leverage, not hedging:
      - CE long means BUY exposure
      - PE long means SELL exposure
      - EQ/FUT use trade_type BUY/SELL
    """
    inst = _enum_str(getattr(trade, "instrument_type", ""))
    if inst == "CE":
        return "BUY"
    if inst == "PE":
        return "SELL"
    ttype = _enum_str(getattr(trade, "trade_type", ""))
    return "BUY" if ttype == "BUY" else "SELL" if ttype == "SELL" else ""


def _trade_summary(rows: List[UserTradeSchema]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        out.append({
            "id": getattr(row, "id", None),
            "symbol": getattr(row, "symbol", None),
            "instrument_type": getattr(row, "instrument_type", None),
            "trade_type": getattr(row, "trade_type", None),
            "entry_status": getattr(row, "entry_status", None),
            "exit_status": getattr(row, "exit_status", None),
            "exposure_side": _trade_exposure_side(row),
        })
    return out


def _mark_opposite_family_for_exit_if_needed(*, userid: str, signal: SignalSchema) -> Optional[Dict[str, Any]]:
    """If a new opposite-side signal appears, flatten old family first.

    Returns a result dict when trade creation should be skipped for this pass.
    Returns None when no opposite active family exists.
    """
    equity_ref = _symbol_family(signal)
    new_side = _signal_side(signal)
    if not userid or not equity_ref or new_side not in ("BUY", "SELL"):
        return None

    active = UserTradeSchema.fetch_active_trades_for_user_equity_ref(
        userid=userid,
        equity_ref=equity_ref,
    ) or []
    if not active:
        return None

    active_sides = {side for side in (_trade_exposure_side(t) for t in active) if side}
    if active_sides and active_sides.issubset({new_side}):
        # Same-side family is still active. TradeDecisionHelper will WAIT/BLOCK
        # because only one active family per user+symbol is allowed.
        return None

    marked = UserTradeSchema.mark_active_trades_exit_for_user_equity_ref(
        userid=userid,
        equity_ref=equity_ref,
        reason="OPPOSITE_SIGNAL",
        rule="trade_generator_opposite_signal",
        ts=_signal_audit_ts(signal, {}),
    )
    return {
        "ok": False,
        "error": "OPPOSITE_SIGNAL_EXIT_MARKED",
        "details": {
            "userid": userid,
            "signal_id": getattr(signal, "signal_id", None),
            "equity_ref": equity_ref,
            "new_side": new_side,
            "active_sides": sorted(active_sides),
            "active_trades": _trade_summary(active),
            "marked_trade_ids": [getattr(t, "id", None) for t in marked or []],
            "message": "Existing opposite-side trade family marked READY for exit; new trade creation deferred.",
        },
    }


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and str(value).strip() != "":
            return value
    return None


def _deep_get(obj: Any, path: List[str]) -> Any:
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _signal_audit_ts(signal: SignalSchema, decision_details: Optional[Dict[str, Any]] = None) -> Any:
    details = decision_details if isinstance(decision_details, dict) else {}
    return _first_present(
        details.get("snapshot_time"),
        details.get("last_eval_time"),
        _deep_get(details, ["details", "snapshot_time"]),
        _deep_get(details, ["signal", "last_eval_time"]),
        _deep_get(details, ["signal", "last_snapshot_time"]),
        _deep_get(details, ["lifecycle", "snapshot_time"]),
        getattr(signal, "last_eval_time", None),
        getattr(signal, "last_snapshot_time", None),
        getattr(signal, "first_seen_time", None),
    )


def _reason_from_dict(value: Any) -> str:
    if isinstance(value, dict):
        return str(
            value.get("reason")
            or value.get("reason_text")
            or value.get("primary_reason")
            or value.get("message")
            or value.get("label")
            or value.get("key")
            or ""
        ).strip()
    return str(value or "").strip()


def _signal_audit_reason(signal: SignalSchema, decision_details: Optional[Dict[str, Any]] = None, fallback: str = "") -> str:
    details = decision_details if isinstance(decision_details, dict) else {}
    meta = _signal_meta(signal)

    candidates = [
        details.get("reason"),
        details.get("reason_text"),
        details.get("primary_reason"),
        details.get("signal_reason"),
        _deep_get(details, ["lifecycle", "reason"]),
        _deep_get(details, ["lifecycle", "signal_reason"]),
        _deep_get(details, ["details", "reason"]),
        _deep_get(details, ["details", "signal_reason"]),
        meta.get("reason"),
        meta.get("signal_reason"),
        meta.get("status_reason"),
        meta.get("primary_reason"),
        getattr(signal, "reason", None),
        getattr(signal, "status_reason", None),
        fallback,
    ]

    for value in candidates:
        text = _reason_from_dict(value)
        if text and not text.upper().startswith("SIGNAL_ACTION_"):
            return text

    return str(fallback or "").strip()

_TRADE_ENTRY_STATE_VERSION = "TRADE_ENTRY_STATE_V1"
_PRICE_DEFER_REASON_CODES = {
    str(TRADE_CONFIG.policy.decision.signal_entry_wait_in_loss_code).strip(),
    str(TRADE_CONFIG.policy.decision.signal_entry_wait_price_missing_code).strip(),
}


def _merge_trade_entry_state(
    *,
    meta_json: Dict[str, Any],
    userid: str,
    state: str,
    reason_code: str,
    evaluated_at: Any,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return signal metadata with one user's entry-decision state updated."""
    if not isinstance(meta_json, dict):
        raise ValueError("signal.meta_json must be an object")
    userid0 = str(userid or "").strip()
    if not userid0:
        raise ValueError("userid is required for trade entry state")

    next_meta = dict(meta_json)
    existing = next_meta.get("trade_entry_state")
    block = dict(existing) if isinstance(existing, dict) else {}
    users_raw = block.get("users")
    users = dict(users_raw) if isinstance(users_raw, dict) else {}
    previous_raw = users.get(userid0)
    previous = dict(previous_raw) if isinstance(previous_raw, dict) else {}

    state0 = str(state or "").strip().upper()
    reason0 = str(reason_code or "").strip()
    count = int(previous.get("evaluation_count") or 0) + 1
    first_deferred_at = previous.get("first_deferred_at")
    if state0 == "DEFERRED" and first_deferred_at in (None, ""):
        first_deferred_at = evaluated_at

    defer_class = (
        "PRICE"
        if state0 == "DEFERRED" and reason0 in _PRICE_DEFER_REASON_CODES
        else previous.get("defer_class")
    )
    users[userid0] = {
        "state": state0,
        "reason_code": reason0,
        "defer_class": defer_class,
        "first_deferred_at": first_deferred_at,
        "last_evaluated_at": evaluated_at,
        "evaluation_count": count,
        "details": dict(details or {}),
    }
    block.update({
        "contract_version": _TRADE_ENTRY_STATE_VERSION,
        "users": users,
    })
    next_meta["trade_entry_state"] = block
    return next_meta


def _persist_trade_entry_state(
    *,
    signal: SignalSchema,
    userid: str,
    state: str,
    reason_code: str,
    decision_details: Optional[Dict[str, Any]] = None,
) -> None:
    """Persist only the non-structural entry-decision projection."""
    try:
        evaluated_at = _signal_audit_ts(signal, decision_details or {})
        signal_id = str(getattr(signal, "signal_id", "") or "").strip()
        latest = SignalSchema.fetch_by_signal_id_strict(signal_id) or signal
        next_meta = _merge_trade_entry_state(
            meta_json=_as_dict(getattr(latest, "meta_json", None)),
            userid=userid,
            state=state,
            reason_code=reason_code,
            evaluated_at=evaluated_at,
            details=decision_details,
        )
        persisted = SignalSchema.update_signal(
            signal_id=signal_id,
            meta_json=next_meta,
        )
        if persisted is None:
            raise RuntimeError("signal row missing while persisting trade entry state")
    except Exception:
        logger.warning(
            "TradeGenerator: failed to persist trade entry state | userid=%s signal_id=%s state=%s",
            userid,
            getattr(signal, "signal_id", None),
            state,
            exc_info=True,
        )


def _record_price_defer_if_needed(
    *,
    signal: SignalSchema,
    userid: str,
    decision: Any,
) -> None:
    reasons = [
        str(reason or "").strip()
        for reason in (getattr(decision, "reasons", None) or [])
    ]
    matched = next(
        (reason for reason in reasons if reason in _PRICE_DEFER_REASON_CODES),
        None,
    )
    if matched is None:
        return
    details = decision.to_dict() if hasattr(decision, "to_dict") else {}
    _persist_trade_entry_state(
        signal=signal,
        userid=userid,
        state="DEFERRED",
        reason_code=matched,
        decision_details=details,
    )


def _is_autogen_eligible_user(user: UserSchema) -> bool:
    """Defence-in-depth eligibility for automatic signal deployment."""
    return (
        int(getattr(user, "active", 0) or 0) == 1
        and int(getattr(user, "logged_in", 0) or 0) == 1
        and int(getattr(user, "autotrade", 0) or 0) == 1
    )

# Lifecycle deployment gating is delegated to TradeDecisionHelper.
# Keep this orchestrator free of legacy signal_action/trade_action checks.



# =============================================================================
# Fetchers
# =============================================================================

class TradeGeneratorFetcher:
    @staticmethod
    def fetch_autogen_users() -> List[UserSchema]:
        try:
            users = UserSchema.fetch_autogen_users() or []
            return [u for u in users if _is_autogen_eligible_user(u)]
        except Exception:
            logger.exception("TradeGenerator: fetch_autogen_users failed")
            return []

    @staticmethod
    def fetch_user(userid: str) -> Optional[UserSchema]:
        try:
            return UserSchema.fetch_user(userid)
        except Exception:
            logger.exception("TradeGenerator: fetch_user failed | userid=%s", userid)
            return None

    @staticmethod
    def fetch_open_signals(limit: int = 1000) -> List[SignalSchema]:
        try:
            return SignalSchema.list_for_ui(
                statuses=[SignalStatus.OPEN.value],
                limit=limit,
            ) or []
        except Exception:
            logger.exception("TradeGenerator: fetch_open_signals failed")
            return []


# =============================================================================
# Orchestrator
# =============================================================================

class TradeGenerator:
    """
    Thin AUTOGEN orchestrator around TradeDecisionHelper + TradeGenHelper.
    """

    def __init__(self):
        self.fetcher = TradeGeneratorFetcher()

    def generate_for_user_signal(
        self,
        *,
        userid: str,
        signal_id: str,
        instrument_choice: str = "MULTI",
        source: str = AUTOGEN_SOURCE,
    ) -> Dict[str, Any]:
        """
        Compatibility wrapper for one user + one signal.
        """
        signal = SignalSchema.fetch_by_signal_id(signal_id)

        if not signal:
            return {
                "ok": False,
                "error": "SIGNAL_NOT_FOUND",
                "details": {"signal_id": signal_id},
            }

        user = self.fetcher.fetch_user(userid)
        if not user:
            return {
                "ok": False,
                "error": "USER_NOT_FOUND",
                "details": {"userid": userid, "signal_id": signal_id},
            }

        if UserTradeSchema.has_any_trade_for_signal(userid=userid, signal_id=signal_id):
            logger.debug(
                "TradeGenerator: signal already deployed; skipping | userid=%s signal_id=%s",
                userid,
                signal_id,
            )
            return {
                "ok": False,
                "error": "SIGNAL_ALREADY_DEPLOYED",
                "details": {"userid": userid, "signal_id": signal_id},
            }

        opposite_exit = _mark_opposite_family_for_exit_if_needed(userid=userid, signal=signal)
        if opposite_exit is not None:
            logger.info(
                "TradeGenerator: opposite signal marked active family for exit | userid=%s signal_id=%s symbol=%s details=%s",
                userid,
                signal_id,
                getattr(signal, "symbol", None),
                opposite_exit.get("details"),
            )
            return opposite_exit

        decision = TradeDecisionHelper.evaluate(
            user=user,
            signal=signal,
            mode=MODE_AUTO,
        )

        if not decision.allowed:
            _record_price_defer_if_needed(
                signal=signal,
                userid=userid,
                decision=decision,
            )
            _audit_trade_decision(
                userid=userid,
                signal=signal,
                decision=decision,
                action=decision.decision,
                reason_code=(decision.reasons[0] if decision.reasons else "not_allowed"),
            )
            return {
                "ok": False,
                "error": "TRADE_DECISION_NOT_ALLOWED",
                "details": decision.to_dict(),
            }

        try:
            result = TradeGenHelper.create_trades_from_signal(
                userid=userid,
                signal_id=signal_id,
                instrument_choice=instrument_choice,
                source=source,
            ) or {}
            trade_ids = [x for x in (result.get("trade_ids") or []) if x]
            _audit_trade_decision(
                userid=userid,
                signal=signal,
                decision=decision,
                action="ALLOW",
                reason_code="allowed",
                result=result,
            )
            if result.get("ok") and trade_ids:
                _persist_trade_entry_state(
                    signal=signal,
                    userid=userid,
                    state="DEPLOYED",
                    reason_code="TRADE_PACKAGE_CREATED",
                    decision_details=decision.to_dict(),
                )
                _audit_trade_created(
                    userid=userid,
                    signal=signal,
                    trade_ids=trade_ids,
                    result=result,
                )
            return result
        except Exception:
            logger.exception(
                "TradeGenerator.generate_for_user_signal failed | userid=%s signal_id=%s instrument_choice=%s",
                userid,
                signal_id,
                instrument_choice,
            )
            return {
                "ok": False,
                "error": "GENERATE_FOR_USER_SIGNAL_FAILED",
                "details": {
                    "userid": userid,
                    "signal_id": signal_id,
                    "instrument_choice": instrument_choice,
                    "source": source,
                },
            }

    def generate_user_trades(self, userid: Optional[str] = None) -> List[UserTradeSchema]:
        """
        Backend AUTOGEN path.

        If userid is provided:
        - generate only for that user, if AUTOGEN-eligible

        Else:
        - generate for all AUTOGEN-eligible users
        """
        open_signals = self.fetcher.fetch_open_signals(limit=1000)

        logger.info(
            "TradeGenerator: signals_open=%d userid=%s | gating delegated to TradeDecisionHelper",
            len(open_signals),
            userid or "ALL",
        )

        if userid:
            user = self.fetcher.fetch_user(userid)

            if not user:
                logger.warning("TradeGenerator: user not found | userid=%s", userid)
                return []

            if not _is_autogen_eligible_user(user):
                logger.info("TradeGenerator: user not AUTOGEN-eligible | userid=%s", userid)
                return []

            created = self._generate_for_user(
                user=user,
                signals=open_signals,
            )

            logger.info(
                "TradeGenerator summary | users_processed=1 signals_open=%d trades_created=%d",
                len(open_signals),
                len(created),
            )
            return created

        users = self.fetcher.fetch_autogen_users()
        logger.info("TradeGenerator: AUTOGEN-eligible users=%d", len(users))

        created_all: List[UserTradeSchema] = []

        for user in users:
            created_all.extend(
                self._generate_for_user(
                    user=user,
                    signals=open_signals,
                )
            )

        logger.info(
            "TradeGenerator summary | users_processed=%d signals_open=%d trades_created=%d",
            len(users),
            len(open_signals),
            len(created_all),
        )

        return created_all

    def _generate_for_user(
        self,
        *,
        user: UserSchema,
        signals: List[SignalSchema],
    ) -> List[UserTradeSchema]:
        userid = _safe_userid(user)
        created_rows: List[UserTradeSchema] = []

        signals_seen = 0
        signals_ok = 0
        signals_failed = 0

        candidate_ids = [
            str(getattr(signal, "signal_id", "") or "").strip()
            for signal in signals
            if str(getattr(signal, "signal_id", "") or "").strip()
        ]
        deployed_signal_ids = UserTradeSchema.fetch_deployed_signal_ids(
            userid=userid,
            signal_ids=candidate_ids,
        )

        for signal in signals:
            signal_id = str(getattr(signal, "signal_id", "") or "").strip()
            if not signal_id:
                continue

            signals_seen += 1

            if signal_id in deployed_signal_ids:
                logger.debug(
                    "TradeGenerator: deployed signal omitted | userid=%s signal_id=%s symbol=%s",
                    userid,
                    signal_id,
                    getattr(signal, "symbol", None),
                )
                continue

            try:
                opposite_exit = _mark_opposite_family_for_exit_if_needed(userid=userid, signal=signal)
                if opposite_exit is not None:
                    signals_failed += 1
                    logger.info(
                        "TradeGenerator: opposite signal marked active family for exit | userid=%s signal_id=%s symbol=%s details=%s",
                        userid,
                        signal_id,
                        getattr(signal, "symbol", None),
                        opposite_exit.get("details"),
                    )
                    continue

                decision = TradeDecisionHelper.evaluate(
                    user=user,
                    signal=signal,
                    mode=MODE_AUTO,
                )

                if not decision.allowed:
                    _record_price_defer_if_needed(
                        signal=signal,
                        userid=userid,
                        decision=decision,
                    )
                    _audit_trade_decision(
                        userid=userid,
                        signal=signal,
                        decision=decision,
                        action=decision.decision,
                        reason_code=(decision.reasons[0] if decision.reasons else "not_allowed"),
                    )
                    logger.info(
                        "TradeGenerator: decision skipped userid=%s signal_id=%s symbol=%s lifecycle=%s decision=%s reasons=%s warnings=%s",
                        userid,
                        signal_id,
                        getattr(signal, "symbol", None),
                        getattr(signal, "lifecycle", None),
                        decision.decision,
                        decision.reasons,
                        decision.warnings,
                    )
                    continue

                result = TradeGenHelper.create_trades_from_signal(
                    userid=userid,
                    signal_id=signal_id,
                    instrument_choice="MULTI",
                    source=AUTOGEN_SOURCE,
                ) or {}

                if not result.get("ok"):
                    signals_failed += 1
                    _audit_trade_decision(
                        userid=userid,
                        signal=signal,
                        decision=decision,
                        action="CREATE_FAILED",
                        reason_code=str(result.get("error") or "create_failed"),
                        result=result,
                    )
                    logger.info(
                        "TradeGenerator: skipped userid=%s signal_id=%s symbol=%s error=%s",
                        userid,
                        signal_id,
                        getattr(signal, "symbol", None),
                        result.get("error"),
                    )
                    continue

                signals_ok += 1
                deployed_signal_ids.add(signal_id)

                trade_ids = [x for x in (result.get("trade_ids") or []) if x]

                _audit_trade_decision(
                    userid=userid,
                    signal=signal,
                    decision=decision,
                    action="ALLOW",
                    reason_code="allowed",
                    result=result,
                )

                if trade_ids:
                    _persist_trade_entry_state(
                        signal=signal,
                        userid=userid,
                        state="DEPLOYED",
                        reason_code="TRADE_PACKAGE_CREATED",
                        decision_details=decision.to_dict(),
                    )
                    _audit_trade_created(
                        userid=userid,
                        signal=signal,
                        trade_ids=trade_ids,
                        result=result,
                    )

                for trade_id in trade_ids:
                    try:
                        trade = UserTradeSchema.fetch_user_trade_by_id(int(trade_id))
                        if trade:
                            created_rows.append(trade)
                    except Exception:
                        logger.exception(
                            "TradeGenerator: failed to fetch created trade | userid=%s trade_id=%s",
                            userid,
                            trade_id,
                        )

                logger.info(
                    "TradeGenerator: created userid=%s signal_id=%s symbol=%s created_count=%s trade_ids=%s",
                    userid,
                    signal_id,
                    getattr(signal, "symbol", None),
                    result.get("created_count"),
                    trade_ids,
                )

            except Exception:
                signals_failed += 1
                logger.exception(
                    "TradeGenerator: create_trades_from_signal failed | userid=%s signal_id=%s symbol=%s",
                    userid,
                    signal_id,
                    getattr(signal, "symbol", None),
                )

        logger.info(
            "TradeGenerator user summary | userid=%s signals_seen=%d signals_ok=%d signals_failed=%d trades_created=%d",
            userid,
            signals_seen,
            signals_ok,
            signals_failed,
            len(created_rows),
        )

        return created_rows