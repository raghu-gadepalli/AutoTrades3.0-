from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from enums.auction_engine import (
    AdvisorAction,
    AuctionEventType,
    BalanceEpisodeState,
    DirectionalBias,
    DirectionalTransition,
    FreshDirection,
)
from enums.enums import LifecycleStage, SignalSide, SignalStatus
from schemas.signal import SignalSchema
from schemas.snapshot import SnapshotSchema
from services.auction_engine.contracts import AdvisorDecision
from services.auction_engine.episode_contracts import AuctionEvent, BalanceEpisodeProjection
from services.auction_engine.directional_contracts import (
    FreshDirectionalEvidence,
    AuctionSnapshotProjection,
    DirectionalProjection,
)
from services.auction_engine.structural_permissions import StructuralPermissionMatrix
from services.signals.signal_generator import SignalAssembler, _signal_id


TS = datetime(2026, 8, 3, 10, 0)


class _Fetcher:
    def __init__(self) -> None:
        self.active = None
        self.by_id = {}
        self.symbol = SimpleNamespace(
            symbol="TEST",
            equity_ref="TEST",
            active=True,
            generate_signals=True,
        )

    def fetch_symbol(self, symbol):
        return self.symbol

    def fetch_active_signal(self, equity_ref, lifecycle):
        return self.active

    def fetch_signal_by_id(self, signal_id):
        return self.by_id.get(signal_id)


class _Persister:
    def __init__(self, fetcher: _Fetcher) -> None:
        self.fetcher = fetcher
        self.closed_statuses = []
        self.created_replaced_opportunity_keys = []
        self.closed_replacement_candidates = []

    def create(
        self,
        *,
        snapshot,
        equity_ref,
        lifecycle,
        candidate,
        meta_json,
        criteria_json,
        analytics,
        evaluation_diagnostic,
        replaced_opportunity_key=None,
    ):
        self.created_replaced_opportunity_keys.append(replaced_opportunity_key)
        signal = SignalSchema.model_validate({
            "signal_id": _signal_id(candidate.opportunity_key),
            "equity_ref": equity_ref,
            "symbol": snapshot.symbol,
            "lifecycle": lifecycle,
            "setup": candidate.setup_family.value,
            "side": SignalSide.from_string(candidate.side.value),
            "stage": LifecycleStage.ACTIVE,
            "status": SignalStatus.OPEN,
            "status_reason": "EVENT_DRIVEN_SETUP_CREATED",
            "first_seen_time": snapshot.snapshot_time,
            "created_price": Decimal(str(snapshot.close)),
            "last_eval_time": snapshot.snapshot_time,
            "last_snapshot_time": snapshot.snapshot_time,
            "criteria_json": criteria_json,
            "snapshot_json": {},
            "meta_json": meta_json,
            "last_price": Decimal(str(snapshot.close)),
            "ltp": Decimal(str(snapshot.ltp)),
        })
        self.fetcher.active = signal
        self.fetcher.by_id[signal.signal_id] = signal
        return signal

    def update(self, **kwargs):
        signal = kwargs["signal"]
        snapshot = kwargs["snapshot"]
        updated = signal.model_copy(update={
            "stage": kwargs["stage"],
            "status_reason": kwargs["reason"],
            "last_eval_time": snapshot.snapshot_time,
            "last_snapshot_time": snapshot.snapshot_time,
            "meta_json": kwargs["meta_json"],
        })
        self.fetcher.active = updated
        return updated

    def close(self, **kwargs):
        signal = kwargs["signal"]
        snapshot = kwargs["snapshot"]
        status = kwargs["status"]
        self.closed_statuses.append(status)
        self.closed_replacement_candidates.append(
            kwargs.get("replacement_candidate")
        )
        closed = signal.model_copy(update={
            "stage": LifecycleStage.FORCE_EXIT,
            "status": status,
            "status_reason": kwargs["reason"],
            "last_eval_time": snapshot.snapshot_time,
            "last_snapshot_time": snapshot.snapshot_time,
            "meta_json": kwargs["meta_json"],
        })
        self.fetcher.active = None
        self.fetcher.by_id[closed.signal_id] = closed
        return closed

    def complete_opportunity(self, **kwargs):
        return None

    def close_at_intraday_cutoff(self, **kwargs):
        raise AssertionError("Cutoff is not part of this test")


class _Advisor:
    def __init__(self, action: AdvisorAction = AdvisorAction.ALLOW) -> None:
        self.action = action

    def evaluate_authoritative(self, snapshot, candidate):
        return AdvisorDecision(
            symbol=snapshot.symbol,
            snapshot_time=snapshot.snapshot_time,
            action=self.action,
            selected_candidate_id=candidate.candidate_id,
            reason_codes=("TEST_ADVISOR",),
            diagnostics={},
        )


def _balance(ts: datetime, state: BalanceEpisodeState) -> BalanceEpisodeProjection:
    return BalanceEpisodeProjection.model_construct(
        episode_id="BAL:1",
        previous_state=BalanceEpisodeState.LOCKED,
        current_state=state,
        started_at=ts - timedelta(minutes=6),
        state_started_at=ts,
        state_age_bars=0,
        range_id="RANGE:1",
        frozen_low=99.0,
        frozen_high=101.0,
        containment_ratio=1.0,
        escape_direction=DirectionalBias.UP,
        failed_escape_count=0,
        escape_attempt_count=1,
        rearm_required=False,
        attempt_limit_reached=False,
    )


def _snapshot(
    ts: datetime,
    *,
    event_type: AuctionEventType,
    direction: DirectionalBias,
    balance_state: BalanceEpisodeState,
    data: dict,
    close: float,
) -> SnapshotSchema:
    event = AuctionEvent(
        event_id=f"EVT:{event_type.value}:{ts.isoformat()}",
        event_type=event_type,
        episode_id=("DIR:2" if event_type is AuctionEventType.DIRECTIONAL_REVERSED else "BAL:1"),
        symbol="TEST",
        trading_day=date(2026, 8, 3),
        event_time=ts,
        direction=direction,
        reason_codes=("TEST_EVENT",),
        data=data,
    )
    balance = _balance(ts, balance_state)
    permissions = StructuralPermissionMatrix().evaluate(
        balance_state=balance_state,
        events=(event,),
    )
    directional = DirectionalProjection(
        active_episode_id="DIR:2",
        previous_episode_id="DIR:1",
        direction=direction,
        started_at=ts - timedelta(minutes=3),
        confirmed_at=ts,
        last_confirmed_at=ts,
        start_price=close,
        extreme_price=close,
        age_bars=1,
        transition=(
            DirectionalTransition.REVERSED
            if event_type is AuctionEventType.DIRECTIONAL_REVERSED
            else DirectionalTransition.NONE
        ),
        transition_reason="TEST",
    )
    evidence = FreshDirectionalEvidence(
        side=FreshDirection(direction.value),
        candidate_side=direction,
        observed_at=ts,
        trend_direction=direction,
        raw_structure_side=direction,
        slope_direction=direction,
        directional_efficiency=0.8,
        support_facts=("TREND_DIRECTION", "RAW_STRUCTURE"),
        reason_codes=("TEST",),
    )
    auction = AuctionSnapshotProjection.model_construct(
        status="OK",
        continuity_mode="COLD_START",
        evidence=evidence,
        directional=directional,
        balance=balance,
        events=(event,),
        permissions=permissions,
    )
    return SnapshotSchema.model_construct(
        symbol="TEST",
        snapshot_time=ts,
        tf="3m",
        close=close,
        ltp=close,
        ltp_time=ts,
        gen_signals=True,
        bar=SimpleNamespace(open=close, high=close + 0.5, low=close - 0.5, close=close),
        indicators=SimpleNamespace(atr=SimpleNamespace(value=1.0)),
        auction=auction,
        memory=SimpleNamespace(
            structure=SimpleNamespace(
                bars_3m=(
                    SimpleNamespace(low=close - 1.0, high=close + 0.5),
                    SimpleNamespace(low=close - 0.5, high=close + 1.0),
                )
            )
        ),
    )


def test_auction_event_can_create_signal() -> None:
    snapshot = _snapshot(
        TS,
        event_type=AuctionEventType.BALANCE_ESCAPE_STARTED,
        direction=DirectionalBias.UP,
        balance_state=BalanceEpisodeState.ESCAPE_WATCH,
        data={"frozen_low": 99.0, "frozen_high": 101.0},
        close=101.2,
    )
    fetcher = _Fetcher()
    persister = _Persister(fetcher)
    events = SignalAssembler(
        fetcher=fetcher,
        persister=persister,
        advisor=_Advisor(),
    ).assemble(snapshot)

    assert [action for action, _signal in events] == ["CREATE"]
    assert events[0][1].setup == "BREAKOUT_INITIATION"
    assert events[0][1].side is SignalSide.BUY


def test_confirmed_opposite_direction_invalidates_active_signal_when_new_setup_blocked() -> None:
    fetcher = _Fetcher()
    persister = _Persister(fetcher)
    assembler = SignalAssembler(
        fetcher=fetcher,
        persister=persister,
        advisor=_Advisor(),
    )
    start = _snapshot(
        TS,
        event_type=AuctionEventType.BALANCE_ESCAPE_STARTED,
        direction=DirectionalBias.UP,
        balance_state=BalanceEpisodeState.ESCAPE_WATCH,
        data={"frozen_low": 99.0, "frozen_high": 101.0},
        close=101.2,
    )
    assert [action for action, _signal in assembler.assemble(start)] == ["CREATE"]

    reversal = _snapshot(
        TS + timedelta(minutes=3),
        event_type=AuctionEventType.DIRECTIONAL_REVERSED,
        direction=DirectionalBias.DOWN,
        balance_state=BalanceEpisodeState.ESCAPE_WATCH,
        data={"start_price": 100.5},
        close=100.5,
    )
    events = assembler.assemble(reversal)

    assert [action for action, _signal in events] == ["CLOSE"]
    assert events[0][1].status is SignalStatus.INVALIDATED
    assert persister.closed_statuses == [SignalStatus.INVALIDATED]
    assert fetcher.active is None


def test_confirmed_opposite_direction_invalidates_when_advisor_defers_new_setup() -> None:
    fetcher = _Fetcher()
    persister = _Persister(fetcher)
    advisor = _Advisor()
    assembler = SignalAssembler(
        fetcher=fetcher,
        persister=persister,
        advisor=advisor,
    )
    start = _snapshot(
        TS,
        event_type=AuctionEventType.BALANCE_ESCAPE_STARTED,
        direction=DirectionalBias.UP,
        balance_state=BalanceEpisodeState.ESCAPE_WATCH,
        data={"frozen_low": 99.0, "frozen_high": 101.0},
        close=101.2,
    )
    assert [action for action, _signal in assembler.assemble(start)] == ["CREATE"]

    advisor.action = AdvisorAction.WATCH
    reversal = _snapshot(
        TS + timedelta(minutes=3),
        event_type=AuctionEventType.DIRECTIONAL_REVERSED,
        direction=DirectionalBias.DOWN,
        balance_state=BalanceEpisodeState.NONE,
        data={"start_price": 100.5},
        close=100.5,
    )
    events = assembler.assemble(reversal)

    assert [action for action, _signal in events] == ["CLOSE"]
    assert events[0][1].status is SignalStatus.INVALIDATED
    assert persister.closed_statuses == [SignalStatus.INVALIDATED]
    assert persister.closed_replacement_candidates == [None]
    assert assembler.last_evaluation_diagnostics[0]["outcome"] == (
        "ADVISOR_WATCH_DEFERRED"
    )
    assert fetcher.active is None


def test_same_side_directional_event_progresses_existing_breakout_signal() -> None:
    fetcher = _Fetcher()
    persister = _Persister(fetcher)
    assembler = SignalAssembler(
        fetcher=fetcher,
        persister=persister,
        advisor=_Advisor(),
    )
    breakout = _snapshot(
        TS,
        event_type=AuctionEventType.BALANCE_ESCAPE_STARTED,
        direction=DirectionalBias.DOWN,
        balance_state=BalanceEpisodeState.ESCAPE_WATCH,
        data={"frozen_low": 99.0, "frozen_high": 101.0},
        close=98.8,
    )
    created_events = assembler.assemble(breakout)
    assert [action for action, _signal in created_events] == ["CREATE"]
    created = created_events[0][1]

    reversal = _snapshot(
        TS + timedelta(minutes=3),
        event_type=AuctionEventType.DIRECTIONAL_REVERSED,
        direction=DirectionalBias.DOWN,
        balance_state=BalanceEpisodeState.NONE,
        data={"start_price": 98.5},
        close=98.5,
    )
    progressed_events = assembler.assemble(reversal)

    assert [action for action, _signal in progressed_events] == ["UPDATE"]
    progressed = progressed_events[0][1]
    assert progressed.signal_id == created.signal_id
    assert progressed.setup == "BREAKOUT_INITIATION"
    assert progressed.stage is LifecycleStage.EXPAND
    assert assembler.last_evaluation_diagnostics[0]["outcome"] == (
        "SAME_SIDE_AUTHORITATIVE_PROGRESSION_UPDATE"
    )
    lineage = progressed.meta_json["authoritative_event_lineage"]
    assert [item["source_event_type"] for item in lineage] == [
        "BALANCE_ESCAPE_STARTED",
        "DIRECTIONAL_REVERSED",
    ]
    latest = progressed.meta_json["latest_authoritative_progression"]
    assert latest["setup_family"] == "REVERSAL"
    assert latest["side"] == "SELL"


def test_opposite_side_directional_event_does_not_progress_active_signal() -> None:
    fetcher = _Fetcher()
    persister = _Persister(fetcher)
    assembler = SignalAssembler(
        fetcher=fetcher,
        persister=persister,
        advisor=_Advisor(),
    )
    breakout = _snapshot(
        TS,
        event_type=AuctionEventType.BALANCE_ESCAPE_STARTED,
        direction=DirectionalBias.UP,
        balance_state=BalanceEpisodeState.ESCAPE_WATCH,
        data={"frozen_low": 99.0, "frozen_high": 101.0},
        close=101.2,
    )
    created_events = assembler.assemble(breakout)
    assert [action for action, _signal in created_events] == ["CREATE"]
    created = created_events[0][1]

    reversal = _snapshot(
        TS + timedelta(minutes=3),
        event_type=AuctionEventType.DIRECTIONAL_REVERSED,
        direction=DirectionalBias.DOWN,
        balance_state=BalanceEpisodeState.NONE,
        data={"start_price": 100.5},
        close=100.5,
    )
    events = assembler.assemble(reversal)

    assert [action for action, _signal in events] == ["REPLACE", "CREATE"]
    assert events[0][1].status is SignalStatus.REPLACED
    assert events[1][1].side is SignalSide.SELL
    assert events[1][1].signal_id != events[0][1].signal_id
    assert persister.closed_statuses[-1] is SignalStatus.REPLACED
    assert persister.closed_replacement_candidates[-1] is not None
    assert persister.created_replaced_opportunity_keys[-1] == (
        created.meta_json["auction_signal"]["opportunity_key"]
    )
    assert assembler.last_evaluation_diagnostics[0]["outcome"] == (
        "CREATED_AFTER_REPLACEMENT"
    )
