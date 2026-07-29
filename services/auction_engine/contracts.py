"""Typed contracts for the AutoTrades auction-state signal engine.

The contracts in this module define layer boundaries only. They contain no
setup-discovery rules and they do not alter the existing signal pipeline.

Design rules
------------
* All models reject unknown fields.
* Models are frozen after validation.
* Decision-time contracts contain only current and prior information.
* Local Auction decisions remain signal-agnostic; SignalGenerator owns persistence.
"""

from __future__ import annotations

import hashlib
import json
import math
from enum import Enum
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


from enums.auction_engine import (
    AdvisorAction as _AdvisorAction,
    BoundarySide as _BoundarySide,
    ContextAlignment as _ContextAlignment,
    DirectionalBias as _DirectionalBias,
    EvidencePolarity as _EvidencePolarity,
    QualityStatus as _QualityStatus,
)


CONTRACT_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    validate_default=True,
    arbitrary_types_allowed=True,
    use_enum_values=False,
    allow_inf_nan=False,
)


class ContractModel(BaseModel):
    """Base class shared by every auction-engine contract."""

    model_config = CONTRACT_CONFIG

    def to_storage_dict(self, *, exclude_none: bool = True) -> Dict[str, Any]:
        """Return a JSON-safe payload for reports and snapshot continuity."""

        return self.model_dump(mode="json", exclude_none=exclude_none)

    def stable_hash(self) -> str:
        """Return a deterministic hash of the strict persisted representation.

        MySQL JSON may normalise signed zero while serialising numeric values.
        Hash the semantic JSON value rather than Python's incidental ``-0.0``
        spelling, and reject every non-finite number instead of allowing the
        general snapshot sanitiser to convert it silently to ``null``.
        """

        canonical = _canonical_storage_value(
            self.to_storage_dict(exclude_none=False)
        )
        payload = json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def _canonical_storage_value(value: Any) -> Any:
    """Canonicalise one already JSON-shaped contract value for hashing."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Auction contract contains a non-finite number")
        return 0.0 if value == 0.0 else value
    if isinstance(value, dict):
        return {str(key): _canonical_storage_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_storage_value(item) for item in value]
    raise TypeError(
        f"Auction contract storage value is not JSON canonical: {type(value).__name__}"
    )




class SourceQuality(ContractModel):
    status: _QualityStatus = _QualityStatus.UNKNOWN
    source: str = ""
    source_time: Optional[datetime] = None
    age_seconds: Optional[float] = Field(default=None, ge=0.0)
    coverage: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    missing_fields: Tuple[str, ...] = ()
    reason_codes: Tuple[str, ...] = ()


class EvidenceFact(ContractModel):
    """One objective fact with provenance and polarity."""

    code: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    polarity: _EvidencePolarity = _EvidencePolarity.NEUTRAL
    observed_at: datetime
    value: Any = None
    unit: str = ""
    source_path: str = ""
    quality: _QualityStatus = _QualityStatus.UNKNOWN
    details: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("code", mode="before")
    @classmethod
    def _normalise_code(cls, value: Any) -> str:
        code = str(value or "").strip().upper()
        if not code:
            raise ValueError("Evidence fact code is required")
        return code

    @field_validator("domain", mode="before")
    @classmethod
    def _normalise_domain(cls, value: Any) -> str:
        domain = str(value or "").strip().lower()
        if not domain:
            raise ValueError("Evidence fact domain is required")
        return domain




class BarEvidence(ContractModel):
    snapshot_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = Field(default=None, ge=0.0)
    direction: _DirectionalBias = _DirectionalBias.UNKNOWN
    move_points: Optional[float] = None
    move_atr: Optional[float] = None
    body_fraction: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    close_position: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    upper_wick_fraction: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    lower_wick_fraction: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_ohlc(self) -> "BarEvidence":
        if self.high < self.low:
            raise ValueError("Bar high must be greater than or equal to low")
        if self.high < max(self.open, self.close):
            raise ValueError("Bar high cannot be below open or close")
        if self.low > min(self.open, self.close):
            raise ValueError("Bar low cannot be above open or close")
        return self


class PriceActionEvidence(ContractModel):
    direction: _DirectionalBias = _DirectionalBias.UNKNOWN
    strength: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    displacement_atr: Optional[float] = None
    directional_efficiency: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    overlap_ratio: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    followthrough: bool = False
    rejection: bool = False
    failed_extreme: bool = False
    swing_structure: str = "UNKNOWN"
    supporting_facts: Tuple[EvidenceFact, ...] = ()
    contradicting_facts: Tuple[EvidenceFact, ...] = ()
    quality: SourceQuality = Field(default_factory=SourceQuality)


class BoundaryObservation(ContractModel):
    boundary_id: str = Field(min_length=1)
    boundary_side: _BoundarySide
    boundary_source: str = Field(min_length=1)
    boundary_price: float = Field(gt=0.0)
    observed_at: datetime
    range_id: Optional[str] = None
    range_version: Optional[int] = Field(default=None, ge=1)
    range_low: Optional[float] = Field(default=None, gt=0.0)
    range_high: Optional[float] = Field(default=None, gt=0.0)
    range_start_time: Optional[datetime] = None
    range_end_time: Optional[datetime] = None
    range_basis: str = ""
    range_quality_score: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    distance_atr: Optional[float] = None
    current_offset_atr: Optional[float] = None
    outside_excursion_atr: Optional[float] = None
    close_outside_atr: Optional[float] = None
    consecutive_outside_closes: int = Field(default=0, ge=0)
    consecutive_inside_closes: int = Field(default=0, ge=0)
    reentry_depth_atr: Optional[float] = None
    retest_detected: bool = False
    reason_codes: Tuple[str, ...] = ()
    quality: SourceQuality = Field(default_factory=SourceQuality)

    @model_validator(mode="after")
    def _validate_observed_range(self) -> "BoundaryObservation":
        if (self.range_low is None) != (self.range_high is None):
            raise ValueError("BoundaryObservation range_low/range_high must be supplied together")
        if self.range_low is not None and self.range_high is not None:
            if self.range_high <= self.range_low:
                raise ValueError("BoundaryObservation range_high must exceed range_low")
            if not self.range_low <= self.boundary_price <= self.range_high:
                raise ValueError("Boundary price must lie on or within the observed range")
        if (
            self.range_start_time is not None
            and self.range_end_time is not None
            and self.range_end_time < self.range_start_time
        ):
            raise ValueError("BoundaryObservation range_end_time cannot precede start")
        return self


class TrendEvidence(ContractModel):
    direction: _DirectionalBias = _DirectionalBias.UNKNOWN
    directional_efficiency: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    vwap_side: str = "UNKNOWN"
    vwap_distance_atr: Optional[float] = None
    open_control: str = "UNKNOWN"
    value_migration: _DirectionalBias = _DirectionalBias.UNKNOWN
    swing_progression: str = "UNKNOWN"
    hma_order: str = "UNKNOWN"
    hma_spread_atr: Optional[float] = None
    hma_change: _DirectionalBias = _DirectionalBias.UNKNOWN
    ema_slow: Optional[float] = Field(default=None, gt=0.0)
    ema_ref: Optional[float] = Field(default=None, gt=0.0)
    ema_slow_ref_spread_atr: Optional[float] = Field(default=None, ge=0.0)
    ema_slow_slope_atr_per_bar: Optional[float] = None
    ema_ref_slope_atr_per_bar: Optional[float] = None
    ema_spread_change_atr_per_bar: Optional[float] = None
    ema_context: str = "UNKNOWN"
    retained_structure: Optional[bool] = None
    supporting_facts: Tuple[EvidenceFact, ...] = ()
    contradicting_facts: Tuple[EvidenceFact, ...] = ()
    quality: SourceQuality = Field(default_factory=SourceQuality)


class CompressionEvidence(ContractModel):
    compressed: Optional[bool] = None
    duration_bars: int = Field(default=0, ge=0)
    duration_minutes: Optional[float] = Field(default=None, ge=0.0)
    range_width_points: Optional[float] = Field(default=None, ge=0.0)
    range_width_atr: Optional[float] = Field(default=None, ge=0.0)
    contraction_ratio: Optional[float] = Field(default=None, ge=0.0)
    hma_convergence: Optional[float] = Field(default=None, ge=0.0)
    atr_contraction_ratio: Optional[float] = Field(default=None, ge=0.0)
    atr_state: str = "UNKNOWN"
    frozen_box_id: Optional[str] = None
    reason_codes: Tuple[str, ...] = ()
    quality: SourceQuality = Field(default_factory=SourceQuality)


class ExtensionEvidence(ContractModel):
    extended: Optional[bool] = None
    mature: Optional[bool] = None
    move_from_anchor_atr: Optional[float] = None
    move_from_anchor_pct: Optional[float] = None
    vwap_distance_atr: Optional[float] = None
    progress_decay: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    failed_extreme_count: int = Field(default=0, ge=0)
    directional_legs: int = Field(default=0, ge=0)
    rsi: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    bollinger_position: Optional[float] = None
    hma_maturity: str = "UNKNOWN"
    structural_failure_confirmed: bool = False
    supporting_facts: Tuple[EvidenceFact, ...] = ()
    contradicting_facts: Tuple[EvidenceFact, ...] = ()
    quality: SourceQuality = Field(default_factory=SourceQuality)


class OpportunityEvidence(ContractModel):
    entry_price: Optional[float] = Field(default=None, gt=0.0)
    entry_distance_points: Optional[float] = Field(default=None, ge=0.0)
    entry_distance_atr: Optional[float] = Field(default=None, ge=0.0)
    structural_stop_price: Optional[float] = Field(default=None, gt=0.0)
    structural_stop_distance_atr: Optional[float] = Field(default=None, ge=0.0)
    room_points: Optional[float] = Field(default=None, ge=0.0)
    room_atr: Optional[float] = Field(default=None, ge=0.0)
    room_pct: Optional[float] = Field(default=None, ge=0.0)
    first_move_available: Optional[bool] = None
    first_move_consumed: Optional[bool] = None
    session_minutes_remaining: Optional[float] = Field(default=None, ge=0.0)
    nearest_barrier_type: str = "NONE"
    nearest_barrier_price: Optional[float] = Field(default=None, gt=0.0)
    freshness_minutes: Optional[float] = Field(default=None, ge=0.0)
    reason_codes: Tuple[str, ...] = ()
    quality: SourceQuality = Field(default_factory=SourceQuality)


class MarketContextEvidence(ContractModel):
    index_alignment: _ContextAlignment = _ContextAlignment.UNKNOWN
    bank_index_alignment: _ContextAlignment = _ContextAlignment.UNKNOWN
    sector_alignment: _ContextAlignment = _ContextAlignment.UNKNOWN
    vix_alignment: _ContextAlignment = _ContextAlignment.UNKNOWN
    regime: str = "UNKNOWN"
    preferred_direction: _DirectionalBias = _DirectionalBias.UNKNOWN
    reason_codes: Tuple[str, ...] = ()
    quality: SourceQuality = Field(default_factory=SourceQuality)


class DerivativesContextEvidence(ContractModel):
    # Absolute directional interpretation from the current derivatives schema.
    # Candidate-relative interpretation is not part of this factual evidence block.
    futures_bias: _DirectionalBias = _DirectionalBias.UNKNOWN
    options_bias: _DirectionalBias = _DirectionalBias.UNKNOWN
    futures_alignment: _ContextAlignment = _ContextAlignment.UNKNOWN
    options_alignment: _ContextAlignment = _ContextAlignment.UNKNOWN
    futures_window: Optional[str] = None
    futures_status: Optional[str] = None
    futures_label: Optional[str] = None
    futures_strength: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    options_window: Optional[str] = None
    options_status: Optional[str] = None
    options_indication: Optional[str] = None
    options_strength: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    basis_points: Optional[float] = None
    basis_change_points: Optional[float] = None
    futures_oi_change_pct: Optional[float] = None
    futures_ltp_delta: Optional[float] = None
    futures_oi_delta: Optional[float] = None
    pcr: Optional[float] = Field(default=None, ge=0.0)
    pcr_delta: Optional[float] = None
    implied_volatility: Optional[float] = Field(default=None, ge=0.0)
    skew: Optional[float] = None
    raw_diagnostics: Dict[str, Any] = Field(default_factory=dict)
    reason_codes: Tuple[str, ...] = ()
    quality: SourceQuality = Field(default_factory=SourceQuality)


class EvidenceSnapshot(ContractModel):
    """Causal, objective evidence computed for one completed snapshot."""

    symbol: str = Field(min_length=1)
    equity_ref: Optional[str] = None
    trading_day: date
    snapshot_time: datetime
    snapshot_id: Optional[str] = None
    close: float = Field(gt=0.0)
    atr: Optional[float] = Field(default=None, gt=0.0)
    bar: BarEvidence
    price_action: PriceActionEvidence = Field(default_factory=PriceActionEvidence)
    boundary: Optional[BoundaryObservation] = None
    trend: TrendEvidence = Field(default_factory=TrendEvidence)
    compression: CompressionEvidence = Field(default_factory=CompressionEvidence)
    extension: ExtensionEvidence = Field(default_factory=ExtensionEvidence)
    opportunity: OpportunityEvidence = Field(default_factory=OpportunityEvidence)
    market: MarketContextEvidence = Field(default_factory=MarketContextEvidence)
    derivatives: DerivativesContextEvidence = Field(default_factory=DerivativesContextEvidence)
    data_quality: SourceQuality = Field(default_factory=SourceQuality)
    reason_codes: Tuple[str, ...] = ()
    raw_facts: Dict[str, Any] = Field(default_factory=dict)
    # Legacy metadata accepted for old persisted payloads; never used as a gate.
    config_version: Optional[str] = None

    @field_validator("symbol", mode="before")
    @classmethod
    def _normalise_symbol(cls, value: Any) -> str:
        symbol = str(value or "").strip().upper()
        if not symbol:
            raise ValueError("Symbol is required")
        return symbol

    @model_validator(mode="after")
    def _validate_chronology(self) -> "EvidenceSnapshot":
        if self.bar.snapshot_time != self.snapshot_time:
            raise ValueError("bar.snapshot_time must equal EvidenceSnapshot.snapshot_time")
        if self.trading_day != self.snapshot_time.date():
            raise ValueError("trading_day must match snapshot_time.date()")

        times: List[Tuple[str, datetime]] = []
        if self.boundary is not None:
            times.append(("boundary.observed_at", self.boundary.observed_at))
        for domain_name, facts in (
            ("price_action.supporting_facts", self.price_action.supporting_facts),
            ("price_action.contradicting_facts", self.price_action.contradicting_facts),
            ("trend.supporting_facts", self.trend.supporting_facts),
            ("trend.contradicting_facts", self.trend.contradicting_facts),
            ("extension.supporting_facts", self.extension.supporting_facts),
            ("extension.contradicting_facts", self.extension.contradicting_facts),
        ):
            times.extend((domain_name, fact.observed_at) for fact in facts)
        quality_objects = (
            self.price_action.quality,
            self.boundary.quality if self.boundary else None,
            self.trend.quality,
            self.compression.quality,
            self.extension.quality,
            self.opportunity.quality,
            self.market.quality,
            self.derivatives.quality,
            self.data_quality,
        )
        for index, quality in enumerate(quality_objects):
            if quality is not None and quality.source_time is not None:
                times.append((f"quality[{index}].source_time", quality.source_time))
        future = [name for name, ts in times if ts > self.snapshot_time]
        if future:
            raise ValueError(
                "Future evidence timestamp(s) are not causal: " + ", ".join(future)
            )
        return self










class AdvisorDecision(ContractModel):
    symbol: str = Field(min_length=1)
    snapshot_time: datetime
    action: _AdvisorAction
    selected_candidate_id: Optional[str] = None
    reason_codes: Tuple[str, ...] = ()
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
    config_version: Optional[str] = None

    @model_validator(mode="after")
    def _validate_advisor(self) -> "AdvisorDecision":
        if self.action in {_AdvisorAction.ALLOW, _AdvisorAction.WATCH, _AdvisorAction.BLOCK}:
            if not self.selected_candidate_id:
                raise ValueError("Advisor decision requires selected_candidate_id")
        if self.action is _AdvisorAction.NO_ACTION and self.selected_candidate_id is not None:
            raise ValueError("Advisor NO_ACTION must not select a candidate")
        return self






def stable_key(prefix: str, *parts: Any, length: int = 24) -> str:
    """Build a deterministic identity from immutable event parts."""

    prefix_text = str(prefix or "KEY").strip().upper().replace(" ", "_")
    serialised = "|".join(_normalise_key_part(part) for part in parts)
    digest = hashlib.sha256(serialised.encode("utf-8")).hexdigest()[:length]
    return f"{prefix_text}:{digest}"


def _normalise_key_part(value: Any) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return str(value)


__all__ = [
    "ContractModel",
    "SourceQuality",
    "EvidenceFact",
    "BarEvidence",
    "PriceActionEvidence",
    "BoundaryObservation",
    "TrendEvidence",
    "CompressionEvidence",
    "ExtensionEvidence",
    "OpportunityEvidence",
    "MarketContextEvidence",
    "DerivativesContextEvidence",
    "EvidenceSnapshot",
    "AdvisorDecision",
    "stable_key",
]
