#!/usr/bin/env python3
from __future__ import annotations

import logging
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

# allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import AppConfig
from database.database import get_trades_db, trades_engine
from logconfig import setup_logging
from models.trade_models import Snapshot as SnapshotORM
from schemas.symbol import SymbolSchema
from schemas.user import UserSchema
from services.snapshot.snapshot_generator import SnapshotGenerator

# ---------------------------------------------------------------------------
# Hard-coded replay configuration
# ---------------------------------------------------------------------------
START = datetime(2026, 7, 24, 9, 18, tzinfo=ZoneInfo("Asia/Kolkata"))
END = datetime(2026, 7, 24, 15, 30, tzinfo=ZoneInfo("Asia/Kolkata"))

# Use exactly one of these forms:
#   ["ALL"]                         -> all symbols allowed by ACTIVE_ONLY
#   ["MARUTI", "INFY", "ASTRAL"]  -> only the named symbols allowed by ACTIVE_ONLY
SYMBOLS: List[str] = ["ALL"]
SYMBOL_TYPE_FILTER = "EQ"

# True  -> replay only enabled symbols whose intraday `active` flag is True.
# False -> replay every enabled symbol, regardless of its intraday `active` flag.
#
# `enabled=True` remains the policy/universe gate in both modes because
# SymbolSchema.fetch_symbols applies it implicitly.
ACTIVE_ONLY: bool = True

# Credentials are read from the DATA_USER configured in config.AppConfig.
# Leave these blank to use the database values. A non-empty value overrides
# only that credential, which is useful when testing with a temporary token.
DATA_USER_ID = AppConfig.DATA_USER
API_KEY_OVERRIDE = ""
ACCESS_TOKEN_OVERRIDE = ""

# Number of parallel worker processes
MAX_WORKERS = 5

MARKET_OPEN_HHMM: Tuple[int, int] = (9, 15)
MARKET_CLOSE_HHMM: Tuple[int, int] = (15, 30)
TF_PRIMARY_MIN = 3

# Backfill mode: when True, replay_snapshots will not regenerate rows that
# already exist in snapshots for the exact (symbol, snapshot_time). This lets us
# safely fill missing intraday gaps after live snapshot delays without
# overwriting already-good live snapshots.
SKIP_EXISTING_SNAPSHOTS = True

# Defensive child-side check. Keep True unless you intentionally want to force
# regeneration. The parent process already filters the task list; this prevents
# races/duplicates if two replay processes are started by mistake.
RECHECK_EXISTS_IN_WORKER = True


def _normalized_symbol_selection() -> List[str]:
    """Return a de-duplicated, uppercase copy of the hard-coded selection."""
    normalized: List[str] = []
    seen = set()
    for value in SYMBOLS:
        symbol = str(value or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        normalized.append(symbol)

    if not normalized:
        raise RuntimeError(
            'SYMBOLS is empty. Use ["ALL"] or provide one or more EQ symbols.'
        )

    if "ALL" in seen and len(normalized) != 1:
        raise RuntimeError(
            'Use SYMBOLS = ["ALL"] by itself; do not combine ALL with named symbols.'
        )

    return normalized


def _all_symbols_requested() -> bool:
    return _normalized_symbol_selection() == ["ALL"]


def _requested_symbols() -> List[str]:
    selection = _normalized_symbol_selection()
    return [] if selection == ["ALL"] else selection


def _selected_symbol_rows():
    # `enabled=True` is applied implicitly inside SymbolSchema.fetch_symbols.
    # ACTIVE_ONLY=True applies the intraday active gate; False ignores it.
    active_filter = 1 if ACTIVE_ONLY else None
    rows = SymbolSchema.fetch_symbols(
        active=active_filter,
        type_filter=SYMBOL_TYPE_FILTER,
    ) or []
    rows = sorted(
        rows,
        key=lambda row: str(getattr(row, "symbol", "") or "").strip().upper(),
    )

    if _all_symbols_requested():
        return rows

    requested = _requested_symbols()
    requested_set = set(requested)
    selected_by_symbol = {
        str(getattr(row, "symbol", "") or "").strip().upper(): row
        for row in rows
    }

    missing = sorted(requested_set - set(selected_by_symbol))
    if missing:
        raise RuntimeError(
            "Requested replay symbols were not found in the selected enabled %s "
            "universe (ACTIVE_ONLY=%s): %s"
            % (SYMBOL_TYPE_FILTER, ACTIVE_ONLY, ", ".join(missing))
        )

    # Preserve the order written in SYMBOLS for easier controlled replays.
    return [selected_by_symbol[symbol] for symbol in requested]


def _resolve_api_credentials() -> Tuple[str, str, str]:
    """Resolve Kite credentials from DATA_USER, with optional hard-coded overrides."""
    userid = str(DATA_USER_ID or "").strip()
    api_key_override = str(API_KEY_OVERRIDE or "").strip()
    access_token_override = str(ACCESS_TOKEN_OVERRIDE or "").strip()

    db_api_key = ""
    db_access_token = ""

    if userid:
        user = UserSchema.fetch_user(userid)
        if user is None:
            if not (api_key_override and access_token_override):
                raise RuntimeError(
                    f"DATA_USER {userid!r} was not found and complete credential "
                    "overrides were not supplied"
                )
        else:
            db_api_key = str(user.apikey or "").strip()
            db_access_token = str(user.access_token or "").strip()
    elif not (api_key_override and access_token_override):
        raise RuntimeError(
            "DATA_USER_ID is blank and complete credential overrides were not supplied"
        )

    api_key = api_key_override or db_api_key
    access_token = access_token_override or db_access_token

    if not api_key or not access_token:
        missing = []
        if not api_key:
            missing.append("apikey")
        if not access_token:
            missing.append("access_token")
        raise RuntimeError(
            "Replay credentials are incomplete for DATA_USER %r; missing %s"
            % (userid, ", ".join(missing))
        )

    if api_key_override and access_token_override:
        source = "HARDCODED_OVERRIDES"
    elif api_key_override or access_token_override:
        source = "DATABASE_WITH_PARTIAL_OVERRIDE"
    else:
        source = "DATABASE"

    return api_key, access_token, source



def _expected_snapshot_time_for_tick(
    tick_time: datetime,
    *,
    period_minutes: int = TF_PRIMARY_MIN,
    market_open_hhmm: Tuple[int, int] = MARKET_OPEN_HHMM,
    market_close_hhmm: Tuple[int, int] = MARKET_CLOSE_HHMM,
) -> Optional[datetime]:
    """Return the snapshot_time that SnapshotGenerator will persist for a tick.

    SnapshotGenerator selects the latest *completed* 3-minute candle. At exact
    boundaries, the candle that just started is not complete yet. Therefore a
    replay tick at 12:15 persists snapshot_time 12:12, not 12:15. The backfill
    skip/existence logic must check the persisted snapshot_time, not the replay
    tick time.
    """
    if tick_time.tzinfo is None:
        tick_time = tick_time.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
    else:
        tick_time = tick_time.astimezone(ZoneInfo("Asia/Kolkata"))

    open_ = tick_time.replace(
        hour=market_open_hhmm[0],
        minute=market_open_hhmm[1],
        second=0,
        microsecond=0,
    )
    close_ = tick_time.replace(
        hour=market_close_hhmm[0],
        minute=market_close_hhmm[1],
        second=0,
        microsecond=0,
    )

    period_minutes = max(1, int(period_minutes))

    if tick_time <= open_:
        return None

    if tick_time >= close_:
        return close_ - timedelta(minutes=period_minutes)

    minutes_since_open = int((tick_time - open_).total_seconds() // 60)
    if minutes_since_open % period_minutes == 0:
        minutes_since_open -= period_minutes
    else:
        minutes_since_open = (minutes_since_open // period_minutes) * period_minutes

    if minutes_since_open < 0:
        return None

    return open_ + timedelta(minutes=minutes_since_open)


def _db_time_variants(snapshot_time: datetime) -> List[datetime]:
    """Return timestamp variants likely to match MySQL DATETIME rows.

    The replay constants are timezone-aware IST datetimes, while MySQL DATETIME
    columns are often stored without timezone information. Existing code usually
    compares the aware value directly, but for gap-filling we defensively check
    both aware and naive forms so existing rows are not accidentally rewritten.
    """
    variants = [snapshot_time]
    if snapshot_time.tzinfo is not None:
        variants.append(snapshot_time.replace(tzinfo=None))
    return variants


def _snapshot_exists(symbol: str, snapshot_time: datetime) -> bool:
    """Return True if a snapshot row already exists for symbol/time.

    This intentionally checks only row existence and does not load the large JSON
    payload. It is used by replay/backfill to fill gaps without rewriting
    already-present live snapshots.
    """
    symbol_key = str(symbol or "").strip().upper()
    if not symbol_key:
        return False

    with get_trades_db() as db:
        return (
            db.query(SnapshotORM.symbol)
            .filter(SnapshotORM.symbol == symbol_key)
            .filter(SnapshotORM.snapshot_time.in_(_db_time_variants(snapshot_time)))
            .first()
            is not None
        )


def _missing_symbols_for_time(symbols: List[str], current_time: datetime) -> List[str]:
    """Return symbols missing snapshots at current_time.

    Query all existing rows for the tick in one DB round trip instead of one
    query per symbol. This keeps gap-filling cheap even when many snapshots
    already exist.
    """
    if not SKIP_EXISTING_SNAPSHOTS:
        return list(symbols)

    if not symbols:
        return []

    with get_trades_db() as db:
        existing_rows = (
            db.query(SnapshotORM.symbol)
            .filter(SnapshotORM.snapshot_time.in_(_db_time_variants(current_time)))
            .filter(SnapshotORM.symbol.in_(symbols))
            .all()
        )

    existing = {str(row[0] or "").strip().upper() for row in existing_rows}
    return [sym for sym in symbols if sym not in existing]


def _symbol_token_map(rows) -> Dict[str, int]:
    tokens: Dict[str, int] = {}
    for row in rows:
        symbol = str(getattr(row, "symbol", "") or "").strip().upper()
        token = getattr(row, "token", None)
        if not symbol:
            raise RuntimeError(f"Replay symbol row has empty symbol: {row}")
        if token is None:
            raise RuntimeError(f"Replay symbol {symbol} has no token")
        tokens[symbol] = int(token)
    return tokens


def _generate_for_symbol(
    symbol: str,
    token: int,
    current_time: datetime,
    expected_snapshot_time: datetime,
    api_key: str,
    access_token: str,
):
    # dispose any inherited connections so each process gets a fresh one
    trades_engine.dispose()

    if SKIP_EXISTING_SNAPSHOTS and RECHECK_EXISTS_IN_WORKER:
        if _snapshot_exists(symbol, expected_snapshot_time):
            return "SKIPPED_EXISTS"

    gen = SnapshotGenerator(
        token=token,
        symbol=symbol,
        api_key=api_key,
        access_token=access_token,
    )
    snapshot = gen.generate_snapshot(
        end_date=current_time,
        persist_snapshot=True,
    )

    if snapshot is None:
        return "NO_SNAPSHOT"

    # Verify the actual persisted row, because generate_snapshot persists the
    # latest completed candle, not necessarily the replay tick timestamp.
    if _snapshot_exists(symbol, expected_snapshot_time):
        return "CREATED"

    actual_time = getattr(snapshot, "snapshot_time", None)
    if actual_time and _snapshot_exists(symbol, actual_time):
        return "CREATED_DIFFERENT_TIME"

    return "NOT_WRITTEN"


def main():
    setup_logging(log_file="replay_snapshots.log")
    logger = logging.getLogger(__name__)

    # Resolve all startup dependencies before spawning worker processes.
    api_key, access_token, credential_source = _resolve_api_credentials()
    rows = _selected_symbol_rows()
    symbols = [str(r.symbol).strip().upper() for r in rows]
    tokens = _symbol_token_map(rows)

    if not symbols:
        raise RuntimeError(
            f"No enabled {SYMBOL_TYPE_FILTER} symbols selected for replay_snapshots"
        )

    logger.info(
        "Starting replay_snapshots for %d symbols from %s to %s | symbols=%s "
        "type=%s active_only=%s data_user=%s credentials=%s",
        len(symbols),
        START,
        END,
        (
            "ALL_ACTIVE_ENABLED_EQ"
            if _all_symbols_requested() and ACTIVE_ONLY
            else "ALL_ENABLED_EQ"
            if _all_symbols_requested()
            else symbols
        ),
        SYMBOL_TYPE_FILTER,
        ACTIVE_ONLY,
        DATA_USER_ID,
        credential_source,
    )

    current = START
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
        while current <= END:
            logger.info(">>> %s", current)
            t0 = time.time()
            expected_snapshot_time = _expected_snapshot_time_for_tick(current)

            if expected_snapshot_time is None:
                logger.info("Skipping tick %s: no completed candle yet", current)
                current += timedelta(minutes=3)
                continue

            symbols_to_generate = _missing_symbols_for_time(symbols, expected_snapshot_time)
            skipped_existing = len(symbols) - len(symbols_to_generate)

            if not symbols_to_generate:
                elapsed = time.time() - t0
                logger.info(
                    "Completed tick %s -> snapshot_time %s: created=0 skipped_existing=%d failed=0 elapsed=%.3f sec",
                    current,
                    expected_snapshot_time,
                    skipped_existing,
                    elapsed,
                )
                current += timedelta(minutes=3)
                continue

            logger.info(
                "Tick %s -> snapshot_time %s: total=%d missing=%d skipped_existing=%d",
                current,
                expected_snapshot_time,
                len(symbols),
                len(symbols_to_generate),
                skipped_existing,
            )

            # submit one task per missing symbol only
            futures = {
                pool.submit(
                    _generate_for_symbol,
                    sym,
                    tokens[sym],
                    current,
                    expected_snapshot_time,
                    api_key,
                    access_token,
                ): sym
                for sym in symbols_to_generate
            }

            created = 0
            created_different_time = 0
            no_snapshot = 0
            not_written = 0
            worker_skipped = 0
            failed = 0

            # wait for all to complete
            for fut in as_completed(futures):
                sym = futures[fut]
                try:
                    result = fut.result()
                    if result == "SKIPPED_EXISTS":
                        worker_skipped += 1
                        logger.debug("  [%s] SKIPPED_EXISTS", sym)
                    elif result == "CREATED":
                        created += 1
                        logger.debug("  [%s] CREATED", sym)
                    elif result == "CREATED_DIFFERENT_TIME":
                        created_different_time += 1
                        logger.warning("  [%s] CREATED_DIFFERENT_TIME", sym)
                    elif result == "NO_SNAPSHOT":
                        no_snapshot += 1
                        logger.warning("  [%s] NO_SNAPSHOT", sym)
                    else:
                        not_written += 1
                        logger.warning("  [%s] NOT_WRITTEN result=%s", sym, result)
                except Exception:
                    failed += 1
                    logger.exception("  [%s] FAILED", sym)

            elapsed = time.time() - t0
            logger.info(
                "Completed tick %s -> snapshot_time %s: created=%d created_different_time=%d skipped_existing=%d worker_skipped=%d no_snapshot=%d not_written=%d failed=%d elapsed=%.3f sec",
                current,
                expected_snapshot_time,
                created,
                created_different_time,
                skipped_existing,
                worker_skipped,
                no_snapshot,
                not_written,
                failed,
                elapsed,
            )

            current += timedelta(minutes=3)

    logger.info("Finished replay_snapshots")


if __name__ == "__main__":
    main()
