"""Strict signal-time deployment Advisor for event-driven Auction candidates.

The Advisor does not discover setups or alter Auction lifecycle.  It evaluates
only the selected authoritative candidate and fails loudly on malformed input.
There is no fail-open path.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from configs.stock_advisor_config import STOCK_ADVISOR_CONFIG, StockAdvisorPolicyConfig
from enums.auction_engine import AdvisorAction
from schemas.snapshot import SnapshotSchema
from services.auction_engine.contracts import AdvisorDecision
from services.auction_engine.setup_contracts import AuthoritativeSetupCandidate


class StockAdvisor:
    def __init__(
        self,
        config: StockAdvisorPolicyConfig = STOCK_ADVISOR_CONFIG,
    ) -> None:
        self.policy = config

    def evaluate_authoritative(
        self,
        snapshot: SnapshotSchema,
        candidate: AuthoritativeSetupCandidate,
    ) -> AdvisorDecision:
        if not isinstance(snapshot, SnapshotSchema):
            raise TypeError("StockAdvisor requires SnapshotSchema")
        if not isinstance(candidate, AuthoritativeSetupCandidate):
            raise TypeError("StockAdvisor requires AuthoritativeSetupCandidate")
        if snapshot.auction.status != "OK" or snapshot.auction.observation is None:
            raise ValueError("StockAdvisor requires authoritative Auction observation")
        symbol = snapshot.symbol.strip().upper()
        if candidate.symbol != symbol:
            raise ValueError("StockAdvisor candidate/snapshot symbol mismatch")
        if candidate.snapshot_time != snapshot.snapshot_time:
            raise ValueError("StockAdvisor candidate/snapshot time mismatch")

        if not self.policy.enabled:
            return self._decision(
                snapshot,
                candidate,
                AdvisorAction.ALLOW,
                ("ADVISOR_DISABLED_ALLOW",),
                (),
            )

        observation = snapshot.auction.observation
        family = candidate.setup_family.value
        subtype = candidate.setup_subtype.upper()
        side_direction = "UP" if candidate.side.value == "BUY" else "DOWN"
        matches: List[Tuple[str, str]] = []

        if (
            observation.exhaustion_active
            and observation.exhausted_side.value == side_direction
            and family in self._normalised(self.policy.same_direction_exhaustion_families)
        ):
            matches.append((
                self.policy.same_direction_exhaustion_action,
                "SAME_DIRECTION_EXHAUSTION",
            ))

        inside_exempt = bool(
            family in self._normalised(self.policy.inside_range_exempt_families)
            or subtype in self._normalised(self.policy.inside_range_exempt_subtypes)
        )
        if observation.accepted_range_inside and not inside_exempt:
            matches.append((
                self.policy.inside_accepted_range_action,
                "INSIDE_ACCEPTED_RANGE",
            ))

        if family in self._normalised(
            self.policy.accepted_breakout_current_context_families
        ):
            range_valid = bool(
                observation.accepted_range_low is not None
                and observation.accepted_range_high is not None
                and observation.accepted_range_breakout_eligible
                and not observation.accepted_range_provisional
            )
            outside_for_side = bool(
                range_valid
                and (
                    (
                        candidate.side.value == "BUY"
                        and snapshot.close > float(observation.accepted_range_high)
                    )
                    or (
                        candidate.side.value == "SELL"
                        and snapshot.close < float(observation.accepted_range_low)
                    )
                )
            )
            if not outside_for_side:
                matches.append((
                    self.policy.accepted_breakout_current_context_action,
                    "ACCEPTED_BREAKOUT_NOT_CURRENTLY_OUTSIDE",
                ))

        action = self._resolve_action(matches)
        reasons = tuple(reason for configured, reason in matches if configured == action.value)
        if not reasons:
            reasons = ("ADVISOR_ALLOW",)
        return self._decision(snapshot, candidate, action, reasons, tuple(matches))

    def _decision(
        self,
        snapshot: SnapshotSchema,
        candidate: AuthoritativeSetupCandidate,
        action: AdvisorAction,
        reasons: Tuple[str, ...],
        matches: Tuple[Tuple[str, str], ...],
    ) -> AdvisorDecision:
        return AdvisorDecision(
            symbol=snapshot.symbol,
            snapshot_time=snapshot.snapshot_time,
            action=action,
            selected_candidate_id=candidate.candidate_id,
            reason_codes=reasons,
            diagnostics={
                "deployment_scope": "NEW_SIGNAL_ONLY",
                "candidate_family": candidate.setup_family.value,
                "candidate_subtype": candidate.setup_subtype,
                "candidate_side": candidate.side.value,
                "source_event_id": candidate.source_event_id,
                "source_episode_id": candidate.source_episode_id,
                "matched_rules": [
                    {"configured_action": configured, "reason": reason}
                    for configured, reason in matches
                ],
            },
            config_version=self.policy.config_version,
        )

    @staticmethod
    def _resolve_action(matches: List[Tuple[str, str]]) -> AdvisorAction:
        precedence: Dict[str, int] = {"ALLOW": 0, "WATCH": 1, "BLOCK": 2}
        action_text = max(
            (configured for configured, _ in matches),
            key=lambda value: precedence[value],
            default="ALLOW",
        )
        return AdvisorAction(action_text)

    @staticmethod
    def _normalised(values: Tuple[str, ...]) -> set[str]:
        return {str(value).strip().upper() for value in values}


__all__ = ["StockAdvisor"]
