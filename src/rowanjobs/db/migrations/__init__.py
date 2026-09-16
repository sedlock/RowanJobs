"""Explicit, ordered schema migrations.

Migrations only ever run from a read-write handle obtained by the collector or
by ``rowanjobs migrate``. Reporting paths open the database read-only, so a
report can never silently change the schema of a production archive.
"""

from __future__ import annotations

from collections.abc import Callable

from ...timeutil import utc_str
from ..connection import Database
from . import (
    m0001_initial,
    m0002_views,
    m0003_comparison_lineage,
    m0004_availability_guard,
)

Migration = tuple[int, str, Callable[[Database], None]]

MIGRATIONS: list[Migration] = [
    (1, "initial", m0001_initial.upgrade),
    (2, "views", m0002_views.upgrade),
    (3, "comparison_lineage", m0003_comparison_lineage.upgrade),
    (4, "availability_guard", m0004_availability_guard.upgrade),
]

SCHEMA_VERSION = max(v for v, _, _ in MIGRATIONS)


def _bootstrap(db: Database) -> None:
    db.conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version         INTEGER PRIMARY KEY,
            name            TEXT NOT NULL,
            applied_at_utc  TEXT NOT NULL
        )
        """
    )


def applied_versions(db: Database) -> set[int]:
    if not db.conn.table_exists("main", "schema_migrations"):
        return set()
    return {int(r[0]) for r in db.conn.execute("SELECT version FROM schema_migrations")}


def current_version(db: Database) -> int:
    applied = applied_versions(db)
    return max(applied) if applied else 0


def pending(db: Database) -> list[Migration]:
    applied = applied_versions(db)
    return [m for m in MIGRATIONS if m[0] not in applied]


def apply_migrations(db: Database) -> list[int]:
    """Apply outstanding migrations. Each runs in its own transaction."""
    _bootstrap(db)
    done: list[int] = []
    for version, name, fn in pending(db):
        with db.write():
            fn(db)
            db.conn.execute(
                "INSERT INTO schema_migrations(version, name, applied_at_utc) VALUES (?,?,?)",
                (version, name, utc_str()),
            )
        done.append(version)
    return done
