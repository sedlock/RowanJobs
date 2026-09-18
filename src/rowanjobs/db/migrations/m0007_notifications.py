"""Migration 7: durable delivery records for run reports.

A report that was composed but never accepted by the provider has to be
distinguishable from one that was never attempted, and from one that was
accepted twice. Delivery state is therefore persisted per run, not inferred
from logs:

* ``UNIQUE(run_id, kind)`` makes a routine report per run idempotent, so a
  retry can never post a second copy of the same report.
* ``accepted_at_utc`` and ``message_id`` record provider acceptance -- which is
  not the same claim as the mail arriving in an inbox, and is labelled as such
  wherever it is reported.
* ``delayed`` marks a report sent later than the run it describes, so a
  catch-up is never presented as having gone out at collection time.
* ``skipped`` records a run that was deliberately not reported -- in practice a
  run that finished before reporting was ever configured. Saying so explicitly
  is the difference between "this predates the feature" and "a report went
  missing", and only a ``skipped`` row is allowed to have no composed body.

Mail failures live here and nowhere near the collection tables: a bounced
report must never make a successful harvest look unsuccessful.
"""

from __future__ import annotations

from ..connection import Database
from ._exec import exec_script

SQL = """
CREATE TABLE notifications (
    notification_id      INTEGER PRIMARY KEY,
    run_id               INTEGER REFERENCES collection_runs(run_id),
    kind                 TEXT NOT NULL,
    recipient            TEXT NOT NULL,
    sender               TEXT,
    -- NULL only for 'skipped': nothing was composed, so there is no body to
    -- record a digest of. Anything claimed to have been sent must have both.
    subject              TEXT,
    body_sha256          TEXT,
    state                TEXT NOT NULL
        CHECK (state IN ('pending','accepted','failed','abandoned','skipped')),
    attempts             INTEGER NOT NULL DEFAULT 0,
    max_attempts         INTEGER NOT NULL DEFAULT 5,
    -- 1 when the report was sent later than the run it describes.
    delayed              INTEGER NOT NULL DEFAULT 0,
    created_at_utc       TEXT NOT NULL,
    updated_at_utc       TEXT NOT NULL,
    first_attempt_at_utc TEXT,
    last_attempt_at_utc  TEXT,
    accepted_at_utc      TEXT,
    message_id           TEXT,
    provider_response    TEXT,
    failure_kind         TEXT,
    last_error           TEXT,
    CHECK (state = 'skipped' OR (subject IS NOT NULL AND body_sha256 IS NOT NULL)),
    CHECK (state != 'accepted' OR (accepted_at_utc IS NOT NULL AND message_id IS NOT NULL)),
    UNIQUE(run_id, kind)
);

CREATE INDEX idx_notifications_state ON notifications(state, updated_at_utc);
CREATE INDEX idx_notifications_run ON notifications(run_id);
"""


def upgrade(db: Database) -> None:
    exec_script(db, SQL)
