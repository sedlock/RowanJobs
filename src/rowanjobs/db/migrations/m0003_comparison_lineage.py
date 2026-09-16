"""Migration 3: make the comparison lineage explicit.

``content_fingerprint`` folded in the comparison contract but not the parser
version or the text contract, while ``posting_versions`` recorded no parser
version at all. A ``TEXT_CONTRACT_VERSION`` or ``PARSER_VERSION`` bump therefore
produced a new fingerprint inside the *same* comparison scope, and the next
collection recorded a ``content_changed`` event for a page the employer had not
touched -- exactly what the project forbids.

Two versions are now only ever compared when their **whole lineage** matches:
parser version, comparison contract and text contract. Existing rows are
backfilled with the versions that actually produced them, which is accurate:
nothing in this archive predates 1.0.0.
"""

from __future__ import annotations

from ... import PARSER_VERSION
from ..connection import Database
from ._exec import exec_script

SQL = """
ALTER TABLE posting_versions ADD COLUMN parser_version TEXT;

CREATE INDEX idx_versions_lineage
    ON posting_versions(posting_id, parser_version, contract_version,
                        text_contract_version, first_seen_at_utc);
"""


def upgrade(db: Database) -> None:
    exec_script(db, SQL)
    db.conn.execute(
        "UPDATE posting_versions SET parser_version = ? WHERE parser_version IS NULL",
        (PARSER_VERSION,),
    )
