"""The PageUp detail adapter.

Labels are read generically, so a field this adapter has never seen is still
captured. Multivalued fields keep the source's own separator, and the
description is handed back as a byte-exact slice of the archived document
whenever that can be demonstrated.
"""

from __future__ import annotations

import pytest

from rowanjobs.constants import FIELD_STATES
from rowanjobs.extract.pageup_detail import classify_link, job_id_from_url, parse_detail

from .conftest import build_detail_page, read_fixture

DETAIL_URL = "https://jobs.rowan.edu/en-us/job/501826/temporary-part-time"


def parse(markup: str, url: str = DETAIL_URL):
    return parse_detail(markup, url)


def value(extraction, field_key: str):
    return next(v for v in extraction.values if v.field_key == field_key)


@pytest.fixture
def captured() -> str:
    return read_fixture("detail_501826")


# ------------------------------------------------------------------- identity


def test_real_capture_yields_title_job_id_and_ordered_source_fields(captured: str) -> None:
    extraction = parse(captured)
    assert extraction.status == "ok"
    assert extraction.structure_recognized is True
    assert extraction.title == (
        "Temporary Part Time Hourly Public Safety Telecommunicator (Police Department)"
    )
    assert extraction.external_job_id == "501826"
    assert [v.field_key for v in extraction.values] == [
        "job_no",
        "work_type",
        "location",
        "categories",
        "advertised",
        "applications_close",
    ]
    assert extraction.closure_signal is None


def test_job_id_prefers_the_span_the_page_displays(captured: str) -> None:
    markup = captured.replace(
        '<span class="job-externalJobNo">501826</span>',
        '<span class="job-externalJobNo">501827</span>',
    )
    extraction = parse(markup)
    assert extraction.external_job_id == "501827"


def test_missing_job_number_is_warned_about_not_guessed() -> None:
    markup = build_detail_page(job_id=None)
    extraction = parse(markup)
    assert extraction.external_job_id is None
    assert any("no job number displayed" in w for w in extraction.warnings)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://jobs.rowan.edu/en-us/job/501826/slug", "501826"),
        ("https://jobs.rowan.edu/en-us/job/501826", "501826"),
        ("https://jobs.rowan.edu/en-us/listing/", None),
    ],
)
def test_job_id_from_url_only_matches_the_detail_url_shape(url: str, expected: str | None) -> None:
    assert job_id_from_url(url) == expected


# ------------------------------------------------------------- field fidelity


def test_location_with_a_comma_is_one_value_and_is_never_split_on_the_comma(
    captured: str,
) -> None:
    location = value(parse(captured), "location")
    assert location.value_text == "Glassboro, New Jersey"
    assert location.normalized is not None
    assert location.normalized["values"] == ["Glassboro, New Jersey"]
    assert location.normalized["separator"] is None


def test_semicolon_multivalue_is_split_in_normalisation_while_the_text_stays_verbatim() -> None:
    extraction = parse(
        build_detail_page(
            location="Glassboro, New Jersey; Camden, New Jersey",
            categories="Engineering; Research Support; Administration",
        )
    )
    location = value(extraction, "location")
    assert location.value_text == "Glassboro, New Jersey; Camden, New Jersey"
    assert location.normalized["values"] == ["Glassboro, New Jersey", "Camden, New Jersey"]
    assert location.normalized["separator"] == ";"
    categories = value(extraction, "categories")
    assert categories.value_text == "Engineering; Research Support; Administration"
    assert len(categories.normalized["values"]) == 3


def test_repeated_location_spans_are_kept_as_separate_ordered_values() -> None:
    extraction = parse(
        build_detail_page(
            extra_fields=(("Location:", '<span class="location">Camden, New Jersey</span>'),)
        )
    )
    locations = [v for v in extraction.values if v.field_key == "location"]
    assert [v.ordinal for v in locations] == [0, 1]
    assert [v.value_text for v in locations] == ["Glassboro, New Jersey", "Camden, New Jersey"]


def test_present_blank_and_absent_field_states_are_distinguished() -> None:
    extraction = parse(
        build_detail_page(
            work_type="",  # label present, value empty
            categories=None,  # label not on the page at all
        )
    )
    assert value(extraction, "location").field_state == "present"
    assert value(extraction, "work_type").field_state == "blank"
    assert value(extraction, "work_type").value_text == ""
    assert [v.field_key for v in extraction.values].count("categories") == 0
    for field in extraction.values:
        assert field.field_state in FIELD_STATES


def test_label_followed_immediately_by_a_break_is_recorded_as_absent() -> None:
    extraction = parse(build_detail_page(extra_fields=(("Salary:", None),)))
    salary = value(extraction, "salary")
    assert salary.field_state == "absent"
    assert salary.value_text is None


def test_unknown_source_label_is_preserved_verbatim_and_flagged() -> None:
    extraction = parse(
        build_detail_page(extra_fields=(("Some New Label:", "a brand new value"),))
    )
    unknown = value(extraction, "some_new_label")
    assert unknown.known_label is False
    assert unknown.source_label == "Some New Label:"
    assert unknown.value_text == "a brand new value"
    assert any("unknown source label preserved" in w for w in extraction.warnings)
    assert value(extraction, "location").known_label is True


def test_bold_run_in_headings_inside_the_description_are_not_read_as_metadata(
    captured: str,
) -> None:
    keys = [v.field_key for v in parse(captured).values]
    assert "summary" not in keys
    assert "major_duties" not in keys
    assert "preferred_qualifications_and_skills" not in keys


def test_date_fields_carry_display_machine_and_timezone_text_separately(captured: str) -> None:
    advertised = value(parse(captured), "advertised")
    assert advertised.value_text == "Sep 15 2026 Eastern Daylight Time"
    assert advertised.date is not None
    assert advertised.date.display_text == "Sep 15 2026"
    assert advertised.date.machine_value == "2026-09-15T12:00:00Z"
    assert advertised.date.tz_text == "Eastern Daylight Time"
    assert advertised.date.precision == "date"

    closes = value(parse(captured), "applications_close")
    assert closes.date.precision == "minute"
    assert closes.date.parsed_utc == "2026-09-30T03:55:00Z"


def test_date_label_with_no_value_reports_an_absent_parse_state() -> None:
    extraction = parse(build_detail_page(advertised=None, extra_fields=(("Advertised:", None),)))
    advertised = value(extraction, "advertised")
    assert advertised.field_state == "absent"
    assert advertised.date is not None
    assert advertised.date.parse_state == "absent"


# -------------------------------------------------------------- description


def test_description_html_is_a_byte_exact_slice_of_the_decoded_document(captured: str) -> None:
    extraction = parse(captured)
    assert extraction.description_html_kind == "source-substring"
    assert extraction.description_html is not None
    assert extraction.description_html in captured
    start = captured.index('<div id="job-details">')
    assert captured[start:].startswith('<div id="job-details">' + extraction.description_html)


def test_description_text_keeps_the_published_bullets_and_punctuation(captured: str) -> None:
    text = parse(captured).description_text
    assert text is not None
    assert "• Receives emergency and non-emergency calls" in text
    assert "Valid New Jersey Driver’s License." in text
    assert " " in text


def test_missing_job_details_container_is_a_partial_extraction_not_a_failure() -> None:
    extraction = parse(build_detail_page(include_job_details=False))
    assert extraction.status == "partial"
    assert extraction.description_html is None
    assert extraction.description_html_kind == "absent"
    assert any("not isolated" in w for w in extraction.warnings)


def test_page_without_a_job_container_is_not_a_recognised_detail_page() -> None:
    extraction = parse("<html><body><h1>Error</h1></body></html>")
    assert extraction.status == "failed"
    assert extraction.structure_recognized is False
    assert extraction.failure_detail is not None
    assert "not a recognised detail page" in extraction.failure_detail


def test_empty_job_container_is_reported_as_a_closure_signal() -> None:
    markup = build_detail_page()
    markup = markup[: markup.index('<div id="job"><div id="job-content">')] + (
        '<div id="job"><div id="job-content">\n</div></div></body></html>'
    )
    extraction = parse(markup)
    assert extraction.structure_recognized is True
    assert extraction.closure_signal == "empty-job-content"
    assert extraction.description_html is None


def test_message_list_closure_template_is_captured_as_the_closure_signal() -> None:
    markup = build_detail_page(
        messages="<li>The job you are looking for is no longer available.</li>"
    )
    extraction = parse(markup)
    assert extraction.closure_signal is not None
    assert extraction.closure_signal.startswith("message-list:")
    assert "no longer available" in extraction.closure_signal


def test_closure_phrase_in_the_body_is_recognised_without_a_message_list() -> None:
    extraction = parse(
        build_detail_page(body_html="<p>This job is no longer advertised.</p>")
    )
    assert extraction.closure_signal == "closure phrase: 'this job is no longer'"


# -------------------------------------------------------------------- links


def test_links_are_classified_with_an_explicit_collection_decision(captured: str) -> None:
    extraction = parse(captured)
    decisions = {
        (link["classification"], link["collection_decision"]) for link in extraction.links
    }
    assert ("apply_workflow", "exclude") in decisions
    assert ("internal_nav", "exclude") in decisions
    for link in extraction.links:
        if link["collection_decision"] == "exclude":
            assert link["exclusion_reason"]


def test_job_documents_on_the_career_site_are_the_only_links_fetched() -> None:
    extraction = parse(
        build_detail_page(
            body_html=(
                '<p><a href="/documents/jd.pdf">Description</a>'
                ' <a href="https://example.org/other.pdf">Elsewhere</a>'
                ' <a href="mailto:hr@rowan.edu">Mail</a></p>'
            )
        )
    )
    fetched = [link for link in extraction.links if link["collection_decision"] == "fetch"]
    assert {link["url_resolved"] for link in fetched} == {
        "https://jobs.rowan.edu/documents/jd.pdf"
    }
    offsite = next(
        link for link in extraction.links if link["url_resolved"].startswith("https://example.org")
    )
    assert offsite["classification"] == "job_document"
    assert offsite["collection_decision"] == "exclude"
    assert "outside the v1 fetch scope" in offsite["exclusion_reason"]


@pytest.mark.parametrize(
    ("url", "classification", "decision"),
    [
        ("mailto:hr@rowan.edu", "mailto", "exclude"),
        ("javascript:void(0)", "anchor", "exclude"),
        ("https://secure.dc4.pageuppeople.com/apply/860/x", "apply_workflow", "exclude"),
        ("https://jobs.rowan.edu/en-us/listing/", "internal_nav", "exclude"),
        ("https://jobs.rowan.edu/documents/jd.docx", "job_document", "fetch"),
        ("https://www.rowan.edu/hr/", "external", "exclude"),
    ],
)
def test_link_classification_rules(url: str, classification: str, decision: str) -> None:
    from lxml import html

    anchor = html.fromstring('<a href="#">x</a>')
    assert classify_link(url, anchor)[:2] == (classification, decision)


def test_application_workflow_links_are_never_followed(captured: str) -> None:
    extraction = parse(captured)
    apply_links = [
        link for link in extraction.links if link["classification"] == "apply_workflow"
    ]
    assert apply_links
    assert all(link["collection_decision"] == "exclude" for link in apply_links)
