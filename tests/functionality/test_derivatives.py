#!/usr/bin/env python3
"""
tests/test_derivatives.py

Safe derivatives smoke test for the config refactor.

This test intentionally does NOT:
- call Zerodha
- persist derivatives rows
- start any service loop

It validates:
- typed DERIVATIVES_CONFIG import
- quote timestamp normalization
- pure helper derived payload generation
- derivatives lifecycle summary interpretation
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Dict, Any, List

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from logconfig import setup_logging
except Exception:  # fallback for very early import failures
    setup_logging = None

from configs.derivatives_config import DERIVATIVES_CONFIG
from services.derivatives.derivatives_generator import normalize_quote_time
from services.derivatives.derivatives_helper import (
    compute_options_lite,
    compute_option_ladder,
    build_derived_from_day,
)
from services.lifecycle.derivatives_lifecycle_helper import evaluate_derivatives_for_lifecycle


logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


def _option(symbol: str, strike: int, kind: str, oi: int, ltp: float, volume: int = 1000) -> Dict[str, Any]:
    return {
        "instrument": symbol,
        "symbol": symbol,
        "exchange": "NFO",
        "expiry": "2026-06-25",
        "last_price": ltp,
        "oi": oi,
        "volume": volume,
        "ohlc": {"open": ltp, "high": ltp + 1, "low": max(0, ltp - 1), "close": ltp},
        "quote_time": "2026-06-04T10:30:00+05:30",
    }


def _raw_payload(*, spot: float, ce_bias_add: int = 0, pe_bias_add: int = 0, fut_oi: int = 100000, fut_ltp: float = 1000.0) -> Dict[str, Any]:
    """Create synthetic raw derivatives payload in derivativeschain_v2 RAW shape."""
    strikes = [980, 990, 1000, 1010, 1020]
    options: Dict[str, Any] = {}
    for strike in strikes:
        # Add asymmetric OI only near ATM to create meaningful option sentiment.
        ce_oi = 1000 + (ce_bias_add if strike in (1000, 1010) else 0)
        pe_oi = 1000 + (pe_bias_add if strike in (990, 1000) else 0)
        options[f"{strike}_CE"] = _option(f"TEST{strike}CE", strike, "CE", ce_oi, max(1.0, (spot - strike) * 0.10 + 20))
        options[f"{strike}_PE"] = _option(f"TEST{strike}PE", strike, "PE", pe_oi, max(1.0, (strike - spot) * 0.10 + 20))

    return {
        "spot_price": spot,
        "future": {
            "instrument": "TESTFUT",
            "exchange": "NFO",
            "expiry": "2026-06-25",
            "last_price": fut_ltp,
            "oi": fut_oi,
            "volume": 50000,
            "ohlc": {"open": fut_ltp - 2, "high": fut_ltp + 5, "low": fut_ltp - 5, "close": fut_ltp - 1},
            "quote_time": "2026-06-04T10:30:00+05:30",
        },
        "options": options,
    }


def _sample(ts: datetime, raw: Dict[str, Any]) -> Dict[str, Any]:
    return {"snapshot_time": ts.replace(tzinfo=None), "raw": raw}


def test_config_import() -> None:
    cfg = DERIVATIVES_CONFIG
    assert cfg.service.window_start == "09:16:00"
    assert cfg.derived.option_ladder.window > 0
    assert "15m" in cfg.derived.option_sentiment.windows
    assert cfg.lifecycle.option_weight + cfg.lifecycle.future_weight > 0
    logger.info("CONFIG_OK | derivatives config loaded")


def test_normalize_quote_time() -> None:
    target = date(2026, 6, 4)

    valid = normalize_quote_time(datetime(2026, 6, 4, 10, 30, tzinfo=IST), target)
    assert valid is not None and valid.startswith("2026-06-04T10:30")

    stale = normalize_quote_time(datetime(2026, 6, 3, 10, 30, tzinfo=IST), target)
    assert stale is None

    logger.info("QUOTE_TIME_OK | valid=%s stale=%s", valid, stale)


def test_pure_derivatives_helpers() -> Dict[str, Any]:
    asof = datetime(2026, 6, 4, 10, 30)
    base_ts = asof - timedelta(minutes=15)

    # Base: neutral-ish. Now: PE OI added and future price+OI rising => bullish confirmation.
    raw_base = _raw_payload(spot=1000.0, ce_bias_add=0, pe_bias_add=0, fut_oi=100000, fut_ltp=1000.0)
    raw_now = _raw_payload(spot=1008.0, ce_bias_add=200, pe_bias_add=1200, fut_oi=108000, fut_ltp=1010.0)

    options_lite = compute_options_lite(raw_now)
    assert options_lite is not None
    assert options_lite["atm_strike"] == 1010.0 or options_lite["atm_strike"] == 1000.0

    ladder = compute_option_ladder(raw_now, raw_base)
    assert ladder is not None
    assert ladder["calls"] and ladder["puts"]

    samples: List[Dict[str, Any]] = [
        _sample(base_ts, raw_base),
        _sample(asof, raw_now),
    ]
    derived = build_derived_from_day(samples=samples, asof=asof)

    assert derived.get("options_lite") is not None
    assert derived.get("option_ladder") is not None
    assert derived.get("option_sentiment_windows")
    assert derived.get("future_sentiment_windows")

    logger.info("DERIVED_OK | keys=%s", sorted(derived.keys()))
    logger.info("OPTIONS_LITE | %s", derived.get("options_lite"))
    logger.info("OPTION_15M | %s", (derived.get("option_sentiment_windows") or {}).get("15m"))
    logger.info("FUTURE_15M | %s", (derived.get("future_sentiment_windows") or {}).get("15m"))

    return derived


def test_lifecycle_derivatives_summary(derived: Dict[str, Any]) -> None:
    ctx = evaluate_derivatives_for_lifecycle(
        derivatives=derived,
        side="BUY",
        evidence_mode="MOMENTUM",
    )
    d = ctx.to_dict()

    assert d["side"] == "BUY"
    assert d["confirmation"] in {"CONFIRMED", "WEAK_CONFIRM", "NEUTRAL", "CONFLICT", "VETO"}
    assert 0 <= int(d["score"]) <= 100

    logger.info("LIFECYCLE_DERIV_OK | %s", d)


def main() -> None:
    if setup_logging:
        setup_logging(log_file="test_derivatives.log")
    logging.getLogger().setLevel(logging.INFO)

    test_config_import()
    test_normalize_quote_time()
    derived = test_pure_derivatives_helpers()
    test_lifecycle_derivatives_summary(derived)

    logger.info("DERIVATIVES_TEST_DONE | status=OK")
    print("DERIVATIVES_TEST_DONE | status=OK")


if __name__ == "__main__":
    main()
