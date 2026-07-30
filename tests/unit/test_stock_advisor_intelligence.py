from __future__ import annotations

from datetime import timedelta

from enums.auction_engine import (
    AdvisorAction,
    AuctionEventType,
    BalanceEpisodeState,
    DirectionalBias,
    SetupFamily,
    TradeSide,
)
from schemas.stock_opportunity import StockOpportunitySchema
from services.auction_engine.event_driven_setup_engine import EventDrivenSetupEngine
from services.auction_engine.setup_event_router import AuthoritativeSetupEventRouter
from services.signals.stock_advisor import StockAdvisor
from services.signals.stock_advisor_context import evaluate_deferred_entry_freshness
from configs.stock_advisor_config import STOCK_ADVISOR_CONFIG
from tests.unit.test_event_driven_setup_engine import TS, _event_snapshot


class _StaticHistoryProvider:
    def __init__(self, *, opportunities=(), snapshots=()):
        self.opportunities = list(opportunities)
        self.snapshots = list(snapshots)

    def fetch_prior_opportunities(self, **kwargs):
        return list(self.opportunities)

    def fetch_day_snapshots(self, **kwargs):
        return list(self.snapshots)


def _candidate(snapshot):
    routes = AuthoritativeSetupEventRouter().route(snapshot.auction.lifecycle)
    evaluations = EventDrivenSetupEngine().evaluate(snapshot, routes)
    approved = [item for item in evaluations if item.approved]
    assert len(approved) == 1
    assert approved[0].candidate is not None
    return approved[0].candidate


def _market_copy(snapshot, close: float, *, red: bool | None = None, vwap: float = 100.25):
    if red is None:
        open_price = close
    elif red:
        open_price = close + 0.10
    else:
        open_price = close - 0.10
    bar = snapshot.bar.model_copy(
        update={
            "open": open_price,
            "high": max(open_price, close) + 0.05,
            "low": min(open_price, close) - 0.05,
            "close": close,
        }
    )
    vwap_block = snapshot.indicators.vwap.model_copy(
        update={
            "value": vwap,
            "side": "ABOVE" if close > vwap else "BELOW",
            "distance_points": close - vwap,
            "distance_pct": (close - vwap) / vwap * 100.0,
            "distance_atr": close - vwap,
        }
    )
    indicators = snapshot.indicators.model_copy(update={"vwap": vwap_block})
    return snapshot.model_copy(update={"close": close, "bar": bar, "indicators": indicators})


def _locked_balance_snapshot(snapshot, *, attempts: int = 1, failures: int = 0):
    balance = snapshot.auction.lifecycle.balance.model_copy(
        update={
            "episode_id": "EPISODE:1",
            "previous_state": BalanceEpisodeState.LOCKED,
            "current_state": BalanceEpisodeState.ESCAPE_WATCH,
            "started_at": TS - timedelta(minutes=45),
            "state_started_at": TS,
            "state_age_bars": 1,
            "range_id": "RANGE:1",
            "candidate_low": None,
            "candidate_high": None,
            "frozen_low": 99.0,
            "frozen_high": 101.0,
            "forming_bars_observed": 15,
            "containment_bars": 12,
            "containment_ratio": 0.8,
            "escape_attempt_count": attempts,
            "failed_escape_count": failures,
            "up_escape_attempt_count": attempts,
            "down_escape_attempt_count": 0,
            "escape_direction": DirectionalBias.UP,
        }
    )
    lifecycle = snapshot.auction.lifecycle.model_copy(update={"balance": balance})
    auction = snapshot.auction.model_copy(update={"lifecycle": lifecycle})
    return snapshot.model_copy(update={"auction": auction})


def test_mature_narrow_range_churn_blocks_candidate() -> None:
    current = _event_snapshot(
        AuctionEventType.BALANCE_ESCAPE_STARTED,
        SetupFamily.BREAKOUT_INITIATION,
        direction=DirectionalBias.UP,
        close=101.2,
        data={"frozen_low": 99.0, "frozen_high": 101.0},
        episode_id="EPISODE:1",
    )
    current = _locked_balance_snapshot(current)
    closes = [99.20, 100.80, 99.30, 100.70, 99.25, 100.75, 99.35,
              100.65, 99.25, 100.80, 99.30, 100.70, 99.20, 100.75]
    history = [_market_copy(current, close, vwap=100.0) for close in closes]
    churn_policy = STOCK_ADVISOR_CONFIG.mature_range_churn.model_copy(
        update={"max_range_width_pct": 2.5}
    )
    config = STOCK_ADVISOR_CONFIG.model_copy(
        update={"mature_range_churn": churn_policy}
    )
    decision = StockAdvisor(
        config=config,
        history_provider=_StaticHistoryProvider(snapshots=history),
    ).evaluate_authoritative(current, _candidate(current))

    assert decision.action is AdvisorAction.BLOCK
    assert "MATURE_NARROW_RANGE_CHURN" in decision.reason_codes
    assert decision.diagnostics["mature_range_churn"]["matched"] is True


def test_uncleared_major_barrier_watches_candidate() -> None:
    current = _event_snapshot(
        AuctionEventType.BALANCE_ESCAPE_STARTED,
        SetupFamily.BREAKOUT_INITIATION,
        direction=DirectionalBias.UP,
        close=101.2,
        data={"frozen_low": 99.0, "frozen_high": 101.0},
        episode_id="EPISODE:1",
    )
    opening_range = current.levels.opening_range.model_copy(
        update={"high": 101.5, "low": 99.0, "ready": True}
    )
    levels = current.levels.model_copy(update={"opening_range": opening_range})
    current = current.model_copy(update={"levels": levels})
    decision = StockAdvisor(
        history_provider=_StaticHistoryProvider()
    ).evaluate_authoritative(current, _candidate(current))

    assert decision.action is AdvisorAction.WATCH
    assert "UPSIDE_BARRIER_NOT_CLEARED_ORH" in decision.reason_codes


def test_repeated_exhausted_same_episode_deployment_blocks() -> None:
    current = _event_snapshot(
        AuctionEventType.BALANCE_ESCAPE_STARTED,
        SetupFamily.BREAKOUT_INITIATION,
        direction=DirectionalBias.UP,
        close=101.2,
        data={"frozen_low": 99.0, "frozen_high": 101.0},
        episode_id="EPISODE:1",
    )
    current = _locked_balance_snapshot(current, attempts=2, failures=1)
    prior = StockOpportunitySchema.model_construct(
        opportunity_key="OPP:PRIOR",
        source_episode_id="EPISODE:1",
        latest_episode_id="EPISODE:1",
        side="BUY",
    )
    decision = StockAdvisor(
        history_provider=_StaticHistoryProvider(opportunities=[prior])
    ).evaluate_authoritative(current, _candidate(current))

    assert decision.action is AdvisorAction.BLOCK
    assert "REPEATED_EXHAUSTED_SAME_EPISODE_DEPLOYMENT" in decision.reason_codes
    history = decision.diagnostics["episode_history"]
    assert history["prior_same_side_same_episode"] == 1
    assert "FAILED_BALANCE_ESCAPE" in history["exhaustion_facts"]


def test_delayed_entry_without_reset_is_watch() -> None:
    base = _event_snapshot(
        AuctionEventType.DIRECTIONAL_REVERSAL_LEG_ESTABLISHED,
        SetupFamily.REVERSAL,
        direction=DirectionalBias.DOWN,
        close=100.0,
        data={"origin_price": 102.0},
    )
    lifecycle = base.auction.lifecycle.model_copy(update={"events": ()})
    base = base.model_copy(
        update={"auction": base.auction.model_copy(update={"lifecycle": lifecycle})}
    )
    rows = [
        _market_copy(base, 102.0, red=True),
        _market_copy(base, 101.5, red=True),
        _market_copy(base, 101.0, red=True),
        _market_copy(base, 100.5, red=True),
        _market_copy(base, 100.0, red=True),
    ]
    summary = evaluate_deferred_entry_freshness(
        snapshots=rows,
        signal_created_time=TS - timedelta(minutes=15),
        side=TradeSide.SELL,
        policy=STOCK_ADVISOR_CONFIG.deferred_entry,
    )

    assert summary.applicable is True
    assert summary.fresh is False
    assert summary.reason == "UNCLEAR_DELAYED_ENTRY_FRESHNESS"


def test_delayed_entry_pullback_resumption_is_fresh() -> None:
    base = _event_snapshot(
        AuctionEventType.DIRECTIONAL_REVERSAL_LEG_ESTABLISHED,
        SetupFamily.REVERSAL,
        direction=DirectionalBias.DOWN,
        close=99.2,
        data={"origin_price": 102.0},
    )
    lifecycle = base.auction.lifecycle.model_copy(update={"events": ()})
    base = base.model_copy(
        update={"auction": base.auction.model_copy(update={"lifecycle": lifecycle})}
    )
    rows = [
        _market_copy(base, 101.0, red=True),
        _market_copy(base, 100.0, red=True),
        _market_copy(base, 99.0, red=True),
        _market_copy(base, 99.5, red=False),
        _market_copy(base, 99.8, red=False),
        _market_copy(base, 99.2, red=True),
    ]
    summary = evaluate_deferred_entry_freshness(
        snapshots=rows,
        signal_created_time=TS - timedelta(minutes=18),
        side=TradeSide.SELL,
        policy=STOCK_ADVISOR_CONFIG.deferred_entry,
    )

    assert summary.fresh is True
    assert summary.reason == "PULLBACK_RESUMPTION_CONFIRMED"
    assert summary.pullback_detected is True
    assert summary.resumption_detected is True
