#!/usr/bin/env python3
"""Refresh the authoritative NSE/NFO broker instrument master.

The broker response is the source of truth.  This operation applies no
AutoTrades universe, price, expiry, whitelist or blacklist policy.  It fetches
both NSE and NFO, validates only transport/structure, converts the complete
response, and then replaces the local ``instruments`` table in one database
session.

There is intentionally no command-line interface.  Sensex/BSE support can be
added later by extending ``DEFAULT_EXCHANGES`` when that market is adopted.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config import AppConfig
from database.database import get_trades_db
from logconfig import setup_logging
from models.trade_models import Instrument
from schemas.user import UserSchema
from services.zerodha.kiteconnect_service import KiteConnectService
from utils.datetime_utils import IST

logger = logging.getLogger(__name__)

# Visible operational defaults.  This operation intentionally has no CLI.
DEFAULT_EXCHANGES: tuple[str, ...] = ("NSE", "NFO")
DEFAULT_LOG_FILE = "/var/www/autotrades/operations/refresh_broker_instruments.log"
DEFAULT_REPORT_DIR = "reports"

_REQUIRED_KEYS = {
    "instrument_token",
    "exchange_token",
    "tradingsymbol",
    "name",
    "last_price",
    "expiry",
    "strike",
    "tick_size",
    "lot_size",
    "instrument_type",
    "segment",
    "exchange",
}


@dataclass(frozen=True)
class DownloadValidation:
    exchange: str
    row_count: int
    issues: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return self.row_count > 0 and not self.issues


def _norm(value: Any) -> str:
    return str(value or "").strip().upper()


def _as_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise ValueError(f"unsupported_expiry_type:{type(value).__name__}")


def validate_broker_download(
    exchange: str,
    rows: Sequence[Mapping[str, Any]] | None,
) -> DownloadValidation:
    """Validate only that a broker download is usable as a complete response."""
    expected_exchange = _norm(exchange)
    issues: list[str] = []

    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return DownloadValidation(expected_exchange, 0, ("response_not_sequence",))
    if not rows:
        return DownloadValidation(expected_exchange, 0, ("empty_response",))

    tokens: set[str] = set()
    identities: set[tuple[str, str]] = set()

    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            issues.append(f"row_not_mapping:{index}")
            continue

        missing = sorted(_REQUIRED_KEYS - set(row))
        if missing:
            issues.append(f"missing_keys:{index}:{','.join(missing)}")
            continue

        token = str(row.get("instrument_token") or "").strip()
        symbol = str(row.get("tradingsymbol") or "").strip()
        row_exchange = _norm(row.get("exchange"))
        segment = _norm(row.get("segment"))

        if not token:
            issues.append(f"missing_instrument_token:{index}")
        elif token in tokens:
            issues.append(f"duplicate_instrument_token:{token}")
        else:
            tokens.add(token)

        if not symbol:
            issues.append(f"missing_tradingsymbol:{index}")
        identity = (row_exchange, symbol)
        if symbol and identity in identities:
            issues.append(f"duplicate_exchange_symbol:{row_exchange}:{symbol}")
        elif symbol:
            identities.add(identity)

        if row_exchange != expected_exchange:
            issues.append(
                f"unexpected_exchange:{index}:{row_exchange or 'EMPTY'}!={expected_exchange}"
            )
        if not segment:
            issues.append(f"missing_segment:{index}")

    return DownloadValidation(expected_exchange, len(rows), tuple(issues))


def convert_broker_row(row: Mapping[str, Any]) -> Instrument:
    """Convert one structurally validated broker row without policy changes."""
    return Instrument(
        instrument_token=str(row["instrument_token"]),
        exchange_token=str(row["exchange_token"]),
        tradingsymbol=str(row["tradingsymbol"]),
        # Kite may publish an empty name for some instruments.  Preserve the
        # broker value as an empty string because the local column is non-null.
        name=str(row.get("name") or ""),
        last_price=row.get("last_price"),
        expiry=_as_date(row.get("expiry")),
        strike=row.get("strike"),
        tick_size=row.get("tick_size"),
        lot_size=row.get("lot_size"),
        instrument_type=str(row.get("instrument_type") or ""),
        segment=str(row.get("segment") or ""),
        exchange=str(row.get("exchange") or ""),
    )


def _validate_combined_rows(rows: Sequence[Mapping[str, Any]]) -> None:
    tokens: set[str] = set()
    duplicates: list[str] = []
    for row in rows:
        token = str(row.get("instrument_token") or "").strip()
        if token in tokens:
            duplicates.append(token)
        tokens.add(token)
    if duplicates:
        sample = ",".join(sorted(set(duplicates))[:20])
        raise ValueError(f"duplicate_tokens_across_downloads:{sample}")


def _group_counts(rows: Iterable[Mapping[str, Any]]) -> Counter[tuple[str, str, str]]:
    return Counter(
        (
            _norm(row.get("exchange")),
            _norm(row.get("segment")),
            _norm(row.get("instrument_type")) or "EMPTY",
        )
        for row in rows
    )


def _write_report(
    rows: Sequence[Mapping[str, Any]],
    *,
    existing_count: int,
    written_count: int,
    run_time: datetime,
) -> Path:
    directory = Path(DEFAULT_REPORT_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"refresh_broker_instruments_{run_time.strftime('%Y%m%d_%H%M%S')}.csv"
    counts = _group_counts(rows)
    fieldnames = [
        "exchange",
        "segment",
        "instrument_type",
        "row_count",
        "existing_master_count",
        "written_master_count",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for (exchange, segment, instrument_type), count in sorted(counts.items()):
            writer.writerow(
                {
                    "exchange": exchange,
                    "segment": segment,
                    "instrument_type": instrument_type,
                    "row_count": count,
                    "existing_master_count": existing_count,
                    "written_master_count": written_count,
                }
            )
    return path


def _data_client() -> KiteConnectService:
    user = UserSchema.fetch_user(AppConfig.DATA_USER)
    if not user:
        raise RuntimeError(f"DATA_USER not found: {AppConfig.DATA_USER}")
    if not user.apikey or not user.access_token:
        raise RuntimeError(f"DATA_USER missing apikey/access_token: {AppConfig.DATA_USER}")
    return KiteConnectService(api_key=user.apikey, access_token=user.access_token)


def main() -> int:
    setup_logging(log_file=DEFAULT_LOG_FILE)
    started = time.monotonic()
    run_time = datetime.now(IST)
    logger.info(
        "REFRESH_BROKER_INSTRUMENTS_START | exchanges=%s | data_user=%s",
        ",".join(DEFAULT_EXCHANGES),
        AppConfig.DATA_USER,
    )

    try:
        kite = _data_client()
    except Exception:
        logger.exception("REFRESH_BROKER_INSTRUMENTS_PREFLIGHT_FAILED")
        return 2

    downloads: dict[str, list[dict[str, Any]]] = {}
    validations: dict[str, DownloadValidation] = {}

    for exchange in DEFAULT_EXCHANGES:
        try:
            logger.info("REFRESH_BROKER_INSTRUMENTS_FETCH | exchange=%s", exchange)
            response = kite.kite.instruments(exchange)
            rows = list(response or [])
        except Exception:
            logger.exception(
                "REFRESH_BROKER_INSTRUMENTS_FETCH_FAILED | exchange=%s", exchange
            )
            return 2

        validation = validate_broker_download(exchange, rows)
        downloads[exchange] = rows
        validations[exchange] = validation
        logger.info(
            "REFRESH_BROKER_INSTRUMENTS_FETCHED | exchange=%s | rows=%d | valid=%s",
            exchange,
            validation.row_count,
            validation.valid,
        )
        if not validation.valid:
            logger.error(
                "REFRESH_BROKER_INSTRUMENTS_INVALID | exchange=%s | issues=%s",
                exchange,
                list(validation.issues[:50]),
            )
            return 2

    all_rows = [row for exchange in DEFAULT_EXCHANGES for row in downloads[exchange]]

    try:
        _validate_combined_rows(all_rows)
        converted = [convert_broker_row(row) for row in all_rows]
    except Exception:
        logger.exception("REFRESH_BROKER_INSTRUMENTS_CONVERSION_FAILED")
        return 2

    existing_count = 0
    written_count = 0
    try:
        with get_trades_db() as db:
            existing_count = int(db.query(Instrument).count())
            db.query(Instrument).delete(synchronize_session=False)
            db.bulk_save_objects(converted)
            db.flush()
            written_count = int(db.query(Instrument).count())
            if written_count != len(converted):
                raise RuntimeError(
                    f"written_count_mismatch:{written_count}!={len(converted)}"
                )
            db.commit()
    except Exception:
        logger.exception(
            "REFRESH_BROKER_INSTRUMENTS_WRITE_FAILED | existing_count=%d | intended_count=%d",
            existing_count,
            len(converted),
        )
        return 2

    try:
        report_path = _write_report(
            all_rows,
            existing_count=existing_count,
            written_count=written_count,
            run_time=run_time,
        )
    except Exception:
        logger.exception("REFRESH_BROKER_INSTRUMENTS_REPORT_FAILED")
        report_path = None

    summary = {
        "exchanges": list(DEFAULT_EXCHANGES),
        "nse_rows": validations.get("NSE").row_count if "NSE" in validations else 0,
        "nfo_rows": validations.get("NFO").row_count if "NFO" in validations else 0,
        "existing_master_count": existing_count,
        "written_master_count": written_count,
        "verified": written_count == len(converted),
        "report": str(report_path) if report_path else None,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    logger.info(
        "REFRESH_BROKER_INSTRUMENTS_SUMMARY | %s",
        json.dumps(summary, sort_keys=True),
    )
    if not summary["verified"]:
        return 2
    return 0 if report_path is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
