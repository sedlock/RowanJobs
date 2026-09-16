"""The sanitised diagnostic bundle must carry coverage evidence, not content."""

from __future__ import annotations

import json

from rowanjobs.ops.diagnostics import BUNDLE_VERSION, collect_diagnostics, write_diagnostics

from .test_runner import make_source


def test_bundle_contains_the_sections_a_reviewer_needs(cfg, db, collect) -> None:
    collect(make_source({"1001": "Boiler Operator", "1002": "Security Officer"}))
    payload = collect_diagnostics(cfg, db)

    assert payload["bundle_version"] == BUNDLE_VERSION
    for section in (
        "health",
        "runs",
        "scan_assessments",
        "coverage_gaps",
        "fetch_exceptions",
        "unknown_source_labels",
        "unresolved_listing_entries",
        "identity_conflicts",
        "resource_outcomes",
        "posting_freshness",
        "posting_sample",
        "version_statistics",
        "extraction_statistics",
    ):
        assert section in payload, section

    assert payload["runs"], "the run that just completed must appear"
    assert payload["scan_assessments"], "scan assessments must appear"
    assert payload["scan_assessments"][0]["checks"], "each assessment keeps its checks"


def test_bundle_never_carries_description_text_or_headers(cfg, db, collect) -> None:
    """A diagnostic bundle is for coverage questions, not for content."""
    collect(make_source({"1001": "Boiler Operator"}))
    payload = collect_diagnostics(cfg, db)
    blob = json.dumps(payload)

    description = db.scalar(
        "SELECT description_text FROM posting_versions WHERE description_text IS NOT NULL LIMIT 1"
    )
    assert description, "the fixture run should have captured a description"
    assert str(description)[:120] not in blob

    assert "response_headers_json" not in blob
    assert "set-cookie" not in blob.lower()
    for sample in payload["posting_sample"]:
        assert "description_text" not in sample
        assert sample["current_content_fingerprint"], "fingerprints stand in for content"


def test_bundle_is_written_atomically_to_the_exports_directory(cfg, db, collect) -> None:
    collect(make_source({"1001": "Boiler Operator"}))
    path = write_diagnostics(cfg, db)

    assert path.parent == cfg.layout.exports_dir
    assert path.stat().st_mode & 0o777 == 0o600
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["application"] == "rowanjobs"
    assert not list(path.parent.glob(".*.tmp")), "no temp file may be left behind"
