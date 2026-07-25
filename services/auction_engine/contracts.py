"""Typed contracts for the AutoTrades auction-state signal engine.

The contracts in this module define layer boundaries only. They contain no
setup-discovery rules and they do not alter the existing signal pipeline.

Design rules
------------
* All models reject unknown fields.
* Models are frozen after validation.
* Decision-time contracts contain only current and prior information.
* Future MFE/MAE is isolated in ``OutcomeMetrics`` and cannot be attached to an
  ``EvidenceSnapshot`` or local Auction decision.
* Local Auction decisions remain signal-agnostic; SignalGenerator owns persistence.
* Reason codes and independent confidence channels remain visible; no layer is
  represented by one opaque score.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StringEnum(str, Enum):
    """Python 3.10-compatible string enum."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class TradeSide(StringEnum):
    BUY = "BUY"
    SELL = "SELL"
    ANY = "ANY"
    NONE = "NONE"

    @property
    def opposite(self) -> "TradeSide":
        if self is TradeSide.BUY:
            return TradeSide.SELL
        if self is TradeSide.SELL:
            return TradeSide.BUY
        return self


class BoundarySide(StringEnum):
    UPPER = "UPPER"
    LOWER = "LOWER"
    NONE = "NONE"


class DirectionalBias(StringEnum):
    UP = "UP"
    DOWN = "DOWN"
    NEUTRAL = "NEUTRAL"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


class QualityStatus(StringEnum):
    GOOD = "GOOD"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    MISSING = "MISSING"
    INVALID = "INVALID"
    UNKNOWN = "UNKNOWN"


class EvidencePolarity(StringEnum):
    SUPPORT = "SUPPORT"
    CONTRADICT = "CONTRADICT"
    NEUTRAL = "NEUTRAL"
    UNKNOWN = "UNKNOWN"


class AuctionStateName(StringEnum):
    UNKNOWN = "UNKNOWN"
    BALANCE = "BALANCE"
    COMPRESSION = "COMPRESSION"
    BOUNDARY_INTERACTION = "BOUNDARY_INTERACTION"
    FRESH_EXPANSION = "FRESH_EXPANSION"
    ORDERLY_UPTREND = "ORDERLY_UPTREND"
    ORDERLY_DOWNTREND = "ORDERLY_DOWNTREND"
    CONTROLLED_PULLBACK = "CONTROLLED_PULLBACK"
    RECOMPRESSION = "RECOMPRESSION"
    REACCELERATION = "REACCELERATION"
    MATURE_EXTENSION = "MATURE_EXTENSION"
    TREND_FAILURE = "TREND_FAILURE"
    REVERSAL = "REVERSAL"
    CHAOTIC_ROTATION = "CHAOTIC_ROTATION"


class AdvisorAction(StringEnum):
    NO_ACTION = "NO_ACTION"
    ALLOW = "ALLOW"
    WATCH = "WATCH"
    BLOCK = "BLOCK"


class BoundaryEpisodeStatus(StringEnum):
    APPROACHING = "APPROACHING"
    OUTSIDE_ATTEMPT = "OUTSIDE_ATTEMPT"
    UNRESOLVED = "UNRESOLVED"
    ACCEPTANCE_BUILDING = "ACCEPTANCE_BUILDING"
    ACCEPTED = "ACCEPTED"
    FAILURE_BUILDING = "FAILURE_BUILDING"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"
    STALE = "STALE"


class BoundaryResolution(StringEnum):
    UNRESOLVED = "UNRESOLVED"
    ACCEPTED = "ACCEPTED"
    FAILED = "FAILED"


class SetupFamily(StringEnum):
    BREAKOUT_INITIATION = "BREAKOUT_INITIATION"
    ACCEPTED_BREAKOUT = "ACCEPTED_BREAKOUT"
    FAILED_BREAKOUT = "FAILED_BREAKOUT"
    CONTINUATION = "CONTINUATION"
    REACCELERATION = "REACCELERATION"
    REVERSAL = "REVERSAL"
    # Historical value retained for old reports only. New setup candidates use
    # family REVERSAL with NORMAL_REVERSAL / EXHAUSTION_REVERSAL subtypes.
    EXHAUSTION_REVERSAL = "EXHAUSTION_REVERSAL"


class CandidateEligibility(StringEnum):
    ELIGIBLE = "ELIGIBLE"
    WATCH = "WATCH"
    INELIGIBLE = "INELIGIBLE"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"
    CONSUMED = "CONSUMED"


class CandidateRole(StringEnum):
    """Operational interpretation of one immutable boundary opportunity.

    Multiple candidate records may legitimately describe the same underlying
    boundary attempt.  ``opportunity_key`` / ``support_group_key`` are the
    authoritative de-duplication identities; this role explains where each
    interpretation sits in that shared lifecycle.
    """

    EARLY_INITIATION = "EARLY_INITIATION"
    ACCEPTED_RESOLUTION_ENTRY = "ACCEPTED_RESOLUTION_ENTRY"
    CONTINUATION_INTERPRETATION = "CONTINUATION_INTERPRETATION"
    FAILED_RESOLUTION_ENTRY = "FAILED_RESOLUTION_ENTRY"
    REVERSAL_ENTRY = "REVERSAL_ENTRY"


class ContextAlignment(StringEnum):
    SUPPORT = "SUPPORT"
    CONFLICT = "CONFLICT"
    NEUTRAL = "NEUTRAL"
    UNKNOWN = "UNKNOWN"


class ManagerAction(StringEnum):
    SELECT = "SELECT"
    DEFER = "DEFER"
    BLOCK = "BLOCK"
    NO_ACTION = "NO_ACTION"


class LocalDecisionAction(StringEnum):
    """Pure market/opportunity outcome emitted by the Auction Engine.

    These values deliberately avoid signal-lifecycle verbs. A downstream
    SignalGenerator may later translate LOCAL_CONFIRMED into CREATE, UPDATE,
    HOLD or INVALIDATE after it loads the exact active signal state.
    """

    NO_OPPORTUNITY = "NO_LOCAL_OPPORTUNITY"
    WATCH = "LOCAL_WATCH"
    CONFIRMED = "LOCAL_CONFIRMED"
    DEFER = "LOCAL_DEFER"
    BLOCKED = "LOCAL_BLOCKED"


CONTRACT_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    validate_default=True,
    arbitrary_types_allowed=True,
    use_enum_values=False,
)


class ContractModel(BaseModel):
    """Base class shared by every auction-engine contract."""

    model_config = CONTRACT_CONFIG

    def to_storage_dict(self, *, exclude_none: bool = True) -> Dict[str, Any]:
        """Return a JSON-safe payload for reports and snapshot continuity."""

        return self.model_dump(mode="json", exclude_none=exclude_none)

    def stable_hash(self) -> str:
        """Return a deterministic content hash for diagnostics and tests."""

        payload = json.dumps(
            self.to_storage_dict(exclude_none=False),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class Reason(ContractModel):
    code: str = Field(min_length=1)
    message: str = ""
    source: str = ""
    data: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("code", mode="before")
    @classmethod
    def _normalise_code(cls, value: Any) -> str:
        code = str(value or "").strip().upper()
        if not code:
            raise ValueError("Reason code is required")
        return code


class SourceQuality(ContractModel):
    status: QualityStatus = QualityStatus.UNKNOWN
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
    polarity: EvidencePolarity = EvidencePolarity.NEUTRAL
    observed_at: datetime
    value: Any = None
    unit: str = ""
    source_path: str = ""
    quality: QualityStatus = QualityStatus.UNKNOWN
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


class ConfidenceChannel(ContractModel):
    """Named confidence dimension; never a hidden total score."""

    name: str = Field(min_length=1)
    score: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    quality: QualityStatus = QualityStatus.UNKNOWN
    supporting_fact_codes: Tuple[str, ...] = ()
    contradicting_fact_codes: Tuple[str, ...] = ()
    reason_codes: Tuple[str, ...] = ()


class BarEvidence(ContractModel):
    snapshot_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = Field(default=None, ge=0.0)
    direction: DirectionalBias = DirectionalBias.UNKNOWN
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
    direction: DirectionalBias = DirectionalBias.UNKNOWN
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
    boundary_side: BoundarySide
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
    direction: DirectionalBias = DirectionalBias.UNKNOWN
    directional_efficiency: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    vwap_side: str = "UNKNOWN"
    vwap_distance_atr: Optional[float] = None
    open_control: str = "UNKNOWN"
    value_migration: DirectionalBias = DirectionalBias.UNKNOWN
    swing_progression: str = "UNKNOWN"
    hma_order: str = "UNKNOWN"
    hma_spread_atr: Optional[float] = None
    hma_change: DirectionalBias = DirectionalBias.UNKNOWN
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
    index_alignment: ContextAlignment = ContextAlignment.UNKNOWN
    bank_index_alignment: ContextAlignment = ContextAlignment.UNKNOWN
    sector_alignment: ContextAlignment = ContextAlignment.UNKNOWN
    vix_alignment: ContextAlignment = ContextAlignment.UNKNOWN
    regime: str = "UNKNOWN"
    preferred_direction: DirectionalBias = DirectionalBias.UNKNOWN
    reason_codes: Tuple[str, ...] = ()
    quality: SourceQuality = Field(default_factory=SourceQuality)


class DerivativesContextEvidence(ContractModel):
    # Absolute directional interpretation from the current derivatives schema.
    # Candidate-relative interpretation is not part of this factual evidence block.
    futures_bias: DirectionalBias = DirectionalBias.UNKNOWN
    options_bias: DirectionalBias = DirectionalBias.UNKNOWN
    futures_alignment: ContextAlignment = ContextAlignment.UNKNOWN
    options_alignment: ContextAlignment = ContextAlignment.UNKNOWN
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
    config_version: str = Field(min_length=1)

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


class AuctionState(ContractModel):
    state_key: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    snapshot_time: datetime
    previous_state: AuctionStateName
    current_state: AuctionStateName
    transition_time: datetime
    entered_at: datetime
    expires_at: Optional[datetime] = None
    supporting_evidence: Tuple[EvidenceFact, ...] = ()
    contradicting_evidence: Tuple[EvidenceFact, ...] = ()
    confidence_channels: Tuple[ConfidenceChannel, ...] = ()
    reason_codes: Tuple[str, ...] = ()
    config_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_state_time(self) -> "AuctionState":
        if self.transition_time > self.snapshot_time:
            raise ValueError("Auction-state transition cannot occur after snapshot_time")
        if self.entered_at > self.snapshot_time:
            raise ValueError("Auction-state entered_at cannot occur after snapshot_time")
        if self.expires_at is not None and self.expires_at <= self.snapshot_time:
            raise ValueError("Active AuctionState.expires_at must be after snapshot_time")
        return self


class FrozenRange(ContractModel):
    range_id: str = Field(min_length=1)
    range_version: int = Field(ge=1)
    source: str = Field(min_length=1)
    low: float = Field(gt=0.0)
    high: float = Field(gt=0.0)
    start_time: datetime
    end_time: Optional[datetime] = None
    frozen_at: datetime
    basis: str = ""
    quality_score: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_range(self) -> "FrozenRange":
        if self.high <= self.low:
            raise ValueError("Frozen range high must be greater than low")
        if self.end_time is not None and self.end_time < self.start_time:
            raise ValueError("Frozen range end_time cannot precede start_time")
        if self.frozen_at < self.start_time:
            raise ValueError("Frozen range cannot be frozen before it starts")
        return self


class BoundaryEpisode(ContractModel):
    event_key: str = Field(min_length=1)
    structural_key: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    episode_sequence: int = Field(default=1, ge=1)
    symbol: str = Field(min_length=1)
    trading_day: date
    snapshot_time: datetime
    event_time: datetime
    first_seen_time: datetime
    last_seen_time: datetime
    attempt_time: Optional[datetime] = None
    first_outside_close_time: Optional[datetime] = None
    last_outside_time: Optional[datetime] = None
    first_reentry_time: Optional[datetime] = None
    boundary_id: str = Field(min_length=1)
    boundary_side: BoundarySide
    boundary_source: str = Field(min_length=1)
    boundary_price: float = Field(gt=0.0)
    breakout_side: TradeSide
    failure_side: TradeSide
    frozen_range: FrozenRange
    status: BoundaryEpisodeStatus = BoundaryEpisodeStatus.UNRESOLVED
    resolution: BoundaryResolution = BoundaryResolution.UNRESOLVED
    acceptance_building_since: Optional[datetime] = None
    failure_building_since: Optional[datetime] = None
    accepted_time: Optional[datetime] = None
    failed_time: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    current_offset_atr: Optional[float] = None
    max_outside_excursion_atr: float = Field(default=0.0, ge=0.0)
    max_close_outside_atr: float = Field(default=0.0, ge=0.0)
    total_outside_closes: int = Field(default=0, ge=0)
    consecutive_outside_closes: int = Field(default=0, ge=0)
    consecutive_inside_closes: int = Field(default=0, ge=0)
    reentry_depth_atr: Optional[float] = None
    retest_detected: bool = False
    reset_inside_closes: int = Field(default=0, ge=0)
    reset_started_at: Optional[datetime] = None
    acceptance_evidence: Tuple[EvidenceFact, ...] = ()
    failure_evidence: Tuple[EvidenceFact, ...] = ()
    contradicting_evidence: Tuple[EvidenceFact, ...] = ()
    reason_codes: Tuple[str, ...] = ()
    terminal: bool = False
    consumed: bool = False
    superseded: bool = False
    terminal_reason: Optional[str] = None
    superseded_by: Optional[str] = None
    emitted_resolutions: Tuple[str, ...] = ()
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
    config_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_episode(self) -> "BoundaryEpisode":
        if self.breakout_side not in (TradeSide.BUY, TradeSide.SELL):
            raise ValueError("BoundaryEpisode.breakout_side must be BUY or SELL")
        if self.failure_side is not self.breakout_side.opposite:
            raise ValueError("failure_side must be the opposite of breakout_side")
        if self.trading_day != self.snapshot_time.date():
            raise ValueError("BoundaryEpisode.trading_day must match snapshot_time")
        if not self.first_seen_time <= self.last_seen_time <= self.snapshot_time:
            raise ValueError("BoundaryEpisode chronology is invalid")
        for label, ts in (
            ("event_time", self.event_time),
            ("attempt_time", self.attempt_time),
            ("first_outside_close_time", self.first_outside_close_time),
            ("last_outside_time", self.last_outside_time),
            ("first_reentry_time", self.first_reentry_time),
            ("reset_started_at", self.reset_started_at),
            ("acceptance_building_since", self.acceptance_building_since),
            ("failure_building_since", self.failure_building_since),
            ("accepted_time", self.accepted_time),
            ("failed_time", self.failed_time),
        ):
            if ts is not None and ts > self.snapshot_time:
                raise ValueError(f"{label} cannot be after snapshot_time")
        if self.status is BoundaryEpisodeStatus.ACCEPTED:
            if self.resolution is not BoundaryResolution.ACCEPTED or self.accepted_time is None:
                raise ValueError("ACCEPTED status requires ACCEPTED resolution and accepted_time")
        if self.status is BoundaryEpisodeStatus.FAILED:
            if self.resolution is not BoundaryResolution.FAILED or self.failed_time is None:
                raise ValueError("FAILED status requires FAILED resolution and failed_time")
        if self.resolution is BoundaryResolution.ACCEPTED and self.accepted_time is None:
            raise ValueError("ACCEPTED resolution requires accepted_time")
        if self.resolution is BoundaryResolution.FAILED and self.failed_time is None:
            raise ValueError("FAILED resolution requires failed_time")
        if self.consumed and not self.terminal:
            raise ValueError("A consumed boundary episode must be terminal")
        if self.superseded and not self.superseded_by:
            raise ValueError("A superseded episode requires superseded_by")
        if self.status is BoundaryEpisodeStatus.SUPERSEDED and not self.superseded:
            raise ValueError("SUPERSEDED status requires superseded=True")
        return self


class SetupCandidate(ContractModel):
    candidate_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    trading_day: date
    snapshot_time: datetime
    candidate_time: datetime
    family: SetupFamily
    subtype: str = Field(min_length=1)
    side: TradeSide
    event_key: str = Field(min_length=1)
    event_time: datetime
    opportunity_key: str = Field(min_length=1)
    boundary_thesis_key: str = Field(min_length=1)
    support_group_key: str = Field(min_length=1)
    candidate_role: CandidateRole

    # Immutable source-boundary contract.  These fields remain attached to the
    # candidate after the boundary engine archives the source episode or moves
    # on to a newer observed range.
    source_boundary_event_key: str = Field(min_length=1)
    source_boundary_status: BoundaryEpisodeStatus
    source_boundary_resolution: BoundaryResolution
    source_boundary_resolution_basis: Optional[str] = None
    source_boundary_id: str = Field(min_length=1)
    source_boundary_side: BoundarySide
    source_boundary_source: str = Field(min_length=1)
    source_boundary_price: float = Field(gt=0.0)
    source_frozen_range_id: str = Field(min_length=1)
    source_frozen_range_version: int = Field(ge=1)
    source_frozen_range_low: float = Field(gt=0.0)
    source_frozen_range_high: float = Field(gt=0.0)

    entry_price: float = Field(gt=0.0)
    stop_anchor_price: Optional[float] = Field(default=None, gt=0.0)
    stop_anchor_type: str = "UNKNOWN"
    target_basis: str = "UNKNOWN"
    target_reference_price: Optional[float] = Field(default=None, gt=0.0)
    room_points: Optional[float] = Field(default=None, ge=0.0)
    room_atr: Optional[float] = Field(default=None, ge=0.0)
    room_pct: Optional[float] = Field(default=None, ge=0.0)
    entry_distance_atr: Optional[float] = Field(default=None, ge=0.0)
    freshness_minutes: Optional[float] = Field(default=None, ge=0.0)
    first_move_consumed: bool = False
    auction_state: AuctionStateName
    eligibility: CandidateEligibility
    blockers: Tuple[str, ...] = ()
    supporting_evidence: Tuple[EvidenceFact, ...] = ()
    opposing_evidence: Tuple[EvidenceFact, ...] = ()
    confidence_channels: Tuple[ConfidenceChannel, ...] = ()
    reason_codes: Tuple[str, ...] = ()
    terminal: bool = False
    consumed: bool = False
    superseded: bool = False
    valid_until: Optional[datetime] = None
    dynamic_watch: bool = False
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
    config_version: str = Field(min_length=1)

    @field_validator("subtype", mode="before")
    @classmethod
    def _normalise_subtype(cls, value: Any) -> str:
        subtype = str(value or "").strip().upper()
        if not subtype:
            raise ValueError("Candidate subtype is required")
        return subtype

    @model_validator(mode="after")
    def _validate_candidate(self) -> "SetupCandidate":
        if self.side not in (TradeSide.BUY, TradeSide.SELL):
            raise ValueError("SetupCandidate.side must be BUY or SELL")
        if self.trading_day != self.snapshot_time.date():
            raise ValueError("SetupCandidate.trading_day must match snapshot_time")
        if self.candidate_time > self.snapshot_time:
            raise ValueError("candidate_time cannot be after snapshot_time")
        if self.event_time > self.candidate_time:
            raise ValueError("event_time cannot be after candidate_time")
        if self.source_boundary_event_key != self.event_key:
            raise ValueError("source_boundary_event_key must match event_key")
        if self.support_group_key != self.opportunity_key:
            raise ValueError("support_group_key must match opportunity_key")
        if self.source_frozen_range_high <= self.source_frozen_range_low:
            raise ValueError("source frozen range high must exceed low")
        expected_price = (
            self.source_frozen_range_high
            if self.source_boundary_side is BoundarySide.UPPER
            else self.source_frozen_range_low
        )
        if self.source_boundary_side in (BoundarySide.UPPER, BoundarySide.LOWER):
            tolerance = max(1e-9, abs(expected_price) * 1e-9)
            if abs(self.source_boundary_price - expected_price) > tolerance:
                raise ValueError("source boundary price must match its frozen range edge")
        if self.valid_until is not None and self.valid_until < self.snapshot_time:
            raise ValueError("An active candidate valid_until cannot precede snapshot_time")
        if self.eligibility is CandidateEligibility.ELIGIBLE and self.blockers:
            raise ValueError("An ELIGIBLE candidate cannot contain blockers")
        if self.eligibility is CandidateEligibility.INELIGIBLE and not self.blockers:
            raise ValueError("An INELIGIBLE candidate must explain at least one blocker")
        if self.consumed and not self.terminal:
            raise ValueError("A consumed candidate must be terminal")
        return self


class StockContext(ContractModel):
    """Objective stock-day context used by the signal-time Advisor.

    This projection deliberately avoids regime labels.  It records only the
    current Auction state, accepted-range geometry, complete day-so-far extreme
    path metrics and the existing exhaustion episode.
    """

    symbol: str = Field(min_length=1)
    snapshot_time: datetime
    current_auction_state: AuctionStateName = AuctionStateName.UNKNOWN
    directional_bias: DirectionalBias = DirectionalBias.UNKNOWN

    accepted_range_id: Optional[str] = None
    accepted_range_source: str = "UNKNOWN"
    accepted_range_low: Optional[float] = None
    accepted_range_high: Optional[float] = None
    accepted_range_established_at: Optional[datetime] = None
    accepted_range_provisional: bool = False
    accepted_range_breakout_eligible: bool = False
    accepted_range_inside: bool = False
    accepted_range_position: Optional[float] = None
    accepted_range_outside_atr: Optional[float] = None

    session_open_price: float = Field(gt=0.0)
    session_high_price: float = Field(gt=0.0)
    session_high_time: datetime
    session_low_price: float = Field(gt=0.0)
    session_low_time: datetime
    session_position: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    distance_to_session_high_atr: float = Field(ge=0.0)
    distance_to_session_low_atr: float = Field(ge=0.0)
    rise_from_session_low_atr: float = Field(ge=0.0)
    rise_from_session_low_pct: float = Field(ge=0.0)
    decline_from_session_high_atr: float = Field(ge=0.0)
    decline_from_session_high_pct: float = Field(ge=0.0)
    path_from_session_low_bars: int = Field(ge=0)
    path_from_session_low_efficiency: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    path_from_session_low_directional_ratio: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    path_from_session_high_bars: int = Field(ge=0)
    path_from_session_high_efficiency: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    path_from_session_high_directional_ratio: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    exhaustion_active: bool = False
    exhausted_side: DirectionalBias = DirectionalBias.UNKNOWN
    exhaustion_started_at: Optional[datetime] = None
    exhaustion_expires_at: Optional[datetime] = None
    reason_codes: Tuple[str, ...] = ()
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
    config_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_stock_context(self) -> "StockContext":
        if (self.accepted_range_low is None) != (self.accepted_range_high is None):
            raise ValueError("Accepted range low/high must be provided together")
        if (
            self.accepted_range_low is not None
            and self.accepted_range_high is not None
            and self.accepted_range_high <= self.accepted_range_low
        ):
            raise ValueError("Accepted range high must exceed accepted range low")
        if self.accepted_range_inside and self.accepted_range_low is None:
            raise ValueError("accepted_range_inside requires accepted range geometry")
        if self.session_high_price < self.session_low_price:
            raise ValueError("Session high cannot be below session low")
        if not (self.session_low_price <= self.session_open_price <= self.session_high_price):
            raise ValueError("Session open must lie within the day-so-far range")
        if self.exhaustion_active:
            if self.exhausted_side not in (DirectionalBias.UP, DirectionalBias.DOWN):
                raise ValueError("Active exhaustion context requires UP or DOWN exhausted_side")
            if self.exhaustion_started_at is None:
                raise ValueError("Active exhaustion context requires exhaustion_started_at")
            if (
                self.exhaustion_expires_at is not None
                and self.exhaustion_expires_at < self.snapshot_time
            ):
                raise ValueError("Active exhaustion context cannot already be expired")
        return self


class AdvisorDecision(ContractModel):
    symbol: str = Field(min_length=1)
    snapshot_time: datetime
    action: AdvisorAction
    selected_candidate_id: Optional[str] = None
    reason_codes: Tuple[str, ...] = ()
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
    config_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_advisor(self) -> "AdvisorDecision":
        if self.action in {AdvisorAction.ALLOW, AdvisorAction.WATCH, AdvisorAction.BLOCK}:
            if not self.selected_candidate_id:
                raise ValueError("Advisor decision requires selected_candidate_id")
        if self.action is AdvisorAction.NO_ACTION and self.selected_candidate_id is not None:
            raise ValueError("Advisor NO_ACTION must not select a candidate")
        return self


class ManagerDecision(ContractModel):
    symbol: str = Field(min_length=1)
    snapshot_time: datetime
    action: ManagerAction
    selected_candidate_id: Optional[str] = None
    same_direction_support_ids: Tuple[str, ...] = ()
    opposing_candidate_ids: Tuple[str, ...] = ()
    material_opposition: bool = False
    active_signal_id: Optional[str] = None
    reason_codes: Tuple[str, ...] = ()
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
    config_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_manager(self) -> "ManagerDecision":
        if self.action is ManagerAction.SELECT and not self.selected_candidate_id:
            raise ValueError("ManagerAction.SELECT requires selected_candidate_id")
        if self.material_opposition and not self.opposing_candidate_ids:
            raise ValueError("material_opposition requires opposing_candidate_ids")
        return self


class LocalDecision(ContractModel):
    """Signal-agnostic local Auction Engine assessment.

    The contract contains only the Setup Manager conclusion and the currently
    selected local candidate. It has no active-signal or persistence-payload fields.
    """

    symbol: str = Field(min_length=1)
    trading_day: date
    snapshot_time: datetime
    action: LocalDecisionAction
    selected_candidate: Optional[SetupCandidate] = None
    manager_decision: ManagerDecision
    reason_codes: Tuple[str, ...] = ()
    reasons: Tuple[Reason, ...] = ()
    valid_until: Optional[datetime] = None
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
    config_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_local_decision(self) -> "LocalDecision":
        if self.trading_day != self.snapshot_time.date():
            raise ValueError("LocalDecision.trading_day must match snapshot_time")
        if self.manager_decision.symbol != self.symbol:
            raise ValueError("ManagerDecision symbol must match LocalDecision symbol")
        if self.manager_decision.snapshot_time != self.snapshot_time:
            raise ValueError("ManagerDecision snapshot_time must match LocalDecision")
        if self.action is LocalDecisionAction.CONFIRMED and self.selected_candidate is None:
            raise ValueError("LOCAL_CONFIRMED requires selected_candidate")
        if self.selected_candidate is not None:
            if self.selected_candidate.symbol != self.symbol:
                raise ValueError("Selected candidate symbol must match LocalDecision")
            if self.selected_candidate.snapshot_time > self.snapshot_time:
                raise ValueError("Selected candidate cannot come from a future snapshot")
        if self.valid_until is not None and self.valid_until < self.snapshot_time:
            raise ValueError("LocalDecision.valid_until cannot precede snapshot_time")
        return self


class StoredStateEnvelope(ContractModel):
    namespace: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    trading_day: date
    snapshot_time: datetime
    event_key: str = Field(min_length=1)
    side: TradeSide = TradeSide.NONE
    state_name: str = Field(min_length=1)
    payload: Dict[str, Any] = Field(default_factory=dict)
    terminal: bool = False
    consumed: bool = False
    superseded: bool = False
    expires_at: Optional[datetime] = None
    reason_codes: Tuple[str, ...] = ()
    config_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_envelope(self) -> "StoredStateEnvelope":
        if self.trading_day != self.snapshot_time.date():
            raise ValueError("StoredStateEnvelope.trading_day must match snapshot_time")
        if self.consumed and not self.terminal:
            raise ValueError("A consumed state envelope must be terminal")
        return self


class AuctionEngineResult(ContractModel):
    symbol: str = Field(min_length=1)
    snapshot_time: datetime
    evidence: EvidenceSnapshot
    auction_state: AuctionState
    boundary_episode: Optional[BoundaryEpisode] = None
    candidates: Tuple[SetupCandidate, ...] = ()
    stock_context: StockContext
    manager_decision: ManagerDecision
    local_decision: LocalDecision
    diagnostics: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_result_alignment(self) -> "AuctionEngineResult":
        objects = (
            self.evidence,
            self.auction_state,
            self.stock_context,
            self.manager_decision,
            self.local_decision,
        )
        for obj in objects:
            if obj.symbol != self.symbol or obj.snapshot_time != self.snapshot_time:
                raise ValueError("AuctionEngineResult contracts must share symbol/snapshot_time")
        if self.boundary_episode is not None:
            if self.boundary_episode.symbol != self.symbol or self.boundary_episode.snapshot_time != self.snapshot_time:
                raise ValueError("BoundaryEpisode must align with AuctionEngineResult")
        for candidate in self.candidates:
            if candidate.symbol != self.symbol or candidate.snapshot_time != self.snapshot_time:
                raise ValueError("Every candidate must align with AuctionEngineResult")
        return self


class RunManifest(ContractModel):
    run_id: str = Field(min_length=1)
    run_type: str = Field(min_length=1)
    started_at: datetime
    completed_at: Optional[datetime] = None
    trading_days: Tuple[date, ...]
    git_commit: str = "UNKNOWN"
    git_tag: str = ""
    config_version: str = Field(min_length=1)
    config_hash: str = Field(min_length=1)
    database_name: str = Field(min_length=1)
    symbol_types: Tuple[str, ...] = ("EQ",)
    symbol_count: int = Field(default=0, ge=0)
    snapshot_count: int = Field(default=0, ge=0)
    enabled_families: Tuple[SetupFamily, ...] = ()
    notes: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_manifest(self) -> "RunManifest":
        if not self.trading_days:
            raise ValueError("RunManifest requires at least one trading day")
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")
        return self


class OutcomeMetrics(ContractModel):
    """Hindsight-only report contract; never an engine input."""

    candidate_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    side: TradeSide
    entry_time: datetime
    entry_price: float = Field(gt=0.0)
    measured_at: datetime
    mfe_pct_by_bars: Dict[int, float] = Field(default_factory=dict)
    mae_pct_by_bars: Dict[int, float] = Field(default_factory=dict)
    full_session_mfe_pct: Optional[float] = None
    full_session_mae_pct: Optional[float] = None
    eod_pnl_pct: Optional[float] = None
    time_to_favorable_minutes: Optional[float] = Field(default=None, ge=0.0)
    time_to_adverse_minutes: Optional[float] = Field(default=None, ge=0.0)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_outcome(self) -> "OutcomeMetrics":
        if self.side not in (TradeSide.BUY, TradeSide.SELL):
            raise ValueError("OutcomeMetrics.side must be BUY or SELL")
        if self.measured_at < self.entry_time:
            raise ValueError("OutcomeMetrics.measured_at cannot precede entry_time")
        if any(int(k) <= 0 for k in self.mfe_pct_by_bars):
            raise ValueError("MFE horizons must be positive bar counts")
        if any(int(k) <= 0 for k in self.mae_pct_by_bars):
            raise ValueError("MAE horizons must be positive bar counts")
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
    "TradeSide", "BoundarySide", "DirectionalBias", "QualityStatus",
    "EvidencePolarity", "AuctionStateName", "AdvisorAction",
    "BoundaryEpisodeStatus",
    "BoundaryResolution", "SetupFamily", "CandidateEligibility", "CandidateRole",
    "ContextAlignment", "ManagerAction", "LocalDecisionAction",
    "ContractModel", "Reason", "SourceQuality", "EvidenceFact", "ConfidenceChannel",
    "BarEvidence", "PriceActionEvidence", "BoundaryObservation", "TrendEvidence",
    "CompressionEvidence", "ExtensionEvidence", "OpportunityEvidence",
    "MarketContextEvidence", "DerivativesContextEvidence", "EvidenceSnapshot",
    "AuctionState", "FrozenRange", "BoundaryEpisode", "SetupCandidate",
    "StockContext", "AdvisorDecision", "ManagerDecision", "LocalDecision",
    "StoredStateEnvelope", "AuctionEngineResult", "RunManifest",
    "OutcomeMetrics", "stable_key",
]
