"""Focused tests for decoupled Auction stock context and signal-time Advisor."""
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
        family: str = "ACCEPTED_BREAKOUT",
        subtype: str = "CONTINUATION_ACCEPTANCE",
        side: str = "SELL",
        entry: float = 99.1,
        low: float = 99.0,
        high: float = 101.0,
        state: str = "ORDERLY_DOWNTREND",
        context_name: str = "ROTATIONAL",
        background_regime: str = "ROTATIONAL",
        rotational: bool = True,
        fresh_expansion: bool = False,
        exhaustion_active: bool = False,
        exhausted_side: str = "UNKNOWN",
        exhaustion_expires_at=None,
        switches: int = 0,
        background_low: float | None = None,
        background_high: float | None = None,
    ) -> SimpleNamespace:
        background_low = low if background_low is None else background_low
        background_high = high if background_high is None else background_high
        candidate = SimpleNamespace(
            candidate_id=f"CANDIDATE:{side}",
            family=family,
            subtype=subtype,
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
            background_regime=background_regime,
            current_auction_state=state,
            reason_codes=[],
            rotational=rotational,
            fresh_expansion_confirmed=fresh_expansion,
            directional_efficiency=0.70 if fresh_expansion else 0.20,
            exhaustion_active=exhaustion_active,
            exhausted_side=exhausted_side,
            exhaustion_expires_at=exhaustion_expires_at,
            background_range_id="RANGE:1",
            background_range_low=background_low,
            background_range_high=background_high,
            background_range_classification="BALANCE_QUALIFIED",
            background_structure_flip_count=4,
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
            exhaustion_expires_at=self.ts + timedelta(minutes=3),
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
            exhaustion_expires_at=self.ts + timedelta(minutes=3),
        )
        result = StockAdvisor(self._policy("ENFORCE")).evaluate(snapshot)
        self.assertEqual(AdvisorAction.BLOCK, result.action)
        self.assertEqual(AdvisorAction.BLOCK, result.effective_action)

    def test_rotational_sell_near_range_low_is_blocked(self) -> None:
        snapshot = self._snapshot(side="SELL", entry=99.1, switches=1)
        result = StockAdvisor(self._policy("ENFORCE")).evaluate(snapshot)
        self.assertEqual(AdvisorAction.BLOCK, result.action)
        self.assertIn(
            "ADVISOR_BLOCK_ROTATIONAL_INSIDE_BACKGROUND_RANGE",
            result.reason_codes,
        )
        self.assertIn("ADVISOR_BLOCK_SELL_NEAR_RANGE_LOW", result.reason_codes)

    def test_confirmed_price_led_expansion_is_allowed(self) -> None:
        snapshot = self._snapshot(
            side="SELL",
            entry=98.6,
            state="FRESH_EXPANSION",
            context_name="EARLY_EXPANSION",
            background_regime="EARLY_EXPANSION",
            rotational=False,
            fresh_expansion=True,
        )
        result = StockAdvisor(self._policy("ENFORCE")).evaluate(snapshot)
        self.assertEqual(AdvisorAction.ALLOW, result.action)
        self.assertIn("ADVISOR_ALLOW_CONFIRMED_PRICE_LED_EXPANSION", result.reason_codes)
        self.assertFalse(result.diagnostics["time_of_day_gate_applied"])

    def test_reversal_is_not_blocked_by_same_side_exhaustion(self) -> None:
        snapshot = self._snapshot(
            family="REVERSAL",
            subtype="NORMAL_REVERSAL",
            side="BUY",
            entry=101.4,
            state="REVERSAL",
            context_name="REVERSAL",
            background_regime="DIRECTIONAL",
            rotational=False,
            exhaustion_active=True,
            exhausted_side="UP",
            exhaustion_expires_at=self.ts + timedelta(minutes=3),
        )
        result = StockAdvisor(self._policy("ENFORCE")).evaluate(snapshot)
        self.assertEqual(AdvisorAction.ALLOW, result.action)
        self.assertFalse(result.diagnostics["exhaustion_family_applicable"])

    def test_expired_exhaustion_does_not_block_continuation(self) -> None:
        snapshot = self._snapshot(
            side="SELL",
            context_name="MATURE_EXTENSION",
            background_regime="DIRECTIONAL",
            rotational=False,
            exhaustion_active=True,
            exhausted_side="DOWN",
            exhaustion_expires_at=self.ts - timedelta(minutes=3),
        )
        result = StockAdvisor(self._policy("ENFORCE")).evaluate(snapshot)
        self.assertEqual(AdvisorAction.ALLOW, result.action)
        self.assertFalse(result.diagnostics["exhaustion_context_current"])

    def test_current_reversal_inside_rotational_background_is_blocked(self) -> None:
        snapshot = self._snapshot(
            family="REVERSAL",
            subtype="NORMAL_REVERSAL",
            side="BUY",
            entry=100.8,
            state="REVERSAL",
            context_name="REVERSAL",
            background_regime="ROTATIONAL",
            rotational=False,
        )
        result = StockAdvisor(self._policy("ENFORCE")).evaluate(snapshot)
        self.assertEqual(AdvisorAction.BLOCK, result.action)
        self.assertIn(
            "ADVISOR_BLOCK_ROTATIONAL_INSIDE_BACKGROUND_RANGE",
            result.reason_codes,
        )

    def test_confirmed_reversal_outside_background_range_is_allowed(self) -> None:
        snapshot = self._snapshot(
            family="REVERSAL",
            subtype="NORMAL_REVERSAL",
            side="BUY",
            entry=101.4,
            state="REVERSAL",
            context_name="REVERSAL",
            background_regime="ROTATIONAL",
            rotational=True,
        )
        result = StockAdvisor(self._policy("ENFORCE")).evaluate(snapshot)
        self.assertEqual(AdvisorAction.ALLOW, result.action)
        self.assertIn(
            "ADVISOR_ALLOW_CONFIRMED_REVERSAL_OUTSIDE_BACKGROUND_RANGE",
            result.reason_codes,
        )

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
