"""Auction orchestration used by snapshot generation."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Deque, Dict, Optional, Sequence

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG, AuctionEngineConfig
from enums.auction_engine import DirectionalBias
from schemas.snapshot import SnapshotSchema
from services.auction_engine.balance_state_machine import (
    BalanceObservationBuilder,
    BalanceStateMachine,
)
from services.auction_engine.contracts import BarEvidence, EvidenceSnapshot
from services.auction_engine.directional_evidence import DirectionalEvidenceBuilder
from services.auction_engine.directional_state_machine import DirectionalStateMachine
from services.auction_engine.episode_contracts import (
    AuctionEvidenceHistoryTrend,
    BalanceEpisodeMemory,
)
from services.auction_engine.evidence import EvidenceBuilder
from services.auction_engine.directional_contracts import (
    AuctionAuthorityResult,
    AuctionMemory,
    DirectionalMemory,
)
from services.auction_engine.structural_permissions import StructuralPermissionMatrix


@dataclass(frozen=True)
class _HistoryEvidence:
    close: float
    bar: BarEvidence
    trend: AuctionEvidenceHistoryTrend
    atr: Optional[float] = None


class AuctionEngine:
    """One current-evidence builder, one directional tracker and existing balance."""

    def __init__(self, config: AuctionEngineConfig = AUCTION_ENGINE_CONFIG) -> None:
        self.config = config
        self.evidence_builder = EvidenceBuilder(config)
        self.directional_evidence_builder = DirectionalEvidenceBuilder(config)
        self.directional_state_machine = DirectionalStateMachine(config)
        self.balance_observation_builder = BalanceObservationBuilder(config)
        self.balance_state_machine = BalanceStateMachine(config)
        self.permission_matrix = StructuralPermissionMatrix(config)
        self._history: Dict[str, Deque[_HistoryEvidence]] = defaultdict(
            lambda: deque(maxlen=self.config.state.history_bars)
        )
        self._memory: Dict[str, AuctionMemory] = {}
        self._last_results: Dict[str, AuctionAuthorityResult] = {}

    def reset(self, symbol: Optional[str] = None) -> None:
        if symbol is None:
            self._history.clear()
            self._memory.clear()
            self._last_results.clear()
            return
        key = self._symbol_key(symbol)
        self._history.pop(key, None)
        self._memory.pop(key, None)
        self._last_results.pop(key, None)

    def evaluate_snapshot(
        self,
        snapshot: SnapshotSchema,
        *,
        equity_ref: Optional[str] = None,
    ) -> AuctionAuthorityResult:
        if not isinstance(snapshot, SnapshotSchema):
            raise TypeError("AuctionEngine requires SnapshotSchema")
        symbol = self._symbol_key(snapshot.symbol)
        snapshot_time = snapshot.snapshot_time
        memory = self._memory.get(symbol)
        if memory is None or memory.trading_day != snapshot_time.date():
            memory = self.initial_memory(symbol, snapshot_time)
        if memory.last_snapshot_time is not None:
            if snapshot_time <= memory.last_snapshot_time:
                raise ValueError(
                    f"Auction snapshot time must advance for {symbol}: "
                    f"{snapshot_time} <= {memory.last_snapshot_time}"
                )

        objective = self.evidence_builder.build(
            snapshot,
            history=tuple(self._history[symbol]),
            equity_ref=equity_ref,
        )
        fresh = self.directional_evidence_builder.build(snapshot, objective)
        balance_observation = self.balance_observation_builder.build(snapshot, objective)
        balance_memory, balance_projection, balance_events = self.balance_state_machine.advance(
            previous_memory=memory.balance,
            observation=balance_observation,
        )
        directional_memory, directional_projection, directional_events = (
            self.directional_state_machine.advance(
                symbol=symbol,
                trading_day=snapshot_time.date(),
                snapshot_time=snapshot_time,
                close=float(objective.close),
                high=float(objective.bar.high),
                low=float(objective.bar.low),
                evidence=fresh,
                previous_memory=memory.directional,
                balance_state=balance_projection.current_state,
            )
        )
        events = tuple((*balance_events, *directional_events))
        permissions = self.permission_matrix.evaluate(
            balance_state=balance_projection.current_state,
            events=events,
        )
        result = AuctionAuthorityResult(
            symbol=symbol,
            snapshot_time=snapshot_time,
            objective_evidence=objective,
            fresh_direction=fresh,
            directional=directional_projection,
            balance=balance_projection,
            events=events,
            permissions=permissions,
            diagnostics={
                "fresh_direction": fresh.side.value,
                "active_episode_id": directional_projection.active_episode_id,
                "directional_transition": directional_projection.transition.value,
                "balance_state": balance_projection.current_state.value,
            },
        )

        self._history[symbol].append(self._compact_history(objective))
        self._memory[symbol] = AuctionMemory(
            symbol=symbol,
            trading_day=snapshot_time.date(),
            last_snapshot_time=snapshot_time,
            directional=directional_memory,
            balance=balance_memory,
        )
        self._last_results[symbol] = result
        return result

    def export_incremental_state(self, symbol: str) -> AuctionMemory:
        key = self._symbol_key(symbol)
        memory = self._memory.get(key)
        if memory is None or memory.last_snapshot_time is None:
            raise ValueError(f"No evaluated Auction memory for {key}")
        return memory

    def restore_incremental_state(
        self,
        symbol: str,
        payload: AuctionMemory | dict,
        *,
        history_snapshots: Sequence[SnapshotSchema] = (),
    ) -> None:
        key = self._symbol_key(symbol)
        memory = (
            payload
            if isinstance(payload, AuctionMemory)
            else AuctionMemory.model_validate(payload)
        )
        if memory.symbol != key:
            raise ValueError(f"Auction memory symbol mismatch: {memory.symbol} != {key}")
        if memory.last_snapshot_time is None:
            raise ValueError("Only evaluated Auction memory may be restored")
        self.reset(key)
        self._memory[key] = memory
        self._history[key] = self._rebuild_history(
            symbol=key,
            trading_day=memory.trading_day,
            through_time=memory.last_snapshot_time,
            snapshots=history_snapshots,
        )

    def _rebuild_history(
        self,
        *,
        symbol: str,
        trading_day: object,
        through_time: datetime,
        snapshots: Sequence[SnapshotSchema],
    ) -> Deque[_HistoryEvidence]:
        history: Deque[_HistoryEvidence] = deque(
            maxlen=self.config.state.history_bars
        )
        ordered = sorted(snapshots, key=lambda item: item.snapshot_time)
        seen = set()
        for snapshot in ordered:
            if snapshot.symbol.strip().upper() != symbol:
                raise ValueError("Auction history snapshot symbol mismatch")
            if snapshot.snapshot_time.date() != trading_day:
                raise ValueError("Auction history snapshot trading day mismatch")
            if snapshot.snapshot_time > through_time:
                raise ValueError("Auction history cannot extend beyond restored memory")
            if snapshot.snapshot_time in seen:
                raise ValueError("Auction history snapshots must be unique")
            seen.add(snapshot.snapshot_time)
            objective = self.evidence_builder.build(
                snapshot,
                history=tuple(history),
                equity_ref=symbol,
            )
            history.append(self._compact_history(objective))
        return history

    @staticmethod
    def initial_memory(symbol: str, snapshot_time: datetime) -> AuctionMemory:
        key = AuctionEngine._symbol_key(symbol)
        return AuctionMemory(
            symbol=key,
            trading_day=snapshot_time.date(),
            last_snapshot_time=None,
            directional=DirectionalMemory(),
            balance=BalanceEpisodeMemory(
                sequence=0,
                episode_id=None,
                state="NONE",
                started_at=None,
                state_started_at=None,
                state_age_bars=0,
                range_id=None,
                candidate_low=None,
                candidate_high=None,
                source_range_ids=(),
                candidate_merge_count=0,
                candidate_bar_expansion_count=0,
                candidate_last_valid_at=None,
                frozen_low=None,
                frozen_high=None,
                containment_bars=0,
                forming_bars_observed=0,
                marginal_excursion_bars=0,
                meaningful_escape_bars=0,
                forming_invalid_bars=0,
                escape_direction=DirectionalBias.UNKNOWN,
                outside_close_count=0,
                reentry_close_count=0,
                escape_attempt_count=0,
                failed_escape_count=0,
                up_escape_attempt_count=0,
                down_escape_attempt_count=0,
                last_escape_direction=DirectionalBias.UNKNOWN,
                last_escape_started_at=None,
                last_escape_failed_at=None,
                rearm_required=False,
                rearm_inside_close_count=0,
                rearm_bars_elapsed=0,
                attempt_limit_reached=False,
                emitted_event_ids=(),
                last_reason_codes=(),
            ),
        )

    @staticmethod
    def _compact_history(evidence: EvidenceSnapshot) -> _HistoryEvidence:
        return _HistoryEvidence(
            close=evidence.close,
            bar=evidence.bar,
            trend=AuctionEvidenceHistoryTrend(
                hma_order=evidence.trend.hma_order,
                hma_spread_atr=evidence.trend.hma_spread_atr,
                ema_slow=evidence.trend.ema_slow,
                ema_ref=evidence.trend.ema_ref,
            ),
            atr=evidence.atr,
        )

    @staticmethod
    def _symbol_key(symbol: str) -> str:
        key = str(symbol or "").strip().upper()
        if not key:
            raise ValueError("Auction symbol is required")
        return key


__all__ = ["AuctionEngine"]
