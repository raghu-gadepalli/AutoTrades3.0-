#!/usr/bin/env python3
"""Offline contract tests for Auction Authority Refactor Stage 1.

These tests do not connect to the database or invoke signal/trade persistence.
"""

from __future__ import annotations

import json
import unittest

import services.auction_engine.contracts as auction_contracts_module
import services.auction_engine.episode_contracts as lifecycle_contracts_module
from datetime import date, datetime, timedelta

from pydantic import ValidationError

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG, AuctionEngineConfig
from enums.auction_engine import (
    AuctionEventType,
    BalanceEpisodeState,
    DirectionalBias,
    SetupFamily,
    StructuralPermissionResult,
)
from enums.auction_engine import (
    BoundarySide,
    EvidencePolarity,
    TradeSide,
)
from services.auction_engine.contracts import (
    BarEvidence,
    EvidenceFact,
    EvidenceSnapshot,
    stable_key,
)
from services.auction_engine.episode_contracts import AuctionEvent, BalanceEpisodeMemory
from services.auction_engine.setup_contracts import AuthoritativeSetupCandidate
from services.auction_engine.structural_permissions import StructuralPermissionMatrix



class AuctionEngineConfigTests(unittest.TestCase):
    def test_default_config_declares_authoritative_runtime_identity(self) -> None:
        self.assertEqual(
            AUCTION_ENGINE_CONFIG.engine.engine_name,
            "AUTOTRADES_AUCTION_ENGINE",
        )
        self.assertEqual(AUCTION_ENGINE_CONFIG.engine.engine_version, "1.1.0")
        self.assertFalse(hasattr(AUCTION_ENGINE_CONFIG.engine, "config_version"))
        self.assertFalse(hasattr(AUCTION_ENGINE_CONFIG, "stable_hash"))
        self.assertFalse(hasattr(AUCTION_ENGINE_CONFIG.engine, "enabled"))
        self.assertFalse(
            hasattr(AUCTION_ENGINE_CONFIG.engine, "replace_current_signal_path")
        )
        self.assertFalse(hasattr(AUCTION_ENGINE_CONFIG.episode, "projection_only"))
        self.assertFalse(
            hasattr(AUCTION_ENGINE_CONFIG.engine, "development_database_name")
        )
        self.assertFalse(
            hasattr(AUCTION_ENGINE_CONFIG.engine, "protected_database_names")
        )
        self.assertNotIn(
            "development_database_name",
            AUCTION_ENGINE_CONFIG.resolved_dict()["engine"],
        )
        self.assertNotIn(
            "protected_database_names",
            AUCTION_ENGINE_CONFIG.resolved_dict()["engine"],
        )

    def test_removed_contract_names_have_no_compatibility_aliases(self) -> None:
        self.assertFalse(hasattr(auction_contracts_module, "AuctionStateName"))
        self.assertFalse(hasattr(lifecycle_contracts_module, "EpisodeObservation"))
        self.assertFalse(hasattr(lifecycle_contracts_module, "EpisodeTransition"))
        self.assertFalse(hasattr(lifecycle_contracts_module, "EpisodeEvaluation"))

    def test_resolved_config_is_json_safe_without_version_or_hash(self) -> None:
        payload = AUCTION_ENGINE_CONFIG.resolved_dict()
        self.assertNotIn("config_version", payload["engine"])
        json.dumps(payload, sort_keys=True)

    def test_config_rejects_unknown_fields(self) -> None:
        with self.assertRaises(ValidationError):
            AuctionEngineConfig(unknown_section={})

    def test_config_is_frozen(self) -> None:
        with self.assertRaises(ValidationError):
            AUCTION_ENGINE_CONFIG.engine.engine_version = "2.0.0"

    def test_permission_matrix_is_fully_typed_and_versioned(self) -> None:
        event_types = {
            rule.event_type for rule in AUCTION_ENGINE_CONFIG.episode.permissions.event_rules
        }
        self.assertIn(
            AuctionEventType.DIRECTIONAL_REVERSAL_LEG_ESTABLISHED,
            event_types,
        )
        self.assertIn(AuctionEventType.BALANCE_ESCAPE_ACCEPTED, event_types)
        self.assertIn(AuctionEventType.BALANCE_ESCAPE_FAILED, event_types)
        self.assertIn(
            AuctionEventType.DIRECTIONAL_CONTINUATION_CONFIRMED,
            event_types,
        )
        self.assertIn(
            AuctionEventType.DIRECTIONAL_REACCELERATION_CONFIRMED,
            event_types,
        )
        permit_families = {
            family
            for rule in AUCTION_ENGINE_CONFIG.episode.permissions.event_rules
            if rule.result is StructuralPermissionResult.PERMIT
            for family in rule.setup_families
        }
        self.assertEqual(permit_families, set(SetupFamily))
        self.assertEqual(
            set(AUCTION_ENGINE_CONFIG.episode.permissions.result_precedence),
            set(StructuralPermissionResult),
        )
        state_rules = AUCTION_ENGINE_CONFIG.episode.permissions.state_rules
        self.assertTrue(
            any(rule.balance_state is BalanceEpisodeState.LOCKED for rule in state_rules)
        )

    def test_balance_rearm_config_rejects_invalid_attempt_limits(self) -> None:
        payload = AUCTION_ENGINE_CONFIG.resolved_dict()
        payload["episode"]["balance"]["max_escape_attempts_per_episode"] = 1
        payload["episode"]["balance"]["max_same_side_escape_attempts"] = 2
        with self.assertRaisesRegex(
            ValidationError,
            "Same-side escape attempt limit cannot exceed total episode limit",
        ):
            AuctionEngineConfig.model_validate(payload)

    def test_balance_rearm_config_rejects_too_few_rearm_bars(self) -> None:
        payload = AUCTION_ENGINE_CONFIG.resolved_dict()
        payload["episode"]["balance"]["failed_escape_rearm_inside_closes"] = 3
        payload["episode"]["balance"]["failed_escape_rearm_min_bars"] = 2
        with self.assertRaisesRegex(
            ValidationError,
            "Balance rearm minimum bars cannot be less than required inside closes",
        ):
            AuctionEngineConfig.model_validate(payload)

    def test_balance_escape_memory_roundtrip_preserves_rearm_history(self) -> None:
        memory = BalanceEpisodeMemory(
            sequence=1,
            episode_id="BAL:TEST:2026-07-20:001:NEUTRAL:100000",
            state=BalanceEpisodeState.FAILED_BACK_INSIDE,
            started_at=datetime(2026, 7, 20, 10, 0),
            state_started_at=datetime(2026, 7, 20, 10, 30),
            state_age_bars=2,
            range_id="RANGE:TEST",
            candidate_low=None,
            candidate_high=None,
            source_range_ids=("RANGE:TEST",),
            candidate_merge_count=0,
            candidate_bar_expansion_count=0,
            candidate_last_valid_at=datetime(2026, 7, 20, 10, 24),
            frozen_low=99.0,
            frozen_high=101.0,
            containment_bars=8,
            forming_bars_observed=10,
            marginal_excursion_bars=0,
            meaningful_escape_bars=0,
            forming_invalid_bars=0,
            escape_direction=DirectionalBias.UP,
            outside_close_count=1,
            reentry_close_count=1,
            escape_attempt_count=2,
            failed_escape_count=2,
            up_escape_attempt_count=2,
            down_escape_attempt_count=0,
            last_escape_direction=DirectionalBias.UP,
            last_escape_started_at=datetime(2026, 7, 20, 10, 27),
            last_escape_failed_at=datetime(2026, 7, 20, 10, 30),
            rearm_required=True,
            rearm_inside_close_count=0,
            rearm_bars_elapsed=0,
            attempt_limit_reached=True,
            emitted_event_ids=("event-1", "event-2"),
            last_reason_codes=("BALANCE_REARM_REQUIRED",),
        )

        restored = BalanceEpisodeMemory.model_validate(
            memory.model_dump(mode="json")
        )

        self.assertEqual(restored, memory)

    def test_escape_started_is_authoritative_breakout_initiation_permission(self) -> None:
        event = AuctionEvent(
            event_id="BAL:TEST:2026-07-20:001:NEUTRAL:100000:BALANCE_ESCAPE_STARTED:100300",
            event_type=AuctionEventType.BALANCE_ESCAPE_STARTED,
            episode_id="BAL:TEST:2026-07-20:001:NEUTRAL:100000",
            symbol="TEST",
            trading_day=date(2026, 7, 20),
            event_time=datetime(2026, 7, 20, 10, 3),
            direction=DirectionalBias.UP,
            reason_codes=("MEANINGFUL_CLOSE_OUTSIDE_FROZEN_BALANCE",),
        )
        permissions = StructuralPermissionMatrix().evaluate(
            balance_state=BalanceEpisodeState.ESCAPE_WATCH,
            events=(event,),
        )
        by_family = {item.setup_family: item for item in permissions}
        initiation = by_family[SetupFamily.BREAKOUT_INITIATION]
        self.assertIs(initiation.result, StructuralPermissionResult.PERMIT)
        self.assertEqual(initiation.source_event_ids, (event.event_id,))

    def test_locked_balance_overrides_reversal_event_permission(self) -> None:
        event = AuctionEvent(
            event_id="DIR:TEST:2026-07-20:001:UP:100000:REVERSAL:100300",
            event_type=AuctionEventType.DIRECTIONAL_REVERSAL_LEG_ESTABLISHED,
            episode_id="DIR:TEST:2026-07-20:001:UP:100000",
            symbol="TEST",
            trading_day=date(2026, 7, 20),
            event_time=datetime(2026, 7, 20, 10, 3),
            direction=DirectionalBias.DOWN,
            reason_codes=("TEST_EVENT",),
        )
        permissions = StructuralPermissionMatrix().evaluate(
            balance_state=BalanceEpisodeState.LOCKED,
            events=(event,),
        )
        by_family = {item.setup_family: item for item in permissions}
        reversal = by_family[SetupFamily.REVERSAL]
        self.assertIs(reversal.result, StructuralPermissionResult.BLOCK)
        self.assertEqual(reversal.source_event_ids, (event.event_id,))


class AuctionEngineContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ts = datetime(2026, 7, 20, 10, 0, 0)

    def _bar(self) -> BarEvidence:
        return BarEvidence(
            snapshot_time=self.ts,
            open=100.0,
            high=102.0,
            low=99.5,
            close=101.5,
            volume=10000,
            direction=DirectionalBias.UP,
            body_fraction=0.60,
            close_position=0.80,
        )


    def test_stable_key_is_deterministic(self) -> None:
        a = stable_key("episode", "TEST", date(2026, 7, 20), "RANGE-1", 1, "UPPER")
        b = stable_key("episode", "TEST", date(2026, 7, 20), "RANGE-1", 1, "UPPER")
        c = stable_key("episode", "TEST", date(2026, 7, 20), "RANGE-1", 2, "UPPER")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_evidence_snapshot_is_causal(self) -> None:
        evidence = EvidenceSnapshot(
            symbol="test",
            trading_day=self.ts.date(),
            snapshot_time=self.ts,
            close=101.5,
            atr=1.0,
            bar=self._bar(),
        )
        self.assertEqual(evidence.symbol, "TEST")
        json.dumps(evidence.to_storage_dict())

        future_fact = EvidenceFact(
            code="FUTURE_FACT",
            domain="price_action",
            polarity=EvidencePolarity.SUPPORT,
            observed_at=self.ts + timedelta(minutes=3),
        )
        with self.assertRaises(ValidationError):
            EvidenceSnapshot(
                symbol="TEST",
                trading_day=self.ts.date(),
                snapshot_time=self.ts,
                close=101.5,
                atr=1.0,
                bar=self._bar(),
                price_action={"supporting_facts": [future_fact]},
            )



    def test_authoritative_candidate_requires_permit_and_event_identity(self) -> None:
        candidate = AuthoritativeSetupCandidate(
            auction_engine_name=AUCTION_ENGINE_CONFIG.engine.engine_name,
            auction_engine_version=AUCTION_ENGINE_CONFIG.engine.engine_version,
            candidate_id="candidate-1",
            opportunity_key="opportunity-1",
            symbol="TEST",
            trading_day=self.ts.date(),
            snapshot_time=self.ts,
            setup_family=SetupFamily.REVERSAL,
            setup_subtype="STRUCTURAL_REVERSAL",
            side=TradeSide.BUY,
            source_event_id="event-1",
            source_event_type=AuctionEventType.DIRECTIONAL_REVERSAL_LEG_ESTABLISHED,
            source_episode_id="episode-1",
            structural_result=StructuralPermissionResult.PERMIT,
            entry_price=101.0,
            stop_anchor_price=100.0,
            stop_anchor_type="DIRECTIONAL_PROTECTION",
            target_reference_price=102.5,
            target_basis="ONE_POINT_FIVE_R",
            reference_price=100.0,
            reference_source="AUTHORITATIVE_EVENT_GEOMETRY",
            risk_points=1.0,
            expected_move_points=1.5,
            expected_move_pct=1.5 / 101.0,
            reward_risk=1.5,
            valid_until=self.ts + timedelta(minutes=6),
            reason_codes=("AUTHORITATIVE_EVENT_ROUTE",),
        )
        self.assertEqual(candidate.source_event_id, "event-1")
        payload = candidate.model_dump(mode="python")
        payload["structural_result"] = StructuralPermissionResult.BLOCK
        with self.assertRaises(ValidationError):
            AuthoritativeSetupCandidate.model_validate(payload)



if __name__ == "__main__":
    unittest.main(verbosity=2)
