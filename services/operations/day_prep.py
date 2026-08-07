"""Prepare AutoTrades operational state for a new trading day."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, inspect, select, text

from configs.service_config import SERVICE_CONFIG
from database.database import get_trades_db
from enums.enums import EntryStatus, ExitStatus
from models.trade_models import (
    AuditLog,
    AuditLogHistory,
    Candle,
    DerivativesChain,
    Signal,
    SignalHistory,
    Snapshot,
    StockOpportunity,
    User,
    UserFunds,
    UserOrders,
    UserPositions,
    UserTrade,
    UserTradeHistory,
)
from schemas.archive import ArchiveResult, ArchiveSpec, archive_rows
from schemas.signal import SignalSchema
from schemas.user_trade import UserTradeSchema

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClearResult:
    table: str
    rows_before: int
    rows_after: int

    def as_dict(self) -> dict[str, int | str]:
        return {
            "table": self.table,
            "rows_before": self.rows_before,
            "rows_after": self.rows_after,
        }


class DayPrepService:
    """Archive durable rows, clear working state and prepare users.

    Symbol membership and generation flags are deliberately outside this
    service. They are owned by the verified universe operations.
    """

    def __init__(self, config=None):
        self.config = config or SERVICE_CONFIG.day_prep
        self.tz = ZoneInfo(SERVICE_CONFIG.tz)

    @staticmethod
    def _audit_archive_spec() -> ArchiveSpec:
        return ArchiveSpec(
            name="auditlog",
            source_model=AuditLog,
            history_model=AuditLogHistory,
            target_to_source={"auditlog_id": "id"},
            excluded_target_columns=frozenset({"history_id"}),
            verification_condition=lambda source, target: and_(
                target.c.auditlog_id == source.c.id,
                target.c.ts == source.c.ts,
            ),
        )

    def _required_models(self) -> tuple[type, ...]:
        models: list[type] = [
            Signal,
            SignalHistory,
            UserTrade,
            UserTradeHistory,
                            StockOpportunity,
            Snapshot,
            Candle,
            DerivativesChain,
            AuditLog,
            User,
        ]
        if self.config.archive_auditlog:
            models.append(AuditLogHistory)
        if self.config.clear_oms_current_state:
            models.extend([UserFunds, UserPositions, UserOrders])
        return tuple(models)

    def _verify_tables_exist(self) -> None:
        with get_trades_db() as db:
            db_inspector = inspect(db.get_bind())
            missing = sorted(
                {
                    model.__table__.name
                    for model in self._required_models()
                    if not db_inspector.has_table(model.__table__.name)
                }
            )
        if missing:
            raise RuntimeError(f"Day Prep required tables missing: {missing}")

    @staticmethod
    def _is_terminal_trade(entry_status: str, exit_status: str) -> bool:
        terminal_without_position = {
            EntryStatus.EXPIRED.value,
            EntryStatus.CANCELLED.value,
            EntryStatus.REJECTED.value,
            EntryStatus.INVALID.value,
        }
        if entry_status in terminal_without_position:
            return True
        return (
            entry_status == EntryStatus.FILLED.value
            and exit_status == ExitStatus.FILLED.value
        )

    def _unresolved_trade_rows(self) -> list[dict[str, Any]]:
        with get_trades_db() as db:
            rows = (
                db.query(
                    UserTrade.id,
                    UserTrade.userid,
                    UserTrade.symbol,
                    UserTrade.entry_status,
                    UserTrade.exit_status,
                )
                .order_by(UserTrade.id.asc())
                .all()
            )

        unresolved = []
        for row in rows:
            entry_status = str(row.entry_status or "").upper()
            exit_status = str(row.exit_status or ExitStatus.NONE.value).upper()
            if self._is_terminal_trade(entry_status, exit_status):
                continue
            unresolved.append(
                {
                    "id": int(row.id),
                    "userid": row.userid,
                    "symbol": row.symbol,
                    "entry_status": entry_status,
                    "exit_status": exit_status,
                }
            )
        return unresolved

    @staticmethod
    def _opportunity_state_counts() -> dict[str, int]:
        with get_trades_db() as db:
            rows = (
                db.query(
                    StockOpportunity.lifecycle_state,
                    func.count(StockOpportunity.id),
                )
                .group_by(StockOpportunity.lifecycle_state)
                .order_by(StockOpportunity.lifecycle_state.asc())
                .all()
            )
        return {str(state): int(count) for state, count in rows}

    @staticmethod
    def _clear_model(model: type) -> ClearResult:
        table_name = model.__table__.name
        with get_trades_db() as db:
            rows_before = int(
                db.execute(
                    select(func.count()).select_from(model.__table__)
                ).scalar_one()
            )
            db.execute(text(f"TRUNCATE TABLE `{table_name}`"))
            db.commit()
            rows_after = int(
                db.execute(
                    select(func.count()).select_from(model.__table__)
                ).scalar_one()
            )
        if rows_after != 0:
            raise RuntimeError(
                f"Day Prep failed to clear {table_name}: rows_after={rows_after}"
            )
        return ClearResult(table_name, rows_before, rows_after)

    def _clear_models(self) -> tuple[type, ...]:
        models: list[type] = [
            UserTrade,
            Signal,
                    StockOpportunity,
            Snapshot,
            Candle,
            DerivativesChain,
            AuditLog,
        ]
        if self.config.clear_oms_current_state:
            models.extend([UserFunds, UserPositions, UserOrders])
        return tuple(models)

    @staticmethod
    def _reset_user_logins() -> int:
        with get_trades_db() as db:
            count = db.query(User).update(
                {"logged_in": False, "logged_time": None},
                synchronize_session=False,
            )
            db.commit()
        return int(count or 0)

    def _enable_virtual_users(self) -> tuple[int, list[str]]:
        requested = [
            str(userid).strip()
            for userid in self.config.virtual_autologin_userids
            if str(userid).strip()
        ]
        if not requested:
            return 0, []

        now = datetime.now(self.tz).replace(tzinfo=None, microsecond=0)
        enabled = 0
        missing: list[str] = []
        with get_trades_db() as db:
            for userid in requested:
                user = db.query(User).filter(User.userid == userid).one_or_none()
                if user is None:
                    missing.append(userid)
                    continue
                user.logged_in = True
                user.logged_time = now
                user.autotrade = True
                user.execution_mode = "VIRTUAL"
                user.broker_login = True
                user.intraday_only = True
                user.active = True
                enabled += 1
            db.commit()
        return enabled, missing

    def _archive_rows(
        self,
    ) -> tuple[dict[str, dict[str, Any]], set[str]]:
        """Archive independent durable tables without aborting Day Prep.

        A failed archive is isolated to its source table.  The failure is logged
        with context and that source table is protected from clearing later in
        the run.  Other archive/clear work continues.
        """

        archive_jobs = [
            ("signals", Signal.__table__.name, SignalSchema.archive_current_rows),
            (
                "user_trades",
                UserTrade.__table__.name,
                UserTradeSchema.archive_current_rows,
            ),
        ]
        if self.config.archive_auditlog:
            archive_jobs.append(
                (
                    "auditlog",
                    AuditLog.__table__.name,
                    lambda: archive_rows(self._audit_archive_spec()),
                )
            )

        archives: dict[str, dict[str, Any]] = {}
        failed_source_tables: set[str] = set()
        for name, source_table, archive_fn in archive_jobs:
            try:
                result: ArchiveResult = archive_fn()
            except Exception as exc:
                failed_source_tables.add(source_table)
                archives[name] = {
                    "name": name,
                    "success": False,
                    "source_table": source_table,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
                logger.exception(
                    "DAY_PREP_ARCHIVE_FAILED | name=%s | table=%s | "
                    "action=SKIP_CLEAR_CONTINUE",
                    name,
                    source_table,
                )
                continue

            payload = result.as_dict()
            payload["success"] = True
            archives[result.name] = payload
            logger.info("DAY_PREP_ARCHIVE | %s", payload)

        return archives, failed_source_tables

    def prepare(self) -> dict[str, Any]:
        started_at = datetime.now(self.tz)
        logger.info(
            "DAY_PREP_START | archive_auditlog=%s | clear_oms=%s | "
            "strict_unresolved_trades=%s | virtual_users=%s",
            bool(self.config.archive_auditlog),
            bool(self.config.clear_oms_current_state),
            bool(self.config.strict_unresolved_trade_check),
            list(self.config.virtual_autologin_userids),
        )

        self._verify_tables_exist()
        unresolved = self._unresolved_trade_rows()
        if unresolved:
            log_unresolved = (
                logger.error
                if self.config.strict_unresolved_trade_check
                else logger.warning
            )
            log_unresolved(
                "DAY_PREP_UNRESOLVED_TRADES | count=%d | rows=%s | "
                "action=ARCHIVE_AND_CONTINUE",
                len(unresolved),
                unresolved[:20],
            )

        opportunity_states = self._opportunity_state_counts()
        logger.info(
            "DAY_PREP_OPPORTUNITIES | rows=%d | states=%s",
            sum(opportunity_states.values()),
            opportunity_states,
        )

        # Archive failures are isolated by table.  A source table whose archive
        # failed is never cleared; independent tables continue through Day Prep.
        archives, archive_failed_tables = self._archive_rows()

        cleared: dict[str, dict[str, Any]] = {}
        for model in self._clear_models():
            table_name = model.__table__.name
            if table_name in archive_failed_tables:
                payload = {
                    "table": table_name,
                    "skipped": True,
                    "reason": "ARCHIVE_FAILED",
                }
                cleared[table_name] = payload
                logger.error(
                    "DAY_PREP_CLEAR_SKIPPED | table=%s | reason=ARCHIVE_FAILED",
                    table_name,
                )
                continue

            try:
                result = self._clear_model(model)
            except Exception as exc:
                payload = {
                    "table": table_name,
                    "skipped": False,
                    "success": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
                cleared[table_name] = payload
                logger.exception(
                    "DAY_PREP_CLEAR_FAILED | table=%s | action=CONTINUE",
                    table_name,
                )
                continue

            payload = result.as_dict()
            payload["success"] = True
            cleared[result.table] = payload
            logger.info("DAY_PREP_CLEAR | %s", payload)

        users_reset = 0
        user_reset_error: dict[str, str] | None = None
        if self.config.reset_user_logins:
            try:
                users_reset = self._reset_user_logins()
            except Exception as exc:
                user_reset_error = {
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
                logger.exception(
                    "DAY_PREP_USER_RESET_FAILED | action=CONTINUE"
                )

        virtual_users_enabled = 0
        missing_virtual_users: list[str] = []
        virtual_user_error: dict[str, str] | None = None
        try:
            virtual_users_enabled, missing_virtual_users = (
                self._enable_virtual_users()
            )
        except Exception as exc:
            virtual_user_error = {
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            logger.exception(
                "DAY_PREP_VIRTUAL_USERS_FAILED | action=CONTINUE"
            )

        logger.info(
            "DAY_PREP_USERS | reset=%d | virtual_enabled=%d | missing=%s",
            users_reset,
            virtual_users_enabled,
            missing_virtual_users,
        )

        finished_at = datetime.now(self.tz)
        clear_failed_tables = sorted(
            table
            for table, payload in cleared.items()
            if payload.get("success") is False
        )
        completed_with_errors = bool(
            unresolved
            or archive_failed_tables
            or clear_failed_tables
            or user_reset_error
            or virtual_user_error
        )

        summary = {
            "success": True,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_seconds": round(
                (finished_at - started_at).total_seconds(), 3
            ),
            "archives": archives,
            "auditlog_archive_enabled": bool(self.config.archive_auditlog),
            "cleared": cleared,
            "stock_opportunity_states_before_clear": opportunity_states,
            "unresolved_trade_count": len(unresolved),
            "unresolved_trades": unresolved,
            "archive_failed_tables": sorted(archive_failed_tables),
            "clear_failed_tables": clear_failed_tables,
            "completed_with_errors": completed_with_errors,
            "users_reset": users_reset,
            "user_reset_error": user_reset_error,
            "virtual_users_enabled": virtual_users_enabled,
            "missing_virtual_users": missing_virtual_users,
            "virtual_user_error": virtual_user_error,
        }
        logger.info("DAY_PREP_SUMMARY | %s", summary)
        return summary


__all__ = ["ClearResult", "DayPrepService"]
