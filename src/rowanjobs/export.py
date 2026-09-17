"""Export with provenance.

Exports always carry the observation time and the coverage quality behind each
row, so a spreadsheet cannot quietly imply that "last known" means "checked
today".

CSV output is spreadsheet-safe: a value that a spreadsheet would treat as a
formula is prefixed with an apostrophe. Job descriptions are untrusted text from
a web page, and "=cmd|..." in a cell is a real attack. The JSON export keeps the
source value untouched.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TextIO

from . import (
    CONTRACT_VERSION,
    EVENT_RULES_VERSION,
    PARSER_VERSION,
    QUALIFICATION_RULES_VERSION,
    TEXT_CONTRACT_VERSION,
    __version__,
)
from .db import Database
from .timeutil import local_str, utc_str

EXPORT_SCHEMA_VERSION = "1"
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

DATASETS = ("postings", "current", "history", "versions", "observations", "runs", "links")


def spreadsheet_safe(value: Any) -> Any:
    """Neutralise values a spreadsheet would evaluate as a formula."""
    if not isinstance(value, str) or not value:
        return value
    if value[0] in FORMULA_PREFIXES:
        return "'" + value
    return value


def provenance(db: Database, dataset: str, filters: dict[str, Any]) -> dict[str, Any]:
    return {
        "export_schema_version": EXPORT_SCHEMA_VERSION,
        "application": "rowanjobs",
        "app_version": __version__,
        "dataset": dataset,
        "filters": filters,
        "exported_at_utc": utc_str(),
        "exported_at_local": local_str(utc_str()),
        "database": str(db.path),
        "versions": {
            "parser": PARSER_VERSION,
            "comparison_contract": CONTRACT_VERSION,
            "text_contract": TEXT_CONTRACT_VERSION,
            "qualification_rules": QUALIFICATION_RULES_VERSION,
            "event_rules": EVENT_RULES_VERSION,
        },
        "notes": [
            "Source values are preserved verbatim. Normalised interpretations are "
            "separate columns and never overwrite the source value.",
            "content_freshness distinguishes content checked in the most recent "
            "observation from content carried forward after a failed check.",
            "CSV output prefixes formula-like values with an apostrophe; the JSON "
            "export keeps the untouched source value.",
        ],
    }


_QUERIES: dict[str, str] = {
    "postings": """
        SELECT p.posting_id, p.external_job_id, p.source_namespace,
               p.first_discovered_at_utc, p.discovery_basis
          FROM postings p ORDER BY p.external_job_id
    """,
    "current": """
        SELECT c.external_job_id, c.current_title, c.discovery_basis,
               c.first_discovered_at_utc, c.last_listed_at_utc,
               c.last_seen_any_scan_at_utc,
               c.last_captured_at_utc, c.last_check_at_utc,
               c.last_availability_state, c.last_identity_state,
               c.content_freshness, c.version_count, c.observation_count,
               c.current_content_fingerprint,
               (SELECT value_text FROM version_values vv
                 WHERE vv.posting_version_id = c.current_version_id
                   AND vv.field_key = 'location' LIMIT 1) AS location_source_value,
               (SELECT value_text FROM version_values vv
                 WHERE vv.posting_version_id = c.current_version_id
                   AND vv.field_key = 'categories' LIMIT 1) AS categories_source_value,
               (SELECT value_text FROM version_values vv
                 WHERE vv.posting_version_id = c.current_version_id
                   AND vv.field_key = 'work_type' LIMIT 1) AS work_type_source_value,
               (SELECT value_text FROM version_values vv
                 WHERE vv.posting_version_id = c.current_version_id
                   AND vv.field_key = 'advertised' LIMIT 1) AS advertised_source_value,
               (SELECT source_precision FROM version_values vv
                 WHERE vv.posting_version_id = c.current_version_id
                   AND vv.field_key = 'advertised' LIMIT 1) AS advertised_precision,
               (SELECT value_text FROM version_values vv
                 WHERE vv.posting_version_id = c.current_version_id
                   AND vv.field_key = 'applications_close' LIMIT 1)
                   AS applications_close_source_value,
               (SELECT parsed_utc FROM version_values vv
                 WHERE vv.posting_version_id = c.current_version_id
                   AND vv.field_key = 'applications_close' LIMIT 1)
                   AS applications_close_parsed_utc
          FROM v_posting_current c ORDER BY c.external_job_id
    """,
    "history": """
        SELECT h.external_job_id, h.observed_at_utc, h.run_id, h.run_kind,
               h.scheduled_slot_local_date, h.identity_state, h.availability_state,
               h.availability_detail, h.redirect_class, h.checked_because,
               h.posting_version_id, h.content_fingerprint, h.title,
               h.http_status, h.final_url, h.access_control_signal
          FROM v_posting_history h ORDER BY h.external_job_id, h.observed_at_utc
    """,
    "versions": """
        SELECT p.external_job_id, v.posting_version_id, v.first_seen_at_utc,
               v.contract_version, v.text_contract_version, v.title,
               v.content_fingerprint, v.description_text_fingerprint,
               v.description_html_fingerprint, v.metadata_fingerprint,
               v.description_html_kind, LENGTH(v.description_text) AS description_chars,
               v.description_text
          FROM posting_versions v JOIN postings p ON p.posting_id = v.posting_id
         ORDER BY p.external_job_id, v.first_seen_at_utc
    """,
    "observations": """
        SELECT p.external_job_id, o.observation_id, o.run_id, o.observed_at_utc,
               o.identity_state, o.availability_state, o.availability_detail,
               o.observed_external_job_id, o.redirect_class, o.checked_because,
               o.posting_version_id, o.conflicts_json
          FROM posting_observations o LEFT JOIN postings p ON p.posting_id = o.posting_id
         ORDER BY o.observed_at_utc, o.observation_id
    """,
    "runs": """
        SELECT run_id, run_uuid, run_kind, attempt_no, parent_run_id,
               scheduled_slot_local_date, started_at_utc, ended_at_utc, outcome,
               outcome_detail, is_baseline, scans, qualified_scans, fetches,
               failed_fetches, access_control_responses, observations, captured,
               resources, coverage_gaps
          FROM v_run_health ORDER BY started_at_utc DESC
    """,
    "links": """
        SELECT p.external_job_id, rl.parent_kind, rl.url_raw, rl.url_resolved,
               rl.link_text, rl.classification, rl.collection_decision,
               rl.exclusion_reason, rl.first_seen_at_utc
          FROM resource_links rl
          JOIN posting_versions v ON v.posting_version_id = rl.posting_version_id
          JOIN postings p ON p.posting_id = v.posting_id
         ORDER BY p.external_job_id, rl.position
    """,
}


# How each dataset is narrowed to one advertisement. Written per dataset rather
# than spliced into the SQL: for `observations` the job id lives on a LEFT JOIN,
# so a naive `WHERE external_job_id = ?` would silently drop observations whose
# identity is not yet registered -- exactly the case worth inspecting.
_JOB_FILTERS: dict[str, str] = {
    "postings": "p.external_job_id = ?",
    "current": "c.external_job_id = ?",
    "history": "h.external_job_id = ?",
    "versions": "p.external_job_id = ?",
    "observations": "(p.external_job_id = ? OR o.expected_external_job_id = ?)",
    "links": "p.external_job_id = ?",
}

_JOB_FILTER_PARAMS: dict[str, int] = {"observations": 2}


def rows_for(
    db: Database, dataset: str, *, job_id: str | None = None, limit: int | None = None
) -> list[dict[str, Any]]:
    if dataset not in _QUERIES:
        raise ValueError(f"unknown dataset {dataset!r}; choose from {', '.join(DATASETS)}")
    sql = _QUERIES[dataset].strip()
    params: list[Any] = []
    if job_id:
        predicate = _JOB_FILTERS.get(dataset)
        if predicate is None:
            raise ValueError(f"dataset {dataset!r} cannot be filtered by job id")
        head, sep, tail = sql.partition(" ORDER BY ")
        if not sep:  # pragma: no cover - every dataset orders its rows
            raise ValueError(f"dataset {dataset!r} has no ORDER BY to split on")
        sql = f"{head} WHERE {predicate} ORDER BY {tail}"
        params.extend([str(job_id)] * _JOB_FILTER_PARAMS.get(dataset, 1))
    if limit:
        sql += f" LIMIT {int(limit)}"
    return db.query(sql, tuple(params))


def write_json_export(db: Database, dataset: str, stream: TextIO, **filters: Any) -> int:
    rows = rows_for(db, dataset, **filters)
    payload = {"provenance": provenance(db, dataset, filters), "rows": rows}
    json.dump(payload, stream, ensure_ascii=False, indent=2)
    stream.write("\n")
    return len(rows)


def write_csv_export(db: Database, dataset: str, stream: TextIO, **filters: Any) -> int:
    rows = rows_for(db, dataset, **filters)
    meta = provenance(db, dataset, filters)
    writer = csv.writer(stream, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([f"# rowanjobs export {json.dumps(meta, ensure_ascii=False)}"])
    if not rows:
        return 0
    headers = list(rows[0].keys())
    writer.writerow(headers)
    for row in rows:
        writer.writerow([spreadsheet_safe(row.get(h)) for h in headers])
    return len(rows)


def export_to_path(
    db: Database, dataset: str, path: Path, fmt: str, **filters: Any
) -> tuple[Path, int]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exports carry full advertisement text; keep them as restrictive as the
    # rest of the data root rather than relying on the directory alone.
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    with path.open("w", encoding="utf-8", newline="") as fh:
        count = (
            write_csv_export(db, dataset, fh, **filters)
            if fmt == "csv"
            else write_json_export(db, dataset, fh, **filters)
        )
    return path, count


def iter_datasets() -> Iterator[str]:
    yield from DATASETS
