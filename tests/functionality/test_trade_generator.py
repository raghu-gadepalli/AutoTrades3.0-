#!/usr/bin/env python3
import os
import sys
import logging
from datetime import datetime

# allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from logconfig import setup_logging
setup_logging(log_file="trade_generator.log")
logger = logging.getLogger(__name__)

from services.trade.generator.trade_generator import TradeGenerator
from schemas.signal import SignalSchema

TEST_USERID = "DR1812"


def _to_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return float(default)


def _enum_value(v):
    try:
        return getattr(v, "value", v)
    except Exception:
        return v


def _lifecycle_meta(signal):
    meta = getattr(signal, "meta_json", None) or {}
    if not isinstance(meta, dict):
        return {}
    lifecycle = meta.get("lifecycle") or {}
    return lifecycle if isinstance(lifecycle, dict) else {}


def _print_open_opportunities():
    try:
        rows = SignalSchema.list_for_ui(statuses=["OPEN"], limit=50) or []
    except Exception:
        logger.exception("Failed to fetch open opportunities")
        return

    logger.info("Open opportunities before trade generation: %d", len(rows))

    for s in rows:
        lc = _lifecycle_meta(s)

        logger.info(
            "Opportunity | signal_id=%s symbol=%s side=%s stage=%s status=%s "
            "signal_action=%s trade_action=%s confidence=%s quality=%s veto=%s",
            getattr(s, "signal_id", ""),
            getattr(s, "symbol", ""),
            _enum_value(getattr(s, "side", "")),
            _enum_value(getattr(s, "stage", "")),
            _enum_value(getattr(s, "status", "")),
            lc.get("signal_action"),
            lc.get("trade_action"),
            lc.get("confidence"),
            lc.get("quality"),
            lc.get("combined_veto"),
        )


def main():
    logger.info("=== TRADE GENERATION lifecycle test for userid=%s ===", TEST_USERID)

    _print_open_opportunities()

    tg = TradeGenerator()

    start = datetime.now()
    trades = tg.generate_user_trades(TEST_USERID)
    duration = (datetime.now() - start).total_seconds()

    logger.info("Trade generation took %.3f sec", duration)

    trades = trades or []
    logger.info("Total rows created: %d", len(trades))

    for ut in trades:
        _id = getattr(ut, "id", "")
        _oppid = getattr(ut, "signal_id", "")
        _userid = getattr(ut, "userid", "")
        _sym = getattr(ut, "symbol", "")
        _eqref = getattr(ut, "equity_ref", "")

        _inst = _enum_value(getattr(ut, "instrument_type", ""))
        _side = _enum_value(getattr(ut, "trade_type", ""))

        _mode = getattr(ut, "execution_mode", "")
        _estatus = _enum_value(getattr(ut, "entry_status", ""))
        _xstatus = _enum_value(getattr(ut, "exit_status", ""))

        _entry = getattr(ut, "entry_price", 0)
        _qty = getattr(ut, "quantity", 0)

        tm = getattr(ut, "trade_management", None) or {}
        _sl = tm.get("current_stop_price") or tm.get("initial_stop_price") or 0
        _tgt = tm.get("current_target_price") or tm.get("initial_target_price") or 0

        logger.info(
            "[%s] user=%s signal=%s %s(%s) %s %s mode=%s entry_status=%s exit_status=%s "
            "@%.2f qty=%s stop=%s target=%s",
            _id,
            _userid,
            _oppid,
            _sym,
            _eqref,
            str(_inst),
            str(_side),
            str(_mode),
            str(_estatus),
            str(_xstatus),
            _to_float(_entry),
            str(_qty or 0),
            _to_float(_sl),
            _to_float(_t1),
            _to_float(_t2),
            _to_float(_t3),
        )

    logger.info("=== TRADE GENERATION complete ===")
    return trades


if __name__ == "__main__":
    logger.info("Starting test_trade_generator")
    main()
    logger.info("Finished test_trade_generator")
