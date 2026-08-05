"""Pure current-snapshot directional evidence assessment."""
from __future__ import annotations

from typing import Iterable, Tuple

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG, AuctionEngineConfig
from enums.auction_engine import DirectionalBias, FreshDirection
from schemas.snapshot import SnapshotSchema
from services.auction_engine.contracts import EvidenceSnapshot
from services.auction_engine.directional_contracts import FreshDirectionalEvidence


class DirectionalEvidenceBuilder:
    """Return one fresh direction without consulting prior directional memory."""

    def __init__(self, config: AuctionEngineConfig = AUCTION_ENGINE_CONFIG) -> None:
        self.config = config
        self.minimum_efficiency = config.state.orderly_trend_efficiency_min

    def build(
        self,
        snapshot: SnapshotSchema,
        evidence: EvidenceSnapshot,
    ) -> FreshDirectionalEvidence:
        if snapshot.symbol.strip().upper() != evidence.symbol:
            raise ValueError("Snapshot and evidence symbol mismatch")
        if snapshot.snapshot_time != evidence.snapshot_time:
            raise ValueError("Snapshot and evidence time mismatch")

        trend_side = evidence.trend.direction
        raw_side = self._direction_from_text(snapshot.structure.raw.side)
        slope_side = self._direction_from_text(snapshot.price_action.slope.state)
        efficiency = evidence.trend.directional_efficiency

        up_qualification = self._support_facts(
            snapshot, evidence, DirectionalBias.UP, include_slope=False
        )
        down_qualification = self._support_facts(
            snapshot, evidence, DirectionalBias.DOWN, include_slope=False
        )
        up_facts = self._support_facts(
            snapshot, evidence, DirectionalBias.UP, include_slope=True
        )
        down_facts = self._support_facts(
            snapshot, evidence, DirectionalBias.DOWN, include_slope=True
        )
        efficiency_ok = efficiency is None or efficiency >= self.minimum_efficiency
        up_qualified = (
            trend_side is DirectionalBias.UP
            and efficiency_ok
            and len(up_qualification) >= 2
        )
        down_qualified = (
            trend_side is DirectionalBias.DOWN
            and efficiency_ok
            and len(down_qualification) >= 2
        )

        if up_qualified and not down_qualified:
            side = FreshDirection.UP
            candidate_side = DirectionalBias.UP
            support = up_facts
            contradict = down_facts
            reasons = ("FRESH_UP_CONFIRMED",)
        elif down_qualified and not up_qualified:
            side = FreshDirection.DOWN
            candidate_side = DirectionalBias.DOWN
            support = down_facts
            contradict = up_facts
            reasons = ("FRESH_DOWN_CONFIRMED",)
        else:
            side = FreshDirection.UNRESOLVED
            candidate_side = self._candidate_side(
                trend_side=trend_side,
                up_count=len(up_facts),
                down_count=len(down_facts),
            )
            prefer_up = candidate_side is DirectionalBias.UP
            support = up_facts if prefer_up else down_facts
            contradict = down_facts if prefer_up else up_facts
            if candidate_side is DirectionalBias.UNKNOWN:
                support = ()
                contradict = tuple((*up_facts, *down_facts))
            reasons = self._unresolved_reasons(
                trend_side=trend_side,
                efficiency_ok=efficiency_ok,
                up_count=len(up_facts),
                down_count=len(down_facts),
            )

        return FreshDirectionalEvidence(
            side=side,
            candidate_side=candidate_side,
            observed_at=evidence.snapshot_time,
            trend_direction=trend_side,
            raw_structure_side=raw_side,
            slope_direction=slope_side,
            directional_efficiency=efficiency,
            support_facts=tuple(support),
            contradict_facts=tuple(contradict),
            reason_codes=tuple(reasons),
        )

    def _support_facts(
        self,
        snapshot: SnapshotSchema,
        evidence: EvidenceSnapshot,
        side: DirectionalBias,
        *,
        include_slope: bool,
    ) -> Tuple[str, ...]:
        trend = evidence.trend
        facts = []
        if trend.direction is side:
            facts.append("TREND_DIRECTION")
        if trend.value_migration is side:
            facts.append("VALUE_MIGRATION")
        if self._direction_from_text(trend.hma_order) is side:
            facts.append("HMA_ORDER")
        if side is DirectionalBias.UP and "ABOVE" in trend.vwap_side.upper():
            facts.append("VWAP_SIDE")
        if side is DirectionalBias.DOWN and "BELOW" in trend.vwap_side.upper():
            facts.append("VWAP_SIDE")
        if side is DirectionalBias.UP and trend.open_control.upper() == "ABOVE_OPEN":
            facts.append("OPEN_CONTROL")
        if side is DirectionalBias.DOWN and trend.open_control.upper() == "BELOW_OPEN":
            facts.append("OPEN_CONTROL")
        if self._direction_from_text(snapshot.structure.raw.side) is side:
            facts.append("RAW_STRUCTURE")
        if include_slope and self._direction_from_text(snapshot.price_action.slope.state) is side:
            facts.append("SLOPE")
        return tuple(facts)

    @staticmethod
    def _candidate_side(
        *,
        trend_side: DirectionalBias,
        up_count: int,
        down_count: int,
    ) -> DirectionalBias:
        if up_count > down_count:
            return DirectionalBias.UP
        if down_count > up_count:
            return DirectionalBias.DOWN
        if trend_side in (DirectionalBias.UP, DirectionalBias.DOWN):
            return trend_side
        return DirectionalBias.UNKNOWN

    @staticmethod
    def _unresolved_reasons(
        *,
        trend_side: DirectionalBias,
        efficiency_ok: bool,
        up_count: int,
        down_count: int,
    ) -> Tuple[str, ...]:
        reasons = ["FRESH_DIRECTION_UNRESOLVED"]
        if trend_side not in (DirectionalBias.UP, DirectionalBias.DOWN):
            reasons.append("TREND_DIRECTION_NOT_CONFIRMED")
        if not efficiency_ok:
            reasons.append("DIRECTIONAL_EFFICIENCY_BELOW_MINIMUM")
        if max(up_count, down_count) < 2:
            reasons.append("INSUFFICIENT_CURRENT_SUPPORT_FACTS")
        if up_count and down_count:
            reasons.append("CURRENT_FACTS_MIXED")
        return tuple(reasons)

    @staticmethod
    def _direction_from_text(value: object) -> DirectionalBias:
        text = str(value or "").strip().upper()
        if text in {"UP", "BUY", "BULL", "BULLISH", "UPTREND", "UP_ACCELERATING", "TURNING_UP"}:
            return DirectionalBias.UP
        if text in {"DOWN", "SELL", "BEAR", "BEARISH", "DOWNTREND", "DOWN_ACCELERATING", "TURNING_DOWN"}:
            return DirectionalBias.DOWN
        if "UP" in text and "DOWN" not in text:
            return DirectionalBias.UP
        if "DOWN" in text:
            return DirectionalBias.DOWN
        return DirectionalBias.UNKNOWN


__all__ = ["DirectionalEvidenceBuilder"]
