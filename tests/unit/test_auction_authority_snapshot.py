from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import unittest

from pydantic import ValidationError

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG, AuctionEngineConfig
from enums.auction_engine import AuctionEventType, DirectionalBias
from schemas.snapshot import SnapshotSchema
from services.auction_engine.engine import AuctionEngine
from services.auction_engine.episode_contracts import (
    AuctionEvent,
    AuctionLifecycleProjection,
    AuctionObservation,
)
from services.auction_engine.snapshot_adapter import (
    empty_auction_block,
    enrich_snapshot_with_auction,
    initial_auction_memory,
)


def _config() -> AuctionEngineConfig:
    payload = AUCTION_ENGINE_CONFIG.resolved_dict()
    payload["evidence"]["minimum_history_bars"] = 1
    payload["evidence"]["extension_min_history_bars_for_maturity"] = 2
    return AuctionEngineConfig.model_validate(payload)


def _state_entry() -> dict:
    return {
        "raw_state": "UNKNOWN",
        "state": "UNKNOWN",
        "count": 1,
        "previous_state": None,
        "previous_count": 0,
        "candidate_state": None,
        "candidate_count": 0,
        "flip_count_today": 0,
    }


def _snapshot(ts: datetime, *, close: float, direction: str = "UP") -> SnapshotSchema:
    up = direction == "UP"
    open_price = close - 0.15 if up else close + 0.15
    high = max(open_price, close) + 0.20
    low = min(open_price, close) - 0.20
    hma_state = "UPTREND" if up else "DOWNTREND"
    vwap_side = "ABOVE" if up else "BELOW"
    slope = 0.12 if up else -0.12
    range_low = 99.0
    range_high = 101.0
    state_memory = {
        key: _state_entry()
        for key in (
            "hma.state",
            "vwap.side",
            "structure.accepted",
            "structure.raw.side",
            "structure.candidate",
            "structure.raw.state",
            "structure.session_phase",
            "structure.accepted.state",
            "structure.candidate.active",
        )
    }
    payload = {
        "version": "SNAPSHOT_AUCTION_AUTHORITY_V3A",
        "symbol": "TEST",
        "snapshot_time": ts,
        "tf": "3m",
        "close": close,
        "bar": {
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": 10000.0,
        },
        "ltp": close,
        "ltp_time": ts,
        "gen_signals": True,
        "levels": {
            "prev_day": {"open": 98.0, "high": 105.0, "low": 95.0, "close": 99.0},
            "today": {"open": 100.0},
            "opening_range": {
                "window": "09:15-09:29",
                "ready": True,
                "high": 101.0,
                "low": 99.0,
            },
        },
        "indicators": {
            "ema": {"fast": None, "mid1": None, "mid2": None, "slow": 100.0, "ref": 99.8},
            "hma": {
                "fast": close + (0.20 if up else -0.20),
                "mid1": close + (0.10 if up else -0.10),
                "mid2": close,
                "slow": close - (0.10 if up else -0.10),
                "state": hma_state,
                "strength": "STRONG",
                "flip_count_today": 0,
            },
            "vwap": {
                "value": close - 0.2 if up else close + 0.2,
                "side": vwap_side,
                "distance_atr": 0.2,
                "distance_pct": 0.2,
                "distance_points": 0.2,
                "flip_count_today": 0,
            },
            "rsi": {"value": 55.0 if up else 45.0, "zone": "MID"},
            "adx": {"value": 25.0, "band": "MEDIUM"},
            "atr": {"value": 1.0, "band": "NORMAL", "pct": 1.0},
            "bollinger": {
                "position": 0.6 if up else 0.4,
                "zone": "MID",
                "upper": None,
                "mid": None,
                "lower": None,
                "bb_width": None,
            },
            "envelopes": {"hma_envelope": 0.1, "ema_envelope": 0.2},
        },
        "volume": {
            "bar_volume": 10000.0,
            "bar_rvol": 1.1,
            "bar_rvol_pct": 110.0,
            "bar_rvol_band": "NORMAL",
            "bar_volume_slope": None,
            "today_cum": 10000.0,
            "prev_day_total": 100000.0,
            "today_vs_prev_ratio": 0.1,
            "periods": {},
        },
        "market_windows": {
            "15m": {"status": "ok", "bars": 5, "move_points": slope * 5, "move_pct": 0.2, "move_atr": slope * 5, "range_points": 2.0, "range_pct": 2.0, "close_position_in_range": 0.7 if up else 0.3},
            "30m": {"status": "ok", "bars": 10, "move_points": slope * 10, "move_pct": 0.4, "move_atr": slope * 10, "range_points": 3.0, "range_pct": 3.0, "close_position_in_range": 0.7 if up else 0.3},
            "60m": {"status": "na", "bars": 0, "move_points": None, "move_pct": None, "move_atr": None, "range_points": None, "range_pct": None, "close_position_in_range": None},
            "sod": {"status": "ok", "bars": 10, "move_points": slope * 10, "move_pct": 0.4, "move_atr": slope * 10, "range_points": 4.0, "range_pct": 4.0, "close_position_in_range": 0.7 if up else 0.3},
        },
        "price_action": {
            "slope": {
                "status": "ok",
                "bars_3_atr": slope * 3,
                "bars_5_atr": slope * 5,
                "bars_3_atr_per_bar": slope,
                "bars_5_atr_per_bar": slope,
                "previous_3_atr_per_bar": slope,
                "state": direction,
            }
        },
        "structure": {
            "raw": {
                "state": "RANGE",
                "side": direction,
                "range": {
                    "range_id": "RANGE-1",
                    "version": 1,
                    "high": range_high,
                    "low": range_low,
                    "width_atr": 2.0,
                    "source": "DYNAMIC",
                    "range_type": "BALANCE",
                    "bars": 10,
                    "provisional": False,
                    "breakout_eligible": True,
                },
                "metrics": {
                    "directional_efficiency": 0.20,
                    "adjacent_overlap_ratio": 0.75,
                    "classification": "BALANCE",
                },
                "recent_swing_high": 101.2,
                "recent_swing_low": 98.8,
            },
            "accepted": {
                "state": "RANGE_ACCEPTED",
                "range": {
                    "range_id": "RANGE-1",
                    "version": 1,
                    "high": range_high,
                    "low": range_low,
                    "width_atr": 2.0,
                    "source": "DYNAMIC",
                    "range_type": "BALANCE",
                    "bars": 10,
                    "provisional": False,
                    "breakout_eligible": True,
                    "established_at": ts,
                },
                "metrics": {
                    "directional_efficiency": 0.20,
                    "adjacent_overlap_ratio": 0.75,
                    "classification": "BALANCE",
                },
                "age_bars": 10,
                "frozen": True,
            },
            "candidate": {"active": False, "status": "NONE", "side": "NEUTRAL", "range": {}, "metrics": {}, "bars_confirmed": 0},
            "session_phase": "MID",
            "flip_count_today": 0,
        },
        "derivatives": {
            "spot_price": None,
            "future": None,
            "options_lite": None,
            "option_ladder": None,
            "option_sentiment_windows": None,
            "future_sentiment_windows": None,
        },
        "auction": empty_auction_block().model_dump(mode="python"),
        "memory": {
            "structure": {
                "snapshot_time": ts,
                "bars_3m": [{"date": ts, "open": open_price, "high": high, "low": low, "close": close, "volume": 10000.0}],
                "bars_15m": [],
                "state": state_memory,
            },
            "auction": initial_auction_memory("TEST", ts).model_dump(mode="python"),
        },
    }
    return SnapshotSchema.model_validate(payload)


def _finalize(pre: SnapshotSchema, previous: SnapshotSchema | None = None) -> SnapshotSchema:
    block, memory = enrich_snapshot_with_auction(pre, previous_snapshot=previous)
    payload = pre.model_dump(mode="python", by_alias=True)
    payload["auction"] = block.model_dump(mode="python")
    payload["memory"]["auction"] = memory.model_dump(mode="python")
    return SnapshotSchema.model_validate(payload)


class AuctionAuthoritySnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ts = datetime(2026, 7, 27, 9, 15)

    def _rows(self) -> list[SnapshotSchema]:
        closes = (100.0, 100.2, 100.4, 100.1, 99.8, 99.6, 99.4, 99.7)
        return [
            _snapshot(
                self.ts + timedelta(minutes=index * 3),
                close=close,
                direction="UP" if index < 4 else "DOWN",
            )
            for index, close in enumerate(closes)
        ]

    @staticmethod
    def _signature(result) -> tuple:
        return (
            result.lifecycle.directional.model_dump(mode="json"),
            result.lifecycle.balance.model_dump(mode="json"),
            tuple(item.model_dump(mode="json") for item in result.lifecycle.events),
            tuple(item.model_dump(mode="json") for item in result.lifecycle.permissions),
        )

    def test_incremental_memory_matches_continuous_engine(self) -> None:
        config = _config()
        continuous = AuctionEngine(config)
        carried = None
        for row in self._rows():
            expected = continuous.evaluate_snapshot(row)
            incremental = AuctionEngine(config)
            if carried is not None:
                incremental.restore_incremental_state("TEST", carried)
            actual = incremental.evaluate_snapshot(row)
            self.assertEqual(self._signature(expected), self._signature(actual))
            carried = incremental.export_incremental_state("TEST")

    def test_snapshot_adapter_continuity_and_no_legacy_projection(self) -> None:
        previous = None
        for row in self._rows():
            current = _finalize(row, previous)
            public = current.auction.model_dump(mode="python")
            self.assertEqual(
                set(public),
                {
                    "status",
                    "continuity_mode",
                    "previous_snapshot_time",
                    "engine",
                    "observation",
                    "lifecycle",
                },
            )
            if previous is None:
                self.assertEqual(current.auction.continuity_mode, "COLD_START")
                self.assertIsNone(current.auction.previous_snapshot_time)
            else:
                self.assertEqual(
                    current.auction.previous_snapshot_time,
                    previous.snapshot_time,
                )
            legacy_keys = {
                "state",
                "stock_context",
                "boundary",
                "candidates",
                "opportunities",
                "decision",
                "changes",
                "error",
            }
            self.assertFalse(legacy_keys.intersection(public))
            previous = current

    def test_restore_rejects_wrong_symbol(self) -> None:
        engine = AuctionEngine(_config())
        row = self._rows()[0]
        engine.evaluate_snapshot(row)
        memory = engine.export_incremental_state("TEST")
        corrupt = memory.model_copy(update={"symbol": "OTHER"})
        with self.assertRaisesRegex(ValueError, "symbol mismatch"):
            AuctionEngine(_config()).restore_incremental_state("TEST", corrupt)

    def test_removed_memory_hash_fields_are_rejected(self) -> None:
        final = _finalize(self._rows()[0])
        payload = final.model_dump(mode="python")
        payload["auction"]["memory_hash"] = "0" * 64
        payload["auction"]["previous_memory_hash"] = "1" * 64
        with self.assertRaisesRegex(ValidationError, "extra_forbidden"):
            SnapshotSchema.model_validate(payload)

    def test_legacy_config_metadata_never_gates_snapshot_validation(self) -> None:
        final = _finalize(self._rows()[0])
        payload = final.model_dump(mode="python")
        payload["auction"]["engine"]["config_version"] = "OLD_VERSION"
        payload["auction"]["engine"]["config_hash"] = "old-hash"
        payload["auction"]["lifecycle"]["config_version"] = "NEW_VERSION"
        payload["auction"]["lifecycle"]["config_hash"] = "different-hash"

        restored = SnapshotSchema.model_validate(payload)

        self.assertEqual(restored.auction.engine.config_version, "OLD_VERSION")
        self.assertEqual(restored.auction.lifecycle.config_version, "NEW_VERSION")

    def test_persisted_json_roundtrip_preserves_episode_memory(self) -> None:
        import json

        final = _finalize(self._rows()[0])
        persisted = json.loads(json.dumps(final.to_db_dict()))
        restored = SnapshotSchema.from_db_dict(persisted)
        self.assertEqual(restored.memory.auction, final.memory.auction)
        self.assertEqual(
            restored.auction.lifecycle.model_dump(mode="json"),
            final.auction.lifecycle.model_dump(mode="json"),
        )

    def test_non_finite_auction_memory_is_rejected_before_persistence(self) -> None:
        final = _finalize(self._rows()[0])
        payload = final.model_dump(mode="python")
        payload["memory"]["auction"]["evidence_history"][0]["trend"][
            "hma_spread_atr"
        ] = float("nan")
        with self.assertRaisesRegex(ValidationError, "finite_number"):
            SnapshotSchema.model_validate(payload)

    def test_observation_contract_has_no_legacy_authority_fields(self) -> None:
        final = _finalize(self._rows()[0])
        observation = final.auction.observation
        self.assertIsNotNone(observation)
        fields = set(type(observation).model_fields)
        self.assertNotIn("established_trend_side", fields)
        self.assertNotIn("failure_watch_active", fields)

    def test_trend_restoration_resolves_matching_active_exhaustion(self) -> None:
        final = _finalize(self._rows()[0])
        observation_payload = final.auction.observation.model_dump(mode="python")
        observation_payload.update(
            {
                "exhaustion_active": True,
                "exhausted_side": DirectionalBias.UP,
            }
        )
        observation = AuctionObservation.model_validate(observation_payload)

        lifecycle_payload = final.auction.lifecycle.model_dump(mode="python")
        event = AuctionEvent(
            event_id="DIR:TEST:RESTORED:UP",
            event_type=AuctionEventType.DIRECTIONAL_TREND_RESTORED,
            episode_id="DIR:TEST:REVERSAL",
            symbol="TEST",
            trading_day=final.snapshot_time.date(),
            event_time=final.snapshot_time,
            direction=DirectionalBias.UP,
            reason_codes=("TEST_RESTORATION",),
            data={
                "exhaustion_was_active": True,
                "exhausted_side_before_restoration": "UP",
                "exhaustion_resolution": (
                    "PARENT_TREND_RESTORED_AFTER_ESTABLISHED_REVERSAL"
                ),
            },
        )
        lifecycle_payload["events"] = [event.model_dump(mode="python")]
        lifecycle_payload["permissions"] = []
        lifecycle = AuctionLifecycleProjection.model_validate(lifecycle_payload)

        engine = AuctionEngine(_config())
        memory = engine.observation_provider._new_memory()
        memory.trading_day = final.snapshot_time.date()
        memory.last_snapshot_time = final.snapshot_time
        memory.exhaustion_active = True
        memory.exhaustion_side = DirectionalBias.UP
        engine.observation_provider._memory["TEST"] = memory

        resolved_observation, resolved_lifecycle = (
            engine._resolve_restoration_exhaustion(
                "TEST",
                observation,
                lifecycle,
            )
        )

        self.assertFalse(resolved_observation.exhaustion_active)
        self.assertIs(resolved_observation.exhausted_side, DirectionalBias.UNKNOWN)
        self.assertIn(
            "EXHAUSTION_RESOLVED_BY_TREND_RESTORATION",
            resolved_observation.source_reason_codes,
        )
        self.assertTrue(
            resolved_lifecycle.diagnostics["exhaustion_resolution_applied"]
        )
        exported = engine.observation_provider.export_memory("TEST")
        self.assertFalse(exported.exhaustion_active)

    def test_trend_restoration_does_not_clear_opposite_exhaustion(self) -> None:
        final = _finalize(self._rows()[0])
        observation_payload = final.auction.observation.model_dump(mode="python")
        observation_payload.update(
            {
                "exhaustion_active": True,
                "exhausted_side": DirectionalBias.DOWN,
            }
        )
        observation = AuctionObservation.model_validate(observation_payload)

        lifecycle_payload = final.auction.lifecycle.model_dump(mode="python")
        event = AuctionEvent(
            event_id="DIR:TEST:RESTORED:UP",
            event_type=AuctionEventType.DIRECTIONAL_TREND_RESTORED,
            episode_id="DIR:TEST:REVERSAL",
            symbol="TEST",
            trading_day=final.snapshot_time.date(),
            event_time=final.snapshot_time,
            direction=DirectionalBias.UP,
            reason_codes=("TEST_RESTORATION",),
        )
        lifecycle_payload["events"] = [event.model_dump(mode="python")]
        lifecycle_payload["permissions"] = []
        lifecycle = AuctionLifecycleProjection.model_validate(lifecycle_payload)

        engine = AuctionEngine(_config())
        memory = engine.observation_provider._new_memory()
        memory.trading_day = final.snapshot_time.date()
        memory.last_snapshot_time = final.snapshot_time
        memory.exhaustion_active = True
        memory.exhaustion_side = DirectionalBias.DOWN
        engine.observation_provider._memory["TEST"] = memory

        unresolved_observation, unresolved_lifecycle = (
            engine._resolve_restoration_exhaustion(
                "TEST",
                observation,
                lifecycle,
            )
        )

        self.assertTrue(unresolved_observation.exhaustion_active)
        self.assertIs(unresolved_observation.exhausted_side, DirectionalBias.DOWN)
        self.assertFalse(
            unresolved_lifecycle.diagnostics["exhaustion_resolution_applied"]
        )


if __name__ == "__main__":
    unittest.main()
