"""Export with provenance.

A spreadsheet must not evaluate an advertisement's text as a formula, and an
export must never imply that "last known" means "checked today".
"""

from __future__ import annotations

import csv
import io
import json

import pytest

from rowanjobs.db import Database
from rowanjobs.export import (
    DATASETS,
    provenance,
    rows_for,
    spreadsheet_safe,
    write_csv_export,
    write_json_export,
)

from .conftest import LISTING_URL, FakeSource, build_detail_page, build_listing_page, listing_job

FORMULA_TITLE = "=cmd|'/C calc'!A0"


@pytest.fixture
def exported(collect, db: Database) -> Database:
    source = FakeSource()
    source.page(
        LISTING_URL,
        build_listing_page([listing_job("1001", FORMULA_TITLE, slug="job-1001")]),
    )
    source.page(
        "https://jobs.rowan.edu/en-us/job/1001/job-1001",
        build_detail_page(job_id="1001", title=FORMULA_TITLE),
    )
    assert collect(source).outcome == "success"
    return db


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("=SUM(A1:A9)", "'=SUM(A1:A9)"),
        ("+1234", "'+1234"),
        ("-lead", "'-lead"),
        ("@import", "'@import"),
        ("\tstarts with tab", "'\tstarts with tab"),
        ("ordinary text", "ordinary text"),
        ("", ""),
        (None, None),
        (42, 42),
    ],
)
def test_spreadsheet_safe_only_neutralises_formula_like_strings(value, expected) -> None:
    assert spreadsheet_safe(value) == expected


def test_csv_prefixes_a_formula_like_value_while_json_keeps_it_verbatim(
    exported: Database,
) -> None:
    csv_buffer = io.StringIO()
    write_csv_export(exported, "versions", csv_buffer)
    json_buffer = io.StringIO()
    write_json_export(exported, "versions", json_buffer)

    rows = list(csv.reader(io.StringIO(csv_buffer.getvalue())))
    header = rows[1]
    data = rows[2]
    title = data[header.index("title")]
    assert title == "'" + FORMULA_TITLE

    payload = json.loads(json_buffer.getvalue())
    assert payload["rows"][0]["title"] == FORMULA_TITLE
    assert not payload["rows"][0]["title"].startswith("'")


def test_both_formats_carry_the_same_provenance_metadata(exported: Database) -> None:
    csv_buffer = io.StringIO()
    write_csv_export(exported, "current", csv_buffer)
    json_buffer = io.StringIO()
    write_json_export(exported, "current", json_buffer)

    comment = next(iter(csv.reader(io.StringIO(csv_buffer.getvalue()))))[0]
    assert comment.startswith("# rowanjobs export ")
    csv_meta = json.loads(comment[len("# rowanjobs export ") :])
    json_meta = json.loads(json_buffer.getvalue())["provenance"]

    for meta in (csv_meta, json_meta):
        assert meta["application"] == "rowanjobs"
        assert meta["export_schema_version"] == "1"
        assert meta["dataset"] == "current"
        assert meta["exported_at_utc"].endswith("Z")
        assert meta["exported_at_local"]
        assert set(meta["versions"]) == {
            "parser",
            "comparison_contract",
            "text_contract",
            "qualification_rules",
            "event_rules",
        }
        assert any("verbatim" in note for note in meta["notes"])
        assert any("content_freshness" in note for note in meta["notes"])


def test_the_current_dataset_states_freshness_and_source_values_separately(
    exported: Database,
) -> None:
    rows = rows_for(exported, "current")
    assert len(rows) == 1
    row = rows[0]
    assert row["external_job_id"] == "1001"
    assert row["content_freshness"] == "checked"
    assert row["last_listed_at_utc"]
    assert row["last_captured_at_utc"]
    assert row["location_source_value"] == "Glassboro, New Jersey"
    assert row["advertised_precision"] == "date"
    assert row["applications_close_parsed_utc"] == "2026-09-30T03:55:00Z"
    assert row["discovery_basis"] == "baseline"


def test_every_documented_dataset_exports_without_error(exported: Database) -> None:
    for dataset in DATASETS:
        buffer = io.StringIO()
        write_json_export(exported, dataset, buffer)
        payload = json.loads(buffer.getvalue())
        assert payload["provenance"]["dataset"] == dataset
        assert isinstance(payload["rows"], list)


def test_a_dataset_can_be_filtered_to_one_advertisement(exported: Database) -> None:
    assert rows_for(exported, "history", job_id="1001")
    assert rows_for(exported, "history", job_id="nope") == []


def test_limit_bounds_the_exported_rows(exported: Database) -> None:
    assert len(rows_for(exported, "observations", limit=1)) == 1


def test_an_unknown_dataset_is_rejected_by_name(exported: Database) -> None:
    with pytest.raises(ValueError, match="unknown dataset"):
        rows_for(exported, "everything")


def test_an_empty_dataset_still_emits_provenance(db: Database) -> None:
    buffer = io.StringIO()
    count = write_csv_export(db, "postings", buffer)
    assert count == 0
    rows = list(csv.reader(io.StringIO(buffer.getvalue())))
    assert len(rows) == 1
    assert rows[0][0].startswith("# rowanjobs export ")

    json_buffer = io.StringIO()
    assert write_json_export(db, "postings", json_buffer) == 0
    assert json.loads(json_buffer.getvalue())["rows"] == []


def test_provenance_names_the_database_it_came_from(db: Database) -> None:
    meta = provenance(db, "postings", {})
    assert meta["database"] == str(db.path)
    assert meta["app_version"]
