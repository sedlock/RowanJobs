"""Migration 6: guard ``availability_state`` on UPDATE as well as INSERT.

Migration 4 constrained inserts. Nothing updates the column today, but a guard
that only covers one direction is a guarantee with a hole in it, and this is the
column that decides whether an advertisement counts as present, closed or merely
unobserved.
"""

from __future__ import annotations

from ..connection import Database
from ._exec import exec_script

SQL = """
CREATE TRIGGER trg_observation_availability_update
BEFORE UPDATE OF availability_state ON posting_observations
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
