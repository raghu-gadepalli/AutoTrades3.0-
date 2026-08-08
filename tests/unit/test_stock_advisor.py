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



def _stockmap_context(*, low: float = 99.0, high: float = 101.0, range_id: str = "SM:R1"):
    from datetime import timedelta
    from types import SimpleNamespace
    from tests.unit.test_event_driven_setup_engine import TS

    return SimpleNamespace(
        stockmap_time=TS,
        source_candle_time=TS - timedelta(minutes=15),
        structure=SimpleNamespace(
            accepted=SimpleNamespace(
                frozen=True,
                range=SimpleNamespace(
                    range_id=range_id,
                    low=low,
                    high=high,
                    breakout_eligible=True,
                    provisional=False,
                ),
            )
        ),
    )


def _deferred_signal(*, side: str, created_price: float, setup: str = "REVERSAL"):
    from decimal import Decimal
    from schemas.signal import SignalSchema
    from tests.unit.test_event_driven_setup_engine import TS

    return SignalSchema.model_construct(
        signal_id=f"SIG:{side}:{created_price}",
        equity_ref="TEST",
        symbol="TEST",
        lifecycle="INTRADAY",
        setup=setup,
        side=side,
        first_seen_time=TS,
        created_price=Decimal(str(created_price)),
        last_eval_time=TS,
        last_snapshot_time=TS,
    )


def _deferred_snapshot(*, close: float, minutes_after: int = 3):
    from datetime import timedelta
    from tests.unit.test_event_driven_setup_engine import TS

    base = _event_snapshot(
        AuctionEventType.DIRECTIONAL_REVERSED,
        SetupFamily.REVERSAL,
        direction=DirectionalBias.DOWN,
        close=close,
        data={"start_price": close + 1.0},
    )
    return base.model_copy(
        update={
            "symbol": "TEST",
            "snapshot_time": TS + timedelta(minutes=minutes_after),
            "close": close,
        }
    )


def test_stockmap_sell_waits_until_price_moves_away_from_frozen_high(monkeypatch) -> None:
    from schemas.stockmap import StockMapSchema

    creation_map = _stockmap_context()
    monkeypatch.setattr(
        StockMapSchema,
        "fetch_latest_for_symbol_asof",
        staticmethod(lambda symbol, asof_time: creation_map),
    )
    decision = _advisor().evaluate_deferred_entry(
        signal=_deferred_signal(side="SELL", created_price=102.0),
        snapshot=_deferred_snapshot(close=102.2),
    )

    assert decision.action is AdvisorAction.WATCH
    assert decision.reason_codes == ("STOCKMAP_EXTREME_WAIT_FAILURE_PROOF",)
    details = decision.diagnostics["stockmap_boundary_transition"]
    assert details["start_position"] == "ABOVE_RANGE"
    assert details["extreme_location_met"] is True
    assert details["moved_away_from_extreme"] is False


def test_stockmap_sell_allows_failure_without_full_range_reentry(monkeypatch) -> None:
    from schemas.stockmap import StockMapSchema

    creation_map = _stockmap_context()
    monkeypatch.setattr(
        StockMapSchema,
        "fetch_latest_for_symbol_asof",
        staticmethod(lambda symbol, asof_time: creation_map),
    )
    decision = _advisor().evaluate_deferred_entry(
        signal=_deferred_signal(side="SELL", created_price=102.0),
        snapshot=_deferred_snapshot(close=101.4),
    )

    assert decision.action is AdvisorAction.ALLOW
    assert decision.reason_codes == ("STOCKMAP_EXTREME_FAILURE_CONFIRMED",)
    details = decision.diagnostics["stockmap_boundary_transition"]
    assert details["full_reentry_confirmed"] is False
    assert details["moved_away_from_extreme"] is True


def test_stockmap_buy_allows_failure_below_frozen_low(monkeypatch) -> None:
    from schemas.stockmap import StockMapSchema

    creation_map = _stockmap_context()
    monkeypatch.setattr(
        StockMapSchema,
        "fetch_latest_for_symbol_asof",
        staticmethod(lambda symbol, asof_time: creation_map),
    )
    decision = _advisor().evaluate_deferred_entry(
        signal=_deferred_signal(side="BUY", created_price=98.0),
        snapshot=_deferred_snapshot(close=98.4),
    )

    assert decision.action is AdvisorAction.ALLOW
    assert decision.reason_codes == ("STOCKMAP_EXTREME_FAILURE_CONFIRMED",)
    details = decision.diagnostics["stockmap_boundary_transition"]
    assert details["start_position"] == "BELOW_RANGE"
    assert details["required_transition"] == "BELOW_AND_RISE"


def test_stockmap_gate_is_setup_family_agnostic(monkeypatch) -> None:
    from schemas.stockmap import StockMapSchema

    creation_map = _stockmap_context()
    monkeypatch.setattr(
        StockMapSchema,
        "fetch_latest_for_symbol_asof",
        staticmethod(lambda symbol, asof_time: creation_map),
    )

    for setup in ("BREAKOUT_INITIATION", "FAILED_BREAKOUT", "CONTINUATION"):
        decision = _advisor().evaluate_deferred_entry(
            signal=_deferred_signal(
                side="SELL",
                created_price=102.0,
                setup=setup,
            ),
            snapshot=_deferred_snapshot(close=101.4),
        )
        assert decision.action is AdvisorAction.ALLOW
        assert decision.reason_codes == ("STOCKMAP_EXTREME_FAILURE_CONFIRMED",)
        assert decision.diagnostics["signal_setup"] == setup


def test_stockmap_gate_defers_signal_not_at_directional_extreme(monkeypatch) -> None:
    from schemas.stockmap import StockMapSchema

    creation_map = _stockmap_context()
    monkeypatch.setattr(
        StockMapSchema,
        "fetch_latest_for_symbol_asof",
        staticmethod(lambda symbol, asof_time: creation_map),
    )
    decision = _advisor().evaluate_deferred_entry(
        signal=_deferred_signal(
            side="SELL",
            created_price=100.5,
            setup="BREAKOUT_INITIATION",
        ),
        snapshot=_deferred_snapshot(close=100.0),
    )

    assert decision.action is AdvisorAction.WATCH
    assert decision.reason_codes == ("STOCKMAP_EXTREME_LOCATION_NOT_MET",)
