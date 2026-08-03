"""Objective observation provider for the authoritative Auction lifecycle.

The provider classifies completed-candle evidence into causal directional
observations.  It owns only observation continuity (trend establishment,
current-leg geometry, pause/reacceleration context, exhaustion and structural
protection).  Persistent Episode Engine remains the sole lifecycle/event
authority.  No setup, opportunity, signal or legacy Auction state is used.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, Optional, Sequence, Tuple

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG, AuctionEngineConfig
from enums.auction_engine import (
    AuctionStateName,
    DirectionalBias,
    DirectionalEfficiencySource,
)
from schemas.snapshot import SnapshotSchema
from services.auction_engine.contracts import EvidenceSnapshot
from services.auction_engine.episode_contracts import (
    AuctionObservation,
    DirectionalObservationMemory,
)


_DIRECTIONAL_STATES = {
    AuctionStateName.FRESH_EXPANSION,
    AuctionStateName.ORDERLY_UPTREND,
    AuctionStateName.ORDERLY_DOWNTREND,
    AuctionStateName.CONTROLLED_PULLBACK,
    AuctionStateName.RECOMPRESSION,
    AuctionStateName.REACCELERATION,
    AuctionStateName.MATURE_EXTENSION,
    AuctionStateName.TREND_FAILURE,
    AuctionStateName.REVERSAL,
}
_PAUSE_STATES = {
    AuctionStateName.CONTROLLED_PULLBACK,
    AuctionStateName.RECOMPRESSION,
}


@dataclass
class _ObservationMemory:
    trading_day: Optional[date]
    last_snapshot_time: Optional[datetime]
    observation_count: int
    current_state: AuctionStateName
    state_age_bars: int
    pending_state: Optional[AuctionStateName]
    pending_bars: int
    trend_candidate_side: DirectionalBias
    trend_candidate_bars: int
    established_side: DirectionalBias
    trend_onset_time: Optional[datetime]
    trend_anchor_price: Optional[float]
    trend_extreme_price: Optional[float]
    protection_level: Optional[float]
    protection_source: str
    protection_time: Optional[datetime]
    leg_anchor_time: Optional[datetime]
    leg_anchor_price: Optional[float]
    leg_extreme_price: Optional[float]
    leg_age_bars: int
    leg_no_progress_bars: int
    leg_maturity_consumed: bool
    leg_maturity_onset_time: Optional[datetime]
    leg_maturity_extreme_price: Optional[float]
    pullback_candidate_bars: int
    pullback_age_bars: int
    pullback_extreme_price: Optional[float]
    compression_candidate_bars: int
    compression_box_low: Optional[float]
    compression_box_high: Optional[float]
    recompression_age_bars: int
    reacceleration_age_bars: int
    exhaustion_active: bool
    exhaustion_side: DirectionalBias
    exhaustion_started_at: Optional[datetime]
    exhaustion_last_seen_at: Optional[datetime]
    exhaustion_age_bars: int
    exhaustion_clear_bars: int
    failure_breach_bars: int
    trend_failure_age_bars: int
    reversal_confirmation_bars: int
    reversal_side: DirectionalBias
    last_close: Optional[float]
    last_reason_codes: Tuple[str, ...]


class AuctionObservationProvider:
    """Advance strict objective observation continuity for one symbol-day."""

    def __init__(self, config: AuctionEngineConfig = AUCTION_ENGINE_CONFIG) -> None:
        self.config = config
        self.state_cfg = config.state
        self.evidence_cfg = config.evidence
        self.balance_cfg = config.episode.balance
        self._memory: Dict[str, _ObservationMemory] = {}

    def reset(self, symbol: Optional[str] = None) -> None:
        if symbol is None:
            self._memory.clear()
            return
        self._memory.pop(self._symbol_key(symbol), None)

    def restore_memory(
        self,
        symbol: str,
        payload: DirectionalObservationMemory | dict,
    ) -> None:
        key = self._symbol_key(symbol)
        memory = (
            payload
            if isinstance(payload, DirectionalObservationMemory)
            else DirectionalObservationMemory.model_validate(payload)
        )
        self._memory[key] = _ObservationMemory(**memory.model_dump(mode="python"))

    def export_memory(self, symbol: str) -> DirectionalObservationMemory:
        key = self._symbol_key(symbol)
        if key not in self._memory:
            raise ValueError(f"No objective observation memory exists for {key}")
        return DirectionalObservationMemory.model_validate(vars(self._memory[key]))

    def resolve_exhaustion_after_trend_restoration(
        self,
        symbol: str,
        *,
        snapshot_time: datetime,
        restored_side: DirectionalBias,
    ) -> bool:
        """Clear active exhaustion only when its side is explicitly restored.

        Persistent Episode Engine owns the restoration event.  The observation
        provider owns the active exhaustion context, so Auction orchestration
        calls this method only after a creation-capable
        ``DIRECTIONAL_TREND_RESTORED`` event has been emitted.  Historical
        exhaustion remains preserved in that event; only the active blocker is
        resolved for the restored parent trend.
        """
        key = self._symbol_key(symbol)
        memory = self._memory.get(key)
        if memory is None:
            return False
        if memory.last_snapshot_time != snapshot_time:
            return False
        if restored_side not in (DirectionalBias.UP, DirectionalBias.DOWN):
            return False
        if not memory.exhaustion_active:
            return False
        if memory.exhaustion_side is not restored_side:
            return False

        self._clear_exhaustion(memory)
        memory.last_reason_codes = self._unique(
            (*memory.last_reason_codes, "EXHAUSTION_RESOLVED_BY_TREND_RESTORATION")
        )
        return True

    @staticmethod
    def initial_memory() -> DirectionalObservationMemory:
        return DirectionalObservationMemory(
            trading_day=None,
            last_snapshot_time=None,
            observation_count=0,
            current_state=AuctionStateName.UNKNOWN,
            state_age_bars=0,
            pending_state=None,
            pending_bars=0,
            trend_candidate_side=DirectionalBias.UNKNOWN,
            trend_candidate_bars=0,
            established_side=DirectionalBias.UNKNOWN,
            trend_onset_time=None,
            trend_anchor_price=None,
            trend_extreme_price=None,
            protection_level=None,
            protection_source="",
            protection_time=None,
            leg_anchor_time=None,
            leg_anchor_price=None,
            leg_extreme_price=None,
            leg_age_bars=0,
            leg_no_progress_bars=0,
            leg_maturity_consumed=False,
            leg_maturity_onset_time=None,
            leg_maturity_extreme_price=None,
            pullback_candidate_bars=0,
            pullback_age_bars=0,
            pullback_extreme_price=None,
            compression_candidate_bars=0,
            compression_box_low=None,
            compression_box_high=None,
            recompression_age_bars=0,
            reacceleration_age_bars=0,
            exhaustion_active=False,
            exhaustion_side=DirectionalBias.UNKNOWN,
            exhaustion_started_at=None,
            exhaustion_last_seen_at=None,
            exhaustion_age_bars=0,
            exhaustion_clear_bars=0,
            failure_breach_bars=0,
            trend_failure_age_bars=0,
            reversal_confirmation_bars=0,
            reversal_side=DirectionalBias.UNKNOWN,
            last_close=None,
            last_reason_codes=(),
        )

    @classmethod
    def _new_memory(cls) -> _ObservationMemory:
        return _ObservationMemory(**cls.initial_memory().model_dump(mode="python"))

    def build(
        self,
        snapshot: SnapshotSchema,
        evidence: EvidenceSnapshot,
    ) -> AuctionObservation:
        if not isinstance(snapshot, SnapshotSchema):
            raise TypeError("AuctionObservationProvider requires SnapshotSchema")
        if not isinstance(evidence, EvidenceSnapshot):
            raise TypeError("AuctionObservationProvider requires EvidenceSnapshot")
        symbol = self._symbol_key(snapshot.symbol)
        if symbol != evidence.symbol:
            raise ValueError("Snapshot and evidence symbols do not match")
        if snapshot.snapshot_time != evidence.snapshot_time:
            raise ValueError("Snapshot and evidence times do not match")
        if evidence.atr is None or evidence.atr <= 0.0:
            raise ValueError("Auction observation requires positive ATR")

        memory = self._memory.setdefault(symbol, self._new_memory())
        self._validate_chronology(memory, evidence)
        flags = self._condition_flags(snapshot, evidence, memory)
        proposed, proposal_reasons = self._propose_state(memory, flags)
        selected, transitioned, policy_reasons = self._apply_transition_policy(
            memory,
            proposed,
            flags,
        )
        previous = memory.current_state
        if transitioned:
            self._on_transition(memory, evidence, previous, selected, flags)
            memory.current_state = selected
            memory.state_age_bars = 1
        else:
            if memory.observation_count == 0:
                memory.current_state = selected
                memory.state_age_bars = 1
            else:
                memory.state_age_bars += 1
            self._advance_active_ages(memory)

        self._update_exhaustion_context(memory, evidence, flags)
        memory.trading_day = evidence.trading_day
        memory.last_snapshot_time = evidence.snapshot_time
        memory.observation_count += 1
        memory.last_close = evidence.close
        reasons = self._unique(
            list(proposal_reasons)
            + list(policy_reasons)
            + [f"OBSERVATION_STATE_{memory.current_state.value}"]
        )
        memory.last_reason_codes = reasons

        accepted_fields = self._accepted_range(snapshot, evidence)
        protection_level = memory.protection_level
        protection_source = memory.protection_source
        efficiency, efficiency_source = self._directional_efficiency(evidence)
        directional_bias = (
            memory.established_side
            if memory.established_side in (DirectionalBias.UP, DirectionalBias.DOWN)
            else flags["observed_side"]
        )

        source_reasons = list(reasons)
        source_reasons.extend(evidence.reason_codes)
        if evidence.price_action.rejection:
            source_reasons.append("OBJECTIVE_REJECTION_OBSERVED")
        if evidence.price_action.failed_extreme:
            source_reasons.append("OBJECTIVE_FAILED_EXTREME_OBSERVED")
        if flags["structural_failure_confirmed"]:
            source_reasons.append("OBJECTIVE_STRUCTURAL_FAILURE_OBSERVED")
        if accepted_fields["accepted_range_id"] is not None:
            source_reasons.append("OBJECTIVE_ACCEPTED_RANGE_GEOMETRY_PRESENT")
        if protection_level is not None:
            source_reasons.append("OBJECTIVE_TREND_PROTECTION_GEOMETRY_PRESENT")

        return AuctionObservation(
            symbol=evidence.symbol,
            trading_day=evidence.trading_day,
            snapshot_time=evidence.snapshot_time,
            close=evidence.close,
            high=evidence.bar.high,
            low=evidence.bar.low,
            atr=float(evidence.atr),
            observation_state=memory.current_state,
            directional_bias=directional_bias,
            trend_direction=evidence.trend.direction,
            current_leg_mature=bool(flags["current_leg_mature"]),
            extension_mature=bool(evidence.extension.mature is True),
            exhaustion_active=memory.exhaustion_active,
            exhausted_side=memory.exhaustion_side,
            rejection_observed=bool(evidence.price_action.rejection),
            failed_extreme_observed=bool(evidence.price_action.failed_extreme),
            structural_failure_confirmed=bool(flags["structural_failure_confirmed"]),
            trend_protection_level=protection_level,
            trend_protection_source=protection_source,
            trend_protection_time=memory.protection_time,
            accepted_range_id=accepted_fields["accepted_range_id"],
            accepted_range_low=accepted_fields["accepted_range_low"],
            accepted_range_high=accepted_fields["accepted_range_high"],
            accepted_range_established_at=accepted_fields[
                "accepted_range_established_at"
            ],
            accepted_range_provisional=accepted_fields[
                "accepted_range_provisional"
            ],
            accepted_range_breakout_eligible=accepted_fields[
                "accepted_range_breakout_eligible"
            ],
            accepted_range_inside=accepted_fields["accepted_range_inside"],
            accepted_range_position=accepted_fields["accepted_range_position"],
            accepted_range_outside_atr=accepted_fields[
                "accepted_range_outside_atr"
            ],
            range_width_atr=accepted_fields["range_width_atr"],
            directional_efficiency=efficiency,
            directional_efficiency_source=efficiency_source,
            overlap_ratio=evidence.price_action.overlap_ratio,
            source_reason_codes=self._unique(source_reasons),
        )

    def _condition_flags(
        self,
        snapshot: SnapshotSchema,
        evidence: EvidenceSnapshot,
        memory: _ObservationMemory,
    ) -> dict:
        atr = float(evidence.atr)
        bar = evidence.bar
        move_atr = float(bar.move_atr or 0.0)
        body = float(bar.body_fraction or 0.0)
        close_position = float(bar.close_position or 0.5)
        strong_body = body >= self.evidence_cfg.strong_bar_body_fraction
        strong_up = bool(
            move_atr >= self.evidence_cfg.strong_bar_move_atr
            and strong_body
            and close_position >= self.evidence_cfg.directional_close_position
        )
        strong_down = bool(
            move_atr <= -self.evidence_cfg.strong_bar_move_atr
            and strong_body
            and close_position
            <= (1.0 - self.evidence_cfg.directional_close_position)
        )

        support_up = self._trend_support_count(snapshot, evidence, DirectionalBias.UP)
        support_down = self._trend_support_count(
            snapshot, evidence, DirectionalBias.DOWN
        )
        efficiency = evidence.trend.directional_efficiency
        trend_up = bool(
            evidence.trend.direction is DirectionalBias.UP
            and (efficiency is None or efficiency >= self.state_cfg.orderly_trend_efficiency_min)
            and support_up >= 2
        )
        trend_down = bool(
            evidence.trend.direction is DirectionalBias.DOWN
            and (efficiency is None or efficiency >= self.state_cfg.orderly_trend_efficiency_min)
            and support_down >= 2
        )
        observed_side = (
            DirectionalBias.UP
            if trend_up and not trend_down
            else DirectionalBias.DOWN
            if trend_down and not trend_up
            else DirectionalBias.UNKNOWN
        )
        self._update_trend_candidate(memory, observed_side)

        established = memory.established_side
        raw_side = self._direction_from_text(snapshot.structure.raw.side)
        structure_loss = False
        adverse_to_trend = False
        trend_resume = False
        opposite_displacement = False
        opposite_support = 0
        if established is DirectionalBias.UP:
            structure_loss = bool(
                raw_side is DirectionalBias.DOWN
                or (
                    evidence.trend.retained_structure is False
                    and evidence.trend.direction is DirectionalBias.UP
                )
            )
            adverse_to_trend = bool(
                bar.direction is DirectionalBias.DOWN
                and abs(move_atr) <= self.state_cfg.controlled_pullback_max_adverse_atr
                and not structure_loss
            )
            trend_resume = bool(
                (strong_up or move_atr >= self.state_cfg.reacceleration_displacement_atr)
                and evidence.trend.direction is not DirectionalBias.DOWN
                and support_up >= 2
            )
            opposite_displacement = bool(
                move_atr <= -self.state_cfg.trend_failure_opposite_displacement_atr
            )
            opposite_support = support_down
        elif established is DirectionalBias.DOWN:
            structure_loss = bool(
                raw_side is DirectionalBias.UP
                or (
                    evidence.trend.retained_structure is False
                    and evidence.trend.direction is DirectionalBias.DOWN
                )
            )
            adverse_to_trend = bool(
                bar.direction is DirectionalBias.UP
                and abs(move_atr) <= self.state_cfg.controlled_pullback_max_adverse_atr
                and not structure_loss
            )
            trend_resume = bool(
                (strong_down or move_atr <= -self.state_cfg.reacceleration_displacement_atr)
                and evidence.trend.direction is not DirectionalBias.UP
                and support_down >= 2
            )
            opposite_displacement = bool(
                move_atr >= self.state_cfg.trend_failure_opposite_displacement_atr
            )
            opposite_support = support_up

        memory.pullback_candidate_bars = (
            memory.pullback_candidate_bars + 1 if adverse_to_trend else 0
        )
        pullback_ready = (
            memory.pullback_candidate_bars >= self.state_cfg.pullback_confirmation_bars
        )

        compression_observed = bool(
            evidence.compression.compressed is True
            and not strong_up
            and not strong_down
            and abs(move_atr) <= self.evidence_cfg.compression_max_bar_move_atr
        )
        if compression_observed:
            # The candidate box is objective geometry accumulated only until
            # recompression is confirmed.  Once RECOMPRESSION is active the
            # box is frozen; expanding it with later bars would move the
            # structural protection level and delay an otherwise valid
            # failure observation.
            if memory.current_state is not AuctionStateName.RECOMPRESSION:
                memory.compression_candidate_bars += 1
                memory.compression_box_low = min(
                    memory.compression_box_low or evidence.bar.low,
                    evidence.bar.low,
                )
                memory.compression_box_high = max(
                    memory.compression_box_high or evidence.bar.high,
                    evidence.bar.high,
                )
        elif memory.current_state is not AuctionStateName.RECOMPRESSION:
            memory.compression_candidate_bars = 0
            memory.compression_box_low = None
            memory.compression_box_high = None
        compression_ready = (
            memory.compression_candidate_bars
            >= self.state_cfg.recompression_confirmation_bars
        )

        if memory.current_state in {
            AuctionStateName.CONTROLLED_PULLBACK,
            AuctionStateName.RECOMPRESSION,
        }:
            if established is DirectionalBias.UP:
                memory.pullback_extreme_price = min(
                    memory.pullback_extreme_price or evidence.bar.low,
                    evidence.bar.low,
                )
            elif established is DirectionalBias.DOWN:
                memory.pullback_extreme_price = max(
                    memory.pullback_extreme_price or evidence.bar.high,
                    evidence.bar.high,
                )

        self._update_current_leg(memory, evidence)
        leg = self._leg_metrics(memory, evidence)
        progress_or_rejection = bool(
            memory.leg_no_progress_bars >= self.state_cfg.current_leg_no_progress_bars
            or evidence.price_action.rejection
            or evidence.price_action.failed_extreme
            or (
                evidence.extension.progress_decay is not None
                and evidence.extension.progress_decay
                >= self.evidence_cfg.extension_progress_decay_min
            )
        )
        current_leg_mature = bool(
            established in (DirectionalBias.UP, DirectionalBias.DOWN)
            and not memory.leg_maturity_consumed
            and memory.leg_age_bars >= self.state_cfg.current_leg_min_bars_for_maturity
            and leg[0] is not None
            and leg[0] >= self.state_cfg.current_leg_extension_atr
            and leg[1] is not None
            and leg[1] >= self.state_cfg.current_leg_current_extension_atr
            and leg[2] is not None
            and leg[2] <= self.state_cfg.current_leg_max_retracement_atr
            and leg[3] is not None
            and leg[3] <= self.state_cfg.current_leg_max_retracement_fraction
            and progress_or_rejection
            and not pullback_ready
        )

        protection_breached = self._protection_breached(memory, evidence)
        failure_observed = bool(
            established in (DirectionalBias.UP, DirectionalBias.DOWN)
            and (
                protection_breached
                or opposite_displacement
                or (
                    bar.direction is self._opposite(established)
                    and opposite_support >= 3
                    and abs(move_atr) >= self.state_cfg.reacceleration_displacement_atr
                )
            )
        )
        if failure_observed and protection_breached:
            memory.failure_breach_bars += 1
        elif not protection_breached:
            memory.failure_breach_bars = 0
        structural_failure_confirmed = bool(
            memory.failure_breach_bars
            >= self.state_cfg.failure_level_confirmation_bars
        )

        reversal_observed = bool(
            memory.current_state is AuctionStateName.TREND_FAILURE
            and observed_side is self._opposite(established)
            and opposite_support >= 2
            and (
                evidence.price_action.followthrough
                or strong_up
                or strong_down
                or abs(move_atr) >= self.state_cfg.reacceleration_displacement_atr
            )
        )
        memory.reversal_confirmation_bars = (
            memory.reversal_confirmation_bars + 1 if reversal_observed else 0
        )
        reversal_ready = (
            memory.reversal_confirmation_bars
            >= self.state_cfg.reversal_confirmation_bars
        )
        if reversal_ready:
            memory.reversal_side = self._opposite(established)

        enough_history = (
            memory.observation_count + 1 >= self.evidence_cfg.minimum_history_bars
        )
        return {
            "enough_history": enough_history,
            "observed_side": observed_side,
            "trend_candidate_ready": bool(
                memory.trend_candidate_side
                in (DirectionalBias.UP, DirectionalBias.DOWN)
                and memory.trend_candidate_bars >= self.state_cfg.trend_establishment_bars
            ),
            "strong_up": strong_up,
            "strong_down": strong_down,
            "pullback_ready": pullback_ready,
            "compression_ready": compression_ready,
            "trend_resume": trend_resume,
            "current_leg_mature": current_leg_mature,
            "leg_distance_atr": leg[0],
            "progress_or_rejection": progress_or_rejection,
            "structural_failure_confirmed": structural_failure_confirmed,
            "reversal_ready": reversal_ready,
            "move_atr": move_atr,
        }

    def _propose_state(
        self,
        memory: _ObservationMemory,
        flags: dict,
    ) -> Tuple[AuctionStateName, Tuple[str, ...]]:
        current = memory.current_state
        established = memory.established_side
        if not flags["enough_history"]:
            return AuctionStateName.UNKNOWN, ("WAITING_FOR_MINIMUM_HISTORY",)
        if current is AuctionStateName.TREND_FAILURE:
            if flags["reversal_ready"]:
                return AuctionStateName.REVERSAL, (
                    "OPPOSITE_FOLLOWTHROUGH_CONFIRMED_AFTER_TREND_FAILURE",
                )
            return AuctionStateName.TREND_FAILURE, (
                "TREND_FAILURE_OBSERVATION_REMAINS_UNRESOLVED",
            )
        if current is AuctionStateName.REVERSAL:
            if established in (DirectionalBias.UP, DirectionalBias.DOWN):
                return self._orderly_state(established), (
                    "REVERSAL_OBSERVATION_GRADUATING_TO_ORDERLY_TREND",
                )
            return AuctionStateName.REVERSAL, ("REVERSAL_OBSERVATION_HELD",)
        if flags["structural_failure_confirmed"]:
            return AuctionStateName.TREND_FAILURE, (
                "FROZEN_TREND_PROTECTION_BREACH_CONFIRMED",
            )
        if current in _PAUSE_STATES and flags["trend_resume"]:
            return AuctionStateName.REACCELERATION, (
                "FRESH_DISPLACEMENT_AFTER_CONFIRMED_TREND_PAUSE",
            )
        if established in (DirectionalBias.UP, DirectionalBias.DOWN):
            if flags["pullback_ready"]:
                return AuctionStateName.CONTROLLED_PULLBACK, (
                    "MULTIBAR_CONTROLLED_ADVERSE_MOVE",
                )
            if (
                flags["compression_ready"]
                and current in _DIRECTIONAL_STATES
                and not flags["trend_resume"]
            ):
                return AuctionStateName.RECOMPRESSION, (
                    "PERSISTENT_COMPACT_VALUE_INSIDE_ESTABLISHED_TREND",
                )
            if (
                current is AuctionStateName.CONTROLLED_PULLBACK
                and memory.pullback_age_bars < self.state_cfg.pullback_max_bars
            ):
                return current, ("CONTROLLED_PULLBACK_OBSERVATION_ACTIVE",)
            if (
                current is AuctionStateName.RECOMPRESSION
                and memory.recompression_age_bars
                < self.state_cfg.recompression_max_bars
            ):
                return current, ("RECOMPRESSION_OBSERVATION_ACTIVE",)
            if flags["current_leg_mature"]:
                return AuctionStateName.MATURE_EXTENSION, (
                    "CURRENT_DIRECTIONAL_LEG_MATURED",
                )
            return self._orderly_state(established), (
                "ESTABLISHED_DIRECTIONAL_CONTEXT_RETAINED",
            )
        if flags["trend_candidate_ready"]:
            return self._orderly_state(memory.trend_candidate_side), (
                "DIRECTIONAL_PROGRESS_CONFIRMED",
            )
        return AuctionStateName.UNKNOWN, ("NO_DIRECTIONAL_OBSERVATION_CONFIRMED",)

    def _apply_transition_policy(
        self,
        memory: _ObservationMemory,
        proposed: AuctionStateName,
        flags: dict,
    ) -> Tuple[AuctionStateName, bool, Tuple[str, ...]]:
        current = memory.current_state
        if proposed is current:
            memory.pending_state = None
            memory.pending_bars = 0
            return current, False, ("OBSERVATION_PROPOSAL_MATCHED_CURRENT",)
        urgent = proposed in {
            AuctionStateName.REACCELERATION,
            AuctionStateName.TREND_FAILURE,
            AuctionStateName.REVERSAL,
        }
        if (
            memory.observation_count > 0
            and memory.state_age_bars < self._minimum_dwell(current)
            and not urgent
        ):
            return current, False, (
                f"MINIMUM_DWELL_{current.value}_{self._minimum_dwell(current)}_BARS",
            )
        required = self._confirmation_bars(current, proposed)
        if memory.pending_state is proposed:
            memory.pending_bars += 1
        else:
            memory.pending_state = proposed
            memory.pending_bars = 1
        if memory.pending_bars < required:
            return current, False, (
                f"AWAITING_{required}_BAR_CONFIRMATION_FOR_{proposed.value}",
            )
        memory.pending_state = None
        memory.pending_bars = 0
        return proposed, True, ("OBSERVATION_TRANSITION_CONFIRMED",)

    def _on_transition(
        self,
        memory: _ObservationMemory,
        evidence: EvidenceSnapshot,
        previous: AuctionStateName,
        selected: AuctionStateName,
        flags: dict,
    ) -> None:
        if selected in {
            AuctionStateName.ORDERLY_UPTREND,
            AuctionStateName.ORDERLY_DOWNTREND,
        }:
            side = (
                DirectionalBias.UP
                if selected is AuctionStateName.ORDERLY_UPTREND
                else DirectionalBias.DOWN
            )
            if memory.established_side is DirectionalBias.UNKNOWN:
                self._establish_direction(memory, evidence, side)
            if previous is AuctionStateName.REVERSAL:
                memory.pullback_candidate_bars = 0
                memory.compression_candidate_bars = 0
        elif selected is AuctionStateName.CONTROLLED_PULLBACK:
            memory.pullback_age_bars = 1
            memory.pullback_extreme_price = (
                evidence.bar.low
                if memory.established_side is DirectionalBias.UP
                else evidence.bar.high
            )
            memory.recompression_age_bars = 0
        elif selected is AuctionStateName.RECOMPRESSION:
            memory.recompression_age_bars = 1
        elif selected is AuctionStateName.REACCELERATION:
            anchor = memory.pullback_extreme_price
            source = "CONFIRMED_PULLBACK_EXTREME"
            if previous is AuctionStateName.RECOMPRESSION:
                anchor = (
                    memory.compression_box_low
                    if memory.established_side is DirectionalBias.UP
                    else memory.compression_box_high
                )
                source = "CONFIRMED_RECOMPRESSION_BOX"
            if anchor is not None:
                self._promote_protection(
                    memory,
                    evidence,
                    anchor,
                    source,
                )
            self._reset_leg(memory, evidence, memory.established_side, anchor)
            memory.pullback_candidate_bars = 0
            memory.pullback_age_bars = 0
            memory.pullback_extreme_price = None
            memory.compression_candidate_bars = 0
            memory.compression_box_low = None
            memory.compression_box_high = None
            memory.recompression_age_bars = 0
            memory.reacceleration_age_bars = 1
        elif selected is AuctionStateName.MATURE_EXTENSION:
            memory.leg_maturity_consumed = True
            memory.leg_maturity_onset_time = evidence.snapshot_time
            memory.leg_maturity_extreme_price = memory.leg_extreme_price
        elif selected is AuctionStateName.TREND_FAILURE:
            memory.trend_failure_age_bars = 1
            memory.reversal_confirmation_bars = 0
        elif selected is AuctionStateName.REVERSAL:
            side = memory.reversal_side
            if side in (DirectionalBias.UP, DirectionalBias.DOWN):
                self._establish_direction(memory, evidence, side)
            self._clear_exhaustion(memory)
            memory.failure_breach_bars = 0
            memory.trend_failure_age_bars = 0
            memory.reversal_confirmation_bars = 0

    def _advance_active_ages(self, memory: _ObservationMemory) -> None:
        if memory.current_state is AuctionStateName.CONTROLLED_PULLBACK:
            memory.pullback_age_bars += 1
            if memory.established_side is DirectionalBias.UP:
                memory.pullback_extreme_price = min(
                    memory.pullback_extreme_price or float("inf"),
                    memory.last_close or float("inf"),
                )
            elif memory.established_side is DirectionalBias.DOWN:
                memory.pullback_extreme_price = max(
                    memory.pullback_extreme_price or 0.0,
                    memory.last_close or 0.0,
                )
        elif memory.current_state is AuctionStateName.RECOMPRESSION:
            memory.recompression_age_bars += 1
        elif memory.current_state is AuctionStateName.REACCELERATION:
            memory.reacceleration_age_bars += 1
        elif memory.current_state is AuctionStateName.TREND_FAILURE:
            memory.trend_failure_age_bars += 1

    def _update_exhaustion_context(
        self,
        memory: _ObservationMemory,
        evidence: EvidenceSnapshot,
        flags: dict,
    ) -> None:
        established = memory.established_side
        if established not in (DirectionalBias.UP, DirectionalBias.DOWN):
            self._clear_exhaustion(memory)
            return
        if memory.exhaustion_active and memory.exhaustion_started_at is not None:
            expires = memory.exhaustion_started_at + timedelta(
                minutes=(
                    self.state_cfg.exhaustion_context_max_bars
                    * self.config.engine.snapshot_interval_minutes
                )
            )
            if evidence.snapshot_time > expires:
                self._clear_exhaustion(memory)
        extension_large = bool(
            flags["leg_distance_atr"] is not None
            and flags["leg_distance_atr"]
            >= self.state_cfg.exhaustion_context_min_extension_atr
        )
        observed = bool(
            memory.leg_age_bars >= self.state_cfg.exhaustion_context_min_leg_age_bars
            and (
                flags["current_leg_mature"]
                or extension_large
                or evidence.extension.mature is True
            )
            and (
                flags["progress_or_rejection"]
                or evidence.price_action.rejection
                or evidence.price_action.failed_extreme
            )
        )
        if observed:
            if not memory.exhaustion_active:
                memory.exhaustion_active = True
                memory.exhaustion_side = established
                memory.exhaustion_started_at = evidence.snapshot_time
                memory.exhaustion_age_bars = 1
            else:
                memory.exhaustion_age_bars += 1
            memory.exhaustion_last_seen_at = evidence.snapshot_time
            memory.exhaustion_clear_bars = 0
            return
        if memory.exhaustion_active:
            memory.exhaustion_age_bars += 1

    def _update_current_leg(
        self,
        memory: _ObservationMemory,
        evidence: EvidenceSnapshot,
    ) -> None:
        side = memory.established_side
        if side not in (DirectionalBias.UP, DirectionalBias.DOWN):
            return
        if memory.leg_anchor_price is None:
            self._reset_leg(memory, evidence, side, None)
            return
        if memory.leg_anchor_time == evidence.snapshot_time:
            return
        memory.leg_age_bars += 1
        tolerance = float(evidence.atr) * self.state_cfg.current_leg_progress_tolerance_atr
        if side is DirectionalBias.UP:
            new_extreme = evidence.bar.high
            prior = memory.leg_extreme_price or new_extreme
            if (
                memory.leg_maturity_consumed
                and memory.leg_maturity_extreme_price is not None
                and new_extreme - memory.leg_maturity_extreme_price
                >= float(evidence.atr) * self.state_cfg.current_leg_reanchor_progress_atr
            ):
                self._reset_leg(memory, evidence, side, None)
                return
            if new_extreme > prior + tolerance:
                memory.leg_extreme_price = new_extreme
                memory.leg_no_progress_bars = 0
            else:
                memory.leg_no_progress_bars += 1
            memory.trend_extreme_price = max(
                memory.trend_extreme_price or new_extreme,
                new_extreme,
            )
        else:
            new_extreme = evidence.bar.low
            prior = memory.leg_extreme_price or new_extreme
            if (
                memory.leg_maturity_consumed
                and memory.leg_maturity_extreme_price is not None
                and memory.leg_maturity_extreme_price - new_extreme
                >= float(evidence.atr) * self.state_cfg.current_leg_reanchor_progress_atr
            ):
                self._reset_leg(memory, evidence, side, None)
                return
            if new_extreme < prior - tolerance:
                memory.leg_extreme_price = new_extreme
                memory.leg_no_progress_bars = 0
            else:
                memory.leg_no_progress_bars += 1
            memory.trend_extreme_price = min(
                memory.trend_extreme_price or new_extreme,
                new_extreme,
            )

    @staticmethod
    def _leg_metrics(
        memory: _ObservationMemory,
        evidence: EvidenceSnapshot,
    ) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
        if memory.leg_anchor_price is None or memory.leg_extreme_price is None:
            return None, None, None, None
        atr = float(evidence.atr)
        if memory.established_side is DirectionalBias.UP:
            maximum = max(0.0, (memory.leg_extreme_price - memory.leg_anchor_price) / atr)
            current = (evidence.close - memory.leg_anchor_price) / atr
            retracement = max(0.0, (memory.leg_extreme_price - evidence.close) / atr)
        elif memory.established_side is DirectionalBias.DOWN:
            maximum = max(0.0, (memory.leg_anchor_price - memory.leg_extreme_price) / atr)
            current = (memory.leg_anchor_price - evidence.close) / atr
            retracement = max(0.0, (evidence.close - memory.leg_extreme_price) / atr)
        else:
            return None, None, None, None
        fraction = retracement / maximum if maximum > 0.0 else 0.0
        return maximum, current, retracement, fraction

    def _establish_direction(
        self,
        memory: _ObservationMemory,
        evidence: EvidenceSnapshot,
        side: DirectionalBias,
    ) -> None:
        anchor = evidence.bar.low if side is DirectionalBias.UP else evidence.bar.high
        memory.established_side = side
        memory.trend_onset_time = evidence.snapshot_time
        memory.trend_anchor_price = anchor
        memory.trend_extreme_price = (
            evidence.bar.high if side is DirectionalBias.UP else evidence.bar.low
        )
        memory.protection_level = anchor
        memory.protection_source = "INITIAL_DIRECTIONAL_ANCHOR"
        memory.protection_time = evidence.snapshot_time
        memory.trend_candidate_side = side
        memory.trend_candidate_bars = max(
            memory.trend_candidate_bars,
            self.state_cfg.trend_establishment_bars,
        )
        self._reset_leg(memory, evidence, side, anchor)

    def _reset_leg(
        self,
        memory: _ObservationMemory,
        evidence: EvidenceSnapshot,
        side: DirectionalBias,
        anchor: Optional[float],
    ) -> None:
        anchor_price = anchor
        if anchor_price is None:
            anchor_price = (
                evidence.bar.low if side is DirectionalBias.UP else evidence.bar.high
            )
        memory.leg_anchor_time = evidence.snapshot_time
        memory.leg_anchor_price = anchor_price
        memory.leg_extreme_price = (
            evidence.bar.high if side is DirectionalBias.UP else evidence.bar.low
        )
        memory.leg_age_bars = 1
        memory.leg_no_progress_bars = 0
        memory.leg_maturity_consumed = False
        memory.leg_maturity_onset_time = None
        memory.leg_maturity_extreme_price = None

    def _promote_protection(
        self,
        memory: _ObservationMemory,
        evidence: EvidenceSnapshot,
        level: float,
        source: str,
    ) -> None:
        prior = memory.protection_level
        minimum = float(evidence.atr) * self.state_cfg.trend_protection_min_improvement_atr
        if prior is not None:
            if memory.established_side is DirectionalBias.UP and level < prior + minimum:
                return
            if memory.established_side is DirectionalBias.DOWN and level > prior - minimum:
                return
        memory.protection_level = float(level)
        memory.protection_source = source
        memory.protection_time = evidence.snapshot_time

    def _protection_breached(
        self,
        memory: _ObservationMemory,
        evidence: EvidenceSnapshot,
    ) -> bool:
        level = memory.protection_level
        side = memory.established_side
        if level is None or side not in (DirectionalBias.UP, DirectionalBias.DOWN):
            return False
        tolerance = float(evidence.atr) * self.state_cfg.failure_level_breach_atr
        if side is DirectionalBias.UP:
            return evidence.close < level - tolerance
        return evidence.close > level + tolerance

    def _accepted_range(self, snapshot: SnapshotSchema, evidence: EvidenceSnapshot) -> dict:
        accepted = snapshot.structure.accepted.range
        range_id = self._optional_text(accepted.range_id)
        low = self._optional_positive(accepted.low)
        high = self._optional_positive(accepted.high)
        if (low is None) != (high is None):
            raise ValueError("Accepted structure range low/high must be supplied together")
        if low is not None and high is not None and high <= low:
            raise ValueError("Accepted structure range high must exceed low")
        if range_id is None and (low is not None or high is not None):
            raise ValueError("Accepted structure geometry requires range_id")
        if range_id is not None and (low is None or high is None):
            raise ValueError("Accepted structure range_id requires geometry")
        inside = False
        position = None
        outside_atr = None
        width_atr = None
        if low is not None and high is not None:
            width = high - low
            width_atr = width / float(evidence.atr)
            position = (evidence.close - low) / width
            tolerance = float(evidence.atr) * self.balance_cfg.source_range_inside_tolerance_atr
            inside = low - tolerance <= evidence.close <= high + tolerance
            if evidence.close > high:
                outside_atr = (evidence.close - high) / float(evidence.atr)
            elif evidence.close < low:
                outside_atr = (low - evidence.close) / float(evidence.atr)
            else:
                outside_atr = 0.0
        return {
            "accepted_range_id": range_id,
            "accepted_range_low": low,
            "accepted_range_high": high,
            "accepted_range_established_at": accepted.established_at,
            "accepted_range_provisional": bool(accepted.provisional),
            "accepted_range_breakout_eligible": bool(accepted.breakout_eligible),
            "accepted_range_inside": inside,
            "accepted_range_position": position,
            "accepted_range_outside_atr": outside_atr,
            "range_width_atr": width_atr,
        }

    def _trend_support_count(
        self,
        snapshot: SnapshotSchema,
        evidence: EvidenceSnapshot,
        side: DirectionalBias,
    ) -> int:
        trend = evidence.trend
        supports = 0
        if trend.direction is side:
            supports += 1
        if trend.value_migration is side:
            supports += 1
        if self._direction_from_text(trend.hma_order) is side:
            supports += 1
        if side is DirectionalBias.UP and "ABOVE" in trend.vwap_side:
            supports += 1
        if side is DirectionalBias.DOWN and "BELOW" in trend.vwap_side:
            supports += 1
        if side is DirectionalBias.UP and trend.open_control == "ABOVE_OPEN":
            supports += 1
        if side is DirectionalBias.DOWN and trend.open_control == "BELOW_OPEN":
            supports += 1
        raw_side = self._direction_from_text(snapshot.structure.raw.side)
        if raw_side is side:
            supports += 1
        return supports

    @staticmethod
    def _update_trend_candidate(
        memory: _ObservationMemory,
        observed_side: DirectionalBias,
    ) -> None:
        if observed_side not in (DirectionalBias.UP, DirectionalBias.DOWN):
            memory.trend_candidate_bars = max(0, memory.trend_candidate_bars - 1)
            if memory.trend_candidate_bars == 0:
                memory.trend_candidate_side = DirectionalBias.UNKNOWN
            return
        if memory.trend_candidate_side is observed_side:
            memory.trend_candidate_bars += 1
        else:
            memory.trend_candidate_side = observed_side
            memory.trend_candidate_bars = 1

    def _confirmation_bars(
        self,
        current: AuctionStateName,
        proposed: AuctionStateName,
    ) -> int:
        if proposed in {
            AuctionStateName.TREND_FAILURE,
            AuctionStateName.REVERSAL,
            AuctionStateName.REACCELERATION,
            AuctionStateName.RECOMPRESSION,
            AuctionStateName.ORDERLY_UPTREND,
            AuctionStateName.ORDERLY_DOWNTREND,
            AuctionStateName.CONTROLLED_PULLBACK,
        }:
            return 1
        if current is AuctionStateName.UNKNOWN:
            return self.state_cfg.initial_state_confirmation_bars
        return self.state_cfg.ordinary_transition_confirmation_bars

    def _minimum_dwell(self, state: AuctionStateName) -> int:
        mapping = {
            AuctionStateName.REACCELERATION: self.state_cfg.reacceleration_min_hold_bars,
            AuctionStateName.MATURE_EXTENSION: self.state_cfg.mature_extension_min_hold_bars,
            AuctionStateName.TREND_FAILURE: self.state_cfg.trend_failure_min_hold_bars,
            AuctionStateName.REVERSAL: self.state_cfg.reversal_min_hold_bars,
        }
        return mapping.get(state, self.state_cfg.minimum_state_hold_bars)

    def _validate_chronology(
        self,
        memory: _ObservationMemory,
        evidence: EvidenceSnapshot,
    ) -> None:
        if memory.trading_day is not None and memory.trading_day != evidence.trading_day:
            raise ValueError("Objective observation memory trading day mismatch")
        if (
            memory.last_snapshot_time is not None
            and evidence.snapshot_time <= memory.last_snapshot_time
        ):
            raise ValueError("Objective observation snapshot time must advance")

    @staticmethod
    def _clear_exhaustion(memory: _ObservationMemory) -> None:
        memory.exhaustion_active = False
        memory.exhaustion_side = DirectionalBias.UNKNOWN
        memory.exhaustion_started_at = None
        memory.exhaustion_last_seen_at = None
        memory.exhaustion_age_bars = 0
        memory.exhaustion_clear_bars = 0

    @staticmethod
    def _directional_efficiency(
        evidence: EvidenceSnapshot,
    ) -> Tuple[Optional[float], DirectionalEfficiencySource]:
        if evidence.price_action.directional_efficiency is not None:
            return float(evidence.price_action.directional_efficiency), DirectionalEfficiencySource.PRICE_ACTION
        if evidence.trend.directional_efficiency is not None:
            return float(evidence.trend.directional_efficiency), DirectionalEfficiencySource.TREND
        return None, DirectionalEfficiencySource.NONE

    @staticmethod
    def _orderly_state(side: DirectionalBias) -> AuctionStateName:
        if side is DirectionalBias.UP:
            return AuctionStateName.ORDERLY_UPTREND
        if side is DirectionalBias.DOWN:
            return AuctionStateName.ORDERLY_DOWNTREND
        return AuctionStateName.UNKNOWN

    @staticmethod
    def _opposite(side: DirectionalBias) -> DirectionalBias:
        if side is DirectionalBias.UP:
            return DirectionalBias.DOWN
        if side is DirectionalBias.DOWN:
            return DirectionalBias.UP
        return DirectionalBias.UNKNOWN

    @staticmethod
    def _direction_from_text(value: object) -> DirectionalBias:
        text = str(value or "").strip().upper()
        if text in {"UP", "BUY", "BULL", "BULLISH", "ABOVE"} or "BUY" in text:
            return DirectionalBias.UP
        if text in {"DOWN", "SELL", "BEAR", "BEARISH", "BELOW"} or "SELL" in text:
            return DirectionalBias.DOWN
        return DirectionalBias.UNKNOWN

    @staticmethod
    def _optional_positive(value: object) -> Optional[float]:
        if value is None:
            return None
        number = float(value)
        if number <= 0.0:
            raise ValueError("Objective price geometry must be positive")
        return number

    @staticmethod
    def _optional_text(value: object) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _symbol_key(symbol: str) -> str:
        key = str(symbol).strip().upper()
        if not key:
            raise ValueError("Auction observation symbol cannot be empty")
        return key

    @staticmethod
    def _unique(values: Sequence[str]) -> Tuple[str, ...]:
        seen = set()
        result = []
        for value in values:
            text = str(value).strip().upper()
            if text and text not in seen:
                seen.add(text)
                result.append(text)
        return tuple(result)


__all__ = ["AuctionObservationProvider"]
