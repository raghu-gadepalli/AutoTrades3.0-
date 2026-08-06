#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from config import AppConfig
from database.database import trades_engine
from logconfig import setup_logging
from schemas.symbol import SymbolSchema
from schemas.user import UserSchema
from services.stockmap.stockmap_generator import StockMapGenerator

# ---------------------------------------------------------------------------
# Hard-coded research defaults. CLI values override these values.
# ---------------------------------------------------------------------------
REPLAY_DAY = date(2026, 8, 5)
SYMBOLS: List[str] = ["ALL"]
ACTIVE_ONLY = True
MAX_WORKERS = 3
SKIP_EXISTING_STOCKMAPS = True
DATA_USER_ID = AppConfig.DATA_USER
API_KEY_OVERRIDE = ""
ACCESS_TOKEN_OVERRIDE = ""
OUTPUT_DIR = "."


def _normalised_selection(values: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values:
        symbol = str(value or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        out.append(symbol)
    if not out:
        raise RuntimeError('SYMBOLS is empty. Use ["ALL"] or named symbols.')
    if "ALL" in seen and len(out) != 1:
        raise RuntimeError("ALL cannot be combined with named symbols")
    return out


def _selected_symbol_rows(symbols: List[str], active_only: bool):
    selection = _normalised_selection(symbols)
    rows = SymbolSchema.fetch_symbols(
        active=1 if active_only else None,
        type_filter="EQ",
    ) or []
    rows = sorted(rows, key=lambda row: str(row.symbol).strip().upper())
    if selection == ["ALL"]:
        return rows

    by_symbol = {str(row.symbol).strip().upper(): row for row in rows}
    missing = [symbol for symbol in selection if symbol not in by_symbol]
    if missing:
        raise RuntimeError(
            "Requested symbols are not in the selected enabled EQ universe: %s"
            % ", ".join(missing)
        )
    return [by_symbol[symbol] for symbol in selection]


def _resolve_credentials(api_key_override: str, access_token_override: str) -> Tuple[str, str, str]:
    user = UserSchema.fetch_user(DATA_USER_ID) if DATA_USER_ID else None
    db_api_key = str(getattr(user, "apikey", "") or "").strip() if user else ""
    db_access_token = str(getattr(user, "access_token", "") or "").strip() if user else ""
    api_key = str(api_key_override or API_KEY_OVERRIDE or db_api_key).strip()
    access_token = str(access_token_override or ACCESS_TOKEN_OVERRIDE or db_access_token).strip()
    if not api_key or not access_token:
        raise RuntimeError("StockMap replay credentials are incomplete")
    if api_key_override or access_token_override or API_KEY_OVERRIDE or ACCESS_TOKEN_OVERRIDE:
        source = "OVERRIDE"
    else:
        source = "DATABASE"
    return api_key, access_token, source


def _flatten(stockmap) -> Dict[str, Any]:
    accepted = stockmap.structure.accepted
    candidate = stockmap.structure.candidate
    ema = stockmap.indicators.ema
    return {
        "symbol": stockmap.symbol,
        "stockmap_time": stockmap.stockmap_time.isoformat(),
        "close": stockmap.close,
        "raw_state": stockmap.structure.raw.state,
        "raw_side": stockmap.structure.raw.side,
        "accepted_state": accepted.state,
        "accepted_range_id": accepted.range.range_id,
        "accepted_range_version": accepted.range.version,
        "accepted_range_source": accepted.range.source,
        "accepted_range_type": accepted.range.range_type,
        "accepted_range_low": accepted.range.low,
        "accepted_range_high": accepted.range.high,
        "accepted_range_width_atr": accepted.range.width_atr,
        "accepted_range_age_bars": accepted.age_bars,
        "accepted_quality": accepted.quality,
        "candidate_active": candidate.active,
        "candidate_status": candidate.status,
        "candidate_side": candidate.side,
        "candidate_low": candidate.range.low,
        "candidate_high": candidate.range.high,
        "candidate_quality": candidate.quality,
        "candidate_bars_confirmed": candidate.bars_confirmed,
        "ema100": ema.ema100,
        "ema200": ema.ema200,
        "ema100_slope": ema.ema100_slope,
        "ema200_slope": ema.ema200_slope,
        "price_to_ema100": ema.price_to_ema100,
        "price_to_ema200": ema.price_to_ema200,
        "ema_ordering": ema.ordering,
        "ema_regime": ema.regime,
        "atr": stockmap.indicators.atr.value,
        "atr_pct": stockmap.indicators.atr.pct,
        "accepted_range_position": stockmap.location.accepted_range_position,
        "accepted_range_position_pct": stockmap.location.accepted_range_position_pct,
        "nearest_support_type": stockmap.location.nearest_support_type,
        "nearest_support_price": stockmap.location.nearest_support_price,
        "nearest_support_distance_atr": stockmap.location.nearest_support_distance_atr,
        "nearest_resistance_type": stockmap.location.nearest_resistance_type,
        "nearest_resistance_price": stockmap.location.nearest_resistance_price,
        "nearest_resistance_distance_atr": stockmap.location.nearest_resistance_distance_atr,
        "room_up_atr": stockmap.location.room_up_atr,
        "room_down_atr": stockmap.location.room_down_atr,
        "pdh": stockmap.levels.prev_day.high,
        "pdl": stockmap.levels.prev_day.low,
        "today_open": stockmap.levels.today.open,
        "orb_ready": stockmap.levels.opening_range.ready,
        "orb_high": stockmap.levels.opening_range.high,
        "orb_low": stockmap.levels.opening_range.low,
        "calculation_mode": stockmap.diagnostics.calculation_mode,
        "calculation_version": stockmap.diagnostics.calculation_version,
        "reason_codes": "|".join(stockmap.diagnostics.reason_codes),
    }


def _generate_symbol(
    symbol: str,
    token: int,
    replay_day: date,
    api_key: str,
    access_token: str,
    skip_existing: bool,
) -> Dict[str, Any]:
    trades_engine.dispose()
    try:
        generator = StockMapGenerator(
            token=int(token),
            symbol=symbol,
            api_key=api_key,
            access_token=access_token,
        )
        maps = generator.generate_day(
            replay_day,
            persist_stockmaps=True,
            skip_existing=skip_existing,
        )
        return {
            "symbol": symbol,
            "rows": [_flatten(stockmap) for stockmap in maps],
            "error": None,
        }
    except Exception as exc:
        logging.exception("StockMap replay failed for %s", symbol)
        return {"symbol": symbol, "rows": [], "error": repr(exc)}


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Generate one causal day of 15-minute StockMaps"
    )
    parser.add_argument(
        "--day",
        default=None,
        help="Override REPLAY_DAY, YYYY-MM-DD",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help="Override SYMBOLS, for example --symbols ABB BSE or --symbols ALL",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help="Override MAX_WORKERS",
    )
    parser.add_argument(
        "--include-inactive",
        action="store_true",
        help="Override ACTIVE_ONLY and include enabled inactive EQ symbols",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate/upsert existing StockMap rows",
    )
    parser.add_argument("--api-key", default="")
    parser.add_argument("--access-token", default="")
    parser.add_argument("--output-dir", default=None)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    replay_day = date.fromisoformat(args.day) if args.day else REPLAY_DAY
    symbols = args.symbols if args.symbols is not None else SYMBOLS
    active_only = False if args.include_inactive else ACTIVE_ONLY
    max_workers = int(args.max_workers or MAX_WORKERS)
    skip_existing = False if args.force else SKIP_EXISTING_STOCKMAPS
    output_dir = Path(args.output_dir or OUTPUT_DIR).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    setup_logging(log_file="replay_stockmaps.log")
    logger = logging.getLogger(__name__)

    api_key, access_token, credential_source = _resolve_credentials(
        args.api_key,
        args.access_token,
    )
    rows = _selected_symbol_rows(symbols, active_only)
    if not rows:
        raise RuntimeError("No enabled EQ symbols selected for StockMap replay")

    logger.info(
        "Starting StockMap replay day=%s symbols=%d active_only=%s workers=%d skip_existing=%s credentials=%s",
        replay_day,
        len(rows),
        active_only,
        max_workers,
        skip_existing,
        credential_source,
    )

    started = time.perf_counter()
    output_rows: List[Dict[str, Any]] = []
    failures: List[Dict[str, str]] = []

    if max_workers == 1:
        logger.info("Running StockMap replay inline for visible progress logging")
        for row in rows:
            symbol = str(row.symbol).strip().upper()
            logger.info("StockMap replay beginning symbol=%s", symbol)
            result = _generate_symbol(
                symbol,
                int(row.token),
                replay_day,
                api_key,
                access_token,
                skip_existing,
            )
            output_rows.extend(result["rows"])
            if result["error"]:
                failures.append({"symbol": symbol, "error": result["error"]})
            else:
                logger.info("StockMap replay completed %s rows=%d", symbol, len(result["rows"]))
    else:
        with ProcessPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(
                    _generate_symbol,
                    str(row.symbol).strip().upper(),
                    int(row.token),
                    replay_day,
                    api_key,
                    access_token,
                    skip_existing,
                ): str(row.symbol).strip().upper()
                for row in rows
            }
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    logger.exception("StockMap replay worker crashed for %s", symbol)
                    failures.append({"symbol": symbol, "error": repr(exc)})
                    continue
                output_rows.extend(result["rows"])
                if result["error"]:
                    failures.append({"symbol": symbol, "error": result["error"]})
                else:
                    logger.info("StockMap replay completed %s rows=%d", symbol, len(result["rows"]))

    output_rows.sort(key=lambda row: (row["stockmap_time"], row["symbol"]))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = output_dir / f"stockmap_replay_{replay_day.isoformat()}_{stamp}.csv"
    if output_rows:
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(output_rows[0].keys()))
            writer.writeheader()
            writer.writerows(output_rows)

    failure_path = output_dir / f"stockmap_replay_{replay_day.isoformat()}_{stamp}_failures.csv"
    if failures:
        with failure_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["symbol", "error"])
            writer.writeheader()
            writer.writerows(failures)

    logger.info(
        "Finished StockMap replay day=%s rows=%d failures=%d elapsed=%.3fs output=%s",
        replay_day,
        len(output_rows),
        len(failures),
        time.perf_counter() - started,
        csv_path if output_rows else "NONE",
    )


if __name__ == "__main__":
    main()
