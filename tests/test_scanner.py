"""Complete unfiltered pagination traversal and its qualification.

A traversal may only support absence analysis when it ended for a reason the
source gave, every page was retrieved and recognised, and every row resolved to
a source identifier. Anything else stays as positive evidence but loses
qualification.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from rowanjobs.collect.repo import Repository
from rowanjobs.collect.scanner import ListingScanner, page_url
from rowanjobs.config import Config
from rowanjobs.db import Database
from rowanjobs.net.client import SourceClient

from .conftest import (
    LISTING_URL,
    UNRELATED_ERROR_PAGE,
    FakeSource,
    build_listing_page,
    challenge_response,
    error_response,
    listing_job,
    read_fixture,
    redirect_response,
)

PAGE2_URL = f"{LISTING_URL}?page=2&page-items=20"
PAGE3_URL = f"{LISTING_URL}?page=3&page-items=20"


@pytest.fixture
def scan(
    cfg: Config,
    repo: Repository,
    run_environment: dict[str, Any],
    make_client: Callable[..., SourceClient],
) -> Callable[..., Any]:
    def run(source: FakeSource, *, scan_ordinal: int = 1, scan_role: str = "discovery"):
        scanner = ListingScanner(
            cfg=cfg,
            repo=repo,
            client=make_client(source),
            run_id=run_environment["run_id"],
            source_id=run_environment["source_id"],
        )
        return scanner.scan(scan_ordinal=scan_ordinal, scan_role=scan_role)

    return run


def checks(result) -> dict[str, bool | None]:
    return {c["name"]: c["passed"] for c in result.assessment.checks}


# ------------------------------------------------------------------ page urls


def test_first_page_url_is_the_configured_listing_url_unchanged() -> None:
    assert page_url(LISTING_URL, 1, 20) == LISTING_URL
    assert page_url(LISTING_URL, 2, 20) == PAGE2_URL


# ----------------------------------------------------------------- traversal


def test_traversal_follows_every_more_link_and_stops_when_it_disappears(scan, cfg: Config) -> None:
    source = FakeSource()
    source.page(
        LISTING_URL,
        build_listing_page(
            [listing_job("1001"), listing_job("1002")],
            more_href="/en-us/listing/?page=2&page-items=20",
            more_count=3,
        ),
    )
    source.page(
        PAGE2_URL,
        build_listing_page(
            [listing_job("1003"), listing_job("1004")],
            more_href="/en-us/listing/?page=3&page-items=20",
            more_count=1,
        ),
    )
    source.page(PAGE3_URL, build_listing_page([listing_job("1005")]))

    result = scan(source)

    assert source.urls == [LISTING_URL, PAGE2_URL, PAGE3_URL]
    assert result.termination_reason == "no_more_link"
    assert result.unique_ids == ["1001", "1002", "1003", "1004", "1005"]
    assert result.source_reported_total == 5
    assert result.qualified is True
    assert checks(result)["source_count_reconciles"] is True
    assert checks(result)["legitimate_termination"] is True


def test_source_count_disagreement_costs_the_scan_its_qualification(scan) -> None:
    source = FakeSource()
    source.page(
        LISTING_URL,
        build_listing_page([listing_job("1001")], more_href="/en-us/listing/?page=2", more_count=9),
    )
    source.page(f"{LISTING_URL}?page=2", build_listing_page([listing_job("1002")]))

    result = scan(source)

    assert result.source_reported_total == 10
    assert len(result.unique_ids) == 2
    assert result.qualified is False
    assert checks(result)["source_count_reconciles"] is False


def test_repeated_section_is_recorded_without_inflating_the_advertisement_count(
    scan, db: Database
) -> None:
    source = FakeSource()
    source.page(LISTING_URL, read_fixture("listing_page_last"))

    result = scan(source)

    assert len(result.unique_ids) == 13
    assert result.entries_seen == 26
    assert result.duplicate_occurrences == 13
    rows = db.query(
        "SELECT section, COUNT(*) AS n FROM listing_entries WHERE scan_id = ? GROUP BY section",
        (result.scan_id,),
    )
    assert {r["section"]: int(r["n"]) for r in rows} == {"search-results": 13, "recent-jobs": 13}
    # Both sections point at the same postings; no duplicate posting rows.
    assert int(db.scalar("SELECT COUNT(*) FROM postings")) == 13
    assert (
        int(
            db.scalar(
                "SELECT COUNT(DISTINCT posting_id) FROM listing_entries WHERE scan_id = ?",
                (result.scan_id,),
            )
        )
        == 13
    )
    scan_row = db.one("SELECT * FROM listing_scans WHERE scan_id = ?", (result.scan_id,))
    assert int(scan_row["entries_seen"]) == 26
    assert int(scan_row["unique_ids_seen"]) == 13
    assert int(scan_row["duplicate_occurrences"]) == 13


def test_a_repeated_page_signature_is_a_pagination_loop(scan, db: Database) -> None:
    jobs = [listing_job("1001"), listing_job("1002")]
    page = build_listing_page(jobs, more_href="/en-us/listing/?page=2&page-items=20", more_count=2)
    source = FakeSource()
    source.page(LISTING_URL, page)
    # Page 2 serves exactly the same rows and the same next link.
    source.page(PAGE2_URL, page)

    result = scan(source)

    assert result.termination_reason == "loop_detected"
    assert result.qualified is False
    assert checks(result)["no_pagination_loop"] is False
    # The evidence from both pages is still stored.
    assert (
        int(db.scalar("SELECT COUNT(*) FROM listing_pages WHERE scan_id = ?", (result.scan_id,)))
        == 2
    )


def test_a_failure_part_way_through_pagination_unqualifies_the_scan(scan, db: Database) -> None:
    source = FakeSource()
    source.page(
        LISTING_URL,
        build_listing_page(
            [listing_job("1001")], more_href="/en-us/listing/?page=2&page-items=20", more_count=1
        ),
    )
    source.add(PAGE2_URL, error_response(500))

    result = scan(source)

    assert result.termination_reason == "fetch_failure"
    assert result.qualified is False
    assert checks(result)["all_pages_retrieved"] is False
    assert checks(result)["legitimate_termination"] is False
    # Page 1's advertisement is still positive evidence.
    assert result.unique_ids == ["1001"]
    assert int(db.scalar("SELECT COUNT(*) FROM postings")) == 1


def test_validated_empty_listing_terminates_cleanly_and_qualifies(scan) -> None:
    source = FakeSource()
    source.page(LISTING_URL, read_fixture("listing_page_empty"))

    result = scan(source)

    assert result.termination_reason == "empty_validated_page"
    assert result.unique_ids == []
    assert result.facts.empty_result_validated is True
    assert result.qualified is True
    assert checks(result)["empty_result_validated"] is True


def test_error_page_with_no_rows_is_never_read_as_an_empty_listing(scan) -> None:
    source = FakeSource()
    source.page(LISTING_URL, UNRELATED_ERROR_PAGE)

    result = scan(source)

    assert result.termination_reason == "structure_unrecognized"
    assert result.unique_ids == []
    assert result.qualified is False
    assert checks(result)["structure_recognized"] is False
    assert checks(result)["empty_result_validated"] is False


def test_zero_rows_in_a_broken_template_is_unrecognized_not_empty(scan) -> None:
    source = FakeSource()
    source.page(LISTING_URL, build_listing_page([], columns=False))

    result = scan(source)

    assert result.termination_reason == "structure_unrecognized"
    assert result.facts.empty_result_validated is False
    assert result.qualified is False


def test_access_control_response_unqualifies_the_scan_and_is_recorded_as_such(
    scan, db: Database
) -> None:
    source = FakeSource()
    source.add(LISTING_URL, challenge_response())

    result = scan(source)

    assert result.qualified is False
    assert checks(result)["no_access_control_response"] is False
    assert result.facts.access_control_pages == [1]
    signals = db.query(
        "SELECT access_control_signal, http_status FROM fetches WHERE purpose = 'listing_page'"
    )
    assert signals
    assert all(s["access_control_signal"] == "aws-waf-challenge" for s in signals)
    assert all(int(s["http_status"]) == 202 for s in signals)


def test_unresolved_job_identifier_is_counted_and_unqualifies_the_scan(scan, db: Database) -> None:
    source = FakeSource()
    source.page(
        LISTING_URL,
        build_listing_page(
            [
                listing_job("1001", "Resolvable"),
                listing_job(None, "Mystery", href="/en-us/search/?q=mystery"),
            ]
        ),
    )

    result = scan(source)

    assert result.unresolved_candidates == 2  # once per repeated section
    assert result.qualified is False
    assert checks(result)["no_unresolved_identity"] is False
    rows = db.query(
        "SELECT resolution_state, external_job_id, href_raw, title_text FROM listing_entries "
        "WHERE scan_id = ? AND resolution_state = 'unresolved_id'",
        (result.scan_id,),
    )
    assert len(rows) == 2
    assert rows[0]["external_job_id"] is None
    assert rows[0]["href_raw"] == "/en-us/search/?q=mystery"
    assert rows[0]["title_text"] == "Mystery"
    assert int(db.scalar("SELECT COUNT(*) FROM postings")) == 1


def test_page_ceiling_stops_the_traversal_and_unqualifies_it(scan, cfg: Config) -> None:
    cfg.collection.max_listing_pages = 2
    source = FakeSource()
    for url, next_page in ((LISTING_URL, 2), (PAGE2_URL, 3), (PAGE3_URL, 4)):
        source.page(
            url,
            build_listing_page(
                [listing_job(f"10{next_page}")],
                more_href=f"/en-us/listing/?page={next_page}&page-items=20",
                more_count=99,
            ),
        )

    result = scan(source)

    assert result.termination_reason == "max_pages"
    assert len(result.pages) == 2
    assert result.qualified is False
    assert checks(result)["within_page_bound"] is False


def test_redirect_away_from_the_listing_unqualifies_the_scan(scan) -> None:
    source = FakeSource()
    source.add(LISTING_URL, redirect_response("/en-us/home/"))
    source.page("https://jobs.rowan.edu/en-us/home/", "<html><body>home</body></html>")

    result = scan(source)

    assert result.qualified is False
    assert checks(result)["no_unexpected_redirect"] is False


# ------------------------------------------------------------------- records


def test_scan_records_pages_entries_assessment_and_encountered_ids(
    scan, db: Database, run_environment: dict[str, Any]
) -> None:
    source = FakeSource()
    source.page(
        LISTING_URL,
        build_listing_page(
            [listing_job("1001"), listing_job("1002")],
            more_href="/en-us/listing/?page=2&page-items=20",
            more_count=1,
        ),
    )
    source.page(PAGE2_URL, build_listing_page([listing_job("1003")]))

    result = scan(source)

    pages = db.query(
        "SELECT * FROM listing_pages WHERE scan_id = ? ORDER BY page_number", (result.scan_id,)
    )
    assert [int(p["page_number"]) for p in pages] == [1, 2]
    assert [p["url"] for p in pages] == [LISTING_URL, PAGE2_URL]
    assert int(pages[0]["entry_count"]) == 4
    assert int(pages[0]["unique_id_count"]) == 2
    assert pages[0]["more_link_url"] == PAGE2_URL
    assert int(pages[0]["more_link_remaining"]) == 1
    assert pages[1]["more_link_url"] is None
    assert pages[0]["page_signature"] != pages[1]["page_signature"]
    assert int(pages[0]["structure_recognized"]) == 1

    assessment = db.one(
        "SELECT * FROM listing_scan_assessments WHERE scan_id = ?", (result.scan_id,)
    )
    assert int(assessment["qualified"]) == 1
    assert assessment["reason"] is None

    scan_row = db.one("SELECT * FROM listing_scans WHERE scan_id = ?", (result.scan_id,))
    assert scan_row["termination_reason"] == "no_more_link"
    assert '"1003"' in str(scan_row["encountered_ids_json"])
    assert int(scan_row["run_id"]) == run_environment["run_id"]
    assert scan_row["ended_at_utc"] is not None


def test_listing_link_urls_are_remembered_for_each_posting(scan, db: Database) -> None:
    source = FakeSource()
    source.page(LISTING_URL, build_listing_page([listing_job("1001", slug="a-job")]))

    scan(source)

    row = db.one(
        "SELECT pu.url, pu.role, pu.provenance, pu.seen_count FROM posting_urls pu "
        "JOIN postings p ON p.posting_id = pu.posting_id WHERE p.external_job_id = '1001'"
    )
    assert row["url"] == "https://jobs.rowan.edu/en-us/job/1001/a-job"
    assert row["role"] == "listing-link"
    assert row["provenance"] == "listing_entry"


def test_first_qualified_collection_is_a_baseline_not_a_burst_of_new_postings(
    scan, db: Database
) -> None:
    source = FakeSource()
    source.page(LISTING_URL, build_listing_page([listing_job("1001")]))
    result = scan(source)
    assert result.qualified is True
    assert db.scalar("SELECT discovery_basis FROM postings WHERE external_job_id = '1001'") == (
        "baseline"
    )

    later = FakeSource()
    later.page(LISTING_URL, build_listing_page([listing_job("1001"), listing_job("1002")]))
    scan(later, scan_ordinal=2, scan_role="verification")
    assert db.scalar("SELECT discovery_basis FROM postings WHERE external_job_id = '1002'") == (
        "observed-new"
    )


def test_extraction_rows_are_reused_when_the_same_bytes_are_parsed_again(
    scan, db: Database
) -> None:
    markup = build_listing_page([listing_job("1001")])
    source = FakeSource()
    source.page(LISTING_URL, markup)

    scan(source)
    scan(source, scan_ordinal=2, scan_role="verification")

    assert int(db.scalar("SELECT COUNT(*) FROM artifacts")) == 1
    assert int(db.scalar("SELECT COUNT(*) FROM extractions")) == 1
    assert int(db.scalar("SELECT COUNT(*) FROM fetches")) == 2
    assert int(db.scalar("SELECT COUNT(*) FROM listing_scans")) == 2
