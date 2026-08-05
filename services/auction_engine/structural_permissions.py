"""Central structural setup-permission matrix for authoritative Auction events."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG, AuctionEngineConfig
from enums.auction_engine import (
    AuctionEventType,
    BalanceEpisodeState,
    SetupFamily,
    StructuralPermissionResult,
)
from services.auction_engine.episode_contracts import (
    AuctionEvent,
    StructuralSetupPermission,
)


@dataclass
class _PermissionAccumulator:
    result: StructuralPermissionResult
    source_event_ids: List[str] = field(default_factory=list)
    source_event_types: List[AuctionEventType] = field(default_factory=list)
    reason_codes: List[str] = field(default_factory=list)


class StructuralPermissionMatrix:
    """Resolve event/state rules into one result per setup family."""

    def __init__(self, config: AuctionEngineConfig = AUCTION_ENGINE_CONFIG) -> None:
        self.config = config
        self.cfg = config.episode.permissions
        self._precedence: Dict[StructuralPermissionResult, int] = {
            result: index for index, result in enumerate(self.cfg.result_precedence)
        }

    def evaluate(
        self,
        *,
        balance_state: BalanceEpisodeState,
        events: Tuple[AuctionEvent, ...],
    ) -> Tuple[StructuralSetupPermission, ...]:
        accumulators: Dict[SetupFamily, _PermissionAccumulator] = {}

        for event in events:
            for rule in self.cfg.event_rules:
                if rule.event_type is not event.event_type:
                    continue
                for family in rule.setup_families:
                    self._apply(
                        accumulators,
                        family=family,
                        result=rule.result,
                        event=event,
                        reason_code=f"EVENT_{event.event_type.value}_{rule.result.value}",
                    )

        for rule in self.cfg.state_rules:
            if rule.balance_state is not balance_state:
                continue
            for family in rule.setup_families:
                self._apply(
                    accumulators,
                    family=family,
                    result=rule.result,
                    event=None,
                    reason_code=(
                        f"BALANCE_{balance_state.value}_{family.value}_{rule.result.value}"
                    ),
                )

        permissions = []
        for family in sorted(accumulators, key=lambda item: item.value):
            accumulator = accumulators[family]
            permissions.append(
                StructuralSetupPermission(
                    setup_family=family,
                    result=accumulator.result,
                    source_event_ids=tuple(accumulator.source_event_ids),
                    source_event_types=tuple(accumulator.source_event_types),
                    balance_state=balance_state,
                    reason_codes=tuple(accumulator.reason_codes),
                )
            )
        return tuple(permissions)

    @staticmethod
    def permission_signature(
        permissions: Tuple[StructuralSetupPermission, ...],
    ) -> Tuple[tuple, ...]:
        """Return the complete deterministic permission projection signature."""
        return tuple(
            sorted(
                (
                    item.setup_family.value,
                    item.result.value,
                    tuple(item.source_event_ids),
                    tuple(event.value for event in item.source_event_types),
                    item.balance_state.value,
                    tuple(item.reason_codes),
                )
                for item in permissions
            )
        )

    def validate_persisted(
        self,
        *,
        balance_state: BalanceEpisodeState,
        events: Tuple[AuctionEvent, ...],
        permissions: Tuple[StructuralSetupPermission, ...],
    ) -> None:
        """Fail when stored permissions do not match authoritative snapshot facts."""
        expected = self.evaluate(balance_state=balance_state, events=events)
        expected_signature = self.permission_signature(expected)
        stored_signature = self.permission_signature(permissions)
        if stored_signature != expected_signature:
            raise ValueError(
                "Persisted Auction permissions do not match authoritative events "
                f"and balance state: stored={stored_signature!r} "
                f"expected={expected_signature!r}"
            )

    def _apply(
        self,
        accumulators: Dict[SetupFamily, _PermissionAccumulator],
        *,
        family: SetupFamily,
        result: StructuralPermissionResult,
        event: AuctionEvent | None,
        reason_code: str,
    ) -> None:
        if family not in accumulators:
            accumulators[family] = _PermissionAccumulator(result=result)
        accumulator = accumulators[family]
        if self._precedence[result] > self._precedence[accumulator.result]:
            accumulator.result = result
        if event is not None:
            if event.event_id not in accumulator.source_event_ids:
                accumulator.source_event_ids.append(event.event_id)
            if event.event_type not in accumulator.source_event_types:
                accumulator.source_event_types.append(event.event_type)
        if reason_code not in accumulator.reason_codes:
            accumulator.reason_codes.append(reason_code)


__all__ = ["StructuralPermissionMatrix"]
