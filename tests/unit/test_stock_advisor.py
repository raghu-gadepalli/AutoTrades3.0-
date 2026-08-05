from __future__ import annotations

from configs.stock_advisor_config import STOCK_ADVISOR_CONFIG
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


def _snapshot_with_accepted_range(
    snapshot: SnapshotSchema,
    *,
    low: float,
    high: float,
    inside: bool,
) -> SnapshotSchema:
    payload = snapshot.model_dump(mode="python", by_alias=True)
    accepted_range = payload["structure"]["accepted"]["range"]
    accepted_range["low"] = low
    accepted_range["high"] = high
    accepted_range["breakout_eligible"] = True
    accepted_range["provisional"] = False
    if inside and not (low <= float(payload["close"]) <= high):
        raise ValueError("inside=True requires close within the accepted range")
    if not inside and low <= float(payload["close"]) <= high:
        raise ValueError("inside=False requires close outside the accepted range")
    return SnapshotSchema.model_validate(payload)


def _candidate(snapshot: SnapshotSchema):
    routes = AuthoritativeSetupEventRouter().route_authority(events=snapshot.auction.events, permissions=snapshot.auction.permissions)
    evaluations = EventDrivenSetupEngine().evaluate(snapshot, routes)
    approved = [evaluation for evaluation in evaluations if evaluation.approved]
    assert len(approved) == 1
    assert approved[0].candidate is not None
    return approved[0].candidate


def test_deferred_entry_freshness_uses_current_directional_event_name() -> None:
    assert STOCK_ADVISOR_CONFIG.deferred_entry.accepted_fresh_event_types == (
        "BALANCE_ESCAPE_STARTED",
        "BALANCE_ESCAPE_ACCEPTED",
        "DIRECTIONAL_REVERSED",
    )


def test_breakout_initiation_uses_frozen_source_range_not_current_accepted_range() -> None:
    snapshot = _event_snapshot(
        AuctionEventType.BALANCE_ESCAPE_STARTED,
        SetupFamily.BREAKOUT_INITIATION,
        direction=DirectionalBias.UP,
        close=101.2,
        data={"frozen_low": 99.0, "frozen_high": 101.0},
    )
    snapshot = _snapshot_with_accepted_range(
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
    assert context["inside_for_rule"] is False


def test_accepted_breakout_uses_frozen_source_range_not_current_accepted_range() -> None:
    snapshot = _event_snapshot(
        AuctionEventType.BALANCE_ESCAPE_ACCEPTED,
        SetupFamily.ACCEPTED_BREAKOUT,
        direction=DirectionalBias.DOWN,
        close=98.8,
        data={"frozen_low": 99.0, "frozen_high": 101.0},
    )
    snapshot = _snapshot_with_accepted_range(
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


def test_non_balance_candidate_uses_current_accepted_range() -> None:
    snapshot = _event_snapshot(
        AuctionEventType.DIRECTIONAL_REVERSED,
        SetupFamily.REVERSAL,
        direction=DirectionalBias.UP,
        close=101.2,
        data={"start_price": 100.5},
    )
    snapshot = _snapshot_with_accepted_range(
        snapshot,
        low=99.0,
        high=102.0,
        inside=True,
    )

    decision = _advisor().evaluate_authoritative(snapshot, _candidate(snapshot))

    assert decision.action is AdvisorAction.WATCH
    assert decision.reason_codes == ("INSIDE_ACCEPTED_RANGE",)
    assert decision.diagnostics["applied_exceptions"] == []
    context = decision.diagnostics["range_context"]
    assert context["authority"] == "CURRENT_ACCEPTED_STRUCTURE"
    assert context["outside_for_side"] is False



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
