"""Strict snapshot-carried Auction Engine adapter.

The adapter consumes a validated ``SnapshotSchema`` and returns a compact
public Auction projection plus bounded private continuity memory. It never
reads or writes signal/trade state and never falls back to alternate snapshot
paths.
"""
from __future__ import annotations

from datetime import datetime
import logging
import time
from typing import Dict, Optional

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG
from configs.snapshot_config import SNAPSHOT_CONFIG
from schemas.snapshot import (
    AuctionChange,
    AuctionDecisionProjection,
    AuctionMemoryBlock,
    AuctionSnapshotBlock,
    AuctionStateProjection,
    BoundaryProjection,
    CandidateProjection,
    FrozenRangeProjection,
    OpportunityProjection,
    SnapshotSchema,
    StockContextProjection,
)
from services.auction_engine.engine import AuctionEngine

logger = logging.getLogger(__name__)


def empty_auction_memory() -> AuctionMemoryBlock:
    """Explicit pre-evaluation placeholder used only during assembly."""
    return AuctionMemoryBlock(
        history=[],
        state_memory=None,
        boundary_current=None,
        boundary_last_time=None,
        boundary_sequences=[],
        boundary_last_terminal=None,
        setup_initiation={},
        setup_failed={},
        setup_emitted_once=[],
        setup_completed=[],
        setup_last_time=None,
        ledger_records={},
        ledger_last_day=None,
    )


def empty_auction_block() -> AuctionSnapshotBlock:
    """Explicit pre-evaluation placeholder used only during assembly."""
    return AuctionSnapshotBlock(
        status="NOT_RUN",
        continuity_mode="COLD_START",
        previous_snapshot_time=None,
        state=None,
        stock_context=None,
        boundary=None,
        candidates=[],
        opportunities=[],
        decision=None,
        changes=[],
        error=None,
    )


def enrich_snapshot_with_auction(
    snapshot: SnapshotSchema,
    *,
    previous_snapshot: Optional[SnapshotSchema] = None,
) -> tuple[AuctionSnapshotBlock, AuctionMemoryBlock]:
    """Evaluate one validated snapshot and return public result + memory."""
    symbol = snapshot.symbol.strip().upper()
    snapshot_time = snapshot.snapshot_time
    engine = AuctionEngine(AUCTION_ENGINE_CONFIG)
    previous_time: Optional[datetime] = None
    continuity_mode = "COLD_START"
    started = time.perf_counter()

    if previous_snapshot is not None:
        previous_time = previous_snapshot.snapshot_time
        if previous_snapshot.symbol.strip().upper() != symbol:
            raise ValueError(
                f"Previous snapshot symbol mismatch: {previous_snapshot.symbol} != {symbol}"
            )
        if previous_time >= snapshot_time:
            raise ValueError(
                f"Previous snapshot time must precede current snapshot: "
                f"{previous_time} >= {snapshot_time}"
            )
        if previous_time.date() == snapshot_time.date():
            gap_minutes = (snapshot_time - previous_time).total_seconds() / 60.0
            max_gap = float(SNAPSHOT_CONFIG.auction.max_incremental_gap_minutes)
            if gap_minutes > max_gap:
                raise ValueError(
                    f"Auction continuity gap is {gap_minutes:.3f} minutes; "
                    f"maximum is {max_gap:.3f}"
                )
            if previous_snapshot.auction.status != "OK":
                raise ValueError("Previous same-day Auction block is not OK")
            engine.restore_incremental_state(
                symbol,
                previous_snapshot.memory.auction.model_dump(mode="python"),
            )
            continuity_mode = "INCREMENTAL_PREVIOUS_SNAPSHOT"

    result = engine.evaluate_snapshot(snapshot, equity_ref=symbol)
    memory = AuctionMemoryBlock.model_validate(
        engine.export_incremental_state(symbol)
    )

    state = _state_projection(result.auction_state)
    stock_context = _stock_context_projection(result.stock_context)
    boundary = (
        _boundary_projection(result.boundary_episode)
        if result.boundary_episode is not None
        else None
    )
    public_candidates = list(result.candidates)
    selected_candidate = result.local_decision.selected_candidate
    if (
        selected_candidate is not None
        and all(
            item.candidate_id != selected_candidate.candidate_id
            for item in public_candidates
        )
    ):
        # SetupManager may select a still-active candidate carried by the
        # opportunity ledger rather than a candidate freshly observed on this
        # snapshot.  The public snapshot decision references that identity, so
        # include its objective geometry in the candidate projection as well.
        public_candidates.append(selected_candidate)
        logger.debug(
            "auction_snapshot carried selected candidate into public projection | "
            "symbol=%s snapshot_time=%s candidate_id=%s",
            symbol,
            snapshot_time,
            selected_candidate.candidate_id,
        )

    candidates = [_candidate_projection(item) for item in public_candidates]
    opportunities = [
        _opportunity_projection(record)
        for record in engine.opportunity_ledger.records(symbol)
    ]
    decision = _decision_projection(result)
    changes = _build_changes(
        previous_snapshot.auction
        if previous_snapshot is not None
        and previous_snapshot.snapshot_time.date() == snapshot_time.date()
        else None,
        state=state,
        stock_context=stock_context,
        boundary=boundary,
        candidates=candidates,
        opportunities=opportunities,
        decision=decision,
    )

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    logger.info(
        "auction_snapshot symbol=%s snapshot_time=%s continuity_mode=%s "
        "elapsed_ms=%.3f state=%s candidates=%d opportunities=%d action=%s changes=%d",
        symbol,
        snapshot_time,
        continuity_mode,
        elapsed_ms,
        state.current,
        len(candidates),
        len(opportunities),
        decision.action,
        len(changes),
    )

    block = AuctionSnapshotBlock(
        status="OK",
        continuity_mode=continuity_mode,
        previous_snapshot_time=previous_time,
        state=state,
        stock_context=stock_context,
        boundary=boundary,
        candidates=candidates,
        opportunities=opportunities,
        decision=decision,
        changes=changes,
        error=None,
    )
    return block, memory


def _state_projection(state) -> AuctionStateProjection:
    return AuctionStateProjection(
        state_key=state.state_key,
        previous=state.previous_state.value,
        current=state.current_state.value,
        transition_time=state.transition_time,
        entered_at=state.entered_at,
        expires_at=state.expires_at,
        reason_codes=list(state.reason_codes),
    )


def _stock_context_projection(context) -> StockContextProjection:
    return StockContextProjection(
        name=context.name.value,
        background_regime=context.background_regime.value,
        current_auction_state=context.current_auction_state.value,
        directional_bias=context.directional_bias.value,
        balanced_non_directional=context.balanced_non_directional,
        compression_active=context.compression_active,
        rotational=context.rotational,
        fresh_expansion_confirmed=context.fresh_expansion_confirmed,
        directional_efficiency=context.directional_efficiency,
        overlap_ratio=context.overlap_ratio,
        ema_context=context.ema_context,
        ema_spread_atr=context.ema_spread_atr,
        ema_spread_change_atr_per_bar=context.ema_spread_change_atr_per_bar,
        hma_context=context.hma_context,
        hma_spread_atr=context.hma_spread_atr,
        atr_state=context.atr_state,
        atr_contraction_ratio=context.atr_contraction_ratio,
        background_range_id=context.background_range_id,
        background_range_low=context.background_range_low,
        background_range_high=context.background_range_high,
        background_range_position=context.background_range_position,
        background_range_outside_atr=context.background_range_outside_atr,
        background_range_classification=context.background_range_classification,
        background_structure_flip_count=context.background_structure_flip_count,
        exhaustion_active=context.exhaustion_active,
        exhausted_side=context.exhausted_side.value,
        exhaustion_started_at=context.exhaustion_started_at,
        exhaustion_expires_at=context.exhaustion_expires_at,
        reason_codes=list(context.reason_codes),
    )


def _boundary_projection(boundary) -> BoundaryProjection:
    frozen = boundary.frozen_range
    return BoundaryProjection(
        event_key=boundary.event_key,
        structural_key=boundary.structural_key,
        attempt_id=boundary.attempt_id,
        sequence=boundary.episode_sequence,
        event_time=boundary.event_time,
        first_seen_time=boundary.first_seen_time,
        last_seen_time=boundary.last_seen_time,
        attempt_time=boundary.attempt_time,
        boundary_id=boundary.boundary_id,
        boundary_side=boundary.boundary_side.value,
        boundary_source=boundary.boundary_source,
        boundary_price=boundary.boundary_price,
        breakout_side=boundary.breakout_side.value,
        failure_side=boundary.failure_side.value,
        frozen_range=FrozenRangeProjection(
            range_id=frozen.range_id,
            version=frozen.range_version,
            source=frozen.source,
            low=frozen.low,
            high=frozen.high,
            start_time=frozen.start_time,
            end_time=frozen.end_time,
            frozen_at=frozen.frozen_at,
            basis=frozen.basis,
            quality=frozen.quality_score,
        ),
        status=boundary.status.value,
        resolution=boundary.resolution.value,
        accepted_time=boundary.accepted_time,
        failed_time=boundary.failed_time,
        expires_at=boundary.expires_at,
        current_offset_atr=boundary.current_offset_atr,
        max_outside_excursion_atr=boundary.max_outside_excursion_atr,
        consecutive_outside_closes=boundary.consecutive_outside_closes,
        consecutive_inside_closes=boundary.consecutive_inside_closes,
        retest_detected=boundary.retest_detected,
        terminal=boundary.terminal,
        consumed=boundary.consumed,
        superseded=boundary.superseded,
        terminal_reason=boundary.terminal_reason,
        superseded_by=boundary.superseded_by,
        reason_codes=list(boundary.reason_codes),
    )


def _candidate_projection(candidate) -> CandidateProjection:
    return CandidateProjection(
        candidate_id=candidate.candidate_id,
        opportunity_key=candidate.opportunity_key,
        family=candidate.family.value,
        subtype=candidate.subtype,
        role=candidate.candidate_role.value,
        side=candidate.side.value,
        eligibility=candidate.eligibility.value,
        blockers=list(candidate.blockers),
        reason_codes=list(candidate.reason_codes),
        event_key=candidate.event_key,
        event_time=candidate.event_time,
        candidate_time=candidate.candidate_time,
        valid_until=candidate.valid_until,
        auction_state=candidate.auction_state.value,
        entry_price=candidate.entry_price,
        stop_anchor_price=candidate.stop_anchor_price,
        stop_anchor_type=candidate.stop_anchor_type,
        target_basis=candidate.target_basis,
        target_reference_price=candidate.target_reference_price,
        room_points=candidate.room_points,
        room_atr=candidate.room_atr,
        room_pct=candidate.room_pct,
        entry_distance_atr=candidate.entry_distance_atr,
        source_boundary_id=candidate.source_boundary_id,
        source_boundary_status=candidate.source_boundary_status.value,
        source_boundary_resolution=candidate.source_boundary_resolution.value,
        source_boundary_side=candidate.source_boundary_side.value,
        source_boundary_price=candidate.source_boundary_price,
        source_frozen_range_id=candidate.source_frozen_range_id,
        source_frozen_range_version=candidate.source_frozen_range_version,
        source_frozen_range_low=candidate.source_frozen_range_low,
        source_frozen_range_high=candidate.source_frozen_range_high,
        terminal=candidate.terminal,
        consumed=candidate.consumed,
        superseded=candidate.superseded,
    )


def _opportunity_projection(record) -> OpportunityProjection:
    candidate = record.primary_candidate
    return OpportunityProjection(
        opportunity_key=record.opportunity_key,
        side=record.side.value,
        lifecycle=record.lifecycle_state,
        boundary_event_key=record.boundary_event_key,
        primary_candidate_id=candidate.candidate_id,
        primary_family=candidate.family.value,
        primary_subtype=candidate.subtype,
        primary_role=candidate.candidate_role.value,
        primary_eligibility=candidate.eligibility.value,
        candidate_ids=list(record.candidate_ids),
        supporting_candidate_ids=list(record.supporting_candidate_ids),
        selected_candidate_id=record.selected_candidate_id,
        first_observed_time=record.first_observed_time,
        last_observed_time=record.last_observed_time,
        eligible_time=record.eligible_time,
        selected_time=record.selected_time,
        reason_codes=list(record.reason_codes),
    )


def _decision_projection(result) -> AuctionDecisionProjection:
    local = result.local_decision
    manager = result.manager_decision
    selected = local.selected_candidate
    return AuctionDecisionProjection(
        action=local.action.value,
        manager_action=manager.action.value,
        selected_candidate_id=(selected.candidate_id if selected is not None else None),
        selected_opportunity_key=(selected.opportunity_key if selected is not None else None),
        family=(selected.family.value if selected is not None else None),
        subtype=(selected.subtype if selected is not None else None),
        side=(selected.side.value if selected is not None else None),
        entry_price=(selected.entry_price if selected is not None else None),
        stop_anchor_price=(selected.stop_anchor_price if selected is not None else None),
        stop_anchor_type=(selected.stop_anchor_type if selected is not None else None),
        target_basis=(selected.target_basis if selected is not None else None),
        target_reference_price=(
            selected.target_reference_price if selected is not None else None
        ),
        valid_until=local.valid_until,
        reason_codes=list(local.reason_codes),
        manager_reason_codes=list(manager.reason_codes),
        manager_diagnostics=dict(manager.diagnostics),
    )


def _build_changes(
    previous: Optional[AuctionSnapshotBlock],
    *,
    state: AuctionStateProjection,
    stock_context: StockContextProjection,
    boundary: Optional[BoundaryProjection],
    candidates: list[CandidateProjection],
    opportunities: list[OpportunityProjection],
    decision: AuctionDecisionProjection,
) -> list[AuctionChange]:
    changes: list[AuctionChange] = []
    if previous is None or previous.state is None:
        changes.append(AuctionChange(
            type="AUCTION_STATE_INITIALIZED",
            entity_key=state.state_key,
            from_=None,
            to=state.current,
        ))
    elif previous.state.current != state.current:
        changes.append(AuctionChange(
            type="AUCTION_STATE_CHANGED",
            entity_key=state.state_key,
            from_=previous.state.current,
            to=state.current,
        ))

    previous_context = previous.stock_context if previous is not None else None
    if previous_context is None:
        changes.append(AuctionChange(
            type="STOCK_CONTEXT_INITIALIZED",
            entity_key="STOCK_CONTEXT",
            from_=None,
            to=stock_context.name,
        ))
    elif (
        previous_context.name != stock_context.name
        or previous_context.exhaustion_active != stock_context.exhaustion_active
        or previous_context.exhausted_side != stock_context.exhausted_side
    ):
        changes.append(AuctionChange(
            type="STOCK_CONTEXT_CHANGED",
            entity_key="STOCK_CONTEXT",
            from_=previous_context.name,
            to=stock_context.name,
        ))


    previous_boundary = previous.boundary if previous is not None else None
    if boundary is not None:
        if previous_boundary is None or previous_boundary.event_key != boundary.event_key:
            changes.append(AuctionChange(
                type="BOUNDARY_STARTED",
                entity_key=boundary.event_key,
                from_=None,
                to=boundary.status,
            ))
        elif previous_boundary.status != boundary.status:
            changes.append(AuctionChange(
                type="BOUNDARY_STATUS_CHANGED",
                entity_key=boundary.event_key,
                from_=previous_boundary.status,
                to=boundary.status,
            ))
    elif previous_boundary is not None:
        changes.append(AuctionChange(
            type="BOUNDARY_CLOSED",
            entity_key=previous_boundary.event_key,
            from_=previous_boundary.status,
            to=None,
        ))

    previous_candidates: Dict[str, CandidateProjection] = (
        {item.candidate_id: item for item in previous.candidates}
        if previous is not None
        else {}
    )
    for candidate in candidates:
        prior = previous_candidates[candidate.candidate_id] if candidate.candidate_id in previous_candidates else None
        if prior is None:
            changes.append(AuctionChange(
                type="CANDIDATE_CREATED",
                entity_key=candidate.candidate_id,
                from_=None,
                to=candidate.eligibility,
            ))
        elif prior.eligibility != candidate.eligibility:
            changes.append(AuctionChange(
                type="CANDIDATE_ELIGIBILITY_CHANGED",
                entity_key=candidate.candidate_id,
                from_=prior.eligibility,
                to=candidate.eligibility,
            ))

    previous_opportunities: Dict[str, OpportunityProjection] = (
        {item.opportunity_key: item for item in previous.opportunities}
        if previous is not None
        else {}
    )
    for opportunity in opportunities:
        prior = previous_opportunities[opportunity.opportunity_key] if opportunity.opportunity_key in previous_opportunities else None
        if prior is None:
            changes.append(AuctionChange(
                type="OPPORTUNITY_CREATED",
                entity_key=opportunity.opportunity_key,
                from_=None,
                to=opportunity.lifecycle,
            ))
        elif prior.lifecycle != opportunity.lifecycle:
            changes.append(AuctionChange(
                type="OPPORTUNITY_LIFECYCLE_CHANGED",
                entity_key=opportunity.opportunity_key,
                from_=prior.lifecycle,
                to=opportunity.lifecycle,
            ))

    previous_decision = previous.decision if previous is not None else None
    if previous_decision is None:
        changes.append(AuctionChange(
            type="LOCAL_DECISION_INITIALIZED",
            entity_key="LOCAL_DECISION",
            from_=None,
            to=decision.action,
        ))
    elif (
        previous_decision.action != decision.action
        or previous_decision.selected_candidate_id != decision.selected_candidate_id
    ):
        changes.append(AuctionChange(
            type="LOCAL_DECISION_CHANGED",
            entity_key=decision.selected_candidate_id or "LOCAL_DECISION",
            from_=previous_decision.action,
            to=decision.action,
        ))

    return changes


__all__ = [
    "empty_auction_block",
    "empty_auction_memory",
    "enrich_snapshot_with_auction",
]
