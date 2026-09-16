"""Derived presence events.

Every row carries the rules version and the evidence that produced it. Absence
needs a qualified scan and is counted per scheduled slot date, so same-day
retries can never add up to a second daily confirmation.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from rowanjobs import EVENT_RULES_VERSION
from rowanjobs.collect.events import EventDeriver, repeatedly_unlisted
from rowanjobs.collect.repo import Repository
from rowanjobs.collect.scanner import ListingScanner
from rowanjobs.config import Config
from rowanjobs.db import Database
from rowanjobs.timeutil import slot_for

from .conftest import (
    LISTING_URL,
    FakeSource,
    build_detail_page,
    build_listing_page,
    listing_job,
)


def job_url(job_id: str) -> str:
    return f"https://jobs.rowan.edu/en-us/job/{job_id}/job-{job_id}"


def make_source(jobs: dict[str, str], *, detail_for: tuple[str, ...] = ()) -> FakeSource:
    source = FakeSource()
    source.page(
        LISTING_URL,
        build_listing_page(
            [listing_job(job_id, title, slug=f"job-{job_id}") for job_id, title in jobs.items()]
        ),
    )
    for job_id in {*jobs, *detail_for}:
        source.page(job_url(job_id), build_detail_page(job_id=job_id))
    return source


def posting_id_of(db: Database, job_id: str) -> int:
    return int(db.scalar("SELECT posting_id FROM postings WHERE external_job_id = ?", (job_id,)))


def absences(db: Database, job_id: str) -> list[dict[str, Any]]:
    return db.query(
        "SELECT e.* FROM presence_events e JOIN postings p ON p.posting_id = e.posting_id "
        "WHERE p.external_job_id = ? AND e.event_kind = 'absent_qualified' "
        "ORDER BY e.presence_event_id",
        (job_id,),
    )


# ---------------------------------------------------- the daily-confirmation rule


def test_same_day_retries_never_add_up_to_two_daily_absence_confirmations(
    collect, repo: Repository, db: Database
) -> None:
    collect(make_source({"1001": "Stays", "1002": "Goes"}))
    without = make_source({"1001": "Stays"}, detail_for=("1002",))

    daily = collect(without, run_kind="daily")
    retry = collect(without, run_kind="retry")

    slots = db.query(
        "SELECT run_id, run_kind, scheduled_slot_local_date FROM collection_runs "
        "WHERE run_id IN (?,?)",
        (daily.run_id, retry.run_id),
    )
    assert len({r["scheduled_slot_local_date"] for r in slots}) == 1

    rows = absences(db, "1002")
    assert len(rows) == 2, "each qualified run records its own evidence"
    assert {r["slot_local_date"] for r in rows} == {slots[0]["scheduled_slot_local_date"]}

    report = repeatedly_unlisted(repo, posting_id_of(db, "1002"))
    assert report["count"] == 1
    assert report["meets_two_day_rule"] is False
    assert len(report["distinct_absent_slot_dates"]) == 1
    assert report["rules_version"] == EVENT_RULES_VERSION


def test_two_distinct_slot_dates_meet_the_two_day_reporting_rule(
    collect, repo: Repository, db: Database
) -> None:
    collect(make_source({"1001": "Stays", "1002": "Goes"}))
    without = make_source({"1001": "Stays"}, detail_for=("1002",))
    collect(without, run_kind="daily")

    # A second qualifying day, spelled out on the derived row.
    posting_id = posting_id_of(db, "1002")
    with db.write():
        db.execute(
            "INSERT INTO presence_events(posting_id, event_kind, rules_version, "
            "interval_end_utc, slot_local_date, comparability_group, evidence_json, "
            "created_at_utc) VALUES (?, 'absent_qualified', ?, ?, ?, ?, '{}', ?)",
            (
                posting_id,
                EVENT_RULES_VERSION,
                "2026-09-17T11:00:00Z",
                "2026-09-17",
                "v1-unfiltered-en-us",
                "2026-09-17T11:00:00Z",
            ),
        )

    report = repeatedly_unlisted(repo, posting_id)
    assert report["count"] == 2
    assert report["meets_two_day_rule"] is True
    assert report["latest_absent_at_utc"] == "2026-09-17T11:00:00Z"


def test_calendar_days_with_no_qualified_scan_are_reported_as_gaps_not_absences(
    repo: Repository, db: Database, cfg: Config, run_environment: dict[str, Any]
) -> None:
    posting_id, _created = repo.ensure_posting(
        source_id=run_environment["source_id"],
        external_job_id="1002",
        run_id=run_environment["run_id"],
        discovery_basis="baseline",
        observed_at_utc="2026-09-10T11:00:00Z",
    )
    for day in ("2026-09-10", "2026-09-13"):
        with db.write():
            db.execute(
                "INSERT INTO presence_events(posting_id, event_kind, rules_version, "
                "interval_end_utc, slot_local_date, comparability_group, evidence_json, "
                "created_at_utc) VALUES (?, 'absent_qualified', ?, ?, ?, ?, '{}', ?)",
                (
                    posting_id,
                    EVENT_RULES_VERSION,
                    f"{day}T11:00:00Z",
                    day,
                    "v1-unfiltered-en-us",
                    f"{day}T11:00:00Z",
                ),
            )

    report = repeatedly_unlisted(repo, posting_id)

    assert report["distinct_absent_slot_dates"] == ["2026-09-10", "2026-09-13"]
    assert report["meets_two_day_rule"] is True
    assert "2026-09-11" in report["intervening_uncovered_dates"]
    assert "2026-09-12" in report["intervening_uncovered_dates"]


def test_a_posting_that_was_never_absent_reports_no_absence_evidence(
    collect, repo: Repository, db: Database
) -> None:
    collect(make_source({"1001": "Stays"}))
    report = repeatedly_unlisted(repo, posting_id_of(db, "1001"))
    assert report["count"] == 0
    assert report["meets_two_day_rule"] is False
    assert report["first_absent_at_utc"] is None


# --------------------------------------------------------------- scope changes


def test_events_carry_the_comparability_group_so_scopes_are_never_compared_silently(
    collect, cfg: Config, db: Database
) -> None:
    collect(make_source({"1001": "Stays", "1002": "Goes"}))
    without = make_source({"1001": "Stays"}, detail_for=("1002",))
    collect(without)

    cfg.collection.comparability_group = "v2-glassboro-only"
    collect(without)

    groups = [r["comparability_group"] for r in absences(db, "1002")]
    assert groups == ["v1-unfiltered-en-us", "v2-glassboro-only"]
    # An absence history is only comparable inside one group.
    assert len(set(groups)) == 2
    listed = db.query(
        "SELECT DISTINCT comparability_group FROM presence_events WHERE event_kind = 'listed'"
    )
    assert {r["comparability_group"] for r in listed} == {
        "v1-unfiltered-en-us",
        "v2-glassboro-only",
    }


def test_a_scope_change_is_recorded_as_a_new_source_configuration(
    collect, cfg: Config, db: Database
) -> None:
    collect(make_source({"1001": "A"}))
    cfg.collection.comparability_group = "v2-glassboro-only"
    cfg.collection.scope_label = "glassboro-only"
    collect(make_source({"1001": "A"}))

    configs = db.query("SELECT * FROM source_configs ORDER BY source_config_id")
    assert len(configs) == 2
    assert [c["comparability_group"] for c in configs] == [
        "v1-unfiltered-en-us",
        "v2-glassboro-only",
    ]
    assert configs[0]["config_hash"] != configs[1]["config_hash"]


# ------------------------------------------------------------------ evidence


def test_every_derived_event_names_its_rules_version_and_supporting_evidence(
    collect, db: Database
) -> None:
    result = collect(make_source({"1001": "A"}))

    rows = db.query("SELECT * FROM presence_events")
    assert rows
    for row in rows:
        assert row["rules_version"] == EVENT_RULES_VERSION
        assert int(row["run_id"]) == result.run_id
        assert row["interval_end_utc"]
        assert row["comparability_group"]
        evidence = json.loads(str(row["evidence_json"]))
        assert evidence.get("scan_id")
    scan_ids = {int(r["scan_id"]) for r in rows}
    known = {
        int(r["scan_id"])
        for r in db.query("SELECT scan_id FROM listing_scans WHERE run_id = ?", (result.run_id,))
    }
    assert scan_ids <= known


def test_deriving_the_same_run_twice_does_not_duplicate_events(
    cfg: Config,
    repo: Repository,
    db: Database,
    run_environment: dict[str, Any],
    make_client,
) -> None:
    source = make_source({"1001": "A", "1002": "B"})
    scanner = ListingScanner(
        cfg=cfg,
        repo=repo,
        client=make_client(source),
        run_id=run_environment["run_id"],
        source_id=run_environment["source_id"],
    )
    scan = scanner.scan(scan_ordinal=1, scan_role="discovery")
    assert scan.qualified is True
    deriver = EventDeriver(cfg=cfg, repo=repo)

    first = deriver.derive_for_run(
        run_id=run_environment["run_id"],
        slot_local_date="2026-09-16",
        qualified_scan=scan,
        listed_ids=set(scan.unique_ids),
    )
    before = int(db.scalar("SELECT COUNT(*) FROM presence_events"))
    second = deriver.derive_for_run(
        run_id=run_environment["run_id"],
        slot_local_date="2026-09-16",
        qualified_scan=scan,
        listed_ids=set(scan.unique_ids),
    )

    assert first.as_dict() == second.as_dict()
    assert int(db.scalar("SELECT COUNT(*) FROM presence_events")) == before


def test_an_unqualified_scan_produces_a_gap_and_no_presence_events(
    cfg: Config, repo: Repository, db: Database, run_environment: dict[str, Any]
) -> None:
    deriver = EventDeriver(cfg=cfg, repo=repo)

    summary = deriver.derive_for_run(
        run_id=run_environment["run_id"],
        slot_local_date="2026-09-16",
        qualified_scan=None,
        listed_ids=set(),
    )

    assert summary.coverage_gaps == 1
    assert summary.absent_qualified == 0
    assert int(db.scalar("SELECT COUNT(*) FROM presence_events")) == 0
    gap = db.one("SELECT * FROM coverage_gaps WHERE kind = 'no_qualified_discovery'")
    assert gap["scope"] == "listing"
    assert gap["slot_local_date"] == "2026-09-16"


@pytest.mark.parametrize("summary_key", ["first_observed", "listed", "absent_qualified"])
def test_the_run_summary_counts_match_the_rows_written(
    collect, db: Database, summary_key: str
) -> None:
    collect(make_source({"1001": "Stays", "1002": "Goes"}))
    without = make_source({"1001": "Stays"}, detail_for=("1002",))
    result = collect(without)

    written = int(
        db.scalar(
            "SELECT COUNT(*) FROM presence_events WHERE event_kind = ? AND run_id = ?",
            (summary_key, result.run_id),
        )
    )
    assert result.counts["events"][summary_key] == written


def test_a_retry_keeps_the_slot_of_the_daily_run_it_is_retrying() -> None:
    """The slot, not the wall clock, is the daily-confirmation key."""
    daily = slot_for("2026-09-16T10:15:00Z", 6, 15)
    retry = slot_for("2026-09-16T13:00:00Z", 6, 15)
    assert daily == retry
    assert daily[1] == "2026-09-16"
    next_day = slot_for("2026-09-17T10:15:00Z", 6, 15)
    assert next_day[1] == "2026-09-17"
    assert next_day != daily
