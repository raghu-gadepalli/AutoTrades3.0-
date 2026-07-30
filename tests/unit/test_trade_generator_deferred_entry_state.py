from datetime import datetime

from services.trade.generator.trade_generator import _merge_trade_entry_state


def test_trade_entry_state_preserves_first_defer_and_counts_rechecks() -> None:
    meta = {"downstream_contract": {"version": "AUCTION_SIGNAL_DOWNSTREAM_V2"}}
    first = datetime(2026, 7, 29, 12, 27)
    second = datetime(2026, 7, 29, 12, 30)

    state1 = _merge_trade_entry_state(
        meta_json=meta,
        userid="USER1",
        state="DEFERRED",
        reason_code="SIGNAL_ENTRY_WAIT_NOT_STRICTLY_PROFITABLE",
        evaluated_at=first,
        details={"current_price": 1314.5},
    )
    state2 = _merge_trade_entry_state(
        meta_json=state1,
        userid="USER1",
        state="DEFERRED",
        reason_code="SIGNAL_ENTRY_WAIT_NOT_STRICTLY_PROFITABLE",
        evaluated_at=second,
        details={"current_price": 1314.9},
    )

    user_state = state2["trade_entry_state"]["users"]["USER1"]
    assert user_state["state"] == "DEFERRED"
    assert user_state["defer_class"] == "PRICE"
    assert user_state["first_deferred_at"] == first
    assert user_state["last_evaluated_at"] == second
    assert user_state["evaluation_count"] == 2


def test_trade_entry_state_can_progress_to_deployed() -> None:
    first = datetime(2026, 7, 29, 11, 30)
    deployed = datetime(2026, 7, 29, 11, 42)
    state = _merge_trade_entry_state(
        meta_json={},
        userid="USER1",
        state="DEFERRED",
        reason_code="SIGNAL_ENTRY_WAIT_NOT_STRICTLY_PROFITABLE",
        evaluated_at=first,
    )
    state = _merge_trade_entry_state(
        meta_json=state,
        userid="USER1",
        state="DEPLOYED",
        reason_code="TRADE_PACKAGE_CREATED",
        evaluated_at=deployed,
    )

    user_state = state["trade_entry_state"]["users"]["USER1"]
    assert user_state["state"] == "DEPLOYED"
    assert user_state["first_deferred_at"] == first
    assert user_state["last_evaluated_at"] == deployed
    assert user_state["evaluation_count"] == 2
