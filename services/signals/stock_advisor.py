"""Strict signal-time deployment Advisor for event-driven Auction candidates.

The Advisor does not discover setups or alter Auction lifecycle.  It evaluates
only the selected authoritative candidate and fails loudly on malformed input.
There is no fail-open path.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Dict, List, Tuple

from configs.stock_advisor_config import STOCK_ADVISOR_CONFIG, StockAdvisorPolicyConfig
from enums.auction_engine import AdvisorAction, AuctionEventType, SetupFamily, TradeSide
from schemas.snapshot import SnapshotSchema
from services.auction_engine.contracts import AdvisorDecision
from services.auction_engine.setup_contracts import AuthoritativeSetupCandidate


_SOURCE_RANGE_EVENT_TYPES = {
    AuctionEventType.BALANCE_ESCAPE_STARTED,
    AuctionEventType.BALANCE_ESCAPE_ACCEPTED,
}


@dataclass(frozen=True)
class _AdvisorRangeContext:
    authority: str
    low: float | None
    high: float | None
    reference_price: float | None
    inside_for_rule: bool
    outside_for_side: bool
    source_event_id: str | None
    source_episode_id: str | None


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
                range_context=None,
            )

        observation = snapshot.auction.observation
        family = candidate.setup_family.value
        subtype = candidate.setup_subtype.upper()
        side_direction = "UP" if candidate.side.value == "BUY" else "DOWN"
        matches: List[Tuple[str, str]] = []
        range_context = self._resolve_range_context(snapshot, candidate)

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
        if range_context.inside_for_rule and not inside_exempt:
            matches.append((
                self.policy.inside_accepted_range_action,
                "INSIDE_ACCEPTED_RANGE",
            ))

        if family in self._normalised(
            self.policy.accepted_breakout_current_context_families
        ):
            if not range_context.outside_for_side:
                matches.append((
                    self.policy.accepted_breakout_current_context_action,
                    "ACCEPTED_BREAKOUT_NOT_CURRENTLY_OUTSIDE",
                ))

        action = self._resolve_action(matches)
        reasons = tuple(reason for configured, reason in matches if configured == action.value)
        if not reasons:
            reasons = ("ADVISOR_ALLOW",)
        return self._decision(
            snapshot,
            candidate,
            action,
            reasons,
            tuple(matches),
            range_context=range_context,
        )

    def _resolve_range_context(
        self,
        snapshot: SnapshotSchema,
        candidate: AuthoritativeSetupCandidate,
    ) -> _AdvisorRangeContext:
        if candidate.source_event_type in _SOURCE_RANGE_EVENT_TYPES:
            return self._source_episode_range_context(snapshot, candidate)
        return self._current_observation_range_context(snapshot, candidate)

    @staticmethod
    def _source_episode_range_context(
        snapshot: SnapshotSchema,
        candidate: AuthoritativeSetupCandidate,
    ) -> _AdvisorRangeContext:
        expected_family_by_event = {
            AuctionEventType.BALANCE_ESCAPE_STARTED: SetupFamily.BREAKOUT_INITIATION,
            AuctionEventType.BALANCE_ESCAPE_ACCEPTED: SetupFamily.ACCEPTED_BREAKOUT,
        }
        expected_family = expected_family_by_event[candidate.source_event_type]
        if candidate.setup_family is not expected_family:
            raise ValueError("StockAdvisor balance-event candidate family mismatch")
        if candidate.reference_source != "FROZEN_BALANCE_BOUNDARY":
            raise ValueError(
                "StockAdvisor balance-event candidate requires frozen boundary reference"
            )

        lifecycle = snapshot.auction.lifecycle
        if lifecycle is None:
            raise ValueError("StockAdvisor source-range resolution requires Auction lifecycle")
        matching_events = [
            event
            for event in lifecycle.events
            if event.event_id == candidate.source_event_id
        ]
        if len(matching_events) != 1:
            raise ValueError("StockAdvisor candidate source event missing or duplicated")
        event = matching_events[0]
        if event.event_type is not candidate.source_event_type:
            raise ValueError("StockAdvisor candidate source event type mismatch")
        if event.episode_id != candidate.source_episode_id:
            raise ValueError("StockAdvisor candidate source episode mismatch")

        if "frozen_low" not in event.data or "frozen_high" not in event.data:
            raise ValueError("StockAdvisor source event requires frozen range geometry")
        low = float(event.data["frozen_low"])
        high = float(event.data["frozen_high"])
        if not math.isfinite(low) or not math.isfinite(high) or low <= 0.0 or high <= low:
            raise ValueError("StockAdvisor source event has invalid frozen range geometry")

        reference = high if candidate.side is TradeSide.BUY else low
        if not math.isclose(
            float(candidate.reference_price),
            reference,
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            raise ValueError("StockAdvisor candidate/source boundary mismatch")
        current_price = float(snapshot.close)
        outside = (
            current_price > reference
            if candidate.side is TradeSide.BUY
            else current_price < reference
        )
        return _AdvisorRangeContext(
            authority="AUCTION_SOURCE_EPISODE",
            low=low,
            high=high,
            reference_price=reference,
            inside_for_rule=not outside,
            outside_for_side=outside,
            source_event_id=candidate.source_event_id,
            source_episode_id=candidate.source_episode_id,
        )

    @staticmethod
    def _current_observation_range_context(
        snapshot: SnapshotSchema,
        candidate: AuthoritativeSetupCandidate,
    ) -> _AdvisorRangeContext:
        observation = snapshot.auction.observation
        assert observation is not None
        range_valid = bool(
            observation.accepted_range_low is not None
            and observation.accepted_range_high is not None
            and observation.accepted_range_breakout_eligible
            and not observation.accepted_range_provisional
        )
        low = (float(observation.accepted_range_low) if range_valid else None)
        high = (float(observation.accepted_range_high) if range_valid else None)
        if range_valid:
            assert low is not None and high is not None
            if (
                not math.isfinite(low)
                or not math.isfinite(high)
                or low <= 0.0
                or high <= low
            ):
                raise ValueError(
                    "StockAdvisor observation has invalid accepted range geometry"
                )
            reference = high if candidate.side is TradeSide.BUY else low
            current_price = float(snapshot.close)
            outside = (
                current_price > reference
                if candidate.side is TradeSide.BUY
                else current_price < reference
            )
        else:
            reference = None
            outside = False
        return _AdvisorRangeContext(
            authority="CURRENT_AUCTION_OBSERVATION",
            low=low,
            high=high,
            reference_price=reference,
            inside_for_rule=bool(observation.accepted_range_inside),
            outside_for_side=outside,
            source_event_id=None,
            source_episode_id=None,
        )

    def _decision(
        self,
        snapshot: SnapshotSchema,
        candidate: AuthoritativeSetupCandidate,
        action: AdvisorAction,
        reasons: Tuple[str, ...],
        matches: Tuple[Tuple[str, str], ...],
        *,
        range_context: _AdvisorRangeContext | None,
    ) -> AdvisorDecision:
        observation = snapshot.auction.observation
        diagnostics = {
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
        }
        if range_context is not None:
            diagnostics["range_context"] = {
                "authority": range_context.authority,
                "low": range_context.low,
                "high": range_context.high,
                "reference_price": range_context.reference_price,
                "inside_for_rule": range_context.inside_for_rule,
                "outside_for_side": range_context.outside_for_side,
                "source_event_id": range_context.source_event_id,
                "source_episode_id": range_context.source_episode_id,
                "observation_accepted_range_inside": (
                    observation.accepted_range_inside if observation is not None else None
                ),
                "observation_accepted_range_low": (
                    observation.accepted_range_low if observation is not None else None
                ),
                "observation_accepted_range_high": (
                    observation.accepted_range_high if observation is not None else None
                ),
            }
        return AdvisorDecision(
            symbol=snapshot.symbol,
            snapshot_time=snapshot.snapshot_time,
            action=action,
            selected_candidate_id=candidate.candidate_id,
            reason_codes=reasons,
            diagnostics=diagnostics,
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
