"""Causal Common Evidence Ledger for the AutoTrades auction-state engine.

Phase 2 is report-only.  This module converts one completed ``SnapshotSchema``
(or an equivalent dictionary) into objective, typed evidence.  It deliberately
uses current and prior snapshots only and does not discover a setup, create a
signal, write external lifecycle state, or invoke a second decision engine.

The builder reuses neutral snapshot facts such as OHLC, VWAP, dynamic range,
market windows and indicator values.  Existing setup labels/conclusions are not
accepted as proof by the new engine.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from schemas.snapshot import SnapshotSchema

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG, AuctionEngineConfig
from enums.auction_engine import (
    BoundarySide,
    ContextAlignment,
    DirectionalBias,
    EvidencePolarity,
    QualityStatus,
)
from services.auction_engine.contracts import (
    BarEvidence,
    BoundaryObservation,
    CompressionEvidence,
    DerivativesContextEvidence,
    EvidenceFact,
    EvidenceSnapshot,
    ExtensionEvidence,
    MarketContextEvidence,
    OpportunityEvidence,
    PriceActionEvidence,
    SourceQuality,
    TrendEvidence,
    stable_key,
)


_UP_WORDS = {
    "UP", "BUY", "BULL", "BULLISH", "ABOVE", "TRENDING_UP", "UPTREND",
    "HH_HL", "HIGHER_HIGH_HIGHER_LOW", "POSITIVE",
}
_DOWN_WORDS = {
    "DOWN", "SELL", "BEAR", "BEARISH", "BELOW", "TRENDING_DOWN", "DOWNTREND",
    "LH_LL", "LOWER_HIGH_LOWER_LOW", "NEGATIVE",
}
_RANGE_WORDS = {
    "RANGE", "RANGE_ACCEPTED", "BALANCE", "BALANCE_QUALIFIED", "COMPRESSION",
    "MICRO_COMPRESSION", "ROTATION", "SIDEWAYS",
}


class EvidenceBuildError(ValueError):
    """Raised when a snapshot cannot be represented causally."""


class EvidenceBuilder:
    """Build one :class:`EvidenceSnapshot` from neutral snapshot facts."""

    def __init__(self, config: AuctionEngineConfig = AUCTION_ENGINE_CONFIG) -> None:
        self.config = config
        self.cfg = config.evidence
        self.version = config.engine.config_version

    def build(
        self,
        snapshot: SnapshotSchema,
        *,
        history: Sequence[EvidenceSnapshot] = (),
        equity_ref: Optional[str] = None,
    ) -> EvidenceSnapshot:
        if not isinstance(snapshot, SnapshotSchema):
            raise EvidenceBuildError(
                "EvidenceBuilder.build requires a validated SnapshotSchema"
            )
        data = snapshot.model_dump(mode="python", by_alias=True)
        symbol = str(data["symbol"]).strip().upper()
        snapshot_time = _as_datetime(data["snapshot_time"])
        if not symbol:
            raise EvidenceBuildError("Snapshot symbol is required")
        if snapshot_time is None:
            raise EvidenceBuildError("Snapshot snapshot_time is required")

        close = _positive_float(data["close"])
        if close is None:
            raise EvidenceBuildError(f"Snapshot close is invalid for {symbol} @ {snapshot_time}")

        atr = _positive_float(_path(data, "indicators.atr.value"))
        if atr is None:
            raise EvidenceBuildError(
                f"Snapshot ATR is invalid for {symbol} @ {snapshot_time}"
            )
        bar, bar_missing = self._build_bar(data, snapshot_time, close, atr)
        quality = self._build_data_quality(data, snapshot_time, bar_missing)
        price_action = self._build_price_action(data, bar, atr, history, quality)
        boundary = self._build_boundary(data, bar, atr)
        trend = self._build_trend(data, bar, atr, price_action, boundary, history, quality)
        compression = self._build_compression(data, bar, atr, trend, history, quality)
        extension = self._build_extension(data, bar, atr, trend, price_action, history, quality)
        opportunity = self._build_opportunity(data, snapshot_time, close, atr, boundary)
        market = self._build_market_context(data, snapshot_time)
        derivatives = self._build_derivatives_context(data, snapshot_time)

        reason_codes = list(quality.reason_codes)
        if boundary is None:
            reason_codes.append("DYNAMIC_BOUNDARY_UNKNOWN")

        raw_facts: Dict[str, Any] = {}
        if self.cfg.retain_raw_fact_diagnostics:
            raw_facts = self._raw_fact_diagnostics(data, history)

        return EvidenceSnapshot(
            symbol=symbol,
            equity_ref=equity_ref,
            trading_day=snapshot_time.date(),
            snapshot_time=snapshot_time,
            snapshot_id=None,
            close=close,
            atr=atr,
            bar=bar,
            price_action=price_action,
            boundary=boundary,
            trend=trend,
            compression=compression,
            extension=extension,
            opportunity=opportunity,
            market=market,
            derivatives=derivatives,
            data_quality=quality,
            reason_codes=_unique(reason_codes),
            raw_facts=raw_facts,
            config_version=self.version,
        )

    def _build_bar(
        self,
        data: Mapping[str, Any],
        snapshot_time: datetime,
        close: float,
        atr: Optional[float],
    ) -> Tuple[BarEvidence, Tuple[str, ...]]:
        missing = []
        open_price = _positive_float(_path(data, "bar.open"))
        high = _positive_float(_path(data, "bar.high"))
        low = _positive_float(_path(data, "bar.low"))
        volume = _nonnegative_float(_path(data, "bar.volume"))

        invalid = [
            name
            for name, value in (
                ("bar.open", open_price),
                ("bar.high", high),
                ("bar.low", low),
                ("bar.volume", volume),
            )
            if value is None
        ]
        if invalid:
            raise EvidenceBuildError(
                f"Validated snapshot contains invalid primary bar fields: {invalid}"
            )
        candle_range = max(high - low, 0.0)
        move_points = close - open_price
        move_atr = move_points / atr if atr else None

        if move_points > self.cfg.floating_point_tolerance:
            direction = DirectionalBias.UP
        elif move_points < -self.cfg.floating_point_tolerance:
            direction = DirectionalBias.DOWN
        else:
            direction = DirectionalBias.NEUTRAL

        body_fraction = abs(move_points) / candle_range if candle_range > 0 else 0.0
        close_position = (close - low) / candle_range if candle_range > 0 else 0.5
        upper_wick = max(0.0, high - max(open_price, close))
        lower_wick = max(0.0, min(open_price, close) - low)

        return (
            BarEvidence(
                snapshot_time=snapshot_time,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
                direction=direction,
                move_points=move_points,
                move_atr=move_atr,
                body_fraction=_clamp(body_fraction),
                close_position=_clamp(close_position),
                upper_wick_fraction=_clamp(upper_wick / candle_range) if candle_range else 0.0,
                lower_wick_fraction=_clamp(lower_wick / candle_range) if candle_range else 0.0,
            ),
            tuple(missing),
        )

    def _build_data_quality(
        self,
        data: Mapping[str, Any],
        snapshot_time: datetime,
        bar_missing: Sequence[str],
    ) -> SourceQuality:
        missing = list(bar_missing)
        for block in self.cfg.required_top_level_blocks:
            value = data[block]
            if value is None:
                missing.append(block)

        history_bars = _strict_int(_path(data, "market_windows.sod.bars"), "market_windows.sod.bars")
        required = len(self.cfg.required_top_level_blocks) + 4
        present = max(0, required - len(set(missing)))
        coverage = _clamp(present / required)

        reason_codes = []
        if history_bars < self.cfg.minimum_history_bars:
            reason_codes.append("INSUFFICIENT_HISTORY_BARS")
        if missing:
            reason_codes.append("SNAPSHOT_FIELDS_PARTIAL")

        if any(path in missing for path in ("bar.open", "bar.high", "bar.low")):
            status = QualityStatus.PARTIAL
        elif not missing and history_bars >= self.cfg.minimum_history_bars:
            status = QualityStatus.GOOD
        else:
            status = QualityStatus.PARTIAL

        return SourceQuality(
            status=status,
            source="SnapshotSchema",
            source_time=snapshot_time,
            age_seconds=0.0,
            coverage=coverage,
            missing_fields=tuple(sorted(set(missing))),
            reason_codes=_unique(reason_codes),
        )

    def _build_price_action(
        self,
        data: Mapping[str, Any],
        bar: BarEvidence,
        atr: Optional[float],
        history: Sequence[EvidenceSnapshot],
        quality: SourceQuality,
    ) -> PriceActionEvidence:
        move_15 = _float(_path(data, "market_windows.15m.move_atr"))
        move_30 = _float(_path(data, "market_windows.30m.move_atr"))
        slope_3 = _float(_path(data, "price_action.slope.bars_3_atr_per_bar"))
        slope_5 = _float(_path(data, "price_action.slope.bars_5_atr_per_bar"))
        raw_side = _normalise_word(_path(data, "structure.raw.side"))

        votes = []
        votes.extend(_signed_vote(move_15, deadband=0.08))
        votes.extend(_signed_vote(move_30, deadband=0.12))
        votes.extend(_signed_vote(slope_3, deadband=0.02))
        votes.extend(_signed_vote(slope_5, deadband=0.015))
        votes.extend(_word_vote(raw_side))
        if bar.direction in (DirectionalBias.UP, DirectionalBias.DOWN):
            votes.append(bar.direction)
        direction = _vote_direction(votes)

        efficiency = self._directional_efficiency(data, history, bar)
        overlap = self._overlap_ratio(data, history, bar)
        previous = history[-1] if history else None
        followthrough = bool(
            previous
            and previous.bar.direction is bar.direction
            and bar.direction in (DirectionalBias.UP, DirectionalBias.DOWN)
            and direction is bar.direction
        )

        upper_wick = _required_number(
            bar.upper_wick_fraction, "bar.upper_wick_fraction"
        )
        lower_wick = _required_number(
            bar.lower_wick_fraction, "bar.lower_wick_fraction"
        )
        close_position = _required_number(
            bar.close_position, "bar.close_position"
        )
        rejection = bool(
            (upper_wick >= 0.45 and close_position <= 0.60)
            or (lower_wick >= 0.45 and close_position >= 0.40)
        )
        rsi = _float(_path(data, "indicators.rsi.value"))
        bb = _float(_path(data, "indicators.bollinger.position"))
        failed_extreme = bool(
            rejection
            and (
                (rsi is not None and (rsi >= self.cfg.extension_rsi_high or rsi <= self.cfg.extension_rsi_low))
                or (bb is not None and (bb >= self.cfg.extension_bollinger_high or bb <= self.cfg.extension_bollinger_low))
            )
        )

        move_abs = abs(_required_number(bar.move_atr, "bar.move_atr"))
        body = _required_number(bar.body_fraction, "bar.body_fraction")
        close_position = _required_number(bar.close_position, "bar.close_position")
        close_edge = max(close_position, 1.0 - close_position)
        strength = 100.0 * _clamp((min(move_abs, 1.0) + body + close_edge) / 3.0)

        supporting = []
        contradicting = []
        if direction is DirectionalBias.UP:
            supporting.append(_fact("PA_DIRECTION_UP", "price_action", bar.snapshot_time, move_15, "atr", "market_windows.15m.move_atr"))
        elif direction is DirectionalBias.DOWN:
            supporting.append(_fact("PA_DIRECTION_DOWN", "price_action", bar.snapshot_time, move_15, "atr", "market_windows.15m.move_atr"))
        if followthrough:
            supporting.append(_fact("PA_FOLLOWTHROUGH", "price_action", bar.snapshot_time, True, source_path="prior_evidence.bar.direction"))
        if rejection:
            contradicting.append(_fact("PA_REJECTION_PRESENT", "price_action", bar.snapshot_time, True, polarity=EvidencePolarity.CONTRADICT, source_path="bar.wicks"))

        swing_state = str(_path(data, "structure.raw.state") or "UNKNOWN").upper()
        if raw_side not in {"", "UNKNOWN", "NEUTRAL", "NONE"}:
            swing_state = f"{swing_state}:{raw_side}"

        return PriceActionEvidence(
            direction=direction,
            strength=strength,
            displacement_atr=bar.move_atr,
            directional_efficiency=efficiency,
            overlap_ratio=overlap,
            followthrough=followthrough,
            rejection=rejection,
            failed_extreme=failed_extreme,
            swing_structure=swing_state,
            supporting_facts=tuple(supporting),
            contradicting_facts=tuple(contradicting),
            quality=quality,
        )

    def _build_boundary(
        self,
        data: Mapping[str, Any],
        bar: BarEvidence,
        atr: Optional[float],
    ) -> Optional[BoundaryObservation]:
        accepted = _path(data, "structure.accepted.range")
        candidates: list[tuple[str, Any]] = [("ACCEPTED_RANGE", accepted)]
        if not self.config.boundary.require_accepted_range:
            if self.config.boundary.allow_candidate_range_fallback:
                candidates.append(("CANDIDATE_RANGE", _path(data, "structure.candidate.range")))
            if self.config.boundary.allow_raw_range_fallback:
                candidates.append(("RAW_RANGE", _path(data, "structure.raw.range")))

        selected_source = ""
        selected: Optional[Mapping[str, Any]] = None
        for source, value in candidates:
            if not isinstance(value, Mapping):
                continue
            high = _positive_float(value["high"])
            low = _positive_float(value["low"])
            if high is None or low is None or high <= low:
                continue

            if source == "ACCEPTED_RANGE":
                if bool(value["provisional"]):
                    continue
                if (
                    self.config.boundary.require_breakout_eligible_accepted_range
                    and not bool(value["breakout_eligible"])
                ):
                    continue
                range_source = _normalise_word(value["source"])
                if (
                    not self.config.boundary.allow_orb_seeded_accepted_range
                    and range_source == "ORB"
                ):
                    continue

            selected_source = source
            selected = value
            break
        if selected is None:
            return None

        high = float(selected["high"])
        low = float(selected["low"])
        upper_distance = abs(bar.close - high)
        lower_distance = abs(bar.close - low)
        if upper_distance <= lower_distance:
            side = BoundarySide.UPPER
            price = high
            offset_points = bar.close - high
            excursion_points = bar.high - high
        else:
            side = BoundarySide.LOWER
            price = low
            offset_points = low - bar.close
            excursion_points = low - bar.low

        range_id = _string_or_none(selected["range_id"])
        range_version = _strict_int(selected["version"], "structure.range.version")
        range_start_time = _align_datetime(
            _as_datetime(selected["start_time"]),
            bar.snapshot_time,
        )
        range_end_time = _align_datetime(
            _as_datetime(selected["end_time"]),
            bar.snapshot_time,
        )
        range_basis = _normalise_word(selected["range_type"])
        if not range_basis:
            raise EvidenceBuildError("structure range_type cannot be empty")
        range_quality_score = None
        boundary_id = f"{range_id or stable_key('range', bar.snapshot_time.date(), low, high)}:{side.value.lower()}"
        reason_codes = [selected_source, f"NEAREST_{side.value}"]
        if self.config.boundary.require_accepted_range:
            reason_codes.append("ACCEPTED_RANGE_ONLY")
        if _normalise_word(selected["range_type"]) == "MICRO_COMPRESSION":
            reason_codes.append("MICRO_COMPRESSION_RANGE")

        return BoundaryObservation(
            boundary_id=boundary_id,
            boundary_side=side,
            boundary_source=_required_text(selected["source"], "structure.range.source"),
            boundary_price=price,
            observed_at=bar.snapshot_time,
            range_id=range_id,
            range_version=range_version if range_version >= 1 else None,
            range_low=low,
            range_high=high,
            range_start_time=range_start_time,
            range_end_time=range_end_time,
            range_basis=range_basis,
            range_quality_score=(
                max(0.0, min(100.0, range_quality_score))
                if range_quality_score is not None
                else None
            ),
            distance_atr=abs(offset_points) / atr if atr else None,
            current_offset_atr=offset_points / atr if atr else None,
            outside_excursion_atr=max(0.0, excursion_points / atr) if atr else None,
            close_outside_atr=max(0.0, offset_points / atr) if atr else None,
            reason_codes=_unique(reason_codes),
            quality=SourceQuality(
                status=QualityStatus.GOOD,
                source=f"structure.{selected_source.lower()}",
                source_time=bar.snapshot_time,
                age_seconds=0.0,
                coverage=1.0,
            ),
        )

    def _build_trend(
        self,
        data: Mapping[str, Any],
        bar: BarEvidence,
        atr: Optional[float],
        price_action: PriceActionEvidence,
        boundary: Optional[BoundaryObservation],
        history: Sequence[EvidenceSnapshot],
        quality: SourceQuality,
    ) -> TrendEvidence:
        hma_state = _normalise_word(_path(data, "indicators.hma.state"))
        hma_strength = _normalise_word(_path(data, "indicators.hma.strength"))
        vwap_side = _normalise_word(_path(data, "indicators.vwap.side")) or "UNKNOWN"
        vwap_distance = _float(_path(data, "indicators.vwap.distance_atr"))
        day_open = _positive_float(_path(data, "levels.today.open"))
        raw_state = _normalise_word(_path(data, "structure.raw.state"))
        raw_side = _normalise_word(_path(data, "structure.raw.side"))
        move_15 = _float(_path(data, "market_windows.15m.move_atr"))
        move_30 = _float(_path(data, "market_windows.30m.move_atr"))
        sod_move = _float(_path(data, "market_windows.sod.move_atr"))

        votes = []
        votes.extend(_word_vote(hma_state))
        votes.extend(_word_vote(vwap_side))
        votes.extend(_word_vote(raw_side))
        votes.extend(_word_vote(raw_state))
        votes.extend(_signed_vote(move_15, deadband=0.10))
        votes.extend(_signed_vote(move_30, deadband=0.15))
        votes.extend(_signed_vote(sod_move, deadband=0.25))
        direction = _vote_direction(votes)
        if direction is DirectionalBias.MIXED and price_action.direction in (DirectionalBias.UP, DirectionalBias.DOWN):
            direction = price_action.direction

        if day_open is None:
            open_control = "UNKNOWN"
        elif bar.close > day_open:
            open_control = "ABOVE_OPEN"
        elif bar.close < day_open:
            open_control = "BELOW_OPEN"
        else:
            open_control = "AT_OPEN"

        migration_votes = _signed_vote(move_15, deadband=0.08) + _signed_vote(move_30, deadband=0.12)
        value_migration = _vote_direction(migration_votes)
        if value_migration is DirectionalBias.MIXED:
            value_migration = DirectionalBias.NEUTRAL

        hma_values = [
            _float(_path(data, "indicators.hma.fast")),
            _float(_path(data, "indicators.hma.mid1")),
            _float(_path(data, "indicators.hma.mid2")),
            _float(_path(data, "indicators.hma.slow")),
        ]
        hma_values = [x for x in hma_values if x is not None]
        hma_spread_atr = (max(hma_values) - min(hma_values)) / atr if atr and len(hma_values) >= 2 else None
        previous_hma = history[-1].trend.hma_order if history else "UNKNOWN"
        current_hma = hma_state or "UNKNOWN"
        if previous_hma == "UNKNOWN" or current_hma == previous_hma:
            hma_change = DirectionalBias.NEUTRAL
        else:
            hma_change = _word_direction(current_hma)

        # EMA100/EMA200 are slow stock-state context, never setup triggers.
        # Derive their current separation and causal slope from the bounded
        # Auction history.  A close/flat pair supports balance; a freshly
        # widening pair may support price-led early expansion despite EMA lag.
        ema_slow = _positive_float(_path(data, "indicators.ema.slow"))
        ema_ref = _positive_float(_path(data, "indicators.ema.ref"))
        ema_spread_atr = (
            abs(ema_slow - ema_ref) / atr
            if ema_slow is not None and ema_ref is not None and atr
            else None
        )
        lookback = int(self.cfg.ema_slow_context_lookback_bars)
        ema_history = [
            item for item in history[-lookback:]
            if getattr(item.trend, "ema_slow", None) is not None
            and getattr(item.trend, "ema_ref", None) is not None
        ]
        ema_slow_slope = None
        ema_ref_slope = None
        ema_spread_change = None
        if ema_history and ema_slow is not None and ema_ref is not None and atr:
            previous = ema_history[0].trend
            bars_elapsed = max(1, len(ema_history))
            ema_slow_slope = (ema_slow - float(previous.ema_slow)) / atr / bars_elapsed
            ema_ref_slope = (ema_ref - float(previous.ema_ref)) / atr / bars_elapsed
            previous_spread = abs(float(previous.ema_slow) - float(previous.ema_ref)) / atr
            if ema_spread_atr is not None:
                ema_spread_change = (ema_spread_atr - previous_spread) / bars_elapsed

        state_cfg = self.config.state
        close_pair = bool(
            ema_spread_atr is not None
            and ema_spread_atr <= state_cfg.slow_ema_close_spread_atr_max
        )
        flat_pair = bool(
            ema_slow_slope is not None
            and ema_ref_slope is not None
            and abs(ema_slow_slope) <= state_cfg.slow_ema_flat_slope_atr_per_bar_max
            and abs(ema_ref_slope) <= state_cfg.slow_ema_flat_slope_atr_per_bar_max
        )
        expanding_pair = bool(
            ema_spread_change is not None
            and ema_spread_change >= state_cfg.slow_ema_spread_expansion_atr_per_bar_min
        )
        if ema_slow is None or ema_ref is None:
            ema_context = "UNKNOWN"
        elif close_pair and flat_pair and not expanding_pair:
            ema_context = "COMPRESSED_FLAT"
        elif close_pair and expanding_pair and ema_slow_slope is not None and ema_ref_slope is not None:
            if ema_slow_slope > 0 and ema_ref_slope >= 0:
                ema_context = "EARLY_DIVERGENCE_UP"
            elif ema_slow_slope < 0 and ema_ref_slope <= 0:
                ema_context = "EARLY_DIVERGENCE_DOWN"
            else:
                ema_context = "COMPRESSED_MIXED"
        elif ema_slow > ema_ref and (ema_slow_slope is None or ema_slow_slope >= 0):
            ema_context = "UP_ALIGNED"
        elif ema_slow < ema_ref and (ema_slow_slope is None or ema_slow_slope <= 0):
            ema_context = "DOWN_ALIGNED"
        else:
            ema_context = "MIXED"

        retained: Optional[bool] = None
        if direction is DirectionalBias.UP:
            if raw_state in _DOWN_WORDS or raw_side in _DOWN_WORDS:
                retained = False
            elif boundary and boundary.boundary_side is BoundarySide.LOWER and _required_number(boundary.current_offset_atr, "boundary.current_offset_atr") > 0:
                retained = False
            elif raw_state in _UP_WORDS or raw_side in _UP_WORDS:
                retained = True
        elif direction is DirectionalBias.DOWN:
            if raw_state in _UP_WORDS or raw_side in _UP_WORDS:
                retained = False
            elif boundary and boundary.boundary_side is BoundarySide.UPPER and _required_number(boundary.current_offset_atr, "boundary.current_offset_atr") > 0:
                retained = False
            elif raw_state in _DOWN_WORDS or raw_side in _DOWN_WORDS:
                retained = True

        supporting = []
        contradicting = []
        if direction is DirectionalBias.UP:
            supporting.append(_fact("TREND_DIRECTION_UP", "trend", bar.snapshot_time, hma_state, source_path="indicators.hma.state"))
        elif direction is DirectionalBias.DOWN:
            supporting.append(_fact("TREND_DIRECTION_DOWN", "trend", bar.snapshot_time, hma_state, source_path="indicators.hma.state"))
        if retained is True:
            supporting.append(_fact("TREND_STRUCTURE_RETAINED", "trend", bar.snapshot_time, True, source_path="structure.raw"))
        elif retained is False:
            contradicting.append(_fact("TREND_STRUCTURE_LOST", "trend", bar.snapshot_time, True, polarity=EvidencePolarity.CONTRADICT, source_path="structure.raw"))

        swing_progression = f"{raw_state or 'UNKNOWN'}:{raw_side or 'UNKNOWN'}"
        if hma_strength and hma_strength != "UNKNOWN":
            swing_progression += f":HMA_{hma_strength}"

        return TrendEvidence(
            direction=direction,
            directional_efficiency=price_action.directional_efficiency,
            vwap_side=vwap_side,
            vwap_distance_atr=vwap_distance,
            open_control=open_control,
            value_migration=value_migration,
            swing_progression=swing_progression,
            hma_order=current_hma,
            hma_spread_atr=hma_spread_atr,
            hma_change=hma_change,
            ema_slow=ema_slow,
            ema_ref=ema_ref,
            ema_slow_ref_spread_atr=ema_spread_atr,
            ema_slow_slope_atr_per_bar=ema_slow_slope,
            ema_ref_slope_atr_per_bar=ema_ref_slope,
            ema_spread_change_atr_per_bar=ema_spread_change,
            ema_context=ema_context,
            retained_structure=retained,
            supporting_facts=tuple(supporting),
            contradicting_facts=tuple(contradicting),
            quality=quality,
        )

    def _build_compression(
        self,
        data: Mapping[str, Any],
        bar: BarEvidence,
        atr: Optional[float],
        trend: TrendEvidence,
        history: Sequence[EvidenceSnapshot],
        quality: SourceQuality,
    ) -> CompressionEvidence:
        range_block = self._preferred_range(data)
        range_width_points = None
        range_width_atr = None
        duration_bars = 0
        range_id = None
        range_type = "UNKNOWN"
        source_state = _normalise_word(_path(data, "structure.raw.state"))
        classification = "UNKNOWN"
        if range_block:
            high = _positive_float(range_block["high"])
            low = _positive_float(range_block["low"])
            if high is not None and low is not None and high > low:
                range_width_points = high - low
            range_width_atr = _nonnegative_float(range_block["width_atr"])
            if range_width_atr is None and atr and range_width_points is not None:
                range_width_atr = range_width_points / atr
            duration_bars = _strict_int(range_block["bars"], "structure.range.bars")
            range_id = _string_or_none(range_block["range_id"])
            range_type = _required_text(range_block["range_type"], "structure.range.range_type").upper()

        source_range_width_points = range_width_points
        source_range_width_atr = range_width_atr
        classification = _normalise_word(_path(data, "structure.accepted.metrics.classification"))
        if classification in {"", "UNKNOWN"}:
            classification = _normalise_word(_path(data, "structure.raw.metrics.classification")) or "UNKNOWN"

        efficiency = trend.directional_efficiency
        overlap = self._overlap_ratio(data, history, bar)
        close_inside_source_range = False
        if range_block:
            range_high = _positive_float(range_block["high"])
            range_low = _positive_float(range_block["low"])
            if range_high is not None and range_low is not None:
                tolerance = atr * 0.10
                close_inside_source_range = (range_low - tolerance) <= bar.close <= (range_high + tolerance)

        # Build a causal local box from completed bars.  The V1 report compared
        # one source range width with the immediately preceding source range,
        # which was usually identical and therefore produced a contraction
        # ratio of 1.0.  Recent-versus-reference width captures an actual
        # compression episode without consuming a setup conclusion.
        chronological_bars = [item.bar for item in history] + [bar]
        recent_count = min(len(chronological_bars), self.cfg.compression_recent_bars)
        reference_count = min(len(chronological_bars), self.cfg.compression_reference_bars)
        local_width_points = None
        local_width_atr = None
        contraction_ratio = None
        if recent_count >= 3:
            recent_bars = chronological_bars[-recent_count:]
            local_width_points = max(item.high for item in recent_bars) - min(item.low for item in recent_bars)
            if atr and local_width_points >= 0:
                local_width_atr = local_width_points / atr
            if reference_count > recent_count:
                reference_bars = chronological_bars[-reference_count:]
                reference_width = max(item.high for item in reference_bars) - min(item.low for item in reference_bars)
                if reference_width > self.cfg.floating_point_tolerance:
                    contraction_ratio = local_width_points / reference_width

        if local_width_points is not None:
            range_width_points = local_width_points
        if local_width_atr is not None:
            range_width_atr = local_width_atr
        if duration_bars <= 0 and recent_count:
            duration_bars = recent_count

        if contraction_ratio is None:
            range_15 = _positive_float(_path(data, "market_windows.15m.range_points"))
            range_30 = _positive_float(_path(data, "market_windows.30m.range_points"))
            if range_15 is not None and range_30 is not None and range_30 > 0:
                contraction_ratio = range_15 / range_30

        low_efficiency = efficiency is not None and efficiency <= 0.35
        high_overlap = overlap is not None and overlap >= 0.55
        quiet_balance = bool(
            low_efficiency and high_overlap
            if self.cfg.compression_require_low_efficiency_and_overlap
            else low_efficiency or high_overlap
        )
        explicit_compression = bool(
            range_type == "MICRO_COMPRESSION"
            or source_state == "COMPRESSION"
            or classification == "MICRO_COMPRESSION"
        )
        quiet_bar = bool(
            bar.move_atr is None
            or abs(bar.move_atr) <= self.cfg.compression_max_bar_move_atr
        )
        local_containment = bool(
            local_width_atr is not None
            and local_width_atr <= self.cfg.compression_range_width_atr_max
            and quiet_balance
            and quiet_bar
        )
        source_containment = bool(
            close_inside_source_range
            and source_range_width_atr is not None
            and source_range_width_atr <= self.cfg.compression_range_width_atr_max
            and quiet_bar
            and (
                explicit_compression
                or (
                    quiet_balance
                    and (
                        source_state in _RANGE_WORDS
                        or range_type in _RANGE_WORDS
                        or classification in _RANGE_WORDS
                    )
                )
            )
        )
        price_contained = bool(local_containment or source_containment)

        hma_converged = bool(
            trend.hma_spread_atr is not None
            and trend.hma_spread_atr <= self.cfg.compression_hma_spread_atr_max
        )
        prior_hma_spreads = [
            item.trend.hma_spread_atr
            for item in history[-self.cfg.compression_recent_bars:]
            if item.trend.hma_spread_atr is not None and item.trend.hma_spread_atr > 0
        ]
        hma_contraction_ratio = None
        if trend.hma_spread_atr is not None and len(prior_hma_spreads) >= 2:
            prior_average = sum(prior_hma_spreads) / len(prior_hma_spreads)
            if prior_average > self.cfg.floating_point_tolerance:
                hma_contraction_ratio = trend.hma_spread_atr / prior_average
        hma_contracting = bool(
            hma_contraction_ratio is not None
            and hma_contraction_ratio <= self.cfg.compression_hma_contraction_ratio_max
        )

        # ATR trend is context, not a directional trigger.  Contracting ATR
        # strengthens quiet compression; stable/rising ATR can still coexist
        # with a volatile rotational range, which is handled by efficiency and
        # overlap rather than ATR alone.
        atr_values = [
            float(value)
            for value in (getattr(item, "atr", None) for item in history[-self.cfg.atr_context_lookback_bars:])
            if value is not None and float(value) > 0
        ]
        atr_contraction_ratio = None
        if atr_values and atr is not None:
            prior_average = sum(atr_values) / len(atr_values)
            if prior_average > self.cfg.floating_point_tolerance:
                atr_contraction_ratio = atr / prior_average
        if atr_contraction_ratio is None:
            atr_state = "UNKNOWN"
        elif atr_contraction_ratio <= self.config.state.atr_contraction_ratio_max:
            atr_state = "CONTRACTING"
        elif atr_contraction_ratio >= self.config.state.atr_expansion_ratio_min:
            atr_state = "EXPANDING"
        else:
            atr_state = "STABLE"

        # HMA bunching/fan behaviour remains supporting evidence only.  This is
        # an objective compression *observation*; the state engine performs the
        # multi-bar confirmation and freezes the episode box.
        contraction_support = bool(
            explicit_compression
            or hma_converged
            or hma_contracting
            or (
                contraction_ratio is not None
                and contraction_ratio <= self.cfg.compression_contraction_ratio_max
            )
        )
        compressed = bool(price_contained and contraction_support and quiet_bar)

        reason_codes = []
        if local_containment:
            reason_codes.append("LOCAL_PRICE_CONTAINMENT")
        elif source_containment:
            reason_codes.append("SOURCE_RANGE_CONTAINMENT")
        if hma_converged:
            reason_codes.append("HMA_ABSOLUTE_CONVERGENCE_SUPPORT")
        if hma_contracting:
            reason_codes.append("HMA_SPREAD_CONTRACTING_SUPPORT")
        if contraction_ratio is not None and contraction_ratio <= self.cfg.compression_contraction_ratio_max:
            reason_codes.append("PRICE_RANGE_CONTRACTING")
        if compressed:
            reason_codes.append("COMPRESSION_OBJECTIVE_TRUE")
        elif price_contained:
            reason_codes.append("BALANCE_WITHOUT_CONFIRMED_COMPRESSION")

        return CompressionEvidence(
            compressed=compressed,
            duration_bars=duration_bars,
            duration_minutes=duration_bars * self.config.engine.snapshot_interval_minutes if duration_bars else None,
            range_width_points=range_width_points,
            range_width_atr=range_width_atr,
            contraction_ratio=contraction_ratio,
            hma_convergence=trend.hma_spread_atr,
            atr_contraction_ratio=atr_contraction_ratio,
            atr_state=atr_state,
            frozen_box_id=range_id if compressed else None,
            reason_codes=_unique(reason_codes),
            quality=quality,
        )

    def _build_extension(
        self,
        data: Mapping[str, Any],
        bar: BarEvidence,
        atr: Optional[float],
        trend: TrendEvidence,
        price_action: PriceActionEvidence,
        history: Sequence[EvidenceSnapshot],
        quality: SourceQuality,
    ) -> ExtensionEvidence:
        move_atr = _float(_path(data, "market_windows.sod.move_atr"))
        move_pct = _float(_path(data, "market_windows.sod.move_pct"))
        if move_atr is None:
            move_atr = _first_number(
                _path(data, "market_windows.60m.move_atr"),
                _path(data, "market_windows.30m.move_atr"),
                _path(data, "market_windows.15m.move_atr"),
            )
        vwap_distance = _float(_path(data, "indicators.vwap.distance_atr"))
        rsi = _float(_path(data, "indicators.rsi.value"))
        bb = _float(_path(data, "indicators.bollinger.position"))
        hma_strength = _normalise_word(_path(data, "indicators.hma.strength")) or "UNKNOWN"

        recent_move = _float(_path(data, "market_windows.15m.move_atr"))
        broader_move = _float(_path(data, "market_windows.30m.move_atr"))
        progress_decay = None
        if recent_move is not None and broader_move is not None and abs(broader_move) > 1e-9:
            recent_rate = abs(recent_move) / 15.0
            broader_rate = abs(broader_move) / 30.0
            if broader_rate > 0:
                progress_decay = _clamp(1.0 - (recent_rate / broader_rate))

        context_components = []
        maturity_components = []
        supporting = []
        directional_distance = False
        if move_atr is not None and abs(move_atr) >= self.cfg.extension_move_from_anchor_atr:
            directional_distance = True
            context_components.append("LARGE_MOVE_FROM_ANCHOR")
            supporting.append(_fact("EXTENSION_LARGE_MOVE", "extension", bar.snapshot_time, move_atr, "atr", "market_windows.sod.move_atr"))
        if vwap_distance is not None and abs(vwap_distance) >= self.cfg.extension_vwap_distance_atr:
            directional_distance = True
            context_components.append("LARGE_VWAP_DISTANCE")
            supporting.append(_fact("EXTENSION_VWAP_DISTANCE", "extension", bar.snapshot_time, vwap_distance, "atr", "indicators.vwap.distance_atr"))
        if rsi is not None and (rsi >= self.cfg.extension_rsi_high or rsi <= self.cfg.extension_rsi_low):
            context_components.append("RSI_EXTREME")
            supporting.append(_fact("EXTENSION_RSI_EXTREME", "extension", bar.snapshot_time, rsi, source_path="indicators.rsi.value"))
        if bb is not None and (bb >= self.cfg.extension_bollinger_high or bb <= self.cfg.extension_bollinger_low):
            context_components.append("BOLLINGER_EXTREME")
            supporting.append(_fact("EXTENSION_BOLLINGER_EXTREME", "extension", bar.snapshot_time, bb, source_path="indicators.bollinger.position"))

        progress_confirmation = bool(
            progress_decay is not None
            and progress_decay >= self.cfg.extension_progress_decay_min
        )
        rejection_confirmation = bool(price_action.rejection or price_action.failed_extreme)
        if progress_confirmation:
            maturity_components.append("PROGRESS_DECAY")
            supporting.append(_fact(
                "EXHAUSTION_PROGRESS_DECAY",
                "extension",
                bar.snapshot_time,
                progress_decay,
                source_path="market_windows.15m_vs_30m.progress_decay",
            ))
        if rejection_confirmation:
            maturity_components.append("REJECTION_PRESENT")
            supporting.append(_fact(
                "EXHAUSTION_REJECTION_PRESENT",
                "extension",
                bar.snapshot_time,
                True,
                source_path="price_action.rejection",
            ))

        indicator_extremes = sum(
            component in {"RSI_EXTREME", "BOLLINGER_EXTREME"}
            for component in context_components
        )
        # One rejection or one slowing window is not an extension.  Extension
        # context requires directional distance, or both independent indicator
        # extremes.  Current-leg maturity is handled by the state engine from a
        # leg-specific anchor.
        extended = bool(directional_distance or indicator_extremes >= 2)
        history_ready = (len(history) + 1) >= self.cfg.extension_min_history_bars_for_maturity
        maturity_confirmation = bool(progress_confirmation or rejection_confirmation)
        mature = bool(
            extended
            and history_ready
            and len(context_components) >= self.cfg.maturity_components_required
            and (
                directional_distance
                or not self.cfg.extension_maturity_requires_directional_distance
            )
            and (
                maturity_confirmation
                or not self.cfg.extension_maturity_requires_progress_or_rejection
            )
        )

        anchor_direction = DirectionalBias.UNKNOWN
        if move_atr is not None and move_atr > 0:
            anchor_direction = DirectionalBias.UP
        elif move_atr is not None and move_atr < 0:
            anchor_direction = DirectionalBias.DOWN
        recent_directions = [item.bar.direction for item in history[-self.config.state.history_bars:]] + [bar.direction]
        directional_legs = 0
        in_leg = False
        for direction in recent_directions:
            aligned = direction is anchor_direction and anchor_direction in (DirectionalBias.UP, DirectionalBias.DOWN)
            if aligned and not in_leg:
                directional_legs += 1
            in_leg = aligned

        raw_state = _normalise_word(_path(data, "structure.raw.state"))
        structural_failure = raw_state in {"TREND_FAILURE", "REVERSAL", "REVERSING"}
        contradicting = []
        if extended and trend.retained_structure is True:
            contradicting.append(_fact(
                "EXTENDED_BUT_TREND_RETAINED",
                "extension",
                bar.snapshot_time,
                True,
                polarity=EvidencePolarity.CONTRADICT,
                source_path="trend.retained_structure",
            ))

        return ExtensionEvidence(
            extended=extended,
            mature=mature,
            move_from_anchor_atr=move_atr,
            move_from_anchor_pct=move_pct,
            vwap_distance_atr=vwap_distance,
            progress_decay=progress_decay,
            failed_extreme_count=1 if price_action.failed_extreme else 0,
            directional_legs=directional_legs,
            rsi=rsi,
            bollinger_position=bb,
            hma_maturity=hma_strength,
            structural_failure_confirmed=structural_failure,
            supporting_facts=tuple(supporting),
            contradicting_facts=tuple(contradicting),
            quality=quality,
        )

    def _build_opportunity(
        self,
        data: Mapping[str, Any],
        snapshot_time: datetime,
        close: float,
        atr: Optional[float],
        boundary: Optional[BoundaryObservation],
    ) -> OpportunityEvidence:
        session_end = datetime.combine(snapshot_time.date(), time(15, 30), tzinfo=snapshot_time.tzinfo)
        minutes_remaining = max(0.0, (session_end - snapshot_time).total_seconds() / 60.0)
        barriers = []
        for label, path in (
            ("PDH", "levels.prev_day.high"),
            ("PDL", "levels.prev_day.low"),
            ("ORB_HIGH", "levels.opening_range.high"),
            ("ORB_LOW", "levels.opening_range.low"),
        ):
            price = _positive_float(_path(data, path))
            if price is not None:
                barriers.append((abs(price - close), label, price))
        if boundary is not None:
            barriers.append((abs(boundary.boundary_price - close), f"DYNAMIC_{boundary.boundary_side.value}", boundary.boundary_price))
        barriers.sort(key=lambda item: item[0])
        nearest = barriers[0] if barriers else None

        return OpportunityEvidence(
            entry_price=close,
            session_minutes_remaining=minutes_remaining,
            nearest_barrier_type=nearest[1] if nearest else "NONE",
            nearest_barrier_price=nearest[2] if nearest else None,
            room_points=nearest[0] if nearest else None,
            room_atr=(nearest[0] / atr) if nearest and atr else None,
            reason_codes=("SIDE_SPECIFIC_OPPORTUNITY_DEFERRED_TO_SETUP_ENGINE",),
            quality=SourceQuality(
                status=QualityStatus.PARTIAL,
                source="SnapshotSchema",
                source_time=snapshot_time,
                age_seconds=0.0,
                coverage=0.5,
                reason_codes=("NO_SETUP_SIDE_IN_PHASE2",),
            ),
        )

    def _build_market_context(
        self,
        data: Mapping[str, Any],
        snapshot_time: datetime,
    ) -> MarketContextEvidence:
        return MarketContextEvidence(
            index_alignment=ContextAlignment.UNKNOWN,
            bank_index_alignment=ContextAlignment.UNKNOWN,
            sector_alignment=ContextAlignment.UNKNOWN,
            vix_alignment=ContextAlignment.UNKNOWN,
            preferred_direction=DirectionalBias.UNKNOWN,
            regime="UNKNOWN",
            reason_codes=("MARKET_CONTEXT_NOT_IN_SNAPSHOT_SCHEMA",),
            quality=SourceQuality(
                status=QualityStatus.UNKNOWN,
                source="snapshot.market_context",
                source_time=snapshot_time,
                age_seconds=0.0,
                coverage=0.0,
                missing_fields=("market_context",),
                reason_codes=("MARKET_CONTEXT_NOT_IN_SNAPSHOT_SCHEMA",),
            ),
        )

    def _build_derivatives_context(
        self,
        data: Mapping[str, Any],
        snapshot_time: datetime,
    ) -> DerivativesContextEvidence:
        derivatives = _path(data, "derivatives")
        if not isinstance(derivatives, Mapping):
            raise EvidenceBuildError("snapshot.derivatives must be a mapping")

        preferred = tuple(self.config.evidence.derivatives_preferred_windows)
        option_windows = derivatives["option_sentiment_windows"]
        future_windows = derivatives["future_sentiment_windows"]
        option_key, option_row = _preferred_derivatives_window(
            option_windows, preferred, "option_sentiment_windows"
        )
        future_key, future_row = _preferred_derivatives_window(
            future_windows, preferred, "future_sentiment_windows"
        )

        option_indication = (
            _normalise_word(_required_mapping_value(option_row, "indication", option_key))
            if option_row is not None
            else ""
        )
        future_label = (
            _normalise_word(_required_mapping_value(future_row, "label", future_key))
            if future_row is not None
            else ""
        )
        options_bias = _options_sentiment_bias(option_indication)
        futures_bias = _future_sentiment_bias(future_label)

        spot = _positive_float(derivatives["spot_price"])
        fut_ltp_now = (
            _positive_float(_required_mapping_value(future_row, "fut_ltp_now", future_key))
            if future_row is not None
            else None
        )
        basis_points = (
            fut_ltp_now - spot
            if fut_ltp_now is not None and spot is not None
            else None
        )

        fut_oi_now = (
            _nonnegative_float(_required_mapping_value(future_row, "fut_oi_now", future_key))
            if future_row is not None
            else None
        )
        fut_oi_delta = (
            _float(_required_mapping_value(future_row, "fut_oi_delta", future_key))
            if future_row is not None
            else None
        )
        oi_change_pct = None
        if fut_oi_now is not None and fut_oi_delta is not None:
            prior_oi = fut_oi_now - fut_oi_delta
            if prior_oi > 0:
                oi_change_pct = (fut_oi_delta / prior_oi) * 100.0

        pcr = (
            _nonnegative_float(_required_mapping_value(option_row, "pcr_now", option_key))
            if option_row is not None
            else None
        )

        directional_count = sum(
            bias is not DirectionalBias.UNKNOWN
            for bias in (options_bias, futures_bias)
        )
        if directional_count == 2:
            quality_status = QualityStatus.GOOD
            coverage = 1.0
        elif directional_count == 1:
            quality_status = QualityStatus.PARTIAL
            coverage = 0.5
        else:
            quality_status = QualityStatus.PARTIAL
            coverage = 0.25

        reasons = ["DERIVATIVES_CONTEXT_PRESENT"]
        reasons.append(
            "OPTIONS_SENTIMENT_WINDOW_PRESENT"
            if option_row is not None
            else "OPTIONS_SENTIMENT_WINDOW_MISSING"
        )
        reasons.append(
            "FUTURES_SENTIMENT_WINDOW_PRESENT"
            if future_row is not None
            else "FUTURES_SENTIMENT_WINDOW_MISSING"
        )

        return DerivativesContextEvidence(
            futures_bias=futures_bias,
            options_bias=options_bias,
            futures_window=future_key,
            futures_status=(
                _normalise_word(_required_mapping_value(future_row, "status", future_key)) or None
                if future_row is not None
                else None
            ),
            futures_label=future_label or None,
            futures_strength=(
                _bounded_fraction(_required_mapping_value(future_row, "strength", future_key))
                if future_row is not None
                else None
            ),
            options_window=option_key,
            options_status=(
                _normalise_word(_required_mapping_value(option_row, "status", option_key)) or None
                if option_row is not None
                else None
            ),
            options_indication=option_indication or None,
            options_strength=(
                _bounded_fraction(_required_mapping_value(option_row, "strength", option_key))
                if option_row is not None
                else None
            ),
            basis_points=basis_points,
            basis_change_points=None,
            futures_oi_change_pct=oi_change_pct,
            futures_ltp_delta=(
                _float(_required_mapping_value(future_row, "fut_ltp_delta", future_key))
                if future_row is not None
                else None
            ),
            futures_oi_delta=fut_oi_delta,
            pcr=pcr,
            pcr_delta=(
                _float(_required_mapping_value(option_row, "pcr_delta", option_key))
                if option_row is not None
                else None
            ),
            implied_volatility=None,
            skew=None,
            raw_diagnostics={},
            reason_codes=_unique(reasons),
            quality=SourceQuality(
                status=quality_status,
                source="snapshot.derivatives.derived",
                source_time=snapshot_time,
                age_seconds=0.0,
                coverage=coverage,
                missing_fields=tuple(
                    field
                    for field, present in (
                        ("option_sentiment_windows", option_row is not None),
                        ("future_sentiment_windows", future_row is not None),
                    )
                    if not present
                ),
                reason_codes=_unique(reasons),
            ),
        )

    def _preferred_range(self, data: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
        for path in (
            "structure.accepted.range",
            "structure.candidate.range",
            "structure.raw.range",
        ):
            value = _path(data, path)
            if isinstance(value, Mapping):
                high = _positive_float(value["high"])
                low = _positive_float(value["low"])
                if high is not None and low is not None and high > low:
                    return value
        return None

    def _directional_efficiency(
        self,
        data: Mapping[str, Any],
        history: Sequence[EvidenceSnapshot],
        bar: BarEvidence,
    ) -> Optional[float]:
        """Return causal local directional efficiency.

        The Phase-2 V1 report used the accepted/raw structure metric first. That
        metric describes the source range object and can remain very small long
        after the local auction has begun trending. Once enough chronological
        bars exist, compute net displacement divided by travelled close-to-close
        path over the configured rolling window. Source metrics remain only an
        early-history fallback.
        """

        bars = list(history[-max(0, self.cfg.rolling_efficiency_bars - 1):])
        closes = [item.close for item in bars] + [bar.close]
        if len(closes) >= min(5, self.cfg.rolling_efficiency_bars):
            travelled = sum(abs(closes[index] - closes[index - 1]) for index in range(1, len(closes)))
            if travelled > self.cfg.floating_point_tolerance:
                return _clamp(abs(closes[-1] - closes[0]) / travelled)
            return 0.0

        value = _first_number(
            _path(data, "structure.accepted.metrics.directional_efficiency"),
            _path(data, "structure.raw.metrics.directional_efficiency"),
            _path(data, "structure.candidate.metrics.directional_efficiency"),
        )
        if value is not None:
            return _clamp(value)
        move = _float(_path(data, "market_windows.sod.move_points"))
        range_points = _positive_float(_path(data, "market_windows.sod.range_points"))
        if move is not None and range_points:
            return _clamp(abs(move) / range_points)
        return None

    def _overlap_ratio(
        self,
        data: Mapping[str, Any],
        history: Sequence[EvidenceSnapshot],
        bar: BarEvidence,
    ) -> Optional[float]:
        """Return causal adjacent-candle overlap for the local auction."""

        prior = list(history[-max(0, self.cfg.rolling_overlap_bars - 1):])
        bars = [item.bar for item in prior] + [bar]
        ratios = []
        for left, right in zip(bars, bars[1:]):
            left_range = max(left.high - left.low, 0.0)
            right_range = max(right.high - right.low, 0.0)
            denominator = min(left_range, right_range)
            if denominator <= self.cfg.floating_point_tolerance:
                continue
            overlap = max(0.0, min(left.high, right.high) - max(left.low, right.low))
            ratios.append(_clamp(overlap / denominator))
        if len(ratios) >= 2:
            return sum(ratios) / len(ratios)

        value = _first_number(
            _path(data, "structure.accepted.metrics.adjacent_overlap_ratio"),
            _path(data, "structure.raw.metrics.adjacent_overlap_ratio"),
            _path(data, "structure.candidate.metrics.adjacent_overlap_ratio"),
        )
        return _clamp(value) if value is not None else None

    def _raw_fact_diagnostics(
        self,
        data: Mapping[str, Any],
        history: Sequence[EvidenceSnapshot],
    ) -> Dict[str, Any]:
        return {
            "source_structure": {
                "raw_state": _path(data, "structure.raw.state"),
                "raw_side": _path(data, "structure.raw.side"),
                "accepted_state": _path(data, "structure.accepted.state"),
                "accepted_range_id": _path(data, "structure.accepted.range.range_id"),
                "accepted_range_version": _path(data, "structure.accepted.range.version"),
                "accepted_range_low": _path(data, "structure.accepted.range.low"),
                "accepted_range_high": _path(data, "structure.accepted.range.high"),
                "accepted_range_source": _path(data, "structure.accepted.range.source"),
                "accepted_range_established_at": _path(data, "structure.accepted.range.established_at"),
                "accepted_range_provisional": _path(data, "structure.accepted.range.provisional"),
                "accepted_range_breakout_eligible": _path(data, "structure.accepted.range.breakout_eligible"),
                "accepted_range_classification": _path(data, "structure.accepted.metrics.classification"),
                "accepted_range_overlap_ratio": _path(data, "structure.accepted.metrics.adjacent_overlap_ratio"),
                "accepted_range_directional_efficiency": _path(data, "structure.accepted.metrics.directional_efficiency"),
                "accepted_range_close_occupancy_ratio": _path(data, "structure.accepted.metrics.close_occupancy_ratio"),
                "candidate_active": _path(data, "structure.candidate.active"),
                "candidate_range_id": _path(data, "structure.candidate.range.range_id"),
                "candidate_range_version": _path(data, "structure.candidate.range.version"),
                "structure_flip_count": _path(data, "structure.flip_count_today"),
            },
            "source_states": {
                "hma": _path(data, "indicators.hma.state"),
                "hma_strength": _path(data, "indicators.hma.strength"),
                "hma_flip_count": _path(data, "indicators.hma.flip_count_today"),
                "vwap": _path(data, "indicators.vwap.side"),
                "vwap_flip_count": _path(data, "indicators.vwap.flip_count_today"),
                "ema_slow": _path(data, "indicators.ema.slow"),
                "ema_ref": _path(data, "indicators.ema.ref"),
                "atr": _path(data, "indicators.atr.value"),
                "atr_band": _path(data, "indicators.atr.band"),
            },
            "source_windows": {
                "sod": _compact_mapping(_path(data, "market_windows.sod")),
                "15m": _compact_mapping(_path(data, "market_windows.15m")),
                "30m": _compact_mapping(_path(data, "market_windows.30m")),
            },
            "source_levels": {
                "prev_day_high": _path(data, "levels.prev_day.high"),
                "prev_day_low": _path(data, "levels.prev_day.low"),
                "opening_range_high": _path(data, "levels.opening_range.high"),
                "opening_range_low": _path(data, "levels.opening_range.low"),
                "today_open": _path(data, "levels.today.open"),
                "vwap": _path(data, "indicators.vwap.value"),
            },
            "history_bars_available": len(history),
        }


def _path(data: Any, path: str) -> Any:
    current = data
    traversed = []
    for part in path.split("."):
        traversed.append(part)
        try:
            current = current[part] if isinstance(current, Mapping) else getattr(current, part)
        except (KeyError, TypeError, AttributeError) as exc:
            raise EvidenceBuildError(
                f"Required snapshot path is missing: {'.'.join(traversed)}"
            ) from exc
    return current


def _first_datetime(*values: Any) -> Optional[datetime]:
    for value in values:
        parsed = _as_datetime(value)
        if parsed is not None:
            return parsed
    return None


def _align_datetime(value: Optional[datetime], reference: datetime) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None and reference.tzinfo is not None:
        return value.replace(tzinfo=reference.tzinfo)
    if value.tzinfo is not None and reference.tzinfo is None:
        return value.replace(tzinfo=None)
    return value


def _as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _float(value: Any) -> Optional[float]:
    try:
        if value is None or isinstance(value, bool):
            return None
        result = float(value)
        if result != result or result in (float("inf"), float("-inf")):
            return None
        return result
    except (TypeError, ValueError):
        return None


def _positive_float(value: Any) -> Optional[float]:
    result = _float(value)
    return result if result is not None and result > 0 else None


def _nonnegative_float(value: Any) -> Optional[float]:
    result = _float(value)
    return result if result is not None and result >= 0 else None


def _strict_int(value: Any, path: str) -> int:
    if isinstance(value, bool):
        raise EvidenceBuildError(f"{path} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise EvidenceBuildError(f"{path} must be an integer") from exc


def _required_text(value: Any, path: str) -> str:
    if value is None:
        raise EvidenceBuildError(f"{path} is required")
    text = str(value).strip()
    if not text:
        raise EvidenceBuildError(f"{path} cannot be empty")
    return text


def _required_mapping_value(
    row: Mapping[str, Any],
    key: str,
    context: Optional[str],
) -> Any:
    if key not in row:
        label = context if context is not None else "derivatives window"
        raise EvidenceBuildError(f"{label}.{key} is required")
    return row[key]



def _required_number(value: Any, path: str) -> float:
    number = _float(value)
    if number is None:
        raise EvidenceBuildError(f"{path} is required and must be finite")
    return number

def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _normalise_word(value: Any) -> str:
    return str(value or "").strip().upper().replace(" ", "_")


def _string_or_none(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _preferred_derivatives_window(
    windows: Any,
    preferred: Sequence[str],
    context: str,
) -> Tuple[Optional[str], Optional[Mapping[str, Any]]]:
    if windows is None:
        return None, None
    if not isinstance(windows, Mapping):
        raise EvidenceBuildError(f"derivatives.{context} must be a mapping or null")
    for key in preferred:
        if key not in windows:
            continue
        row = windows[key]
        if not isinstance(row, Mapping):
            raise EvidenceBuildError(f"derivatives.{context}.{key} must be a mapping")
        status = _required_mapping_value(row, "status", f"{context}.{key}")
        if str(status).strip().lower() == "ok":
            return str(key), row
    return None, None


def _options_sentiment_bias(value: Any) -> DirectionalBias:
    word = _normalise_word(value)
    if word == "BULLISH":
        return DirectionalBias.UP
    if word == "BEARISH":
        return DirectionalBias.DOWN
    if word == "NEUTRAL":
        return DirectionalBias.NEUTRAL
    return DirectionalBias.UNKNOWN


def _future_sentiment_bias(value: Any) -> DirectionalBias:
    word = _normalise_word(value)
    if word in {"LONG_BUILDUP", "SHORT_COVERING"}:
        return DirectionalBias.UP
    if word in {"SHORT_BUILDUP", "LONG_UNWINDING"}:
        return DirectionalBias.DOWN
    if word == "NEUTRAL":
        return DirectionalBias.NEUTRAL
    return DirectionalBias.UNKNOWN


def _bounded_fraction(value: Any) -> Optional[float]:
    number = _float(value)
    if number is None:
        return None
    return max(0.0, min(1.0, number))


def _word_direction(value: Any) -> DirectionalBias:
    word = _normalise_word(value)
    if word in _UP_WORDS or any(token in word for token in ("UPTREND", "BULL", "ABOVE")):
        return DirectionalBias.UP
    if word in _DOWN_WORDS or any(token in word for token in ("DOWNTREND", "BEAR", "BELOW")):
        return DirectionalBias.DOWN
    if word in {"NEUTRAL", "NO_TREND", "RANGE", "BALANCE", "SIDEWAYS", "AT"}:
        return DirectionalBias.NEUTRAL
    return DirectionalBias.UNKNOWN


def _word_vote(value: Any) -> list[DirectionalBias]:
    direction = _word_direction(value)
    return [direction] if direction in (DirectionalBias.UP, DirectionalBias.DOWN) else []


def _signed_vote(value: Any, *, deadband: float) -> list[DirectionalBias]:
    number = _float(value)
    if number is None:
        return []
    if number > deadband:
        return [DirectionalBias.UP]
    if number < -deadband:
        return [DirectionalBias.DOWN]
    return []


def _vote_direction(votes: Iterable[DirectionalBias]) -> DirectionalBias:
    up = sum(1 for vote in votes if vote is DirectionalBias.UP)
    down = sum(1 for vote in votes if vote is DirectionalBias.DOWN)
    if up == 0 and down == 0:
        return DirectionalBias.UNKNOWN
    if up > down:
        return DirectionalBias.UP
    if down > up:
        return DirectionalBias.DOWN
    return DirectionalBias.MIXED


def _alignment(value: Any) -> ContextAlignment:
    word = _normalise_word(value)
    if word in {"SUPPORT", "ALIGNED", "ALLOW", "POSITIVE"}:
        return ContextAlignment.SUPPORT
    if word in {"CONFLICT", "OPPOSE", "BLOCK", "NEGATIVE"}:
        return ContextAlignment.CONFLICT
    if word in {"NEUTRAL", "MIXED"}:
        return ContextAlignment.NEUTRAL
    return ContextAlignment.UNKNOWN


def _first_number(*values: Any) -> Optional[float]:
    for value in values:
        number = _float(value)
        if number is not None:
            return number
    return None


def _first_value(data: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _unique(values: Iterable[str]) -> Tuple[str, ...]:
    seen = set()
    out = []
    for value in values:
        text = str(value or "").strip().upper()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return tuple(out)


def _fact(
    code: str,
    domain: str,
    observed_at: datetime,
    value: Any,
    unit: str = "",
    source_path: str = "",
    *,
    polarity: EvidencePolarity = EvidencePolarity.SUPPORT,
) -> EvidenceFact:
    return EvidenceFact(
        code=code,
        domain=domain,
        polarity=polarity,
        observed_at=observed_at,
        value=value,
        unit=unit,
        source_path=source_path,
        quality=QualityStatus.GOOD if value is not None else QualityStatus.UNKNOWN,
    )


def _compact_mapping(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    keys = (
        "status", "bars", "minutes", "move_points", "move_pct", "move_atr",
        "range_points", "range_pct", "slope_atr_per_bar", "close_position_in_range",
    )
    return {key: value[key] for key in keys if key in value and value[key] is not None}


__all__ = ["EvidenceBuildError", "EvidenceBuilder"]
