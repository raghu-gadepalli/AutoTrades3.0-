"""Signal-time deployment Advisor.

Auction/snapshot generation owns objective structure and stock context. This
helper evaluates the selected Auction candidate only when SignalGenerator reads
the stored snapshot, allowing rule changes and SHADOW/ENFORCE comparisons on the
same market history.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from configs.stock_advisor_config import (
    STOCK_ADVISOR_CONFIG,
    StockAdvisorPolicyConfig,
)
from schemas.snapshot import CandidateProjection, SnapshotSchema, StockContextProjection
from services.auction_engine.contracts import AdvisorAction, AdvisorDecision


class StockAdvisor:
    """Return ALLOW/WATCH/BLOCK for the snapshot's selected candidate."""

    _BALANCED_CONTEXTS = {"BALANCED", "COMPRESSION", "ROTATIONAL"}

    def __init__(
        self,
        config: StockAdvisorPolicyConfig = STOCK_ADVISOR_CONFIG,
    ) -> None:
        self.policy = config

    def evaluate(self, snapshot: SnapshotSchema) -> AdvisorDecision:
        decision = snapshot.auction.decision
        if decision is None:
            raise ValueError("StockAdvisor requires snapshot.auction.decision")

        if (
            decision.action.strip().upper() != "LOCAL_CONFIRMED"
            or not decision.selected_candidate_id
        ):
            return self.not_applied(snapshot, "ADVISOR_NO_SELECTED_CANDIDATE")

        context = snapshot.auction.stock_context
        if context is None:
            raise ValueError("StockAdvisor requires snapshot.auction.stock_context")

        selected = self._selected_candidate(snapshot, decision.selected_candidate_id)
        if not self.policy.enabled:
            return AdvisorDecision(
                symbol=snapshot.symbol,
                snapshot_time=snapshot.snapshot_time,
                mode=self.policy.mode,
                action=AdvisorAction.ALLOW,
                effective_action=AdvisorAction.ALLOW,
                selected_candidate_id=selected.candidate_id,
                reason_codes=("ADVISOR_DISABLED_ALLOW",),
                diagnostics={
                    "auction_action": decision.action,
                    "manager_action": decision.manager_action,
                    "deployment_scope": "NEW_SIGNAL_ONLY",
                    "time_of_day_gate_applied": False,
                },
                config_version=self.policy.config_version,
            )

        reasons: List[str] = []
        diagnostics: Dict[str, object] = {
            "auction_action": decision.action,
            "manager_action": decision.manager_action,
            "manager_reason_codes": list(decision.manager_reason_codes),
            "stock_context": context.name,
            "stock_context_reasons": list(context.reason_codes),
            "candidate_family": selected.family,
            "candidate_subtype": selected.subtype,
            "candidate_side": selected.side,
            "deployment_scope": "NEW_SIGNAL_ONLY",
            "time_of_day_gate_applied": False,
        }

        atr = float(snapshot.indicators.atr.value)
        entry_percentile = self._entry_percentile(selected)
        signed_outside_atr = self._signed_outside_atr(selected, atr)
        diagnostics["entry_percentile_in_source_range"] = entry_percentile
        diagnostics["signed_outside_source_range_atr"] = signed_outside_atr

        same_direction_exhaustion = bool(
            context.exhaustion_active
            and self._direction_for_side(selected.side) == context.exhausted_side
        )
        diagnostics["same_direction_exhaustion"] = same_direction_exhaustion

        fresh_escape = self._confirmed_fresh_escape(
            selected,
            context,
            signed_outside_atr,
        )
        diagnostics["fresh_escape_confirmed"] = fresh_escape

        range_edge = self._unfavourable_range_edge(selected, entry_percentile)
        diagnostics["unfavourable_range_edge"] = range_edge

        manager_diagnostics = decision.manager_diagnostics
        if "recent_eligible_side_switches" in manager_diagnostics:
            switch_value = manager_diagnostics["recent_eligible_side_switches"]
        elif "historical_side_switches_in_lookback" in manager_diagnostics:
            switch_value = manager_diagnostics["historical_side_switches_in_lookback"]
        else:
            switch_value = 0
        projected_switches = int(switch_value or 0)
        diagnostics["recent_eligible_side_switches"] = projected_switches

        action = AdvisorAction.ALLOW
        context_name = context.name.strip().upper()

        if self.policy.block_exhausted_direction and same_direction_exhaustion:
            action = AdvisorAction.BLOCK
            reasons.append("ADVISOR_BLOCK_EXHAUSTED_DIRECTION")

        elif (
            self.policy.block_rotational_range_edge
            and context.rotational
            and range_edge
            and not fresh_escape
        ):
            action = AdvisorAction.BLOCK
            reasons.extend((
                "ADVISOR_BLOCK_ROTATIONAL_RANGE_EDGE",
                "ADVISOR_BLOCK_BUY_NEAR_RANGE_HIGH"
                if selected.side == "BUY"
                else "ADVISOR_BLOCK_SELL_NEAR_RANGE_LOW",
            ))

        elif (
            self.policy.block_rotational_range_edge
            and context.rotational
            and projected_switches >= self.policy.rotational_side_switches_to_block
            and not fresh_escape
        ):
            action = AdvisorAction.BLOCK
            reasons.append("ADVISOR_BLOCK_RECENT_SIDE_ROTATION")

        elif (
            context_name in self._BALANCED_CONTEXTS
            and self.policy.watch_unconfirmed_fresh_escape
            and signed_outside_atr is not None
            and signed_outside_atr >= 0.0
            and not fresh_escape
        ):
            action = AdvisorAction.WATCH
            reasons.append("ADVISOR_WATCH_RANGE_ESCAPE_NOT_CONFIRMED")

        elif (
            self.policy.block_balanced_non_directional
            and context_name in self._BALANCED_CONTEXTS
            and not fresh_escape
        ):
            action = AdvisorAction.BLOCK
            reasons.append("ADVISOR_BLOCK_BALANCED_NON_DIRECTIONAL")
            if range_edge:
                reasons.append(
                    "ADVISOR_BLOCK_BUY_NEAR_RANGE_HIGH"
                    if selected.side == "BUY"
                    else "ADVISOR_BLOCK_SELL_NEAR_RANGE_LOW"
                )

        if action is AdvisorAction.ALLOW:
            reasons.append("ADVISOR_ALLOW_DEPLOYMENT_CONTEXT")
            if fresh_escape:
                reasons.append("ADVISOR_ALLOW_CONFIRMED_PRICE_LED_EXPANSION")

        effective = action
        if self.policy.mode == "SHADOW" and action in {
            AdvisorAction.WATCH,
            AdvisorAction.BLOCK,
        }:
            effective = AdvisorAction.ALLOW
            reasons.append("ADVISOR_SHADOW_DECISION_NOT_ENFORCED")

        diagnostics["advisor_action"] = action.value
        diagnostics["advisor_effective_action"] = effective.value

        return AdvisorDecision(
            symbol=snapshot.symbol,
            snapshot_time=snapshot.snapshot_time,
            mode=self.policy.mode,
            action=action,
            effective_action=effective,
            selected_candidate_id=selected.candidate_id,
            reason_codes=tuple(dict.fromkeys(reasons)),
            diagnostics=diagnostics,
            config_version=self.policy.config_version,
        )

    def not_applied(
        self,
        snapshot: SnapshotSchema,
        reason_code: str,
    ) -> AdvisorDecision:
        decision = snapshot.auction.decision
        manager_action = decision.manager_action if decision is not None else "UNKNOWN"
        return AdvisorDecision(
            symbol=snapshot.symbol,
            snapshot_time=snapshot.snapshot_time,
            mode=self.policy.mode,
            action=AdvisorAction.NO_ACTION,
            effective_action=AdvisorAction.NO_ACTION,
            selected_candidate_id=None,
            reason_codes=(reason_code,),
            diagnostics={
                "manager_action": manager_action,
                "deployment_scope": "NEW_SIGNAL_ONLY",
                "deployment_applied": False,
                "time_of_day_gate_applied": False,
            },
            config_version=self.policy.config_version,
        )

    @staticmethod
    def _selected_candidate(
        snapshot: SnapshotSchema,
        candidate_id: str,
    ) -> CandidateProjection:
        matches = [
            candidate
            for candidate in snapshot.auction.candidates
            if candidate.candidate_id == candidate_id
        ]
        if len(matches) != 1:
            raise ValueError(
                "StockAdvisor requires exactly one selected candidate: "
                f"candidate_id={candidate_id} matches={len(matches)}"
            )
        return matches[0]

    def _confirmed_fresh_escape(
        self,
        candidate: CandidateProjection,
        context: StockContextProjection,
        signed_outside_atr: Optional[float],
    ) -> bool:
        if not self.policy.allow_confirmed_fresh_expansion_override:
            return False
        if signed_outside_atr is None or signed_outside_atr < self.policy.fresh_escape_min_atr:
            return False
        efficiency = context.directional_efficiency
        if efficiency is None or efficiency < self.policy.fresh_escape_efficiency_min:
            return False
        if context.fresh_expansion_confirmed:
            return True
        aligned_states = {
            "BUY": {"FRESH_EXPANSION", "ORDERLY_UPTREND", "REACCELERATION"},
            "SELL": {"FRESH_EXPANSION", "ORDERLY_DOWNTREND", "REACCELERATION"},
        }
        return candidate.auction_state in aligned_states[candidate.side]

    def _unfavourable_range_edge(
        self,
        candidate: CandidateProjection,
        percentile: Optional[float],
    ) -> bool:
        if percentile is None:
            return False
        if candidate.side == "BUY":
            return percentile >= self.policy.buy_range_edge_percentile
        return percentile <= self.policy.sell_range_edge_percentile

    @staticmethod
    def _entry_percentile(candidate: CandidateProjection) -> Optional[float]:
        if (
            candidate.source_frozen_range_low is None
            or candidate.source_frozen_range_high is None
        ):
            return None
        low = float(candidate.source_frozen_range_low)
        high = float(candidate.source_frozen_range_high)
        width = high - low
        if width <= 0:
            return None
        return (float(candidate.entry_price) - low) / width

    @staticmethod
    def _signed_outside_atr(
        candidate: CandidateProjection,
        atr: float,
    ) -> Optional[float]:
        if atr <= 0:
            return None
        if candidate.side == "BUY":
            if candidate.source_frozen_range_high is None:
                return None
            return (float(candidate.entry_price) - float(candidate.source_frozen_range_high)) / atr
        if candidate.source_frozen_range_low is None:
            return None
        return (float(candidate.source_frozen_range_low) - float(candidate.entry_price)) / atr

    @staticmethod
    def _direction_for_side(side: str) -> str:
        if side == "BUY":
            return "UP"
        if side == "SELL":
            return "DOWN"
        raise ValueError(f"Unsupported Advisor candidate side: {side}")


__all__ = ["StockAdvisor"]
