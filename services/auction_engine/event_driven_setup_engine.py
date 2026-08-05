"""Strict event-driven setup quality evaluation for all Auction setup families.

The engine consumes only routes produced by :mod:`setup_event_router`.  It may
assess price and room quality, but it never rediscovers structural events.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
import hashlib
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG, AuctionEngineConfig
from enums.auction_engine import (
    AuctionEventType,
    DirectionalBias,
    SetupEventAction,
    SetupFamily,
    StructuralPermissionResult,
    TradeSide,
)
from schemas.snapshot import SnapshotSchema
from services.auction_engine.episode_contracts import (
    AuctionEvent,
    AuthoritativeSetupEventRoute,
)
from services.auction_engine.setup_contracts import (
    AuthoritativeSetupCandidate,
    SetupEvaluationResult,
    SetupManagerDecision,
)


_FAMILY_PRIORITY: Dict[SetupFamily, int] = {
    SetupFamily.REVERSAL: 600,
    SetupFamily.FAILED_BREAKOUT: 500,
    SetupFamily.ACCEPTED_BREAKOUT: 400,
    SetupFamily.REACCELERATION: 300,
    SetupFamily.CONTINUATION: 200,
    SetupFamily.BREAKOUT_INITIATION: 100,
}


class EventDrivenSetupEngine:
    """Evaluate setup quality only after authoritative event permission."""

    def __init__(self, config: AuctionEngineConfig = AUCTION_ENGINE_CONFIG) -> None:
        self.config = config

    def evaluate(
        self,
        snapshot: SnapshotSchema,
        routes: Sequence[AuthoritativeSetupEventRoute],
    ) -> Tuple[SetupEvaluationResult, ...]:
        if not isinstance(snapshot, SnapshotSchema):
            raise TypeError("EventDrivenSetupEngine requires SnapshotSchema")
        if snapshot.auction.status != "OK":
            raise ValueError("Authoritative Auction projection is required")
        event_by_id = {event.event_id: event for event in snapshot.auction.events}
        results: List[SetupEvaluationResult] = []
        for route in routes:
            if route.action is not SetupEventAction.EVALUATE:
                continue
            if route.source_event_id not in event_by_id:
                raise ValueError(
                    f"Setup route references absent event {route.source_event_id}"
                )
            event = event_by_id[route.source_event_id]
            if event.event_type is not route.source_event_type:
                raise ValueError("Setup route event type mismatch")
            results.append(self._evaluate_route(snapshot, route, event))
        return tuple(results)

    def _evaluate_route(
        self,
        snapshot: SnapshotSchema,
        route: AuthoritativeSetupEventRoute,
        event: AuctionEvent,
    ) -> SetupEvaluationResult:
        side = self._trade_side(route.direction)
        structural = route.structural_result
        if structural is None:
            raise ValueError("Creation-capable setup route requires structural result")
        if structural is not StructuralPermissionResult.PERMIT:
            return self._rejected(
                route,
                side,
                structural,
                (f"STRUCTURAL_PERMISSION_{structural.value}",),
            )

        blockers: List[str] = []
        atr = float(snapshot.indicators.atr.value)
        entry = float(snapshot.close)
        if atr <= 0.0:
            blockers.append("ATR_NOT_POSITIVE")
        if entry <= 0.0:
            blockers.append("ENTRY_PRICE_NOT_POSITIVE")
        if side is TradeSide.NONE:
            blockers.append("AUTHORITATIVE_EVENT_DIRECTION_REQUIRED")
        if blockers:
            return self._rejected(route, side, structural, tuple(blockers))

        geometry = self._geometry(snapshot, route.setup_family, event, side, atr)
        blockers.extend(geometry["blockers"])
        minutes_remaining = self._session_minutes_remaining(snapshot.snapshot_time)
        required_minutes = self._minimum_session_minutes(route.setup_family)
        if minutes_remaining < required_minutes:
            blockers.append("INSUFFICIENT_SESSION_TIME_REMAINING")

        if blockers:
            return self._rejected(route, side, structural, tuple(dict.fromkeys(blockers)))

        stop = float(geometry["stop"])
        target = float(geometry["target"])
        reference = float(geometry["reference"])
        candidate_id = self._stable_id(
            "CAND",
            snapshot.symbol,
            snapshot.snapshot_time.date().isoformat(),
            route.setup_family.value,
            side.value,
            route.source_episode_id,
            route.source_event_id,
        )
        opportunity_key = self._stable_id(
            "OPP",
            snapshot.symbol,
            snapshot.snapshot_time.date().isoformat(),
            route.setup_family.value,
            side.value,
            route.source_episode_id,
            route.source_event_id,
        )
        candidate = AuthoritativeSetupCandidate(
            auction_engine_name=self.config.engine.engine_name,
            auction_engine_version=self.config.engine.engine_version,
            candidate_id=candidate_id,
            opportunity_key=opportunity_key,
            symbol=snapshot.symbol.strip().upper(),
            trading_day=snapshot.snapshot_time.date(),
            snapshot_time=snapshot.snapshot_time,
            setup_family=route.setup_family,
            setup_subtype=self._subtype(route.source_event_type),
            side=side,
            source_event_id=route.source_event_id,
            source_event_type=route.source_event_type,
            source_episode_id=route.source_episode_id,
            structural_result=structural,
            entry_price=entry,
            stop_anchor_price=stop,
            stop_anchor_type=str(geometry["stop_type"]),
            target_reference_price=target,
            target_basis=str(geometry["target_basis"]),
            reference_price=reference,
            reference_source=str(geometry["reference_source"]),
            valid_until=snapshot.snapshot_time + timedelta(minutes=6),
            reason_codes=tuple(dict.fromkeys((
                "AUTHORITATIVE_EVENT_ROUTE",
                "STRUCTURAL_PERMISSION_PERMIT",
                *route.reason_codes,
                *tuple(geometry["reasons"]),
            ))),
        )
        return SetupEvaluationResult(
            source_event_id=route.source_event_id,
            source_event_type=route.source_event_type,
            source_episode_id=route.source_episode_id,
            setup_family=route.setup_family,
            side=side,
            structural_result=structural,
            approved=True,
            candidate=candidate,
            blockers=(),
            reason_codes=candidate.reason_codes,
        )

    def _geometry(
        self,
        snapshot: SnapshotSchema,
        family: SetupFamily,
        event: AuctionEvent,
        side: TradeSide,
        atr: float,
    ) -> Dict[str, object]:
        data: Mapping[str, object] = event.data
        entry = float(snapshot.close)
        blockers: List[str] = []
        reasons: List[str] = []

        if family in {
            SetupFamily.BREAKOUT_INITIATION,
            SetupFamily.ACCEPTED_BREAKOUT,
            SetupFamily.FAILED_BREAKOUT,
        }:
            low = self._positive_number(data, "frozen_low")
            high = self._positive_number(data, "frozen_high")
            if low is None or high is None or high <= low:
                return {"blockers": ["FROZEN_BALANCE_GEOMETRY_REQUIRED"]}
            width = high - low
            if family is SetupFamily.FAILED_BREAKOUT:
                if side is TradeSide.BUY:
                    stop = low - max(0.10 * atr, entry * 0.0005)
                    target = high
                    reference = low
                else:
                    stop = high + max(0.10 * atr, entry * 0.0005)
                    target = low
                    reference = high
                reasons.append("FAILED_ESCAPE_TO_OPPOSITE_RANGE_EDGE")
                return {
                    "stop": stop,
                    "target": target,
                    "reference": reference,
                    "stop_type": "FAILED_ESCAPE_BOUNDARY",
                    "target_basis": "OPPOSITE_FROZEN_RANGE_EDGE",
                    "reference_source": "FAILED_ESCAPE_BOUNDARY",
                    "blockers": blockers,
                    "reasons": reasons,
                }

            boundary = high if side is TradeSide.BUY else low
            outside = entry > boundary if side is TradeSide.BUY else entry < boundary
            if not outside:
                blockers.append("ENTRY_NOT_OUTSIDE_FROZEN_BALANCE")
            max_distance = (
                self.config.initiation.max_entry_distance_atr
                if family is SetupFamily.BREAKOUT_INITIATION
                else self.config.acceptance.max_entry_distance_atr
            )
            if abs(entry - boundary) / atr > max_distance:
                blockers.append("ENTRY_TOO_FAR_FROM_ESCAPE_BOUNDARY")
            stop = (
                boundary - max(0.15 * atr, entry * 0.0005)
                if side is TradeSide.BUY
                else boundary + max(0.15 * atr, entry * 0.0005)
            )
            target = boundary + width if side is TradeSide.BUY else boundary - width
            reasons.append("BALANCE_MEASURED_MOVE_REFERENCE")
            return {
                "stop": stop,
                "target": target,
                "reference": boundary,
                "stop_type": "ESCAPE_BOUNDARY",
                "target_basis": "FROZEN_BALANCE_MEASURED_MOVE",
                "reference_source": "FROZEN_BALANCE_BOUNDARY",
                "blockers": blockers,
                "reasons": reasons,
            }

        if (
            family is SetupFamily.REVERSAL
            and event.event_type is AuctionEventType.DIRECTIONAL_REVERSED
        ):
            bars = tuple(snapshot.memory.structure.bars_3m[-2:])
            if len(bars) < 2:
                return {"blockers": ["REVERSAL_CONFIRMATION_BARS_REQUIRED"]}
            reference = self._positive_number(data, "start_price")
            if reference is None:
                return {"blockers": ["REVERSAL_START_PRICE_REQUIRED"]}
            if side is TradeSide.BUY:
                stop = min(float(bar.low) for bar in bars)
                if stop >= entry:
                    return {"blockers": ["BUY_REVERSAL_STOP_BELOW_ENTRY_REQUIRED"]}
                target = entry + 1.5 * (entry - stop)
            else:
                stop = max(float(bar.high) for bar in bars)
                if stop <= entry:
                    return {"blockers": ["SELL_REVERSAL_STOP_ABOVE_ENTRY_REQUIRED"]}
                target = entry - 1.5 * (stop - entry)
            if (
                abs(entry - reference) / atr
                > self.config.reversal.max_entry_distance_from_failure_level_atr
            ):
                blockers.append("ENTRY_TOO_FAR_FROM_REVERSAL_START")
            return {
                "stop": stop,
                "target": target,
                "reference": reference,
                "stop_type": "TWO_BAR_CONFIRMATION_EXTREME",
                "target_basis": "ONE_POINT_FIVE_R",
                "reference_source": "DIRECTIONAL_REVERSAL_START",
                "blockers": blockers,
                "reasons": ["TWO_BAR_DIRECTIONAL_REVERSAL_CONFIRMATION_GEOMETRY"],
            }

        valid_stops: List[Tuple[float, str]] = []
        for key, label in (
            ("protection_level", "DIRECTIONAL_PROTECTION"),
            ("origin_price", "EVENT_ORIGIN"),
            ("reversal_confirmation_level", "REVERSAL_CONFIRMATION_LEVEL"),
        ):
            value = self._positive_number(data, key)
            if value is None:
                continue
            if side is TradeSide.BUY and value < entry:
                valid_stops.append((value, label))
            elif side is TradeSide.SELL and value > entry:
                valid_stops.append((value, label))
        if not valid_stops:
            blockers.append("AUTHORITATIVE_STOP_GEOMETRY_REQUIRED")
            return {"blockers": blockers}

        # Reversal proof is anchored to the objective confirmation boundary
        # that caused the handoff.  Prefer that boundary when it remains valid
        # at establishment; using the later leg-origin close can create an
        # artificially tight stop and erase the original high/low geometry.
        reversal_reference = (
            self._positive_number(data, "reversal_confirmation_level")
            if family is SetupFamily.REVERSAL
            else None
        )
        reversal_reference_valid = bool(
            reversal_reference is not None
            and (
                (side is TradeSide.BUY and reversal_reference < entry)
                or (side is TradeSide.SELL and reversal_reference > entry)
            )
        )
        if reversal_reference_valid:
            stop = float(reversal_reference)
            stop_type = "REVERSAL_CONFIRMATION_LEVEL"
            # The confirmation boundary owns structural stop proof.  Entry
            # timeliness remains measured from the event handoff/origin so a
            # valid reversal is not rejected merely because its protective
            # boundary is farther away than the configured freshness window.
            reference = (
                self._positive_number(data, "origin_price")
                or float(reversal_reference)
            )
        else:
            if side is TradeSide.BUY:
                stop, stop_type = max(valid_stops, key=lambda item: item[0])
            else:
                stop, stop_type = min(valid_stops, key=lambda item: item[0])
            reference = self._positive_number(data, "origin_price") or stop

        risk = abs(entry - stop)
        target = entry + 1.5 * risk if side is TradeSide.BUY else entry - 1.5 * risk
        max_distance = (
            self.config.reversal.max_entry_distance_from_failure_level_atr
            if family is SetupFamily.REVERSAL
            else self.config.continuation.max_entry_distance_atr
        )
        if abs(entry - reference) / atr > max_distance:
            blockers.append("ENTRY_TOO_FAR_FROM_EVENT_ANCHOR")
        return {
            "stop": stop,
            "target": target,
            "reference": reference,
            "stop_type": stop_type,
            "target_basis": "ONE_POINT_FIVE_R",
            "reference_source": "AUTHORITATIVE_EVENT_GEOMETRY",
            "blockers": blockers,
            "reasons": ["AUTHORITATIVE_DIRECTIONAL_EVENT_GEOMETRY"],
        }

    def _minimum_session_minutes(self, family: SetupFamily) -> float:
        if family is SetupFamily.REVERSAL:
            return float(self.config.reversal.minimum_session_minutes)
        if family is SetupFamily.BREAKOUT_INITIATION:
            return float(self.config.initiation.minimum_session_minutes)
        if family is SetupFamily.ACCEPTED_BREAKOUT:
            return float(self.config.acceptance.minimum_session_minutes)
        if family in {SetupFamily.CONTINUATION, SetupFamily.REACCELERATION}:
            return float(self.config.continuation.minimum_session_minutes)
        return 0.0

    @staticmethod
    def _session_minutes_remaining(ts: datetime) -> float:
        session_end = datetime.combine(ts.date(), time(15, 30), tzinfo=ts.tzinfo)
        return max(0.0, (session_end - ts).total_seconds() / 60.0)

    @staticmethod
    def _trade_side(direction: DirectionalBias) -> TradeSide:
        if direction is DirectionalBias.UP:
            return TradeSide.BUY
        if direction is DirectionalBias.DOWN:
            return TradeSide.SELL
        return TradeSide.NONE

    @staticmethod
    def _positive_number(data: Mapping[str, object], key: str) -> Optional[float]:
        if key not in data or data[key] is None:
            return None
        value = float(data[key])
        return value if value > 0.0 else None

    @staticmethod
    def _subtype(event_type: AuctionEventType) -> str:
        mapping = {
            AuctionEventType.DIRECTIONAL_REVERSED: "STRUCTURAL_REVERSAL",
            AuctionEventType.BALANCE_ESCAPE_STARTED: "BALANCE_ESCAPE_INITIATION",
            AuctionEventType.BALANCE_ESCAPE_ACCEPTED: "ACCEPTED_BALANCE_ESCAPE",
            AuctionEventType.BALANCE_ESCAPE_FAILED: "FAILED_BALANCE_ESCAPE",
        }
        if event_type not in mapping:
            raise ValueError(f"No setup subtype for event {event_type.value}")
        return mapping[event_type]

    @staticmethod
    def _stable_id(prefix: str, *parts: str) -> str:
        payload = "|".join(str(part).strip() for part in parts)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
        return f"{prefix}:{digest}"

    @staticmethod
    def _rejected(
        route: AuthoritativeSetupEventRoute,
        side: TradeSide,
        structural: StructuralPermissionResult,
        blockers: Tuple[str, ...],
    ) -> SetupEvaluationResult:
        return SetupEvaluationResult(
            source_event_id=route.source_event_id,
            source_event_type=route.source_event_type,
            source_episode_id=route.source_episode_id,
            setup_family=route.setup_family,
            side=side,
            structural_result=structural,
            approved=False,
            candidate=None,
            blockers=tuple(dict.fromkeys(blockers)),
            reason_codes=route.reason_codes,
        )


class EventDrivenSetupManager:
    """Deterministic arbitration among already-approved event candidates."""

    def select(
        self,
        snapshot: SnapshotSchema,
        evaluations: Iterable[SetupEvaluationResult],
    ) -> SetupManagerDecision:
        approved = [item.candidate for item in evaluations if item.candidate is not None]
        candidates = [item for item in approved if item is not None]
        if not candidates:
            return SetupManagerDecision(
                symbol=snapshot.symbol,
                snapshot_time=snapshot.snapshot_time,
                selected_candidate=None,
                reason_codes=("NO_EVENT_DRIVEN_SETUP_APPROVED",),
            )
        sides = {candidate.side for candidate in candidates}
        if len(sides) > 1:
            return SetupManagerDecision(
                symbol=snapshot.symbol,
                snapshot_time=snapshot.snapshot_time,
                selected_candidate=None,
                deferred_candidate_ids=tuple(sorted(c.candidate_id for c in candidates)),
                reason_codes=("OPPOSING_AUTHORITATIVE_SETUPS_DEFERRED",),
            )
        ordered = sorted(
            candidates,
            key=lambda item: (
                -_FAMILY_PRIORITY[item.setup_family],
                item.candidate_id,
            ),
        )
        selected = ordered[0]
        return SetupManagerDecision(
            symbol=snapshot.symbol,
            snapshot_time=snapshot.snapshot_time,
            selected_candidate=selected,
            supporting_candidate_ids=tuple(c.candidate_id for c in ordered[1:]),
            reason_codes=("EVENT_DRIVEN_SETUP_SELECTED",),
        )


__all__ = ["EventDrivenSetupEngine", "EventDrivenSetupManager"]
