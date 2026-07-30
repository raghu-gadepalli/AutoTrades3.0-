from __future__ import annotations

from models.trade_models import StockOpportunity
from schemas.stock_opportunity import (
    STOCK_OPPORTUNITY_CONTRACT_VERSION,
    _append_unique,
)


def test_stock_opportunity_model_has_required_unique_keys_and_indexes() -> None:
    table = StockOpportunity.__table__
    unique_names = {constraint.name for constraint in table.constraints if constraint.name}
    index_names = {index.name for index in table.indexes}

    assert "uq_stock_opportunity_key" in unique_names
    assert "uq_stock_opportunity_signal_id" in unique_names
    assert {
        "idx_stock_opp_symbol_day",
        "idx_stock_opp_equity_day",
        "idx_stock_opp_day_state",
        "idx_stock_opp_family_side_day",
        "idx_stock_opp_episode",
        "idx_stock_opp_latest_episode",
        "idx_stock_opp_last_eval",
    }.issubset(index_names)


def test_transition_append_is_idempotent() -> None:
    item = {"transition_key": "DEPLOYED:event:signal", "state": "DEPLOYED"}
    once = _append_unique([], item, identity_key="transition_key")
    twice = _append_unique(once, item, identity_key="transition_key")

    assert once == twice
    assert len(twice) == 1


def test_stock_opportunity_contract_version_is_frozen() -> None:
    assert STOCK_OPPORTUNITY_CONTRACT_VERSION == "STOCK_OPPORTUNITY_V1"


def test_intraday_cutoff_completes_only_active_opportunity(monkeypatch) -> None:
    from datetime import date, datetime
    from types import SimpleNamespace

    from enums.enums import SignalSide
    from schemas.stock_opportunity import StockOpportunitySchema

    existing = SimpleNamespace(
        lifecycle_state="DEPLOYED",
        trading_day=date(2026, 7, 29),
        latest_episode_id="EPISODE:1",
        current_setup_family="ACCEPTED_BREAKOUT",
        transition_history=[],
        completed_at=None,
    )
    updates = []
    monkeypatch.setattr(
        StockOpportunitySchema,
        "fetch_by_signal_id",
        staticmethod(lambda signal_id: existing),
    )
    monkeypatch.setattr(
        StockOpportunitySchema,
        "update_opportunity",
        staticmethod(lambda *, signal_id, update_data: updates.append(update_data) or update_data),
    )

    result = StockOpportunitySchema.complete_at_intraday_cutoff(
        snapshot=SimpleNamespace(snapshot_time=datetime(2026, 7, 29, 15, 18)),
        signal=SimpleNamespace(signal_id="SIG1", side=SignalSide.BUY),
    )

    assert result["lifecycle_state"] == "COMPLETED"
    assert result["lifecycle_reason"] == "INTRADAY_SIGNAL_CUTOFF"
    assert result["completed_at"] == datetime(2026, 7, 29, 15, 18)
    assert result["transition_history"][0]["source_event_type"] == "INTRADAY_SIGNAL_CUTOFF"
    assert len(updates) == 1


def test_intraday_cutoff_preserves_prior_opportunity_completion_reason(monkeypatch) -> None:
    from datetime import date, datetime
    from types import SimpleNamespace

    from enums.enums import SignalSide
    from schemas.stock_opportunity import StockOpportunitySchema

    existing = SimpleNamespace(
        lifecycle_state="COMPLETED",
        lifecycle_reason="BALANCE_COMPLETED_OPPORTUNITY_WINDOW_COMPLETED",
        trading_day=date(2026, 7, 29),
        latest_episode_id="EPISODE:1",
        current_setup_family="ACCEPTED_BREAKOUT",
        transition_history=[],
        completed_at=datetime(2026, 7, 29, 12, 0),
    )
    monkeypatch.setattr(
        StockOpportunitySchema,
        "fetch_by_signal_id",
        staticmethod(lambda signal_id: existing),
    )
    monkeypatch.setattr(
        StockOpportunitySchema,
        "update_opportunity",
        staticmethod(lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not update"))),
    )

    result = StockOpportunitySchema.complete_at_intraday_cutoff(
        snapshot=SimpleNamespace(snapshot_time=datetime(2026, 7, 29, 15, 18)),
        signal=SimpleNamespace(signal_id="SIG1", side=SignalSide.BUY),
    )

    assert result.lifecycle_reason == "BALANCE_COMPLETED_OPPORTUNITY_WINDOW_COMPLETED"
