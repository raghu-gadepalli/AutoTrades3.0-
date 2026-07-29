# schemas/stock_opportunity.py
#
# DB/IO schema for the single stock_opportunities persistence table.
# SignalGenerator resolves the Auction lifecycle operation and calls the
# explicit persistence methods in this schema directly. Writes use the same
# simple session/commit pattern as the other DB-backed schemas.

from __future__ import annotations

from datetime import date, datetime
import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

from pydantic import BaseModel, Field

from database.database import get_trades_db
from enums.enums import SignalStatus
from models.trade_models import StockOpportunity as StockOpportunityORM
from schemas.signal import SignalSchema
from schemas.snapshot import SnapshotSchema
from services.auction_engine.episode_contracts import AuthoritativeSetupEventRoute
from services.auction_engine.setup_contracts import AuthoritativeSetupCandidate
from utils.datetime_utils import IST
from utils.json_utils import sanitize_json


logger = logging.getLogger(__name__)


STOCK_OPPORTUNITY_CONTRACT_VERSION = "STOCK_OPPORTUNITY_V1"

_JSON_FIELDS = {
    "transition_history",
    "candidate_interpretations",
    "authoritative_event_lineage",
    "latest_setup_evaluation",
    "latest_advisor_evaluation",
    "metadata_json",
}


def _to_ist_naive(ts: datetime) -> datetime:
    if not isinstance(ts, datetime):
        raise TypeError("Stock opportunity timestamp must be datetime")
    if ts.tzinfo is not None:
        ts = ts.astimezone(IST)
    return ts.replace(tzinfo=None)


def _number(value: Any) -> Decimal:
    return Decimal(str(value))


def _append_unique(
    existing: Any,
    item: Dict[str, Any],
    *,
    identity_key: str,
) -> List[Dict[str, Any]]:
    rows = existing if isinstance(existing, list) else []
    clean = [dict(row) for row in rows if isinstance(row, dict)]
    identity = item[identity_key]
    if not any(row.get(identity_key) == identity for row in clean):
        clean.append(sanitize_json(item))
    return clean


def _candidate_interpretation(
    candidate: AuthoritativeSetupCandidate,
    diagnostic: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    return sanitize_json({
        "candidate_id": candidate.candidate_id,
        "opportunity_key": candidate.opportunity_key,
        "snapshot_time": candidate.snapshot_time,
        "setup_family": candidate.setup_family.value,
        "setup_subtype": candidate.setup_subtype,
        "side": candidate.side.value,
        "source_event_id": candidate.source_event_id,
        "source_event_type": candidate.source_event_type.value,
        "source_episode_id": candidate.source_episode_id,
        "entry_price": candidate.entry_price,
        "stop_reference_price": candidate.stop_anchor_price,
        "stop_reference_source": candidate.stop_anchor_type,
        "target_reference_price": candidate.target_reference_price,
        "target_reference_source": candidate.target_basis,
        "reference_price": candidate.reference_price,
        "reference_source": candidate.reference_source,
        "risk_points": candidate.risk_points,
        "expected_move_points": candidate.expected_move_points,
        "expected_move_pct": candidate.expected_move_pct,
        "reward_risk": candidate.reward_risk,
        "valid_until": candidate.valid_until,
        "reason_codes": candidate.reason_codes,
        "evaluation": diagnostic,
    })


def _lineage_record(candidate: AuthoritativeSetupCandidate) -> Dict[str, Any]:
    return sanitize_json({
        "source_event_id": candidate.source_event_id,
        "source_event_type": candidate.source_event_type.value,
        "source_episode_id": candidate.source_episode_id,
        "setup_family": candidate.setup_family.value,
        "setup_subtype": candidate.setup_subtype,
        "side": candidate.side.value,
        "candidate_id": candidate.candidate_id,
        "candidate_opportunity_key": candidate.opportunity_key,
        "snapshot_time": candidate.snapshot_time,
    })


def _advisor_evaluation(
    diagnostic: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if not isinstance(diagnostic, dict) or diagnostic.get("advisor_action") in (None, ""):
        return None
    return sanitize_json({
        "action": diagnostic["advisor_action"],
        "reason_codes": diagnostic.get("advisor_reason_codes", ()),
        "outcome": diagnostic.get("outcome"),
    })


def _transition(
    *,
    transition_key: str,
    transition_time: datetime,
    state: str,
    reason: str,
    signal_id: str,
    event_id: str,
    event_type: str,
    episode_id: str,
    setup_family: str,
    side: str,
) -> Dict[str, Any]:
    return sanitize_json({
        "transition_key": transition_key,
        "transition_time": transition_time,
        "state": state,
        "reason": reason,
        "signal_id": signal_id,
        "source_event_id": event_id,
        "source_event_type": event_type,
        "source_episode_id": episode_id,
        "setup_family": setup_family,
        "side": side,
    })


class StockOpportunitySchema(BaseModel):
    model_config = {"from_attributes": True, "extra": "forbid"}

    id: Optional[int] = None

    opportunity_key: str
    candidate_id: str
    latest_candidate_id: str

    symbol: str
    equity_ref: str
    trading_day: date

    setup_family: str
    current_setup_family: str
    setup_subtype: str
    side: str

    source_event_id: str
    source_event_type: str
    source_episode_id: str
    boundary_event_key: str

    latest_event_id: str
    latest_event_type: str
    latest_episode_id: str

    lifecycle_state: str
    lifecycle_reason: str
    structural_result: str

    first_seen_time: datetime
    last_eval_time: datetime
    deployed_at: datetime
    progressed_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    invalidated_at: Optional[datetime] = None
    replaced_at: Optional[datetime] = None

    entry_price: Decimal
    reference_price: Decimal
    stop_reference_price: Decimal
    target_reference_price: Decimal

    signal_id: str
    replacement_opportunity_key: Optional[str] = None
    replaced_opportunity_key: Optional[str] = None

    transition_history: List[Dict[str, Any]] = Field(default_factory=list)
    candidate_interpretations: List[Dict[str, Any]] = Field(default_factory=list)
    authoritative_event_lineage: List[Dict[str, Any]] = Field(default_factory=list)
    latest_setup_evaluation: Optional[Dict[str, Any]] = None
    latest_advisor_evaluation: Optional[Dict[str, Any]] = None
    metadata_json: Optional[Dict[str, Any]] = None

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @staticmethod
    def create_opportunity(**values: Any) -> "StockOpportunitySchema":
        payload = StockOpportunitySchema(**values)
        orm_values = payload.model_dump(
            exclude={"id", "created_at", "updated_at"},
        )
        for field_name in _JSON_FIELDS:
            if field_name in orm_values and orm_values[field_name] is not None:
                orm_values[field_name] = sanitize_json(orm_values[field_name])

        with get_trades_db() as db:
            rec = StockOpportunityORM(**orm_values)
            db.add(rec)
            db.commit()
            db.refresh(rec)
            return StockOpportunitySchema.model_validate(rec)

    @staticmethod
    def fetch_by_opportunity_key(
        opportunity_key: str,
    ) -> Optional["StockOpportunitySchema"]:
        key = str(opportunity_key).strip()
        if not key:
            raise ValueError("opportunity_key is required")
        with get_trades_db() as db:
            rec = (
                db.query(StockOpportunityORM)
                .filter(StockOpportunityORM.opportunity_key == key)
                .one_or_none()
            )
        return StockOpportunitySchema.model_validate(rec) if rec else None

    @staticmethod
    def fetch_by_signal_id(signal_id: str) -> Optional["StockOpportunitySchema"]:
        key = str(signal_id).strip()
        if not key:
            raise ValueError("signal_id is required")
        with get_trades_db() as db:
            rec = (
                db.query(StockOpportunityORM)
                .filter(StockOpportunityORM.signal_id == key)
                .one_or_none()
            )
        return StockOpportunitySchema.model_validate(rec) if rec else None

    @staticmethod
    def update_opportunity(
        *,
        signal_id: str,
        update_data: Dict[str, Any],
    ) -> Optional["StockOpportunitySchema"]:
        key = str(signal_id).strip()
        if not key:
            raise ValueError("signal_id is required")
        if not isinstance(update_data, dict) or not update_data:
            raise ValueError("update_data must be a non-empty dictionary")

        allowed_fields = set(StockOpportunitySchema.model_fields) - {
            "id",
            "opportunity_key",
            "signal_id",
            "created_at",
            "updated_at",
        }
        unknown_fields = set(update_data) - allowed_fields
        if unknown_fields:
            raise ValueError(
                "Unsupported stock opportunity update fields: "
                f"{sorted(unknown_fields)}"
            )

        with get_trades_db() as db:
            rec = (
                db.query(StockOpportunityORM)
                .filter(StockOpportunityORM.signal_id == key)
                .one_or_none()
            )
            if rec is None:
                return None

            for field_name, value in update_data.items():
                if field_name in _JSON_FIELDS and value is not None:
                    value = sanitize_json(value)
                setattr(rec, field_name, value)

            db.commit()
            db.refresh(rec)
            return StockOpportunitySchema.model_validate(rec)

    @staticmethod
    def create_deployed_opportunity(
        *,
        snapshot: SnapshotSchema,
        equity_ref: str,
        candidate: AuthoritativeSetupCandidate,
        signal: SignalSchema,
        evaluation_diagnostic: Optional[Dict[str, Any]],
        replaced_opportunity_key: Optional[str] = None,
    ) -> StockOpportunitySchema:
        ts = _to_ist_naive(snapshot.snapshot_time)
        transition = _transition(
            transition_key=f"DEPLOYED:{candidate.source_event_id}:{signal.signal_id}",
            transition_time=ts,
            state="DEPLOYED",
            reason="EVENT_DRIVEN_SETUP_CREATED",
            signal_id=signal.signal_id,
            event_id=candidate.source_event_id,
            event_type=candidate.source_event_type.value,
            episode_id=candidate.source_episode_id,
            setup_family=candidate.setup_family.value,
            side=candidate.side.value,
        )
        interpretation = _candidate_interpretation(candidate, evaluation_diagnostic)
        lineage = _lineage_record(candidate)
        advisor = _advisor_evaluation(evaluation_diagnostic)

        try:
            existing = StockOpportunitySchema.fetch_by_opportunity_key(
                candidate.opportunity_key
            )
            if existing is None:
                return StockOpportunitySchema.create_opportunity(
                    opportunity_key=candidate.opportunity_key,
                    candidate_id=candidate.candidate_id,
                    latest_candidate_id=candidate.candidate_id,
                    symbol=snapshot.symbol.strip().upper(),
                    equity_ref=equity_ref.strip().upper(),
                    trading_day=candidate.trading_day,
                    setup_family=candidate.setup_family.value,
                    current_setup_family=candidate.setup_family.value,
                    setup_subtype=candidate.setup_subtype,
                    side=candidate.side.value,
                    source_event_id=candidate.source_event_id,
                    source_event_type=candidate.source_event_type.value,
                    source_episode_id=candidate.source_episode_id,
                    boundary_event_key=candidate.source_event_id,
                    latest_event_id=candidate.source_event_id,
                    latest_event_type=candidate.source_event_type.value,
                    latest_episode_id=candidate.source_episode_id,
                    lifecycle_state="DEPLOYED",
                    lifecycle_reason="EVENT_DRIVEN_SETUP_CREATED",
                    structural_result=candidate.structural_result.value,
                    first_seen_time=ts,
                    last_eval_time=ts,
                    deployed_at=ts,
                    entry_price=_number(candidate.entry_price),
                    reference_price=_number(candidate.reference_price),
                    stop_reference_price=_number(candidate.stop_anchor_price),
                    target_reference_price=_number(candidate.target_reference_price),
                    signal_id=signal.signal_id,
                    replacement_opportunity_key=None,
                    replaced_opportunity_key=replaced_opportunity_key,
                    transition_history=[transition],
                    candidate_interpretations=[interpretation],
                    authoritative_event_lineage=[lineage],
                    latest_setup_evaluation=sanitize_json(evaluation_diagnostic),
                    latest_advisor_evaluation=advisor,
                    metadata_json={
                        "contract_version": STOCK_OPPORTUNITY_CONTRACT_VERSION,
                        "source": "EVENT_DRIVEN_SIGNAL_GENERATOR",
                        "scope": "DEPLOYED_OPPORTUNITIES_ONLY",
                        "write_mode": "SEQUENTIAL_COMMIT",
                    },
                )

            if existing.signal_id != signal.signal_id:
                raise ValueError(
                    "Opportunity key already belongs to a different signal: "
                    f"{candidate.opportunity_key}"
                )

            updated = StockOpportunitySchema.update_opportunity(
                signal_id=signal.signal_id,
                update_data={
                    "lifecycle_state": "DEPLOYED",
                    "lifecycle_reason": "EVENT_DRIVEN_SETUP_CREATED",
                    "last_eval_time": ts,
                    "latest_candidate_id": candidate.candidate_id,
                    "latest_event_id": candidate.source_event_id,
                    "latest_event_type": candidate.source_event_type.value,
                    "latest_episode_id": candidate.source_episode_id,
                    "transition_history": _append_unique(
                        existing.transition_history,
                        transition,
                        identity_key="transition_key",
                    ),
                    "candidate_interpretations": _append_unique(
                        existing.candidate_interpretations,
                        interpretation,
                        identity_key="candidate_id",
                    ),
                    "authoritative_event_lineage": _append_unique(
                        existing.authoritative_event_lineage,
                        lineage,
                        identity_key="source_event_id",
                    ),
                    "latest_setup_evaluation": sanitize_json(evaluation_diagnostic),
                    "latest_advisor_evaluation": advisor,
                    "replaced_opportunity_key": replaced_opportunity_key,
                },
            )
            if updated is None:
                raise RuntimeError(
                    f"Missing deployed opportunity for signal {signal.signal_id}"
                )
            return updated
        except Exception:
            logger.exception(
                "stock opportunity deploy failed | opportunity_key=%s signal_id=%s",
                candidate.opportunity_key,
                signal.signal_id,
            )
            raise

    @staticmethod
    def progress_opportunity(
        *,
        snapshot: SnapshotSchema,
        signal: SignalSchema,
        candidate: AuthoritativeSetupCandidate,
        evaluation_diagnostic: Optional[Dict[str, Any]],
    ) -> StockOpportunitySchema:
        ts = _to_ist_naive(snapshot.snapshot_time)
        transition = _transition(
            transition_key=f"PROGRESSED:{candidate.source_event_id}:{signal.signal_id}",
            transition_time=ts,
            state="PROGRESSED",
            reason="AUTHORITATIVE_OPPORTUNITY_PROGRESSED",
            signal_id=signal.signal_id,
            event_id=candidate.source_event_id,
            event_type=candidate.source_event_type.value,
            episode_id=candidate.source_episode_id,
            setup_family=candidate.setup_family.value,
            side=candidate.side.value,
        )
        interpretation = _candidate_interpretation(candidate, evaluation_diagnostic)
        lineage = _lineage_record(candidate)

        try:
            existing = StockOpportunitySchema.fetch_by_signal_id(signal.signal_id)
            if existing is None:
                raise RuntimeError(
                    f"Missing deployed opportunity for signal {signal.signal_id}"
                )
            update_data: Dict[str, Any] = {
                "latest_candidate_id": candidate.candidate_id,
                "current_setup_family": candidate.setup_family.value,
                "latest_event_id": candidate.source_event_id,
                "latest_event_type": candidate.source_event_type.value,
                "latest_episode_id": candidate.source_episode_id,
                "lifecycle_state": "PROGRESSED",
                "lifecycle_reason": "AUTHORITATIVE_OPPORTUNITY_PROGRESSED",
                "last_eval_time": ts,
                "progressed_at": ts,
                "transition_history": _append_unique(
                    existing.transition_history,
                    transition,
                    identity_key="transition_key",
                ),
                "candidate_interpretations": _append_unique(
                    existing.candidate_interpretations,
                    interpretation,
                    identity_key="candidate_id",
                ),
                "authoritative_event_lineage": _append_unique(
                    existing.authoritative_event_lineage,
                    lineage,
                    identity_key="source_event_id",
                ),
                "latest_setup_evaluation": sanitize_json(evaluation_diagnostic),
            }
            advisor = _advisor_evaluation(evaluation_diagnostic)
            if advisor is not None:
                update_data["latest_advisor_evaluation"] = advisor

            updated = StockOpportunitySchema.update_opportunity(
                signal_id=signal.signal_id,
                update_data=update_data,
            )
            if updated is None:
                raise RuntimeError(
                    f"Missing deployed opportunity for signal {signal.signal_id}"
                )
            return updated
        except Exception:
            logger.exception(
                "stock opportunity progress failed | signal_id=%s event_id=%s",
                signal.signal_id,
                candidate.source_event_id,
            )
            raise

    @staticmethod
    def complete_opportunity(
        *,
        snapshot: SnapshotSchema,
        signal: SignalSchema,
        route: AuthoritativeSetupEventRoute,
    ) -> StockOpportunitySchema:
        ts = _to_ist_naive(snapshot.snapshot_time)
        reason = f"{route.source_event_type.value}_OPPORTUNITY_WINDOW_COMPLETED"
        transition = _transition(
            transition_key=f"COMPLETED:{route.source_event_id}:{signal.signal_id}",
            transition_time=ts,
            state="COMPLETED",
            reason=reason,
            signal_id=signal.signal_id,
            event_id=route.source_event_id,
            event_type=route.source_event_type.value,
            episode_id=route.source_episode_id,
            setup_family=route.setup_family.value,
            side=signal.side.value,
        )

        try:
            existing = StockOpportunitySchema.fetch_by_signal_id(signal.signal_id)
            if existing is None:
                raise RuntimeError(
                    f"Missing deployed opportunity for signal {signal.signal_id}"
                )
            updated = StockOpportunitySchema.update_opportunity(
                signal_id=signal.signal_id,
                update_data={
                    "latest_event_id": route.source_event_id,
                    "latest_event_type": route.source_event_type.value,
                    "latest_episode_id": route.source_episode_id,
                    "lifecycle_state": "COMPLETED",
                    "lifecycle_reason": reason,
                    "last_eval_time": ts,
                    "completed_at": existing.completed_at or ts,
                    "transition_history": _append_unique(
                        existing.transition_history,
                        transition,
                        identity_key="transition_key",
                    ),
                },
            )
            if updated is None:
                raise RuntimeError(
                    f"Missing deployed opportunity for signal {signal.signal_id}"
                )
            return updated
        except Exception:
            logger.exception(
                "stock opportunity completion failed | signal_id=%s event_id=%s",
                signal.signal_id,
                route.source_event_id,
            )
            raise

    @staticmethod
    def terminate_opportunity(
        *,
        snapshot: SnapshotSchema,
        signal: SignalSchema,
        status: SignalStatus,
        reason: str,
        terminal_route: Optional[AuthoritativeSetupEventRoute] = None,
        replacement_candidate: Optional[AuthoritativeSetupCandidate] = None,
    ) -> StockOpportunitySchema:
        if status not in {SignalStatus.INVALIDATED, SignalStatus.REPLACED}:
            raise ValueError(
                "StockOpportunity terminate supports INVALIDATED or REPLACED only"
            )

        ts = _to_ist_naive(snapshot.snapshot_time)
        state = status.value
        if terminal_route is not None:
            event_id = terminal_route.source_event_id
            event_type = terminal_route.source_event_type.value
            episode_id = terminal_route.source_episode_id
            setup_family = terminal_route.setup_family.value
        elif replacement_candidate is not None:
            event_id = replacement_candidate.source_event_id
            event_type = replacement_candidate.source_event_type.value
            episode_id = replacement_candidate.source_episode_id
            setup_family = replacement_candidate.setup_family.value
        else:
            raise ValueError(
                "Opportunity termination requires route or replacement candidate"
            )

        transition = _transition(
            transition_key=f"{state}:{event_id}:{signal.signal_id}",
            transition_time=ts,
            state=state,
            reason=reason,
            signal_id=signal.signal_id,
            event_id=event_id,
            event_type=event_type,
            episode_id=episode_id,
            setup_family=setup_family,
            side=signal.side.value,
        )

        try:
            existing = StockOpportunitySchema.fetch_by_signal_id(signal.signal_id)
            if existing is None:
                raise RuntimeError(
                    f"Missing deployed opportunity for signal {signal.signal_id}"
                )
            update_data: Dict[str, Any] = {
                "latest_event_id": event_id,
                "latest_event_type": event_type,
                "latest_episode_id": episode_id,
                "lifecycle_state": state,
                "lifecycle_reason": reason,
                "last_eval_time": ts,
                "transition_history": _append_unique(
                    existing.transition_history,
                    transition,
                    identity_key="transition_key",
                ),
            }
            if status is SignalStatus.INVALIDATED:
                update_data["invalidated_at"] = ts
            else:
                if replacement_candidate is None:
                    raise ValueError(
                        "REPLACED opportunity requires replacement_candidate"
                    )
                update_data["replaced_at"] = ts
                update_data["replacement_opportunity_key"] = (
                    replacement_candidate.opportunity_key
                )

            updated = StockOpportunitySchema.update_opportunity(
                signal_id=signal.signal_id,
                update_data=update_data,
            )
            if updated is None:
                raise RuntimeError(
                    f"Missing deployed opportunity for signal {signal.signal_id}"
                )
            return updated
        except Exception:
            logger.exception(
                "stock opportunity termination failed | signal_id=%s state=%s",
                signal.signal_id,
                state,
            )
            raise

    @staticmethod
    def delete_for_replay(*, symbols: Sequence[str], trading_day: date) -> int:
        clean_symbols = [str(symbol).strip().upper() for symbol in symbols]
        if not clean_symbols:
            return 0
        with get_trades_db() as db:
            deleted = int(
                db.query(StockOpportunityORM)
                .filter(
                    StockOpportunityORM.symbol.in_(clean_symbols),
                    StockOpportunityORM.trading_day == trading_day,
                )
                .delete(synchronize_session=False)
            )
            db.commit()
        return deleted

    @staticmethod
    def list_for_replay(
        *,
        symbols: Sequence[str],
        trading_day: date,
    ) -> List["StockOpportunitySchema"]:
        clean_symbols = [str(symbol).strip().upper() for symbol in symbols]
        if not clean_symbols:
            return []
        with get_trades_db() as db:
            rows = (
                db.query(StockOpportunityORM)
                .filter(
                    StockOpportunityORM.symbol.in_(clean_symbols),
                    StockOpportunityORM.trading_day == trading_day,
                )
                .order_by(
                    StockOpportunityORM.first_seen_time.asc(),
                    StockOpportunityORM.id.asc(),
                )
                .all()
            )
        return [StockOpportunitySchema.model_validate(row) for row in rows]


__all__ = [
    "STOCK_OPPORTUNITY_CONTRACT_VERSION",
    "StockOpportunitySchema",
    "_append_unique",
]
