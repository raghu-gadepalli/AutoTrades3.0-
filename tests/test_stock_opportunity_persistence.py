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
