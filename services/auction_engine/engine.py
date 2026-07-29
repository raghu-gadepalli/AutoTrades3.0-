"""Authoritative Auction orchestration.

The active engine owns only objective evidence construction, objective
observation classification, Persistent Episode lifecycle and structural
permissions.  Legacy Auction state, boundary lifecycle, setup discovery,
opportunity arbitration and local decision engines are not instantiated or
consulted by this path.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Any, Deque, Dict, Optional

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG, AuctionEngineConfig
from schemas.snapshot import SnapshotSchema
from services.auction_engine.contracts import BarEvidence, EvidenceSnapshot
from services.auction_engine.episode_contracts import (
    AuctionAuthorityResult,
    AuctionEpisodeMemory,
    AuctionEvidenceHistoryEntry,
    AuctionEvidenceHistoryTrend,
    BalanceEpisodeMemory,
    DirectionalEpisodeMemory,
    DirectionalObservationMemory,
)
from services.auction_engine.episode_engine import (
    PersistentEpisodeEngine,
    _BalanceMemory,
    _DirectionalMemory,
    _SymbolMemory,
)
from services.auction_engine.evidence import EvidenceBuilder
from services.auction_engine.observation_provider import AuctionObservationProvider


@dataclass(frozen=True)
class _HistoryEvidence:
    close: float
    bar: BarEvidence
    trend: AuctionEvidenceHistoryTrend
    atr: Optional[float] = None


class AuctionEngine:
    """Advance one strict authoritative Auction snapshot at a time."""

    def __init__(self, config: AuctionEngineConfig = AUCTION_ENGINE_CONFIG) -> None:
        self.config = config
        self.evidence_builder = EvidenceBuilder(config)
        self.observation_provider = AuctionObservationProvider(config)
        self.episode_engine = PersistentEpisodeEngine(config)
        self._history: Dict[str, Deque[_HistoryEvidence]] = defaultdict(
            lambda: deque(maxlen=self.config.state.history_bars)
        )
        self._last_results: Dict[str, AuctionAuthorityResult] = {}
        self._last_input_hashes: Dict[str, str] = {}

    def reset(self, symbol: Optional[str] = None) -> None:
        if symbol is None:
            self._history.clear()
            self._last_results.clear()
            self._last_input_hashes.clear()
            self.episode_engine.reset()
            self.observation_provider.reset()
            return
        key = self._symbol_key(symbol)
        self._history.pop(key, None)
        self._last_results.pop(key, None)
        self._last_input_hashes.pop(key, None)
        self.episode_engine.reset(key)
        self.observation_provider.reset(key)

    def evaluate_snapshot(
        self,
        snapshot: SnapshotSchema,
        *,
        equity_ref: Optional[str] = None,
    ) -> AuctionAuthorityResult:
        if not isinstance(snapshot, SnapshotSchema):
            raise TypeError("AuctionEngine.evaluate_snapshot requires SnapshotSchema")
        symbol = self._symbol_key(snapshot.symbol)
        snapshot_time = snapshot.snapshot_time
        input_hash = self._snapshot_content_hash(snapshot)

        prior = self._last_results[symbol] if symbol in self._last_results else None
        if prior is not None and prior.snapshot_time == snapshot_time:
            prior_hash = self._last_input_hashes[symbol]
            if prior_hash == input_hash:
                return prior
            raise ValueError(
                f"Conflicting duplicate authoritative snapshot for {symbol} @ {snapshot_time}"
            )

        controller_memory = (
            self.episode_engine._memory[symbol]
            if symbol in self.episode_engine._memory
            else None
        )
        if controller_memory is not None:
            if controller_memory.trading_day != snapshot_time.date():
                self.reset(symbol)
            elif (
                controller_memory.last_snapshot_time is not None
                and snapshot_time <= controller_memory.last_snapshot_time
            ):
                raise ValueError(
                    f"Authoritative snapshot time must advance for {symbol}: "
                    f"{snapshot_time} <= {controller_memory.last_snapshot_time}"
                )

        history = tuple(self._history[symbol])
        evidence = self.evidence_builder.build(
            snapshot,
            history=history,
            equity_ref=equity_ref,
        )
        observation = self.observation_provider.build(snapshot, evidence)
        lifecycle = self.episode_engine.advance(observation)
        result = AuctionAuthorityResult(
            symbol=symbol,
            snapshot_time=snapshot_time,
            evidence=evidence,
            observation=observation,
            lifecycle=lifecycle,
        )

        self._history[symbol].append(self._compact_history(evidence))
        self._last_results[symbol] = result
        self._last_input_hashes[symbol] = input_hash
        return result

    def export_incremental_state(self, symbol: str) -> AuctionEpisodeMemory:
        key = self._symbol_key(symbol)
        if key not in self.episode_engine._memory:
            raise ValueError(f"No authoritative Auction memory exists for {key}")
        controller = self.episode_engine._memory[key]
        if controller.last_snapshot_time is None:
            raise ValueError("Cannot export unevaluated authoritative Auction memory")
        return AuctionEpisodeMemory(
            symbol=key,
            trading_day=controller.trading_day,
            last_snapshot_time=controller.last_snapshot_time,
            last_observation_hash=controller.last_observation_hash,
            evidence_history=tuple(
                AuctionEvidenceHistoryEntry(
                    close=item.close,
                    bar=item.bar,
                    trend=item.trend,
                    atr=item.atr,
                )
                for item in self._history[key]
            ),
            observation=self.observation_provider.export_memory(key),
            directional=self._directional_memory_contract(controller.directional),
            balance=self._balance_memory_contract(controller.balance),
        )

    def restore_incremental_state(
        self,
        symbol: str,
        payload: AuctionEpisodeMemory | Dict[str, Any],
    ) -> None:
        key = self._symbol_key(symbol)
        memory = (
            payload
            if isinstance(payload, AuctionEpisodeMemory)
            else AuctionEpisodeMemory.model_validate(payload)
        )
        if memory.symbol != key:
            raise ValueError(
                f"Authoritative Auction memory symbol mismatch: {memory.symbol} != {key}"
            )
        if memory.last_snapshot_time is None:
            raise ValueError("Only evaluated Auction memory may be restored")

        self.reset(key)
        self.observation_provider.restore_memory(key, memory.observation)
        restored_history: Deque[_HistoryEvidence] = deque(
            (
                _HistoryEvidence(
                    close=item.close,
                    bar=item.bar,
                    trend=item.trend,
                    atr=item.atr,
                )
                for item in memory.evidence_history
            ),
            maxlen=self.config.state.history_bars,
        )
        self._history[key] = restored_history

        directional_data = memory.directional.model_dump(mode="python")
        directional_data["emitted_event_ids"] = set(
            directional_data["emitted_event_ids"]
        )
        balance_data = memory.balance.model_dump(mode="python")
        balance_data["source_range_ids"] = list(balance_data["source_range_ids"])
        balance_data["emitted_event_ids"] = set(balance_data["emitted_event_ids"])
        self.episode_engine._memory[key] = _SymbolMemory(
            trading_day=memory.trading_day,
            last_snapshot_time=memory.last_snapshot_time,
            last_observation_hash=memory.last_observation_hash,
            last_evaluation=None,
            directional=_DirectionalMemory(**directional_data),
            balance=_BalanceMemory(**balance_data),
        )

    @staticmethod
    def initial_memory(symbol: str, snapshot_time: datetime) -> AuctionEpisodeMemory:
        key = AuctionEngine._symbol_key(symbol)
        return AuctionEpisodeMemory(
            symbol=key,
            trading_day=snapshot_time.date(),
            last_snapshot_time=None,
            last_observation_hash="",
            evidence_history=(),
            observation=AuctionObservationProvider.initial_memory(),
            directional=DirectionalEpisodeMemory(
                sequence=0,
                episode_id=None,
                state="NONE",
                direction="UNKNOWN",
                origin_source="NONE",
                parent_episode_id=None,
                origin_event_id=None,
                started_at=None,
                state_started_at=None,
                state_age_bars=0,
                origin_price=None,
                extreme_price=None,
                extreme_time=None,
                protection_level=None,
                protection_source="",
                protection_time=None,
                start_candidate_side="UNKNOWN",
                start_candidate_bars=0,
                rejection_seen=False,
                rejection_seen_at=None,
                continuation_failure_seen=False,
                continuation_failure_seen_at=None,
                continuation_failure_progress_bars=0,
                first_adverse_bar_time=None,
                first_adverse_bar_level=None,
                first_adverse_bar_close=None,
                reversal_confirmation_level=None,
                reversal_confirmation_source="",
                reversal_confirmation_level_time=None,
                reversal_confirmation_breach_closes=0,
                reversal_watch_age_bars=0,
                reversal_leg_progress_bars=0,
                reversal_leg_failure_closes=0,
                reversal_leg_progress_atr=0.0,
                trend_restore_bars=0,
                opposite_control_bars=0,
                inactive_bars=0,
                emitted_event_ids=(),
                last_close=None,
                last_observation_state="UNKNOWN",
                last_observation_state_time=None,
                last_reason_codes=(),
            ),
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
                escape_direction="UNKNOWN",
                outside_close_count=0,
                reentry_close_count=0,
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
    def _directional_memory_contract(
        memory: _DirectionalMemory,
    ) -> DirectionalEpisodeMemory:
        payload = dict(memory.__dict__)
        payload["emitted_event_ids"] = tuple(sorted(memory.emitted_event_ids))
        return DirectionalEpisodeMemory.model_validate(payload)

    @staticmethod
    def _balance_memory_contract(memory: _BalanceMemory) -> BalanceEpisodeMemory:
        payload = dict(memory.__dict__)
        payload["source_range_ids"] = tuple(memory.source_range_ids)
        payload["emitted_event_ids"] = tuple(sorted(memory.emitted_event_ids))
        return BalanceEpisodeMemory.model_validate(payload)

    @staticmethod
    def _symbol_key(symbol: str) -> str:
        key = str(symbol).strip().upper()
        if not key:
            raise ValueError("Auction symbol is required")
        return key

    @staticmethod
    def _snapshot_content_hash(snapshot: SnapshotSchema) -> str:
        payload = snapshot.model_dump(
            mode="json",
            by_alias=True,
            exclude={"auction": True, "memory": {"auction": True}},
        )
        raw = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


__all__ = ["AuctionEngine"]
