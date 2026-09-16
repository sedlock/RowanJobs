"""Migration 4: constrain ``availability_state`` in the database itself.

``identity_state`` had a CHECK constraint from the start; ``availability_state``
-- the column that decides whether an advertisement is treated as present,
closed or merely unobserved -- did not. A typo or a future adapter inventing a
state would have been stored silently and quietly excluded from every query that
enumerates the known states.

SQLite cannot add a CHECK to an existing table, so the guard is a trigger. It
mirrors ``rowanjobs.constants.AVAILABILITY_STATES``; a test asserts the two stay
in step.
"""

from __future__ import annotations

from ..connection import Database
from ._exec import exec_script

SQL = """
CREATE TRIGGER trg_observation_availability_insert
BEFORE INSERT ON posting_observations
FOR EACH ROW WHEN NEW.availability_state NOT IN (
    'content_captured','explicit_closure','not_found','redirected_to_listing',
    'redirected_to_other_job','identity_mismatch','access_control_challenge',
    'retrieval_failed')
BEGIN
    SELECT RAISE(ABORT, 'unknown availability_state');
END;
"""


def upgrade(db: Database) -> None:
    exec_script(db, SQL)
