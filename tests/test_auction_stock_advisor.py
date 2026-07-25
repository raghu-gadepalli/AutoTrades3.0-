"""Focused tests for decoupled Auction stock context and signal-time Advisor."""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG
from configs.stock_advisor_config import StockAdvisorPolicyConfig
from schemas.snapshot import AdvisorDecisionProjection, AuctionSnapshotBlock
from services.auction_engine.contracts import (
    AdvisorAction,
    AuctionStateName,
    BarEvidence,
    DirectionalBias,
    EvidenceSnapshot,
    ExtensionEvidence,
    PriceActionEvidence,
)
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
        side: str = "SELL",
        entry: float = 99.1,
        low: float = 99.0,
        high: float = 101.0,
        state: str = "ORDERLY_DOWNTREND",
        context_name: str = "ROTATIONAL",
        rotational: bool = True,
        fresh_expansion: bool = False,
        exhaustion_active: bool = False,
        exhausted_side: str = "UNKNOWN",
        switches: int = 0,
    ) -> SimpleNamespace:
        candidate = SimpleNamespace(
            candidate_id=f"CANDIDATE:{side}",
            family="ACCEPTED_BREAKOUT",
            subtype="CONTINUATION_ACCEPTANCE",
            side=side,
            auction_state=state,
            entry_price=entry,
            source_frozen_range_low=low,
            source_frozen_range_high=high,
        )
        decision = SimpleNamespace(
            action="LOCAL_CONFIRMED",
            manager_action="SELECT",
            selected_candidate_id=candidate.candidate_id,
            manager_reason_codes=[],
            manager_diagnostics={"recent_eligible_side_switches": switches},
        )
        context = SimpleNamespace(
            name=context_name,
            reason_codes=[],
            rotational=rotational,
            fresh_expansion_confirmed=fresh_expansion,
            directional_efficiency=0.70 if fresh_expansion else 0.20,
            exhaustion_active=exhaustion_active,
            exhausted_side=exhausted_side,
        )
        return SimpleNamespace(
            symbol="TEST",
            snapshot_time=self.ts,
            indicators=SimpleNamespace(atr=SimpleNamespace(value=1.0)),
            auction=SimpleNamespace(
                decision=decision,
                stock_context=context,
                candidates=[candidate],
            ),
        )

    def test_exhausted_direction_is_blocked_but_shadow_preserves_flow(self) -> None:
        snapshot = self._snapshot(
            side="SELL",
            context_name="MATURE_EXTENSION",
            rotational=False,
            exhaustion_active=True,
            exhausted_side="DOWN",
        )
        result = StockAdvisor(self._policy("SHADOW")).evaluate(snapshot)
        self.assertEqual(AdvisorAction.BLOCK, result.action)
        self.assertEqual(AdvisorAction.ALLOW, result.effective_action)
        self.assertIn("ADVISOR_BLOCK_EXHAUSTED_DIRECTION", result.reason_codes)

    def test_exhausted_direction_is_enforced(self) -> None:
        snapshot = self._snapshot(
            side="SELL",
            context_name="MATURE_EXTENSION",
            rotational=False,
            exhaustion_active=True,
            exhausted_side="DOWN",
        )
        result = StockAdvisor(self._policy("ENFORCE")).evaluate(snapshot)
        self.assertEqual(AdvisorAction.BLOCK, result.action)
        self.assertEqual(AdvisorAction.BLOCK, result.effective_action)

    def test_rotational_sell_near_range_low_is_blocked(self) -> None:
        snapshot = self._snapshot(side="SELL", entry=99.1, switches=1)
        result = StockAdvisor(self._policy("ENFORCE")).evaluate(snapshot)
        self.assertEqual(AdvisorAction.BLOCK, result.action)
        self.assertIn("ADVISOR_BLOCK_ROTATIONAL_RANGE_EDGE", result.reason_codes)
        self.assertIn("ADVISOR_BLOCK_SELL_NEAR_RANGE_LOW", result.reason_codes)

    def test_confirmed_price_led_expansion_is_allowed(self) -> None:
        snapshot = self._snapshot(
            side="SELL",
            entry=98.6,
            state="FRESH_EXPANSION",
            context_name="EARLY_EXPANSION",
            rotational=False,
            fresh_expansion=True,
        )
        result = StockAdvisor(self._policy("ENFORCE")).evaluate(snapshot)
        self.assertEqual(AdvisorAction.ALLOW, result.action)
        self.assertIn("ADVISOR_ALLOW_CONFIRMED_PRICE_LED_EXPANSION", result.reason_codes)
        self.assertFalse(result.diagnostics["time_of_day_gate_applied"])

    def test_advisor_applies_only_to_new_signal_deployment(self) -> None:
        snapshot = self._snapshot(side="SELL")
        advisor = StockAdvisor(self._policy("ENFORCE")).evaluate(snapshot)

        action, reasons = _advisor_adjusted_auction_action(
            snapshot=snapshot,
            existing_signal=None,
            advisor=advisor,
        )
        self.assertEqual("LOCAL_BLOCKED", action)
        self.assertIn("SIGNAL_DEPLOYMENT_BLOCKED_BY_STOCK_ADVISOR", reasons)

        existing = SimpleNamespace(signal_id="SIG-1")
        action, reasons = _advisor_adjusted_auction_action(
            snapshot=snapshot,
            existing_signal=existing,
            advisor=advisor,
        )
        self.assertEqual("LOCAL_CONFIRMED", action)
        self.assertEqual((), reasons)

    def test_legacy_advisor_projection_is_not_serialized_in_snapshot(self) -> None:
        block = AuctionSnapshotBlock(
            status="NOT_RUN",
            continuity_mode="COLD_START",
            previous_snapshot_time=None,
            state=None,
            stock_context=None,
            advisor=AdvisorDecisionProjection(
                mode="SHADOW",
                action="BLOCK",
                effective_action="ALLOW",
                selected_candidate_id="CANDIDATE:SELL",
                reason_codes=["TEST"],
                diagnostics={},
            ),
            boundary=None,
            candidates=[],
            opportunities=[],
            decision=None,
            changes=[],
            error=None,
        )
        self.assertNotIn("advisor", block.model_dump(mode="python"))

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
