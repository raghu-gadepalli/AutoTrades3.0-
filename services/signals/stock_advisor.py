"""Signal-time deployment Advisor.

Auction and setup lifecycles decide which opportunity exists.  This helper only
applies conservative new-signal deployment checks using objective stock-day
context carried by the stored snapshot.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Set

from configs.stock_advisor_config import (
    STOCK_ADVISOR_CONFIG,
    StockAdvisorPolicyConfig,
)
from schemas.snapshot import CandidateProjection, SnapshotSchema, StockContextProjection
from services.auction_engine.contracts import AdvisorAction, AdvisorDecision

logger = logging.getLogger(__name__)


class _AdvisorInputError(Exception):
    """Internal validation error converted into a logged fail-open decision."""


class StockAdvisor:
    """Return ALLOW/WATCH/BLOCK for the snapshot's selected candidate."""

    def __init__(
        self,
        config: StockAdvisorPolicyConfig = STOCK_ADVISOR_CONFIG,
    ) -> None:
        self.policy = config

    def evaluate(self, snapshot: SnapshotSchema) -> AdvisorDecision:
        """Evaluate deployment context without stopping snapshot processing."""
        try:
            return self._evaluate_strict(snapshot)
        except _AdvisorInputError as exc:
            logger.error(
                "StockAdvisor input error; failing open | symbol=%s snapshot_time=%s "
                "selected_candidate_id=%s error=%s",
                snapshot.symbol,
                snapshot.snapshot_time,
                self._decision_candidate_id(snapshot),
                exc,
            )
            return self._fail_open(snapshot, exc, unexpected=False)
        except Exception as exc:
            logger.exception(
                "StockAdvisor evaluation error; failing open | symbol=%s "
                "snapshot_time=%s selected_candidate_id=%s",
                snapshot.symbol,
                snapshot.snapshot_time,
                self._decision_candidate_id(snapshot),
            )
            return self._fail_open(snapshot, exc, unexpected=True)

    def _evaluate_strict(self, snapshot: SnapshotSchema) -> AdvisorDecision:
        decision = snapshot.auction.decision
        if decision is None:
            raise _AdvisorInputError("snapshot.auction.decision is missing")

        if (
            decision.action.strip().upper() != "LOCAL_CONFIRMED"
            or not decision.selected_candidate_id
        ):
            return self.not_applied(snapshot, "ADVISOR_NO_SELECTED_CANDIDATE")

        context = snapshot.auction.stock_context
        if context is None:
            raise _AdvisorInputError("snapshot.auction.stock_context is missing")

        selected = self._selected_candidate(snapshot, decision.selected_candidate_id)
        if not self.policy.enabled:
            return AdvisorDecision(
                symbol=snapshot.symbol,
                snapshot_time=snapshot.snapshot_time,
                action=AdvisorAction.ALLOW,
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

        atr = float(snapshot.indicators.atr.value)
        if atr <= 0:
            raise _AdvisorInputError("snapshot.indicators.atr.value must be positive")

        family = selected.family.strip().upper()
        subtype = selected.subtype.strip().upper()
        diagnostics: Dict[str, object] = {
            "auction_action": decision.action,
            "manager_action": decision.manager_action,
            "manager_reason_codes": list(decision.manager_reason_codes),
            "current_auction_state": context.current_auction_state,
            "candidate_family": family,
            "candidate_subtype": subtype,
            "candidate_side": selected.side,
            "deployment_scope": "NEW_SIGNAL_ONLY",
            "time_of_day_gate_applied": False,
            "accepted_range_id": context.accepted_range_id,
            "accepted_range_source": context.accepted_range_source,
            "accepted_range_low": context.accepted_range_low,
            "accepted_range_high": context.accepted_range_high,
            "accepted_range_provisional": context.accepted_range_provisional,
            "accepted_range_breakout_eligible": (
                context.accepted_range_breakout_eligible
            ),
            "accepted_range_inside": context.accepted_range_inside,
            "accepted_range_position": context.accepted_range_position,
            "accepted_range_outside_atr": context.accepted_range_outside_atr,
            "session_high_price": context.session_high_price,
            "session_high_time": context.session_high_time.isoformat(),
            "session_low_price": context.session_low_price,
            "session_low_time": context.session_low_time.isoformat(),
            "session_position": context.session_position,
            "distance_to_session_high_atr": context.distance_to_session_high_atr,
            "distance_to_session_low_atr": context.distance_to_session_low_atr,
            "rise_from_session_low_atr": context.rise_from_session_low_atr,
            "decline_from_session_high_atr": context.decline_from_session_high_atr,
            "path_from_session_low_bars": context.path_from_session_low_bars,
            "path_from_session_low_efficiency": (
                context.path_from_session_low_efficiency
            ),
            "path_from_session_low_directional_ratio": (
                context.path_from_session_low_directional_ratio
            ),
            "path_from_session_high_bars": context.path_from_session_high_bars,
            "path_from_session_high_efficiency": (
                context.path_from_session_high_efficiency
            ),
            "path_from_session_high_directional_ratio": (
                context.path_from_session_high_directional_ratio
            ),
        }

        strong_confirmation = self._strong_confirmation(selected, context, atr)
        diagnostics["strong_confirmation"] = strong_confirmation

        exhaustion_current = self._exhaustion_is_current(snapshot, context)
        exhaustion_family_applicable = family in self._normalised_set(
            self.policy.same_direction_exhaustion_families
        )
        same_direction_exhaustion = bool(
            exhaustion_current
            and exhaustion_family_applicable
            and self._direction_for_side(selected.side) == context.exhausted_side
        )
        diagnostics["exhaustion_context_current"] = exhaustion_current
        diagnostics["exhaustion_family_applicable"] = exhaustion_family_applicable
        diagnostics["same_direction_exhaustion"] = same_direction_exhaustion

        inside_range_exempt = self._is_exempt(
            family,
            subtype,
            self.policy.inside_range_exempt_families,
            self.policy.inside_range_exempt_subtypes,
        )
        candidate_inside_accepted_range = self._candidate_inside_accepted_range(
            selected,
            context,
            atr,
        )
        diagnostics["candidate_inside_accepted_range"] = candidate_inside_accepted_range
        inside_range_condition = bool(
            candidate_inside_accepted_range
            and not inside_range_exempt
            and not strong_confirmation
        )
        diagnostics["inside_range_exempt"] = inside_range_exempt
        diagnostics["inside_range_condition"] = inside_range_condition

        extreme_chase = bool(
            self._is_extreme_chase(selected, context, atr)
            and not strong_confirmation
        )
        diagnostics["extreme_chase"] = extreme_chase

        session_path_exempt = self._is_exempt(
            family,
            subtype,
            self.policy.session_path_exempt_families,
            self.policy.session_path_exempt_subtypes,
        )
        against_session_path = bool(
            not session_path_exempt
            and not strong_confirmation
            and self._against_persistent_session_path(selected, context)
        )
        diagnostics["session_path_exempt"] = session_path_exempt
        diagnostics["against_persistent_session_path"] = against_session_path

        configured_rule_actions = {
            "same_direction_exhaustion": self.policy.same_direction_exhaustion_action,
            "inside_accepted_range": self.policy.inside_accepted_range_action,
            "extreme_chase": self.policy.extreme_chase_action,
            "against_session_path": self.policy.against_session_path_action,
        }
        diagnostics["configured_rule_actions"] = dict(configured_rule_actions)

        reasons: List[str] = []
        triggered_rules: List[Dict[str, str]] = []
        action = AdvisorAction.ALLOW

        def apply_rule(rule_name: str, configured_action: str, reason_suffix: str) -> None:
            nonlocal action
            rule_action = self._configured_action(configured_action)
            reason_code = f"ADVISOR_{rule_action.value}_{reason_suffix}"
            triggered_rules.append({
                "rule": rule_name,
                "action": rule_action.value,
                "reason_code": reason_code,
            })
            reasons.append(reason_code)
            action = self._stronger_action(action, rule_action)

        if same_direction_exhaustion:
            apply_rule(
                "same_direction_exhaustion",
                self.policy.same_direction_exhaustion_action,
                "EXHAUSTED_DIRECTION",
            )
        if inside_range_condition:
            apply_rule(
                "inside_accepted_range",
                self.policy.inside_accepted_range_action,
                "INSIDE_ACCEPTED_RANGE",
            )
        if extreme_chase:
            apply_rule(
                "extreme_chase",
                self.policy.extreme_chase_action,
                (
                    "BUY_NEAR_SESSION_HIGH"
                    if selected.side == "BUY"
                    else "SELL_NEAR_SESSION_LOW"
                ),
            )
        if against_session_path:
            apply_rule(
                "against_session_path",
                self.policy.against_session_path_action,
                (
                    "SELL_AGAINST_CLIMB_FROM_SESSION_LOW"
                    if selected.side == "SELL"
                    else "BUY_AGAINST_DECLINE_FROM_SESSION_HIGH"
                ),
            )

        if not triggered_rules:
            reasons.append(
                "ADVISOR_ALLOW_STRONG_ACCEPTED_RANGE_ESCAPE"
                if strong_confirmation
                else "ADVISOR_ALLOW_SIMPLE_DEPLOYMENT_CONTEXT"
            )

        diagnostics["triggered_rules"] = triggered_rules
        diagnostics["advisor_action"] = action.value
        diagnostics["signal_creation_allowed"] = action is not AdvisorAction.BLOCK

        return AdvisorDecision(
            symbol=snapshot.symbol,
            snapshot_time=snapshot.snapshot_time,
            action=action,
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
            action=AdvisorAction.NO_ACTION,
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
            raise _AdvisorInputError(
                "selected candidate projection mismatch: "
                f"candidate_id={candidate_id} matches={len(matches)}"
            )
        return matches[0]

    @staticmethod
    def _decision_candidate_id(snapshot: SnapshotSchema) -> Optional[str]:
        decision = snapshot.auction.decision
        return decision.selected_candidate_id if decision is not None else None

    def _fail_open(
        self,
        snapshot: SnapshotSchema,
        error: Exception,
        *,
        unexpected: bool,
    ) -> AdvisorDecision:
        candidate_id = self._decision_candidate_id(snapshot)
        decision = snapshot.auction.decision
        if candidate_id is None:
            return self.not_applied(snapshot, "ADVISOR_ERROR_NO_SELECTED_CANDIDATE")

        error_code = (
            "ADVISOR_UNEXPECTED_ERROR_FAIL_OPEN"
            if unexpected
            else "ADVISOR_INPUT_ERROR_FAIL_OPEN"
        )
        return AdvisorDecision(
            symbol=snapshot.symbol,
            snapshot_time=snapshot.snapshot_time,
            action=AdvisorAction.ALLOW,
            selected_candidate_id=candidate_id,
            reason_codes=(error_code,),
            diagnostics={
                "auction_action": decision.action if decision is not None else "UNKNOWN",
                "manager_action": (
                    decision.manager_action if decision is not None else "UNKNOWN"
                ),
                "deployment_scope": "NEW_SIGNAL_ONLY",
                "deployment_applied": False,
                "fail_open": True,
                "error_type": type(error).__name__,
                "error": str(error),
                "time_of_day_gate_applied": False,
            },
            config_version=self.policy.config_version,
        )

    def _candidate_inside_accepted_range(
        self,
        candidate: CandidateProjection,
        context: StockContextProjection,
        atr: float,
    ) -> bool:
        if (
            context.accepted_range_low is None
            or context.accepted_range_high is None
        ):
            return False
        tolerance = atr * self.policy.accepted_range_tolerance_atr
        entry = float(candidate.entry_price)
        return bool(
            float(context.accepted_range_low) - tolerance
            <= entry
            <= float(context.accepted_range_high) + tolerance
        )

    def _strong_confirmation(
        self,
        candidate: CandidateProjection,
        context: StockContextProjection,
        atr: float,
    ) -> bool:
        if candidate.family.strip().upper() not in self._normalised_set(
            self.policy.strong_confirmation_families
        ):
            return False
        if context.accepted_range_provisional:
            return False
        if not context.accepted_range_breakout_eligible:
            return False
        signed_outside = self._signed_outside_accepted_range_atr(
            candidate,
            context,
            atr,
        )
        if (
            signed_outside is None
            or signed_outside < self.policy.strong_confirmation_min_outside_atr
        ):
            return False
        state = context.current_auction_state.strip().upper()
        aligned = (
            self.policy.strong_confirmation_states_buy
            if candidate.side == "BUY"
            else self.policy.strong_confirmation_states_sell
        )
        return state in self._normalised_set(aligned)

    def _is_extreme_chase(
        self,
        candidate: CandidateProjection,
        context: StockContextProjection,
        atr: float,
    ) -> bool:
        entry = float(candidate.entry_price)
        if candidate.side == "BUY":
            distance = max(0.0, float(context.session_high_price) - entry) / atr
            return bool(
                distance <= self.policy.extreme_near_atr
                and context.rise_from_session_low_atr
                >= self.policy.extreme_min_prior_move_atr
            )
        if candidate.side == "SELL":
            distance = max(0.0, entry - float(context.session_low_price)) / atr
            return bool(
                distance <= self.policy.extreme_near_atr
                and context.decline_from_session_high_atr
                >= self.policy.extreme_min_prior_move_atr
            )
        raise _AdvisorInputError(
            f"Unsupported Advisor candidate side: {candidate.side}"
        )

    def _against_persistent_session_path(
        self,
        candidate: CandidateProjection,
        context: StockContextProjection,
    ) -> bool:
        if candidate.side == "SELL":
            return self._path_is_persistent(
                bars=context.path_from_session_low_bars,
                move_atr=context.rise_from_session_low_atr,
                efficiency=context.path_from_session_low_efficiency,
                directional_ratio=context.path_from_session_low_directional_ratio,
            )
        if candidate.side == "BUY":
            return self._path_is_persistent(
                bars=context.path_from_session_high_bars,
                move_atr=context.decline_from_session_high_atr,
                efficiency=context.path_from_session_high_efficiency,
                directional_ratio=context.path_from_session_high_directional_ratio,
            )
        raise _AdvisorInputError(
            f"Unsupported Advisor candidate side: {candidate.side}"
        )

    def _path_is_persistent(
        self,
        *,
        bars: int,
        move_atr: float,
        efficiency: Optional[float],
        directional_ratio: Optional[float],
    ) -> bool:
        return bool(
            bars >= self.policy.session_path_min_bars
            and move_atr >= self.policy.session_path_min_move_atr
            and efficiency is not None
            and efficiency >= self.policy.session_path_efficiency_min
            and directional_ratio is not None
            and directional_ratio >= self.policy.session_path_directional_ratio_min
        )

    @staticmethod
    def _exhaustion_is_current(
        snapshot: SnapshotSchema,
        context: StockContextProjection,
    ) -> bool:
        if not context.exhaustion_active:
            return False
        expires_at = context.exhaustion_expires_at
        if expires_at is None:
            return True
        return snapshot.snapshot_time <= expires_at

    @staticmethod
    def _signed_outside_accepted_range_atr(
        candidate: CandidateProjection,
        context: StockContextProjection,
        atr: float,
    ) -> Optional[float]:
        if atr <= 0:
            return None
        if (
            context.accepted_range_low is None
            or context.accepted_range_high is None
        ):
            return None
        if candidate.side == "BUY":
            return (
                float(candidate.entry_price) - float(context.accepted_range_high)
            ) / atr
        if candidate.side == "SELL":
            return (
                float(context.accepted_range_low) - float(candidate.entry_price)
            ) / atr
        raise _AdvisorInputError(
            f"Unsupported Advisor candidate side: {candidate.side}"
        )

    @staticmethod
    def _is_exempt(
        family: str,
        subtype: str,
        families: tuple[str, ...],
        subtypes: tuple[str, ...],
    ) -> bool:
        return bool(
            family in StockAdvisor._normalised_set(families)
            or subtype in StockAdvisor._normalised_set(subtypes)
        )

    @staticmethod
    def _configured_action(value: str) -> AdvisorAction:
        normalized = str(value or "").strip().upper()
        try:
            action = AdvisorAction(normalized)
        except ValueError as exc:
            raise _AdvisorInputError(
                f"Unsupported configured Advisor rule action: {value!r}"
            ) from exc
        if action not in {AdvisorAction.ALLOW, AdvisorAction.WATCH, AdvisorAction.BLOCK}:
            raise _AdvisorInputError(
                f"Unsupported configured Advisor rule action: {normalized}"
            )
        return action

    @staticmethod
    def _stronger_action(
        current: AdvisorAction,
        candidate: AdvisorAction,
    ) -> AdvisorAction:
        precedence = {
            AdvisorAction.ALLOW: 0,
            AdvisorAction.WATCH: 1,
            AdvisorAction.BLOCK: 2,
        }
        return candidate if precedence[candidate] > precedence[current] else current

    @staticmethod
    def _normalised_set(values: tuple[str, ...]) -> Set[str]:
        return {str(value).strip().upper() for value in values}

    @staticmethod
    def _direction_for_side(side: str) -> str:
        if side == "BUY":
            return "UP"
        if side == "SELL":
            return "DOWN"
        raise _AdvisorInputError(f"Unsupported Advisor candidate side: {side}")


__all__ = ["StockAdvisor"]
