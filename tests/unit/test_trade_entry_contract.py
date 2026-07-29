#!/usr/bin/env python3
"""Focused Patch 5.3 trade-entry and UI contract tests.

These tests intentionally use ``unittest`` and do not invoke any live services,
Auction replay scripts, trade execution, or database writes.
"""

from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.trade.generator.tradegen_validator import (
    DOWNSTREAM_CONTRACT_VERSION,
    MODE_AUTO,
    MODE_MANUAL_CONFIRM,
    MODE_MANUAL_PREVIEW,
    TRADE_DECISION_ALLOW,
    TRADE_DECISION_BLOCK,
    TRADE_DECISION_WAIT,
    TradeDecisionHelper,
)
from services.trade.generator.tradegen_helper import _signal_setup_levels
from schemas.user_trade import UserTradeSchema


ROOT = Path(__file__).resolve().parents[2]


def make_user(*, userid: str = "TEST", autotrade: int = 1):
    return SimpleNamespace(
        userid=userid,
        active=1,
        logged_in=1,
        autotrade=autotrade,
    )


def make_signal(
    *,
    stage: str = "ACTIVE",
    posture: str = "STRENGTHEN",
    trade_action: str = "HOLD_POSITION",
    should_exit: bool = False,
    status: str = "OPEN",
):
    setup_levels = {
        "entry_price": 100.0,
        "initial_stop_reference_price": 99.0,
        "reference_price": 99.0,
        "reference_source": "FROZEN_BOUNDARY",
        "opportunity_key": "OPPORTUNITY:test",
        "candidate_id": "CANDIDATE:test",
        "boundary_event_key": "BOUNDARY_EVENT:test",
        "setup_label": "ACCEPTED_BREAKOUT",
        "setup_subtype": "CONTINUATION_ACCEPTANCE",
        "side": "BUY",
    }
    setup_levels["contract_version"] = DOWNSTREAM_CONTRACT_VERSION
    setup_levels["source"] = "AUCTION"
    reason = f"TEST_{stage}_{posture}"
    alignment = "ALIGNED" if posture == "STRENGTHEN" else "NEUTRAL"
    meta = {
        "downstream_contract": {
            "version": DOWNSTREAM_CONTRACT_VERSION,
            "source": "AUCTION_SIGNAL_GENERATOR",
        },
        "signal": {
            "contract_version": DOWNSTREAM_CONTRACT_VERSION,
            "stage": stage,
            "status": status,
            "side": "BUY",
            "setup_label": "ACCEPTED_BREAKOUT",
            "primary_pattern": "CONTINUATION_ACCEPTANCE",
            "signal_action": "HOLD",
            "signal_state": "MANAGE",
            "signal_reason": reason,
        },
        "lifecycle": {
            "contract_version": DOWNSTREAM_CONTRACT_VERSION,
            "stage": stage,
            "status": status,
            "side": "BUY",
            "trade_action": trade_action,
            "signal_action": "HOLD",
            "signal_state": "MANAGE",
            "signal_reason": reason,
        },
        "management": {
            "contract_version": DOWNSTREAM_CONTRACT_VERSION,
            "action": posture,
            "reason_code": reason,
            "stage": stage,
            "signal_status": status,
            "side": "BUY",
            "setup_family": "ACCEPTED_BREAKOUT",
            "setup_subtype": "CONTINUATION_ACCEPTANCE",
            "opportunity_key": "OPPORTUNITY:test",
            "candidate_id": "CANDIDATE:test",
            "boundary_event_key": "BOUNDARY_EVENT:test",
            "should_exit_signal": should_exit,
            "auction_action": "LOCAL_CONFIRMED",
            "auction_state": "ORDERLY_UPTREND",
            "directional_alignment": alignment,
        },
        "setup_levels": setup_levels,
        "auction_signal": {
            "opportunity_key": "OPPORTUNITY:test",
            "candidate_id": "CANDIDATE:test",
            "boundary_event_key": "BOUNDARY_EVENT:test",
            "setup_family": "ACCEPTED_BREAKOUT",
            "setup_subtype": "CONTINUATION_ACCEPTANCE",
            "side": "BUY",
        },
    }
    return SimpleNamespace(
        signal_id="SIGNAL:test",
        symbol="TEST",
        equity_ref="TEST",
        setup="ACCEPTED_BREAKOUT",
        side="BUY",
        stage=stage,
        status=status,
        status_reason=f"TEST_{stage}_{posture}",
        created_price=100.0,
        last_price=100.0,
        ltp=100.0,
        first_seen_time=datetime.now(),
        meta_json=meta,
    )


class TradeEntryLifecycleTests(unittest.TestCase):
    def evaluate(self, *, signal, mode=MODE_AUTO, user=None):
        user = user or make_user()
        with (
            patch.object(UserTradeSchema, "has_any_trade_for_signal", return_value=False),
            patch.object(UserTradeSchema, "fetch_active_trades_for_signal", return_value=[]),
            patch.object(UserTradeSchema, "fetch_active_trades_for_user_equity_ref", return_value=[]),
        ):
            return TradeDecisionHelper.evaluate(user=user, signal=signal, mode=mode)

    def test_active_strengthen_allows_automatic_entry(self):
        decision = self.evaluate(signal=make_signal())
        self.assertTrue(decision.allowed)
        self.assertEqual(TRADE_DECISION_ALLOW, decision.decision)
        self.assertEqual("ACTIVE", decision.details["signal_stage"])
        self.assertEqual("STRENGTHEN", decision.details["management_posture"])

    def test_defensive_signal_waits_for_manual_confirmation(self):
        signal = make_signal(stage="PROTECT", posture="CAUTION", trade_action="TIGHTEN_STOP")
        auto = self.evaluate(signal=signal, mode=MODE_AUTO)
        preview = self.evaluate(signal=signal, mode=MODE_MANUAL_PREVIEW)
        confirmed = self.evaluate(signal=signal, mode=MODE_MANUAL_CONFIRM)

        self.assertEqual(TRADE_DECISION_WAIT, auto.decision)
        self.assertEqual(TRADE_DECISION_WAIT, preview.decision)
        self.assertEqual(TRADE_DECISION_ALLOW, confirmed.decision)
        self.assertIn("signal_defensive_posture_requires_manual_confirmation", confirmed.warnings)

    def test_exit_bias_is_hard_block_even_after_manual_confirmation(self):
        signal = make_signal(
            stage="EXIT_BIAS",
            posture="EXIT",
            trade_action="EXIT_POSITION",
            should_exit=True,
        )
        decision = self.evaluate(signal=signal, mode=MODE_MANUAL_CONFIRM)
        self.assertFalse(decision.allowed)
        self.assertEqual(TRADE_DECISION_BLOCK, decision.decision)
        self.assertEqual(["signal_exit_posture"], decision.reasons)

    def test_should_exit_is_hard_block_independent_of_stage(self):
        signal = make_signal(stage="ACTIVE", posture="STRENGTHEN", should_exit=True)
        decision = self.evaluate(signal=signal, mode=MODE_MANUAL_CONFIRM)
        self.assertEqual(TRADE_DECISION_BLOCK, decision.decision)
        self.assertEqual(["signal_exit_posture"], decision.reasons)

    def test_missing_downstream_contract_fails_visibly(self):
        signal = make_signal()
        del signal.meta_json["downstream_contract"]
        with self.assertRaisesRegex(ValueError, "downstream_contract"):
            TradeDecisionHelper.evaluate(user=make_user(), signal=signal, mode=MODE_AUTO)

    def test_duplicate_check_database_error_propagates(self):
        signal = make_signal()
        with patch.object(
            UserTradeSchema,
            "has_any_trade_for_signal",
            side_effect=RuntimeError("db unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "db unavailable"):
                TradeDecisionHelper.evaluate(user=make_user(), signal=signal, mode=MODE_AUTO)

    def test_setup_levels_have_one_canonical_path(self):
        signal = make_signal()
        levels = _signal_setup_levels(signal)
        self.assertEqual("OPPORTUNITY:test", levels["opportunity_key"])

        del signal.meta_json["setup_levels"]
        with self.assertRaisesRegex(ValueError, "meta_json.setup_levels"):
            _signal_setup_levels(signal)


class TradeEntryStaticContractTests(unittest.TestCase):
    def test_manual_modal_has_no_advanced_mode_and_uses_confirmation_name(self):
        html = (ROOT / "templates/partials/_trade_create_modal.html").read_text()
        js = (ROOT / "static/js/trade_create.js").read_text()
        self.assertNotIn("wtm-mode-advanced", html)
        self.assertNotIn("wtm-mode-advanced", js)
        self.assertIn("confirm_entry_warning", js)
        self.assertIn("Entry Eligibility", html)

    def test_watchlist_no_longer_loads_legacy_signal_modal(self):
        html = (ROOT / "templates/dash_watchlist.html").read_text()
        sw = (ROOT / "static/js/sw.js").read_text()
        self.assertNotIn("_signals_modal.html", html)
        self.assertNotIn("signalsmodal.js", html)
        self.assertNotIn("signalsmodal.js", sw)

    def test_current_snapshot_paths_replace_removed_structure_paths(self):
        for rel in ("static/js/signals.js", "static/js/watchlist.js", "static/js/snapshot.js"):
            src = (ROOT / rel).read_text()
            self.assertNotIn("structure.breakout_context", src, rel)
            self.assertNotIn("structure.anchors", src, rel)
            self.assertNotIn("structure.breakout.", src, rel)
        snapshot_src = (ROOT / "static/js/snapshot.js").read_text()
        self.assertNotIn("auction.decision", snapshot_src)
        self.assertNotIn("auction.state", snapshot_src)
        self.assertNotIn("auction.boundary", snapshot_src)
        self.assertIn("auction.lifecycle", snapshot_src)
        self.assertIn("market_windows", snapshot_src)

    def test_manual_derivative_price_never_falls_back_to_equity_close(self):
        src = (ROOT / "services/trade/generator/tradegen_helper.py").read_text()
        marker = "derivative leg skipped because price is unavailable"
        self.assertIn(marker, src)
        self.assertIn("DERIVATIVE_PRICE_UNAVAILABLE", src)
        # The only explicit risk_ref_eq assignment to entry_exec is inside the
        # EQ branch; derivative branches return/skip when their own price is absent.
        self.assertIn('if inst == "EQ":\n                    entry_exec = risk_ref_eq', src)

    def test_orders_and_positions_expose_origin_and_management(self):
        for rel in ("templates/dash_orders.html", "templates/dash_positions.html"):
            html = (ROOT / rel).read_text()
            self.assertIn("Origin", html, rel)
            self.assertIn("Management", html, rel)
        for rel in ("static/js/orders.js", "static/js/positions.js"):
            src = (ROOT / rel).read_text()
            self.assertIn("signal_reference", src, rel)
            self.assertIn("management_mode", src, rel)


if __name__ == "__main__":
    unittest.main(verbosity=2)
