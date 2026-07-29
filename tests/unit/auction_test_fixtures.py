"""Reusable strict-snapshot test fixtures for current Auction tests.

This module is a fixture module, not a legacy phase test. Raw construction is
converted through ``tests.test_auction_engine_snapshot_state._strict_snapshot``
before it reaches the strict AuctionEngine boundary.
"""
from __future__ import annotations

from datetime import datetime

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG, AuctionEngineConfig


def _test_config() -> AuctionEngineConfig:
    payload = AUCTION_ENGINE_CONFIG.resolved_dict()
    payload["evidence"]["minimum_history_bars"] = 1
    payload["evidence"]["extension_min_history_bars_for_maturity"] = 2
    return AuctionEngineConfig.model_validate(payload)


def _snapshot(
    ts: datetime,
    *,
    open_price: float,
    high: float,
    low: float,
    close: float,
    atr: float = 1.0,
    hma_state: str = "NO_TREND",
    hma_strength: str = "NA",
    vwap_side: str = "UNKNOWN",
    vwap_distance_atr: float = 0.0,
    rsi: float = 50.0,
    bb_position: float = 0.5,
    move_15m: float = 0.0,
    move_30m: float = 0.0,
    move_sod: float = 0.0,
    raw_state: str = "RANGE",
    raw_side: str = "NEUTRAL",
    range_type: str = "BALANCE",
    range_low: float = 99.0,
    range_high: float = 101.0,
    range_width_atr: float = 2.0,
    efficiency: float = 0.20,
    overlap: float = 0.75,
    flip_count: int = 0,
    bars: int = 10,
) -> dict:
    return {
        "symbol": "TEST",
        "snapshot_time": ts,
        "tf": "3m",
        "close": close,
        "bar": {
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": 10000,
        },
        "levels": {
            "prev_day": {"open": 98.0, "high": 105.0, "low": 95.0, "close": 99.0},
            "today": {"open": 100.0},
            "opening_range": {"ready": True, "high": 101.0, "low": 99.0},
        },
        "indicators": {
            "ema": {},
            "hma": {
                "fast": close + (0.20 if hma_state == "UPTREND" else -0.20 if hma_state == "DOWNTREND" else 0.02),
                "mid1": close + (0.10 if hma_state == "UPTREND" else -0.10 if hma_state == "DOWNTREND" else 0.01),
                "mid2": close,
                "slow": close - (0.10 if hma_state == "UPTREND" else -0.10 if hma_state == "DOWNTREND" else 0.01),
                "state": hma_state,
                "strength": hma_strength,
            },
            "vwap": {
                "value": close - vwap_distance_atr if vwap_side == "ABOVE" else close + vwap_distance_atr if vwap_side == "BELOW" else close,
                "side": vwap_side,
                "distance_atr": vwap_distance_atr,
            },
            "rsi": {"value": rsi, "zone": "MID"},
            "adx": {"value": 25.0, "band": "MEDIUM"},
            "atr": {"value": atr, "band": "NORMAL", "pct": 1.0},
            "bollinger": {"position": bb_position, "zone": "MID"},
            "envelopes": {"hma_envelope": 0.1, "ema_envelope": 0.2},
        },
        "volume": {"bar_volume": 10000, "bar_rvol": 1.1, "bar_rvol_band": "NORMAL"},
        "market_windows": {
            "sod": {
                "status": "ok", "bars": bars, "minutes": bars * 3,
                "move_atr": move_sod, "move_pct": move_sod,
                "move_points": move_sod * atr, "range_points": 4.0,
                "close_position_in_range": 0.5,
            },
            "15m": {
                "status": "ok", "bars": 5, "minutes": 15,
                "move_atr": move_15m, "move_points": move_15m * atr,
                "range_points": 2.0, "close_position_in_range": 0.5,
            },
            "30m": {
                "status": "ok", "bars": 10, "minutes": 30,
                "move_atr": move_30m, "move_points": move_30m * atr,
                "range_points": 3.0, "close_position_in_range": 0.5,
            },
        },
        "indicator_windows": {"hma": {}, "vwap": {}, "rsi": {}, "adx": {}, "atr": {}, "bollinger": {}, "volume": {}},
        "price_action": {
            "orb": {"position": "INSIDE"},
            "vwap": {"position": vwap_side},
            "moves": {},
            "slope": {
                "bars_3_atr_per_bar": move_15m / 5.0,
                "bars_5_atr_per_bar": move_15m / 5.0,
                "state": "UP" if move_15m > 0 else "DOWN" if move_15m < 0 else "FLAT",
            },
        },
        "structure": {
            "raw": {
                "state": raw_state,
                "side": raw_side,
                "range": {
                    "range_id": "RANGE-1", "version": 1,
                    "high": range_high, "low": range_low,
                    "width_atr": range_width_atr, "source": "DYNAMIC",
                    "range_type": range_type, "bars": bars,
                    "breakout_eligible": True,
                },
                "metrics": {
                    "directional_efficiency": efficiency,
                    "adjacent_overlap_ratio": overlap,
                    "classification": range_type,
                },
            },
            "accepted": {
                "state": "RANGE_ACCEPTED",
                "range": {
                    "range_id": "RANGE-1", "version": 1,
                    "high": range_high, "low": range_low,
                    "width_atr": range_width_atr, "source": "DYNAMIC",
                    "range_type": range_type, "bars": bars,
                    "breakout_eligible": True,
                },
                "metrics": {
                    "directional_efficiency": efficiency,
                    "adjacent_overlap_ratio": overlap,
                    "classification": range_type,
                },
                "age_bars": bars,
            },
            "candidate": {"active": False, "range": {}, "metrics": {}},
            "recent_closes": [], "anchors": {}, "breakout_context": {},
            "diagnostics": {}, "session_phase": "MID", "count": bars,
            "flip_count_today": flip_count,
        },
        "state_context": {
            "hma": {"flip_count_today": flip_count},
            "hma_strength": {},
            "vwap": {"flip_count_today": flip_count},
            "rsi": {}, "adx": {}, "atr": {}, "bollinger": {}, "volume": {},
            "structure": {"flip_count_today": flip_count},
        },
        "derivatives": {},
    }

