
#!/usr/bin/env python3
"""
generate_futopt_from_master.py  INSERT-ONLY (index EQ fetched by display)

- Scans instrument master for all F&O underlyings.
- Inserts EQ rows for each underlying:
    * Stocks: symbol=name=equity_ref=<stock symbol>
    * Indices: retrieve instrument DIRECTLY with "NIFTY 50"/"NIFTY BANK"
               and insert EQ as "NIFTY 50"/"NIFTY BANK" (symbol, name, equity_ref).
- Inserts configured front FUT/OPT expiry + next configured expiries.
- Insert-only: duplicates skipped quietly.
- Old convention: no argparse.
"""

import logging
import os
import sys
from datetime import date
from typing import Iterable, Optional, Dict, List, Tuple

# allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from logconfig import setup_logging
from schemas.symbol import SymbolSchema
from schemas.instrument import InstrumentSchema
from utils.datetime_utils import current_fo_expiry, fo_load_expiry_count

logger: Optional[logging.Logger] = None

# Underlying (instrument.name) -> EQ display to write to `symbols`
UNDERLYING_TO_DISPLAY = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK"}

DEFAULT_FLAGS = {
    "generate_candles":   False,
    "merge_candles":      False,
    "update_performance": False,
    "generate_signals":   False,
    "processed":          False,
    "active":             True,
    "enabled":            True,
}

def _norm(s: str) -> str:
    return (s or "").strip().upper()

def _display_for_underlying(underlying: str) -> str:
    return UNDERLYING_TO_DISPLAY.get(_norm(underlying), _norm(underlying))

def _as_date(value):
    """Normalize DB expiry values to date for reliable comparisons.

    MySQL may return DATE columns as date and DATETIME columns as datetime.
    The F&O config uses date, so compare date-to-date.
    """
    if value is None:
        return None
    if hasattr(value, "date"):
        return value.date()
    return value

def _symbol_type_for_inst(inst) -> str:
    """Map broker instrument types to the symbols.type values used by runtime."""
    itype = _norm(getattr(inst, "instrument_type", ""))
    if itype in ("FUT", "FUTIDX", "FUTSTK"):
        return "FUT"
    if itype in ("CE", "PE"):
        return itype
    return itype

def _build_master_caches() -> Tuple[List[InstrumentSchema], Dict[str, List[InstrumentSchema]], Dict[str, InstrumentSchema]]:
    all_insts = InstrumentSchema.fetch_instruments() or []
    by_underlying: Dict[str, List[InstrumentSchema]] = {}
    equity_by_symbol: Dict[str, InstrumentSchema] = {}

    for inst in all_insts:
        name = _norm(getattr(inst, "name", None))
        if name:
            by_underlying.setdefault(name, []).append(inst)

        seg = _norm(getattr(inst, "segment", ""))            # "NSE", "BSE", "NFO-OPT", etc.
        itype = _norm(getattr(inst, "instrument_type", ""))  # "EQ","FUT","FUTIDX","FUTSTK","CE","PE"...
        tsym = _norm(getattr(inst, "tradingsymbol", ""))
        if tsym and (seg in ("NSE", "BSE") or itype == "EQ"):
            equity_by_symbol.setdefault(tsym, inst)

    return all_insts, by_underlying, equity_by_symbol

def _iter_fo_underlyings(by_underlying: Dict[str, List[InstrumentSchema]]) -> Iterable[str]:
    for name, items in by_underlying.items():
        has_fo = any(_norm(getattr(x, "instrument_type", "")) in ("FUT", "FUTIDX", "FUTSTK", "CE", "PE") for x in items)
        if has_fo:
            yield name

# -------------------------- INSERT HELPERS ----------------------------
def _insert(payload: Dict) -> bool:
    try:
        created = SymbolSchema.create_symbol(payload)
        return bool(created)
    except Exception as e:
        msg = str(e).lower()
        if "duplicate" in msg or "unique" in msg or "integrity" in msg:
            logger.debug("Skip existing: %s", payload.get("symbol"))
            return False
        logger.exception("Create failed for %s", payload.get("symbol"))
        return False

def _insert_eq(eq_display: str, equity_inst: Optional[InstrumentSchema]) -> None:
    display = _norm(eq_display)
    symbol_val = display
    # For indices, force display for name; for stocks, use the instrument's name
    name_val   = display if display in ("NIFTY 50", "NIFTY BANK") else getattr(equity_inst, "name", display)

    payload = {
        "symbol":        symbol_val,
        "token":         getattr(equity_inst, "instrument_token", None),
        "name":          name_val,
        "type":          "EQ",
        "price":         getattr(equity_inst, "last_price", None),
        "exchange":      getattr(equity_inst, "exchange", "NSE"),
        "segment":       getattr(equity_inst, "segment", "NSE"),
        "signal_profile":      "MOMENTUM",
        "lotsize":       getattr(equity_inst, "lot_size", 1) or 1,
        "expiry":        None,
        "strike_price":  None,
        "tick_size":     getattr(equity_inst, "tick_size", None),
        "equity_ref":    display,   # always display for EQ row
        "last_time":     None,
        "last_snapshot": None,
        **DEFAULT_FLAGS,
    }
    if _insert(payload):
        logger.info("  + EQ    %s (token=%s)", display, str(getattr(equity_inst, "instrument_token", None)))

def _insert_from_inst(inst, equity_ref_display: str) -> None:
    itype = _norm(getattr(inst, "instrument_type", ""))
    signal_profile = "MOMENTUM"
    payload = {
        "symbol":        inst.tradingsymbol,
        "token":         inst.instrument_token,
        "name":          inst.name,
        "type":          _symbol_type_for_inst(inst),
        "price":         getattr(inst, "last_price", None),
        "exchange":      inst.exchange,
        "segment":       inst.segment,
        "signal_profile":      signal_profile,
        "lotsize":       getattr(inst, "lot_size", 1) or 1,
        "expiry":        getattr(inst, "expiry", None),
        "strike_price":  getattr(inst, "strike", None),
        "tick_size":     getattr(inst, "tick_size", None),
        "equity_ref":    _norm(equity_ref_display),
        "last_time":     None,
        "last_snapshot": None,
        **DEFAULT_FLAGS,
    }
    if _insert(payload):
        exp = getattr(inst, "expiry", None)
        strike = getattr(inst, "strike", None)
        if itype in ("FUT", "FUTIDX", "FUTSTK"):
            logger.info("  + FUT   %s  exp=%s", inst.tradingsymbol, str(exp))
        elif itype in ("CE", "PE"):
            logger.info("  + OPT   %s  exp=%s  strike=%s", inst.tradingsymbol, str(exp), str(strike))

def generate_futopt_from_master():
    today = date.today()
    _all, by_underlying, equity_by_symbol = _build_master_caches()

    underlyings = sorted(set(_iter_fo_underlyings(by_underlying)))
    logger.info("Found %d F&O underlyings in master", len(underlyings))

    for underlying in underlyings:
        eq_display = _display_for_underlying(underlying)
        # Minimal change you requested:
        # For index EQ rows, retrieve the instrument DIRECTLY with "NIFTY 50" / "NIFTY BANK".
        if eq_display in ("NIFTY 50", "NIFTY BANK"):
            equity_inst = InstrumentSchema.fetch_instrument(eq_display)
        else:
            equity_inst = equity_by_symbol.get(_norm(underlying))

        logger.info("UNDERLYING=%-12s  EQ=%-12s", underlying, eq_display)

        # 1) EQ (insert-only)
        _insert_eq(eq_display, equity_inst)

        # 2) FUT/OPT expiries. Load configured front expiry + next N-1 expiries.
        # Runtime selects the configured front expiry via current_fo_expiry().
        front_expiry = current_fo_expiry()
        expiry_count = fo_load_expiry_count()
        insts = by_underlying.get(_norm(underlying), []) or []

        futs = [
            x for x in insts
            if _norm(getattr(x, "instrument_type", "")) in ("FUT", "FUTIDX", "FUTSTK")
            and _as_date(getattr(x, "expiry", None)) is not None
            and _as_date(getattr(x, "expiry", None)) >= front_expiry
        ]
        futs.sort(key=lambda x: (_as_date(getattr(x, "expiry", None)) or date.max))
        selected_futs = futs[:expiry_count]

        if not selected_futs:
            logger.debug("  ! No FUT expiry >= %s for %s", front_expiry, underlying)
            continue

        for fut in selected_futs:
            expiry = _as_date(getattr(fut, "expiry", None))
            _insert_from_inst(fut, eq_display)

            # 3) Same-expiry CE/PE
            added = 0
            for inst in insts:
                itype = _norm(getattr(inst, "instrument_type", ""))
                if itype in ("CE", "PE") and _as_date(getattr(inst, "expiry", None)) == expiry:
                    _insert_from_inst(inst, eq_display)
                    added += 1
            logger.info("  + OPTs  expiry=%s count=%d", expiry, added)

def main():
    global logger
    setup_logging(log_file="generate_futopt.log")
    logger = logging.getLogger(__name__)
    logger.info("Starting FUT/OPT+EQ generation from instrument master")
    generate_futopt_from_master()
    logger.info("Done")

if __name__ == "__main__":
    main()
