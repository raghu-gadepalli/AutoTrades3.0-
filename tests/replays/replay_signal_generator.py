#!/usr/bin/env python3
"""Replay persisted snapshots through the live SignalGenerator.

This harness does not run Auction again and does not mark snapshots processed.
It reads the already validated snapshot.auction projection, calls the same
SignalGenerator used by scripts/gen_signals.py, and writes compact reports.

It writes signal and stock-opportunity rows to the database selected by the
application configuration. Source defaults are visible below and may be
overridden from the command line. Use --reset-symbol-day for a bounded replay;
it deletes only the requested symbol/day signal and opportunity outputs. The
runner consumes structural permissions persisted in each snapshot. It verifies
those permissions against the authoritative events and balance state but never
replaces or repairs the stored projection.
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import date, datetime, time, timedelta
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence
from zoneinfo import ZoneInfo

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from configs.signal_config import SIGNAL_CONFIG
from database.database import get_trades_db, trades_engine
from logconfig import setup_logging
from models.trade_models import Signal as SignalORM
from models.trade_models import StockOpportunity as StockOpportunityORM
from schemas.signal import SignalSchema
from schemas.stock_opportunity import StockOpportunitySchema
from schemas.snapshot import SnapshotSchema
from services.advisor_context.reporting import flatten_advisor_context_for_csv
from services.auction_engine.structural_permissions import StructuralPermissionMatrix
from services.signals.signal_generator import SignalGenerator
from utils.json_utils import sanitize_json

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

# =============================================================================
# SOURCE DEFAULTS
# =============================================================================

DEFAULT_TRADING_DAY = "2026-07-27"
DEFAULT_SYMBOLS = "LT,BHEL,INDIGO,MAXHEALTH,PERSISTENT,PNBHOUSING,TCS"
DEFAULT_CLEAR_DATA = False
DEFAULT_RESET_SYMBOL_DAY = False
DEFAULT_REPORT_DIR = "reports"
DEFAULT_BATCH_SIZE = 500
DEFAULT_LOG_FILE: Optional[str] = None


def _args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Replay stored snapshots through the live SignalGenerator. "
            "All options have visible source defaults and command-line overrides."
        )
    )
    parser.add_argument(
        "--day", "--date",
        dest="date",
        default=DEFAULT_TRADING_DAY,
        help=f"Trading day YYYY-MM-DD (default: {DEFAULT_TRADING_DAY})",
    )
    parser.add_argument(
        "--symbols",
        default=DEFAULT_SYMBOLS,
        help=f"Comma-separated symbols (default: {DEFAULT_SYMBOLS})",
    )
    parser.add_argument(
        "--clear-data",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_CLEAR_DATA,
        help=(
            "Clear complete signals and stock_opportunities tables before replay "
            f"(default: {DEFAULT_CLEAR_DATA})"
        ),
    )
    parser.add_argument(
        "--reset-symbol-day",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_RESET_SYMBOL_DAY,
        help=(
            "Delete only matching symbol/day signals and stock opportunities before "
            f"replay (default: {DEFAULT_RESET_SYMBOL_DAY})"
        ),
    )
    parser.add_argument(
        "--report-dir",
        default=DEFAULT_REPORT_DIR,
        help=f"Report directory (default: {DEFAULT_REPORT_DIR})",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Snapshot fetch batch size (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument("--log-file", default=DEFAULT_LOG_FILE)
    return parser.parse_args(argv)


def _symbols(raw: str) -> List[str]:
    values = sorted({item.strip().upper() for item in raw.split(",") if item.strip()})
    if not values:
        raise ValueError("At least one symbol is required")
    return values


def _write_csv(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    data = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not data:
        path.write_text("", encoding="utf-8")
        return
    fields = sorted({key for row in data for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(data)


def _configured_database_name() -> str:
    database_name = str(trades_engine.url.database or "").strip()
    return database_name or "UNKNOWN"


def _clear_data() -> Dict[str, int]:
    """Clear only replay output tables; persisted snapshots remain untouched."""
    with get_trades_db() as db:
        opportunities_deleted = int(
            db.query(StockOpportunityORM).delete(synchronize_session=False)
        )
        signals_deleted = int(
            db.query(SignalORM).delete(synchronize_session=False)
        )
        db.commit()
    return {
        "signals": signals_deleted,
        "opportunities": opportunities_deleted,
    }


def _reset_symbol_day(
    *,
    trading_day: date,
    symbols: List[str],
    lifecycle: str,
) -> Dict[str, int]:
    """Delete only replay outputs for the requested symbol/day scope."""

    day_start = datetime.combine(trading_day, time.min)
    day_end = day_start + timedelta(days=1)
    with get_trades_db() as db:
        opportunities_deleted = int(
            db.query(StockOpportunityORM)
            .filter(StockOpportunityORM.symbol.in_(symbols))
            .filter(StockOpportunityORM.trading_day == trading_day)
            .delete(synchronize_session=False)
        )
        signals_deleted = int(
            db.query(SignalORM)
            .filter(SignalORM.symbol.in_(symbols))
            .filter(SignalORM.lifecycle == lifecycle)
            .filter(SignalORM.first_seen_time >= day_start)
            .filter(SignalORM.first_seen_time < day_end)
            .delete(synchronize_session=False)
        )
        db.commit()
    return {
        "signals": signals_deleted,
        "opportunities": opportunities_deleted,
    }


def _load_snapshots(
    *,
    trading_day: date,
    symbols: List[str],
    batch_size: int,
) -> List[SnapshotSchema]:
    output: List[SnapshotSchema] = []
    after_time: Optional[datetime] = None
    after_symbol = ""
    while True:
        batch = SnapshotSchema.fetch_day_replay_batch(
            trading_day=trading_day,
            after_time=after_time,
            after_symbol=after_symbol,
            symbols=symbols,
            limit=max(1, batch_size),
        )
        if not batch:
            break
        output.extend(batch)
        last = batch[-1]
        after_time = last.snapshot_time.replace(tzinfo=None)
        after_symbol = last.symbol
        if len(batch) < max(1, batch_size):
            break
    return output


def _opportunity_rows(
    *,
    trading_day: date,
    symbols: List[str],
) -> List[Dict[str, Any]]:
    rows = StockOpportunitySchema.list_for_replay(
        symbols=symbols,
        trading_day=trading_day,
    )
    output: List[Dict[str, Any]] = []
    for row in rows:
        output.append(sanitize_json({
            "opportunity_key": row.opportunity_key,
            "candidate_id": row.candidate_id,
            "latest_candidate_id": row.latest_candidate_id,
            "symbol": row.symbol,
            "equity_ref": row.equity_ref,
            "trading_day": row.trading_day,
            "setup_family": row.setup_family,
            "current_setup_family": row.current_setup_family,
            "setup_subtype": row.setup_subtype,
            "side": row.side,
            "source_event_id": row.source_event_id,
            "source_event_type": row.source_event_type,
            "source_episode_id": row.source_episode_id,
            "latest_event_id": row.latest_event_id,
            "latest_event_type": row.latest_event_type,
            "latest_episode_id": row.latest_episode_id,
            "lifecycle_state": row.lifecycle_state,
            "lifecycle_reason": row.lifecycle_reason,
            "structural_result": row.structural_result,
            "first_seen_time": row.first_seen_time,
            "last_eval_time": row.last_eval_time,
            "deployed_at": row.deployed_at,
            "progressed_at": row.progressed_at,
            "completed_at": row.completed_at,
            "invalidated_at": row.invalidated_at,
            "replaced_at": row.replaced_at,
            "entry_price": row.entry_price,
            "reference_price": row.reference_price,
            "stop_reference_price": row.stop_reference_price,
            "target_reference_price": row.target_reference_price,
            "signal_id": row.signal_id,
            "replacement_opportunity_key": row.replacement_opportunity_key,
            "replaced_opportunity_key": row.replaced_opportunity_key,
            "transition_count": len(row.transition_history),
            "candidate_interpretation_count": len(row.candidate_interpretations),
            "authoritative_event_count": len(row.authoritative_event_lineage),
            "transition_history": json.dumps(row.transition_history, default=str),
            "candidate_interpretations": json.dumps(
                row.candidate_interpretations, default=str
            ),
            "authoritative_event_lineage": json.dumps(
                row.authoritative_event_lineage, default=str
            ),
            "latest_setup_evaluation": json.dumps(
                row.latest_setup_evaluation, default=str
            ),
            "latest_advisor_evaluation": json.dumps(
                row.latest_advisor_evaluation, default=str
            ),
        }))
    return output


def _downstream_meta(signal: SignalSchema) -> Dict[str, Dict[str, Any]]:
    meta = signal.meta_json
    if not isinstance(meta, dict):
        raise ValueError(f"Signal {signal.signal_id} meta_json must be an object")
    required = (
        "downstream_contract",
        "signal",
        "lifecycle",
        "management",
        "setup_levels",
        "auction_signal",
    )
    missing = [key for key in required if key not in meta]
    if missing:
        raise ValueError(
            f"Signal {signal.signal_id} missing downstream contract blocks: {missing}"
        )
    output: Dict[str, Dict[str, Any]] = {}
    for key in required:
        value = meta[key]
        if not isinstance(value, dict):
            raise ValueError(
                f"Signal {signal.signal_id} downstream block {key} must be an object"
            )
        output[key] = value
    version = output["downstream_contract"]["version"]
    if version != "AUCTION_SIGNAL_DOWNSTREAM_V2":
        raise ValueError(
            f"Signal {signal.signal_id} downstream contract version is {version}"
        )
    setup_levels = output["setup_levels"]
    for key in (
        "setup_label",
        "opportunity_key",
        "candidate_id",
        "reference_price",
        "initial_stop_reference_price",
    ):
        if key not in setup_levels:
            raise ValueError(
                f"Signal {signal.signal_id} setup_levels missing {key}"
            )
    return output


def _signal_rows(
    *,
    trading_day: date,
    symbols: List[str],
    lifecycle: str,
) -> List[Dict[str, Any]]:
    start = datetime.combine(trading_day, time.min)
    end = start + timedelta(days=1)
    with get_trades_db() as db:
        rows = (
            db.query(SignalORM)
            .filter(
                SignalORM.symbol.in_(symbols),
                SignalORM.lifecycle == lifecycle,
                SignalORM.first_seen_time >= start,
                SignalORM.first_seen_time < end,
            )
            .order_by(SignalORM.first_seen_time.asc(), SignalORM.id.asc())
            .all()
        )

    result: List[Dict[str, Any]] = []
    for row in rows:
        signal = SignalSchema.model_validate(row)
        meta = signal.meta_json
        if not isinstance(meta, dict) or "auction_signal" not in meta:
            raise ValueError(f"Signal {signal.signal_id} missing auction_signal metadata")
        identity = meta["auction_signal"]
        if not isinstance(identity, dict):
            raise ValueError(f"Signal {signal.signal_id} auction_signal must be an object")
        latest = meta["latest_auction_evaluation"]
        if not isinstance(latest, dict):
            raise ValueError(f"Signal {signal.signal_id} latest_auction_evaluation must be an object")
        history = meta["auction_posture_history"]
        if not isinstance(history, list):
            raise ValueError(f"Signal {signal.signal_id} auction_posture_history must be a list")
        lifecycle_latest = meta["signal_lifecycle"]
        if not isinstance(lifecycle_latest, dict):
            raise ValueError(f"Signal {signal.signal_id} signal_lifecycle must be an object")
        lifecycle_history = meta["signal_lifecycle_history"]
        if not isinstance(lifecycle_history, list):
            raise ValueError(f"Signal {signal.signal_id} signal_lifecycle_history must be a list")
        downstream = _downstream_meta(signal)
        downstream_contract = downstream["downstream_contract"]
        signal_block = downstream["signal"]
        lifecycle_block = downstream["lifecycle"]
        management = downstream["management"]
        setup_levels = downstream["setup_levels"]
        result.append(sanitize_json({
            "signal_id": signal.signal_id,
            "symbol": signal.symbol,
            "equity_ref": signal.equity_ref,
            "lifecycle": signal.lifecycle,
            "setup": signal.setup,
            "side": signal.side.value,
            "stage": signal.stage.value,
            "status": signal.status.value,
            "status_reason": signal.status_reason,
            "first_seen_time": signal.first_seen_time,
            "last_snapshot_time": signal.last_snapshot_time,
            "created_price": signal.created_price,
            "last_price": signal.last_price,
            "ltp": signal.ltp,
            "last_pnl": signal.last_pnl,
            "max_pnl": signal.max_pnl,
            "min_pnl": signal.min_pnl,
            "opportunity_key": identity["opportunity_key"],
            "candidate_id": identity["candidate_id"],
            "boundary_event_key": identity["boundary_event_key"],
            "latest_auction_action": latest["auction_action"],
            "latest_signal_action": lifecycle_latest["signal_action"],
            "latest_signal_stage": lifecycle_latest["stage"],
            "latest_signal_status": lifecycle_latest["status"],
            "latest_signal_reason_code": lifecycle_latest["reason_code"],
            "latest_directional_alignment": lifecycle_latest["directional_alignment"],
            "downstream_contract_version": downstream_contract["version"],
            "downstream_signal_action": signal_block["signal_action"],
            "downstream_signal_state": signal_block["signal_state"],
            "downstream_trade_action": lifecycle_block["trade_action"],
            "management_posture": management["action"],
            "management_reason_code": management["reason_code"],
            "trail_mode": management["trail_mode"],
            "exit_pressure": management["exit_pressure"],
            "target_expansion_allowed": management["target_expansion_allowed"],
            "should_exit_signal": management["should_exit_signal"],
            "setup_reference_price": setup_levels["reference_price"],
            "setup_reference_source": setup_levels["reference_source"],
            "posture_history_count": len(history),
            "signal_lifecycle_history_count": len(lifecycle_history),
        }))
    return result


def _latest_signal_for_symbol(
    *,
    trading_day: date,
    symbol: str,
    lifecycle: str,
) -> Optional[SignalSchema]:
    start = datetime.combine(trading_day, time.min)
    end = start + timedelta(days=1)
    with get_trades_db() as db:
        row = (
            db.query(SignalORM)
            .filter(
                SignalORM.symbol == symbol,
                SignalORM.lifecycle == lifecycle,
                SignalORM.first_seen_time >= start,
                SignalORM.first_seen_time < end,
            )
            .order_by(SignalORM.id.desc())
            .first()
        )
    return SignalSchema.model_validate(row) if row is not None else None


def _same_replay_snapshot_time(signal_time: Optional[datetime], snapshot_time: datetime) -> bool:
    if signal_time is None:
        return False
    left = signal_time
    right = snapshot_time
    if left.tzinfo is None and right.tzinfo is not None:
        left = left.replace(tzinfo=right.tzinfo)
    elif left.tzinfo is not None and right.tzinfo is None:
        right = right.replace(tzinfo=left.tzinfo)
    return left == right



def _transition_record(action: str, signal: SignalSchema) -> Dict[str, Any]:
    downstream = _downstream_meta(signal)
    management = downstream["management"]
    lifecycle = downstream["lifecycle"]
    signal_block = downstream["signal"]
    return sanitize_json({
        "action": action,
        "signal_id": signal.signal_id,
        "setup": signal.setup,
        "side": signal.side.value,
        "stage": signal.stage.value,
        "status": signal.status.value,
        "status_reason": signal.status_reason,
        "signal_action": signal_block["signal_action"],
        "signal_state": signal_block["signal_state"],
        "trade_action": lifecycle["trade_action"],
        "management_posture": management["action"],
        "management_reason_code": management["reason_code"],
        "should_exit_signal": management["should_exit_signal"],
    })


def _count_signal_observation(
    signal: SignalSchema,
    *,
    stage_counts: Counter[str],
    status_counts: Counter[str],
    management_posture_counts: Counter[str],
    trade_action_counts: Counter[str],
) -> bool:
    downstream = _downstream_meta(signal)
    management = downstream["management"]
    lifecycle = downstream["lifecycle"]
    stage_counts[signal.stage.value] += 1
    status_counts[signal.status.value] += 1
    management_posture_counts[str(management["action"])] += 1
    trade_action_counts[str(lifecycle["trade_action"])] += 1
    return bool(management["should_exit_signal"])



def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _args(argv)
    trading_day = date.fromisoformat(args.date)
    symbols = _symbols(args.symbols)
    lifecycle = SIGNAL_CONFIG.default_lifecycle.strip().upper()
    if args.clear_data and args.reset_symbol_day:
        raise ValueError("Choose either --clear-data or --reset-symbol-day, not both")
    report_dir = Path(args.report_dir)
    log_file = args.log_file or str(report_dir / "replay_signal_generator.log")
    setup_logging(log_file=log_file)
    database_name = _configured_database_name()
    global logger
    logger = logging.getLogger(__name__)

    cleared = {"signals": 0, "opportunities": 0}
    if args.clear_data:
        cleared = _clear_data()
    elif args.reset_symbol_day:
        cleared = _reset_symbol_day(
            trading_day=trading_day,
            symbols=symbols,
            lifecycle=lifecycle,
        )
    logger.info(
        "Resolved replay configuration | database=%s date=%s symbols=%s "
        "clear_data=%s reset_symbol_day=%s batch_size=%s report_dir=%s",
        database_name,
        trading_day,
        symbols,
        bool(args.clear_data),
        bool(args.reset_symbol_day),
        max(1, int(args.batch_size)),
        report_dir,
    )
    snapshots = _load_snapshots(
        trading_day=trading_day,
        symbols=symbols,
        batch_size=max(1, int(args.batch_size)),
    )
    if not snapshots:
        raise RuntimeError(
            f"No snapshots found for date={trading_day} symbols={symbols}"
        )

    event_rows: List[Dict[str, Any]] = []
    evaluation_rows: List[Dict[str, Any]] = []
    failure_rows: List[Dict[str, Any]] = []
    action_counts: Counter[str] = Counter()
    evaluation_outcome_counts: Counter[str] = Counter()
    advisor_action_counts: Counter[str] = Counter()
    advisor_context_presence_counts: Counter[str] = Counter()
    market_regime_availability_counts: Counter[str] = Counter()
    market_regime_state_counts: Counter[str] = Counter()
    market_regime_influence_counts: Counter[str] = Counter()
    stage_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    management_posture_counts: Counter[str] = Counter()
    trade_action_counts: Counter[str] = Counter()
    should_exit_signal_count = 0
    total_snapshots = len(snapshots)
    symbol_totals = Counter(snapshot.symbol for snapshot in snapshots)
    symbol_progress: Counter[str] = Counter()
    permission_matrix = StructuralPermissionMatrix()
    for index, snapshot in enumerate(snapshots, start=1):
        symbol_progress[snapshot.symbol] += 1
        progress = (
            f"[{index}/{total_snapshots}] "
            f"symbol={snapshot.symbol} "
            f"symbol_snapshot={symbol_progress[snapshot.symbol]}/{symbol_totals[snapshot.symbol]} "
            f"snapshot_time={snapshot.snapshot_time.isoformat()}"
        )
        print(progress, flush=True)
        logger.info("signal_replay_progress %s", progress)
        signal_snapshot = snapshot
        failure_stage = "SNAPSHOT_PERMISSION_VALIDATION"
        try:
            if snapshot.auction.balance is None:
                raise ValueError(
                    "Snapshot Auction balance projection missing"
                )
            permission_matrix.validate_persisted(
                balance_state=snapshot.auction.balance.current_state,
                events=snapshot.auction.events,
                permissions=tuple(snapshot.auction.permissions),
            )
            failure_stage = "SIGNAL_GENERATOR"
            generator = SignalGenerator(signal_snapshot)
            generated_events = generator.generate_events()
        except Exception as exc:
            logger.exception(
                "signal_replay_record_failed | symbol=%s snapshot_time=%s stage=%s",
                signal_snapshot.symbol,
                signal_snapshot.snapshot_time,
                failure_stage,
            )
            action_counts["FAILED"] += 1
            failure_rows.append(sanitize_json({
                "index": index,
                "symbol": signal_snapshot.symbol,
                "snapshot_time": signal_snapshot.snapshot_time,
                "stage": failure_stage,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            }))
            event_rows.append(sanitize_json({
                "index": index,
                "symbol": signal_snapshot.symbol,
                "snapshot_time": signal_snapshot.snapshot_time,
                "auction_action": ",".join(
                    event.event_type.value for event in signal_snapshot.auction.events
                ) or "NO_EVENT",
                "signal_action": "FAILED",
                "record_failure": True,
                "failure_type": type(exc).__name__,
                "failure_message": str(exc),
            }))
            continue
        generated_actions = [action for action, _signal in generated_events]
        if generated_actions:
            action_counts.update(generated_actions)
            action_name = ",".join(generated_actions)
        else:
            action_counts["NO_ACTION"] += 1
            action_name = "NO_ACTION"
        for diagnostic in generator.assembler.last_evaluation_diagnostics:
            evaluation_outcome_counts[str(diagnostic["outcome"])] += 1
            advisor_action = diagnostic["advisor_action"]
            context_fields = flatten_advisor_context_for_csv(
                advisor_action=advisor_action,
                advisor_diagnostics=diagnostic["advisor_diagnostics"],
            )
            if advisor_action is not None:
                advisor_action_counts[str(advisor_action)] += 1
                advisor_context_presence_counts[
                    "PRESENT" if context_fields["advisor_context_present"] else "MISSING"
                ] += 1
                market_regime_availability_counts[
                    str(context_fields["market_regime_availability"])
                ] += 1
                market_regime_state_counts[
                    str(context_fields["market_regime_state"])
                ] += 1
                market_regime_influence_counts[
                    str(context_fields["market_regime_influence"])
                ] += 1
            evaluation_rows.append(sanitize_json({
                "index": index,
                "symbol": signal_snapshot.symbol,
                "snapshot_time": signal_snapshot.snapshot_time,
                "source_event_id": diagnostic["source_event_id"],
                "source_event_type": diagnostic["source_event_type"],
                "source_episode_id": diagnostic["source_episode_id"],
                "setup_family": diagnostic["setup_family"],
                "side": diagnostic["side"],
                "structural_result": diagnostic["structural_result"],
                "approved": diagnostic["approved"],
                "candidate_id": diagnostic["candidate_id"],
                "blockers": json.dumps(diagnostic["blockers"]),
                "reason_codes": json.dumps(diagnostic["reason_codes"]),
                "manager_reason_codes": json.dumps(diagnostic["manager_reason_codes"]),
                "advisor_action": advisor_action,
                "advisor_reason_codes": json.dumps(diagnostic["advisor_reason_codes"]),
                "outcome": diagnostic["outcome"],
                **context_fields,
            }))
        directional_projection = signal_snapshot.auction.directional
        balance_projection = signal_snapshot.auction.balance
        if directional_projection is None or balance_projection is None:
            raise ValueError("Snapshot Auction projection missing")
        authoritative_event_ids = [event.event_id for event in signal_snapshot.auction.events]
        authoritative_event_types = [
            event.event_type.value for event in signal_snapshot.auction.events
        ]
        permission_results = [
            f"{permission.setup_family.value}:{permission.result.value}"
            for permission in signal_snapshot.auction.permissions
        ]
        latest_signal = _latest_signal_for_symbol(
            trading_day=trading_day,
            symbol=signal_snapshot.symbol,
            lifecycle=lifecycle,
        )
        if latest_signal is not None and not _same_replay_snapshot_time(
            latest_signal.last_snapshot_time,
            signal_snapshot.snapshot_time,
        ):
            latest_signal = None
        signal_stage = latest_signal.stage.value if latest_signal is not None else None
        signal_status = latest_signal.status.value if latest_signal is not None else None
        signal_reason = latest_signal.status_reason if latest_signal is not None else None
        latest_lifecycle_action = None
        latest_alignment = None
        management_posture = None
        management_reason_code = None
        trail_mode = None
        exit_pressure = None
        target_expansion_allowed = None
        should_exit_signal = None
        downstream_trade_action = None
        downstream_signal_state = None
        downstream_contract_version = None
        setup_reference_price = None
        if latest_signal is not None:
            meta = latest_signal.meta_json
            if not isinstance(meta, dict) or "signal_lifecycle" not in meta:
                raise ValueError(
                    f"Signal {latest_signal.signal_id} missing signal_lifecycle metadata"
                )
            lifecycle_payload = meta["signal_lifecycle"]
            if not isinstance(lifecycle_payload, dict):
                raise ValueError("signal_lifecycle metadata must be an object")
            latest_lifecycle_action = lifecycle_payload["signal_action"]
            latest_alignment = lifecycle_payload.get("directional_alignment")
            downstream = _downstream_meta(latest_signal)
            management = downstream["management"]
            lifecycle_block = downstream["lifecycle"]
            signal_block = downstream["signal"]
            setup_levels = downstream["setup_levels"]
            downstream_contract_version = downstream["downstream_contract"]["version"]
            management_posture = management["action"]
            management_reason_code = management["reason_code"]
            trail_mode = management["trail_mode"]
            exit_pressure = management["exit_pressure"]
            target_expansion_allowed = management["target_expansion_allowed"]
            should_exit_signal = management["should_exit_signal"]
            downstream_trade_action = lifecycle_block["trade_action"]
            downstream_signal_state = signal_block["signal_state"]
            setup_reference_price = setup_levels["reference_price"]
        observed_signals = [signal for _action, signal in generated_events]
        if not observed_signals and latest_signal is not None:
            observed_signals = [latest_signal]
        for observed_signal in observed_signals:
            if _count_signal_observation(
                observed_signal,
                stage_counts=stage_counts,
                status_counts=status_counts,
                management_posture_counts=management_posture_counts,
                trade_action_counts=trade_action_counts,
            ):
                should_exit_signal_count += 1
        transition_records = [
            _transition_record(action, signal)
            for action, signal in generated_events
        ]
        displaced_records = [
            record for record in transition_records
            if record["action"] in {"CLOSE", "REPLACE"}
        ]
        event_rows.append(sanitize_json({
            "index": index,
            "symbol": signal_snapshot.symbol,
            "snapshot_time": signal_snapshot.snapshot_time,
            "auction_action": ",".join(authoritative_event_types) if authoritative_event_types else "NO_EVENT",
            "auction_state": (
                directional_projection.direction.value
                if directional_projection.active_episode_id is not None
                else "NONE"
            ),
            "balance_state": balance_projection.current_state.value,
            "authoritative_event_ids": json.dumps(authoritative_event_ids),
            "authoritative_event_types": json.dumps(authoritative_event_types),
            "permission_results": json.dumps(permission_results),
            "permission_source": "SNAPSHOT_PERSISTED",
            "selected_opportunity_key": (
                latest_signal.meta_json["auction_signal"]["opportunity_key"]
                if latest_signal is not None else None
            ),
            "selected_candidate_id": (
                latest_signal.meta_json["auction_signal"]["candidate_id"]
                if latest_signal is not None else None
            ),
            "signal_action": action_name,
            "generated_signal_transitions": json.dumps(transition_records, default=str),
            "displaced_signal_ids": json.dumps([record["signal_id"] for record in displaced_records]),
            "displaced_signal_actions": json.dumps([record["action"] for record in displaced_records]),
            "displaced_trade_actions": json.dumps([record["trade_action"] for record in displaced_records]),
            "persisted_signal_action": latest_lifecycle_action,
            "signal_stage": signal_stage,
            "signal_status": signal_status,
            "signal_status_reason": signal_reason,
            "directional_alignment": latest_alignment,
            "downstream_contract_version": downstream_contract_version,
            "downstream_signal_state": downstream_signal_state,
            "downstream_trade_action": downstream_trade_action,
            "management_posture": management_posture,
            "management_reason_code": management_reason_code,
            "trail_mode": trail_mode,
            "exit_pressure": exit_pressure,
            "target_expansion_allowed": target_expansion_allowed,
            "should_exit_signal": should_exit_signal,
            "setup_reference_price": setup_reference_price,
        }))

    signals = _signal_rows(
        trading_day=trading_day,
        symbols=symbols,
        lifecycle=lifecycle,
    )
    opportunities = _opportunity_rows(
        trading_day=trading_day,
        symbols=symbols,
    )
    opportunity_state_counts = Counter(
        str(row["lifecycle_state"]) for row in opportunities
    )
    stamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
    prefix = report_dir / f"signal_replay_{trading_day}_{stamp}"
    _write_csv(prefix.with_name(prefix.name + "_lifecycle.csv"), event_rows)
    _write_csv(prefix.with_name(prefix.name + "_evaluations.csv"), evaluation_rows)
    _write_csv(prefix.with_name(prefix.name + "_signals.csv"), signals)
    _write_csv(prefix.with_name(prefix.name + "_opportunities.csv"), opportunities)
    _write_csv(prefix.with_name(prefix.name + "_failures.csv"), failure_rows)

    summary = sanitize_json({
        "trading_day": trading_day,
        "database_name": database_name,
        "database_writes": True,
        "symbols": symbols,
        "lifecycle": lifecycle,
        "snapshots": len(snapshots),
        "first_snapshot_time": snapshots[0].snapshot_time,
        "last_snapshot_time": snapshots[-1].snapshot_time,
        "clear_data": bool(args.clear_data),
        "reset_symbol_day": bool(args.reset_symbol_day),
        "cleared": cleared,
        "signal_action_counts": dict(sorted(action_counts.items())),
        "setup_evaluation_outcome_counts": dict(sorted(evaluation_outcome_counts.items())),
        "advisor_evaluations": sum(advisor_action_counts.values()),
        "advisor_action_counts": dict(sorted(advisor_action_counts.items())),
        "advisor_context_presence_counts": dict(
            sorted(advisor_context_presence_counts.items())
        ),
        "market_regime_availability_counts": dict(
            sorted(market_regime_availability_counts.items())
        ),
        "market_regime_state_counts": dict(
            sorted(market_regime_state_counts.items())
        ),
        "market_regime_influence_counts": dict(
            sorted(market_regime_influence_counts.items())
        ),
        "signal_stage_observation_counts": dict(sorted(stage_counts.items())),
        "signal_status_observation_counts": dict(sorted(status_counts.items())),
        "management_posture_counts": dict(
            sorted(management_posture_counts.items())
        ),
        "downstream_trade_action_counts": dict(sorted(trade_action_counts.items())),
        "should_exit_signal_observations": should_exit_signal_count,
        "signals_persisted": len(signals),
        "opportunities_persisted": len(opportunities),
        "record_failures": len(failure_rows),
        "opportunity_state_counts": dict(sorted(opportunity_state_counts.items())),
        "snapshots_marked_processed": 0,
    })
    prefix.with_name(prefix.name + "_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    _write_csv(prefix.with_name(prefix.name + "_summary.csv"), [{
        **summary,
        "symbols": json.dumps(summary["symbols"]),
        "cleared": json.dumps(summary["cleared"], sort_keys=True),
        "signal_action_counts": json.dumps(summary["signal_action_counts"], sort_keys=True),
        "setup_evaluation_outcome_counts": json.dumps(
            summary["setup_evaluation_outcome_counts"], sort_keys=True
        ),
        "advisor_action_counts": json.dumps(
            summary["advisor_action_counts"], sort_keys=True
        ),
        "advisor_context_presence_counts": json.dumps(
            summary["advisor_context_presence_counts"], sort_keys=True
        ),
        "market_regime_availability_counts": json.dumps(
            summary["market_regime_availability_counts"], sort_keys=True
        ),
        "market_regime_state_counts": json.dumps(
            summary["market_regime_state_counts"], sort_keys=True
        ),
        "market_regime_influence_counts": json.dumps(
            summary["market_regime_influence_counts"], sort_keys=True
        ),
        "signal_stage_observation_counts": json.dumps(
            summary["signal_stage_observation_counts"], sort_keys=True
        ),
        "signal_status_observation_counts": json.dumps(
            summary["signal_status_observation_counts"], sort_keys=True
        ),
        "management_posture_counts": json.dumps(
            summary["management_posture_counts"], sort_keys=True
        ),
        "downstream_trade_action_counts": json.dumps(
            summary["downstream_trade_action_counts"], sort_keys=True
        ),
        "opportunity_state_counts": json.dumps(
            summary["opportunity_state_counts"], sort_keys=True
        ),
    }])

    logger.info("Auction SignalGenerator replay complete | %s", summary)
    logger.info("Reports: %s_*", prefix)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
