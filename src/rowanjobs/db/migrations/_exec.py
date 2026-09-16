"""Helper for running a multi-statement migration script.

APSW executes every statement in a script, but lazily: the second and later
statements only run as the cursor is consumed. Draining the cursor here means a
migration cannot silently apply half of itself.
"""

from __future__ import annotations

from ..connection import Database


def exec_script(db: Database, sql: str) -> None:
    for _ in db.conn.execute(sql):
        pass
