import os
import sys
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from pprint import pformat

# allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from logconfig import setup_logging
setup_logging(log_file="test_derivatives_kite.log")
logger = logging.getLogger(__name__)

from configs.derivatives_config import DERIVATIVES_CONFIG
from schemas.symbol import SymbolSchema
from services.derivatives.derivatives_generator import DerivativesGenerator
from services.derivatives.derivatives_helper import build_derived_from_day

IST = ZoneInfo("Asia/Kolkata")


# -------------------------------------------------------------------
# HARD-CODED TEST SETTINGS
# -------------------------------------------------------------------
# Configuration
TOKEN        = 2815745
API_KEY      = "bv185n0541aaoish"
ACCESS_TOKEN = "5Y8RLgQsikDAFbsNjEoNsCcRC3XvwWoB"
TEST_SYMBOL = "MARUTI"

def _now_minute_aware_ist() -> datetime:
    return datetime.now(IST).replace(second=0, microsecond=0)


def _safe_len(x) -> int:
    try:
        return len(x or {})
    except Exception:
        return 0


def _sample_option_rows(raw_options: dict, n: int = 8) -> dict:
    """Return a small sample for display so logs are readable."""
    out = {}
    for i, (k, v) in enumerate((raw_options or {}).items()):
        if i >= n:
            break
        out[k] = {
            "instrument": (v or {}).get("instrument"),
            "last_price": (v or {}).get("last_price"),
            "oi": (v or {}).get("oi"),
            "volume": (v or {}).get("volume"),
            "quote_time": (v or {}).get("quote_time"),
        }
    return out


def main():
    if not API_KEY or not ACCESS_TOKEN:
        logger.error(
            "Missing Zerodha credentials. Set ZERODHA_API_KEY and ZERODHA_ACCESS_TOKEN, "
            "or hardcode API_KEY / ACCESS_TOKEN in this test file."
        )
        print("DERIVATIVES_KITE_TEST_DONE | status=MISSING_CREDENTIALS")
        return

    symbol = TEST_SYMBOL.strip().upper()
    asof_aware = _now_minute_aware_ist()

    logger.info("DERIV_KITE_TEST_START | symbol=%s asof=%s", symbol, asof_aware.isoformat())

    # This uses symbol master only. No derivativeschain read/write is performed.
    sym = SymbolSchema.fetch_symbol(symbol)
    logger.info("SYMBOL_LOOKUP | symbol=%s found=%s", symbol, bool(sym))

    if not sym:
        logger.error("Symbol %s not found in SymbolSchema.", symbol)
        print("DERIVATIVES_KITE_TEST_DONE | status=NO_SYMBOL")
        return

    logger.info(
        "SYMBOL_DETAILS | symbol=%s type=%s equity_ref=%s exchange=%s",
        getattr(sym, "symbol", None),
        getattr(sym, "type", None),
        getattr(sym, "equity_ref", None),
        getattr(sym, "exchange", None),
    )

    gen = DerivativesGenerator(api_key=API_KEY, access_token=ACCESS_TOKEN)

    # Use generator resolution and quote methods, but avoid gen.generate()
    # because generate() persists by design.
    ts_aware, ts_naive = gen._minute_from(asof_aware)

    chain_symbol, spot_sym, fut, opts = gen._resolve_chain_inputs(symbol, ts_naive)

    logger.info(
        "CHAIN_RESOLUTION | chain_symbol=%s spot=%s fut=%s options=%s",
        chain_symbol,
        getattr(spot_sym, "symbol", None) if spot_sym else None,
        getattr(fut, "symbol", None) if fut else None,
        len(opts or []),
    )

    if not chain_symbol or not spot_sym or not fut or not opts:
        logger.error(
            "Cannot continue: chain inputs missing | chain_symbol=%s spot=%s fut=%s opts=%s",
            chain_symbol,
            bool(spot_sym),
            bool(fut),
            len(opts or []),
        )
        print("DERIVATIVES_KITE_TEST_DONE | status=CHAIN_INPUTS_MISSING")
        return

    logger.info(
        "FUTURE_DETAILS | symbol=%s expiry=%s exchange=%s",
        getattr(fut, "symbol", None),
        getattr(fut, "expiry", None),
        getattr(fut, "exchange", None),
    )

    t0 = time.perf_counter()
    spot_price, raw_future, raw_options = gen._fetch_all_quotes_once(
        spot_sym=spot_sym,
        fut=fut,
        opts=opts,
        ts_aware=ts_aware,
    )
    took = time.perf_counter() - t0

    raw_payload = {
        "spot_price": spot_price,
        "future": raw_future,
        "options": raw_options,
    }

    logger.info(
        "QUOTE_FETCH_OK | symbol=%s spot=%s fut_quote_time=%s option_count=%s took=%.3fs",
        chain_symbol,
        spot_price,
        (raw_future or {}).get("quote_time"),
        _safe_len(raw_options),
        took,
    )

    logger.info("FUTURE_RAW | %s", pformat(raw_future))
    logger.info("OPTIONS_SAMPLE | %s", pformat(_sample_option_rows(raw_options, n=8)))

    # Build derived using current live raw only. No DB history is read.
    # Sentiment windows may be neutral/insufficient because there is only one sample.
    derived_cfg = DERIVATIVES_CONFIG.derived
    os_cfg = derived_cfg.option_sentiment
    ladder_cfg = derived_cfg.option_ladder
    lite_cfg = derived_cfg.options_lite

    try:
        derived = build_derived_from_day(
            samples=[{"snapshot_time": ts_naive, "raw": raw_payload}],
            asof=ts_naive,
            windows=os_cfg.windows,
            ladder_window=ladder_cfg.window,
            opt_sent_atm_window=os_cfg.atm_window,
            opt_sent_notional_floor=os_cfg.notional_floor,
            opt_sent_min_contracts_floor=os_cfg.min_contracts_floor,
            top_n=lite_cfg.top_n,
        )
    except Exception:
        logger.exception("DERIVED_BUILD_FAILED")
        print("DERIVATIVES_KITE_TEST_DONE | status=DERIVED_FAILED")
        return

    logger.info("DERIVED_KEYS | %s", sorted((derived or {}).keys()))
    logger.info("OPTIONS_LITE | %s", pformat((derived or {}).get("options_lite")))
    logger.info("OPTION_LADDER_SAMPLE | %s", pformat((derived or {}).get("option_ladder")))
    logger.info("OPTION_SENTIMENT_WINDOWS | %s", pformat((derived or {}).get("option_sentiment_windows")))
    logger.info("FUTURE_SENTIMENT_WINDOWS | %s", pformat((derived or {}).get("future_sentiment_windows")))

    print("DERIVATIVES_KITE_TEST_DONE | status=OK")


if __name__ == "__main__":
    main()
