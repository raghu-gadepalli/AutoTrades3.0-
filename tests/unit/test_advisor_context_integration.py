from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from configs.stock_advisor_config import STOCK_ADVISOR_CONFIG
from enums.advisor_context import (
    ContextAvailability,
    ContextInfluence,
    MarketRegimeState,
    StockRankTier,
)
from enums.auction_engine import AdvisorAction, AuctionEventType, DirectionalBias, SetupFamily
from services.auction_engine.event_driven_setup_engine import EventDrivenSetupEngine
from services.auction_engine.setup_event_router import AuthoritativeSetupEventRouter
from services.market.market_regime import MarketRegimeService
from services.advisor_context.service import AdvisorContextService, StockRankContextProvider
from services.advisor_context.contracts import StockRankAssessment
from services.signals.stock_advisor import StockAdvisor
from tests.unit.advisor_context_test_fixtures import StaticAdvisorContextProvider
from tests.unit.test_event_driven_setup_engine import TS, _event_snapshot


class _EmptyHistoryProvider:
    def fetch_prior_opportunities(self, **kwargs):
        return []

    def fetch_day_snapshots(self, **kwargs):
        return []


def _candidate(snapshot):
    routes = AuthoritativeSetupEventRouter().route(snapshot.auction.lifecycle)
    evaluations = EventDrivenSetupEngine().evaluate(snapshot, routes)
    approved = [item for item in evaluations if item.approved]
    assert len(approved) == 1
    assert approved[0].candidate is not None
    return approved[0].candidate


def _rank_row(*, rank_time):
    return SimpleNamespace(
        rank_time=rank_time,
        rank_position=7,
        universe_size=80,
        attention_tier="PRIORITY",
        direction="UP",
        classification="MOVING_UP",
        total_score=48.5,
        movement_score=51.0,
        quality_score=43.0,
        range_penalty=5.0,
        stall_penalty=2.0,
    )


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


def test_stock_rank_context_provider_maps_fresh_and_stale(monkeypatch) -> None:
    provider = StockRankContextProvider()
    current = _rank_row(rank_time=TS - timedelta(minutes=6))
    monkeypatch.setattr(
        "services.advisor_context.service.StockRankSchema.fetch_latest_for_symbol_at_or_before",
        lambda **kwargs: current,
    )

    fresh = provider.assess(symbol="test", as_of=TS, max_age_seconds=540.0)
    stale = provider.assess(symbol="test", as_of=TS, max_age_seconds=300.0)

    assert fresh.availability is ContextAvailability.AVAILABLE
    assert fresh.fresh is True
    assert fresh.rank_position == 7
    assert fresh.attention_tier is StockRankTier.PRIORITY
    assert fresh.reason_codes == ("STOCK_RANK_AVAILABLE",)

    assert stale.availability is ContextAvailability.STALE
    assert stale.fresh is False
    assert stale.age_seconds == 360.0
    assert stale.reason_codes == ("STOCK_RANK_STALE",)


def test_stock_rank_context_provider_reports_explicit_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        "services.advisor_context.service.StockRankSchema.fetch_latest_for_symbol_at_or_before",
        lambda **kwargs: None,
    )

    result = StockRankContextProvider().assess(
        symbol="TEST",
        as_of=TS,
        max_age_seconds=540.0,
    )

    assert result.availability is ContextAvailability.UNAVAILABLE
    assert result.rank_time is None
    assert result.rank_position is None
    assert result.reason_codes == ("STOCK_RANK_NOT_AVAILABLE",)


def test_advisor_persists_both_contexts_without_changing_action() -> None:
    snapshot = _event_snapshot(
        AuctionEventType.BALANCE_ESCAPE_STARTED,
        SetupFamily.BREAKOUT_INITIATION,
        direction=DirectionalBias.UP,
        close=101.2,
        data={"frozen_low": 99.0, "frozen_high": 101.0},
    )
    rank = StockRankAssessment(
        symbol=snapshot.symbol,
        as_of=snapshot.snapshot_time,
        availability=ContextAvailability.AVAILABLE,
        rank_time=snapshot.snapshot_time - timedelta(minutes=3),
        age_seconds=180.0,
        fresh=True,
        rank_position=7,
        universe_size=80,
        attention_tier=StockRankTier.PRIORITY,
        direction="UP",
        classification="MOVING_UP",
        total_score=48.5,
        movement_score=51.0,
        quality_score=43.0,
        range_penalty=5.0,
        stall_penalty=2.0,
        reason_codes=("STOCK_RANK_AVAILABLE",),
    )
    advisor = StockAdvisor(
        history_provider=_EmptyHistoryProvider(),
        context_provider=StaticAdvisorContextProvider(stock_rank=rank),
    )

    decision = advisor.evaluate_authoritative(snapshot, _candidate(snapshot))

    assert decision.action is AdvisorAction.ALLOW
    context = decision.diagnostics["advisor_context"]
    assert context["stock_rank_influence"] == ContextInfluence.DIAGNOSTIC.value
    assert context["market_regime_influence"] == ContextInfluence.NONE.value
    assert context["stock_rank"]["rank_position"] == 7
    assert context["stock_rank"]["attention_tier"] == StockRankTier.PRIORITY.value
    assert context["market_regime"]["state"] == MarketRegimeState.UNKNOWN.value
    assert context["market_regime"]["availability"] == ContextAvailability.UNAVAILABLE.value


def test_context_policy_cannot_influence_decisions_in_initial_patch() -> None:
    assert STOCK_ADVISOR_CONFIG.stock_rank_context.influence == "DIAGNOSTIC"
    assert STOCK_ADVISOR_CONFIG.market_regime_context.influence == "NONE"

    invalid = STOCK_ADVISOR_CONFIG.model_copy(
        update={
            "stock_rank_context": STOCK_ADVISOR_CONFIG.stock_rank_context.model_copy(
                update={"influence": "WEIGHTED"}
            )
        }
    )
    with pytest.raises(ValueError, match="must remain DIAGNOSTIC"):
        AdvisorContextService(config=invalid)
