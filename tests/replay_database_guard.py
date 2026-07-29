"""Strict database preflight for replay programs that can write or delete rows."""
from __future__ import annotations

from sqlalchemy.engine import make_url

PROTECTED_REPLAY_DATABASES = frozenset({"autotrades"})


def require_allowed_replay_database(*, database_uri: str, allowed_database: str) -> str:
    """Return the configured database name only when the replay scope is safe.

    The configured database must exactly match ``allowed_database``. Protected
    databases are rejected regardless of the command-line value, so explicitly
    passing a production database name cannot bypass the guard.
    """

    allowed = str(allowed_database).strip()
    if not allowed:
        raise ValueError("allowed_database must be a non-empty database name")

    database = str(make_url(database_uri).database or "").strip()
    if not database:
        raise RuntimeError("Configured replay database URI has no database name")

    protected = {name.casefold() for name in PROTECTED_REPLAY_DATABASES}
    if allowed.casefold() in protected:
        raise RuntimeError(
            f"Replay database {allowed!r} is protected and cannot be allowed"
        )
    if database.casefold() in protected:
        raise RuntimeError(
            f"Configured replay database {database!r} is protected; refusing replay"
        )
    if database.casefold() != allowed.casefold():
        raise RuntimeError(
            "Configured replay database does not match the explicitly allowed "
            f"database: configured={database!r} allowed={allowed!r}"
        )
    return database
