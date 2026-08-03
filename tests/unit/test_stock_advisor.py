from __future__ import annotations

from enums.auction_engine import (
    AdvisorAction,
    AuctionEventType,
    DirectionalBias,
    SetupFamily,
)
from schemas.snapshot import SnapshotSchema
from services.auction_engine.event_driven_setup_engine import EventDrivenSetupEngine
from services.auction_engine.setup_event_router import AuthoritativeSetupEventRouter
from services.signals.stock_advisor import StockAdvisor
from tests.unit.advisor_context_test_fixtures import StaticAdvisorContextProvider
from tests.unit.test_event_driven_setup_engine import _event_snapshot


class _EmptyHistoryProvider:
    def fetch_prior_opportunities(self, **kwargs):
        return []

    def fetch_day_snapshots(self, **kwargs):
        return []


def _advisor() -> StockAdvisor:
    return StockAdvisor(
        history_provider=_EmptyHistoryProvider(),
        context_provider=StaticAdvisorContextProvider(),
    )


def _snapshot_with_observation_range(
    snapshot: SnapshotSchema,
    *,
    low: float,
    high: float,
    inside: bool,
) -> SnapshotSchema:
    payload = snapshot.model_dump(mode="python", by_alias=True)
    observation = payload["auction"]["observation"]
    observation["accepted_range_low"] = low
    observation["accepted_range_high"] = high
    observation["accepted_range_inside"] = inside
    observation["accepted_range_breakout_eligible"] = True
    observation["accepted_range_provisional"] = False
    observation["exhaustion_active"] = False
    observation["exhausted_side"] = "UNKNOWN"
    return SnapshotSchema.model_validate(payload)


def _candidate(snapshot: SnapshotSchema):
    routes = AuthoritativeSetupEventRouter().route(snapshot.auction.lifecycle)
    evaluations = EventDrivenSetupEngine().evaluate(snapshot, routes)
    approved = [evaluation for evaluation in evaluations if evaluation.approved]
    assert len(approved) == 1
    assert approved[0].candidate is not None
    return approved[0].candidate


def test_breakout_initiation_uses_frozen_source_range_not_current_observation() -> None:
    snapshot = _event_snapshot(
        AuctionEventType.BALANCE_ESCAPE_STARTED,
        SetupFamily.BREAKOUT_INITIATION,
        direction=DirectionalBias.UP,
        close=101.2,
        data={"frozen_low": 99.0, "frozen_high": 101.0},
    )
    snapshot = _snapshot_with_observation_range(
        snapshot,
        low=99.0,
        high=102.0,
        inside=True,
    )

    decision = _advisor().evaluate_authoritative(snapshot, _candidate(snapshot))

    assert decision.action is AdvisorAction.ALLOW
    assert decision.reason_codes == ("ADVISOR_ALLOW",)
    context = decision.diagnostics["range_context"]
    assert context["authority"] == "AUCTION_SOURCE_EPISODE"
    assert context["high"] == 101.0
    assert context["outside_for_side"] is True
    assert context["observation_accepted_range_inside"] is True


def test_accepted_breakout_uses_frozen_source_range_not_current_observation() -> None:
    snapshot = _event_snapshot(
        AuctionEventType.BALANCE_ESCAPE_ACCEPTED,
        SetupFamily.ACCEPTED_BREAKOUT,
        direction=DirectionalBias.DOWN,
        close=98.8,
        data={"frozen_low": 99.0, "frozen_high": 101.0},
    )
    snapshot = _snapshot_with_observation_range(
        snapshot,
        low=98.5,
        high=101.0,
        inside=True,
    )

    decision = _advisor().evaluate_authoritative(snapshot, _candidate(snapshot))

    assert decision.action is AdvisorAction.ALLOW
    assert "ACCEPTED_BREAKOUT_NOT_CURRENTLY_OUTSIDE" not in decision.reason_codes
    context = decision.diagnostics["range_context"]
    assert context["authority"] == "AUCTION_SOURCE_EPISODE"
    assert context["low"] == 99.0
    assert context["outside_for_side"] is True


def test_non_balance_candidate_still_uses_current_observation_range() -> None:
    snapshot = _event_snapshot(
        AuctionEventType.DIRECTIONAL_CONTINUATION_CONFIRMED,
        SetupFamily.CONTINUATION,
        direction=DirectionalBias.UP,
        close=101.2,
        data={"origin_price": 100.5, "protection_level": 100.0},
    )
    snapshot = _snapshot_with_observation_range(
        snapshot,
        low=99.0,
        high=102.0,
        inside=True,
    )

    decision = _advisor().evaluate_authoritative(snapshot, _candidate(snapshot))

    assert decision.action is AdvisorAction.WATCH
    assert decision.reason_codes == ("INSIDE_ACCEPTED_RANGE",)
    context = decision.diagnostics["range_context"]
    assert context["authority"] == "CURRENT_AUCTION_OBSERVATION"
    assert context["outside_for_side"] is False


def test_trend_restoration_is_not_suppressed_only_for_being_inside_range() -> None:
    snapshot = _event_snapshot(
        AuctionEventType.DIRECTIONAL_TREND_RESTORED,
        SetupFamily.CONTINUATION,
        direction=DirectionalBias.DOWN,
        close=100.5,
        data={"origin_price": 101.0, "protection_level": 102.0},
    )
    snapshot = _snapshot_with_observation_range(
        snapshot,
        low=99.0,
        high=102.0,
        inside=True,
    )
    candidate = _candidate(snapshot)

    assert candidate.setup_subtype == "TREND_RESTORATION"
    decision = _advisor().evaluate_authoritative(snapshot, candidate)

    assert decision.action is AdvisorAction.ALLOW
    assert decision.reason_codes == ("ADVISOR_ALLOW",)
    assert "INSIDE_ACCEPTED_RANGE" not in {
        match["reason"] for match in decision.diagnostics["matched_rules"]
    }
    context = decision.diagnostics["range_context"]
    assert context["authority"] == "CURRENT_AUCTION_OBSERVATION"
    assert context["inside_for_rule"] is True


def test_balance_candidate_rejects_reference_boundary_mismatch() -> None:
    snapshot = _event_snapshot(
        AuctionEventType.BALANCE_ESCAPE_STARTED,
        SetupFamily.BREAKOUT_INITIATION,
        direction=DirectionalBias.UP,
        close=101.2,
        data={"frozen_low": 99.0, "frozen_high": 101.0},
    )
    candidate = _candidate(snapshot).model_copy(update={"reference_price": 100.5})

    try:
        _advisor().evaluate_authoritative(snapshot, candidate)
    except ValueError as exc:
        assert str(exc) == "StockAdvisor candidate/source boundary mismatch"
    else:
        raise AssertionError("Expected source-boundary mismatch to fail loudly")
