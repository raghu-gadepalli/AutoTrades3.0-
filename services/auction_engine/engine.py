"""Deterministic, signal-agnostic Auction Engine orchestration.

The engine owns evidence interpretation, auction state, boundary episodes,
setup candidates, the stock-day opportunity ledger, local arbitration and the
final local opportunity assessment. It does not read signal/trade state, create persistence payloads or perform database writes.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
import hashlib
import json
from typing import Any, Deque, Dict, Iterable, List, Mapping, Optional

from schemas.snapshot import SnapshotSchema

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG, AuctionEngineConfig
from services.auction_engine.contracts import (
    AuctionEngineResult,
    EvidenceSnapshot,
    ManagerAction,
)
from services.auction_engine.boundary_engine import BoundaryEpisodeEngine, _EpisodeMemory
from services.auction_engine.evidence import EvidenceBuilder
from services.auction_engine.state_engine import (
    AuctionStateChronologyError,
    AuctionStateEngine,
    _StateMemory,
)
from services.auction_engine.setup_engine import SetupCandidateEngine, _FailedWatch, _InitiationWatch
from services.auction_engine.opportunity_ledger import OpportunityLedger, OpportunityRecord
from services.auction_engine.setup_manager import SetupManager
from services.auction_engine.decision_engine import DecisionEngine
from services.auction_engine.checkpoint_codec import (
    decode_checkpoint_value,
    encode_checkpoint_value,
)
from services.auction_engine.compact_state import restore_dataclass, to_plain
from services.auction_engine.contracts import BarEvidence, SetupCandidate


@dataclass(frozen=True)
class _HistoryTrend:
    hma_order: str
    hma_spread_atr: Optional[float]
    ema_slow: Optional[float] = None
    ema_ref: Optional[float] = None


@dataclass(frozen=True)
class _HistoryEvidence:
    close: float
    bar: Any
    trend: _HistoryTrend
    atr: Optional[float] = None


class AuctionEngine:
    """Chronological pure Auction Engine.

    Incremental-state methods serialize the bounded Auction memory carried by
    the strict snapshot contract. They do not read or write a database table.
    """

    def __init__(
        self,
        config: AuctionEngineConfig = AUCTION_ENGINE_CONFIG,
    ) -> None:
        self.config = config
        self.evidence_builder = EvidenceBuilder(config)
        self.state_engine = AuctionStateEngine(config)
        self.boundary_engine = BoundaryEpisodeEngine(config)
        self.setup_engine = SetupCandidateEngine(config)
        self.opportunity_ledger = OpportunityLedger()
        self.setup_manager = SetupManager(config)
        self.decision_engine = DecisionEngine(config)
        self._history: Dict[str, Deque[Any]] = defaultdict(
            lambda: deque(maxlen=self.config.state.history_bars)
        )
        self._last_results: Dict[str, AuctionEngineResult] = {}
        self._last_input_hashes: Dict[str, str] = {}

    def reset(self, symbol: Optional[str] = None) -> None:
        if symbol is None:
            self._history.clear()
            self._last_results.clear()
            self._last_input_hashes.clear()
            self.state_engine.reset()
            self.boundary_engine.reset()
            self.setup_engine.reset()
            self.opportunity_ledger.reset()
            return
        key = str(symbol).strip().upper()
        self._history.pop(key, None)
        self._last_results.pop(key, None)
        self._last_input_hashes.pop(key, None)
        self.state_engine.reset(key)
        self.boundary_engine.reset(key)
        self.setup_engine.reset(key)
        self.opportunity_ledger.reset(key)

    def evaluate_snapshot(
        self,
        snapshot: SnapshotSchema,
        *,
        equity_ref: Optional[str] = None,
    ) -> AuctionEngineResult:
        if not isinstance(snapshot, SnapshotSchema):
            raise TypeError(
                "AuctionEngine.evaluate_snapshot requires a validated SnapshotSchema"
            )
        symbol, snapshot_time = _snapshot_identity(snapshot)
        input_hash = _snapshot_content_hash(snapshot)
        last_result = self._last_results[symbol] if symbol in self._last_results else None
        if last_result is not None:
            if (
                snapshot_time < last_result.snapshot_time
                and self.config.state.strict_chronology
            ):
                raise AuctionStateChronologyError(
                    f"Out-of-order snapshot for {symbol}: "
                    f"{snapshot_time} < {last_result.snapshot_time}"
                )
            if snapshot_time.date() != last_result.snapshot_time.date():
                self.reset(symbol)
                last_result = None
            elif snapshot_time == last_result.snapshot_time:
                prior_hash = (
                    self._last_input_hashes[symbol]
                    if symbol in self._last_input_hashes
                    else None
                )
                if prior_hash == input_hash:
                    return last_result
                if self.config.state.strict_chronology:
                    raise AuctionStateChronologyError(
                        f"Conflicting duplicate snapshot for {symbol} @ {snapshot_time}"
                    )

        history = tuple(self._history[symbol])
        evidence = self.evidence_builder.build(
            snapshot,
            history=history,
            equity_ref=equity_ref,
        )

        state_evaluation = self.state_engine.evaluate(evidence)
        boundary_evaluation = self.boundary_engine.evaluate(
            evidence,
            state_evaluation.state,
        )
        candidates = self.setup_engine.evaluate(
            evidence,
            state_evaluation.state,
            boundary_evaluation.episode,
            state_diagnostics=state_evaluation.diagnostics,
            closed_episode=boundary_evaluation.closed_episode,
        )
        ledger_records = self.opportunity_ledger.update(
            symbol,
            snapshot_time,
            candidates,
            boundary_episode=boundary_evaluation.episode,
            closed_episode=boundary_evaluation.closed_episode,
        )
        manager = self.setup_manager.evaluate(
            symbol,
            snapshot_time,
            ledger_records,
        )

        selected = None
        selected_record = None
        if manager.selected_candidate_id:
            for record in self.opportunity_ledger.active_eligible(symbol):
                candidate = (
                    record.candidates[manager.selected_candidate_id]
                    if manager.selected_candidate_id in record.candidates
                    else None
                )
                if candidate is not None and candidate.eligibility.value == "ELIGIBLE":
                    selected = candidate
                    selected_record = record
                    break

        local_decision = self.decision_engine.evaluate(
            manager=manager,
            selected=selected,
        )

        selected_key = None
        selection_recorded = False
        if manager.action is ManagerAction.SELECT and selected is not None:
            selected_key = selected.opportunity_key
            # Selection is an Auction fact, but it must be idempotent. Without
            # signal-level consumption the same live opportunity may remain
            # selected for several snapshots. Keep the first local selection
            # time so rotation diagnostics remain causal and stable.
            if selected_record is not None and selected_record.selected_time is None:
                self.opportunity_ledger.mark_selected(
                    selected.opportunity_key,
                    snapshot_time,
                    selected.candidate_id,
                )
                selection_recorded = True

        result = AuctionEngineResult(
            symbol=symbol,
            snapshot_time=snapshot_time,
            evidence=evidence,
            auction_state=state_evaluation.state,
            boundary_episode=boundary_evaluation.episode,
            candidates=candidates,
            stock_context=state_evaluation.stock_context,
            manager_decision=manager,
            local_decision=local_decision,
            diagnostics={
                "phase": "PURE_ANALYTICAL_CORE",
                "decision_scope": "LOCAL_AUCTION_ONLY",
                "signal_lifecycle_applied": False,
                "active_signal_context_applied": False,
                "opportunity_consumption_applied": False,
                "proposed_state": state_evaluation.proposed_state.value,
                "transitioned": state_evaluation.transitioned,
                "state_flags": state_evaluation.flags,
                "state_diagnostics": state_evaluation.diagnostics,
                "stock_context": state_evaluation.stock_context.to_storage_dict(exclude_none=False),
                "boundary_transitioned": boundary_evaluation.transitioned,
                "boundary_previous_status": (
                    boundary_evaluation.previous_status.value
                    if boundary_evaluation.previous_status is not None
                    else None
                ),
                "boundary_diagnostics": boundary_evaluation.diagnostics,
                "boundary_closed_episode": (
                    boundary_evaluation.closed_episode.to_storage_dict(
                        exclude_none=False
                    )
                    if boundary_evaluation.closed_episode is not None
                    else None
                ),
                "candidate_count": len(candidates),
                "unique_opportunity_count": len(
                    {item.opportunity_key for item in candidates}
                ),
                "candidate_ids": [item.candidate_id for item in candidates],
                "opportunity_keys": [item.opportunity_key for item in candidates],
                "candidate_families": [item.family.value for item in candidates],
                "candidate_eligibilities": [
                    item.eligibility.value for item in candidates
                ],
                "ledger_records": list(
                    self.opportunity_ledger.record_dicts(symbol)
                ),
                "ledger_events": [
                    item.to_dict()
                    for item in self.opportunity_ledger.events(symbol)
                ],
                "local_selected_opportunity_key": selected_key,
                "local_selection_recorded_now": selection_recorded,
                "local_action": local_decision.action.value,
            },
        )
        self._history[symbol].append(_compact_history_evidence(evidence))
        self._last_results[symbol] = result
        self._last_input_hashes[symbol] = input_hash
        return result

    def export_incremental_state(self, symbol: str) -> Dict[str, Any]:
        """Export the exact bounded state required by the next snapshot.

        The payload intentionally contains no engine/config/schema version
        metadata. The root snapshot carries the development version indicator;
        continuity compatibility is enforced by the strict SnapshotSchema shape
        and by direct restoration of every required field.
        """
        key = str(symbol or "").strip().upper()
        if not key:
            raise ValueError("Auction incremental-state symbol is required")

        state_memory = self.state_engine._memory[key] if key in self.state_engine._memory else None
        compact_state_memory = (
            replace(state_memory, last_evaluation=None)
            if state_memory is not None
            else None
        )
        now = state_memory.last_snapshot_time if state_memory is not None else None
        rotation_window = timedelta(
            minutes=float(self.config.decision.rotation_lookback_minutes)
        )
        terminal_states = {"INELIGIBLE", "EXPIRED", "SUPERSEDED", "CONSUMED"}
        ledger_records: Dict[str, Any] = {}
        for opportunity_key, record in self.opportunity_ledger._records.items():
            if record.symbol != key:
                continue
            selected_recently = bool(
                record.selected_time is not None
                and now is not None
                and record.selected_time <= now
                and now - record.selected_time <= rotation_window
            )
            if record.lifecycle_state in terminal_states and not selected_recently:
                continue

            kept_candidates = {
                candidate_id: candidate
                for candidate_id, candidate in record.candidates.items()
                if (
                    not candidate.terminal
                    or candidate_id == record.primary_candidate.candidate_id
                    or candidate_id == record.selected_candidate_id
                )
            }
            if record.primary_candidate.candidate_id not in kept_candidates:
                kept_candidates[record.primary_candidate.candidate_id] = record.primary_candidate
            compact_record = replace(
                record,
                candidates=kept_candidates,
                candidate_ids=list(record.candidate_ids),
                supporting_candidate_ids=list(record.supporting_candidate_ids),
            )
            record_payload = to_plain(compact_record)
            record_payload["primary_candidate_id"] = (
                compact_record.primary_candidate.candidate_id
            )
            del record_payload["primary_candidate"]
            record_payload["candidates"] = {
                candidate_id: _compact_candidate(candidate)
                for candidate_id, candidate in kept_candidates.items()
            }
            ledger_records[opportunity_key] = record_payload

        history = [
            {
                "close": float(item.close),
                "bar": to_plain(item.bar),
                "trend": to_plain(item.trend),
                "atr": item.atr,
            }
            for item in self._history[key]
        ]

        boundary_sequences = [
            {
                "structural_key": structural_key,
                "sequence": int(value),
            }
            for (sequence_symbol, structural_key), value
            in self.boundary_engine._sequences.items()
            if sequence_symbol == key
        ]

        return {
            "history": history,
            "state_memory": to_plain(compact_state_memory),
            "boundary_current": to_plain(
                self.boundary_engine._current[key]
                if key in self.boundary_engine._current
                else None
            ),
            "boundary_last_time": to_plain(
                self.boundary_engine._last_time[key]
                if key in self.boundary_engine._last_time
                else None
            ),
            "boundary_sequences": boundary_sequences,
            "boundary_last_terminal": to_plain(
                self.boundary_engine._last_terminal[key]
                if key in self.boundary_engine._last_terminal
                else None
            ),
            "setup_initiation": {
                item_key: to_plain(value)
                for item_key, value in self.setup_engine._initiation.items()
                if value.symbol == key
            },
            "setup_failed": {
                item_key: to_plain(value)
                for item_key, value in self.setup_engine._failed.items()
                if value.symbol == key
            },
            "setup_emitted_once": sorted(self.setup_engine._emitted_once),
            "setup_completed": sorted(self.setup_engine._completed),
            "setup_last_time": to_plain(
                self.setup_engine._last_time[key]
                if key in self.setup_engine._last_time
                else None
            ),
            "ledger_records": ledger_records,
            "ledger_last_day": to_plain(
                self.opportunity_ledger._last_day[key]
                if key in self.opportunity_ledger._last_day
                else None
            ),
        }

    def restore_incremental_state(
        self,
        symbol: str,
        payload: Mapping[str, Any],
    ) -> None:
        """Restore strict plain-JSON state from the previous snapshot."""
        key = str(symbol or "").strip().upper()
        if not key:
            raise ValueError("Auction incremental-state symbol is required")
        decoded = dict(payload)
        required = {
            "history",
            "state_memory",
            "boundary_current",
            "boundary_last_time",
            "boundary_sequences",
            "boundary_last_terminal",
            "setup_initiation",
            "setup_failed",
            "setup_emitted_once",
            "setup_completed",
            "setup_last_time",
            "ledger_records",
            "ledger_last_day",
        }
        missing = required.difference(decoded)
        extra = set(decoded).difference(required)
        if missing or extra:
            raise ValueError(
                f"Invalid Auction continuity keys; missing={sorted(missing)} extra={sorted(extra)}"
            )

        self.reset()
        restored_history = [
            _HistoryEvidence(
                close=float(item["close"]),
                bar=BarEvidence.model_validate(item["bar"]),
                trend=restore_dataclass(_HistoryTrend, item["trend"]),
                atr=(
                    float(item["atr"])
                    if "atr" in item and item["atr"] is not None
                    else None
                ),
            )
            for item in decoded["history"]
        ]
        self._history[key] = deque(
            restored_history,
            maxlen=self.config.state.history_bars,
        )

        state_memory = decoded["state_memory"]
        if state_memory is not None:
            self.state_engine._memory[key] = restore_dataclass(
                _StateMemory,
                state_memory,
            )

        boundary_current = decoded["boundary_current"]
        if boundary_current is not None:
            self.boundary_engine._current[key] = restore_dataclass(
                _EpisodeMemory,
                boundary_current,
                overrides={"trading_day": date},
            )
        boundary_last_time = decoded["boundary_last_time"]
        if boundary_last_time is not None:
            self.boundary_engine._last_time[key] = (
                boundary_last_time
                if isinstance(boundary_last_time, datetime)
                else datetime.fromisoformat(str(boundary_last_time))
            )
        self.boundary_engine._sequences = {
            (key, str(item["structural_key"])): int(item["sequence"])
            for item in decoded["boundary_sequences"]
        }
        boundary_last_terminal = decoded["boundary_last_terminal"]
        if boundary_last_terminal is not None:
            self.boundary_engine._last_terminal[key] = dict(boundary_last_terminal)

        self.setup_engine._initiation = {
            item_key: restore_dataclass(_InitiationWatch, value)
            for item_key, value in decoded["setup_initiation"].items()
        }
        self.setup_engine._failed = {
            item_key: restore_dataclass(_FailedWatch, value)
            for item_key, value in decoded["setup_failed"].items()
        }
        self.setup_engine._emitted_once = set(decoded["setup_emitted_once"])
        self.setup_engine._completed = set(decoded["setup_completed"])
        setup_last_time = decoded["setup_last_time"]
        if setup_last_time is not None:
            self.setup_engine._last_time[key] = (
                setup_last_time
                if isinstance(setup_last_time, datetime)
                else datetime.fromisoformat(str(setup_last_time))
            )

        restored_records: Dict[str, OpportunityRecord] = {}
        for item_key, value in decoded["ledger_records"].items():
            record_payload = dict(value)
            primary_candidate_id = str(record_payload["primary_candidate_id"])
            del record_payload["primary_candidate_id"]
            candidate_payloads = record_payload["candidates"]
            restored_candidates = {
                candidate_id: _restore_candidate(
                    candidate_payload,
                    config_version=self.config.engine.config_version,
                )
                for candidate_id, candidate_payload in candidate_payloads.items()
            }
            if primary_candidate_id not in restored_candidates:
                raise ValueError(
                    f"Primary candidate is missing from Auction continuity: "
                    f"{primary_candidate_id}"
                )
            record_payload["candidates"] = {
                candidate_id: candidate.model_dump(mode="python")
                for candidate_id, candidate in restored_candidates.items()
            }
            record_payload["primary_candidate"] = restored_candidates[
                primary_candidate_id
            ].model_dump(mode="python")
            restored_records[item_key] = restore_dataclass(
                OpportunityRecord,
                record_payload,
            )
        self.opportunity_ledger._records = restored_records
        ledger_last_day = decoded["ledger_last_day"]
        if ledger_last_day is not None:
            self.opportunity_ledger._last_day[key] = (
                ledger_last_day
                if isinstance(ledger_last_day, date)
                else date.fromisoformat(str(ledger_last_day))
            )

    def export_checkpoint(self, symbol: str) -> Dict[str, Any]:
        """Export complete recoverable state for one symbol.

        The live runner uses one AuctionEngine instance per symbol, so these
        component collections contain only that symbol's current-day state.
        """
        key = str(symbol or "").strip().upper()
        if not key:
            raise ValueError("Checkpoint symbol is required")
        payload = {
            "checkpoint_schema": "AUCTION_ENGINE_STATE_V1",
            "engine_name": self.config.engine.engine_name,
            "engine_version": self.config.engine.engine_version,
            "config_version": self.config.engine.config_version,
            "symbol": key,
            "history": list(self._history[key]) if key in self._history else [],
            "last_result": self._last_results[key] if key in self._last_results else None,
            "last_input_hash": (
                self._last_input_hashes[key]
                if key in self._last_input_hashes
                else None
            ),
            "state_memory": dict(self.state_engine._memory),
            "boundary_current": dict(self.boundary_engine._current),
            "boundary_last_time": dict(self.boundary_engine._last_time),
            "boundary_sequences": dict(self.boundary_engine._sequences),
            "boundary_last_terminal": dict(self.boundary_engine._last_terminal),
            "setup_initiation": dict(self.setup_engine._initiation),
            "setup_failed": dict(self.setup_engine._failed),
            "setup_emitted_once": set(self.setup_engine._emitted_once),
            "setup_completed": set(self.setup_engine._completed),
            "setup_last_time": dict(self.setup_engine._last_time),
            "ledger_records": dict(self.opportunity_ledger._records),
            "ledger_events": list(self.opportunity_ledger._events),
            "ledger_last_day": dict(self.opportunity_ledger._last_day),
        }
        return encode_checkpoint_value(payload)

    def restore_checkpoint(self, symbol: str, payload: Mapping[str, Any]) -> None:
        """Restore a checkpoint previously produced by ``export_checkpoint``."""
        key = str(symbol or "").strip().upper()
        decoded = decode_checkpoint_value(dict(payload))
        if not isinstance(decoded, dict):
            raise ValueError("Auction checkpoint root must be a mapping")
        expected = {
            "checkpoint_schema": "AUCTION_ENGINE_STATE_V1",
            "engine_name": self.config.engine.engine_name,
            "engine_version": self.config.engine.engine_version,
            "config_version": self.config.engine.config_version,
            "symbol": key,
        }
        required = set(expected) | {
            "history", "last_result", "last_input_hash", "state_memory",
            "boundary_current", "boundary_last_time", "boundary_sequences",
            "boundary_last_terminal", "setup_initiation", "setup_failed",
            "setup_emitted_once", "setup_completed", "setup_last_time",
            "ledger_records", "ledger_events", "ledger_last_day",
        }
        missing = required.difference(decoded)
        extra = set(decoded).difference(required)
        if missing or extra:
            raise ValueError(
                f"Invalid Auction checkpoint keys; "
                f"missing={sorted(missing)} extra={sorted(extra)}"
            )
        for field, value in expected.items():
            actual = decoded[field]
            if actual != value:
                raise ValueError(
                    f"Auction checkpoint mismatch for {field}: "
                    f"{actual!r} != {value!r}"
                )

        self.reset()
        self._history[key] = deque(
            decoded["history"],
            maxlen=self.config.state.history_bars,
        )
        if decoded["last_result"] is not None:
            self._last_results[key] = decoded["last_result"]
        if decoded["last_input_hash"] is not None:
            self._last_input_hashes[key] = decoded["last_input_hash"]

        self.state_engine._memory = dict(decoded["state_memory"])
        self.boundary_engine._current = dict(decoded["boundary_current"])
        self.boundary_engine._last_time = dict(decoded["boundary_last_time"])
        self.boundary_engine._sequences = dict(decoded["boundary_sequences"])
        self.boundary_engine._last_terminal = dict(decoded["boundary_last_terminal"])
        self.setup_engine._initiation = dict(decoded["setup_initiation"])
        self.setup_engine._failed = dict(decoded["setup_failed"])
        self.setup_engine._emitted_once = set(decoded["setup_emitted_once"])
        self.setup_engine._completed = set(decoded["setup_completed"])
        self.setup_engine._last_time = dict(decoded["setup_last_time"])
        self.opportunity_ledger._records = dict(decoded["ledger_records"])
        self.opportunity_ledger._events = list(decoded["ledger_events"])
        self.opportunity_ledger._last_day = dict(decoded["ledger_last_day"])

    def evaluate_many(
        self,
        snapshots: Iterable[SnapshotSchema],
        *,
        equity_refs: Optional[Mapping[str, str]] = None,
    ) -> List[AuctionEngineResult]:
        results: List[AuctionEngineResult] = []
        refs = equity_refs if equity_refs is not None else {}
        for snapshot in snapshots:
            symbol, _ = _snapshot_identity(snapshot)
            results.append(
                self.evaluate_snapshot(
                    snapshot,
                    equity_ref=refs[symbol] if symbol in refs else None,
                )
            )
        return results


def _compact_candidate(candidate: SetupCandidate) -> Dict[str, Any]:
    """Persist only candidate facts required for lifecycle continuation."""
    payload = to_plain(candidate)
    for field in (
        "supporting_evidence",
        "opposing_evidence",
        "confidence_channels",
        "diagnostics",
        "config_version",
    ):
        if field in payload:
            del payload[field]
    return payload


def _restore_candidate(
    payload: Mapping[str, Any],
    *,
    config_version: str,
) -> SetupCandidate:
    """Restore a compact candidate without persisted diagnostics/version data."""
    candidate_payload = dict(payload)
    candidate_payload["supporting_evidence"] = []
    candidate_payload["opposing_evidence"] = []
    candidate_payload["confidence_channels"] = []
    candidate_payload["diagnostics"] = {}
    candidate_payload["config_version"] = config_version
    return SetupCandidate.model_validate(candidate_payload)


def _compact_history_evidence(evidence: EvidenceSnapshot) -> _HistoryEvidence:
    """Keep only the three historical facts read by EvidenceBuilder."""
    return _HistoryEvidence(
        close=evidence.close,
        bar=evidence.bar,
        trend=_HistoryTrend(
            hma_order=evidence.trend.hma_order,
            hma_spread_atr=evidence.trend.hma_spread_atr,
            ema_slow=evidence.trend.ema_slow,
            ema_ref=evidence.trend.ema_ref,
        ),
        atr=evidence.atr,
    )


def _snapshot_identity(snapshot: SnapshotSchema) -> tuple[str, datetime]:
    key = snapshot.symbol.strip().upper()
    if not key:
        raise ValueError("Snapshot symbol is required")
    return key, snapshot.snapshot_time


def _snapshot_content_hash(snapshot: SnapshotSchema) -> str:
    data = snapshot.model_dump(mode="json", by_alias=True)
    payload = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = ["AuctionEngine"]
