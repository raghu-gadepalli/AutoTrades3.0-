"""Focused tests for the simplified signal-time StockAdvisor."""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG
from configs.stock_advisor_config import StockAdvisorPolicyConfig
from pydantic import ValidationError
from schemas.snapshot import AuctionSnapshotBlock
from services.auction_engine.contracts import (
    AdvisorAction,
    AuctionStateName,
    BarEvidence,
    BoundarySide,
    DirectionalBias,
    EvidenceSnapshot,
    ExtensionEvidence,
    PriceActionEvidence,
)
from services.auction_engine.evidence import EvidenceBuilder
from services.auction_engine.state_engine import AuctionStateEngine, _StateMemory
from services.signals.signal_generator import _advisor_adjusted_auction_action
from services.signals.stock_advisor import StockAdvisor


class AuctionStockAdvisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ts = datetime(2026, 7, 24, 10, 54)
        self.version = AUCTION_ENGINE_CONFIG.engine.config_version

    def _policy(self, mode: str) -> StockAdvisorPolicyConfig:
        return StockAdvisorPolicyConfig(mode=mode)

    def _snapshot(
        self,
        *,
        family: str = "REVERSAL",
        subtype: str = "NORMAL_REVERSAL",
        side: str = "SELL",
        entry: float = 105.0,
        state: str = "REVERSAL",
        accepted_low: float | None = 100.0,
        accepted_high: float | None = 110.0,
        accepted_inside: bool = True,
        accepted_breakout_eligible: bool = True,
        accepted_provisional: bool = False,
        session_high: float = 112.0,
        session_low: float = 98.0,
        rise_from_low_atr: float = 1.0,
        decline_from_high_atr: float = 1.0,
        distance_to_high_atr: float = 0.7,
        distance_to_low_atr: float = 0.7,
        low_path_bars: int = 0,
        low_path_efficiency: float | None = None,
        low_path_ratio: float | None = None,
        high_path_bars: int = 0,
        high_path_efficiency: float | None = None,
        high_path_ratio: float | None = None,
        exhaustion_active: bool = False,
        exhausted_side: str = "UNKNOWN",
        exhaustion_expires_at=None,
    ) -> SimpleNamespace:
        candidate = SimpleNamespace(
            candidate_id=f"CANDIDATE:{side}",
            family=family,
            subtype=subtype,
            side=side,
            auction_state=state,
            entry_price=entry,
            source_frozen_range_low=accepted_low,
            source_frozen_range_high=accepted_high,
        )
        decision = SimpleNamespace(
            action="LOCAL_CONFIRMED",
            manager_action="SELECT",
            selected_candidate_id=candidate.candidate_id,
            manager_reason_codes=[],
            manager_diagnostics={},
        )
        context = SimpleNamespace(
            current_auction_state=state,
            directional_bias="UNKNOWN",
            reason_codes=[],
            accepted_range_id=("RANGE:1" if accepted_low is not None else None),
            accepted_range_source="INTRADAY_BALANCE",
            accepted_range_low=accepted_low,
            accepted_range_high=accepted_high,
            accepted_range_established_at=self.ts - timedelta(minutes=30),
            accepted_range_provisional=accepted_provisional,
            accepted_range_breakout_eligible=accepted_breakout_eligible,
            accepted_range_inside=accepted_inside,
            accepted_range_position=0.5 if accepted_inside else None,
            accepted_range_outside_atr=0.0 if accepted_inside else 0.3,
            session_open_price=105.0,
            session_high_price=session_high,
            session_high_time=self.ts - timedelta(minutes=18),
            session_low_price=session_low,
            session_low_time=self.ts - timedelta(minutes=45),
            session_position=0.5,
            distance_to_session_high_atr=distance_to_high_atr,
            distance_to_session_low_atr=distance_to_low_atr,
            rise_from_session_low_atr=rise_from_low_atr,
            rise_from_session_low_pct=0.01,
            decline_from_session_high_atr=decline_from_high_atr,
            decline_from_session_high_pct=0.01,
            path_from_session_low_bars=low_path_bars,
            path_from_session_low_efficiency=low_path_efficiency,
            path_from_session_low_directional_ratio=low_path_ratio,
            path_from_session_high_bars=high_path_bars,
            path_from_session_high_efficiency=high_path_efficiency,
            path_from_session_high_directional_ratio=high_path_ratio,
            exhaustion_active=exhaustion_active,
            exhausted_side=exhausted_side,
            exhaustion_expires_at=exhaustion_expires_at,
        )
        return SimpleNamespace(
            symbol="TEST",
            snapshot_time=self.ts,
            indicators=SimpleNamespace(atr=SimpleNamespace(value=10.0)),
            auction=SimpleNamespace(
                decision=decision,
                stock_context=context,
                candidates=[candidate],
            ),
        )

    def test_exhausted_direction_is_blocked_but_shadow_preserves_flow(self) -> None:
        snapshot = self._snapshot(
            family="ACCEPTED_BREAKOUT",
            subtype="CONTINUATION_ACCEPTANCE",
            side="SELL",
            entry=97.0,
            accepted_inside=False,
            exhaustion_active=True,
            exhausted_side="DOWN",
            exhaustion_expires_at=self.ts + timedelta(minutes=3),
        )
        result = StockAdvisor(self._policy("SHADOW")).evaluate(snapshot)
        self.assertEqual(AdvisorAction.BLOCK, result.action)
        self.assertEqual(AdvisorAction.ALLOW, result.effective_action)
        self.assertIn("ADVISOR_BLOCK_EXHAUSTED_DIRECTION", result.reason_codes)

    def test_normal_reversal_inside_accepted_range_is_deferred(self) -> None:
        result = StockAdvisor(self._policy("ENFORCE")).evaluate(self._snapshot())
        self.assertEqual(AdvisorAction.WATCH, result.action)
        self.assertIn("ADVISOR_WATCH_INSIDE_ACCEPTED_RANGE", result.reason_codes)

    def test_failed_breakout_inside_range_uses_own_setup_logic(self) -> None:
        snapshot = self._snapshot(
            family="FAILED_BREAKOUT",
            subtype="NEUTRAL_RANGE_FAILED_AUCTION",
        )
        result = StockAdvisor(self._policy("ENFORCE")).evaluate(snapshot)
        self.assertEqual(AdvisorAction.ALLOW, result.action)
        self.assertTrue(result.diagnostics["inside_range_exempt"])

    def test_exhaustion_reversal_inside_range_is_not_reinterpreted(self) -> None:
        snapshot = self._snapshot(
            family="REVERSAL",
            subtype="EXHAUSTION_REVERSAL",
        )
        result = StockAdvisor(self._policy("ENFORCE")).evaluate(snapshot)
        self.assertEqual(AdvisorAction.ALLOW, result.action)
        self.assertTrue(result.diagnostics["inside_range_exempt"])

    def test_buy_near_session_high_after_material_rise_is_deferred(self) -> None:
        snapshot = self._snapshot(
            side="BUY",
            entry=111.5,
            accepted_inside=False,
            rise_from_low_atr=1.35,
            distance_to_high_atr=0.05,
        )
        result = StockAdvisor(self._policy("ENFORCE")).evaluate(snapshot)
        self.assertEqual(AdvisorAction.WATCH, result.action)
        self.assertIn("ADVISOR_WATCH_BUY_NEAR_SESSION_HIGH", result.reason_codes)

    def test_sell_near_session_low_after_material_decline_is_deferred(self) -> None:
        snapshot = self._snapshot(
            side="SELL",
            entry=98.5,
            accepted_inside=False,
            decline_from_high_atr=1.35,
            distance_to_low_atr=0.05,
        )
        result = StockAdvisor(self._policy("ENFORCE")).evaluate(snapshot)
        self.assertEqual(AdvisorAction.WATCH, result.action)
        self.assertIn("ADVISOR_WATCH_SELL_NEAR_SESSION_LOW", result.reason_codes)

    def test_sell_against_persistent_climb_from_session_low_is_deferred(self) -> None:
        snapshot = self._snapshot(
            side="SELL",
            entry=106.0,
            accepted_inside=False,
            rise_from_low_atr=0.9,
            low_path_bars=5,
            low_path_efficiency=0.70,
            low_path_ratio=0.80,
        )
        result = StockAdvisor(self._policy("ENFORCE")).evaluate(snapshot)
        self.assertEqual(AdvisorAction.WATCH, result.action)
        self.assertIn(
            "ADVISOR_WATCH_SELL_AGAINST_CLIMB_FROM_SESSION_LOW",
            result.reason_codes,
        )

    def test_buy_against_persistent_decline_from_session_high_is_deferred(self) -> None:
        snapshot = self._snapshot(
            side="BUY",
            entry=104.0,
            accepted_inside=False,
            decline_from_high_atr=0.9,
            high_path_bars=5,
            high_path_efficiency=0.70,
            high_path_ratio=0.80,
        )
        result = StockAdvisor(self._policy("ENFORCE")).evaluate(snapshot)
        self.assertEqual(AdvisorAction.WATCH, result.action)
        self.assertIn(
            "ADVISOR_WATCH_BUY_AGAINST_DECLINE_FROM_SESSION_HIGH",
            result.reason_codes,
        )

    def test_strong_accepted_breakout_can_clear_extreme_and_path_deferral(self) -> None:
        snapshot = self._snapshot(
            family="ACCEPTED_BREAKOUT",
            subtype="CONTINUATION_ACCEPTANCE",
            side="BUY",
            entry=111.8,
            state="FRESH_EXPANSION",
            accepted_low=100.0,
            accepted_high=110.0,
            accepted_inside=False,
            session_high=112.0,
            session_low=98.0,
            rise_from_low_atr=1.38,
            distance_to_high_atr=0.02,
            high_path_bars=5,
            decline_from_high_atr=0.8,
            high_path_efficiency=0.70,
            high_path_ratio=0.80,
        )
        result = StockAdvisor(self._policy("ENFORCE")).evaluate(snapshot)
        self.assertEqual(AdvisorAction.ALLOW, result.action)
        self.assertIn(
            "ADVISOR_ALLOW_STRONG_ACCEPTED_RANGE_ESCAPE",
            result.reason_codes,
        )

    def test_advisor_applies_only_to_new_signal_deployment(self) -> None:
        snapshot = self._snapshot()
        advisor = StockAdvisor(self._policy("ENFORCE")).evaluate(snapshot)

        action, reasons = _advisor_adjusted_auction_action(
            snapshot=snapshot,
            existing_signal=None,
            advisor=advisor,
        )
        self.assertEqual("LOCAL_WATCH", action)
        self.assertIn("SIGNAL_DEPLOYMENT_WATCHED_BY_STOCK_ADVISOR", reasons)

        existing = SimpleNamespace(signal_id="SIG-1")
        action, reasons = _advisor_adjusted_auction_action(
            snapshot=snapshot,
            existing_signal=existing,
            advisor=advisor,
        )
        self.assertEqual("LOCAL_CONFIRMED", action)
        self.assertEqual((), reasons)

    def test_snapshot_contract_rejects_removed_advisor_field(self) -> None:
        payload = {
            "status": "NOT_RUN",
            "continuity_mode": "COLD_START",
            "previous_snapshot_time": None,
            "state": None,
            "stock_context": None,
            "advisor": {
                "mode": "SHADOW",
                "action": "BLOCK",
                "effective_action": "ALLOW",
                "selected_candidate_id": "CANDIDATE:SELL",
                "reason_codes": ["TEST"],
                "diagnostics": {},
            },
            "boundary": None,
            "candidates": [],
            "opportunities": [],
            "decision": None,
            "changes": [],
            "error": None,
        }
        with self.assertRaises(ValidationError):
            AuctionSnapshotBlock.model_validate(payload)

    def test_missing_selected_candidate_is_logged_and_fails_open(self) -> None:
        snapshot = self._snapshot(side="SELL")
        snapshot.auction.candidates = []
        with self.assertLogs(
            "services.signals.stock_advisor",
            level="ERROR",
        ) as captured:
            result = StockAdvisor(self._policy("ENFORCE")).evaluate(snapshot)

        self.assertEqual(AdvisorAction.ALLOW, result.action)
        self.assertEqual(AdvisorAction.ALLOW, result.effective_action)
        self.assertEqual("CANDIDATE:SELL", result.selected_candidate_id)
        self.assertIn("ADVISOR_INPUT_ERROR_FAIL_OPEN", result.reason_codes)
        self.assertTrue(result.diagnostics["fail_open"])
        self.assertIn("candidate_id=CANDIDATE:SELL matches=0", " ".join(captured.output))

    def test_session_path_memory_uses_all_completed_candles(self) -> None:
        engine = AuctionStateEngine(AUCTION_ENGINE_CONFIG)
        memory = _StateMemory(trading_day=self.ts.date())
        bars = [
            (100.0, 101.0, 99.0, 100.0),
            (100.0, 102.0, 99.5, 101.0),
            (101.0, 103.0, 100.5, 102.0),
            (102.0, 104.0, 101.5, 103.0),
        ]
        for index, (opn, high, low, close) in enumerate(bars):
            ts = self.ts + timedelta(minutes=index * 3)
            evidence = EvidenceSnapshot(
                symbol="TEST",
                trading_day=ts.date(),
                snapshot_time=ts,
                close=close,
                atr=1.0,
                bar=BarEvidence(
                    snapshot_time=ts,
                    open=opn,
                    high=high,
                    low=low,
                    close=close,
                    direction=DirectionalBias.UP,
                ),
                price_action=PriceActionEvidence(
                    direction=DirectionalBias.UP,
                    rejection=False,
                    failed_extreme=False,
                ),
                extension=ExtensionEvidence(
                    extended=False,
                    mature=False,
                    move_from_anchor_atr=0.5,
                    progress_decay=0.0,
                    failed_extreme_count=0,
                ),
                config_version=self.version,
            )
            engine._update_session_path_memory(memory, evidence)

        self.assertEqual(99.0, memory.session_low_price)
        self.assertEqual(104.0, memory.session_high_price)
        self.assertGreaterEqual(memory.path_from_low_steps, 3)
        self.assertGreater(memory.path_from_low_up_steps, 0)

    def test_boundary_builder_rejects_orb_seed_and_candidate_fallback(self) -> None:
        builder = EvidenceBuilder(AUCTION_ENGINE_CONFIG)
        bar = BarEvidence(
            snapshot_time=self.ts,
            open=100.0,
            high=101.0,
            low=99.5,
            close=100.5,
            direction=DirectionalBias.UP,
        )
        base_range = {
            "range_id": "ORB:2026-07-24",
            "version": 1,
            "high": 101.0,
            "low": 99.0,
            "source": "ORB",
            "range_type": "OPENING_RANGE",
            "start_time": self.ts - timedelta(minutes=15),
            "end_time": self.ts,
            "provisional": False,
            "breakout_eligible": False,
        }
        data = {
            "structure": {
                "accepted": {"range": base_range},
                "candidate": {"range": {**base_range, "source": "INTRADAY_BALANCE"}},
                "raw": {"range": {**base_range, "source": "INTRADAY_BALANCE"}},
            }
        }
        self.assertIsNone(builder._build_boundary(data, bar, 1.0))

        evolved = dict(base_range)
        evolved.update({
            "range_id": "DYNAMIC:1",
            "source": "INTRADAY_BALANCE",
            "range_type": "BALANCE",
            "breakout_eligible": True,
        })
        data["structure"]["accepted"]["range"] = evolved
        observation = builder._build_boundary(data, bar, 1.0)
        self.assertIsNotNone(observation)
        self.assertEqual("INTRADAY_BALANCE", observation.boundary_source)
        self.assertIn("ACCEPTED_RANGE_ONLY", observation.reason_codes)

    def test_exhaustion_context_survives_confirmed_reversal_state(self) -> None:
        engine = AuctionStateEngine(AUCTION_ENGINE_CONFIG)
        memory = _StateMemory(
            trading_day=self.ts.date(),
            current_state=AuctionStateName.REVERSAL,
            established_trend_side=DirectionalBias.UP,
            exhaustion_active=True,
            exhaustion_side=DirectionalBias.DOWN,
            exhaustion_started_at=self.ts - timedelta(minutes=3),
            exhaustion_age_bars=1,
        )
        evidence = EvidenceSnapshot(
            symbol="TEST",
            trading_day=self.ts.date(),
            snapshot_time=self.ts,
            close=101.0,
            atr=1.0,
            bar=BarEvidence(
                snapshot_time=self.ts,
                open=100.5,
                high=101.2,
                low=100.4,
                close=101.0,
                direction=DirectionalBias.UP,
            ),
            price_action=PriceActionEvidence(
                direction=DirectionalBias.UP,
                rejection=False,
                failed_extreme=False,
            ),
            extension=ExtensionEvidence(
                extended=False,
                mature=False,
                move_from_anchor_atr=0.5,
                progress_decay=0.0,
                failed_extreme_count=0,
            ),
            config_version=self.version,
        )
        engine._update_exhaustion_context(
            memory,
            evidence,
            established=DirectionalBias.UP,
            current_leg_mature=False,
            leg_distance_atr=0.5,
            leg_progress_or_rejection=False,
            trend_resume=False,
            reversal_ready=True,
        )
        self.assertTrue(memory.exhaustion_active)
        self.assertEqual(DirectionalBias.DOWN, memory.exhaustion_side)


if __name__ == "__main__":
    unittest.main()
