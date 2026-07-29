#!/usr/bin/env python3
"""Strict event-driven Auction signal persistence.

Signal creation is possible only from an authoritative Auction event routed to
one setup family, structurally permitted, approved by the setup-quality engine,
and selected by the common setup manager.  There is no legacy Auction decision,
setup rediscovery, compatibility adapter, or fallback path.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import logging
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

from configs.signal_config import SIGNAL_CONFIG
from enums.auction_engine import (
    AdvisorAction,
    SetupEventAction,
    SetupFamily,
    TradeSide,
)
from enums.enums import LifecycleStage, SignalSide, SignalStatus
from schemas.signal import SignalSchema
from schemas.stock_opportunity import StockOpportunitySchema
from schemas.snapshot import SnapshotSchema
from schemas.symbol import SymbolSchema
from services.auction_engine.event_driven_setup_engine import (
    EventDrivenSetupEngine,
    EventDrivenSetupManager,
)
from services.auction_engine.setup_contracts import AuthoritativeSetupCandidate
from services.auction_engine.setup_event_router import AuthoritativeSetupEventRouter
from services.signals.signal_metrics import calculate_signal_metrics
from services.signals.stock_advisor import StockAdvisor
from utils.json_utils import sanitize_json

logger = logging.getLogger(__name__)

DEFAULT_LIFECYCLE = SIGNAL_CONFIG.default_lifecycle.strip().upper()
_SIGNAL_ID_NAMESPACE = uuid.UUID("66bf75a2-909b-4c44-b5db-b9dd2eff598e")
_DOWNSTREAM_CONTRACT_VERSION = "AUCTION_SIGNAL_DOWNSTREAM_V2"


@dataclass(frozen=True)
class AuctionSignalIdentity:
    opportunity_key: str
    candidate_id: str
    boundary_event_key: str
    setup_family: str
    setup_subtype: str
    side: str
    source_event_id: str
    source_event_type: str
    source_episode_id: str
    created_snapshot_time: datetime


class SignalFetcher:
    def fetch_symbol(self, symbol: str) -> Optional[SymbolSchema]:
        return SymbolSchema.fetch_symbol_strict(symbol)

    def fetch_active_signal(self, equity_ref: str, lifecycle: str) -> Optional[SignalSchema]:
        return SignalSchema.fetch_active_signal_strict(equity_ref, lifecycle)

    def fetch_signal_by_id(self, signal_id: str) -> Optional[SignalSchema]:
        return SignalSchema.fetch_by_signal_id_strict(signal_id)


class SignalPersister:
    def create(
        self,
        *,
        snapshot: SnapshotSchema,
        equity_ref: str,
        lifecycle: str,
        candidate: AuthoritativeSetupCandidate,
        meta_json: Dict[str, Any],
        criteria_json: Dict[str, Any],
        analytics: Dict[str, Any],
        evaluation_diagnostic: Optional[Dict[str, Any]],
        replaced_opportunity_key: Optional[str] = None,
    ) -> SignalSchema:
        signal = SignalSchema.create_signal(
            signal_id=_signal_id(candidate.opportunity_key),
            equity_ref=equity_ref,
            symbol=snapshot.symbol,
            lifecycle=lifecycle,
            setup=candidate.setup_family.value,
            side=SignalSide.from_string(candidate.side.value),
            stage=LifecycleStage.ACTIVE,
            status=SignalStatus.OPEN,
            status_reason="EVENT_DRIVEN_SETUP_CREATED",
            last_eval_time=snapshot.snapshot_time,
            last_snapshot_time=snapshot.snapshot_time,
            criteria_json=criteria_json,
            snapshot_json=_snapshot_json(snapshot),
            meta_json=meta_json,
            last_price=_decimal(snapshot.close),
            ltp=_decimal_optional(snapshot.ltp),
            ltp_time=snapshot.ltp_time,
            **analytics,
        )
        StockOpportunitySchema.create_deployed_opportunity(
            snapshot=snapshot,
            equity_ref=equity_ref,
            candidate=candidate,
            signal=signal,
            evaluation_diagnostic=evaluation_diagnostic,
            replaced_opportunity_key=replaced_opportunity_key,
        )
        return signal

    def update(
        self,
        *,
        signal: SignalSchema,
        snapshot: SnapshotSchema,
        stage: LifecycleStage,
        reason: str,
        meta_json: Dict[str, Any],
        criteria_json: Dict[str, Any],
        analytics: Dict[str, Any],
        progression_candidate: Optional[AuthoritativeSetupCandidate] = None,
        evaluation_diagnostic: Optional[Dict[str, Any]] = None,
    ) -> SignalSchema:
        persisted = SignalSchema.update_signal(
            signal_id=signal.signal_id,
            stage=stage,
            status=SignalStatus.OPEN,
            setup=signal.setup,
            status_reason=reason,
            last_eval_time=snapshot.snapshot_time,
            last_snapshot_time=snapshot.snapshot_time,
            criteria_json=criteria_json,
            snapshot_json=_snapshot_json(snapshot),
            meta_json=meta_json,
            last_price=_decimal(snapshot.close),
            ltp=_decimal_optional(snapshot.ltp),
            ltp_time=snapshot.ltp_time,
            **analytics,
        )
        if persisted is None:
            raise RuntimeError(f"Signal update returned no row: {signal.signal_id}")
        if progression_candidate is not None:
            StockOpportunitySchema.progress_opportunity(
                snapshot=snapshot,
                signal=persisted,
                candidate=progression_candidate,
                evaluation_diagnostic=evaluation_diagnostic,
            )
        return persisted

    def close(
        self,
        *,
        signal: SignalSchema,
        snapshot: SnapshotSchema,
        status: SignalStatus,
        reason: str,
        meta_json: Dict[str, Any],
        criteria_json: Dict[str, Any],
        analytics: Dict[str, Any],
        terminal_route: Optional[Any] = None,
        replacement_candidate: Optional[AuthoritativeSetupCandidate] = None,
    ) -> SignalSchema:
        persisted = SignalSchema.close_signal(
            signal_id=signal.signal_id,
            stage=LifecycleStage.FORCE_EXIT,
            status=status,
            setup=signal.setup,
            reason=reason,
            ts=snapshot.snapshot_time,
            last_eval_time=snapshot.snapshot_time,
            last_snapshot_time=snapshot.snapshot_time,
            criteria_json=criteria_json,
            snapshot_json=_snapshot_json(snapshot),
            meta_json=meta_json,
            last_price=_decimal(snapshot.close),
            ltp=_decimal_optional(snapshot.ltp),
            ltp_time=snapshot.ltp_time,
            **analytics,
        )
        if persisted is None:
            raise RuntimeError(f"Signal close returned no row: {signal.signal_id}")
        StockOpportunitySchema.terminate_opportunity(
            snapshot=snapshot,
            signal=persisted,
            status=status,
            reason=reason,
            terminal_route=terminal_route,
            replacement_candidate=replacement_candidate,
        )
        return persisted

    def complete_opportunity(
        self,
        *,
        signal: SignalSchema,
        snapshot: SnapshotSchema,
        route: Any,
    ) -> None:
        StockOpportunitySchema.complete_opportunity(
            snapshot=snapshot,
            signal=signal,
            route=route,
        )


class SignalAssembler:
    """Translate one authoritative Auction snapshot into signal writes."""

    def __init__(
        self,
        *,
        fetcher: Optional[SignalFetcher] = None,
        persister: Optional[SignalPersister] = None,
        advisor: Optional[StockAdvisor] = None,
        router: Optional[AuthoritativeSetupEventRouter] = None,
        setup_engine: Optional[EventDrivenSetupEngine] = None,
        setup_manager: Optional[EventDrivenSetupManager] = None,
    ) -> None:
        self.lifecycle = DEFAULT_LIFECYCLE
        self.fetcher = fetcher or SignalFetcher()
        self.persister = persister or SignalPersister()
        self.advisor = advisor or StockAdvisor()
        self.router = router or AuthoritativeSetupEventRouter()
        self.setup_engine = setup_engine or EventDrivenSetupEngine()
        self.setup_manager = setup_manager or EventDrivenSetupManager()
        self.last_evaluation_diagnostics: List[Dict[str, Any]] = []

    def assemble(self, snapshot: SnapshotSchema) -> List[Tuple[str, SignalSchema]]:
        self.last_evaluation_diagnostics = []
        if not isinstance(snapshot, SnapshotSchema):
            raise TypeError("SignalAssembler requires SnapshotSchema")
        if snapshot.auction.status != "OK" or snapshot.auction.lifecycle is None:
            raise ValueError("Signal generation requires authoritative Auction lifecycle")

        symbol = snapshot.symbol.strip().upper()
        symbol_row = self.fetcher.fetch_symbol(symbol)
        if symbol_row is None:
            logger.info("SIG_SKIP | %s | SYMBOL_RECORD_MISSING", symbol)
            return []
        if not bool(symbol_row.active) or not bool(symbol_row.generate_signals):
            logger.info("SIG_SKIP | %s | SYMBOL_NOT_SIGNAL_ENABLED", symbol)
            return []
        if not bool(snapshot.gen_signals):
            logger.info("SIG_SKIP | %s | SNAPSHOT_SIGNAL_GENERATION_DISABLED", symbol)
            return []
        equity_ref = str(symbol_row.equity_ref or symbol_row.symbol).strip().upper()
        active = self.fetcher.fetch_active_signal(equity_ref, self.lifecycle)

        routes = self.router.route(snapshot.auction.lifecycle)
        evaluations = self.setup_engine.evaluate(snapshot, routes)
        manager = self.setup_manager.select(snapshot, evaluations)
        self.last_evaluation_diagnostics = _evaluation_diagnostics(evaluations, manager)
        selected = manager.selected_candidate
        events: List[Tuple[str, SignalSchema]] = []

        terminal = self._terminal_action(active, routes)
        if active is not None and terminal is not None:
            status, reason, terminal_route = terminal
            identity = _signal_identity(active)
            stage = LifecycleStage.FORCE_EXIT
            meta = _updated_meta(
                signal=active,
                snapshot=snapshot,
                identity=identity,
                stage=stage,
                status=status,
                signal_action="CLOSE",
                reason=reason,
                auction_action="EVENT_INVALIDATE" if status is SignalStatus.INVALIDATED else "EVENT_CLOSE",
            )
            analytics = _analytics(active, snapshot)
            persisted = self.persister.close(
                signal=active,
                snapshot=snapshot,
                status=status,
                reason=reason,
                meta_json=meta,
                criteria_json=_criteria_json(snapshot, evaluations, manager),
                analytics=analytics,
                terminal_route=terminal_route,
            )
            events.append(("CLOSE", persisted))
            active = None

        if active is not None:
            active_identity = _signal_identity(active)
            progression_pending = (
                selected is not None
                and _same_authoritative_lineage(active_identity, selected)
            )
            for route in routes:
                if route.action is not SetupEventAction.CLOSE:
                    continue
                if route.setup_family.value != active_identity.setup_family:
                    continue
                if route.source_episode_id != active_identity.source_episode_id:
                    continue
                if progression_pending:
                    continue
                self.persister.complete_opportunity(
                    signal=active,
                    snapshot=snapshot,
                    route=route,
                )

        if selected is not None:
            replaces_active = active is not None
            if active is not None:
                active_identity = _signal_identity(active)
                if active_identity.opportunity_key == selected.opportunity_key:
                    _mark_evaluation_outcome(
                        self.last_evaluation_diagnostics,
                        selected.candidate_id,
                        outcome="SAME_OPPORTUNITY_UPDATE",
                    )
                    meta = _updated_meta(
                        signal=active,
                        snapshot=snapshot,
                        identity=active_identity,
                        stage=LifecycleStage.ACTIVE,
                        status=SignalStatus.OPEN,
                        signal_action="UPDATE",
                        reason="SAME_AUTHORITATIVE_OPPORTUNITY",
                        auction_action="EVENT_REEVALUATED",
                    )
                    persisted = self.persister.update(
                        signal=active,
                        snapshot=snapshot,
                        stage=LifecycleStage.ACTIVE,
                        reason="SAME_AUTHORITATIVE_OPPORTUNITY",
                        meta_json=meta,
                        criteria_json=_criteria_json(snapshot, evaluations, manager),
                        analytics=_analytics(active, snapshot),
                    )
                    events.append(("UPDATE", persisted))
                    return events

                if _same_authoritative_lineage(active_identity, selected):
                    if _candidate_event_consumed(active, selected):
                        _mark_evaluation_outcome(
                            self.last_evaluation_diagnostics,
                            selected.candidate_id,
                            outcome="AUTHORITATIVE_EVENT_ALREADY_IN_LINEAGE",
                        )
                        selected = None
                    else:
                        _mark_evaluation_outcome(
                            self.last_evaluation_diagnostics,
                            selected.candidate_id,
                            outcome="AUTHORITATIVE_PROGRESSION_UPDATE",
                        )
                        meta = _updated_meta(
                            signal=active,
                            snapshot=snapshot,
                            identity=active_identity,
                            stage=LifecycleStage.EXPAND,
                            status=SignalStatus.OPEN,
                            signal_action="UPDATE",
                            reason="AUTHORITATIVE_OPPORTUNITY_PROGRESSED",
                            auction_action="EVENT_PROGRESS",
                            progression_candidate=selected,
                        )
                        persisted = self.persister.update(
                            signal=active,
                            snapshot=snapshot,
                            stage=LifecycleStage.EXPAND,
                            reason="AUTHORITATIVE_OPPORTUNITY_PROGRESSED",
                            meta_json=meta,
                            criteria_json=_criteria_json(snapshot, evaluations, manager),
                            analytics=_analytics(active, snapshot),
                            progression_candidate=selected,
                            evaluation_diagnostic=_diagnostic_for_candidate(
                                self.last_evaluation_diagnostics,
                                selected.candidate_id,
                            ),
                        )
                        events.append(("UPDATE", persisted))
                        return events

            if selected is not None:
                advisor = self.advisor.evaluate_authoritative(snapshot, selected)
                if advisor.action is AdvisorAction.BLOCK:
                    _mark_evaluation_outcome(
                        self.last_evaluation_diagnostics,
                        selected.candidate_id,
                        outcome="ADVISOR_BLOCK",
                        advisor_action=advisor.action.value,
                        advisor_reason_codes=advisor.reason_codes,
                    )
                    logger.info(
                        "SIG_BLOCK | %s @ %s | candidate=%s reasons=%s",
                        symbol,
                        snapshot.snapshot_time,
                        selected.candidate_id,
                        advisor.reason_codes,
                    )
                    selected = None
                elif advisor.action not in {AdvisorAction.ALLOW, AdvisorAction.WATCH}:
                    raise ValueError(
                        f"Unsupported deployment Advisor action: {advisor.action.value}"
                    )
                else:
                    _mark_evaluation_outcome(
                        self.last_evaluation_diagnostics,
                        selected.candidate_id,
                        outcome="ADVISOR_PASSED",
                        advisor_action=advisor.action.value,
                        advisor_reason_codes=advisor.reason_codes,
                    )

        if selected is not None:
            deterministic_signal_id = _signal_id(selected.opportunity_key)
            existing_event_signal = self.fetcher.fetch_signal_by_id(
                deterministic_signal_id
            )
            if existing_event_signal is not None:
                _mark_evaluation_outcome(
                    self.last_evaluation_diagnostics,
                    selected.candidate_id,
                    outcome="AUTHORITATIVE_EVENT_ALREADY_CONSUMED",
                )
                logger.info(
                    "SIG_NO_ACTION | %s @ %s | source_event=%s "
                    "reason=AUTHORITATIVE_EVENT_ALREADY_CONSUMED",
                    symbol,
                    snapshot.snapshot_time,
                    selected.source_event_id,
                )
                selected = None

        if selected is not None:
            replaced_opportunity_key: Optional[str] = None
            if active is not None:
                active_identity = _signal_identity(active)
                close_meta = _updated_meta(
                    signal=active,
                    snapshot=snapshot,
                    identity=active_identity,
                    stage=LifecycleStage.FORCE_EXIT,
                    status=SignalStatus.REPLACED,
                    signal_action="REPLACE",
                    reason="REPLACED_BY_NEW_AUTHORITATIVE_OPPORTUNITY",
                    auction_action="EVENT_REPLACE",
                )
                replaced = self.persister.close(
                    signal=active,
                    snapshot=snapshot,
                    status=SignalStatus.REPLACED,
                    reason="REPLACED_BY_NEW_AUTHORITATIVE_OPPORTUNITY",
                    meta_json=close_meta,
                    criteria_json=_criteria_json(snapshot, evaluations, manager),
                    analytics=_analytics(active, snapshot),
                    replacement_candidate=selected,
                )
                events.append(("REPLACE", replaced))
                replaced_opportunity_key = active_identity.opportunity_key
                active = None

            _mark_evaluation_outcome(
                self.last_evaluation_diagnostics,
                selected.candidate_id,
                outcome=("CREATED_AFTER_REPLACEMENT" if replaces_active else "CREATED"),
            )
            meta = _candidate_meta(
                snapshot=snapshot,
                candidate=selected,
                stage=LifecycleStage.ACTIVE,
                status=SignalStatus.OPEN,
                signal_action="CREATE",
                reason="EVENT_DRIVEN_SETUP_CREATED",
            )
            persisted = self.persister.create(
                snapshot=snapshot,
                equity_ref=equity_ref,
                lifecycle=self.lifecycle,
                candidate=selected,
                meta_json=meta,
                criteria_json=_criteria_json(snapshot, evaluations, manager),
                analytics=calculate_signal_metrics(
                    existing_signal=None,
                    side=SignalSide.from_string(selected.side.value),
                    current_price=snapshot.close,
                    current_time=snapshot.snapshot_time,
                ),
                evaluation_diagnostic=_diagnostic_for_candidate(
                    self.last_evaluation_diagnostics,
                    selected.candidate_id,
                ),
                replaced_opportunity_key=replaced_opportunity_key,
            )
            events.append(("CREATE", persisted))
            return events

        if active is not None:
            identity = _signal_identity(active)
            stage, reason = _operational_posture(snapshot, identity)
            meta = _updated_meta(
                signal=active,
                snapshot=snapshot,
                identity=identity,
                stage=stage,
                status=SignalStatus.OPEN,
                signal_action="HOLD",
                reason=reason,
                auction_action="NO_CREATION_EVENT",
            )
            persisted = self.persister.update(
                signal=active,
                snapshot=snapshot,
                stage=stage,
                reason=reason,
                meta_json=meta,
                criteria_json=_criteria_json(snapshot, evaluations, manager),
                analytics=_analytics(active, snapshot),
            )
            events.append(("HOLD", persisted))
        return events

    @staticmethod
    def _terminal_action(
        active: Optional[SignalSchema],
        routes: Sequence[Any],
    ) -> Optional[Tuple[SignalStatus, str, Any]]:
        if active is None:
            return None
        identity = _signal_identity(active)
        # CLOSE ends only the setup/opportunity creation window. It must not
        # force-exit a deployed signal; only explicit INVALIDATE routes can.
        for route in routes:
            if route.action is not SetupEventAction.INVALIDATE:
                continue
            if route.setup_family.value != identity.setup_family:
                continue
            same_episode = route.source_episode_id == identity.source_episode_id
            opposite_direction = _route_direction_opposes_signal(route, identity)
            if not same_episode and not opposite_direction:
                continue
            suffix = (
                "INVALIDATED_SETUP"
                if same_episode
                else "INVALIDATED_OPPOSITE_ACTIVE_SIGNAL"
            )
            return (
                SignalStatus.INVALIDATED,
                f"{route.source_event_type.value}_{suffix}",
                route,
            )
        return None


class SignalGenerator:
    def __init__(self, snapshot: SnapshotSchema):
        if not isinstance(snapshot, SnapshotSchema):
            raise TypeError("SignalGenerator requires SnapshotSchema")
        self.snapshot = snapshot
        self.assembler = SignalAssembler()

    def generate_events(self) -> List[Tuple[str, SignalSchema]]:
        return self.assembler.assemble(self.snapshot)

    def generate_signal(self) -> Optional[str]:
        events = self.generate_events()
        return events[-1][0] if events else None

    def generate(self) -> Optional[str]:
        return self.generate_signal()


def _route_direction_opposes_signal(
    route: Any,
    identity: AuctionSignalIdentity,
) -> bool:
    if route.direction.value == "UP":
        return identity.side == "SELL"
    if route.direction.value == "DOWN":
        return identity.side == "BUY"
    return False


def _evaluation_diagnostics(
    evaluations: Sequence[Any],
    manager: Any,
) -> List[Dict[str, Any]]:
    selected_id = (
        manager.selected_candidate.candidate_id
        if manager.selected_candidate is not None
        else None
    )
    supporting_ids = set(manager.supporting_candidate_ids)
    deferred_ids = set(manager.deferred_candidate_ids)
    records: List[Dict[str, Any]] = []
    for evaluation in evaluations:
        candidate_id = (
            evaluation.candidate.candidate_id
            if evaluation.candidate is not None
            else None
        )
        if not evaluation.approved:
            outcome = (
                "STRUCTURAL_PERMISSION_BLOCKED"
                if evaluation.structural_result.value != "PERMIT"
                else "SETUP_QUALITY_REJECTED"
            )
        elif candidate_id == selected_id:
            outcome = "MANAGER_SELECTED"
        elif candidate_id in supporting_ids:
            outcome = "MANAGER_SUPPORTING"
        elif candidate_id in deferred_ids:
            outcome = "MANAGER_DEFERRED"
        else:
            outcome = "MANAGER_NOT_SELECTED"
        records.append({
            "source_event_id": evaluation.source_event_id,
            "source_event_type": evaluation.source_event_type.value,
            "source_episode_id": evaluation.source_episode_id,
            "setup_family": evaluation.setup_family.value,
            "side": evaluation.side.value,
            "structural_result": evaluation.structural_result.value,
            "approved": bool(evaluation.approved),
            "candidate_id": candidate_id,
            "blockers": tuple(evaluation.blockers),
            "reason_codes": tuple(evaluation.reason_codes),
            "manager_reason_codes": tuple(manager.reason_codes),
            "advisor_action": None,
            "advisor_reason_codes": (),
            "outcome": outcome,
        })
    return records


def _mark_evaluation_outcome(
    records: List[Dict[str, Any]],
    candidate_id: str,
    *,
    outcome: str,
    advisor_action: Optional[str] = None,
    advisor_reason_codes: Sequence[str] = (),
) -> None:
    matches = [record for record in records if record["candidate_id"] == candidate_id]
    if len(matches) != 1:
        raise ValueError(
            f"Expected one setup evaluation diagnostic for candidate {candidate_id}; "
            f"found {len(matches)}"
        )
    record = matches[0]
    record["outcome"] = outcome
    if advisor_action is not None:
        record["advisor_action"] = advisor_action
        record["advisor_reason_codes"] = tuple(advisor_reason_codes)


def _diagnostic_for_candidate(
    records: Sequence[Dict[str, Any]],
    candidate_id: str,
) -> Dict[str, Any]:
    matches = [record for record in records if record.get("candidate_id") == candidate_id]
    if len(matches) != 1:
        raise ValueError(
            f"Expected one diagnostic for candidate {candidate_id}; found {len(matches)}"
        )
    return dict(matches[0])


def _signal_identity(signal: SignalSchema) -> AuctionSignalIdentity:
    meta = signal.meta_json
    if not isinstance(meta, dict) or not isinstance(meta.get("auction_signal"), dict):
        raise ValueError("Active signal lacks authoritative auction_signal identity")
    raw = meta["auction_signal"]
    required = (
        "opportunity_key",
        "candidate_id",
        "boundary_event_key",
        "setup_family",
        "setup_subtype",
        "side",
        "source_event_id",
        "source_event_type",
        "source_episode_id",
        "created_snapshot_time",
    )
    missing = [key for key in required if key not in raw or raw[key] in (None, "")]
    if missing:
        raise ValueError(f"auction_signal identity missing fields: {missing}")
    created = raw["created_snapshot_time"]
    if isinstance(created, str):
        created = datetime.fromisoformat(created.replace("Z", "+00:00"))
    if not isinstance(created, datetime):
        raise ValueError("auction_signal.created_snapshot_time must be datetime")
    return AuctionSignalIdentity(
        opportunity_key=str(raw["opportunity_key"]),
        candidate_id=str(raw["candidate_id"]),
        boundary_event_key=str(raw["boundary_event_key"]),
        setup_family=str(raw["setup_family"]).upper(),
        setup_subtype=str(raw["setup_subtype"]).upper(),
        side=str(raw["side"]).upper(),
        source_event_id=str(raw["source_event_id"]),
        source_event_type=str(raw["source_event_type"]).upper(),
        source_episode_id=str(raw["source_episode_id"]),
        created_snapshot_time=created,
    )


def _candidate_meta(
    *,
    snapshot: SnapshotSchema,
    candidate: AuthoritativeSetupCandidate,
    stage: LifecycleStage,
    status: SignalStatus,
    signal_action: str,
    reason: str,
) -> Dict[str, Any]:
    identity = AuctionSignalIdentity(
        opportunity_key=candidate.opportunity_key,
        candidate_id=candidate.candidate_id,
        boundary_event_key=candidate.source_event_id,
        setup_family=candidate.setup_family.value,
        setup_subtype=candidate.setup_subtype,
        side=candidate.side.value,
        source_event_id=candidate.source_event_id,
        source_event_type=candidate.source_event_type.value,
        source_episode_id=candidate.source_episode_id,
        created_snapshot_time=snapshot.snapshot_time,
    )
    setup_levels = {
        "contract_version": _DOWNSTREAM_CONTRACT_VERSION,
        "source": "AUTHORITATIVE_AUCTION_EVENT",
        "setup_contract_version": candidate.contract_version,
        "auction_engine_name": candidate.auction_engine_name,
        "auction_engine_version": candidate.auction_engine_version,
        "source_event_id": candidate.source_event_id,
        "source_event_type": candidate.source_event_type.value,
        "source_episode_id": candidate.source_episode_id,
        "setup_label": identity.setup_family,
        "setup_subtype": identity.setup_subtype,
        "side": identity.side,
        "entry_price": candidate.entry_price,
        "initial_stop_reference_price": candidate.stop_anchor_price,
        "initial_stop_reference_source": candidate.stop_anchor_type,
        "initial_target_reference_price": candidate.target_reference_price,
        "initial_target_reference_source": candidate.target_basis,
        "reference_price": candidate.reference_price,
        "reference_source": candidate.reference_source,
        "opportunity_key": identity.opportunity_key,
        "candidate_id": identity.candidate_id,
        "boundary_event_key": identity.boundary_event_key,
    }
    return _downstream_meta(
        snapshot=snapshot,
        identity=identity,
        setup_levels=setup_levels,
        stage=stage,
        status=status,
        signal_action=signal_action,
        reason=reason,
    )


def _updated_meta(
    *,
    signal: SignalSchema,
    snapshot: SnapshotSchema,
    identity: AuctionSignalIdentity,
    stage: LifecycleStage,
    status: SignalStatus,
    signal_action: str,
    reason: str,
    auction_action: str,
    progression_candidate: Optional[AuthoritativeSetupCandidate] = None,
) -> Dict[str, Any]:
    if not isinstance(signal.meta_json, dict) or not isinstance(signal.meta_json.get("setup_levels"), dict):
        raise ValueError("Existing signal lacks immutable setup_levels")
    meta = _downstream_meta(
        snapshot=snapshot,
        identity=identity,
        setup_levels=dict(signal.meta_json["setup_levels"]),
        stage=stage,
        status=status,
        signal_action=signal_action,
        reason=reason,
        auction_action=auction_action,
    )
    previous_posture = signal.meta_json.get("auction_posture_history", [])
    previous_lifecycle = signal.meta_json.get("signal_lifecycle_history", [])
    if not isinstance(previous_posture, list) or not isinstance(previous_lifecycle, list):
        raise ValueError("Existing signal histories must be lists")
    meta["auction_posture_history"] = [
        *previous_posture,
        meta["latest_auction_evaluation"],
    ]
    meta["signal_lifecycle_history"] = [
        *previous_lifecycle,
        meta["signal_lifecycle"],
    ]

    previous_lineage = signal.meta_json.get("authoritative_event_lineage")
    if previous_lineage is None:
        previous_lineage = [_identity_lineage_record(identity)]
    if not isinstance(previous_lineage, list) or not all(
        isinstance(item, dict) for item in previous_lineage
    ):
        raise ValueError("Existing authoritative_event_lineage must be a list of objects")
    lineage = [dict(item) for item in previous_lineage]
    if progression_candidate is not None:
        progression = _candidate_lineage_record(progression_candidate)
        if not any(
            item.get("source_event_id") == progression_candidate.source_event_id
            for item in lineage
        ):
            lineage.append(progression)
        meta["latest_authoritative_progression"] = progression
    elif "latest_authoritative_progression" in signal.meta_json:
        meta["latest_authoritative_progression"] = signal.meta_json[
            "latest_authoritative_progression"
        ]
    meta["authoritative_event_lineage"] = lineage
    return sanitize_json(meta)


def _downstream_meta(
    *,
    snapshot: SnapshotSchema,
    identity: AuctionSignalIdentity,
    setup_levels: Dict[str, Any],
    stage: LifecycleStage,
    status: SignalStatus,
    signal_action: str,
    reason: str,
    auction_action: str = "LOCAL_CONFIRMED",
) -> Dict[str, Any]:
    side = identity.side
    setup = identity.setup_family
    terminal = status is not SignalStatus.OPEN
    management_action = "EXIT" if terminal else ("STRENGTHEN" if stage in {LifecycleStage.ACTIVE, LifecycleStage.EXPAND} else "CAUTION")
    trade_action = "FORCE_EXIT" if terminal else ("CREATE_TRADE" if signal_action == "CREATE" else "HOLD_POSITION")
    signal_state = "TERMINAL" if terminal else "OPEN"
    auction_state = (
        snapshot.auction.lifecycle.directional.current_state.value
        if snapshot.auction.lifecycle is not None
        else "UNKNOWN"
    )
    directional_alignment = _directional_alignment(snapshot, side)
    signal_block = {
        "contract_version": _DOWNSTREAM_CONTRACT_VERSION,
        "side": side,
        "setup_label": setup,
        "stage": stage.value,
        "status": status.value,
        "signal_action": signal_action,
        "signal_state": signal_state,
        "signal_reason": reason,
        "setup_levels": setup_levels,
    }
    lifecycle_block = {
        "contract_version": _DOWNSTREAM_CONTRACT_VERSION,
        "stage": stage.value,
        "status": status.value,
        "signal_action": signal_action,
        "signal_state": signal_state,
        "signal_reason": reason,
        "trade_action": trade_action,
    }
    management = {
        "contract_version": _DOWNSTREAM_CONTRACT_VERSION,
        "action": management_action,
        "reason_code": reason,
        "stage": stage.value,
        "side": side,
        "signal_status": status.value,
        "auction_action": auction_action,
        "auction_state": auction_state,
        "directional_alignment": directional_alignment,
        "target_expansion_allowed": bool(
            not terminal and stage in {LifecycleStage.ACTIVE, LifecycleStage.EXPAND}
        ),
        "should_exit_signal": terminal,
        "trail_mode": "ATR_MULTIPLE",
        "exit_pressure": "HIGH" if terminal else "NONE",
        "snapshot_time": snapshot.snapshot_time.isoformat(),
        "opportunity_key": identity.opportunity_key,
        "candidate_id": identity.candidate_id,
        "boundary_event_key": identity.boundary_event_key,
        "setup_family": setup,
        "setup_subtype": identity.setup_subtype,
    }
    auction_signal = {
        "opportunity_key": identity.opportunity_key,
        "candidate_id": identity.candidate_id,
        "boundary_event_key": identity.boundary_event_key,
        "setup_family": setup,
        "setup_subtype": identity.setup_subtype,
        "side": side,
        "source_event_id": identity.source_event_id,
        "source_event_type": identity.source_event_type,
        "source_episode_id": identity.source_episode_id,
        "created_snapshot_time": identity.created_snapshot_time,
    }
    history_record = {
        "snapshot_time": snapshot.snapshot_time,
        "signal_action": signal_action,
        "stage": stage.value,
        "status": status.value,
        "reason_code": reason,
        "auction_action": auction_action,
        "auction_state": auction_state,
        "directional_alignment": directional_alignment,
        "management_posture": management_action,
        "trade_action": trade_action,
    }
    return sanitize_json({
        "downstream_contract": {
            "version": _DOWNSTREAM_CONTRACT_VERSION,
            "source": "EVENT_DRIVEN_AUCTION_SETUP",
            "setup_levels_immutable": True,
        },
        "signal": signal_block,
        "lifecycle": lifecycle_block,
        "management": management,
        "setup_levels": setup_levels,
        "auction_signal": auction_signal,
        "authoritative_event_lineage": [_identity_lineage_record(identity)],
        "latest_auction_evaluation": history_record,
        "auction_posture_history": [history_record],
        "signal_lifecycle": history_record,
        "signal_lifecycle_history": [history_record],
    })


def _same_authoritative_lineage(
    identity: AuctionSignalIdentity,
    candidate: AuthoritativeSetupCandidate,
) -> bool:
    return (
        identity.source_episode_id == candidate.source_episode_id
        and identity.side == candidate.side.value
    )


def _candidate_event_consumed(
    signal: SignalSchema,
    candidate: AuthoritativeSetupCandidate,
) -> bool:
    if not isinstance(signal.meta_json, dict):
        raise ValueError("Existing signal metadata must be an object")
    lineage = signal.meta_json.get("authoritative_event_lineage")
    if lineage is None:
        return False
    if not isinstance(lineage, list) or not all(isinstance(item, dict) for item in lineage):
        raise ValueError("Existing authoritative_event_lineage must be a list of objects")
    return any(
        item.get("source_event_id") == candidate.source_event_id
        for item in lineage
    )


def _identity_lineage_record(identity: AuctionSignalIdentity) -> Dict[str, Any]:
    return {
        "source_event_id": identity.source_event_id,
        "source_event_type": identity.source_event_type,
        "source_episode_id": identity.source_episode_id,
        "setup_family": identity.setup_family,
        "setup_subtype": identity.setup_subtype,
        "side": identity.side,
        "candidate_id": identity.candidate_id,
        "opportunity_key": identity.opportunity_key,
        "snapshot_time": identity.created_snapshot_time,
    }


def _candidate_lineage_record(
    candidate: AuthoritativeSetupCandidate,
) -> Dict[str, Any]:
    return {
        "source_event_id": candidate.source_event_id,
        "source_event_type": candidate.source_event_type.value,
        "source_episode_id": candidate.source_episode_id,
        "setup_family": candidate.setup_family.value,
        "setup_subtype": candidate.setup_subtype,
        "side": candidate.side.value,
        "candidate_id": candidate.candidate_id,
        "opportunity_key": candidate.opportunity_key,
        "snapshot_time": candidate.snapshot_time,
    }


def _operational_posture(
    snapshot: SnapshotSchema,
    identity: AuctionSignalIdentity,
) -> Tuple[LifecycleStage, str]:
    lifecycle = snapshot.auction.lifecycle
    if lifecycle is None:
        raise ValueError("Operational posture requires Auction lifecycle")
    side_direction = "UP" if identity.side == "BUY" else "DOWN"
    current_direction = lifecycle.directional.direction.value
    state = lifecycle.directional.current_state.value
    if current_direction == side_direction and state == "MATURE":
        return LifecycleStage.EXPAND, "AUTHORITATIVE_DIRECTIONAL_MATURE"
    if current_direction == side_direction and state in {"DIRECTIONAL", "REVERSAL_LEG"}:
        return LifecycleStage.ACTIVE, "AUTHORITATIVE_DIRECTIONAL_ALIGNED"
    if state == "REVERSAL_WATCH":
        return LifecycleStage.PROTECT, "AUTHORITATIVE_REVERSAL_WATCH"
    if current_direction not in {"UNKNOWN", side_direction}:
        return LifecycleStage.WEAKENING, "AUTHORITATIVE_DIRECTION_OPPOSED"
    return LifecycleStage.TRANSITION, "NO_CURRENT_CREATION_EVENT"


def _directional_alignment(snapshot: SnapshotSchema, side: str) -> str:
    lifecycle = snapshot.auction.lifecycle
    if lifecycle is None:
        return "UNKNOWN"
    expected = "UP" if side == "BUY" else "DOWN"
    actual = lifecycle.directional.direction.value
    if actual == expected:
        return "ALIGNED"
    if actual in {"UP", "DOWN"}:
        return "OPPOSED"
    return "UNKNOWN"


def _criteria_json(snapshot: SnapshotSchema, evaluations: Sequence[Any], manager: Any) -> Dict[str, Any]:
    return sanitize_json({
        "snapshot_time": snapshot.snapshot_time,
        "authoritative_event_ids": [event.event_id for event in snapshot.auction.lifecycle.events],
        "setup_evaluations": [item.model_dump(mode="python") for item in evaluations],
        "setup_manager": manager.model_dump(mode="python"),
    })


def _snapshot_json(snapshot: SnapshotSchema) -> Dict[str, Any]:
    return sanitize_json(snapshot.model_dump(mode="python", by_alias=True))


def _analytics(signal: SignalSchema, snapshot: SnapshotSchema) -> Dict[str, Any]:
    return calculate_signal_metrics(
        existing_signal=signal,
        side=signal.side,
        current_price=snapshot.close,
        current_time=snapshot.snapshot_time,
    )


def _signal_id(opportunity_key: str) -> str:
    return str(uuid.uuid5(_SIGNAL_ID_NAMESPACE, opportunity_key))


def _decimal(value: float) -> Decimal:
    return Decimal(str(value))


def _decimal_optional(value: Optional[float]) -> Optional[Decimal]:
    return Decimal(str(value)) if value is not None else None


__all__ = [
    "AuctionSignalIdentity",
    "SignalFetcher",
    "SignalPersister",
    "SignalAssembler",
    "SignalGenerator",
]
