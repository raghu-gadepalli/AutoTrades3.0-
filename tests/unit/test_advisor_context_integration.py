from __future__ import annotations

import pytest

from configs.stock_advisor_config import STOCK_ADVISOR_CONFIG
from enums.advisor_context import ContextAvailability, ContextInfluence, MarketRegimeState
from enums.auction_engine import AdvisorAction, AuctionEventType, DirectionalBias, SetupFamily
from services.auction_engine.event_driven_setup_engine import EventDrivenSetupEngine
from services.auction_engine.setup_event_router import AuthoritativeSetupEventRouter
from services.market.market_regime import MarketRegimeService
from services.advisor_context.service import AdvisorContextService
from services.signals.stock_advisor import StockAdvisor
from tests.unit.advisor_context_test_fixtures import StaticAdvisorContextProvider
from tests.unit.test_event_driven_setup_engine import TS, _event_snapshot


class _EmptyHistoryProvider:
    def fetch_prior_opportunities(self, **kwargs):
        return []

    def fetch_day_snapshots(self, **kwargs):
        return []


def _candidate(snapshot):
    routes = AuthoritativeSetupEventRouter().route_authority(
        events=snapshot.auction.events, permissions=snapshot.auction.permissions
    )
    evaluations = EventDrivenSetupEngine().evaluate(snapshot, routes)
    approved = [item for item in evaluations if item.approved]
    assert len(approved) == 1
    assert approved[0].candidate is not None
    return approved[0].candidate


def test_market_regime_service_freezes_neutral_contract_and_caches() -> None:
    service = MarketRegimeService()

    first = service.assess(as_of=TS)
    second = service.assess(as_of=TS)

    assert first is second
    assert first.availability is ContextAvailability.UNAVAILABLE
    assert first.state is MarketRegimeState.UNKNOWN
    assert first.confidence == 0.0
    assert first.reason_codes == ("MARKET_REGIME_NOT_IMPLEMENTED",)
    assert first.hysteresis.transition_pending is False


def test_advisor_persists_market_regime_context_without_changing_action() -> None:
    snapshot = _event_snapshot(
        AuctionEventType.BALANCE_ESCAPE_STARTED,
        SetupFamily.BREAKOUT_INITIATION,
        direction=DirectionalBias.UP,
        close=101.2,
        data={"frozen_low": 99.0, "frozen_high": 101.0},
    )
    advisor = StockAdvisor(
        history_provider=_EmptyHistoryProvider(),
        context_provider=StaticAdvisorContextProvider(),
    )

    decision = advisor.evaluate_authoritative(snapshot, _candidate(snapshot))

    assert decision.action is AdvisorAction.ALLOW
    context = decision.diagnostics["advisor_context"]
    assert context["market_regime_influence"] == ContextInfluence.NONE.value
    assert context["market_regime"]["state"] == MarketRegimeState.UNKNOWN.value
    assert context["market_regime"]["availability"] == ContextAvailability.UNAVAILABLE.value


def test_market_regime_context_policy_cannot_influence_decisions() -> None:
    assert STOCK_ADVISOR_CONFIG.market_regime_context.influence == "NONE"

    invalid = STOCK_ADVISOR_CONFIG.model_copy(
        update={
            "market_regime_context": STOCK_ADVISOR_CONFIG.market_regime_context.model_copy(
                update={"influence": "WEIGHTED"}
            )
        }
    )
    with pytest.raises(ValueError, match="must remain NONE"):
        AdvisorContextService(config=invalid)
