"""Offline reprocessing.

Re-running the parsers over archived payloads must never look like a website
edit and must never touch the network: new extraction rows and, when the
comparison contract changes, a parallel line of content versions -- with the
original observation times left exactly as they were.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

import rowanjobs
from rowanjobs.config import Config
from rowanjobs.db import Database
from rowanjobs.reprocess import reprocess_details, reprocess_listings

from .conftest import LISTING_URL, FakeSource, build_detail_page, build_listing_page, listing_job

DETAIL_URL = "https://jobs.rowan.edu/en-us/job/1001/job-1001"

# The shipped versions, read rather than hard-coded: bumping one is a normal
# event and must not require editing assertions.
BASELINE_PARSER = rowanjobs.PARSER_VERSION
BASELINE_CONTRACT = rowanjobs.CONTRACT_VERSION

# Modules import the version constants by value, so each reference has to be
# repointed for the test to simulate a genuine parser/contract upgrade.
VERSION_ATTRIBUTES = (
    ("rowanjobs", "PARSER_VERSION"),
    ("rowanjobs", "CONTRACT_VERSION"),
    ("rowanjobs", "TEXT_CONTRACT_VERSION"),
    ("rowanjobs.reprocess", "PARSER_VERSION"),
    ("rowanjobs.reprocess", "CONTRACT_VERSION"),
    ("rowanjobs.reprocess", "TEXT_CONTRACT_VERSION"),
    ("rowanjobs.collect.repo", "PARSER_VERSION"),
    ("rowanjobs.collect.repo", "CONTRACT_VERSION"),
    ("rowanjobs.collect.repo", "TEXT_CONTRACT_VERSION"),
    ("rowanjobs.collect.events", "PARSER_VERSION"),
    ("rowanjobs.collect.events", "CONTRACT_VERSION"),
    ("rowanjobs.collect.events", "TEXT_CONTRACT_VERSION"),
    # fingerprint.py reads the constants from the rowanjobs package at call
    # time, so patching the package attributes above is enough for it.
    ("rowanjobs.extract.pageup_detail", "PARSER_VERSION"),
    ("rowanjobs.extract.pageup_listing", "PARSER_VERSION"),
)


@pytest.fixture
def archive(collect, db: Database) -> FakeSource:
    source = FakeSource()
    source.page(
        LISTING_URL, build_listing_page([listing_job("1001", "Archivist", slug="job-1001")])
    )
    source.page(DETAIL_URL, build_detail_page(job_id="1001", title="Archivist"))
    assert collect(source).outcome == "success"
    return source


def upgrade_parser(
    monkeypatch: pytest.MonkeyPatch, version: str = "2.0.0", *, only: str | None = None
) -> None:
    """Simulate a genuine upgrade of one or all of the version constants.

    ``only`` restricts the bump to a single constant, which is how a real
    upgrade usually happens: changing markup-to-text rules bumps
    ``TEXT_CONTRACT_VERSION`` alone, and that must still not look like a
    website edit.
    """
    for module, attribute in VERSION_ATTRIBUTES:
        if only is None or attribute == only:
            monkeypatch.setattr(f"{module}.{attribute}", version)


def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("reprocessing must not make any network request")

    monkeypatch.setattr(httpx.Client, "send", explode)


def snapshot_observations(db: Database) -> list[tuple[Any, ...]]:
    return [
        (r["observation_id"], r["observed_at_utc"], r["availability_state"], r["run_id"])
        for r in db.query("SELECT * FROM posting_observations ORDER BY observation_id")
    ]


def test_a_parser_upgrade_reinterprets_archived_bytes_without_any_network_access(
    archive: FakeSource, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    before_observations = snapshot_observations(db)
    before_artifacts = int(db.scalar("SELECT COUNT(*) FROM artifacts"))
    before_fetches = int(db.scalar("SELECT COUNT(*) FROM fetches"))
    requests_before = len(archive.requests)
    upgrade_parser(monkeypatch)
    forbid_network(monkeypatch)

    report = reprocess_details(db)

    assert report.artifacts_considered == 1
    assert report.extractions_created == 1
    assert report.extractions_reused == 0
    assert report.versions_created == 1
    assert report.failures == []
    assert len(archive.requests) == requests_before
    assert int(db.scalar("SELECT COUNT(*) FROM fetches")) == before_fetches
    # Observation evidence is untouched: same rows, same times.
    assert snapshot_observations(db) == [
        (row[0], row[1], row[2], row[3]) for row in before_observations
    ]
    assert int(db.scalar("SELECT COUNT(*) FROM artifacts")) == before_artifacts
    assert report.as_dict()["note"] == (
        "no network requests were made; observation times are unchanged"
    )


def test_a_parser_upgrade_creates_no_content_changed_events(
    archive: FakeSource, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = int(
        db.scalar("SELECT COUNT(*) FROM presence_events WHERE event_kind = 'content_changed'")
    )
    all_events_before = int(db.scalar("SELECT COUNT(*) FROM presence_events"))
    upgrade_parser(monkeypatch)
    forbid_network(monkeypatch)

    reprocess_details(db)

    assert (
        int(db.scalar("SELECT COUNT(*) FROM presence_events WHERE event_kind = 'content_changed'"))
        == before
        == 0
    )
    assert int(db.scalar("SELECT COUNT(*) FROM presence_events")) == all_events_before


def test_the_old_interpretation_is_kept_alongside_the_new_one(
    archive: FakeSource, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    upgrade_parser(monkeypatch)
    forbid_network(monkeypatch)

    reprocess_details(db)

    extractions = db.query(
        "SELECT parser_version, contract_version FROM extractions "
        "WHERE parser_name = 'pageup_detail' ORDER BY extraction_id"
    )
    assert [e["parser_version"] for e in extractions] == [BASELINE_PARSER, "2.0.0"]
    versions = db.query(
        "SELECT contract_version, description_text, first_run_id FROM posting_versions "
        "ORDER BY posting_version_id"
    )
    assert [v["contract_version"] for v in versions] == [BASELINE_CONTRACT, "2.0.0"]
    assert versions[0]["description_text"] == versions[1]["description_text"]
    # The reinterpretation belongs to no run: it observed nothing.
    assert versions[0]["first_run_id"] is not None
    assert versions[1]["first_run_id"] is None


def test_observations_keep_their_original_interpretation_by_default(
    archive: FakeSource, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Relinking mutates an evidence row, so it is opt-in.

    An observation records what we understood at the time it was made. Rewriting
    that pointer by default would quietly erase which interpretation produced a
    historical reading.
    """
    original = db.one("SELECT * FROM posting_observations LIMIT 1")
    upgrade_parser(monkeypatch)
    forbid_network(monkeypatch)

    report = reprocess_details(db)

    assert report.observations_relinked == 0
    assert report.versions_created == 1, "the new reading is still recorded"
    untouched = db.one(
        "SELECT * FROM posting_observations WHERE observation_id = ?",
        (original["observation_id"],),
    )
    assert untouched["posting_version_id"] == original["posting_version_id"]
    assert untouched["extraction_id"] == original["extraction_id"]


def test_observations_can_be_relinked_to_the_current_contract_on_request(
    archive: FakeSource, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = db.one("SELECT * FROM posting_observations LIMIT 1")
    upgrade_parser(monkeypatch)
    forbid_network(monkeypatch)

    report = reprocess_details(db, relink=True)

    assert report.observations_relinked == 1
    relinked = db.one(
        "SELECT * FROM posting_observations WHERE observation_id = ?",
        (original["observation_id"],),
    )
    assert relinked["observed_at_utc"] == original["observed_at_utc"]
    assert relinked["posting_version_id"] != original["posting_version_id"]
    assert relinked["availability_state"] == original["availability_state"]
    contract = db.scalar(
        "SELECT contract_version FROM posting_versions WHERE posting_version_id = ?",
        (relinked["posting_version_id"],),
    )
    assert contract == "2.0.0"


def test_relinking_is_explicitly_declinable(
    archive: FakeSource, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = db.one("SELECT * FROM posting_observations LIMIT 1")
    upgrade_parser(monkeypatch)
    forbid_network(monkeypatch)

    report = reprocess_details(db, relink=False)

    assert report.versions_created == 1
    assert report.observations_relinked == 0
    unchanged = db.one(
        "SELECT * FROM posting_observations WHERE observation_id = ?",
        (original["observation_id"],),
    )
    assert unchanged["posting_version_id"] == original["posting_version_id"]


def test_reprocessing_without_a_version_change_reuses_everything(
    archive: FakeSource, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = db.query("SELECT * FROM posting_versions")
    forbid_network(monkeypatch)

    report = reprocess_details(db)

    assert report.extractions_created == 0
    assert report.extractions_reused == 1
    assert report.versions_created == 0
    assert report.observations_relinked == 0
    assert db.query("SELECT * FROM posting_versions") == before


def test_reprocessing_can_be_limited_to_one_advertisement(
    collect, db: Database, monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    source = FakeSource()
    source.page(
        LISTING_URL,
        build_listing_page(
            [
                listing_job("1001", "One", slug="job-1001"),
                listing_job("1002", "Two", slug="job-1002"),
            ]
        ),
    )
    for job_id, title in (("1001", "One"), ("1002", "Two")):
        source.page(
            f"https://jobs.rowan.edu/en-us/job/{job_id}/job-{job_id}",
            build_detail_page(job_id=job_id, title=title),
        )
    collect(source)
    upgrade_parser(monkeypatch)
    forbid_network(monkeypatch)

    report = reprocess_details(db, job_id="1002")

    assert report.artifacts_considered == 1
    assert report.versions_created == 1
    reinterpreted = db.query(
        "SELECT p.external_job_id FROM posting_versions v JOIN postings p "
        "ON p.posting_id = v.posting_id WHERE v.contract_version = '2.0.0'"
    )
    assert [r["external_job_id"] for r in reinterpreted] == ["1002"]


def test_listing_payloads_can_be_reinterpreted_offline_too(
    archive: FakeSource, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    upgrade_parser(monkeypatch)
    forbid_network(monkeypatch)

    report = reprocess_listings(db)

    assert report.parser == "pageup_listing"
    assert report.artifacts_considered >= 1
    assert report.extractions_created >= 1
    assert report.failures == []
    parsers = db.query(
        "SELECT DISTINCT parser_version FROM extractions WHERE parser_name = 'pageup_listing'"
    )
    assert {p["parser_version"] for p in parsers} == {BASELINE_PARSER, "2.0.0"}
    # Listing reprocessing never invents listing entries or scans.
    assert int(db.scalar("SELECT COUNT(*) FROM listing_scans")) == 2


def test_an_unreadable_payload_is_reported_rather_than_skipped_silently(
    archive: FakeSource, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    upgrade_parser(monkeypatch)
    forbid_network(monkeypatch)
    artifact_id = int(
        db.scalar(
            "SELECT f.artifact_id FROM posting_observations o JOIN fetches f "
            "ON f.fetch_id = o.fetch_id LIMIT 1"
        )
    )
    with db.write():
        db.execute(
            "UPDATE artifacts SET blob = ? WHERE artifact_id = ?", (b"not zlib", artifact_id)
        )

    report = reprocess_details(db)

    assert report.versions_created == 0
    assert [f["artifact_id"] for f in report.failures] == [artifact_id]


@pytest.mark.parametrize(
    "constant", ["TEXT_CONTRACT_VERSION", "PARSER_VERSION", "CONTRACT_VERSION"]
)
def test_bumping_one_version_constant_is_never_recorded_as_a_website_edit(
    archive: FakeSource, collect, db: Database, monkeypatch: pytest.MonkeyPatch, constant: str
) -> None:
    """A changed reading starts a parallel line of versions, not a content change.

    The page bytes are identical across both collections; only our own
    interpretation changed. Recording that as ``content_changed`` would put a
    fabricated edit into the archive's history of a real advertisement.
    """
    before = db.scalar("SELECT COUNT(*) FROM presence_events WHERE event_kind='content_changed'")

    upgrade_parser(monkeypatch, only=constant)
    assert collect(archive).outcome == "success"

    after = db.scalar("SELECT COUNT(*) FROM presence_events WHERE event_kind='content_changed'")
    assert after == before == 0

    lineages = db.query(
        "SELECT DISTINCT parser_version, contract_version, text_contract_version "
        "FROM posting_versions ORDER BY posting_version_id"
    )
    assert len(lineages) == 2, "the upgraded reading must be its own lineage"


def test_versions_record_the_parser_that_produced_them(archive: FakeSource, db: Database) -> None:
    rows = db.query(
        "SELECT parser_version, contract_version, text_contract_version FROM posting_versions"
    )
    assert rows
    for row in rows:
        assert row["parser_version"], "a version with no parser cannot be scoped for comparison"
        assert row["contract_version"]
        assert row["text_contract_version"]
