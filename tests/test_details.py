"""Detail retrieval, identity checking and resource capture.

The job number the page displays is compared with the one that was expected on
every retrieval. A mismatch or a redirect to a different advertisement is
preserved as a conflict; the destination's description is never assigned to the
posting that was asked for.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from rowanjobs.collect.details import DetailCollector
from rowanjobs.collect.repo import Repository
from rowanjobs.config import Config
from rowanjobs.db import Database
from rowanjobs.net.client import SourceClient
from rowanjobs.net.guard import UrlPolicy

from .conftest import (
    LISTING_URL,
    FakeSource,
    build_detail_page,
    build_listing_page,
    bytes_response,
    challenge_response,
    error_response,
    html_response,
    listing_job,
    redirect_response,
)

JOB_A = "501826"
JOB_B = "501829"
URL_A = f"https://jobs.rowan.edu/en-us/job/{JOB_A}/job-a"
URL_B = f"https://jobs.rowan.edu/en-us/job/{JOB_B}/job-b"


@pytest.fixture
def collector(
    cfg: Config,
    repo: Repository,
    run_environment: dict[str, Any],
    make_client: Callable[..., SourceClient],
) -> Callable[..., DetailCollector]:
    def make(source: FakeSource) -> DetailCollector:
        return DetailCollector(
            cfg=cfg,
            repo=repo,
            client=make_client(source),
            run_id=run_environment["run_id"],
        )

    return make


@pytest.fixture
def register(repo: Repository, run_environment: dict[str, Any]) -> Callable[..., int]:
    def make(job_id: str, url: str) -> int:
        posting_id, _created = repo.ensure_posting(
            source_id=run_environment["source_id"],
            external_job_id=job_id,
            run_id=run_environment["run_id"],
            discovery_basis="baseline",
            observed_at_utc="2026-09-16T10:00:00Z",
        )
        repo.record_posting_url(
            posting_id, url, "listing-link", "listing_entry", "2026-09-16T10:00:00Z"
        )
        return posting_id

    return make


def observation(db: Database, job_id: str = JOB_A) -> dict[str, Any]:
    return db.one(
        "SELECT * FROM posting_observations WHERE expected_external_job_id = ? "
        "ORDER BY observation_id DESC LIMIT 1",
        (job_id,),
    )


# ------------------------------------------------------------- happy capture


def test_capture_records_a_version_an_observation_and_the_canonical_url(
    collector, register, db: Database
) -> None:
    posting_id = register(JOB_A, URL_A)
    source = FakeSource()
    source.page(URL_A, build_detail_page(job_id=JOB_A, title="Telecommunicator"))

    outcome = collector(source).collect(
        external_job_id=JOB_A, url=URL_A, posting_id=posting_id, checked_because="listed"
    )

    assert outcome.availability_state == "content_captured"
    assert outcome.identity_state == "match"
    assert outcome.version_created is True
    row = observation(db)
    assert row["availability_state"] == "content_captured"
    assert row["observed_external_job_id"] == JOB_A
    assert row["checked_because"] == "listed"
    assert row["conflicts_json"] is None
    assert int(row["posting_version_id"]) == outcome.posting_version_id
    version = db.one(
        "SELECT * FROM posting_versions WHERE posting_version_id = ?", (outcome.posting_version_id,)
    )
    assert version["title"] == "Telecommunicator"
    assert version["description_html_kind"] == "source-substring"
    roles = {
        r["role"]: r["url"]
        for r in db.query("SELECT role, url FROM posting_urls WHERE posting_id = ?", (posting_id,))
    }
    assert roles["canonical-detail"] == URL_A


def test_version_values_keep_labels_states_and_date_precision(
    collector, register, db: Database
) -> None:
    posting_id = register(JOB_A, URL_A)
    source = FakeSource()
    source.page(
        URL_A,
        build_detail_page(
            job_id=JOB_A,
            location="Glassboro, New Jersey",
            categories="Engineering; Research Support",
            extra_fields=(("Some New Label:", "brand new value"),),
        ),
    )

    outcome = collector(source).collect(
        external_job_id=JOB_A, url=URL_A, posting_id=posting_id, checked_because="listed"
    )

    values = {
        r["field_key"]: r
        for r in db.query(
            "SELECT * FROM version_values WHERE posting_version_id = ?",
            (outcome.posting_version_id,),
        )
    }
    location = values["location"]
    assert location["value_text"] == "Glassboro, New Jersey"
    assert json.loads(location["normalized_json"])["values"] == ["Glassboro, New Jersey"]
    assert int(location["known_label"]) == 1
    assert location["field_state"] == "present"

    categories = values["categories"]
    assert categories["value_text"] == "Engineering; Research Support"
    assert json.loads(categories["normalized_json"])["values"] == [
        "Engineering",
        "Research Support",
    ]

    unknown = values["some_new_label"]
    assert int(unknown["known_label"]) == 0
    assert unknown["source_label"] == "Some New Label:"
    assert unknown["value_text"] == "brand new value"

    advertised = values["advertised"]
    assert advertised["source_precision"] == "date"
    assert advertised["date_parse_state"] == "parsed"
    assert advertised["source_tz_text"] == "Eastern Daylight Time"
    assert advertised["source_machine_value"] == "2026-09-15T12:00:00Z"

    closes = values["applications_close"]
    assert closes["source_precision"] == "minute"
    assert closes["parsed_utc"] == "2026-09-30T03:55:00Z"


# ------------------------------------------------------------ access control


def test_a_challenge_is_collection_uncertainty_and_never_a_missing_page(
    collector, register, db: Database
) -> None:
    posting_id = register(JOB_A, URL_A)
    source = FakeSource()
    source.add(URL_A, challenge_response())

    outcome = collector(source).collect(
        external_job_id=JOB_A, url=URL_A, posting_id=posting_id, checked_because="listed"
    )

    assert outcome.availability_state == "access_control_challenge"
    assert outcome.availability_state != "not_found"
    assert outcome.identity_state == "not_observed"
    assert outcome.uncertain is True
    row = observation(db)
    assert row["availability_state"] == "access_control_challenge"
    assert "challenge" in str(row["availability_detail"])
    assert row["posting_version_id"] is None
    fetch = db.one("SELECT * FROM fetches WHERE fetch_id = ?", (row["fetch_id"],))
    assert fetch["access_control_signal"] == "aws-waf-challenge"


def test_a_transport_failure_is_uncertainty_rather_than_absence(
    collector, register, db: Database
) -> None:
    posting_id = register(JOB_A, URL_A)
    source = FakeSource()
    source.add(URL_A, error_response(500))

    outcome = collector(source).collect(
        external_job_id=JOB_A, url=URL_A, posting_id=posting_id, checked_because="listed"
    )

    assert outcome.availability_state == "retrieval_failed"
    assert outcome.uncertain is True
    assert observation(db)["identity_state"] == "not_observed"


def test_a_definite_not_found_is_recorded_as_not_found(collector, register, db: Database) -> None:
    posting_id = register(JOB_A, URL_A)
    source = FakeSource()
    source.add(URL_A, error_response(404, "gone"))

    outcome = collector(source).collect(
        external_job_id=JOB_A, url=URL_A, posting_id=posting_id, checked_because="historical-daily"
    )

    assert outcome.availability_state == "not_found"
    assert outcome.uncertain is False
    assert observation(db)["checked_because"] == "historical-daily"


def test_a_closure_template_is_an_explicit_closure_not_a_capture(
    collector, register, db: Database
) -> None:
    """A real closure template replaces the advertisement rather than sitting beside it."""
    posting_id = register(JOB_A, URL_A)
    source = FakeSource()
    source.page(
        URL_A,
        build_detail_page(
            job_id=JOB_A,
            include_job_details=False,
            messages="<li>The job you are looking for is no longer available.</li>",
        ),
    )

    outcome = collector(source).collect(
        external_job_id=JOB_A, url=URL_A, posting_id=posting_id, checked_because="historical-daily"
    )

    assert outcome.availability_state == "explicit_closure"
    assert observation(db)["posting_version_id"] is None
    assert int(db.scalar("SELECT COUNT(*) FROM posting_versions")) == 0


# ----------------------------------------------------------------- redirects


def test_redirect_to_the_general_listing_is_not_a_successful_capture(
    collector, register, db: Database
) -> None:
    posting_id = register(JOB_A, URL_A)
    source = FakeSource()
    source.add(URL_A, redirect_response("/en-us/listing/"))
    source.page(LISTING_URL, build_listing_page([listing_job("999999")]))

    outcome = collector(source).collect(
        external_job_id=JOB_A, url=URL_A, posting_id=posting_id, checked_because="historical-daily"
    )

    assert outcome.availability_state == "redirected_to_listing"
    assert outcome.identity_state == "absent_on_page"
    row = observation(db)
    assert row["redirect_class"] == "to_listing"
    assert row["posting_version_id"] is None
    assert int(db.scalar("SELECT COUNT(*) FROM posting_versions")) == 0


def test_redirect_to_another_job_never_attaches_that_description_to_this_posting(
    collector, register, db: Database
) -> None:
    posting_a = register(JOB_A, URL_A)
    source = FakeSource()
    source.add(URL_A, redirect_response(URL_B))
    source.page(URL_B, build_detail_page(job_id=JOB_B, title="A completely different job"))

    outcome = collector(source).collect(
        external_job_id=JOB_A, url=URL_A, posting_id=posting_a, checked_because="listed"
    )

    assert outcome.availability_state == "redirected_to_other_job"
    assert outcome.identity_state == "mismatch"
    row = observation(db)
    assert row["redirect_class"] == "to_other_job"
    assert row["observed_external_job_id"] == JOB_B
    assert row["posting_version_id"] is None
    assert int(db.scalar("SELECT COUNT(*) FROM posting_versions")) == 0
    conflicts = json.loads(str(row["conflicts_json"]))
    assert conflicts[0]["kind"] == "job_id_mismatch"
    assert conflicts[0]["expected"] == JOB_A
    assert conflicts[0]["observed"] == JOB_B
    assert "NOT assigned to the expected posting" in conflicts[0]["note"]
    # The destination's title is nowhere in this posting's archived content.
    assert (
        int(db.scalar("SELECT COUNT(*) FROM posting_versions WHERE posting_id = ?", (posting_a,)))
        == 0
    )


def test_identity_mismatch_without_a_redirect_is_preserved_unresolved(
    collector, register, db: Database
) -> None:
    posting_id = register(JOB_A, URL_A)
    source = FakeSource()
    source.page(URL_A, build_detail_page(job_id=JOB_B))

    outcome = collector(source).collect(
        external_job_id=JOB_A, url=URL_A, posting_id=posting_id, checked_because="listed"
    )

    assert outcome.availability_state == "identity_mismatch"
    row = observation(db)
    assert row["redirect_class"] == "none"
    assert row["observed_external_job_id"] == JOB_B
    assert row["posting_version_id"] is None


def test_canonicalising_redirect_to_the_same_job_is_still_a_capture(
    collector, register, db: Database
) -> None:
    posting_id = register(JOB_A, URL_A)
    canonical = f"https://jobs.rowan.edu/en-us/job/{JOB_A}/canonical-slug"
    source = FakeSource()
    source.add(URL_A, redirect_response(canonical))
    source.page(canonical, build_detail_page(job_id=JOB_A))

    outcome = collector(source).collect(
        external_job_id=JOB_A, url=URL_A, posting_id=posting_id, checked_because="listed"
    )

    assert outcome.availability_state == "content_captured"
    row = observation(db)
    assert row["redirect_class"] == "same_job_canonicalised"
    urls = {
        r["url"]
        for r in db.query("SELECT url FROM posting_urls WHERE posting_id = ?", (posting_id,))
    }
    assert urls == {URL_A, canonical}


# ----------------------------------------------------------------- conflicts


def test_disagreeing_job_number_label_and_span_are_both_preserved(
    collector, register, db: Database
) -> None:
    posting_id = register(JOB_A, URL_A)
    # The labelled value reads "501826 999999" while the span says "501826".
    markup = build_detail_page(job_id=JOB_A).replace(
        f'<span class="job-externalJobNo">{JOB_A}</span>',
        f'<span class="job-externalJobNo">{JOB_A}</span> 999999',
    )
    source = FakeSource()
    source.page(URL_A, markup)

    outcome = collector(source).collect(
        external_job_id=JOB_A, url=URL_A, posting_id=posting_id, checked_because="listed"
    )

    assert outcome.availability_state == "content_captured"
    conflicts = json.loads(str(observation(db)["conflicts_json"]))
    kinds = {c["kind"] for c in conflicts}
    assert "job_no_label_vs_span" in kinds
    conflict = next(c for c in conflicts if c["kind"] == "job_no_label_vs_span")
    assert conflict["label_value"] == f"{JOB_A} 999999"
    assert conflict["span_value"] == JOB_A
    assert "no authoritative value invented" in conflict["note"]


def test_a_field_the_source_repeats_with_different_values_is_recorded_as_a_conflict(
    collector, register, db: Database
) -> None:
    posting_id = register(JOB_A, URL_A)
    source = FakeSource()
    source.page(
        URL_A,
        build_detail_page(
            job_id=JOB_A,
            location="Glassboro, New Jersey",
            extra_fields=(("Location:", '<span class="location">Camden, New Jersey</span>'),),
        ),
    )

    outcome = collector(source).collect(
        external_job_id=JOB_A, url=URL_A, posting_id=posting_id, checked_because="listed"
    )

    conflicts = json.loads(str(observation(db)["conflicts_json"]))
    conflict = next(c for c in conflicts if c["kind"] == "location_multiple_values")
    assert conflict["values"] == ["Glassboro, New Jersey", "Camden, New Jersey"]
    assert "all are preserved in order" in conflict["note"]
    # Both values survive, in order, with their own ordinals.
    stored = db.query(
        "SELECT ordinal, value_text FROM version_values WHERE posting_version_id = ? "
        "AND field_key = 'location' ORDER BY ordinal",
        (outcome.posting_version_id,),
    )
    assert [r["value_text"] for r in stored] == ["Glassboro, New Jersey", "Camden, New Jersey"]


# ------------------------------------------------------------------ versions


def test_unchanged_content_reuses_the_version_and_adds_an_observation(
    collector, register, db: Database
) -> None:
    posting_id = register(JOB_A, URL_A)
    source = FakeSource()
    source.page(URL_A, build_detail_page(job_id=JOB_A))

    first = collector(source).collect(
        external_job_id=JOB_A, url=URL_A, posting_id=posting_id, checked_because="listed"
    )
    second = collector(source).collect(
        external_job_id=JOB_A, url=URL_A, posting_id=posting_id, checked_because="listed"
    )

    assert first.version_created is True
    assert second.version_created is False
    assert second.posting_version_id == first.posting_version_id
    assert int(db.scalar("SELECT COUNT(*) FROM posting_versions")) == 1
    assert int(db.scalar("SELECT COUNT(*) FROM posting_observations")) == 2
    assert int(db.scalar("SELECT COUNT(*) FROM artifacts")) == 1


def test_changed_content_creates_a_second_version_and_keeps_the_first(
    collector, register, db: Database
) -> None:
    posting_id = register(JOB_A, URL_A)
    source = FakeSource()
    source.add(
        URL_A,
        html_response(build_detail_page(job_id=JOB_A, body_html="<p>Original body.</p>")),
        html_response(build_detail_page(job_id=JOB_A, body_html="<p>Edited body.</p>")),
    )

    first = collector(source).collect(
        external_job_id=JOB_A, url=URL_A, posting_id=posting_id, checked_because="listed"
    )
    second = collector(source).collect(
        external_job_id=JOB_A, url=URL_A, posting_id=posting_id, checked_because="listed"
    )

    assert second.posting_version_id != first.posting_version_id
    versions = db.query(
        "SELECT description_text, content_fingerprint FROM posting_versions "
        "WHERE posting_id = ? ORDER BY posting_version_id",
        (posting_id,),
    )
    assert len(versions) == 2
    assert versions[0]["description_text"] == "Original body."
    assert versions[1]["description_text"] == "Edited body."
    assert versions[0]["content_fingerprint"] != versions[1]["content_fingerprint"]


def test_a_metadata_only_edit_is_a_new_version_with_the_same_description(
    collector, register, db: Database
) -> None:
    posting_id = register(JOB_A, URL_A)
    source = FakeSource()
    source.add(
        URL_A,
        html_response(build_detail_page(job_id=JOB_A, work_type="Temporary Part-Time")),
        html_response(build_detail_page(job_id=JOB_A, work_type="Full-Time")),
    )

    first = collector(source).collect(
        external_job_id=JOB_A, url=URL_A, posting_id=posting_id, checked_because="listed"
    )
    second = collector(source).collect(
        external_job_id=JOB_A, url=URL_A, posting_id=posting_id, checked_because="listed"
    )

    rows = db.query(
        "SELECT description_text_fingerprint, metadata_fingerprint FROM posting_versions "
        "WHERE posting_id = ? ORDER BY posting_version_id",
        (posting_id,),
    )
    assert first.posting_version_id != second.posting_version_id
    assert rows[0]["description_text_fingerprint"] == rows[1]["description_text_fingerprint"]
    assert rows[0]["metadata_fingerprint"] != rows[1]["metadata_fingerprint"]


def test_an_unregistered_posting_is_captured_without_inventing_an_identity(
    collector, db: Database
) -> None:
    source = FakeSource()
    source.page(URL_A, build_detail_page(job_id=JOB_A))

    outcome = collector(source).collect(
        external_job_id=JOB_A, url=URL_A, posting_id=None, checked_because="manual"
    )

    assert outcome.availability_state == "content_captured"
    assert outcome.posting_version_id is None
    row = observation(db)
    assert row["posting_id"] is None
    assert row["availability_detail"] == "posting identity not yet registered"
    assert int(db.scalar("SELECT COUNT(*) FROM posting_versions")) == 0


# ----------------------------------------------------------------- resources


def test_a_job_document_is_fetched_once_and_linked_to_the_observation(
    collector, register, db: Database
) -> None:
    posting_id = register(JOB_A, URL_A)
    pdf_url = "https://jobs.rowan.edu/documents/jd.pdf"
    source = FakeSource()
    source.page(
        URL_A,
        build_detail_page(
            job_id=JOB_A, body_html='<p><a href="/documents/jd.pdf">Position description</a></p>'
        ),
    )
    source.add(pdf_url, bytes_response(b"%PDF-1.7 payload"))

    outcome = collector(source).collect(
        external_job_id=JOB_A, url=URL_A, posting_id=posting_id, checked_because="listed"
    )

    assert source.count(pdf_url) == 1
    resource = db.one("SELECT * FROM resource_observations WHERE url_resolved = ?", (pdf_url,))
    assert resource["outcome"] == "captured"
    assert resource["media_type"] == "application/pdf"
    assert resource["content_sha256"]
    assert resource["text_extraction_state"] == "unavailable"
    associations = db.query(
        "SELECT * FROM resource_associations WHERE observation_id = ?", (outcome.observation_id,)
    )
    assert len(associations) >= 1
    # The description link and the job-content link share one retrieval.
    assert [r["outcome"] for r in outcome.resources].count("reused_within_run") == 1


def test_a_resource_over_the_size_limit_is_a_partial_capture_never_a_complete_one(
    collector, register, cfg: Config, db: Database
) -> None:
    cfg.network.max_resource_bytes = 64
    posting_id = register(JOB_A, URL_A)
    pdf_url = "https://jobs.rowan.edu/documents/huge.pdf"
    source = FakeSource()
    source.page(
        URL_A,
        build_detail_page(job_id=JOB_A, body_html='<p><a href="/documents/huge.pdf">Big</a></p>'),
    )
    source.add(pdf_url, bytes_response(b"%PDF-1.7" + b"A" * 4000))

    collector(source).collect(
        external_job_id=JOB_A, url=URL_A, posting_id=posting_id, checked_because="listed"
    )

    resource = db.one("SELECT * FROM resource_observations WHERE url_resolved = ?", (pdf_url,))
    assert resource["outcome"] == "too_large"
    assert "exceeded" in str(resource["outcome_detail"])
    assert int(resource["byte_length"]) == 64
    artifact = db.one("SELECT * FROM artifacts WHERE artifact_id = ?", (resource["artifact_id"],))
    assert artifact["capture_state"] == "partial"
    assert artifact["capture_exception"] is not None
    assert artifact["capture_state"] != "complete"
    fetch = db.one("SELECT * FROM fetches WHERE fetch_id = ?", (resource["fetch_id"],))
    assert fetch["response_state"] == "partial"
    assert (
        int(db.scalar("SELECT COUNT(*) FROM resource_observations WHERE outcome='captured'")) == 0
    )


def test_a_failed_resource_retrieval_is_recorded_with_its_reason(
    collector, register, db: Database
) -> None:
    posting_id = register(JOB_A, URL_A)
    pdf_url = "https://jobs.rowan.edu/documents/missing.pdf"
    source = FakeSource()
    source.page(
        URL_A,
        build_detail_page(
            job_id=JOB_A, body_html='<p><a href="/documents/missing.pdf">Gone</a></p>'
        ),
    )
    source.add(pdf_url, error_response(404, "no such document"))

    collector(source).collect(
        external_job_id=JOB_A, url=URL_A, posting_id=posting_id, checked_because="listed"
    )

    resource = db.one("SELECT * FROM resource_observations WHERE url_resolved = ?", (pdf_url,))
    assert resource["outcome"] == "failed"
    assert "404" in str(resource["outcome_detail"])


def test_links_the_policy_excludes_are_never_retrieved(collector, register, db: Database) -> None:
    posting_id = register(JOB_A, URL_A)
    source = FakeSource()
    source.page(
        URL_A,
        build_detail_page(
            job_id=JOB_A,
            body_html=(
                '<p><a class="apply-link" href="https://secure.dc4.pageuppeople.com/apply/860/x">'
                "Apply</a> <a href='https://www.rowan.edu/hr/'>HR</a></p>"
            ),
        ),
    )

    collector(source).collect(
        external_job_id=JOB_A, url=URL_A, posting_id=posting_id, checked_because="listed"
    )

    assert source.urls == [URL_A]
    assert int(db.scalar("SELECT COUNT(*) FROM resource_observations")) == 0
    decisions = {
        r["classification"]: r["collection_decision"]
        for r in db.query("SELECT classification, collection_decision FROM resource_links")
    }
    assert decisions["apply_workflow"] == "exclude"
    assert decisions["external"] == "exclude"


def test_resource_capture_can_be_switched_off_for_a_run(
    collector, register, cfg: Config, db: Database
) -> None:
    cfg.collection.collect_resources = False
    posting_id = register(JOB_A, URL_A)
    source = FakeSource()
    source.page(
        URL_A,
        build_detail_page(job_id=JOB_A, body_html='<p><a href="/documents/jd.pdf">JD</a></p>'),
    )

    outcome = collector(source).collect(
        external_job_id=JOB_A, url=URL_A, posting_id=posting_id, checked_because="listed"
    )

    assert outcome.resources == []
    assert source.urls == [URL_A]
    # The link is still classified and recorded as fetchable evidence.
    assert (
        int(db.scalar("SELECT COUNT(*) FROM resource_links WHERE collection_decision = 'fetch'"))
        >= 1
    )


def test_a_document_edited_at_the_same_url_is_captured_twice_with_its_own_times(
    collector, register, db: Database, advancing_clock
) -> None:
    posting_id = register(JOB_A, URL_A)
    pdf_url = "https://jobs.rowan.edu/documents/jd.pdf"
    detail_markup = build_detail_page(
        job_id=JOB_A, body_html='<p><a href="/documents/jd.pdf">Position description</a></p>'
    )
    source = FakeSource()
    source.page(URL_A, detail_markup)
    source.add(
        pdf_url,
        bytes_response(b"%PDF-1.7 first revision"),
        bytes_response(b"%PDF-1.7 SECOND revision, materially different"),
    )

    for _run in range(2):
        collector(source).collect(
            external_job_id=JOB_A, url=URL_A, posting_id=posting_id, checked_because="listed"
        )

    resources = db.query(
        "SELECT * FROM resource_observations WHERE url_resolved = ? ORDER BY resource_observation_id",
        (pdf_url,),
    )
    assert len(resources) == 2
    assert resources[0]["content_sha256"] != resources[1]["content_sha256"]
    assert resources[0]["observed_at_utc"] != resources[1]["observed_at_utc"]
    assert all(r["outcome"] == "captured" for r in resources)
    # Each retrieval carries its own time, taken from its own fetch.
    for row in resources:
        fetch = db.one("SELECT * FROM fetches WHERE fetch_id = ?", (row["fetch_id"],))
        assert row["observed_at_utc"] == fetch["started_at_utc"]
    # Both revisions are archived; the unchanged parent page is stored once.
    assert int(db.scalar("SELECT COUNT(DISTINCT artifact_id) FROM resource_observations")) == 2
    assert int(db.scalar("SELECT COUNT(*) FROM posting_versions")) == 1
    parent_artifacts = db.query(
        "SELECT DISTINCT artifact_id FROM fetches WHERE purpose = 'posting_detail'"
    )
    assert len(parent_artifacts) == 1
    assert int(db.scalar("SELECT COUNT(*) FROM posting_observations")) == 2


def test_a_closure_notice_alongside_a_body_captures_the_content_as_a_conflict(
    collector, register, db: Database
) -> None:
    """A page cannot be both closed and a complete advertisement.

    Treating the notice as authoritative would discard the description that was
    right there on the page, and assert a closure the employer did not display.
    """
    posting_id = register(JOB_A, URL_A)
    source = FakeSource().page(
        URL_A,
        build_detail_page(
            job_id=JOB_A,
            messages="<li>This job is no longer available.</li>",
            body_html="<p>A complete advertisement body that must survive.</p>",
        ),
    )

    outcome = collector(source).collect(
        external_job_id=JOB_A, url=URL_A, posting_id=posting_id, checked_because="listed"
    )

    assert outcome.availability_state == "content_captured"
    assert outcome.posting_version_id is not None

    row = db.one(
        "SELECT conflicts_json FROM posting_observations WHERE observation_id = ?",
        (outcome.observation_id,),
    )
    conflicts = json.loads(str(row["conflicts_json"]))
    assert any(c["kind"] == "closure_signal_with_content" for c in conflicts)

    version = db.one(
        "SELECT description_text FROM posting_versions WHERE posting_version_id = ?",
        (outcome.posting_version_id,),
    )
    assert "must survive" in str(version["description_text"])


def test_widening_the_document_scope_takes_effect_without_a_content_change(
    cfg: Config, repo: Repository, run_environment, make_client, register, db: Database
) -> None:
    """Stored classifications must never disagree with what was retrieved.

    The same unchanged advertisement is observed twice; between the two, the
    adapter is upgraded so the document's host counts as in scope. The second
    observation records the new decision *and* retrieves the file -- it is not
    frozen by what the adapter decided the first time this version was seen --
    while the advertisement itself is not reported as having changed.
    """
    import rowanjobs
    from rowanjobs.extract import pageup_detail

    document = "https://engineering.rowan.edu/_docs/flow.pdf"
    page = build_detail_page(
        job_id=JOB_A, body_html=f'<p>See <a href="{document}">the chart</a>.</p>'
    )
    posting_id = register(JOB_A, URL_A)
    original = pageup_detail.classify_link

    def narrow(url: str, anchor: Any) -> tuple[str, str, str | None]:
        if url == document:
            return "job_document", "exclude", "hosted off the career site"
        return original(url, anchor)

    pageup_detail.classify_link = narrow  # type: ignore[assignment]
    try:
        first = DetailCollector(
            cfg=cfg,
            repo=repo,
            client=make_client(FakeSource().page(URL_A, page)),
            run_id=run_environment["run_id"],
        ).collect(external_job_id=JOB_A, url=URL_A, posting_id=posting_id, checked_because="listed")
    finally:
        pageup_detail.classify_link = original  # type: ignore[assignment]

    assert first.resources == []
    assert {
        r["collection_decision"]
        for r in db.query(
            "SELECT collection_decision FROM resource_links WHERE url_resolved = ?", (document,)
        )
    } == {"exclude"}

    # The upgraded adapter is a new parser version, which is what gives the new
    # reading its own extraction and its own classification rows.
    previous_parser = rowanjobs.PARSER_VERSION
    for module in (
        rowanjobs,
        pageup_detail,
        repo.__module__ and __import__("rowanjobs.collect.repo", fromlist=["x"]),
    ):
        module.PARSER_VERSION = "9.9.9"  # type: ignore[attr-defined]
    try:
        source = FakeSource().page(URL_A, page)
        source.add(document, bytes_response(b"%PDF-1.4 fake"))
        second = DetailCollector(
            cfg=cfg,
            repo=repo,
            client=make_client(
                source,
                policy=UrlPolicy(
                    ("jobs.rowan.edu", "rowan.edu"), allow_subdomains=True, resolve=False
                ),
            ),
            run_id=run_environment["run_id"],
        ).collect(external_job_id=JOB_A, url=URL_A, posting_id=posting_id, checked_because="listed")
    finally:
        for module in (
            rowanjobs,
            pageup_detail,
            __import__("rowanjobs.collect.repo", fromlist=["x"]),
        ):
            module.PARSER_VERSION = previous_parser  # type: ignore[attr-defined]

    assert {r["outcome"] for r in second.resources} == {"captured", "reused_within_run"}
    assert "fetch" in {
        r["collection_decision"]
        for r in db.query(
            "SELECT collection_decision FROM resource_links WHERE url_resolved = ?", (document,)
        )
    }, "the new decision must be recorded, not only acted on"
    assert (
        db.scalar("SELECT COUNT(*) FROM presence_events WHERE event_kind = 'content_changed'") == 0
    ), "our own reading changed; the advertisement did not"
