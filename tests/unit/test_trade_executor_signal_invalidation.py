from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from enums.enums import EntryStatus, ExitStatus, OrderStatus
from services.trade.executor import trade_executor as executor_module
from services.trade.executor.trade_executor import TradeExecutor


def _trade(*, trade_id=1, mode="VIRTUAL", entry_status="READY", source="TRADE_GENERATOR"):
    return SimpleNamespace(
        id=trade_id,
        userid="U1",
        signal_id="SIG1",
        source=source,
        symbol="ABC",
        equity_ref="ABC",
        instrument_type="EQ",
        trade_type="BUY",
        execution_mode=mode,
        entry_status=entry_status,
        exit_status="NONE",
        intraday_only=True,
        entry_order_id="OID1",
        quantity=10,
        executed_entry_qty=None,
        exec_status=None,
    )


def _signal(*, status="OPEN", side="BUY", created_price="100"):
    return SimpleNamespace(
        signal_id="SIG1",
        status=status,
        side=side,
        created_price=Decimal(created_price),
        equity_ref="ABC",
    )


def _persist_in_memory(ut, updates):
    for key, value in updates.items():
        setattr(ut, key, value)
    return ut


def test_executor_signal_guard_rejects_terminal_signal(monkeypatch):
    monkeypatch.setattr(
        executor_module.SignalSchema,
        "fetch_by_signal_id_strict",
        staticmethod(lambda signal_id: _signal(status="INVALIDATED")),
    )

    state, _, reason = TradeExecutor()._entry_signal_guard(_trade())

    assert state == executor_module.ENTRY_SIGNAL_GUARD_INVALID
    assert "INVALIDATED" in reason


def test_executor_strict_profitability_rejects_equality(monkeypatch):
    now = datetime(2026, 7, 29, 12, 0, 0)
    monkeypatch.setattr(
        executor_module,
        "_latest_underlying_snapshot_price",
        lambda symbol, asof_time: (Decimal("100"), now, "LTP"),
    )

    reason = TradeExecutor()._entry_profitability_defer_reason(
        _trade(),
        signal=_signal(created_price="100"),
        asof_time=now,
    )

    assert reason.startswith("ENTRY_DEFER_SIGNAL_NOT_STRICTLY_PROFITABLE")


def test_invalidated_virtual_package_expires_ready_and_exits_filled(monkeypatch):
    now = datetime(2026, 7, 29, 12, 0, 0)
    ready = _trade(trade_id=1, mode="VIRTUAL", entry_status="READY")
    filled = _trade(trade_id=2, mode="VIRTUAL", entry_status="FILLED")
    filled.executed_entry_qty = 10

    monkeypatch.setattr(
        executor_module.UserTradeSchema,
        "fetch_active_trades_for_signal",
        staticmethod(lambda **kwargs: [ready, filled]),
    )
    monkeypatch.setattr(executor_module, "_persist_executor_update", _persist_in_memory)

    TradeExecutor()._cancel_invalidated_entry_package(
        ready,
        reason="ENTRY_CANCEL_SIGNAL_STATUS_INVALIDATED",
        asof_time=now,
        broker=None,
    )

    assert ready.entry_status == EntryStatus.EXPIRED.value
    assert filled.exit_status == ExitStatus.READY.value


def test_invalidated_real_submitted_order_requests_cancellation(monkeypatch):
    now = datetime(2026, 7, 29, 12, 0, 0)
    submitted = _trade(trade_id=3, mode="REAL", entry_status="SUBMITTED")

    monkeypatch.setattr(
        executor_module.UserTradeSchema,
        "fetch_active_trades_for_signal",
        staticmethod(lambda **kwargs: [submitted]),
    )
    monkeypatch.setattr(executor_module, "_persist_executor_update", _persist_in_memory)
    monkeypatch.setattr(
        executor_module.OrderProfileSchema,
        "fetch_order_profile",
        staticmethod(lambda *args, **kwargs: SimpleNamespace(order_variety="regular")),
    )

    cancel_calls = []

    class PendingBroker:
        def latest_status(self, order_id):
            return OrderStatus.OPEN

        def history(self, order_id):
            return []

        class svc:
            @staticmethod
            def cancel_order(**kwargs):
                cancel_calls.append(kwargs)
                return {"ok": True}

    TradeExecutor()._cancel_invalidated_entry_package(
        submitted,
        reason="ENTRY_CANCEL_SIGNAL_STATUS_INVALIDATED",
        asof_time=now,
        broker=PendingBroker(),
    )

    assert submitted.entry_status == EntryStatus.SUBMITTED.value
    assert submitted.exec_status == "ENTRY_CANCEL_REQUESTED_SIGNAL_INVALIDATED"
    assert len(cancel_calls) == 1


def test_fill_cancel_race_queues_exit_for_actual_filled_quantity(monkeypatch):
    now = datetime(2026, 7, 29, 12, 0, 0)
    submitted = _trade(trade_id=4, mode="REAL", entry_status="SUBMITTED")

    monkeypatch.setattr(
        executor_module.UserTradeSchema,
        "fetch_active_trades_for_signal",
        staticmethod(lambda **kwargs: [submitted]),
    )
    monkeypatch.setattr(executor_module, "_persist_executor_update", _persist_in_memory)
    monkeypatch.setattr(
        executor_module.OrderProfileSchema,
        "fetch_order_profile",
        staticmethod(lambda *args, **kwargs: SimpleNamespace(order_variety="regular")),
    )
    monkeypatch.setattr(
        executor_module.TradeMonHelper,
        "rebase_trade_management_after_fill",
        staticmethod(lambda **kwargs: {"rebased": True}),
    )

    class FilledBroker:
        def latest_status(self, order_id):
            return OrderStatus.COMPLETE

        def history(self, order_id):
            return [
                {
                    "average_price": 101.25,
                    "filled_quantity": 4,
                    "order_timestamp": "2026-07-29T12:00:01+05:30",
                }
            ]

    TradeExecutor()._cancel_invalidated_entry_package(
        submitted,
        reason="ENTRY_CANCEL_SIGNAL_STATUS_INVALIDATED",
        asof_time=now,
        broker=FilledBroker(),
    )

    assert submitted.entry_status == EntryStatus.FILLED.value
    assert submitted.executed_entry_qty == 4
    assert submitted.exit_status == ExitStatus.READY.value
