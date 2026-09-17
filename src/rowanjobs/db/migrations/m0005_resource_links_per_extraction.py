"""Migration 5: classify linked resources per extraction, not per version.

``resource_links`` rows were written only when a *version* was first created,
and were unique on ``(posting_version_id, url_raw, position)``. A link's
classification is a property of the extraction that read it, though, not of the
content: widening the collection scope meant the collector started retrieving a
document while the stored row still said ``exclude``. Anyone later asking "which
documents were in scope?" would have got the wrong answer from the table while
``resource_observations`` showed the retrieval.

Rows are now unique on ``(extraction_id, url_raw, position)`` and are written on
every content capture. Extractions are themselves deduplicated by
``(artifact, parser, contracts)``, so an unchanged page re-observed tomorrow
reuses the same extraction and adds no rows.

Existing rows keep their ids and their original decisions; nothing recorded is
rewritten.
"""

from __future__ import annotations

from ..connection import Database
from ._exec import exec_script

# resource_associations holds a foreign key into resource_links, so the table
# cannot be rebuilt with enforcement on. apply_migrations turns it off around
# this migration and runs foreign_key_check afterwards.
REQUIRES_FK_OFF = True

SQL = """
CREATE TABLE resource_links_new (
    resource_link_id    INTEGER PRIMARY KEY,
    posting_version_id  INTEGER REFERENCES posting_versions(posting_version_id),
    extraction_id       INTEGER REFERENCES extractions(extraction_id),
    parent_kind         TEXT NOT NULL,
    url_raw             TEXT NOT NULL,
    url_resolved        TEXT,
    link_text           TEXT,
    rel                 TEXT,
    position            INTEGER,
    classification      TEXT NOT NULL,
    collection_decision TEXT NOT NULL CHECK (collection_decision IN ('fetch','exclude')),
    exclusion_reason    TEXT,
    first_seen_at_utc   TEXT NOT NULL,
    UNIQUE(extraction_id, url_raw, position)
);

INSERT INTO resource_links_new(
    resource_link_id, posting_version_id, extraction_id, parent_kind, url_raw,
    url_resolved, link_text, rel, position, classification, collection_decision,
    exclusion_reason, first_seen_at_utc)
SELECT resource_link_id, posting_version_id, extraction_id, parent_kind, url_raw,
       url_resolved, link_text, rel, position, classification, collection_decision,
       exclusion_reason, first_seen_at_utc
  FROM resource_links;

DROP TABLE resource_links;

ALTER TABLE resource_links_new RENAME TO resource_links;

CREATE INDEX idx_resource_links_class
    ON resource_links(classification, collection_decision);
CREATE INDEX idx_resource_links_version ON resource_links(posting_version_id);
"""


def upgrade(db: Database) -> None:
    exec_script(db, SQL)
