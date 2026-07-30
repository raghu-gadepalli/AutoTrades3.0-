"""Causal read-only history provider for StockAdvisor.

Database access stays behind schema methods so policy logic remains pure and
unit tests can inject deterministic history without touching persistence.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import List, Protocol

from schemas.snapshot import SnapshotSchema
from schemas.stock_opportunity import StockOpportunitySchema


class StockAdvisorHistoryProviderProtocol(Protocol):
    def fetch_prior_opportunities(
        self,
        *,
        symbol: str,
        trading_day: date,
        before_time: datetime,
        limit: int,
    ) -> List[StockOpportunitySchema]: ...

    def fetch_day_snapshots(
        self,
        *,
        symbol: str,
        trading_day: date,
        through_time: datetime,
        limit: int,
        include_current: bool,
    ) -> List[SnapshotSchema]: ...


class StockAdvisorHistoryProvider:
    """Default database-backed provider."""

    def fetch_prior_opportunities(
        self,
        *,
        symbol: str,
        trading_day: date,
        before_time: datetime,
        limit: int,
    ) -> List[StockOpportunitySchema]:
        return StockOpportunitySchema.fetch_prior_deployed_for_advisor(
            symbol=symbol,
            trading_day=trading_day,
            before_time=before_time,
            limit=limit,
        )

    def fetch_day_snapshots(
        self,
        *,
        symbol: str,
        trading_day: date,
        through_time: datetime,
        limit: int,
        include_current: bool,
    ) -> List[SnapshotSchema]:
        return SnapshotSchema.fetch_day_context_for_advisor(
            symbol=symbol,
            trading_day=trading_day,
            through_time=through_time,
            limit=limit,
            include_current=include_current,
        )


__all__ = [
    "StockAdvisorHistoryProviderProtocol",
    "StockAdvisorHistoryProvider",
]
