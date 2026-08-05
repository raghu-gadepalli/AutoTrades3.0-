"""Strict causal deployment Advisor for Auction candidates and deferred entries.

The Advisor never discovers a setup, changes Auction structure or episode state, closes a signal,
or manages a trade.  It answers only whether deployment quality is ALLOW,
WATCH, or BLOCK at the current completed snapshot.  Required context failures
raise; there is no fail-open path.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Dict, List, Optional, Tuple

from configs.stock_advisor_config import STOCK_ADVISOR_CONFIG, StockAdvisorPolicyConfig
from enums.auction_engine import AdvisorAction, AuctionEventType, SetupFamily, TradeSide
from schemas.signal import SignalSchema
from schemas.snapshot import SnapshotSchema
from services.auction_engine.contracts import AdvisorDecision
from services.auction_engine.setup_contracts import AuthoritativeSetupCandidate
from services.advisor_context.service import (
    AdvisorContextService,
    StockAdvisorContextProviderProtocol,
)
from services.advisor_context.contracts import StockAdvisorContextAssessment
from services.signals.stock_advisor_context import (
    AdvisorDayPathSummary,
    DeferredEntryFreshnessSummary,
    evaluate_deferred_entry_freshness,
    is_mature_narrow_range_churn,
    summarise_barrier,
    summarise_day_path,
    summarise_episode_history,
)
from services.signals.stock_advisor_history import (
    StockAdvisorHistoryProvider,
    StockAdvisorHistoryProviderProtocol,
)



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
        history_provider: Optional[StockAdvisorHistoryProviderProtocol] = None,
        context_provider: Optional[StockAdvisorContextProviderProtocol] = None,
    ) -> None:
        self.policy = config
        self.history_provider = history_provider or StockAdvisorHistoryProvider()
        self.context_provider = context_provider or AdvisorContextService(config=config)

    def evaluate_authoritative(
        self,
        snapshot: SnapshotSchema,
        candidate: AuthoritativeSetupCandidate,
    ) -> AdvisorDecision:
        self._validate_candidate_inputs(snapshot, candidate)
        advisor_context = self.context_provider.assess(
            symbol=snapshot.symbol,
            as_of=snapshot.snapshot_time,
        )
        if not self.policy.enabled:
            return self._decision(
                snapshot,
                candidate,
                AdvisorAction.ALLOW,
                ("ADVISOR_DISABLED_ALLOW",),
                (),
                range_context=None,
                context_diagnostics={},
                advisor_context=advisor_context,
            )

        balance = snapshot.auction.balance
        assert balance is not None
        family = candidate.setup_family.value
        subtype = candidate.setup_subtype.upper()
        matches: List[Tuple[str, str]] = []
        applied_exceptions: List[str] = []
        range_context = self._resolve_range_context(snapshot, candidate)

        prior_opportunities = self.history_provider.fetch_prior_opportunities(
            symbol=snapshot.symbol,
            trading_day=snapshot.snapshot_time.date(),
            before_time=snapshot.snapshot_time,
            limit=self.policy.prior_opportunity_limit,
        )
        prior_snapshots = self.history_provider.fetch_day_snapshots(
            symbol=snapshot.symbol,
            trading_day=snapshot.snapshot_time.date(),
            through_time=snapshot.snapshot_time,
            limit=self.policy.day_history_limit,
            include_current=False,
        )
        day_snapshots = [*prior_snapshots, snapshot]

        inside_exempt = bool(
            family in self._normalised(self.policy.inside_range_exempt_families)
            or subtype in self._normalised(self.policy.inside_range_exempt_subtypes)
        )
        if range_context.inside_for_rule:
            if not inside_exempt:
                matches.append((
                    self.policy.inside_accepted_range_action,
                    "INSIDE_ACCEPTED_RANGE",
                ))

        if family in self._normalised(
            self.policy.accepted_breakout_current_context_families
        ) and not range_context.outside_for_side:
            matches.append((
                self.policy.accepted_breakout_current_context_action,
                "ACCEPTED_BREAKOUT_NOT_CURRENTLY_OUTSIDE",
            ))

        day_path = self._day_path_summary(
            snapshot=snapshot,
            day_snapshots=day_snapshots,
            range_context=range_context,
            candidate=candidate,
        )
        churn_matched = False
        churn_facts: Tuple[str, ...] = ()
        churn_policy = self.policy.mature_range_churn
        if (
            churn_policy.enabled
            and family in self._normalised(churn_policy.families)
            and range_context.low is not None
            and range_context.high is not None
        ):
            churn_matched, churn_facts = is_mature_narrow_range_churn(
                day_path,
                failed_escape_count=balance.failed_escape_count,
                policy=churn_policy,
            )
            if churn_matched:
                matches.append((churn_policy.action, "MATURE_NARROW_RANGE_CHURN"))

        episode_history = summarise_episode_history(
            prior_opportunities,
            source_episode_id=candidate.source_episode_id,
            side=candidate.side,
            snapshot=snapshot,
            policy=self.policy.repeated_episode,
        )
        if (
            self.policy.repeated_episode.enabled
            and episode_history.exhausted_objective_context
        ):
            matches.append((
                self.policy.repeated_episode.action,
                "REPEATED_EXHAUSTED_SAME_EPISODE_DEPLOYMENT",
            ))

        barrier = summarise_barrier(
            snapshot,
            prior_snapshots,
            side=candidate.side,
            policy=self.policy.barriers,
            excluded_price=range_context.reference_price,
        )
        if (
            self.policy.barriers.enabled
            and family in self._normalised(self.policy.barriers.families)
            and barrier.active
        ):
            direction = "UPSIDE" if candidate.side is TradeSide.BUY else "DOWNSIDE"
            assert barrier.barrier_type is not None
            matches.append((
                self.policy.barriers.action,
                f"{direction}_BARRIER_NOT_CLEARED_{barrier.barrier_type}",
            ))

        action = self._resolve_action(matches)
        reasons = tuple(
            reason for configured, reason in matches if configured == action.value
        )
        if not reasons:
            reasons = ("ADVISOR_ALLOW",)
        if action is AdvisorAction.ALLOW and applied_exceptions:
            reasons = tuple(dict.fromkeys((*reasons, *applied_exceptions)))
        return self._decision(
            snapshot,
            candidate,
            action,
            reasons,
            tuple(matches),
            range_context=range_context,
            context_diagnostics={
                "applied_exceptions": list(applied_exceptions),
                "day_path": day_path.to_dict(),
                "mature_range_churn": {
                    "matched": churn_matched,
                    "facts": list(churn_facts),
                },
                "episode_history": episode_history.to_dict(),
                "barrier": barrier.to_dict(),
            },
            advisor_context=advisor_context,
        )

    def evaluate_deferred_entry(
        self,
        *,
        signal: SignalSchema,
        snapshot: SnapshotSchema,
    ) -> AdvisorDecision:
        """Evaluate entry freshness for an already-open undeployed signal.

        WATCH means keep the signal open but do not create a trade package at
        this cadence.  It never closes or invalidates the signal.
        """
        if not isinstance(signal, SignalSchema):
            raise TypeError("Deferred StockAdvisor requires SignalSchema")
        if not isinstance(snapshot, SnapshotSchema):
            raise TypeError("Deferred StockAdvisor requires SnapshotSchema")
        symbol = snapshot.symbol.strip().upper()
        signal_symbol = str(signal.symbol or signal.equity_ref or "").strip().upper()
        if signal_symbol != symbol:
            raise ValueError("Deferred StockAdvisor signal/snapshot symbol mismatch")
        if (
            snapshot.auction.status != "OK"
            or snapshot.auction.directional is None
            or snapshot.auction.balance is None
        ):
            raise ValueError(
                "Deferred StockAdvisor requires authoritative Auction projection"
            )
        if signal.first_seen_time is None:
            raise ValueError("Deferred StockAdvisor requires signal.first_seen_time")
        side = TradeSide(str(getattr(signal.side, "value", signal.side)).upper())
        advisor_context = self.context_provider.assess(
            symbol=symbol,
            as_of=snapshot.snapshot_time,
        )

        if not self.policy.enabled or not self.policy.deferred_entry.enabled:
            summary = DeferredEntryFreshnessSummary(
                applicable=False,
                fresh=True,
                reason="DEFERRED_ENTRY_ADVISOR_DISABLED",
                age_minutes=None,
                matching_fresh_events=(),
                pullback_detected=False,
                pullback_atr=None,
                resumption_detected=False,
                resumption_atr=None,
                consolidation_detected=False,
                consolidation_range_atr=None,
                consolidation_break_detected=False,
                bars_considered=0,
            )
            action = AdvisorAction.ALLOW
            reasons = ("DEFERRED_ENTRY_ADVISOR_DISABLED",)
        else:
            prior_snapshots = self.history_provider.fetch_day_snapshots(
                symbol=symbol,
                trading_day=snapshot.snapshot_time.date(),
                through_time=snapshot.snapshot_time,
                limit=self.policy.deferred_entry.history_bars,
                include_current=False,
            )
            signal_time = self._naive_time(signal.first_seen_time)
            causal = [
                row
                for row in prior_snapshots
                if self._naive_time(row.snapshot_time) >= signal_time
            ]
            causal.append(snapshot)
            summary = evaluate_deferred_entry_freshness(
                snapshots=causal,
                signal_created_time=signal.first_seen_time,
                side=side,
                policy=self.policy.deferred_entry,
            )
            action = AdvisorAction.ALLOW if summary.fresh else AdvisorAction.WATCH
            reasons = (summary.reason,)

        return AdvisorDecision(
            symbol=symbol,
            snapshot_time=snapshot.snapshot_time,
            action=action,
            selected_candidate_id=str(signal.signal_id),
            reason_codes=reasons,
            diagnostics={
                "deployment_scope": "DEFERRED_TRADE_ENTRY_ONLY",
                "signal_id": signal.signal_id,
                "signal_setup": str(getattr(signal.setup, "value", signal.setup)),
                "signal_side": side.value,
                "freshness": summary.to_dict(),
                "advisor_context": advisor_context.to_diagnostics(),
            },
        )

    @staticmethod
    def _validate_candidate_inputs(
        snapshot: SnapshotSchema,
        candidate: AuthoritativeSetupCandidate,
    ) -> None:
        if not isinstance(snapshot, SnapshotSchema):
            raise TypeError("StockAdvisor requires SnapshotSchema")
        if not isinstance(candidate, AuthoritativeSetupCandidate):
            raise TypeError("StockAdvisor requires AuthoritativeSetupCandidate")
        if (
            snapshot.auction.status != "OK"
            or snapshot.auction.directional is None
            or snapshot.auction.balance is None
        ):
            raise ValueError(
                "StockAdvisor requires authoritative Auction projection"
            )
        symbol = snapshot.symbol.strip().upper()
        if candidate.symbol != symbol:
            raise ValueError("StockAdvisor candidate/snapshot symbol mismatch")
        if candidate.snapshot_time != snapshot.snapshot_time:
            raise ValueError("StockAdvisor candidate/snapshot time mismatch")

    def _day_path_summary(
        self,
        *,
        snapshot: SnapshotSchema,
        day_snapshots: List[SnapshotSchema],
        range_context: _AdvisorRangeContext,
        candidate: AuthoritativeSetupCandidate,
    ) -> AdvisorDayPathSummary:
        balance = snapshot.auction.balance
        assert balance is not None
        if balance.episode_id == candidate.source_episode_id:
            started_at = balance.started_at
            containment = balance.containment_ratio
        else:
            started_at = snapshot.structure.accepted.promoted_time
            containment = (
                balance.containment_ratio
                if balance.episode_id is not None
                else None
            )
        return summarise_day_path(
            day_snapshots,
            range_low=range_context.low,
            range_high=range_context.high,
            episode_started_at=started_at,
            containment_ratio=containment,
        )

    def _resolve_range_context(
        self,
        snapshot: SnapshotSchema,
        candidate: AuthoritativeSetupCandidate,
    ) -> _AdvisorRangeContext:
        if candidate.source_event_type in _SOURCE_RANGE_EVENT_TYPES:
            return self._source_episode_range_context(snapshot, candidate)
        return self._current_accepted_range_context(snapshot, candidate)

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

        matching_events = [
            event
            for event in snapshot.auction.events
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
    def _current_accepted_range_context(
        snapshot: SnapshotSchema,
        candidate: AuthoritativeSetupCandidate,
    ) -> _AdvisorRangeContext:
        accepted = snapshot.structure.accepted
        accepted_range = accepted.range
        range_valid = bool(
            accepted_range is not None
            and accepted_range.low is not None
            and accepted_range.high is not None
            and accepted_range.breakout_eligible
            and not accepted_range.provisional
            and accepted.frozen
        )
        low = (
            float(accepted_range.low)
            if range_valid and accepted_range is not None
            else None
        )
        high = (
            float(accepted_range.high)
            if range_valid and accepted_range is not None
            else None
        )
        if range_valid:
            assert low is not None and high is not None
            if (
                not math.isfinite(low)
                or not math.isfinite(high)
                or low <= 0.0
                or high <= low
            ):
                raise ValueError(
                    "StockAdvisor accepted structure has invalid range geometry"
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
            authority="CURRENT_ACCEPTED_STRUCTURE",
            low=low,
            high=high,
            reference_price=reference,
            inside_for_rule=bool(
                range_valid
                and low is not None
                and high is not None
                and low <= float(snapshot.close) <= high
            ),
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
        context_diagnostics: Dict[str, Any],
        advisor_context: StockAdvisorContextAssessment,
    ) -> AdvisorDecision:
        accepted = snapshot.structure.accepted
        accepted_range = accepted.range
        diagnostics: Dict[str, Any] = {
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
            "advisor_context": advisor_context.to_diagnostics(),
            **context_diagnostics,
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
                "accepted_structure_inside": bool(
                    accepted_range is not None
                    and accepted_range.low is not None
                    and accepted_range.high is not None
                    and float(accepted_range.low)
                    <= float(snapshot.close)
                    <= float(accepted_range.high)
                ),
                "accepted_structure_low": (
                    accepted_range.low if accepted_range is not None else None
                ),
                "accepted_structure_high": (
                    accepted_range.high if accepted_range is not None else None
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

    @staticmethod
    def _naive_time(value: Any):
        if not hasattr(value, "replace"):
            raise TypeError("Advisor timestamp must be datetime")
        if getattr(value, "tzinfo", None) is not None:
            from utils.datetime_utils import IST
            value = value.astimezone(IST)
        return value.replace(tzinfo=None)


__all__ = ["StockAdvisor"]
