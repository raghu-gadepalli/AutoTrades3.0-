"""Reusable, verified current-table to history-table archival.

Archive contracts derive the insert projection from SQLAlchemy model metadata.
Only exceptional column names and the source/history identity join are declared
by each domain schema. This keeps archive writes aligned when matching model
columns evolve.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Type

from sqlalchemy import Table, func, insert, select
from sqlalchemy.orm import DeclarativeBase

from database.database import get_trades_db


VerificationCondition = Callable[[Table, Table], object]


@dataclass(frozen=True)
class ArchiveSpec:
    """Source/history contract for one idempotent archive operation."""

    name: str
    source_model: Type[DeclarativeBase]
    history_model: Type[DeclarativeBase]
    target_to_source: Mapping[str, str] = field(default_factory=dict)
    excluded_target_columns: frozenset[str] = frozenset()
    verification_condition: VerificationCondition | None = None


@dataclass(frozen=True)
class ArchiveResult:
    name: str
    source_rows: int
    inserted_rows: int
    verified_rows: int

    @property
    def duplicate_rows(self) -> int:
        return max(0, self.source_rows - self.inserted_rows)

    def as_dict(self) -> dict[str, int | str]:
        return {
            "name": self.name,
            "source_rows": self.source_rows,
            "inserted_rows": self.inserted_rows,
            "duplicate_rows": self.duplicate_rows,
            "verified_rows": self.verified_rows,
        }


def archive_columns(spec: ArchiveSpec) -> tuple[list, list]:
    """Return target/source SQLAlchemy column projections for an archive."""

    source = spec.source_model.__table__
    target = spec.history_model.__table__
    target_columns = []
    source_columns = []

    for target_column in target.columns:
        name = target_column.name
        if name in spec.excluded_target_columns:
            continue
        if target_column.computed is not None:
            continue
        if target_column.primary_key and target_column.autoincrement:
            continue

        source_name = spec.target_to_source.get(name, name)
        if source_name in source.c:
            target_columns.append(target_column)
            source_columns.append(source.c[source_name])
            continue

        has_default = (
            target_column.default is not None
            or target_column.server_default is not None
        )
        if target_column.nullable or has_default:
            continue

        raise RuntimeError(
            f"Archive contract {spec.name}: target column {name!r} has no "
            "source mapping or database default"
        )

    if not target_columns:
        raise RuntimeError(f"Archive contract {spec.name}: no insertable columns")
    return target_columns, source_columns


def archive_rows(spec: ArchiveSpec) -> ArchiveResult:
    """Archive every source row and verify source/history identity coverage.

    MySQL ``INSERT IGNORE`` makes a rerun idempotent when the history table has
    the corresponding unique identity. Verification occurs before commit in
    the same database session.
    """

    if spec.verification_condition is None:
        raise RuntimeError(
            f"Archive contract {spec.name}: verification condition required"
        )

    source = spec.source_model.__table__
    target = spec.history_model.__table__
    target_columns, source_columns = archive_columns(spec)

    with get_trades_db() as db:
        source_rows = int(
            db.execute(select(func.count()).select_from(source)).scalar_one()
        )
        if source_rows == 0:
            return ArchiveResult(spec.name, 0, 0, 0)

        statement = (
            insert(target)
            .prefix_with("IGNORE")
            .from_select(
                target_columns,
                select(*source_columns).select_from(source),
            )
        )
        result = db.execute(statement)
        inserted_rows = int(result.rowcount or 0)

        condition = spec.verification_condition(source, target)
        verified_rows = int(
            db.execute(
                select(func.count()).select_from(source.join(target, condition))
            ).scalar_one()
        )
        if verified_rows != source_rows:
            db.rollback()
            raise RuntimeError(
                f"Archive verification failed for {spec.name}: "
                f"source={source_rows} verified={verified_rows}"
            )
        db.commit()

    return ArchiveResult(
        name=spec.name,
        source_rows=source_rows,
        inserted_rows=inserted_rows,
        verified_rows=verified_rows,
    )


__all__ = [
    "ArchiveResult",
    "ArchiveSpec",
    "archive_columns",
    "archive_rows",
]
