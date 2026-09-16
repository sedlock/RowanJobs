"""The PageUp listing adapter.

The page repeats every advertisement in a second section, paginates with a
"More Jobs" link whose count is the number of jobs *remaining*, and answers past
the last page with an intact but empty table. Each of those has to be told apart
from a failure.
"""

from __future__ import annotations

import pytest

from rowanjobs.extract.pageup_listing import job_id_from_href, parse_listing

from .conftest import (
    LISTING_URL,
    UNRELATED_ERROR_PAGE,
    build_listing_page,
    listing_job,
    read_fixture,
)


@pytest.fixture
def page1() -> str:
    return read_fixture("listing_page1")


def parse(markup: str):
    return parse_listing(markup, LISTING_URL)


# ------------------------------------------------------------------- sections


def test_repeated_recent_jobs_section_does_not_inflate_the_advertisement_count(
    page1: str,
) -> None:
    extraction = parse(page1)
    assert extraction.sections_found == ["search-results", "recent-jobs"]
    # The page carries 40 a.job-link elements: 20 advertisements, listed twice.
    assert len(extraction.entries) == 40
    assert len(extraction.authoritative_entries) == 20
    assert len(extraction.unique_job_ids()) == 20
    assert len(set(extraction.unique_job_ids())) == 20


def test_each_section_is_recorded_separately_with_its_own_positions(page1: str) -> None:
    extraction = parse(page1)
    search = [e for e in extraction.entries if e.section == "search-results"]
    recent = [e for e in extraction.entries if e.section == "recent-jobs"]
    assert len(search) == len(recent) == 20
    assert [e.position_in_section for e in search] == list(range(1, 21))
    assert [e.position_in_section for e in recent] == list(range(1, 21))
    assert [e.external_job_id for e in search] == [e.external_job_id for e in recent]


def test_only_the_search_results_section_is_authoritative() -> None:
    markup = build_listing_page(
        [listing_job("1001", "Listed")],
        recent_jobs=[listing_job("9001", "Only in the repeated section")],
    )
    extraction = parse(markup)
    assert extraction.unique_job_ids() == ["1001"]
    assert {e.external_job_id for e in extraction.entries} == {"1001", "9001"}


def test_summary_row_is_attached_to_the_advertisement_above_it(page1: str) -> None:
    extraction = parse(page1)
    first = extraction.authoritative_entries[0]
    assert first.external_job_id == "501826"
    assert first.summary_text is not None
    assert first.summary_text.startswith("Summary:")
    assert first.summary_html is not None
    # The repeated section comments its summaries out, so none is invented there.
    repeated = next(e for e in extraction.entries if e.section == "recent-jobs")
    assert repeated.summary_text is None


def test_orphan_summary_row_is_warned_about_rather_than_dropped_silently() -> None:
    markup = build_listing_page([listing_job("1001")]).replace(
        '<tbody id="search-results-content">',
        '<tbody id="search-results-content">\n<tr class="summary"><td>orphan</td></tr>',
        1,
    )
    extraction = parse(markup)
    assert any("orphan summary row" in w for w in extraction.warnings)
    assert extraction.unique_job_ids() == ["1001"]


# ------------------------------------------------------------------ pagination


def test_more_link_reports_the_jobs_remaining_after_this_page(page1: str) -> None:
    extraction = parse(page1)
    assert extraction.more_link_url == f"{LISTING_URL}?page=2&page-items=20"
    assert extraction.more_link_remaining == 113


def test_final_page_has_no_more_link() -> None:
    extraction = parse(read_fixture("listing_page_last"))
    assert extraction.more_link_url is None
    assert extraction.more_link_remaining is None
    assert len(extraction.unique_job_ids()) == 13


def test_more_link_without_a_count_is_still_followed_but_warned_about() -> None:
    markup = build_listing_page([listing_job("1001")], more_href="/en-us/listing/?page=2")
    extraction = parse(markup)
    assert extraction.more_link_url == f"{LISTING_URL}?page=2"
    assert extraction.more_link_remaining is None
    assert any("without a count" in w for w in extraction.warnings)


def test_page_signature_tracks_the_identifier_sequence_and_the_next_page() -> None:
    jobs = [listing_job("1001"), listing_job("1002")]
    first = parse(build_listing_page(jobs, more_href="/en-us/listing/?page=2", more_count=2))
    same = parse(build_listing_page(jobs, more_href="/en-us/listing/?page=2", more_count=2))
    reordered = parse(
        build_listing_page(jobs[::-1], more_href="/en-us/listing/?page=2", more_count=2)
    )
    assert first.page_signature == same.page_signature
    assert first.page_signature != reordered.page_signature


# ---------------------------------------------------------------- empty result


def test_intact_empty_table_validates_as_a_real_empty_result() -> None:
    extraction = parse(read_fixture("listing_page_empty"))
    assert extraction.status == "ok"
    assert extraction.structure_recognized is True
    assert extraction.empty_result_validated is True
    assert extraction.entries == []
    assert extraction.more_link_url is None


def test_error_page_with_zero_rows_does_not_validate_as_empty() -> None:
    extraction = parse(UNRELATED_ERROR_PAGE)
    assert extraction.status == "partial"
    assert extraction.structure_recognized is False
    assert extraction.empty_result_validated is False
    assert any("structure not recognised" in w for w in extraction.warnings)


def test_zero_rows_without_the_result_heading_does_not_validate_as_empty() -> None:
    markup = build_listing_page([], heading="Something else entirely")
    extraction = parse(markup)
    assert extraction.structure_recognized is True
    assert extraction.empty_result_validated is False


def test_zero_rows_without_the_expected_columns_does_not_validate_as_empty() -> None:
    markup = build_listing_page([], columns=False)
    extraction = parse(markup)
    assert extraction.structure_recognized is True
    assert extraction.empty_result_validated is False


def test_unparseable_markup_fails_rather_than_reporting_an_empty_listing() -> None:
    extraction = parse("")
    assert extraction.status == "failed"
    assert extraction.structure_recognized is False
    assert extraction.empty_result_validated is False
    assert extraction.failure_detail is not None


# ------------------------------------------------------------------- identity


def test_job_link_whose_href_is_not_a_job_url_is_recorded_as_unresolved() -> None:
    markup = build_listing_page(
        [
            listing_job("1001", "Resolvable"),
            listing_job(None, "Mystery", href="/en-us/search/?keywords=mystery"),
        ]
    )
    extraction = parse(markup)
    unresolved = [e for e in extraction.entries if e.resolution_state == "unresolved_id"]
    assert len(unresolved) == 2  # once in each section
    assert unresolved[0].external_job_id is None
    assert unresolved[0].title_text == "Mystery"
    assert unresolved[0].href_raw == "/en-us/search/?keywords=mystery"
    assert unresolved[0].resolution_detail is not None
    assert extraction.unresolved_count() == 2
    assert extraction.unique_job_ids() == ["1001"]


def test_row_with_content_but_no_job_link_is_kept_as_an_unrecognized_row() -> None:
    markup = build_listing_page([listing_job("1001")]).replace(
        '<tbody id="search-results-content">',
        '<tbody id="search-results-content">\n'
        '    <tr><td colspan="3">No matching jobs in this category</td></tr>',
        1,
    )
    extraction = parse(markup)
    states = [e.resolution_state for e in extraction.entries if e.section == "search-results"]
    assert "unrecognized_row" in states
    assert extraction.unresolved_count() >= 1


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("/en-us/job/501826/some-slug", "501826"),
        ("https://jobs.rowan.edu/en-us/job/501826/some-slug", "501826"),
        ("/en-us/job/501826", "501826"),
        ("/en-us/job/abc/slug", None),
        ("/en-us/listing/", None),
        ("/fr-ca/job/501826/slug", "501826"),
    ],
)
def test_job_id_is_read_only_from_a_job_url_shape(href: str, expected: str | None) -> None:
    assert job_id_from_href(href) == expected


# ------------------------------------------------------------------- metadata


def test_displayed_row_metadata_keeps_the_location_comma_intact(page1: str) -> None:
    extraction = parse(page1)
    metadata = extraction.authoritative_entries[0].displayed_metadata
    assert metadata["location"] == {"label": "Location", "text": "Glassboro, New Jersey"}
    assert metadata["close_date"]["text"] == "Sep 29 2026"
    assert metadata["close_date"]["machine_value"] == "2026-09-30T03:55:00Z"


def test_unknown_labelled_span_in_a_row_is_preserved_rather_than_dropped() -> None:
    markup = build_listing_page([listing_job("1001")]).replace(
        '<span class="location">',
        '<span class="salary-band">Band 7</span><span class="location">',
        1,
    )
    extraction = parse(markup)
    metadata = extraction.authoritative_entries[0].displayed_metadata
    assert {"class": "salary-band", "text": "Band 7"} in metadata["unknown_spans"]


def test_non_job_links_are_recorded_with_why_they_are_out_of_scope(page1: str) -> None:
    extraction = parse(page1)
    classifications = {link["classification"] for link in extraction.other_links}
    assert "internal_nav" in classifications
    assert all("job-link" not in link["classes"] for link in extraction.other_links)


def test_as_dict_carries_the_parser_identity_for_the_extraction_record() -> None:
    payload = parse(build_listing_page([listing_job("1001")])).as_dict()
    assert payload["parser"] == "pageup_listing"
    assert payload["status"] == "ok"
    assert payload["structure_recognized"] is True
    assert len(payload["entries"]) == 2
