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
    posting_id = register(JOB_A, URL_A)
    source = FakeSource()
    source.page(
        URL_A,
        build_detail_page(
            job_id=JOB_A, messages="<li>The job you are looking for is no longer available.</li>"
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
        int(
            db.scalar(
                "SELECT COUNT(*) FROM posting_versions WHERE posting_id = ?", (posting_a,)
            )
        )
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
    source = FakeSource()
    source.page(URL_A, build_detail_page(job_id=JOB_A, job_no_span=JOB_A))
    # The labelled value says one thing, the span another.
    source._routes[URL_A] = [
        html_response(
            build_detail_page(job_id=JOB_A).replace(
                f'<span class="job-externalJobNo">{JOB_A}</span>',
                f'<span class="job-externalJobNo">{JOB_A}</span> 999999',
            )
        )
    ]

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
    artifact = db.one(
        "SELECT * FROM artifacts WHERE artifact_id = ?", (resource["artifact_id"],)
    )
    assert artifact["capture_state"] == "partial"
    assert artifact["capture_exception"] is not None
    assert artifact["capture_state"] != "complete"
    fetch = db.one("SELECT * FROM fetches WHERE fetch_id = ?", (resource["fetch_id"],))
    assert fetch["response_state"] == "partial"
    assert int(db.scalar("SELECT COUNT(*) FROM resource_observations WHERE outcome='captured'")) == 0


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
    assert int(
        db.scalar("SELECT COUNT(*) FROM resource_links WHERE collection_decision = 'fetch'")
    ) >= 1
