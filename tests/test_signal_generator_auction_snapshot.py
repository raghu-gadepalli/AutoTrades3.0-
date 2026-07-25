from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from configs.signal_config import SIGNAL_CONFIG
from enums.enums import LifecycleStage, SignalSide, SignalStatus
from schemas.signal import SignalSchema
from schemas.snapshot import (
    AuctionDecisionProjection,
    AuctionSnapshotBlock,
    AuctionStateProjection,
    BarBlock,
    CandidateProjection,
    OpportunityProjection,
    SnapshotSchema,
    StockContextProjection,
)
from services.signals.signal_generator import SignalAssembler
from services.trade.monitor.signal_contract import AuctionTradeSignalContext


TS = datetime(2026, 7, 20, 11, 48, tzinfo=timezone.utc)
OPP = "OPPORTUNITY:test"
CAND = "ACCEPT:test"
BOUNDARY = "BOUNDARY_EVENT:test"


def _state(current: str = "ORDERLY_DOWNTREND") -> AuctionStateProjection:
    return AuctionStateProjection.model_validate({
        "state_key": "STATE:test",
        "previous": "REACCELERATION",
        "current": current,
        "transition_time": TS,
        "entered_at": TS,
        "expires_at": None,
        "reason_codes": ["TEST_STATE"],
    })


def _stock_context() -> StockContextProjection:
    return StockContextProjection.model_validate({
        "name": "DIRECTIONAL",
        "background_regime": "EARLY_EXPANSION",
        "current_auction_state": "ORDERLY_DOWNTREND",
        "directional_bias": "DOWN",
        "balanced_non_directional": False,
        "compression_active": False,
        "rotational": False,
        "fresh_expansion_confirmed": True,
        "directional_efficiency": 0.75,
        "overlap_ratio": 0.20,
        "ema_context": "DIVERGING_DOWN",
        "ema_spread_atr": 0.50,
        "ema_spread_change_atr_per_bar": 0.05,
        "hma_context": "DOWNTREND",
        "hma_spread_atr": 0.40,
        "atr_state": "EXPANDING",
        "atr_contraction_ratio": 1.10,
        "background_range_id": "RANGE:test",
        "background_range_low": 1499.0,
        "background_range_high": 1501.0,
        "background_range_position": -0.65,
        "background_range_outside_atr": 0.65,
        "background_range_classification": "BALANCE_QUALIFIED",
        "background_structure_flip_count": 1,
        "exhaustion_active": False,
        "exhausted_side": "UNKNOWN",
        "exhaustion_started_at": None,
        "exhaustion_expires_at": None,
        "reason_codes": ["TEST_DIRECTIONAL_CONTEXT"],
    })


def _candidate(candidate_id: str = CAND, opportunity_key: str = OPP) -> CandidateProjection:
    return CandidateProjection.model_validate({
        "candidate_id": candidate_id,
        "opportunity_key": opportunity_key,
        "family": "ACCEPTED_BREAKOUT",
        "subtype": "CONTINUATION_ACCEPTANCE",
        "role": "ACCEPTED_RESOLUTION_ENTRY",
        "side": "SELL",
        "eligibility": "ELIGIBLE",
        "blockers": [],
        "reason_codes": ["ACCEPTED_BREAKOUT_ELIGIBLE"],
        "event_key": BOUNDARY,
        "event_time": TS,
        "candidate_time": TS,
        "valid_until": None,
        "auction_state": "ORDERLY_DOWNTREND",
        "entry_price": 1497.7,
        "stop_anchor_price": 1499.1,
        "stop_anchor_type": "FROZEN_BOUNDARY",
        "target_basis": "OPEN_ENDED_BREAKOUT_NO_ASSUMED_TARGET",
        "target_reference_price": None,
        "room_points": None,
        "room_atr": None,
        "room_pct": None,
        "entry_distance_atr": None,
        "source_boundary_id": "BOUNDARY:test",
        "source_boundary_status": "ACCEPTED",
        "source_boundary_resolution": "ACCEPTED",
        "source_boundary_side": "LOWER",
        "source_boundary_price": 1499.1,
        "source_frozen_range_id": "RANGE:test",
        "source_frozen_range_version": 1,
        "terminal": True,
        "consumed": False,
        "superseded": False,
    })


def _opportunity(
    opportunity_key: str = OPP,
    candidate_id: str = CAND,
    lifecycle: str = "ELIGIBLE",
    side: str = "SELL",
) -> OpportunityProjection:
    return OpportunityProjection.model_validate({
        "opportunity_key": opportunity_key,
        "side": side,
        "lifecycle": lifecycle,
        "boundary_event_key": BOUNDARY,
        "primary_candidate_id": candidate_id,
        "primary_family": "ACCEPTED_BREAKOUT",
        "primary_subtype": "CONTINUATION_ACCEPTANCE",
        "primary_role": "ACCEPTED_RESOLUTION_ENTRY",
        "primary_eligibility": "ELIGIBLE" if lifecycle == "ELIGIBLE" else "WATCH",
        "candidate_ids": [candidate_id],
        "supporting_candidate_ids": [],
        "selected_candidate_id": candidate_id if lifecycle == "ELIGIBLE" else None,
        "first_observed_time": TS,
        "last_observed_time": TS,
        "eligible_time": TS if lifecycle == "ELIGIBLE" else None,
        "selected_time": TS if lifecycle == "ELIGIBLE" else None,
        "reason_codes": ["TEST_OPPORTUNITY"],
    })


def _decision(
    action: str,
    opportunity_key: str = OPP,
    candidate_id: str = CAND,
    side: str = "SELL",
) -> AuctionDecisionProjection:
    confirmed = action == "LOCAL_CONFIRMED"
    return AuctionDecisionProjection.model_validate({
        "action": action,
        "manager_action": "SELECT" if confirmed else "NO_ACTION",
        "selected_candidate_id": candidate_id if confirmed else None,
        "selected_opportunity_key": opportunity_key if confirmed else None,
        "family": "ACCEPTED_BREAKOUT" if confirmed else None,
        "subtype": "CONTINUATION_ACCEPTANCE" if confirmed else None,
        "side": side if confirmed else None,
        "entry_price": 1497.7 if confirmed else None,
        "stop_anchor_price": 1499.1 if confirmed else None,
        "stop_anchor_type": "FROZEN_BOUNDARY" if confirmed else None,
        "target_basis": "OPEN_ENDED_BREAKOUT_NO_ASSUMED_TARGET" if confirmed else None,
        "target_reference_price": None,
        "valid_until": None,
        "reason_codes": [f"TEST_{action}"],
    })


def _snapshot(
    *,
    action: str,
    opportunities: list[OpportunityProjection],
    candidates: list[CandidateProjection],
    opportunity_key: str = OPP,
    candidate_id: str = CAND,
    ltp: float | None = None,
    auction_state: str = "ORDERLY_DOWNTREND",
    raw_side: str = "SELL",
    decision_side: str = "SELL",
) -> SnapshotSchema:
    auction = AuctionSnapshotBlock.model_validate({
        "status": "OK",
        "continuity_mode": "INCREMENTAL_PREVIOUS_SNAPSHOT",
        "previous_snapshot_time": TS,
        "state": _state(auction_state).model_dump(mode="python"),
        "stock_context": _stock_context().model_dump(mode="python"),
        "boundary": None,
        "candidates": [x.model_dump(mode="python") for x in candidates],
        "opportunities": [x.model_dump(mode="python") for x in opportunities],
        "decision": _decision(action, opportunity_key, candidate_id, decision_side).model_dump(mode="python"),
        "changes": [],
        "error": None,
    })
    return SnapshotSchema.model_construct(
        version="SNAPSHOT_AUCTION_V1",
        symbol="COFORGE",
        snapshot_time=TS,
        tf="3m",
        close=1497.7,
        bar=BarBlock(open=1499.0, high=1500.0, low=1497.0, close=1497.7, volume=1000.0),
        ltp=ltp,
        ltp_time=None,
        gen_signals=True,
        indicators=SimpleNamespace(atr=SimpleNamespace(value=10.0)),
        structure=SimpleNamespace(raw=SimpleNamespace(side=raw_side)),
        auction=auction,
    )


def _active_signal(opportunity_key: str = OPP) -> SignalSchema:
    candidate_id = CAND if opportunity_key == OPP else f"ACCEPT:{opportunity_key}"
    snapshot = _snapshot(
        action="LOCAL_CONFIRMED",
        opportunities=[_opportunity(opportunity_key, candidate_id)],
        candidates=[_candidate(candidate_id, opportunity_key)],
        opportunity_key=opportunity_key,
        candidate_id=candidate_id,
    )
    events = SignalAssembler(
        fetcher=FakeFetcher(),
        persister=FakePersister(),
    ).assemble(snapshot)
    if len(events) != 1 or events[0][0] != "CREATE":
        raise AssertionError("active signal fixture failed to create")
    return events[0][1]


class FakeFetcher:
    def __init__(self, active: SignalSchema | None = None, by_id: SignalSchema | None = None):
        self.active = active
        self.by_id = by_id
        self.symbol = SimpleNamespace(active=True, generate_signals=True, equity_ref="COFORGE")

    def fetch_symbol(self, symbol: str):
        return self.symbol

    def fetch_active_signal(self, equity_ref: str, lifecycle: str):
        return self.active

    def fetch_signal_by_id(self, signal_id: str):
        return self.by_id


class FakePersister:
    def __init__(self):
        self.created = None
        self.updated = None

    def create(self, **kwargs):
        self.created = kwargs
        identity = kwargs["instruction"].current_opportunity
        decision = kwargs["instruction"].decision
        return SignalSchema.model_construct(
            signal_id=kwargs["signal_id"],
            equity_ref=kwargs["equity_ref"],
            symbol=kwargs["snapshot"].symbol,
            lifecycle=kwargs["lifecycle"],
            setup=decision.family,
            side=SignalSide.from_string(decision.side),
            stage=kwargs["instruction"].lifecycle.stage,
            status=kwargs["instruction"].lifecycle.status,
            status_reason="created",
            first_seen_time=kwargs["snapshot"].snapshot_time,
            created_price=kwargs["snapshot"].close,
            last_eval_time=kwargs["snapshot"].snapshot_time,
            last_snapshot_time=kwargs["snapshot"].snapshot_time,
            criteria_json=kwargs["criteria_json"],
            snapshot_json={},
            meta_json=kwargs["meta_json"],
        )

    def update(self, **kwargs):
        self.updated = kwargs
        signal = kwargs["signal"]
        signal.meta_json = kwargs["meta_json"]
        signal.criteria_json = kwargs["criteria_json"]
        signal.stage = kwargs["instruction"].lifecycle.stage
        signal.status = kwargs["instruction"].lifecycle.status
        signal.status_reason = kwargs["instruction"].lifecycle.reason_code
        return signal

    def close(self, **kwargs):
        self.updated = kwargs
        signal = kwargs["signal"]
        signal.meta_json = kwargs["meta_json"]
        signal.criteria_json = kwargs["criteria_json"]
        signal.stage = kwargs["instruction"].lifecycle.stage
        signal.status = kwargs["instruction"].lifecycle.status
        signal.status_reason = kwargs["instruction"].lifecycle.reason_code
        return signal


class AuctionSignalGeneratorTests(unittest.TestCase):
    def setUp(self):
        self._audit_enabled = SIGNAL_CONFIG.audit.enabled
        SIGNAL_CONFIG.audit.enabled = False

    def tearDown(self):
        SIGNAL_CONFIG.audit.enabled = self._audit_enabled

    def test_local_confirmed_creates_once(self):
        snapshot = _snapshot(
            action="LOCAL_CONFIRMED",
            opportunities=[_opportunity()],
            candidates=[_candidate()],
            ltp=None,
        )
        persister = FakePersister()
        events = SignalAssembler(fetcher=FakeFetcher(), persister=persister).assemble(snapshot)
        self.assertEqual("CREATE", events[0][0])
        self.assertEqual(OPP, persister.created["instruction"].current_opportunity.opportunity_key)
        self.assertEqual("ACTIVE", events[0][1].meta_json["signal_lifecycle"]["stage"])
        self.assertEqual("OPEN", events[0][1].meta_json["signal_lifecycle"]["status"])
        meta = events[0][1].meta_json
        self.assertEqual(
            "AUCTION_SIGNAL_DOWNSTREAM_V2",
            meta["downstream_contract"]["version"],
        )
        self.assertEqual("CREATE", meta["signal"]["signal_action"])
        self.assertEqual("READY", meta["signal"]["signal_state"])
        self.assertTrue(meta["signal"]["price_action_confirmed"])
        self.assertEqual("CREATE_TRADE", meta["lifecycle"]["trade_action"])
        self.assertEqual("STRENGTHEN", meta["management"]["action"])
        self.assertTrue(meta["management"]["target_expansion_allowed"])
        self.assertFalse(meta["management"]["should_exit_signal"])
        self.assertEqual(1499.1, meta["setup_levels"]["reference_price"])
        self.assertEqual(OPP, meta["setup_levels"]["opportunity_key"])
        self.assertEqual(meta["setup_levels"], meta["signal"]["setup_levels"])
        context = AuctionTradeSignalContext.from_signal(events[0][1])
        self.assertEqual("AUCTION_SIGNAL_DOWNSTREAM_V2", context.contract_version)
        self.assertEqual("ACTIVE", context.stage)
        self.assertEqual("STRENGTHEN", context.management_posture)
        self.assertEqual(OPP, context.opportunity_key)
        self.assertIsNone(snapshot.ltp)

    def test_second_confirmed_updates_when_selected_candidate_is_not_in_current_list(self):
        snapshot = _snapshot(
            action="LOCAL_CONFIRMED",
            opportunities=[_opportunity()],
            candidates=[],
        )
        persister = FakePersister()
        events = SignalAssembler(
            fetcher=FakeFetcher(active=_active_signal()),
            persister=persister,
        ).assemble(snapshot)
        self.assertEqual("HOLD", events[0][0])
        self.assertTrue(persister.updated["instruction"].same_opportunity)
        self.assertEqual(LifecycleStage.ACTIVE, events[0][1].stage)

    def test_watch_updates_created_signal_for_exact_opportunity(self):
        snapshot = _snapshot(
            action="LOCAL_WATCH",
            opportunities=[_opportunity(lifecycle="WATCH")],
            candidates=[_candidate()],
        )
        persister = FakePersister()
        events = SignalAssembler(
            fetcher=FakeFetcher(active=_active_signal()),
            persister=persister,
        ).assemble(snapshot)
        self.assertEqual("DOWNGRADE", events[0][0])
        self.assertTrue(persister.updated["instruction"].same_opportunity)
        self.assertEqual(LifecycleStage.PROTECT, events[0][1].stage)
        self.assertEqual(
            "PROTECT",
            events[0][1].meta_json["signal_lifecycle"]["stage"],
        )
        self.assertEqual(
            2,
            len(events[0][1].meta_json["signal_lifecycle_history"]),
        )
        meta = events[0][1].meta_json
        self.assertEqual("CAUTION", meta["management"]["action"])
        self.assertEqual("DEFENSIVE", meta["management"]["trail_mode"])
        self.assertFalse(meta["management"]["target_expansion_allowed"])
        self.assertFalse(meta["management"]["should_exit_signal"])
        self.assertEqual("TIGHTEN_STOP", meta["lifecycle"]["trade_action"])
        self.assertEqual("MANAGE", meta["signal"]["signal_state"])

    def test_watch_does_not_create_without_active_signal(self):
        snapshot = _snapshot(
            action="LOCAL_WATCH",
            opportunities=[_opportunity(lifecycle="WATCH")],
            candidates=[_candidate()],
        )
        persister = FakePersister()
        events = SignalAssembler(fetcher=FakeFetcher(), persister=persister).assemble(snapshot)
        self.assertEqual([], events)
        self.assertIsNone(persister.created)

    def test_confirmed_different_opportunity_updates_active_signal_without_replacement(self):
        other_opp = "OPPORTUNITY:other"
        other_cand = "ACCEPT:other"
        snapshot = _snapshot(
            action="LOCAL_CONFIRMED",
            opportunities=[_opportunity(other_opp, other_cand)],
            candidates=[_candidate(other_cand, other_opp)],
            opportunity_key=other_opp,
            candidate_id=other_cand,
        )
        persister = FakePersister()
        events = SignalAssembler(
            fetcher=FakeFetcher(active=_active_signal()),
            persister=persister,
        ).assemble(snapshot)
        self.assertEqual("DOWNGRADE", events[0][0])
        self.assertTrue(persister.updated["instruction"].competing_confirmed_opportunity)
        self.assertEqual(LifecycleStage.PROTECT, events[0][1].stage)
        self.assertEqual(OPP, persister.updated["signal"].meta_json["auction_signal"]["opportunity_key"])

    def test_aligned_reacceleration_promotes_to_expand(self):
        snapshot = _snapshot(
            action="LOCAL_CONFIRMED",
            opportunities=[_opportunity()],
            candidates=[],
            auction_state="REACCELERATION",
            raw_side="SELL",
        )
        persister = FakePersister()
        events = SignalAssembler(
            fetcher=FakeFetcher(active=_active_signal()),
            persister=persister,
        ).assemble(snapshot)
        self.assertEqual("PROMOTE", events[0][0])
        self.assertEqual(LifecycleStage.EXPAND, events[0][1].stage)
        meta = events[0][1].meta_json
        self.assertEqual("STRENGTHEN", meta["management"]["action"])
        self.assertEqual("NORMAL", meta["management"]["trail_mode"])
        self.assertTrue(meta["management"]["target_expansion_allowed"])
        self.assertEqual("HOLD_POSITION", meta["lifecycle"]["trade_action"])

    def test_opposite_orderly_trend_downgrades_to_exit_bias(self):
        snapshot = _snapshot(
            action="LOCAL_CONFIRMED",
            opportunities=[_opportunity()],
            candidates=[],
            auction_state="ORDERLY_UPTREND",
            raw_side="BUY",
        )
        persister = FakePersister()
        events = SignalAssembler(
            fetcher=FakeFetcher(active=_active_signal()),
            persister=persister,
        ).assemble(snapshot)
        self.assertEqual("DOWNGRADE", events[0][0])
        self.assertEqual(LifecycleStage.EXIT_BIAS, events[0][1].stage)
        self.assertEqual("OPPOSITE", persister.updated["instruction"].lifecycle.directional_alignment)
        meta = events[0][1].meta_json
        self.assertEqual("EXIT", meta["management"]["action"])
        self.assertEqual("EXIT_READY", meta["management"]["trail_mode"])
        self.assertEqual("HIGH", meta["management"]["exit_pressure"])
        self.assertTrue(meta["management"]["should_exit_signal"])
        self.assertEqual("EXIT_POSITION", meta["lifecycle"]["trade_action"])
        self.assertEqual("MANAGE", meta["signal"]["signal_state"])

    def test_expired_active_opportunity_closes_signal(self):
        snapshot = _snapshot(
            action="NO_LOCAL_OPPORTUNITY",
            opportunities=[_opportunity(lifecycle="EXPIRED")],
            candidates=[],
        )
        persister = FakePersister()
        events = SignalAssembler(
            fetcher=FakeFetcher(active=_active_signal()),
            persister=persister,
        ).assemble(snapshot)
        self.assertEqual("EXPIRE", events[0][0])
        self.assertEqual(LifecycleStage.FORCE_EXIT, events[0][1].stage)
        self.assertEqual(SignalStatus.EXPIRED, events[0][1].status)
        meta = events[0][1].meta_json
        self.assertEqual("FORCE_EXIT", meta["lifecycle"]["trade_action"])
        self.assertEqual("EXPIRED", meta["signal"]["signal_state"])
        self.assertTrue(meta["management"]["should_exit_signal"])

    def test_missing_active_opportunity_holds_current_stage(self):
        signal = _active_signal()
        signal.stage = LifecycleStage.WEAKENING
        snapshot = _snapshot(
            action="NO_LOCAL_OPPORTUNITY",
            opportunities=[],
            candidates=[],
            auction_state="ORDERLY_UPTREND",
            raw_side="BUY",
        )
        persister = FakePersister()
        events = SignalAssembler(
            fetcher=FakeFetcher(active=signal),
            persister=persister,
        ).assemble(snapshot)
        self.assertEqual("HOLD", events[0][0])
        self.assertEqual(LifecycleStage.WEAKENING, events[0][1].stage)
        self.assertEqual(
            "ACTIVE_OPPORTUNITY_NOT_IN_CURRENT_PROJECTION_HOLD",
            persister.updated["instruction"].lifecycle.reason_code,
        )

    def test_legacy_v1_downstream_contract_fails_visible(self):
        signal = _active_signal()
        signal.meta_json["downstream_contract"]["version"] = "AUCTION_SIGNAL_DOWNSTREAM_V1"
        snapshot = _snapshot(
            action="LOCAL_WATCH",
            opportunities=[_opportunity(lifecycle="WATCH")],
            candidates=[_candidate()],
        )
        with self.assertRaisesRegex(ValueError, "Legacy downstream contracts are not supported"):
            SignalAssembler(
                fetcher=FakeFetcher(active=signal),
                persister=FakePersister(),
            ).assemble(snapshot)

    def test_active_non_auction_signal_fails_visible(self):
        signal = _active_signal()
        signal.meta_json = {"reason": "legacy"}
        snapshot = _snapshot(
            action="NO_LOCAL_OPPORTUNITY",
            opportunities=[],
            candidates=[],
        )
        with self.assertRaisesRegex(ValueError, "not Auction-linked"):
            SignalAssembler(
                fetcher=FakeFetcher(active=signal),
                persister=FakePersister(),
            ).assemble(snapshot)

    def test_confirmed_identity_mismatch_fails(self):
        snapshot = _snapshot(
            action="LOCAL_CONFIRMED",
            opportunities=[_opportunity(candidate_id="ACCEPT:different")],
            candidates=[],
        )
        with self.assertRaises(ValueError):
            SignalAssembler(fetcher=FakeFetcher(), persister=FakePersister()).assemble(snapshot)


if __name__ == "__main__":
    unittest.main()
