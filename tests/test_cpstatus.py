"""The ControlPanel status contract, and the startup failures it surfaces.

Two things are being pinned here.

First, that asking how things are going never changes how they are going. A
console polls this once a minute; if it could collect, migrate, send mail or
write to the archive, monitoring would be a source of the very incidents it is
meant to reveal.

Second, that the document tells the truth about the gap that made 2026-09-18
invisible. A unit that dies before opening a run row leaves the archive with
nothing to report, and every collection-derived number looks fine. The
``startup-integrity`` component is the only thing standing between that and a
console showing a confident green light over a day that never collected.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from rowanjobs.cli import EXIT_OK, main
from rowanjobs.config import Config
from rowanjobs.db import Database
from rowanjobs.ops import failures as failures_mod
from rowanjobs.ops.cpstatus import build_status

from .conftest import (
    LISTING_URL,
    FakeSource,
    build_detail_page,
    build_listing_page,
    listing_job,
)

DETAIL_URL = "https://jobs.rowan.edu/en-us/job/1001/job-1001"


@pytest.fixture
def cli_source(monkeypatch: pytest.MonkeyPatch, make_client) -> FakeSource:
    """Give the CLI's own collector a mock transport instead of the network."""
    from rowanjobs.collect.runner import Collector

    fake = FakeSource()
    fake.page(LISTING_URL, build_listing_page([listing_job("1001", slug="job-1001")]))
    fake.page(DETAIL_URL, build_detail_page(job_id="1001"))
    monkeypatch.setattr(Collector, "_make_client", lambda _self: make_client(fake))
    return fake


@pytest.fixture
def one_job_source() -> FakeSource:
    source = FakeSource()
    source.page(LISTING_URL, build_listing_page([listing_job("1001", slug="job-1001")]))
    source.page(DETAIL_URL, build_detail_page(job_id="1001"))
    return source


def write_failure(cfg: Config, *, slot: str, unit: str = "rowanjobs.service", **extra: Any) -> None:
    """Simulate what ops/onfailure.py records, without invoking systemd."""
    path = cfg.layout.runtime_dir / failures_mod.RECORD_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": "rowanjobs.startup-failure.v1",
        "kind": "startup_failure",
        "unit": unit,
        "slot_local_date": slot,
        "failed_at_utc": f"{slot}T10:15:29Z",
        "failed_at_local": f"{slot} 06:15:29 EDT",
        "result": "exit-code",
        "exit_status": "5",
        "config_error": None,
        **extra,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def status(cfg: Config, db: Database) -> dict[str, Any]:
    return build_status(cfg, db, include_timer=False)


def component(document: dict[str, Any], component_id: str) -> dict[str, Any]:
    found = [c for c in document["components"] if c["id"] == component_id]
    assert found, f"no {component_id} component in {[c['id'] for c in document['components']]}"
    return found[0]


# ------------------------------------------------------------- the contract


def test_the_document_matches_the_shape_controlpanel_expects(
    cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    collect(one_job_source, run_kind="daily")

    document = status(cfg, db)

    assert document["schema_version"] == "controlpanel.status.v1"
    assert document["project"] == "rowanjobs"
    assert set(document["overall"]) == {"health", "summary"}
    assert document["overall"]["health"] in ("healthy", "degraded", "failed", "running", "unknown")
    for item in document["components"]:
        assert {"id", "name", "health", "summary", "actions", "evidence"} <= set(item)
        assert item["health"] in ("healthy", "degraded", "failed", "running", "unknown")
        for value in item["evidence"]:
            assert set(value) == {"label", "value"}
            assert isinstance(value["value"], str)
    assert document["recent_runs"], "a collected run should be reported"
    assert document["errors"] == []


def test_every_number_a_console_shows_is_present_and_labelled(
    cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    collect(one_job_source, run_kind="daily")

    metrics = status(cfg, db)["metrics"]

    # Lifetime totals are named as lifetime, so nobody reads them as "today".
    assert metrics["advertisements_listed"] == 1
    assert metrics["advertisements_tracked_lifetime"] == 1
    assert metrics["descriptions_archived_lifetime"] == 1
    assert metrics["content_versions_lifetime"] >= 1
    assert metrics["runs_lifetime"] == 1
    assert metrics["missed_days"] == 0
    assert metrics["unresolved_startup_failures"] == 0


def test_asking_for_health_never_collects_migrates_or_sends(
    cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    collect(one_job_source, run_kind="daily")
    before = {
        "runs": db.scalar("SELECT COUNT(*) FROM collection_runs"),
        "observations": db.scalar("SELECT COUNT(*) FROM posting_observations"),
        "fetches": db.scalar("SELECT COUNT(*) FROM fetches"),
        "notifications": db.scalar("SELECT COUNT(*) FROM notifications"),
    }
    requested = len(one_job_source.requests)

    for _ in range(3):
        status(cfg, db)

    assert len(one_job_source.requests) == requested, "health contacted the source"
    assert {
        "runs": db.scalar("SELECT COUNT(*) FROM collection_runs"),
        "observations": db.scalar("SELECT COUNT(*) FROM posting_observations"),
        "fetches": db.scalar("SELECT COUNT(*) FROM fetches"),
        "notifications": db.scalar("SELECT COUNT(*) FROM notifications"),
    } == before


def test_the_cli_opens_the_archive_read_only(
    cfg: Config, cli_source: FakeSource, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--data-root", str(cfg.data_root), "collect", "--kind", "daily"]) == EXIT_OK
    capsys.readouterr()

    code = main(["--data-root", str(cfg.data_root), "health", "--no-timer"])

    document = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    assert document["schema_version"] == "controlpanel.status.v1"
    # --health is the same thing spelled the way an operator reaches for it.
    assert main(["--data-root", str(cfg.data_root), "--health", "--no-timer"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["project"] == "rowanjobs"


def test_evidence_timestamps_are_when_it_was_recorded_not_when_it_was_asked_for(
    cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    result = collect(one_job_source, run_kind="daily")
    ended = db.one("SELECT ended_at_utc FROM collection_runs WHERE run_id = ?", (result.run_id,))[
        "ended_at_utc"
    ]

    first = component(status(cfg, db), "daily-collection")["observed_at"]
    second = component(status(cfg, db), "daily-collection")["observed_at"]

    assert first == second == ended, "health refreshed a timestamp it had not re-observed"


# ------------------------------------------------------- startup integrity


def test_an_activation_that_never_recorded_a_run_is_reported_as_failed(
    cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    """The 2026-09-18 gap: healthy-looking collection data, a day with nothing."""
    collect(one_job_source, run_kind="daily")
    write_failure(cfg, slot="2026-09-18", config_error="unknown configuration key")

    document = status(cfg, db)
    integrity = component(document, "startup-integrity")

    assert integrity["health"] == "failed"
    assert "2026-09-18" in integrity["summary"]
    assert document["overall"]["health"] == "failed"
    assert "2026-09-18" in document["overall"]["summary"]
    assert document["metrics"]["unresolved_startup_failures"] == 1


def test_a_startup_failure_answered_by_a_later_collection_stops_counting(
    cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    result = collect(one_job_source, run_kind="daily")
    slot = db.one(
        "SELECT scheduled_slot_local_date AS slot FROM collection_runs WHERE run_id = ?",
        (result.run_id,),
    )["slot"]
    # The morning activation died; a later run covered the same slot.
    write_failure(cfg, slot=str(slot), config_error="unknown configuration key")

    document = status(cfg, db)
    integrity = component(document, "startup-integrity")

    assert integrity["health"] == "healthy"
    assert "answered by a later collection" in integrity["summary"]
    # Resolved does not mean erased: the record is still on disk.
    assert failures_mod.assess(cfg.layout, db)["recorded"] == 1
    assert failures_mod.assess(cfg.layout, db)["resolved"] == 1


def test_repeated_failures_in_one_day_are_one_unresolved_day(
    cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    collect(one_job_source, run_kind="daily")
    write_failure(cfg, slot="2026-09-18", unit="rowanjobs.service")
    write_failure(cfg, slot="2026-09-18", unit="rowanjobs-retry.service")

    assessment = failures_mod.assess(cfg.layout, db)

    assert assessment["unresolved_slots"] == ["2026-09-18"]
    assert len(assessment["unresolved"]) == 2


def test_a_startup_failure_never_implies_a_source_observation(
    cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    collect(one_job_source, run_kind="daily")
    before = db.scalar("SELECT COUNT(*) FROM posting_observations")
    write_failure(cfg, slot="2026-09-18")

    assessment = failures_mod.assess(cfg.layout, db)

    assert "no source observation occurred" in assessment["note"]
    assert db.scalar("SELECT COUNT(*) FROM posting_observations") == before
    assert (
        db.scalar(
            "SELECT COUNT(*) FROM collection_runs WHERE scheduled_slot_local_date = ?",
            ("2026-09-18",),
        )
        == 0
    )


def test_a_malformed_failure_record_is_skipped_rather_than_fatal(cfg: Config, db: Database) -> None:
    path = cfg.layout.runtime_dir / failures_mod.RECORD_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"broken\nnot json at all\n{"slot_local_date": "2026-09-18"}\n')

    assessment = failures_mod.assess(cfg.layout, db)

    assert assessment["recorded"] == 1
    assert assessment["unresolved_slots"] == ["2026-09-18"]


def test_no_failure_journal_at_all_is_simply_healthy(cfg: Config, db: Database) -> None:
    assessment = failures_mod.assess(cfg.layout, db)
    assert assessment == {
        "recorded": 0,
        "unresolved": [],
        "unresolved_slots": [],
        "resolved": 0,
        "path": str(cfg.layout.runtime_dir / failures_mod.RECORD_NAME),
        "note": assessment["note"],
    }


# --------------------------------------------------------------- scoring


def test_a_missing_off_host_backup_does_not_lower_required_health(
    cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    """The operator decided local snapshots are sufficient here.

    Asserted as "off-host is not what decides the verdict" rather than "the
    verdict is healthy", so the test keeps meaning whatever else is going on in
    the fixture archive.
    """
    collect(one_job_source, run_kind="daily")
    # Configured the way production is, so the only remaining candidate for an
    # "unknown" component would be off-host protection.
    cfg.notify.kind = "smtp"
    cfg.notify.recipient = "operator@example.com"

    document = status(cfg, db)

    # It is not a component at all, so nothing can score it. ControlPanel rolls
    # components up with max(), so an UNKNOWN component would hold the whole
    # project at unknown permanently -- which is exactly how it first behaved
    # when this was published as a component.
    assert [c for c in document["components"] if c["id"] == "offhost-backup"] == []
    backup_evidence = {
        e["label"]: e["value"] for e in component(document, "local-backup")["evidence"]
    }
    assert backup_evidence["Off-host protection"] == "UNCONFIGURED"
    assert document["metrics"]["offhost_backup_state"] == "UNCONFIGURED"
    unknown = [c["id"] for c in document["components"] if c["health"] == "unknown"]
    assert unknown == [], f"unscoreable components remain: {unknown}"


def test_mail_trouble_is_degraded_but_never_failed(
    cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    """A report that did not arrive does not unmake the harvest it describes."""
    result = collect(one_job_source, run_kind="daily")
    with db.write():
        db.execute(
            "INSERT INTO notifications(run_id, kind, recipient, subject, body_sha256, state, "
            "attempts, max_attempts, created_at_utc, updated_at_utc, last_error) "
            "VALUES (?, 'run_report', 'a@b', 's', 'd', 'abandoned', 5, 5, ?, ?, 'refused')",
            (result.run_id, "2026-09-19T10:00:00Z", "2026-09-19T10:00:00Z"),
        )
    cfg.notify.kind = "smtp"
    cfg.notify.recipient = "a@b"

    document = status(cfg, db)

    assert component(document, "run-reporting")["health"] == "degraded"
    assert document["overall"]["health"] == "degraded"
    assert "Run reporting" in document["overall"]["summary"]


def test_a_source_side_not_found_document_is_recorded_not_scored_as_a_fault(
    cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    """An employer linking a PDF that does not exist is a fact, not a fault.

    Scoring it as a fault would leave the console permanently yellow over
    something nobody can act on, which is how people learn to ignore consoles.
    """
    result = collect(one_job_source, run_kind="daily")
    with db.write():
        db.execute(
            "INSERT INTO resource_observations(run_id, outcome, outcome_detail, url_resolved, "
            "observed_at_utc) VALUES (?, 'failed', 'HTTP 404', 'https://rowan.edu/gone.pdf', ?)",
            (result.run_id, "2026-09-19T10:00:00Z"),
        )

    documents = component(status(cfg, db), "linked-documents")

    assert documents["health"] == "healthy"
    assert "not-found" in documents["summary"]
    assert {"label": "Not found at source (recorded)", "value": "1"} in documents["evidence"]


def test_a_document_we_could_not_check_does_lower_health(
    cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    result = collect(one_job_source, run_kind="daily")
    with db.write():
        db.execute(
            "INSERT INTO resource_observations(run_id, outcome, outcome_detail, url_resolved, "
            "observed_at_utc) VALUES (?, 'failed', 'connection timed out', "
            "'https://rowan.edu/slow.pdf', ?)",
            (result.run_id, "2026-09-19T10:00:00Z"),
        )

    documents = component(status(cfg, db), "linked-documents")

    assert documents["health"] == "degraded"
    assert "could not be checked" in documents["summary"]


def test_a_run_in_progress_is_reported_as_running_not_as_a_problem(
    cfg: Config, db: Database, collect: Callable[..., Any], one_job_source: FakeSource
) -> None:
    collect(one_job_source, run_kind="daily")
    # Clone the completed run and reopen it, rather than assembling a synthetic
    # row: the real table has required columns this test has no opinion about.
    columns = [
        str(r["name"])
        for r in db.query("PRAGMA table_info(collection_runs)")
        if str(r["name"]) != "run_id"
    ]
    names = ", ".join(columns)
    # run_uuid is unique, so the clone needs its own.
    selected = ", ".join("'in-flight'" if c == "run_uuid" else c for c in columns)
    with db.write():
        db.execute(
            f"INSERT INTO collection_runs({names}) SELECT {selected} FROM collection_runs "
            "ORDER BY run_id DESC LIMIT 1"
        )
        db.execute(
            "UPDATE collection_runs SET ended_at_utc=NULL, "
            "outcome=NULL, outcome_detail=NULL, scheduled_slot_local_date='2026-09-20', "
            "started_at_utc='2026-09-20T10:15:00Z' "
            "WHERE run_id = (SELECT MAX(run_id) FROM collection_runs)"
        )

    document = status(cfg, db)

    assert component(document, "daily-collection")["health"] == "running"
    assert document["overall"]["summary"] == "A collection is running now"
