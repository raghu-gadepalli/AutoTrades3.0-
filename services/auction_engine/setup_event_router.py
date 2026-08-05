"""Common authoritative Auction-event routing for every setup family.

This module does not create setup candidates.  It converts the ordered events
and structural permissions already published by Auction into a strict setup-
consumption contract for later setup evaluators.  No evaluator is allowed to
infer a structural event that is absent from this router.

``CLOSE`` ends the creation/update window of a structural setup opportunity.
It is not an instruction to force-exit an already deployed signal or trade.
Only explicit ``INVALIDATE`` routes are structurally terminal downstream.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Tuple

from enums.auction_engine import (
    AuctionEventType,
    SetupEventAction,
    SetupFamily,
)
from services.auction_engine.episode_contracts import (
    AuctionEvent,
    AuthoritativeSetupEventRoute,
    StructuralSetupPermission,
)


@dataclass(frozen=True)
class _RoutePolicy:
    setup_family: SetupFamily
    action: SetupEventAction


_EVENT_ROUTE_POLICY: Dict[AuctionEventType, Tuple[_RoutePolicy, ...]] = {
    AuctionEventType.DIRECTIONAL_REVERSED: (
        _RoutePolicy(SetupFamily.REVERSAL, SetupEventAction.EVALUATE),
        _RoutePolicy(SetupFamily.REVERSAL, SetupEventAction.INVALIDATE),
        _RoutePolicy(SetupFamily.CONTINUATION, SetupEventAction.INVALIDATE),
        _RoutePolicy(SetupFamily.REACCELERATION, SetupEventAction.INVALIDATE),
        _RoutePolicy(SetupFamily.BREAKOUT_INITIATION, SetupEventAction.INVALIDATE),
        _RoutePolicy(SetupFamily.ACCEPTED_BREAKOUT, SetupEventAction.INVALIDATE),
        _RoutePolicy(SetupFamily.FAILED_BREAKOUT, SetupEventAction.INVALIDATE),
    ),
    AuctionEventType.DIRECTIONAL_ENDED: (
        _RoutePolicy(SetupFamily.REVERSAL, SetupEventAction.CLOSE),
        _RoutePolicy(SetupFamily.CONTINUATION, SetupEventAction.CLOSE),
        _RoutePolicy(SetupFamily.REACCELERATION, SetupEventAction.CLOSE),
    ),
    AuctionEventType.BALANCE_ESCAPE_STARTED: (
        _RoutePolicy(SetupFamily.BREAKOUT_INITIATION, SetupEventAction.EVALUATE),
    ),
    AuctionEventType.BALANCE_ESCAPE_ACCEPTED: (
        _RoutePolicy(SetupFamily.BREAKOUT_INITIATION, SetupEventAction.CLOSE),
        _RoutePolicy(SetupFamily.ACCEPTED_BREAKOUT, SetupEventAction.EVALUATE),
    ),
    AuctionEventType.BALANCE_ESCAPE_FAILED: (
        _RoutePolicy(SetupFamily.BREAKOUT_INITIATION, SetupEventAction.INVALIDATE),
        _RoutePolicy(SetupFamily.ACCEPTED_BREAKOUT, SetupEventAction.INVALIDATE),
        _RoutePolicy(SetupFamily.FAILED_BREAKOUT, SetupEventAction.EVALUATE),
    ),
    AuctionEventType.BALANCE_COMPLETED: (
        _RoutePolicy(SetupFamily.BREAKOUT_INITIATION, SetupEventAction.CLOSE),
        _RoutePolicy(SetupFamily.ACCEPTED_BREAKOUT, SetupEventAction.CLOSE),
        _RoutePolicy(SetupFamily.FAILED_BREAKOUT, SetupEventAction.CLOSE),
    ),
}


class AuthoritativeSetupEventRouter:
    """Route authoritative Auction events without setup discovery or compatibility logic."""

    def route_authority(
        self,
        *,
        events: Tuple[AuctionEvent, ...],
        permissions: Tuple[StructuralSetupPermission, ...],
    ) -> Tuple[AuthoritativeSetupEventRoute, ...]:
        """Route an explicit event/permission projection without structural inference."""

        permission_by_family = {
            permission.setup_family: permission
            for permission in permissions
        }
        routes: List[AuthoritativeSetupEventRoute] = []
        for event in events:
            routes.extend(
                self._route_event(
                    event,
                    permission_by_family=permission_by_family,
                )
            )
        return tuple(routes)

    def _route_event(
        self,
        event: AuctionEvent,
        *,
        permission_by_family: Dict[SetupFamily, StructuralSetupPermission],
    ) -> Iterable[AuthoritativeSetupEventRoute]:
        for policy in _EVENT_ROUTE_POLICY.get(event.event_type, ()):
            permission = None
            if policy.action is SetupEventAction.EVALUATE:
                permission = permission_by_family.get(policy.setup_family)
                if permission is None:
                    raise ValueError(
                        f"Creation-capable event {event.event_type.value} has no "
                        f"structural permission for {policy.setup_family.value}"
                    )
                if event.event_id not in permission.source_event_ids:
                    raise ValueError(
                        f"Structural permission for {policy.setup_family.value} "
                        f"does not reference source event {event.event_id}"
                    )
            elif policy.action is SetupEventAction.WATCH:
                candidate = permission_by_family.get(policy.setup_family)
                if candidate is not None and event.event_id in candidate.source_event_ids:
                    permission = candidate

            yield AuthoritativeSetupEventRoute(
                source_event_id=event.event_id,
                source_event_type=event.event_type,
                source_episode_id=event.episode_id,
                setup_family=policy.setup_family,
                action=policy.action,
                direction=event.direction,
                structural_result=(permission.result if permission is not None else None),
                reason_codes=event.reason_codes,
            )

    @staticmethod
    def creation_event_types() -> Dict[SetupFamily, Tuple[AuctionEventType, ...]]:
        by_family: Dict[SetupFamily, List[AuctionEventType]] = {
            family: [] for family in SetupFamily
        }
        for event_type, policies in _EVENT_ROUTE_POLICY.items():
            for policy in policies:
                if policy.action is SetupEventAction.EVALUATE:
                    by_family[policy.setup_family].append(event_type)
        return {
            family: tuple(event_types)
            for family, event_types in by_family.items()
        }


__all__ = ["AuthoritativeSetupEventRouter"]
