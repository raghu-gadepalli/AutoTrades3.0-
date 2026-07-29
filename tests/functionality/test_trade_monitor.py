#!/usr/bin/env python3
"""
Test harness for setup-aware TradeMonitor.

Purpose:
- Run TradeMonitor against a controlled userid / optional symbols.
- Avoid touching every user's open trades during local testing.
- Print BEFORE / AFTER open positions and recent exit-ready/closed rows.

Edit only the constants below before running.
"""

import os
import sys
import logging
from decimal import Decimal
from typing import Iterable, Optional

# allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from logconfig import setup_logging
setup_logging(log_file="trade_monitor_test.log")
logger = logging.getLogger(__name__)

from database.database import get_trades_db
from models.trade_models import UserTrade as UserTradeORM
from services.trade.monitor.trade_monitor import TradeMonitor
from schemas.user_trade import UserTradeSchema
from enums.enums import EntryStatus, ExitStatus


# -----------------------------------------------------------------------------
# Hardcoded test controls - no CLI args by design.
# -----------------------------------------------------------------------------
TEST_USERID = "VZS807"          # change to DR1812 if needed
SYMBOLS: list[str] = []          # e.g. ["AXISBANK", "NUVAMA26JUNFUT"] ; [] = all for user
TRADE_IDS: list[int] = []        # e.g. [905, 2419] ; [] = no id filter
RESET_BEFORE_RUN = False         # keep False for live/replay output review
APPLY_DB_UPDATES = True          # monitor() writes by design; keep True for realistic test


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _enum_value(v):
    try:
        return getattr(v, "value", v)
    except Exception:
        return v


def _to_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return float(default)


def _upper_set(values: Iterable[str]) -> set[str]:
    return {str(v or "").strip().upper() for v in values if str(v or "").strip()}


def _fmt_dt(v):
    try:
        return v.strftime("%d-%m-%Y %H:%M:%S") if v else ""
    except Exception:
        return str(v or "")


def _base_query(db):
    q = db.query(UserTradeORM).filter(UserTradeORM.userid == TEST_USERID)

    syms = _upper_set(SYMBOLS)
    if syms:
        q = q.filter(UserTradeORM.symbol.in_(syms))

    if TRADE_IDS:
        q = q.filter(UserTradeORM.id.in_([int(x) for x in TRADE_IDS]))

    return q


def _is_open_monitor_row(ut) -> bool:
    return (
        str(getattr(ut, "entry_status", "")).upper() == EntryStatus.FILLED.value
        and str(getattr(ut, "exit_status", "")).upper() != ExitStatus.FILLED.value
    )


def reset_monitor_fields():
    """
    Reset only monitor-managed fields for selected currently-open trades.
    Does not touch identity/order entry fields.
    """
    with get_trades_db() as db:
        rows = (
            _base_query(db)
            .filter(UserTradeORM.entry_status == EntryStatus.FILLED.value)
            .filter(UserTradeORM.exit_status != ExitStatus.FILLED.value)
            .order_by(UserTradeORM.id.asc())
            .all()
        )

        count = 0
        for ut in rows:
            entry_basis = getattr(ut, "executed_entry_price", None)
            if entry_basis is None or Decimal(str(entry_basis or 0)) <= 0:
                entry_basis = getattr(ut, "entry_price", None)
            if entry_basis is None or Decimal(str(entry_basis or 0)) <= 0:
                logger.warning("Skip reset trade_id=%s; missing entry basis", getattr(ut, "id", None))
                continue

            base_time = getattr(ut, "entry_exec_time", None) or getattr(ut, "entry_time", None)

            ut.last_time = base_time
            ut.last_price = entry_basis
            ut.last_pnl = Decimal("0")
            ut.last_pnl_value = Decimal("0")

            ut.max_price = entry_basis
            ut.min_price = entry_basis
            ut.max_time = base_time
            ut.min_time = base_time


            if getattr(ut, "exit_status", None) in (ExitStatus.READY.value, ExitStatus.SUBMITTED.value):
                ut.exit_status = ExitStatus.NONE.value

            ut.exit_time = None
            ut.exit_price = None
            ut.exit_pnl = None
            ut.exit_intent_time = None
            ut.exit_reason = None
            ut.exit_rule = None

            count += 1

        db.commit()

    logger.info("Reset monitor-managed fields | count=%d userid=%s symbols=%s trade_ids=%s", count, TEST_USERID, SYMBOLS, TRADE_IDS)
    return count


def _print_trade_row(prefix: str, ut):
    logger.info(
        "%s [%s] signal=%s %s(%s) %s %s mode=%s "
        "entry=%0.2f exec_entry=%0.2f qty=%s open_qty=%s "
        "last=%0.2f pnl=%0.2f pnl_value=%0.2f max=%0.2f min=%0.2f "
        "stop=%s target=%s "
        "entry_status=%s exit_status=%s exit_reason=%s exit_rule=%s "
        "entry_time=%s last_time=%s exit_time=%s",
        prefix,
        getattr(ut, "id", ""),
        getattr(ut, "signal_id", ""),
        getattr(ut, "symbol", ""),
        getattr(ut, "equity_ref", ""),
        _enum_value(getattr(ut, "instrument_type", "")),
        _enum_value(getattr(ut, "trade_type", "")),
        getattr(ut, "execution_mode", ""),
        _to_float(getattr(ut, "entry_price", 0)),
        _to_float(getattr(ut, "executed_entry_price", 0)),
        getattr(ut, "quantity", 0),
        max(int(getattr(ut, "quantity", 0) or 0) - int(getattr(ut, "executed_exit_qty", 0) or 0), 0),
        _to_float(getattr(ut, "last_price", 0)),
        _to_float(getattr(ut, "last_pnl", 0)),
        _to_float(getattr(ut, "last_pnl_value", 0)),
        _to_float(getattr(ut, "max_price", 0)),
        _to_float(getattr(ut, "min_price", 0)),
        str((getattr(ut, "trade_management", None) or {}).get("current_stop_price") or ""),
        str((getattr(ut, "trade_management", None) or {}).get("current_target_price") or ""),
        _enum_value(getattr(ut, "entry_status", "")),
        _enum_value(getattr(ut, "exit_status", "")),
        getattr(ut, "exit_reason", None),
        getattr(ut, "exit_rule", None),
        _fmt_dt(getattr(ut, "entry_time", None)),
        _fmt_dt(getattr(ut, "last_time", None)),
        _fmt_dt(getattr(ut, "exit_time", None)),
    )


def print_open_positions(label):
    with get_trades_db() as db:
        rows = (
            _base_query(db)
            .filter(UserTradeORM.entry_status == EntryStatus.FILLED.value)
            .filter(UserTradeORM.exit_status != ExitStatus.FILLED.value)
            .order_by(UserTradeORM.id.asc())
            .all()
        )

    logger.info("=== %s | monitor-open positions=%d userid=%s symbols=%s trade_ids=%s ===", label, len(rows), TEST_USERID, SYMBOLS, TRADE_IDS)
    for ut in rows:
        _print_trade_row(label, ut)


def print_recent_exits(label, limit: int = 30):
    with get_trades_db() as db:
        rows = (
            _base_query(db)
            .filter(UserTradeORM.entry_status == EntryStatus.FILLED.value)
            .filter(UserTradeORM.exit_status == ExitStatus.FILLED.value)
            .order_by(UserTradeORM.exit_time.desc(), UserTradeORM.id.desc())
            .limit(limit)
            .all()
        )

    logger.info("=== %s | recent filled exits=%d userid=%s ===", label, len(rows), TEST_USERID)
    for ut in rows:
        _print_trade_row(label, ut)


def patch_fetch_open_positions_for_test():
    """
    Restrict TradeMonitor.monitor() to this test user/symbol/trade set.
    This avoids accidentally monitoring every open trade while testing.
    """
    original_fetch = UserTradeSchema.fetch_open_positions

    def patched_fetch_open_positions(*, userid: Optional[str] = None, symbol: Optional[str] = None):
        rows = original_fetch(userid=TEST_USERID, symbol=symbol)

        syms = _upper_set(SYMBOLS)
        ids = {int(x) for x in TRADE_IDS}

        out = []
        for row in rows:
            row_symbol = str(getattr(row, "symbol", "") or "").upper()
            row_id = int(getattr(row, "id", 0) or 0)
            if syms and row_symbol not in syms:
                continue
            if ids and row_id not in ids:
                continue
            out.append(row)

        return out

    UserTradeSchema.fetch_open_positions = staticmethod(patched_fetch_open_positions)
    return original_fetch


def restore_fetch_open_positions(original_fetch):
    UserTradeSchema.fetch_open_positions = staticmethod(original_fetch)


def main():
    logger.info(
        "=== TEST TRADE MONITOR START | userid=%s reset=%s symbols=%s trade_ids=%s APPLY_DB_UPDATES=%s ===",
        TEST_USERID,
        RESET_BEFORE_RUN,
        SYMBOLS,
        TRADE_IDS,
        APPLY_DB_UPDATES,
    )

    if RESET_BEFORE_RUN:
        reset_monitor_fields()

    print_open_positions("BEFORE")
    print_recent_exits("BEFORE_RECENT_EXITS", limit=10)

    original_fetch = patch_fetch_open_positions_for_test()
    try:
        updated = TradeMonitor().monitor()
    finally:
        restore_fetch_open_positions(original_fetch)

    logger.info("TradeMonitor returned updated=%s", updated)

    print_open_positions("AFTER")
    print_recent_exits("AFTER_RECENT_EXITS", limit=20)

    logger.info("=== TEST TRADE MONITOR COMPLETE | updated=%s ===", updated)
    return updated


if __name__ == "__main__":
    main()
