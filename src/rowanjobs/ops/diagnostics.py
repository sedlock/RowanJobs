"""Sanitised diagnostic bundle.

Produced for later review. Deliberately excludes:

* archived payloads and full advertisement descriptions (the archive is the
  place for those; a diagnostic bundle is not);
* request and response headers (they may carry cookies -- already redacted in
  the archive, but there is no reason to re-export them);
* absolute paths outside the data root, and anything resembling a credential.

What it keeps is the evidence needed to judge coverage: run outcomes, scan
assessments with every check, coverage gaps, failure counts, and per-posting
freshness with fingerprints rather than content.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import __version__
from ..db import Database
from ..timeutil import duration_str, local_str, utc_str
from .atomic import write_json
from .health import build_health

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config

BUNDLE_VERSION = "1"


def collect_diagnostics(cfg: Config, db: Database, *, sample: int = 25) -> dict[str, Any]:
    health = build_health(cfg, db)
    runs = db.query("SELECT * FROM v_run_health ORDER BY started_at_utc DESC LIMIT 25")
    assessments = db.query(
        """
        SELECT a.assessment_id, a.scan_id, s.run_id, s.scan_role, s.pages_requested,
               s.entries_seen, s.unique_ids_seen, s.duplicate_occurrences,
               s.unresolved_candidates, s.source_reported_total, s.termination_reason,
               s.started_at_utc, s.ended_at_utc, a.qualified, a.reason,
               a.rules_version, a.checks_json
          FROM listing_scan_assessments a
          JOIN listing_scans s ON s.scan_id = a.scan_id
         ORDER BY a.assessment_id DESC LIMIT 20
        """
    )
    for row in assessments:
        try:
            row["checks"] = json.loads(str(row.pop("checks_json")))
        except ValueError:  # pragma: no cover
            row["checks"] = []

    gaps = db.query(
        "SELECT kind, scope, run_id, slot_local_date, detected_at_utc, detail, "
        "evidence_json, resolved_at_utc FROM coverage_gaps "
        "ORDER BY detected_at_utc DESC LIMIT 100"
    )
    failures = db.query(
        """
        SELECT purpose, http_status, access_control_signal, failure_kind,
               response_state, COUNT(*) AS n, MAX(started_at_utc) AS latest
          FROM fetches
         WHERE response_state != 'complete' OR access_control_signal IS NOT NULL
            OR http_status != 200
         GROUP BY purpose, http_status, access_control_signal, failure_kind, response_state
         ORDER BY n DESC
        """
    )
    unknown_labels = db.query(
        "SELECT DISTINCT source_label, field_key, COUNT(*) AS n FROM version_values "
        "WHERE known_label = 0 GROUP BY source_label, field_key ORDER BY n DESC LIMIT 50"
    )
    unresolved = db.query(
        "SELECT scan_id, page_number, section, position_in_section, href_raw, "
        "resolution_state, resolution_detail FROM listing_entries "
        "WHERE resolution_state != 'resolved' ORDER BY listing_entry_id DESC LIMIT 100"
    )
    conflicts = db.query(
        "SELECT observation_id, expected_external_job_id, observed_external_job_id, "
        "identity_state, availability_state, conflicts_json, observed_at_utc "
        "FROM posting_observations WHERE conflicts_json IS NOT NULL "
        "ORDER BY observation_id DESC LIMIT 50"
    )
    resources = db.query(
        "SELECT outcome, COUNT(*) AS n, MAX(observed_at_utc) AS latest, "
        "SUM(byte_length) AS bytes FROM resource_observations GROUP BY outcome"
    )
    freshness = db.query(
        "SELECT content_freshness, last_availability_state, COUNT(*) AS n "
        "FROM v_posting_current GROUP BY content_freshness, last_availability_state "
        "ORDER BY n DESC"
    )
    postings = db.query(
        "SELECT external_job_id, discovery_basis, first_discovered_at_utc, "
        "last_listed_at_utc, last_captured_at_utc, last_availability_state, "
        "content_freshness, version_count, observation_count, "
        "current_content_fingerprint FROM v_posting_current "
        "ORDER BY external_job_id LIMIT ?",
        (sample,),
    )
    versions = db.query(
        "SELECT description_html_kind, COUNT(*) AS n, "
        "MIN(LENGTH(description_text)) AS min_chars, "
        "MAX(LENGTH(description_text)) AS max_chars, "
        "CAST(AVG(LENGTH(description_text)) AS INTEGER) AS avg_chars "
        "FROM posting_versions GROUP BY description_html_kind"
    )
    extraction_warnings = db.query(
        "SELECT parser_name, status, decode_strategy, SUM(decode_error_count) AS decode_errors, "
        "COUNT(*) AS n FROM extractions GROUP BY parser_name, status, decode_strategy"
    )

    return {
        "bundle_version": BUNDLE_VERSION,
        "application": "rowanjobs",
        "app_version": __version__,
        "generated_at_utc": utc_str(),
        "generated_at_local": local_str(utc_str()),
        "sanitised": {
            "excluded": [
                "archived response payloads",
                "advertisement description text and markup",
                "request and response headers",
                "configuration secrets and credentials",
            ],
            "included": "coverage evidence, counts, fingerprints and failure detail",
        },
        "health": health,
        "runs": [
            {
                **r,
                "duration": duration_str(str(r["started_at_utc"]), r["ended_at_utc"]),
                "started_at_local": local_str(str(r["started_at_utc"])),
                "ended_at_local": local_str(r["ended_at_utc"]) if r["ended_at_utc"] else None,
            }
            for r in runs
        ],
        "scan_assessments": assessments,
        "coverage_gaps": gaps,
        "fetch_exceptions": failures,
        "unknown_source_labels": unknown_labels,
        "unresolved_listing_entries": unresolved,
        "identity_conflicts": conflicts,
        "resource_outcomes": resources,
        "posting_freshness": freshness,
        "posting_sample": postings,
        "version_statistics": versions,
        "extraction_statistics": extraction_warnings,
    }


def write_diagnostics(cfg: Config, db: Database, destination: Path | None = None) -> Path:
    payload = collect_diagnostics(cfg, db)
    target = (
        Path(destination)
        if destination
        else (cfg.layout.exports_dir / f"diagnostics-{utc_str().replace(':', '')}.json")
    )
    write_json(target, payload)
    return target
