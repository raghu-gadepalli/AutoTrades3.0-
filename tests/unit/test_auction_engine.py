from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG, AuctionEngineConfig
from enums.auction_engine import AuctionEventType, DirectionalBias
from schemas.snapshot import SnapshotSchema
from services.auction_engine.engine import AuctionEngine
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

class AuctionEngineTests(unittest.TestCase):
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
            result.fresh_direction.model_dump(mode="json"),
            result.directional.model_dump(mode="json"),
            result.balance.model_dump(mode="json"),
            tuple(item.model_dump(mode="json") for item in result.events),
            tuple(item.model_dump(mode="json") for item in result.permissions),
        )

    def test_incremental_memory_matches_continuous_engine(self) -> None:
        config = _config()
        rows = self._rows()
        continuous = AuctionEngine(config)
        carried = None
        history: list[SnapshotSchema] = []
        for row in rows:
            expected = continuous.evaluate_snapshot(row)
            incremental = AuctionEngine(config)
            if carried is not None:
                incremental.restore_incremental_state(
                    "TEST",
                    carried,
                    history_snapshots=history,
                )
            actual = incremental.evaluate_snapshot(row)
            self.assertEqual(self._signature(expected), self._signature(actual))
            carried = incremental.export_incremental_state("TEST")
            history.append(row)

    def test_snapshot_adapter_persists_only_current_projection(self) -> None:
        previous = None
        expected_fields = {
            "status",
            "continuity_mode",
            "previous_snapshot_time",
            "evidence",
            "directional",
            "balance",
            "events",
            "permissions",
            "diagnostics",
        }
        with patch.object(
            SnapshotSchema,
            "fetch_recent_today_for_symbol_before_time",
            return_value=[],
        ):
            for row in self._rows():
                current = _finalize(row, previous)
                public = current.auction.model_dump(mode="python")
                self.assertEqual(set(public), expected_fields)
                if previous is None:
                    self.assertEqual(current.auction.continuity_mode, "COLD_START")
                    self.assertIsNone(current.auction.previous_snapshot_time)
                else:
                    self.assertEqual(
                        current.auction.continuity_mode,
                        "INCREMENTAL_PREVIOUS_SNAPSHOT",
                    )
                    self.assertEqual(
                        current.auction.previous_snapshot_time,
                        previous.snapshot_time,
                    )
                for removed in ("engine", "observation", "lifecycle", "state", "decision"):
                    self.assertNotIn(removed, public)
                previous = current

    def test_restore_rejects_wrong_symbol(self) -> None:
        engine = AuctionEngine(_config())
        row = self._rows()[0]
        engine.evaluate_snapshot(row)
        memory = engine.export_incremental_state("TEST")
        corrupt = memory.model_copy(update={"symbol": "OTHER"})
        with self.assertRaisesRegex(ValueError, "symbol mismatch"):
            AuctionEngine(_config()).restore_incremental_state("TEST", corrupt)

    def test_persisted_json_roundtrip_preserves_auction_memory_and_projection(self) -> None:
        import json

        final = _finalize(self._rows()[0])
        persisted = json.loads(json.dumps(final.to_db_dict()))
        restored = SnapshotSchema.from_db_dict(persisted)
        self.assertEqual(restored.memory.auction, final.memory.auction)
        self.assertEqual(
            restored.auction.model_dump(mode="json"),
            final.auction.model_dump(mode="json"),
        )

    def test_removed_legacy_projection_fields_are_rejected(self) -> None:
        final = _finalize(self._rows()[0])
        payload = final.model_dump(mode="python")
        payload["auction"]["lifecycle"] = {}
        with self.assertRaisesRegex(ValidationError, "extra_forbidden"):
            SnapshotSchema.model_validate(payload)

