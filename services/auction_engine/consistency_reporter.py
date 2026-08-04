"""Read-only Auction consistency CSV reporting over persisted snapshots.

This module deliberately does not replay Auction, mutate snapshots, mark rows
processed, create opportunities/signals, or use the machine clock for any
historical decision.  It only flattens the authoritative persisted snapshot
state and adds neutral comparison fields that make corpus-wide review easier.
"""

from __future__ import annotations

from collections import Counter
import csv
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from enum import Enum
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

if TYPE_CHECKING:
    from schemas.snapshot import SnapshotSchema

logger = logging.getLogger(__name__)


REPORT_FIELDS: Tuple[str, ...] = (
    # Run provenance and record status.
    "experiment_id",
    "dataset_split",
    "code_commit",
    "config_hash",
    "record_status",
    "error_stage",
    "error_type",
    "error_message",
    # Snapshot identity.
    "symbol",
    "trading_date",
    "snapshot_time",
    "previous_snapshot_time",
    "snapshot_version",
    "tf",
    "gen_signals",
    "db_processed",
    "ltp",
    "ltp_time",
    # Market facts.
    "bar_open",
    "bar_high",
    "bar_low",
    "bar_close",
    "bar_volume",
    "atr",
    "raw_structure_state",
    "raw_structure_side",
    "raw_structure_reason",
    "accepted_structure_state",
    "accepted_range_id",
    "accepted_range_low",
    "accepted_range_high",
    "accepted_structure_age_bars",
    "slope_status",
    "slope_state",
    "slope_3_atr_per_bar",
    "slope_5_atr_per_bar",
    # Auction identity and current objective observation.
    "auction_status",
    "continuity_mode",
    "engine_name",
    "engine_version",
    "auction_config_version",
    "auction_config_hash",
    "observation_state",
    "directional_bias",
    "trend_direction",
    "current_leg_mature",
    "extension_mature",
    "exhaustion_active",
    "exhausted_side",
    "rejection_observed",
    "failed_extreme_observed",
    "structural_failure_confirmed",
    "trend_protection_level",
    "trend_protection_source",
    "trend_protection_time",
    "observation_range_id",
    "observation_range_low",
    "observation_range_high",
    "observation_range_inside",
    "observation_range_position",
    "directional_efficiency",
    "directional_efficiency_source",
    "overlap_ratio",
    "observation_reason_codes",
    # Public directional projection.
    "episode_id",
    "episode_previous_state",
    "episode_current_state",
    "episode_direction",
    "episode_origin_source",
    "parent_episode_id",
    "origin_event_id",
    "episode_started_at",
    "episode_state_started_at",
    "episode_state_age_bars",
    "episode_origin_price",
    "episode_extreme_price",
    "episode_extreme_time",
    "episode_protection_level",
    "episode_protection_source",
    "reversal_confirmation_level",
    "reversal_confirmation_source",
    "reversal_confirmation_time",
    "reversal_progress_bars",
    "reversal_progress_atr",
    "reversal_failure_closes",
    "continuation_failure_seen",
    "episode_reason_codes",
    # Private continuity counters useful for explaining transitions.
    "memory_directional_sequence",
    "memory_start_candidate_side",
    "memory_start_candidate_bars",
    "memory_reversal_watch_age_bars",
    "memory_trend_restore_bars",
    "memory_opposite_control_bars",
    "memory_inactive_bars",
    "memory_last_observation_state",
    "memory_last_observation_state_time",
    "memory_last_reason_codes",
    # Balance projection.
    "balance_episode_id",
    "balance_previous_state",
    "balance_current_state",
    "balance_state_age_bars",
    "balance_range_id",
    "balance_candidate_low",
    "balance_candidate_high",
    "balance_frozen_low",
    "balance_frozen_high",
    "balance_escape_direction",
    "balance_outside_close_count",
    "balance_reentry_close_count",
    "balance_escape_attempt_count",
    "balance_failed_escape_count",
    "balance_rearm_required",
    "balance_attempt_limit_reached",
    "balance_reason_codes",
    # Events and setup permissions.
    "event_count",
    "event_ids",
    "event_types",
    "event_directions",
    "event_episode_ids",
    "event_reason_codes",
    "permission_count",
    "setup_families",
    "permission_results",
    "permission_source_event_ids",
    "permission_reason_codes",
    # Neutral comparisons. These are diagnostics, not decision authority.
    "episode_vs_trend",
    "episode_vs_raw_structure",
    "episode_vs_slope",
    "trend_vs_raw_structure",
    "fresh_opposite_evidence",
    "opposite_evidence_streak",
    "episode_changed",
    "direction_changed",
    "state_changed",
    "lineage_complete",
    "consistency_flags",
    "consistency_class",
    # Full persisted detail for follow-up without another DB query.
    "events_json",
    "permissions_json",
    "observation_json",
    "directional_json",
    "balance_json",
    "auction_diagnostics_json",
    "auction_memory_json",
)



SUMMARY_FIELDS: Tuple[str, ...] = (
    "trading_date",
    "symbol",
    "snapshot_count",
    "ok_count",
    "error_count",
    "unassessable_count",
    "coherent_count",
    "transitional_conflict_count",
    "persistent_conflict_count",
    "hard_contract_failure_count",
    "transitional_conflict_incidents",
    "persistent_conflict_incidents",
    "transition_explained_incidents",
    "unresolved_conflict_incidents",
    "unresolved_transitional_conflict_incidents",
    "unresolved_persistent_conflict_incidents",
    "hard_failure_incidents",
    "directional_start_conflicts",
    "directional_maturity_conflicts",
    "lineage_anomalies",
    "episode_count",
    "event_count",
    "max_opposite_evidence_streak",
    "finding_codes",
    "review_priority",
    "needs_review",
    "detail_file",
)

_DIRECTION_UP = "UP"
_DIRECTION_DOWN = "DOWN"
_DIRECTIONAL_VALUES = {_DIRECTION_UP, _DIRECTION_DOWN}


@dataclass(frozen=True)
class ConsistencyReportSummary:
    output_path: Path
    rows_written: int
    error_rows: int
    symbols_seen: Tuple[str, ...]


@dataclass(frozen=True)
class _RawSnapshotRow:
    symbol: str
    snapshot_time: datetime
    ltp: Any
    ltp_time: Optional[datetime]
    data: Any
    processed: bool


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().upper() in {"1", "TRUE", "YES", "Y"}


def _event_type_set(row: Mapping[str, Any]) -> set[str]:
    return {
        token.strip().upper()
        for token in str(row.get("event_types") or "").split("|")
        if token.strip()
    }


def _transition_explains_conflict(row: Mapping[str, Any]) -> bool:
    """Return True when the persisted row shows an explicit legal handoff path.

    This is intentionally conservative and diagnostic. It does not assert that
    the transition is correct; it only prevents a resolved handoff from being
    counted as an unresolved conflict incident in the all-symbol summary.
    """

    state = str(row.get("episode_current_state") or "").strip().upper()
    events = _event_type_set(row)
    if state == "COMPLETED" and any(event.startswith("DIRECTIONAL_") for event in events):
        return True

    resolution_markers = (
        "DIRECTIONAL_TREND_RESTORED",
        "DIRECTIONAL_COMPLETED",
        "DIRECTIONAL_REVERSAL_LEG_STARTED",
        "DIRECTIONAL_REVERSAL_LEG_CONFIRMED",
        "DIRECTIONAL_REVERSAL_LEG_ESTABLISHED",
        "BALANCE_ESCAPE_ACCEPTED",
        "BALANCE_ESCAPE_FAILED",
    )
    return any(
        event == marker or event.endswith(marker)
        for event in events
        for marker in resolution_markers
    )


class _SymbolSummaryAccumulator:
    """Incrementally aggregate one symbol without retaining wide snapshot rows."""

    def __init__(
        self,
        *,
        trading_day: date,
        symbol: str,
        detail_file: str = "",
    ) -> None:
        self.trading_day = trading_day
        self.symbol = str(symbol).strip().upper()
        self.detail_file = detail_file
        self.class_counts: Counter[str] = Counter()
        self.status_counts: Counter[str] = Counter()
        self.episode_ids: set[str] = set()
        self.finding_codes: set[str] = set()
        self.snapshot_count = 0
        self.event_count = 0
        self.max_opposite_streak = 0
        self.transitional_conflict_incidents = 0
        self.persistent_conflict_incidents = 0
        self.transition_explained_incidents = 0
        self.unresolved_conflict_incidents = 0
        self.unresolved_transitional_conflict_incidents = 0
        self.unresolved_persistent_conflict_incidents = 0
        self.hard_failure_incidents = 0
        self.directional_start_conflicts = 0
        self.directional_maturity_conflicts = 0
        self.lineage_anomalies = 0
        self._conflict_active = False
        self._conflict_persistent = False
        self._conflict_explained = False
        self._hard_active = False

    def _finish_conflict(self) -> None:
        if not self._conflict_active:
            return
        if self._conflict_persistent:
            self.persistent_conflict_incidents += 1
        else:
            self.transitional_conflict_incidents += 1
        if self._conflict_explained:
            self.transition_explained_incidents += 1
        else:
            self.unresolved_conflict_incidents += 1
            if self._conflict_persistent:
                self.unresolved_persistent_conflict_incidents += 1
            else:
                self.unresolved_transitional_conflict_incidents += 1
        self._conflict_active = False
        self._conflict_persistent = False
        self._conflict_explained = False

    def add(self, row: Mapping[str, Any]) -> None:
        self.snapshot_count += 1
        status = str(row.get("record_status") or "").strip().upper()
        consistency_class = str(row.get("consistency_class") or "").strip().upper()
        self.status_counts[status] += 1
        self.class_counts[consistency_class] += 1

        episode_id = str(row.get("episode_id") or "").strip()
        if episode_id:
            self.episode_ids.add(episode_id)

        raw_event_count = row.get("event_count")
        try:
            self.event_count += int(raw_event_count or 0)
        except (TypeError, ValueError):
            self.finding_codes.add("INVALID_EVENT_COUNT")

        raw_streak = row.get("opposite_evidence_streak")
        try:
            streak = int(raw_streak or 0)
        except (TypeError, ValueError):
            streak = 0
            self.finding_codes.add("INVALID_OPPOSITE_EVIDENCE_STREAK")
        self.max_opposite_streak = max(self.max_opposite_streak, streak)

        flags = {
            code.strip()
            for code in str(row.get("consistency_flags") or "").split("|")
            if code.strip()
        }
        self.finding_codes.update(flags)
        events = _event_type_set(row)
        fresh_opposite = _as_bool(row.get("fresh_opposite_evidence"))

        if fresh_opposite:
            if not self._conflict_active:
                self._conflict_active = True
            self._conflict_persistent = (
                self._conflict_persistent
                or consistency_class == "PERSISTENT_CONFLICT"
                or streak >= 3
            )
            self._conflict_explained = (
                self._conflict_explained or _transition_explains_conflict(row)
            )
        elif self._conflict_active:
            self._conflict_explained = (
                self._conflict_explained or _transition_explains_conflict(row)
            )
            self._finish_conflict()

        hard_now = consistency_class == "HARD_CONTRACT_FAILURE"
        if hard_now and not self._hard_active:
            self.hard_failure_incidents += 1
        self._hard_active = hard_now

        if fresh_opposite and "DIRECTIONAL_STARTED" in events:
            self.directional_start_conflicts += 1
            self.finding_codes.add("DIRECTIONAL_START_CONFLICT")
        if fresh_opposite and "DIRECTIONAL_MATURED" in events:
            self.directional_maturity_conflicts += 1
            self.finding_codes.add("DIRECTIONAL_MATURITY_CONFLICT")
        if "REVERSAL_LINEAGE_MISSING" in flags:
            self.lineage_anomalies += 1

    def result(self) -> Dict[str, Any]:
        self._finish_conflict()

        if (
            self.hard_failure_incidents > 0
            or self.status_counts["ERROR"] > 0
            or self.lineage_anomalies > 0
        ):
            review_priority = "P1"
        elif (
            self.unresolved_persistent_conflict_incidents > 0
            or self.directional_start_conflicts > 0
            or self.directional_maturity_conflicts > 0
        ):
            review_priority = "P2"
        elif (
            self.transition_explained_incidents > 0
            or self.unresolved_transitional_conflict_incidents > 0
            or self.class_counts["TRANSITIONAL_CONFLICT"] > 0
            or self.class_counts["UNASSESSABLE"] > 0
        ):
            review_priority = "P3"
        else:
            review_priority = "NONE"

        return {
            "trading_date": self.trading_day.isoformat(),
            "symbol": self.symbol,
            "snapshot_count": self.snapshot_count,
            "ok_count": self.status_counts["OK"],
            "error_count": self.status_counts["ERROR"],
            "unassessable_count": self.class_counts["UNASSESSABLE"],
            "coherent_count": self.class_counts["COHERENT"],
            "transitional_conflict_count": self.class_counts["TRANSITIONAL_CONFLICT"],
            "persistent_conflict_count": self.class_counts["PERSISTENT_CONFLICT"],
            "hard_contract_failure_count": self.class_counts["HARD_CONTRACT_FAILURE"],
            "transitional_conflict_incidents": self.transitional_conflict_incidents,
            "persistent_conflict_incidents": self.persistent_conflict_incidents,
            "transition_explained_incidents": self.transition_explained_incidents,
            "unresolved_conflict_incidents": self.unresolved_conflict_incidents,
            "unresolved_transitional_conflict_incidents": self.unresolved_transitional_conflict_incidents,
            "unresolved_persistent_conflict_incidents": self.unresolved_persistent_conflict_incidents,
            "hard_failure_incidents": self.hard_failure_incidents,
            "directional_start_conflicts": self.directional_start_conflicts,
            "directional_maturity_conflicts": self.directional_maturity_conflicts,
            "lineage_anomalies": self.lineage_anomalies,
            "episode_count": len(self.episode_ids),
            "event_count": self.event_count,
            "max_opposite_evidence_streak": self.max_opposite_streak,
            "finding_codes": "|".join(sorted(self.finding_codes)),
            "review_priority": review_priority,
            "needs_review": review_priority in {"P1", "P2"},
            "detail_file": self.detail_file,
        }


def _value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return "|".join(str(_value(item)) for item in value)
    if isinstance(value, list):
        return "|".join(str(_value(item)) for item in value)
    return value


def _json_payload(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "model_dump"):
        payload = value.model_dump(mode="json", exclude_none=False)
    elif isinstance(value, Mapping):
        payload = dict(value)
    else:
        payload = value
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _direction(value: Any) -> str:
    text = str(_value(value) or "").strip().upper()
    return text if text in _DIRECTIONAL_VALUES else ""


def _raw_side_direction(value: Any) -> str:
    text = str(_value(value) or "").strip().upper()
    if text in {"BUY", "UP", "BULLISH"}:
        return _DIRECTION_UP
    if text in {"SELL", "DOWN", "BEARISH"}:
        return _DIRECTION_DOWN
    return ""


def _slope_direction(value: Any) -> str:
    text = str(_value(value) or "").strip().upper()
    if "UP" in text and "DOWN" not in text:
        return _DIRECTION_UP
    if "DOWN" in text and "UP" not in text:
        return _DIRECTION_DOWN
    return ""


def _comparison(left: str, right: str) -> str:
    if not left or not right:
        return "UNASSESSABLE"
    return "MATCH" if left == right else "CONFLICT"


def _pipe(values: Iterable[Any]) -> str:
    return "|".join(str(_value(value)) for value in values)


def _event_reason_codes(events: Sequence[Any]) -> str:
    return "|".join(
        f"{_value(event.event_type)}:{','.join(str(code) for code in event.reason_codes)}"
        for event in events
    )


def _permission_reason_codes(permissions: Sequence[Any]) -> str:
    return "|".join(
        f"{_value(permission.setup_family)}:{','.join(str(code) for code in permission.reason_codes)}"
        for permission in permissions
    )


def _empty_row(**values: Any) -> Dict[str, Any]:
    row = {field: "" for field in REPORT_FIELDS}
    row.update({key: _value(value) for key, value in values.items() if key in row})
    return row


def build_report_row(
    snapshot: "SnapshotSchema",
    *,
    db_processed: bool,
    experiment_id: str,
    dataset_split: str,
    code_commit: str,
    config_hash: str,
    previous: Optional[Dict[str, str]],
    previous_opposite_streak: int,
) -> Tuple[Dict[str, Any], Dict[str, str], int]:
    """Build one neutral report row and return state for the next snapshot."""

    row = _empty_row(
        experiment_id=experiment_id,
        dataset_split=dataset_split,
        code_commit=code_commit,
        config_hash=config_hash,
        record_status="OK",
        symbol=snapshot.symbol,
        trading_date=snapshot.snapshot_time.date(),
        snapshot_time=snapshot.snapshot_time,
        previous_snapshot_time=snapshot.auction.previous_snapshot_time,
        snapshot_version=snapshot.version,
        tf=snapshot.tf,
        gen_signals=snapshot.gen_signals,
        db_processed=db_processed,
        ltp=snapshot.ltp,
        ltp_time=snapshot.ltp_time,
        bar_open=snapshot.bar.open,
        bar_high=snapshot.bar.high,
        bar_low=snapshot.bar.low,
        bar_close=snapshot.bar.close,
        bar_volume=snapshot.bar.volume,
        atr=snapshot.indicators.atr.value,
        raw_structure_state=snapshot.structure.raw.state,
        raw_structure_side=snapshot.structure.raw.side,
        raw_structure_reason=snapshot.structure.raw.reason,
        accepted_structure_state=snapshot.structure.accepted.state,
        accepted_range_id=snapshot.structure.accepted.range.range_id,
        accepted_range_low=snapshot.structure.accepted.range.low,
        accepted_range_high=snapshot.structure.accepted.range.high,
        accepted_structure_age_bars=snapshot.structure.accepted.age_bars,
        slope_status=snapshot.price_action.slope.status,
        slope_state=snapshot.price_action.slope.state,
        slope_3_atr_per_bar=snapshot.price_action.slope.bars_3_atr_per_bar,
        slope_5_atr_per_bar=snapshot.price_action.slope.bars_5_atr_per_bar,
        auction_status=snapshot.auction.status,
        continuity_mode=snapshot.auction.continuity_mode,
    )

    if snapshot.auction.status != "OK":
        row["record_status"] = "UNASSESSABLE"
        row["consistency_class"] = "UNASSESSABLE"
        row["consistency_flags"] = "AUCTION_NOT_RUN"
        next_state = {"episode_id": "", "direction": "", "state": ""}
        return row, next_state, 0

    observation = snapshot.auction.observation
    lifecycle = snapshot.auction.lifecycle
    if observation is None or lifecycle is None:
        raise ValueError("Auction status OK requires observation and lifecycle")

    directional = lifecycle.directional
    balance = lifecycle.balance
    memory = snapshot.memory.auction.directional
    events = tuple(lifecycle.events)
    permissions = tuple(lifecycle.permissions)

    engine = snapshot.auction.engine
    row.update({
        "engine_name": _value(engine.name if engine else lifecycle.engine_name),
        "engine_version": _value(engine.version if engine else lifecycle.engine_version),
        "auction_config_version": _value(lifecycle.config_version),
        "auction_config_hash": _value(lifecycle.config_hash),
        "observation_state": _value(observation.observation_state),
        "directional_bias": _value(observation.directional_bias),
        "trend_direction": _value(observation.trend_direction),
        "current_leg_mature": observation.current_leg_mature,
        "extension_mature": observation.extension_mature,
        "exhaustion_active": observation.exhaustion_active,
        "exhausted_side": _value(observation.exhausted_side),
        "rejection_observed": observation.rejection_observed,
        "failed_extreme_observed": observation.failed_extreme_observed,
        "structural_failure_confirmed": observation.structural_failure_confirmed,
        "trend_protection_level": observation.trend_protection_level,
        "trend_protection_source": observation.trend_protection_source,
        "trend_protection_time": _value(observation.trend_protection_time),
        "observation_range_id": observation.accepted_range_id,
        "observation_range_low": observation.accepted_range_low,
        "observation_range_high": observation.accepted_range_high,
        "observation_range_inside": observation.accepted_range_inside,
        "observation_range_position": observation.accepted_range_position,
        "directional_efficiency": observation.directional_efficiency,
        "directional_efficiency_source": _value(observation.directional_efficiency_source),
        "overlap_ratio": observation.overlap_ratio,
        "observation_reason_codes": _pipe(observation.source_reason_codes),
        "episode_id": directional.episode_id,
        "episode_previous_state": _value(directional.previous_state),
        "episode_current_state": _value(directional.current_state),
        "episode_direction": _value(directional.direction),
        "episode_origin_source": _value(directional.origin_source),
        "parent_episode_id": directional.parent_episode_id,
        "origin_event_id": directional.origin_event_id,
        "episode_started_at": _value(directional.started_at),
        "episode_state_started_at": _value(directional.state_started_at),
        "episode_state_age_bars": directional.state_age_bars,
        "episode_origin_price": directional.origin_price,
        "episode_extreme_price": directional.extreme_price,
        "episode_extreme_time": _value(directional.extreme_time),
        "episode_protection_level": directional.protection_level,
        "episode_protection_source": directional.protection_source,
        "reversal_confirmation_level": directional.reversal_confirmation_level,
        "reversal_confirmation_source": directional.reversal_confirmation_source,
        "reversal_confirmation_time": _value(directional.reversal_confirmation_level_time),
        "reversal_progress_bars": directional.reversal_leg_progress_bars,
        "reversal_progress_atr": directional.reversal_leg_progress_atr,
        "reversal_failure_closes": directional.reversal_leg_failure_closes,
        "continuation_failure_seen": directional.continuation_failure_seen,
        "episode_reason_codes": _pipe(directional.reason_codes),
        "memory_directional_sequence": memory.sequence,
        "memory_start_candidate_side": _value(memory.start_candidate_side),
        "memory_start_candidate_bars": memory.start_candidate_bars,
        "memory_reversal_watch_age_bars": memory.reversal_watch_age_bars,
        "memory_trend_restore_bars": memory.trend_restore_bars,
        "memory_opposite_control_bars": memory.opposite_control_bars,
        "memory_inactive_bars": memory.inactive_bars,
        "memory_last_observation_state": _value(memory.last_observation_state),
        "memory_last_observation_state_time": _value(memory.last_observation_state_time),
        "memory_last_reason_codes": _pipe(memory.last_reason_codes),
        "balance_episode_id": balance.episode_id,
        "balance_previous_state": _value(balance.previous_state),
        "balance_current_state": _value(balance.current_state),
        "balance_state_age_bars": balance.state_age_bars,
        "balance_range_id": balance.range_id,
        "balance_candidate_low": balance.candidate_low,
        "balance_candidate_high": balance.candidate_high,
        "balance_frozen_low": balance.frozen_low,
        "balance_frozen_high": balance.frozen_high,
        "balance_escape_direction": _value(balance.escape_direction),
        "balance_outside_close_count": balance.outside_close_count,
        "balance_reentry_close_count": balance.reentry_close_count,
        "balance_escape_attempt_count": balance.escape_attempt_count,
        "balance_failed_escape_count": balance.failed_escape_count,
        "balance_rearm_required": balance.rearm_required,
        "balance_attempt_limit_reached": balance.attempt_limit_reached,
        "balance_reason_codes": _pipe(balance.reason_codes),
        "event_count": len(events),
        "event_ids": _pipe(event.event_id for event in events),
        "event_types": _pipe(event.event_type for event in events),
        "event_directions": _pipe(event.direction for event in events),
        "event_episode_ids": _pipe(event.episode_id for event in events),
        "event_reason_codes": _event_reason_codes(events),
        "permission_count": len(permissions),
        "setup_families": _pipe(permission.setup_family for permission in permissions),
        "permission_results": _pipe(permission.result for permission in permissions),
        "permission_source_event_ids": _pipe(
            ",".join(permission.source_event_ids) for permission in permissions
        ),
        "permission_reason_codes": _permission_reason_codes(permissions),
        "events_json": _json_payload(events),
        "permissions_json": _json_payload(permissions),
        "observation_json": _json_payload(observation),
        "directional_json": _json_payload(directional),
        "balance_json": _json_payload(balance),
        "auction_diagnostics_json": _json_payload(lifecycle.diagnostics),
        "auction_memory_json": _json_payload(snapshot.memory.auction),
    })

    episode_direction = _direction(directional.direction)
    trend_direction = _direction(observation.trend_direction)
    raw_direction = _raw_side_direction(snapshot.structure.raw.side)
    slope_direction = _slope_direction(snapshot.price_action.slope.state)

    episode_vs_trend = _comparison(episode_direction, trend_direction)
    episode_vs_raw = _comparison(episode_direction, raw_direction)
    episode_vs_slope = _comparison(episode_direction, slope_direction)
    trend_vs_raw = _comparison(trend_direction, raw_direction)

    fresh_opposite = bool(
        episode_direction
        and trend_direction
        and trend_direction != episode_direction
        and raw_direction == trend_direction
        and slope_direction == trend_direction
    )
    opposite_streak = previous_opposite_streak + 1 if fresh_opposite else 0

    previous = previous or {"episode_id": "", "direction": "", "state": ""}
    current_episode_id = str(directional.episode_id or "")
    current_state = str(_value(directional.current_state) or "")
    episode_changed = bool(previous["episode_id"] != current_episode_id)
    direction_changed = bool(previous["direction"] != episode_direction)
    state_changed = bool(previous["state"] != current_state)

    origin_source = str(_value(directional.origin_source) or "")
    reversal_handoff = origin_source == "REVERSAL_EVENT_HANDOFF"
    lineage_complete = not reversal_handoff or bool(
        directional.parent_episode_id and directional.origin_event_id
    )

    flags: List[str] = []
    if episode_vs_trend == "CONFLICT":
        flags.append("EPISODE_TREND_CONFLICT")
    if episode_vs_raw == "CONFLICT":
        flags.append("EPISODE_RAW_STRUCTURE_CONFLICT")
    if episode_vs_slope == "CONFLICT":
        flags.append("EPISODE_SLOPE_CONFLICT")
    if fresh_opposite:
        flags.append("FRESH_OPPOSITE_EVIDENCE")
    if opposite_streak >= 3:
        flags.append("PERSISTENT_OPPOSITE_EVIDENCE")
    if not lineage_complete:
        flags.append("REVERSAL_LINEAGE_MISSING")

    if not lineage_complete:
        consistency_class = "HARD_CONTRACT_FAILURE"
    elif opposite_streak >= 3:
        consistency_class = "PERSISTENT_CONFLICT"
    elif flags:
        consistency_class = "TRANSITIONAL_CONFLICT"
    else:
        consistency_class = "COHERENT"

    row.update({
        "episode_vs_trend": episode_vs_trend,
        "episode_vs_raw_structure": episode_vs_raw,
        "episode_vs_slope": episode_vs_slope,
        "trend_vs_raw_structure": trend_vs_raw,
        "fresh_opposite_evidence": fresh_opposite,
        "opposite_evidence_streak": opposite_streak,
        "episode_changed": episode_changed,
        "direction_changed": direction_changed,
        "state_changed": state_changed,
        "lineage_complete": lineage_complete,
        "consistency_flags": "|".join(flags),
        "consistency_class": consistency_class,
    })

    next_state = {
        "episode_id": current_episode_id,
        "direction": episode_direction,
        "state": current_state,
    }
    return row, next_state, opposite_streak


def _fetch_batch(
    *,
    trading_day: date,
    after_time: Optional[datetime],
    after_symbol: str,
    symbols: Optional[Sequence[str]],
    limit: int,
) -> List[_RawSnapshotRow]:
    from sqlalchemy import and_, or_

    from database.database import get_trades_db
    from models.trade_models import Snapshot as SnapshotORM

    day_start = datetime.combine(trading_day, dtime.min)
    day_end = day_start + timedelta(days=1)
    clean_symbols = sorted({str(symbol).strip().upper() for symbol in symbols or () if str(symbol).strip()})

    with get_trades_db() as db:
        query = (
            db.query(
                SnapshotORM.symbol,
                SnapshotORM.snapshot_time,
                SnapshotORM.ltp,
                SnapshotORM.ltp_time,
                SnapshotORM.data,
                SnapshotORM.processed,
            )
            .filter(SnapshotORM.snapshot_time >= day_start)
            .filter(SnapshotORM.snapshot_time < day_end)
        )
        if clean_symbols:
            query = query.filter(SnapshotORM.symbol.in_(clean_symbols))
        if after_time is not None:
            query = query.filter(or_(
                SnapshotORM.snapshot_time > after_time,
                and_(
                    SnapshotORM.snapshot_time == after_time,
                    SnapshotORM.symbol > str(after_symbol or ""),
                ),
            ))
        rows = (
            query.order_by(SnapshotORM.snapshot_time.asc(), SnapshotORM.symbol.asc())
            .limit(max(1, int(limit)))
            .all()
        )

    return [
        _RawSnapshotRow(
            symbol=str(row.symbol).strip().upper(),
            snapshot_time=row.snapshot_time,
            ltp=row.ltp,
            ltp_time=row.ltp_time,
            data=row.data,
            processed=bool(row.processed),
        )
        for row in rows
    ]


def _parse_snapshot(raw: _RawSnapshotRow) -> "SnapshotSchema":
    from schemas.snapshot import SnapshotSchema

    payload = dict(raw.data or {})
    if not payload:
        raise ValueError("Snapshot data is empty")
    if str(payload.get("symbol") or "").strip().upper() != raw.symbol:
        raise ValueError("Snapshot JSON symbol differs from DB symbol")
    payload_time = payload.get("snapshot_time")
    if isinstance(payload_time, str):
        payload_time = datetime.fromisoformat(payload_time)
    if not isinstance(payload_time, datetime):
        raise ValueError("Snapshot JSON snapshot_time is missing or invalid")
    if payload_time.replace(tzinfo=None) != raw.snapshot_time.replace(tzinfo=None):
        raise ValueError("Snapshot JSON time differs from DB snapshot_time")
    payload["ltp"] = float(raw.ltp) if raw.ltp is not None else None
    payload["ltp_time"] = raw.ltp_time
    return SnapshotSchema.from_db_dict(payload)


def _iter_consistency_rows(
    *,
    trading_day: date,
    symbols: Optional[Sequence[str]],
    experiment_id: str,
    dataset_split: str,
    code_commit: str,
    config_hash: str,
    batch_size: int,
) -> Iterator[Dict[str, Any]]:
    """Yield chronological report rows while isolating per-record failures."""

    previous_state: Dict[str, Dict[str, str]] = {}
    opposite_streak: Dict[str, int] = {}
    after_time: Optional[datetime] = None
    after_symbol = ""

    while True:
        batch = _fetch_batch(
            trading_day=trading_day,
            after_time=after_time,
            after_symbol=after_symbol,
            symbols=symbols,
            limit=batch_size,
        )
        if not batch:
            break

        for raw in batch:
            try:
                snapshot = _parse_snapshot(raw)
                row, next_state, next_streak = build_report_row(
                    snapshot,
                    db_processed=raw.processed,
                    experiment_id=experiment_id,
                    dataset_split=dataset_split,
                    code_commit=code_commit,
                    config_hash=config_hash,
                    previous=previous_state.get(raw.symbol),
                    previous_opposite_streak=opposite_streak.get(raw.symbol, 0),
                )
                previous_state[raw.symbol] = next_state
                opposite_streak[raw.symbol] = next_streak
            except Exception as exc:
                logger.exception(
                    "Auction consistency report failed for %s @ %s",
                    raw.symbol,
                    raw.snapshot_time,
                )
                row = _empty_row(
                    experiment_id=experiment_id,
                    dataset_split=dataset_split,
                    code_commit=code_commit,
                    config_hash=config_hash,
                    record_status="ERROR",
                    error_stage="PARSE_OR_FLATTEN",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    symbol=raw.symbol,
                    trading_date=trading_day,
                    snapshot_time=raw.snapshot_time,
                    db_processed=raw.processed,
                    ltp=raw.ltp,
                    ltp_time=raw.ltp_time,
                    consistency_class="UNASSESSABLE",
                    consistency_flags="RECORD_ERROR",
                )
            yield row

        last = batch[-1]
        after_time = last.snapshot_time
        after_symbol = last.symbol
        if len(batch) < max(1, int(batch_size)):
            break


def generate_consistency_report(
    *,
    trading_day: date,
    output_path: Path,
    symbols: Optional[Sequence[str]] = None,
    experiment_id: str = "auction-consistency-input",
    dataset_split: str = "development",
    code_commit: str = "UNSPECIFIED",
    config_hash: str = "UNSPECIFIED",
    batch_size: int = 500,
    overwrite: bool = False,
) -> ConsistencyReportSummary:
    """Write one chronological, read-only CSV row per persisted snapshot."""

    output_path = Path(output_path)
    if output_path.exists() and not overwrite:
        raise FileExistsError(
            f"Report already exists: {output_path}. Use --overwrite explicitly."
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    symbols_seen: set[str] = set()
    rows_written = 0
    error_rows = 0
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in _iter_consistency_rows(
            trading_day=trading_day,
            symbols=symbols,
            experiment_id=experiment_id,
            dataset_split=dataset_split,
            code_commit=code_commit,
            config_hash=config_hash,
            batch_size=batch_size,
        ):
            writer.writerow(row)
            rows_written += 1
            symbol = str(row.get("symbol") or "").strip().upper()
            if symbol:
                symbols_seen.add(symbol)
            if str(row.get("record_status") or "").strip().upper() == "ERROR":
                error_rows += 1

    return ConsistencyReportSummary(
        output_path=output_path,
        rows_written=rows_written,
        error_rows=error_rows,
        symbols_seen=tuple(sorted(symbols_seen)),
    )


def generate_consistency_summary(
    *,
    trading_day: date,
    summary_path: Path,
    experiment_id: str = "auction-consistency-input",
    dataset_split: str = "development",
    code_commit: str = "UNSPECIFIED",
    config_hash: str = "UNSPECIFIED",
    batch_size: int = 500,
    overwrite: bool = False,
) -> ConsistencyReportSummary:
    """Scan all symbols and write only a compact anomaly summary CSV."""

    summary_path = Path(summary_path)
    if summary_path.exists() and not overwrite:
        raise FileExistsError(
            f"Summary already exists: {summary_path}. Use overwrite explicitly."
        )
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    accumulators: Dict[str, _SymbolSummaryAccumulator] = {}
    rows_written = 0
    error_rows = 0
    for row in _iter_consistency_rows(
        trading_day=trading_day,
        symbols=None,
        experiment_id=experiment_id,
        dataset_split=dataset_split,
        code_commit=code_commit,
        config_hash=config_hash,
        batch_size=batch_size,
    ):
        rows_written += 1
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            logger.error("Skipping summary row without symbol | row=%s", row)
            error_rows += 1
            continue
        if str(row.get("record_status") or "").strip().upper() == "ERROR":
            error_rows += 1
        detail_path = summary_path.parent / f"{symbol}.csv"
        accumulator = accumulators.setdefault(
            symbol,
            _SymbolSummaryAccumulator(
                trading_day=trading_day,
                symbol=symbol,
                detail_file=detail_path.name if detail_path.exists() else "",
            ),
        )
        accumulator.add(row)

    temporary_path = summary_path.with_suffix(summary_path.suffix + ".tmp")
    with temporary_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for symbol in sorted(accumulators):
            writer.writerow(accumulators[symbol].result())
    temporary_path.replace(summary_path)

    return ConsistencyReportSummary(
        output_path=summary_path,
        rows_written=rows_written,
        error_rows=error_rows,
        symbols_seen=tuple(sorted(accumulators)),
    )


def summarize_symbol_report(
    *,
    detail_path: Path,
    trading_day: date,
    symbol: str,
) -> Dict[str, Any]:
    """Summarize one generated symbol report without re-querying the database."""

    detail_path = Path(detail_path)
    accumulator = _SymbolSummaryAccumulator(
        trading_day=trading_day,
        symbol=symbol,
        detail_file=detail_path.name,
    )
    with detail_path.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            accumulator.add(row)
    return accumulator.result()


def upsert_symbol_summary(
    *,
    summary_path: Path,
    summary_row: Mapping[str, Any],
) -> None:
    """Insert or replace one symbol/day row in the accumulated summary CSV."""

    summary_path = Path(summary_path)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    key = (
        str(summary_row.get("trading_date") or "").strip(),
        str(summary_row.get("symbol") or "").strip().upper(),
    )
    if not all(key):
        raise ValueError("Summary row requires trading_date and symbol")

    rows: Dict[Tuple[str, str], Dict[str, Any]] = {}
    if summary_path.exists():
        with summary_path.open("r", newline="", encoding="utf-8") as handle:
            for existing in csv.DictReader(handle):
                existing_key = (
                    str(existing.get("trading_date") or "").strip(),
                    str(existing.get("symbol") or "").strip().upper(),
                )
                if all(existing_key):
                    rows[existing_key] = {field: existing.get(field, "") for field in SUMMARY_FIELDS}

    rows[key] = {field: _value(summary_row.get(field, "")) for field in SUMMARY_FIELDS}
    temporary_path = summary_path.with_suffix(summary_path.suffix + ".tmp")
    with temporary_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row_key in sorted(rows):
            writer.writerow(rows[row_key])
    temporary_path.replace(summary_path)


__all__ = [
    "ConsistencyReportSummary",
    "SUMMARY_FIELDS",
    "REPORT_FIELDS",
    "build_report_row",
    "generate_consistency_report",
    "generate_consistency_summary",
    "summarize_symbol_report",
    "upsert_symbol_summary",
]
