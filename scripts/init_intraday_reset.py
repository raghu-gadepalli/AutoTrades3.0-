#!/usr/bin/env python3
"""
Init-day intraday reset (consistent logging like other runners)

- Archive signals & user_trades to *_history (optional)
- Truncate intraday tables (signals, user_trades, snapshots, candles, derivativeschain)
- Reset daily EQ selection flags (enabled remains the monthly universe gate)
- Activate configured universe.whitelist symbols for the daily snapshot universe
- Reset user logins (logged_in=0, logged_time=NULL)

Config: SERVICE_CONFIG.init_reset
All reset behavior and table scope are explicitly defined by InitResetConfig.
"""

import os
import sys
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

# Project root on PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import text, inspect as sqla_inspect

from logconfig import setup_logging
from configs.service_config import SERVICE_CONFIG
from utils.universe_policy import universe_whitelist
from database.database import get_trades_db

# centralized day gate (holidays/weekends/whitelist/blackout)
from utils.run_control import allow_run_today

# ORM models (import ensures tables are mapped)
from models.trade_models import (
    Base,
    Signal,
    UserTrade,
    Snapshot,
    Candle,
    DerivativesChain,
    User as UserORM,
    AuditLog,
    AuditLogHistory,
)
from schemas.symbol import SymbolSchema

# ---------------------------
# Config
# ---------------------------
TZ = SERVICE_CONFIG.tz
IST = ZoneInfo(TZ)

CONF = SERVICE_CONFIG.init_reset

LOG_FILE = CONF.log_file
ARCHIVE_BEFORE_TRUNCATE = bool(CONF.archive_before_truncate)
RESET_EQ_FLAGS = bool(CONF.reset_eq_flags)
RESET_USER_LOGS = bool(CONF.reset_user_logins)
ARCHIVE_AUDITLOG = bool(CONF.archive_auditlog)
AUTOLOGIN_VIRTUAL_USERS = list(CONF.virtual_autologin_userids)
INTRADAY_TABLES = frozenset(CONF.intraday_tables)

logger: logging.Logger | None = None


# ---------------------------
# Helpers to discover DB/engine per mapped model
# ---------------------------
def _engine_db_for_model(session, model):
    """Return (engine, db_name, Table) for a mapped ORM model via this session."""
    mapper = sqla_inspect(model)
    engine = session.get_bind(mapper=mapper)
    if engine is None or not engine.url.database:
        raise RuntimeError(f"Could not resolve database for model {model.__name__}")
    db_name = engine.url.database
    table = mapper.local_table
    return engine, db_name, table


def _select_intraday_tables(session):
    """Pick only the intraday tables explicitly declared in InitResetConfig."""
    selected = []  # list of (engine, db_name, table_name, fq_name)

    for mapper in Base.registry.mappers:
        table = mapper.local_table
        if table.name not in INTRADAY_TABLES:
            continue

        model_cls = mapper.class_
        engine, db_name, _ = _engine_db_for_model(session, model_cls)
        fq = f"`{db_name}`.`{table.name}`"
        selected.append((engine, db_name, table.name, fq))

    grouped = {}
    for eng, dbname, tname, fq in selected:
        grouped.setdefault((id(eng), eng, dbname), []).append((tname, fq))
    return grouped

# ---------------------------
# Archiving before truncate
# ---------------------------
def archive_intraday_data():
    signals_sql = text(
        """
        INSERT IGNORE INTO signals_history (
          id,
          signal_id,
          equity_ref,
          symbol,
          lifecycle,
          setup,
          side,
          stage,
          status,
          status_reason,
          first_seen_time,
          created_price,
          last_eval_time,
          last_snapshot_time,
          stage_changed_time,
          status_changed_time,
          qualified_time,
          actionable_time,
          closed_time,
          closed_price,
          last_price,
          ltp,
          ltp_time,
          last_pnl,
          last_pnl_value,
          max_price,
          min_price,
          max_time,
          min_time,
          max_pnl,
          min_pnl,
          max_pnl_value,
          min_pnl_value,
          criteria_json,
          snapshot_json,
          meta_json
        )
        SELECT
          id,
          signal_id,
          equity_ref,
          symbol,
          lifecycle,
          setup,
          side,
          stage,
          status,
          status_reason,
          first_seen_time,
          created_price,
          last_eval_time,
          last_snapshot_time,
          stage_changed_time,
          status_changed_time,
          qualified_time,
          actionable_time,
          closed_time,
          closed_price,
          last_price,
          ltp,
          ltp_time,
          last_pnl,
          last_pnl_value,
          max_price,
          min_price,
          max_time,
          min_time,
          max_pnl,
          min_pnl,
          max_pnl_value,
          min_pnl_value,
          criteria_json,
          snapshot_json,
          meta_json
        FROM signals
        """
    )

    user_trades_sql = text(
        """
        INSERT IGNORE INTO user_trades_history (
        id,
        userid,
        signal_id,
        source,
        message,
        entry_snapshot,
        last_snapshot,
        symbol,
        equity_ref,
        instrument_type,
        trade_type,
        position_style,
        hedged_symbol,
        entry_status,
        exit_status,
        execution_mode,
        intraday_only,
        entry_time,
        entry_intent_time,
        entry_exec_time,
        entry_reconciled_at,
        entry_price,
        executed_entry_price,
        executed_entry_qty,
        quantity,
        entry_order_id,
        entry_order_response_json,
        entry_retries,

        trade_management,

        exit_reason,
        exit_rule,
        exit_time,
        exit_intent_time,
        exit_exec_time,
        exit_reconciled_at,
        exit_price,
        executed_exit_price,
        executed_exit_qty,
        exit_order_id,
        exit_order_response_json,
        exit_retries,
        exit_pnl,
        last_time,
        last_price,
        last_pnl,
        last_pnl_value,
        max_price,
        min_price,
        max_time,
        min_time,
        exec_last_checked_at,
        exec_status,
        exec_status_message,
        reconcile_last_checked_at,
        reconcile_status,
        reconcile_status_message
        )
        SELECT
        id,
        userid,
        signal_id,
        source,
        message,
        entry_snapshot,
        last_snapshot,
        symbol,
        equity_ref,
        instrument_type,
        trade_type,
        position_style,
        hedged_symbol,
        entry_status,
        exit_status,
        execution_mode,
        intraday_only,
        entry_time,
        entry_intent_time,
        entry_exec_time,
        entry_reconciled_at,
        entry_price,
        executed_entry_price,
        executed_entry_qty,
        quantity,
        entry_order_id,
        entry_order_response_json,
        entry_retries,

        trade_management,

        exit_reason,
        exit_rule,
        exit_time,
        exit_intent_time,
        exit_exec_time,
        exit_reconciled_at,
        exit_price,
        executed_exit_price,
        executed_exit_qty,
        exit_order_id,
        exit_order_response_json,
        exit_retries,
        exit_pnl,
        last_time,
        last_price,
        last_pnl,
        last_pnl_value,
        max_price,
        min_price,
        max_time,
        min_time,
        exec_last_checked_at,
        exec_status,
        exec_status_message,
        reconcile_last_checked_at,
        reconcile_status,
        reconcile_status_message
        FROM user_trades
        """
    )
    with get_trades_db() as db:
        engine, dbname, _ = _engine_db_for_model(db, Signal)
        logger.info("Archiving into %s.signals_history ...", dbname)
        with engine.begin() as conn:
            res1 = conn.execute(signals_sql)
        logger.info("Archived signals rows inserted: %s", getattr(res1, "rowcount", "unknown"))

        engine, dbname, _ = _engine_db_for_model(db, UserTrade)
        logger.info("Archiving into %s.user_trades_history ...", dbname)
        with engine.begin() as conn:
            res2 = conn.execute(user_trades_sql)
        logger.info("Archived user_trades rows inserted: %s", getattr(res2, "rowcount", "unknown"))

# ---------------------------
# Auditlog archive before truncate
# ---------------------------
def archive_auditlog_data():
    """Archive auditlog rows to auditlog_history before auditlog is truncated."""
    auditlog_sql = text(
        """
        INSERT INTO auditlog_history (
          auditlog_id,
          ts,
          entity_type,
          entity_id,
          symbol,
          userid,
          evaluation_stage,
          previous_state,
          new_state,
          action,
          reason_code,
          reason_text,
          confidence,
          payload_json
        )
        SELECT
          id,
          ts,
          entity_type,
          entity_id,
          symbol,
          userid,
          evaluation_stage,
          previous_state,
          new_state,
          action,
          reason_code,
          reason_text,
          confidence,
          payload_json
        FROM auditlog
        """
    )

    with get_trades_db() as db:
        engine, dbname, _ = _engine_db_for_model(db, AuditLog)
        logger.info("Archiving into %s.auditlog_history ...", dbname)
        with engine.begin() as conn:
            res = conn.execute(auditlog_sql)
        logger.info("Archived auditlog rows inserted: %s", getattr(res, "rowcount", "unknown"))


# ---------------------------
# Truncation
# ---------------------------
def truncate_intraday_tables():
    with get_trades_db() as db:
        grouped = _select_intraday_tables(db)
        for (_key, engine, dbname), items in grouped.items():
            fq_list = [fq for (_t, fq) in items]
            logger.info("Truncating (%s): %s", dbname, ", ".join(fq_list))
            with engine.begin() as conn:
                conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
                for (_tname, fq) in items:
                    conn.execute(text(f"TRUNCATE TABLE {fq}"))
                conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))
    logger.info("Truncate completed.")


# ---------------------------
# Daily stock-selection reset
# ---------------------------
def reset_eq_processing_flags() -> int:
    whitelist_symbols = sorted(universe_whitelist())
    result = SymbolSchema.reset_daily_selection_flags(
        whitelist_symbols=whitelist_symbols,
        type_filter="EQ",
    )
    logger.info(
        "Reset daily selection flags | reset=%d whitelist_active=%d",
        int(result["reset_count"]),
        int(result["whitelist_active_count"]),
    )
    return int(result["reset_count"])


# ---------------------------
# User login reset
# ---------------------------
def reset_all_user_logins() -> int:
    with get_trades_db() as db:
        count = (
            db.query(UserORM)
            .update(
                {
                    "logged_in": 0,
                    "logged_time": None,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        logger.info("Reset logged_in/logged_time for users: %d", int(count or 0))
        return int(count or 0)


# ---------------------------
# Virtual auto-login baseline user(s)
# ---------------------------
def enable_virtual_autologin_users() -> int:
    """
    Ensure configured virtual users are active/logged-in for the day.

    This is intended for intraday virtual/autotrade testing so that trade
    records are generated even when no real user is actively logged in.
    """
    if not AUTOLOGIN_VIRTUAL_USERS:
        logger.info("No virtual_autologin_userids configured")
        return 0

    now = datetime.now(IST).replace(tzinfo=None, microsecond=0)
    updated = 0

    with get_trades_db() as db:
        for userid in AUTOLOGIN_VIRTUAL_USERS:
            user = db.query(UserORM).filter(UserORM.userid == userid).first()
            if not user:
                logger.warning("Virtual autologin user not found: %s", userid)
                continue

            user.logged_in = True
            user.logged_time = now
            user.autotrade = True
            user.execution_mode = "VIRTUAL"
            user.broker_login = True
            user.intraday_only = True
            user.active = True
            updated += 1

        db.commit()

    logger.info("Enabled virtual autologin users: %d (%s)", updated, ", ".join(AUTOLOGIN_VIRTUAL_USERS))
    return updated


# ---------------------------
# Entry point
# ---------------------------
def main():
    global logger
    setup_logging(log_file=LOG_FILE)
    logger = logging.getLogger(__name__)

    # global day gate
    if not allow_run_today(logger, "init_reset"):
        return

    logger.info(
        "=== init_intraday_reset starting | tz=%s | archive=%s | archive_auditlog=%s | reset_eq=%s | reset_user=%s | virtual_users=%s ===",
        TZ, ARCHIVE_BEFORE_TRUNCATE, ARCHIVE_AUDITLOG, RESET_EQ_FLAGS, RESET_USER_LOGS, AUTOLOGIN_VIRTUAL_USERS,
    )
    reset_now = datetime.now(IST)
    logger.info("init_intraday_reset @ %s", reset_now.isoformat())

    try:
        if ARCHIVE_BEFORE_TRUNCATE:
            try:
                archive_intraday_data()
                if ARCHIVE_AUDITLOG:
                    archive_auditlog_data()
            except Exception:
                logger.exception("Archiving failed (signals/user_trades/auditlog). Aborting reset to avoid data loss.")
                sys.exit(1)

        truncate_intraday_tables()

        if RESET_EQ_FLAGS:
            reset_eq_processing_flags()

        if RESET_USER_LOGS:
            reset_all_user_logins()

        enable_virtual_autologin_users()

    except Exception:
        logger.exception("init_intraday_reset failed.")
        sys.exit(1)

    logger.info("=== init_intraday_reset done ===")


if __name__ == "__main__":
    main()