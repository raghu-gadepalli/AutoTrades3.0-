from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from enums.auction_engine import (
    AdvisorAction,
    AuctionEventType,
    DirectionalBias,
    SetupFamily,
    StructuralPermissionResult,
)
from enums.enums import LifecycleStage, SignalSide, SignalStatus
from schemas.signal import SignalSchema
from services.auction_engine.contracts import AdvisorDecision
from services.signals.signal_generator import SignalAssembler, SignalFetcher, _signal_id
from services.trade.monitor.signal_contract import AuctionTradeSignalContext
from tests.unit.test_event_driven_setup_engine import _event_snapshot


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
        self.created_candidate = None
        self.created_evaluation = None
        self.completed_routes = []
        self.cutoff_closes = []
        self.progressed_candidates = []
        self.terminal_records = []

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
        self.created_evaluation = evaluation_diagnostic
        self.created_candidate = candidate
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
            "snapshot_json": snapshot.model_dump(mode="python", by_alias=True),
            "meta_json": meta_json,
            "last_price": Decimal(str(snapshot.close)),
            "ltp": Decimal(str(snapshot.ltp)),
        })
        self.fetcher.active = signal
        self.fetcher.by_id[signal.signal_id] = signal
        return signal

    def update(
        self,
        *,
        signal,
        snapshot,
        stage,
        reason,
        meta_json,
        criteria_json,
        analytics,
        progression_candidate=None,
        evaluation_diagnostic=None,
    ):
        if progression_candidate is not None:
            self.progressed_candidates.append((progression_candidate, evaluation_diagnostic))
        updated = signal.model_copy(update={
            "stage": stage,
            "status_reason": reason,
            "last_eval_time": snapshot.snapshot_time,
            "last_snapshot_time": snapshot.snapshot_time,
            "meta_json": meta_json,
        })
        self.fetcher.active = updated
        return updated

    def close(
        self,
        *,
        signal,
        snapshot,
        status,
        reason,
        meta_json,
        criteria_json,
        analytics,
        terminal_route=None,
        replacement_candidate=None,
    ):
        self.terminal_records.append((status, terminal_route, replacement_candidate))
        closed = signal.model_copy(update={
            "stage": LifecycleStage.FORCE_EXIT,
            "status": status,
            "status_reason": reason,
            "last_eval_time": snapshot.snapshot_time,
            "last_snapshot_time": snapshot.snapshot_time,
            "meta_json": meta_json,
        })
        self.fetcher.active = None
        return closed

    def complete_opportunity(self, *, signal, snapshot, route):
        self.completed_routes.append(route)

    def close_at_intraday_cutoff(
        self,
        *,
        signal,
        snapshot,
        meta_json,
        analytics,
    ):
        self.cutoff_closes.append(signal.signal_id)
        closed = signal.model_copy(update={
            "stage": LifecycleStage.FORCE_EXIT,
            "status": SignalStatus.CLOSED,
            "status_reason": "INTRADAY_SIGNAL_CUTOFF",
            "last_eval_time": snapshot.snapshot_time,
            "last_snapshot_time": snapshot.snapshot_time,
            "meta_json": meta_json,
        })
        self.fetcher.active = None
        self.fetcher.by_id[signal.signal_id] = closed
        return closed


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


def test_symbol_generate_signals_gate_uses_schema_field() -> None:
    snapshot = _event_snapshot(
        AuctionEventType.DIRECTIONAL_REVERSAL_LEG_ESTABLISHED,
        SetupFamily.REVERSAL,
        direction=DirectionalBias.UP,
        close=102.0,
        data={"origin_price": 100.0},
    )
    fetcher = _Fetcher()
    fetcher.symbol.generate_signals = False
    persister = _Persister(fetcher)
    assembler = SignalAssembler(fetcher=fetcher, persister=persister, advisor=_Advisor())

    assert assembler.assemble(snapshot) == []
    assert persister.created_candidate is None


def test_signal_creation_uses_only_authoritative_event_identity() -> None:
    snapshot = _event_snapshot(
        AuctionEventType.DIRECTIONAL_REVERSAL_LEG_ESTABLISHED,
        SetupFamily.REVERSAL,
        direction=DirectionalBias.UP,
        close=102.0,
        data={"origin_price": 100.0},
    )
    fetcher = _Fetcher()
    persister = _Persister(fetcher)
    assembler = SignalAssembler(fetcher=fetcher, persister=persister, advisor=_Advisor())
    events = assembler.assemble(snapshot)
    assert [item[0] for item in events] == ["CREATE"]
    signal = events[0][1]
    identity = signal.meta_json["auction_signal"]
    candidate = persister.created_candidate
    assert identity["source_event_id"] == candidate.source_event_id
    assert identity["source_event_type"] == candidate.source_event_type.value
    assert identity["source_episode_id"] == candidate.source_episode_id
    assert identity["opportunity_key"] == candidate.opportunity_key
    assert signal.setup == "REVERSAL"
    downstream = AuctionTradeSignalContext.from_signal(signal)
    assert downstream.contract_version == "AUCTION_SIGNAL_DOWNSTREAM_V2"
    assert downstream.opportunity_key == candidate.opportunity_key
    assert downstream.candidate_id == candidate.candidate_id
    assert downstream.boundary_event_key == candidate.source_event_id


def test_structural_block_cannot_create_signal() -> None:
    snapshot = _event_snapshot(
        AuctionEventType.DIRECTIONAL_REVERSAL_LEG_ESTABLISHED,
        SetupFamily.REVERSAL,
        result=StructuralPermissionResult.BLOCK,
        close=102.0,
        data={"origin_price": 100.0},
    )
    fetcher = _Fetcher()
    persister = _Persister(fetcher)
    assembler = SignalAssembler(fetcher=fetcher, persister=persister, advisor=_Advisor())
    assert assembler.assemble(snapshot) == []
    assert persister.created_candidate is None
    assert assembler.last_evaluation_diagnostics[0]["outcome"] == "STRUCTURAL_PERMISSION_BLOCKED"
    assert assembler.last_evaluation_diagnostics[0]["blockers"] == ("STRUCTURAL_PERMISSION_BLOCK",)


def test_advisor_watch_defers_new_signal_creation() -> None:
    snapshot = _event_snapshot(
        AuctionEventType.BALANCE_ESCAPE_STARTED,
        SetupFamily.BREAKOUT_INITIATION,
        direction=DirectionalBias.UP,
        close=101.2,
        data={"frozen_low": 99.0, "frozen_high": 101.0},
    )
    fetcher = _Fetcher()
    persister = _Persister(fetcher)
    assembler = SignalAssembler(
        fetcher=fetcher,
        persister=persister,
        advisor=_Advisor(AdvisorAction.WATCH),
    )

    assert assembler.assemble(snapshot) == []
    assert persister.created_candidate is None
    assert fetcher.active is None
    diagnostic = assembler.last_evaluation_diagnostics[0]
    assert diagnostic["outcome"] == "ADVISOR_WATCH_DEFERRED"
    assert diagnostic["advisor_action"] == AdvisorAction.WATCH.value
    assert diagnostic["advisor_reason_codes"] == ("TEST_ADVISOR",)


def test_watched_initiation_can_deploy_on_later_allowed_acceptance() -> None:
    initiation = _event_snapshot(
        AuctionEventType.BALANCE_ESCAPE_STARTED,
        SetupFamily.BREAKOUT_INITIATION,
        direction=DirectionalBias.UP,
        close=101.2,
        data={"frozen_low": 99.0, "frozen_high": 101.0},
    )
    accepted = _event_snapshot(
        AuctionEventType.BALANCE_ESCAPE_ACCEPTED,
        SetupFamily.ACCEPTED_BREAKOUT,
        direction=DirectionalBias.UP,
        close=101.4,
        data={"frozen_low": 99.0, "frozen_high": 101.0},
    )
    fetcher = _Fetcher()
    persister = _Persister(fetcher)

    deferred = SignalAssembler(
        fetcher=fetcher,
        persister=persister,
        advisor=_Advisor(AdvisorAction.WATCH),
    ).assemble(initiation)
    assert deferred == []
    assert fetcher.active is None

    events = SignalAssembler(
        fetcher=fetcher,
        persister=persister,
        advisor=_Advisor(AdvisorAction.ALLOW),
    ).assemble(accepted)
    assert [item[0] for item in events] == ["CREATE"]
    assert events[0][1].setup == SetupFamily.ACCEPTED_BREAKOUT.value


def test_advisor_block_suppresses_only_new_creation() -> None:
    snapshot = _event_snapshot(
        AuctionEventType.BALANCE_ESCAPE_ACCEPTED,
        SetupFamily.ACCEPTED_BREAKOUT,
        close=101.2,
        data={"frozen_low": 99.0, "frozen_high": 101.0},
    )
    fetcher = _Fetcher()
    persister = _Persister(fetcher)
    assembler = SignalAssembler(
        fetcher=fetcher,
        persister=persister,
        advisor=_Advisor(AdvisorAction.BLOCK),
    )
    assert assembler.assemble(snapshot) == []
    assert persister.created_candidate is None
    diagnostic = assembler.last_evaluation_diagnostics[0]
    assert diagnostic["outcome"] == "ADVISOR_BLOCK"
    assert diagnostic["advisor_action"] == AdvisorAction.BLOCK.value
    assert diagnostic["advisor_reason_codes"] == ("TEST_ADVISOR",)


def test_advisor_is_not_reapplied_to_same_active_opportunity() -> None:
    snapshot = _event_snapshot(
        AuctionEventType.DIRECTIONAL_REVERSAL_LEG_ESTABLISHED,
        SetupFamily.REVERSAL,
        direction=DirectionalBias.UP,
        close=102.0,
        data={"origin_price": 100.0},
    )
    fetcher = _Fetcher()
    persister = _Persister(fetcher)
    creator = SignalAssembler(fetcher=fetcher, persister=persister, advisor=_Advisor())
    assert creator.assemble(snapshot)[0][0] == "CREATE"

    blocker = SignalAssembler(
        fetcher=fetcher,
        persister=persister,
        advisor=_Advisor(AdvisorAction.BLOCK),
    )
    events = blocker.assemble(snapshot)
    assert [item[0] for item in events] == ["UPDATE"]
    assert events[0][1].status is SignalStatus.OPEN


def test_consumed_event_identity_cannot_create_duplicate_signal() -> None:
    snapshot = _event_snapshot(
        AuctionEventType.BALANCE_ESCAPE_ACCEPTED,
        SetupFamily.ACCEPTED_BREAKOUT,
        close=101.2,
        data={"frozen_low": 99.0, "frozen_high": 101.0},
    )
    fetcher = _Fetcher()
    persister = _Persister(fetcher)
    assembler = SignalAssembler(fetcher=fetcher, persister=persister, advisor=_Advisor())
    created = assembler.assemble(snapshot)[0][1]
    fetcher.active = None
    fetcher.by_id[created.signal_id] = created.model_copy(
        update={"status": SignalStatus.CLOSED, "stage": LifecycleStage.FORCE_EXIT}
    )
    assert assembler.assemble(snapshot) == []


@pytest.mark.parametrize(
    ("event_type", "family", "direction", "close", "data"),
    (
        (AuctionEventType.BALANCE_ESCAPE_STARTED, SetupFamily.BREAKOUT_INITIATION, DirectionalBias.UP, 101.2, {"frozen_low": 99.0, "frozen_high": 101.0}),
        (AuctionEventType.BALANCE_ESCAPE_ACCEPTED, SetupFamily.ACCEPTED_BREAKOUT, DirectionalBias.UP, 101.2, {"frozen_low": 99.0, "frozen_high": 101.0}),
        (AuctionEventType.BALANCE_ESCAPE_FAILED, SetupFamily.FAILED_BREAKOUT, DirectionalBias.DOWN, 100.5, {"frozen_low": 99.0, "frozen_high": 101.0}),
        (AuctionEventType.DIRECTIONAL_CONTINUATION_CONFIRMED, SetupFamily.CONTINUATION, DirectionalBias.UP, 102.0, {"origin_price": 101.4, "protection_level": 100.8}),
        (AuctionEventType.DIRECTIONAL_REACCELERATION_CONFIRMED, SetupFamily.REACCELERATION, DirectionalBias.UP, 102.0, {"origin_price": 101.4, "protection_level": 100.8}),
        (AuctionEventType.DIRECTIONAL_REVERSAL_LEG_ESTABLISHED, SetupFamily.REVERSAL, DirectionalBias.UP, 102.0, {"origin_price": 100.0}),
    ),
)
def test_all_six_families_reach_signal_persistence_only_from_events(
    event_type, family, direction, close, data
) -> None:
    snapshot = _event_snapshot(
        event_type,
        family,
        direction=direction,
        close=close,
        data=data,
    )
    fetcher = _Fetcher()
    persister = _Persister(fetcher)
    events = SignalAssembler(
        fetcher=fetcher,
        persister=persister,
        advisor=_Advisor(),
    ).assemble(snapshot)
    assert [item[0] for item in events] == ["CREATE"]
    signal = events[0][1]
    candidate = persister.created_candidate
    assert signal.setup == family.value
    assert candidate.source_event_type is event_type
    assert signal.meta_json["auction_signal"]["source_event_id"] == candidate.source_event_id
    assert signal.meta_json["setup_levels"]["setup_contract_version"] == "AUTHORITATIVE_SETUP_V1"


def test_directional_completion_keeps_signal_open_and_completes_opportunity() -> None:
    create_snapshot = _event_snapshot(
        AuctionEventType.DIRECTIONAL_REVERSAL_LEG_ESTABLISHED,
        SetupFamily.REVERSAL,
        direction=DirectionalBias.UP,
        close=102.0,
        data={"origin_price": 100.0},
    )
    fetcher = _Fetcher()
    persister = _Persister(fetcher)
    assembler = SignalAssembler(fetcher=fetcher, persister=persister, advisor=_Advisor())
    created = assembler.assemble(create_snapshot)[0][1]

    completed_snapshot = _event_snapshot(
        AuctionEventType.DIRECTIONAL_COMPLETED,
        SetupFamily.REVERSAL,
        direction=DirectionalBias.UP,
        close=102.1,
    )
    events = assembler.assemble(completed_snapshot)

    assert [item[0] for item in events] == ["HOLD"]
    held = events[0][1]
    assert held.signal_id == created.signal_id
    assert held.status is SignalStatus.OPEN
    assert held.stage is not LifecycleStage.FORCE_EXIT
    assert held.meta_json["management"]["should_exit_signal"] is False
    assert held.meta_json["lifecycle"]["trade_action"] == "HOLD_POSITION"
    assert [route.source_event_type for route in persister.completed_routes] == [
        AuctionEventType.DIRECTIONAL_COMPLETED
    ]
    assert persister.cutoff_closes == []


def test_balance_completion_keeps_signal_open_and_completes_opportunity() -> None:
    create_snapshot = _event_snapshot(
        AuctionEventType.BALANCE_ESCAPE_ACCEPTED,
        SetupFamily.ACCEPTED_BREAKOUT,
        direction=DirectionalBias.UP,
        close=101.2,
        data={"frozen_low": 99.0, "frozen_high": 101.0},
    )
    fetcher = _Fetcher()
    persister = _Persister(fetcher)
    assembler = SignalAssembler(fetcher=fetcher, persister=persister, advisor=_Advisor())
    created = assembler.assemble(create_snapshot)[0][1]

    completed_snapshot = _event_snapshot(
        AuctionEventType.BALANCE_COMPLETED,
        SetupFamily.ACCEPTED_BREAKOUT,
        direction=DirectionalBias.UP,
        close=101.4,
    )
    events = assembler.assemble(completed_snapshot)

    assert [item[0] for item in events] == ["HOLD"]
    held = events[0][1]
    assert held.signal_id == created.signal_id
    assert held.status is SignalStatus.OPEN
    assert held.stage is not LifecycleStage.FORCE_EXIT
    assert held.meta_json["management"]["should_exit_signal"] is False
    assert held.meta_json["lifecycle"]["trade_action"] == "HOLD_POSITION"
    assert [route.source_event_type for route in persister.completed_routes] == [
        AuctionEventType.BALANCE_COMPLETED
    ]
    assert persister.cutoff_closes == []


def test_intraday_cutoff_closes_open_signal_and_requests_trade_exit() -> None:
    create_snapshot = _event_snapshot(
        AuctionEventType.BALANCE_ESCAPE_ACCEPTED,
        SetupFamily.ACCEPTED_BREAKOUT,
        direction=DirectionalBias.UP,
        close=101.2,
        data={"frozen_low": 99.0, "frozen_high": 101.0},
    )
    fetcher = _Fetcher()
    persister = _Persister(fetcher)
    assembler = SignalAssembler(fetcher=fetcher, persister=persister, advisor=_Advisor())
    created = assembler.assemble(create_snapshot)[0][1]

    cutoff_snapshot = create_snapshot.model_copy(update={
        "snapshot_time": datetime(2026, 7, 29, 15, 18),
        "gen_signals": False,
    })
    fetcher.symbol.active = False
    fetcher.symbol.generate_signals = False
    events = assembler.assemble(cutoff_snapshot)

    assert [item[0] for item in events] == ["CLOSE"]
    closed = events[0][1]
    assert closed.signal_id == created.signal_id
    assert closed.status is SignalStatus.CLOSED
    assert closed.stage is LifecycleStage.FORCE_EXIT
    assert closed.status_reason == "INTRADAY_SIGNAL_CUTOFF"
    assert closed.meta_json["management"]["should_exit_signal"] is True
    assert closed.meta_json["lifecycle"]["trade_action"] == "FORCE_EXIT"
    assert closed.meta_json["management"]["auction_action"] == "SESSION_CUTOFF"
    assert persister.cutoff_closes == [created.signal_id]


def test_intraday_cutoff_does_not_create_new_signal() -> None:
    snapshot = _event_snapshot(
        AuctionEventType.BALANCE_ESCAPE_ACCEPTED,
        SetupFamily.ACCEPTED_BREAKOUT,
        direction=DirectionalBias.UP,
        close=101.2,
        data={"frozen_low": 99.0, "frozen_high": 101.0},
    ).model_copy(update={"snapshot_time": datetime(2026, 7, 29, 15, 18)})
    fetcher = _Fetcher()
    persister = _Persister(fetcher)
    assembler = SignalAssembler(fetcher=fetcher, persister=persister, advisor=_Advisor())

    assert assembler.assemble(snapshot) == []
    assert persister.created_candidate is None


def test_same_episode_breakout_acceptance_progresses_signal_without_replacement() -> None:
    initiation_snapshot = _event_snapshot(
        AuctionEventType.BALANCE_ESCAPE_STARTED,
        SetupFamily.BREAKOUT_INITIATION,
        direction=DirectionalBias.UP,
        close=101.2,
        data={"frozen_low": 99.0, "frozen_high": 101.0},
    )
    fetcher = _Fetcher()
    persister = _Persister(fetcher)
    assembler = SignalAssembler(fetcher=fetcher, persister=persister, advisor=_Advisor())
    created = assembler.assemble(initiation_snapshot)[0][1]

    accepted_snapshot = _event_snapshot(
        AuctionEventType.BALANCE_ESCAPE_ACCEPTED,
        SetupFamily.ACCEPTED_BREAKOUT,
        direction=DirectionalBias.UP,
        close=101.3,
        data={"frozen_low": 99.0, "frozen_high": 101.0},
    )
    events = assembler.assemble(accepted_snapshot)

    assert [item[0] for item in events] == ["UPDATE"]
    progressed = events[0][1]
    assert progressed.signal_id == created.signal_id
    assert progressed.setup == SetupFamily.BREAKOUT_INITIATION.value
    assert progressed.status is SignalStatus.OPEN
    assert progressed.stage is LifecycleStage.EXPAND
    assert progressed.status_reason == "AUTHORITATIVE_OPPORTUNITY_PROGRESSED"
    progression = progressed.meta_json["latest_authoritative_progression"]
    assert progression["setup_family"] == SetupFamily.ACCEPTED_BREAKOUT.value
    assert progression["source_event_type"] == AuctionEventType.BALANCE_ESCAPE_ACCEPTED.value
    assert len(progressed.meta_json["authoritative_event_lineage"]) == 2
    assert len(persister.progressed_candidates) == 1
    assert persister.progressed_candidates[0][0].source_event_type is (
        AuctionEventType.BALANCE_ESCAPE_ACCEPTED
    )
    assert persister.completed_routes == []

    replayed = assembler.assemble(accepted_snapshot)
    assert [item[0] for item in replayed] == ["HOLD"]
    assert len(replayed[0][1].meta_json["authoritative_event_lineage"]) == 2


def test_structural_invalidation_still_force_exits_before_opposite_setup() -> None:
    initiation_snapshot = _event_snapshot(
        AuctionEventType.BALANCE_ESCAPE_STARTED,
        SetupFamily.BREAKOUT_INITIATION,
        direction=DirectionalBias.UP,
        close=101.2,
        data={"frozen_low": 99.0, "frozen_high": 101.0},
    )
    fetcher = _Fetcher()
    persister = _Persister(fetcher)
    assembler = SignalAssembler(fetcher=fetcher, persister=persister, advisor=_Advisor())
    created = assembler.assemble(initiation_snapshot)[0][1]

    failed_snapshot = _event_snapshot(
        AuctionEventType.BALANCE_ESCAPE_FAILED,
        SetupFamily.FAILED_BREAKOUT,
        direction=DirectionalBias.DOWN,
        close=100.5,
        data={"frozen_low": 99.0, "frozen_high": 101.0},
    )
    events = assembler.assemble(failed_snapshot)

    assert [item[0] for item in events] == ["CLOSE", "CREATE"]
    invalidated, replacement = events[0][1], events[1][1]
    assert invalidated.signal_id == created.signal_id
    assert invalidated.status is SignalStatus.INVALIDATED
    assert invalidated.stage is LifecycleStage.FORCE_EXIT
    assert replacement.setup == SetupFamily.FAILED_BREAKOUT.value
    assert replacement.side is SignalSide.SELL
    assert replacement.status is SignalStatus.OPEN
    terminal_status, terminal_route, replacement_candidate = persister.terminal_records[0]
    assert terminal_status is SignalStatus.INVALIDATED
    assert terminal_route.source_event_type is AuctionEventType.BALANCE_ESCAPE_FAILED
    assert replacement_candidate is None


def test_opposite_trend_restoration_invalidates_reversal_without_replacement() -> None:
    create_snapshot = _event_snapshot(
        AuctionEventType.DIRECTIONAL_REVERSAL_LEG_ESTABLISHED,
        SetupFamily.REVERSAL,
        direction=DirectionalBias.DOWN,
        close=98.0,
        data={"origin_price": 100.0},
        episode_id="EPISODE:DOWN:1",
    )
    fetcher = _Fetcher()
    persister = _Persister(fetcher)
    assembler = SignalAssembler(fetcher=fetcher, persister=persister, advisor=_Advisor())
    created = assembler.assemble(create_snapshot)[0][1]

    restored_snapshot = _event_snapshot(
        AuctionEventType.DIRECTIONAL_TREND_RESTORED,
        SetupFamily.CONTINUATION,
        direction=DirectionalBias.UP,
        close=101.0,
        data={},
        episode_id="EPISODE:UP:2",
    )
    events = assembler.assemble(restored_snapshot)

    assert [item[0] for item in events] == ["CLOSE"]
    invalidated = events[0][1]
    assert invalidated.signal_id == created.signal_id
    assert invalidated.status is SignalStatus.INVALIDATED
    assert invalidated.stage is LifecycleStage.FORCE_EXIT
    assert invalidated.status_reason == (
        "DIRECTIONAL_TREND_RESTORED_INVALIDATED_OPPOSITE_ACTIVE_SIGNAL"
    )
    diagnostic = assembler.last_evaluation_diagnostics[0]
    assert diagnostic["outcome"] == "SETUP_QUALITY_REJECTED"
    assert diagnostic["blockers"] == ("AUTHORITATIVE_STOP_GEOMETRY_REQUIRED",)


def test_same_direction_cross_episode_restoration_does_not_invalidate_reversal() -> None:
    create_snapshot = _event_snapshot(
        AuctionEventType.DIRECTIONAL_REVERSAL_LEG_ESTABLISHED,
        SetupFamily.REVERSAL,
        direction=DirectionalBias.UP,
        close=102.0,
        data={"origin_price": 100.0},
        episode_id="EPISODE:UP:1",
    )
    fetcher = _Fetcher()
    persister = _Persister(fetcher)
    assembler = SignalAssembler(fetcher=fetcher, persister=persister, advisor=_Advisor())
    created = assembler.assemble(create_snapshot)[0][1]

    restored_snapshot = _event_snapshot(
        AuctionEventType.DIRECTIONAL_TREND_RESTORED,
        SetupFamily.CONTINUATION,
        direction=DirectionalBias.UP,
        close=102.1,
        data={},
        episode_id="EPISODE:UP:2",
    )
    events = assembler.assemble(restored_snapshot)

    assert [item[0] for item in events] == ["HOLD"]
    held = events[0][1]
    assert held.signal_id == created.signal_id
    assert held.status is SignalStatus.OPEN
    assert held.stage is not LifecycleStage.FORCE_EXIT


def test_generate_events_preserves_replacement_and_creation_transitions() -> None:
    first_snapshot = _event_snapshot(
        AuctionEventType.DIRECTIONAL_REVERSAL_LEG_ESTABLISHED,
        SetupFamily.REVERSAL,
        direction=DirectionalBias.UP,
        close=102.0,
        data={"origin_price": 100.0},
        episode_id="EPISODE:UP:1",
    )
    fetcher = _Fetcher()
    persister = _Persister(fetcher)
    generator = SignalAssembler(fetcher=fetcher, persister=persister, advisor=_Advisor())
    generator.assemble(first_snapshot)

    replacement_snapshot = _event_snapshot(
        AuctionEventType.DIRECTIONAL_REVERSAL_LEG_ESTABLISHED,
        SetupFamily.REVERSAL,
        direction=DirectionalBias.DOWN,
        close=98.0,
        data={"origin_price": 100.0},
        episode_id="EPISODE:DOWN:2",
    )
    events = generator.assemble(replacement_snapshot)
    assert [action for action, _signal in events] == ["REPLACE", "CREATE"]
    assert events[0][1].meta_json["lifecycle"]["trade_action"] == "FORCE_EXIT"
    assert events[0][1].meta_json["management"]["should_exit_signal"] is True
    assert events[1][1].meta_json["lifecycle"]["trade_action"] == "CREATE_TRADE"
    assert generator.last_evaluation_diagnostics[0]["outcome"] == "CREATED_AFTER_REPLACEMENT"
    terminal_status, terminal_route, replacement_candidate = persister.terminal_records[0]
    assert terminal_status is SignalStatus.REPLACED
    assert terminal_route is None
    assert replacement_candidate.source_event_type is (
        AuctionEventType.DIRECTIONAL_REVERSAL_LEG_ESTABLISHED
    )


def test_balance_completion_keeps_price_deferred_signal_open() -> None:
    create_snapshot = _event_snapshot(
        AuctionEventType.BALANCE_ESCAPE_ACCEPTED,
        SetupFamily.ACCEPTED_BREAKOUT,
        direction=DirectionalBias.DOWN,
        close=99.8,
        data={"frozen_low": 100.0, "frozen_high": 102.0},
    )
    fetcher = _Fetcher()
    persister = _Persister(fetcher)
    assembler = SignalAssembler(fetcher=fetcher, persister=persister, advisor=_Advisor())
    created = assembler.assemble(create_snapshot)[0][1]

    meta = dict(created.meta_json)
    meta["trade_entry_state"] = {
        "contract_version": "TRADE_ENTRY_STATE_V1",
        "users": {
            "TESTUSER": {
                "state": "DEFERRED",
                "reason_code": "SIGNAL_ENTRY_WAIT_NOT_STRICTLY_PROFITABLE",
                "first_deferred_at": create_snapshot.snapshot_time,
                "last_evaluated_at": create_snapshot.snapshot_time,
                "evaluation_count": 1,
                "details": {},
            }
        },
    }
    deferred = created.model_copy(update={"meta_json": meta})
    fetcher.active = deferred
    fetcher.by_id[deferred.signal_id] = deferred

    completed_snapshot = _event_snapshot(
        AuctionEventType.BALANCE_COMPLETED,
        SetupFamily.ACCEPTED_BREAKOUT,
        direction=DirectionalBias.DOWN,
        close=99.6,
    )
    events = assembler.assemble(completed_snapshot)

    assert [item[0] for item in events] == ["HOLD"]
    held = events[0][1]
    assert held.signal_id == created.signal_id
    assert held.status is SignalStatus.OPEN
    assert held.meta_json["trade_entry_state"]["users"]["TESTUSER"]["state"] == "DEFERRED"
    assert [route.source_event_type for route in persister.completed_routes] == [
        AuctionEventType.BALANCE_COMPLETED
    ]
    assert persister.cutoff_closes == []


def test_opposite_parent_trend_restoration_replaces_active_reversal_when_proved() -> None:
    create_snapshot = _event_snapshot(
        AuctionEventType.DIRECTIONAL_REVERSAL_LEG_ESTABLISHED,
        SetupFamily.REVERSAL,
        direction=DirectionalBias.UP,
        close=102.0,
        data={"origin_price": 100.0},
        episode_id="EPISODE:REVERSAL:UP",
    )
    fetcher = _Fetcher()
    persister = _Persister(fetcher)
    assembler = SignalAssembler(fetcher=fetcher, persister=persister, advisor=_Advisor())
    created = assembler.assemble(create_snapshot)[0][1]

    restored_snapshot = _event_snapshot(
        AuctionEventType.DIRECTIONAL_TREND_RESTORED,
        SetupFamily.CONTINUATION,
        direction=DirectionalBias.DOWN,
        close=101.0,
        data={"origin_price": 101.0, "protection_level": 103.0},
        episode_id="EPISODE:REVERSAL:UP",
    )
    events = assembler.assemble(restored_snapshot)

    assert [item[0] for item in events] == ["CLOSE", "CREATE"]
    invalidated, replacement = events[0][1], events[1][1]
    assert invalidated.signal_id == created.signal_id
    assert invalidated.status is SignalStatus.INVALIDATED
    assert replacement.setup == SetupFamily.CONTINUATION.value
    assert replacement.side is SignalSide.SELL
    assert replacement.status is SignalStatus.OPEN
