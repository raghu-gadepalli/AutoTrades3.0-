"""Minimal authoritative directional episode tracker."""
from __future__ import annotations

from datetime import date, datetime
from typing import List, Tuple

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG, AuctionEngineConfig
from enums.auction_engine import (
    AuctionEventType,
    BalanceEpisodeState,
    DirectionalBias,
    DirectionalTransition,
    FreshDirection,
)
from services.auction_engine.episode_contracts import AuctionEvent
from services.auction_engine.directional_contracts import (
    FreshDirectionalEvidence,
    DirectionalMemory,
    DirectionalProjection,
)


class DirectionalStateMachine:
    """Advance one minimal directional memory from one completed snapshot."""

    def __init__(self, config: AuctionEngineConfig = AUCTION_ENGINE_CONFIG) -> None:
        self.config = config
        self.cfg = config.episode.directional

    def advance(
        self,
        *,
        symbol: str,
        trading_day: date,
        snapshot_time: datetime,
        close: float,
        high: float,
        low: float,
        evidence: FreshDirectionalEvidence,
        previous_memory: DirectionalMemory,
        balance_state: BalanceEpisodeState,
    ) -> tuple[
        DirectionalMemory,
        DirectionalProjection,
        Tuple[AuctionEvent, ...],
    ]:
        if snapshot_time.date() != trading_day:
            raise ValueError("Directional snapshot time must match trading day")
        payload = previous_memory.model_dump(mode="python")
        emitted_ids = set(payload.pop("emitted_event_ids"))
        reasons: List[str] = []
        events: List[AuctionEvent] = []
        transition = DirectionalTransition.NONE
        transition_reason = ""

        active_id = payload["active_episode_id"]
        current_side = payload["direction"]
        fresh_side = self._fresh_bias(evidence.side)

        if active_id is None:
            transition, transition_reason = self._advance_without_episode(
                payload=payload,
                symbol=symbol,
                fresh_side=fresh_side,
                snapshot_time=snapshot_time,
                close=close,
                high=high,
                low=low,
                balance_state=balance_state,
                reasons=reasons,
            )
            if transition is DirectionalTransition.STARTED:
                event = self._event(
                    symbol=symbol,
                    trading_day=trading_day,
                    snapshot_time=snapshot_time,
                    episode_id=payload["active_episode_id"],
                    event_type=AuctionEventType.DIRECTIONAL_STARTED,
                    direction=payload["direction"],
                    reason_codes=("FRESH_DIRECTION_CONFIRMED",),
                    data={
                        "start_price": payload["start_price"],
                        "previous_episode_id": payload["previous_episode_id"],
                    },
                )
                if event.event_id not in emitted_ids:
                    emitted_ids.add(event.event_id)
                    events.append(event)
        else:
            payload["age_bars"] += 1
            if fresh_side is current_side:
                payload["support_streak"] += 1
                self._clear_opposition(payload)
                payload["unresolved_streak"] = 0
                payload["last_confirmed_at"] = snapshot_time
                payload["extreme_price"] = self._new_extreme(
                    current_side,
                    payload["extreme_price"],
                    high,
                    low,
                )
                transition = DirectionalTransition.CONTINUED
                transition_reason = "FRESH_DIRECTION_SUPPORTS_ACTIVE_EPISODE"
                reasons.append("ACTIVE_EPISODE_SUPPORTED")
            elif fresh_side in (DirectionalBias.UP, DirectionalBias.DOWN):
                transition, transition_reason = self._advance_opposition(
                    payload=payload,
                    symbol=symbol,
                    fresh_side=fresh_side,
                    snapshot_time=snapshot_time,
                    close=close,
                    high=high,
                    low=low,
                    reasons=reasons,
                )
                if transition is DirectionalTransition.REVERSED:
                    event = self._event(
                        symbol=symbol,
                        trading_day=trading_day,
                        snapshot_time=snapshot_time,
                        episode_id=payload["active_episode_id"],
                        event_type=AuctionEventType.DIRECTIONAL_REVERSED,
                        direction=payload["direction"],
                        reason_codes=("CONFIRMED_OPPOSITE_FRESH_DIRECTION",),
                        data={
                            "previous_episode_id": payload["previous_episode_id"],
                            "start_price": payload["start_price"],
                        },
                    )
                    if event.event_id not in emitted_ids:
                        emitted_ids.add(event.event_id)
                        events.append(event)
            else:
                payload["unresolved_streak"] += 1
                payload["support_streak"] = 0
                self._clear_opposition(payload)
                transition = DirectionalTransition.DEFERRED
                transition_reason = "FRESH_DIRECTION_UNRESOLVED"
                reasons.append("ACTIVE_EPISODE_RETAINED_WITHOUT_NEW_EVENT")

        payload["emitted_event_ids"] = tuple(sorted(emitted_ids))
        payload["last_reason_codes"] = tuple(reasons)
        memory = DirectionalMemory.model_validate(payload)
        projection = self._projection(
            memory,
            transition=transition,
            transition_reason=transition_reason,
        )
        return memory, projection, tuple(events)

    def _advance_without_episode(
        self,
        *,
        payload: dict,
        symbol: str,
        fresh_side: DirectionalBias,
        snapshot_time: datetime,
        close: float,
        high: float,
        low: float,
        balance_state: BalanceEpisodeState,
        reasons: List[str],
    ) -> tuple[DirectionalTransition, str]:
        if balance_state in self.cfg.start_blocking_balance_states:
            self._clear_start_candidate(payload)
            reasons.append("DIRECTIONAL_START_BLOCKED_BY_BALANCE")
            return DirectionalTransition.DEFERRED, "BALANCE_BLOCKS_DIRECTIONAL_START"
        if fresh_side not in (DirectionalBias.UP, DirectionalBias.DOWN):
            self._clear_start_candidate(payload)
            reasons.append("NO_CONFIRMED_FRESH_DIRECTION")
            return DirectionalTransition.DEFERRED, "FRESH_DIRECTION_UNRESOLVED"

        if payload["start_candidate_side"] is fresh_side:
            payload["start_candidate_streak"] += 1
            payload["start_candidate_extreme_price"] = self._new_extreme(
                fresh_side,
                payload["start_candidate_extreme_price"],
                high,
                low,
            )
        else:
            payload["start_candidate_side"] = fresh_side
            payload["start_candidate_started_at"] = snapshot_time
            payload["start_candidate_price"] = close
            payload["start_candidate_extreme_price"] = (
                high if fresh_side is DirectionalBias.UP else low
            )
            payload["start_candidate_streak"] = 1
        reasons.append("DIRECTIONAL_START_CANDIDATE_PROGRESS")
        if payload["start_candidate_streak"] < self.cfg.start_confirmation_bars:
            return DirectionalTransition.DEFERRED, "DIRECTIONAL_START_AWAITING_CONFIRMATION"

        payload["sequence"] += 1
        payload["active_episode_id"] = self._episode_id(
            symbol, payload["sequence"], fresh_side, snapshot_time
        )
        payload["previous_episode_id"] = None
        payload["direction"] = fresh_side
        payload["started_at"] = payload["start_candidate_started_at"]
        payload["confirmed_at"] = snapshot_time
        payload["last_confirmed_at"] = snapshot_time
        payload["start_price"] = payload["start_candidate_price"] or close
        payload["extreme_price"] = payload["start_candidate_extreme_price"]
        payload["age_bars"] = payload["start_candidate_streak"]
        payload["support_streak"] = payload["start_candidate_streak"]
        payload["unresolved_streak"] = 0
        self._clear_opposition(payload)
        self._clear_start_candidate(payload)
        reasons.append("DIRECTIONAL_EPISODE_STARTED")
        return DirectionalTransition.STARTED, "CONFIRMED_FRESH_DIRECTION"

    def _advance_opposition(
        self,
        *,
        payload: dict,
        symbol: str,
        fresh_side: DirectionalBias,
        snapshot_time: datetime,
        close: float,
        high: float,
        low: float,
        reasons: List[str],
    ) -> tuple[DirectionalTransition, str]:
        if payload["opposition_side"] is fresh_side:
            payload["opposition_streak"] += 1
            payload["opposition_extreme_price"] = self._new_extreme(
                fresh_side,
                payload["opposition_extreme_price"],
                high,
                low,
            )
        else:
            payload["opposition_side"] = fresh_side
            payload["opposition_started_at"] = snapshot_time
            payload["opposition_start_price"] = close
            payload["opposition_extreme_price"] = (
                high if fresh_side is DirectionalBias.UP else low
            )
            payload["opposition_streak"] = 1
        payload["support_streak"] = 0
        payload["unresolved_streak"] = 0
        reasons.append("OPPOSITE_FRESH_DIRECTION_PROGRESS")
        if payload["opposition_streak"] < self.cfg.opposite_completion_bars:
            return DirectionalTransition.DEFERRED, "OPPOSITE_DIRECTION_AWAITING_CONFIRMATION"

        previous_episode_id = payload["active_episode_id"]
        started_at = payload["opposition_started_at"] or snapshot_time
        start_price = payload["opposition_start_price"] or close
        support_streak = payload["opposition_streak"]
        payload["sequence"] += 1
        payload["active_episode_id"] = self._episode_id(
            symbol, payload["sequence"], fresh_side, snapshot_time
        )
        payload["previous_episode_id"] = previous_episode_id
        payload["direction"] = fresh_side
        payload["started_at"] = started_at
        payload["confirmed_at"] = snapshot_time
        payload["last_confirmed_at"] = snapshot_time
        payload["start_price"] = start_price
        payload["extreme_price"] = payload["opposition_extreme_price"]
        payload["age_bars"] = support_streak
        payload["support_streak"] = support_streak
        self._clear_opposition(payload)
        self._clear_start_candidate(payload)
        reasons.append("DIRECTIONAL_EPISODE_REVERSED")
        return DirectionalTransition.REVERSED, "CONFIRMED_OPPOSITE_FRESH_DIRECTION"

    @staticmethod
    def _projection(
        memory: DirectionalMemory,
        *,
        transition: DirectionalTransition,
        transition_reason: str,
    ) -> DirectionalProjection:
        return DirectionalProjection(
            active_episode_id=memory.active_episode_id,
            previous_episode_id=memory.previous_episode_id,
            direction=memory.direction,
            started_at=memory.started_at,
            confirmed_at=memory.confirmed_at,
            last_confirmed_at=memory.last_confirmed_at,
            start_price=memory.start_price,
            extreme_price=memory.extreme_price,
            age_bars=memory.age_bars,
            support_streak=memory.support_streak,
            opposition_streak=memory.opposition_streak,
            unresolved_streak=memory.unresolved_streak,
            transition=transition,
            transition_reason=transition_reason,
            reason_codes=memory.last_reason_codes,
        )

    @staticmethod
    def _fresh_bias(side: FreshDirection) -> DirectionalBias:
        if side is FreshDirection.UP:
            return DirectionalBias.UP
        if side is FreshDirection.DOWN:
            return DirectionalBias.DOWN
        return DirectionalBias.UNKNOWN

    @staticmethod
    def _new_extreme(
        side: DirectionalBias,
        previous: float | None,
        high: float,
        low: float,
    ) -> float:
        if side is DirectionalBias.UP:
            return max(previous or high, high)
        return min(previous or low, low)

    @staticmethod
    def _clear_start_candidate(payload: dict) -> None:
        payload["start_candidate_side"] = DirectionalBias.UNKNOWN
        payload["start_candidate_started_at"] = None
        payload["start_candidate_price"] = None
        payload["start_candidate_extreme_price"] = None
        payload["start_candidate_streak"] = 0

    @staticmethod
    def _clear_opposition(payload: dict) -> None:
        payload["opposition_side"] = DirectionalBias.UNKNOWN
        payload["opposition_started_at"] = None
        payload["opposition_start_price"] = None
        payload["opposition_extreme_price"] = None
        payload["opposition_streak"] = 0

    @staticmethod
    def _episode_id(
        symbol: str, sequence: int, side: DirectionalBias, confirmed_at: datetime
    ) -> str:
        return f"DIR-{symbol}-{confirmed_at:%Y%m%d}-{sequence:04d}-{side.value}"

    @staticmethod
    def _event(
        *,
        symbol: str,
        trading_day: date,
        snapshot_time: datetime,
        episode_id: str,
        event_type: AuctionEventType,
        direction: DirectionalBias,
        reason_codes: Tuple[str, ...],
        data: dict,
    ) -> AuctionEvent:
        event_id = (
            f"EVT-{symbol}-{snapshot_time:%Y%m%d%H%M%S}-"
            f"{event_type.value}-{episode_id}"
        )
        return AuctionEvent(
            event_id=event_id,
            event_type=event_type,
            episode_id=episode_id,
            symbol=symbol,
            trading_day=trading_day,
            event_time=snapshot_time,
            direction=direction,
            reason_codes=reason_codes,
            data=data,
        )


__all__ = ["DirectionalStateMachine"]
