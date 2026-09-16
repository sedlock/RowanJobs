"""The whole collection workflow, end to end against a mock source.

Discovery, detail harvest, a second traversal, identifier-*set* comparison and a
bounded reconciliation pass. Two matching traversals are consistency evidence,
not proof the site held still, and everything that cannot be settled is recorded
as a coverage exception rather than resolved by guessing.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from rowanjobs.collect.repo import Repository
from rowanjobs.config import Config
from rowanjobs.db import Database

from .conftest import (
    LISTING_URL,
    FakeSource,
    build_detail_page,
    build_listing_page,
    error_response,
    html_response,
    listing_job,
)


def job_url(job_id: str) -> str:
    return f"https://jobs.rowan.edu/en-us/job/{job_id}/job-{job_id}"


def listing_markup(jobs: dict[str, str], **kwargs: Any) -> str:
    return build_listing_page(
        [listing_job(job_id, title, slug=f"job-{job_id}") for job_id, title in jobs.items()],
        **kwargs,
    )


def make_source(jobs: dict[str, str], **detail_kwargs: Any) -> FakeSource:
    """A source serving one listing page plus a detail page per advertisement."""
    source = FakeSource()
    source.page(LISTING_URL, listing_markup(jobs))
    for job_id, title in jobs.items():
        source.page(
            job_url(job_id), build_detail_page(job_id=job_id, title=title, **detail_kwargs)
        )
    return source


def events(db: Database, kind: str | None = None) -> list[dict[str, Any]]:
    sql = (
        "SELECT e.*, p.external_job_id FROM presence_events e "
        "JOIN postings p ON p.posting_id = e.posting_id"
    )
    params: tuple[Any, ...] = ()
    if kind:
        sql += " WHERE e.event_kind = ?"
        params = (kind,)
    return db.query(sql + " ORDER BY e.presence_event_id", params)


# ------------------------------------------------------------------ baseline


def test_a_complete_run_collects_listings_details_and_derived_events(
    collect, db: Database
) -> None:
    source = make_source({"1001": "Telecommunicator", "1002": "Parking Clerk"})

    result = collect(source)

    assert result.outcome == "success"
    assert result.counts["final_qualified_listing_count"] == 2
    assert result.counts["detail_captured"] == 2
    assert result.counts["versions_created"] == 2
    assert result.coverage["baseline_run"] is True
    assert result.coverage["absence_analysis_supported"] is True
    assert result.coverage["set_comparison"]["sets_match"] is True
    assert int(db.scalar("SELECT COUNT(*) FROM postings")) == 2
    assert int(db.scalar("SELECT COUNT(*) FROM posting_versions")) == 2
    assert int(db.scalar("SELECT COUNT(*) FROM posting_observations")) == 2
    assert {e["event_kind"] for e in events(db)} == {"first_observed", "listed"}
    run = db.one("SELECT * FROM collection_runs WHERE run_id = ?", (result.run_id,))
    assert run["outcome"] == "success"
    assert run["ended_at_utc"] is not None
    assert int(run["is_baseline"]) == 1


def test_postings_present_at_the_first_collection_are_a_baseline_not_new_arrivals(
    collect, db: Database
) -> None:
    collect(make_source({"1001": "First"}))
    collect(make_source({"1001": "First", "1002": "Second"}))

    bases = {
        r["external_job_id"]: r["discovery_basis"]
        for r in db.query("SELECT external_job_id, discovery_basis FROM postings")
    }
    assert bases == {"1001": "baseline", "1002": "observed-new"}


def test_two_advertisements_with_the_same_title_stay_separate_postings(
    collect, db: Database
) -> None:
    source = make_source({"1001": "Adjunct Instructor", "1002": "Adjunct Instructor"})

    collect(source)

    rows = db.query("SELECT external_job_id FROM postings ORDER BY external_job_id")
    assert [r["external_job_id"] for r in rows] == ["1001", "1002"]
    titles = db.query("SELECT DISTINCT title FROM posting_versions")
    assert [t["title"] for t in titles] == ["Adjunct Instructor"]
    assert int(db.scalar("SELECT COUNT(*) FROM posting_versions")) == 2


def test_same_identifier_with_a_new_title_and_url_stays_one_posting(
    collect, db: Database
) -> None:
    first = FakeSource()
    first.page(LISTING_URL, listing_markup({"1001": "Original Title"}))
    first.page(job_url("1001"), build_detail_page(job_id="1001", title="Original Title"))
    collect(first)

    # The source renames the advertisement and moves it to a new slug.
    moved_url = "https://jobs.rowan.edu/en-us/job/1001/renamed-slug"
    second = FakeSource()
    second.page(
        LISTING_URL,
        build_listing_page([listing_job("1001", "Renamed Title", slug="renamed-slug")]),
    )
    renamed = build_detail_page(job_id="1001", title="Renamed Title")
    second.page(moved_url, renamed)
    second.page(job_url("1001"), renamed)
    collect(second)

    assert int(db.scalar("SELECT COUNT(*) FROM postings")) == 1
    versions = db.query(
        "SELECT title FROM posting_versions ORDER BY posting_version_id",
    )
    assert [v["title"] for v in versions] == ["Original Title", "Renamed Title"]
    urls = {
        (r["url"], r["role"])
        for r in db.query("SELECT url, role FROM posting_urls ORDER BY posting_url_id")
    }
    assert (job_url("1001"), "listing-link") in urls
    assert (moved_url, "listing-link") in urls
    assert any(role == "canonical-detail" for _url, role in urls)


# ------------------------------------------------------- two-pass comparison


def test_a_source_change_between_passes_is_reconciled_rather_than_averaged(
    collect, db: Database
) -> None:
    source = FakeSource()
    source.add(
        LISTING_URL,
        html_response(listing_markup({"1001": "Stays", "1002": "Disappears"})),
        html_response(listing_markup({"1001": "Stays", "1003": "Appears"})),
    )
    for job_id, title in (("1001", "Stays"), ("1002", "Disappears"), ("1003", "Appears")):
        source.page(job_url(job_id), build_detail_page(job_id=job_id, title=title))

    result = collect(source)

    comparison = result.coverage["set_comparison"]
    assert comparison["performed"] is True
    assert comparison["appeared_between_passes"] == ["1003"]
    assert comparison["disappeared_between_passes"] == ["1002"]
    assert comparison["sets_match"] is False
    assert "reconciliation_unique" in comparison
    roles = [
        r["scan_role"]
        for r in db.query("SELECT scan_role FROM listing_scans ORDER BY scan_ordinal")
    ]
    assert roles == ["discovery", "verification", "reconciliation"]
    # The advertisement that appeared late was still retrieved this run.
    assert db.one(
        "SELECT * FROM posting_observations WHERE expected_external_job_id = '1003'"
    )["availability_state"] == "content_captured"


def test_identical_counts_with_different_identifiers_do_not_count_as_a_match(
    collect,
) -> None:
    source = FakeSource()
    source.add(
        LISTING_URL,
        html_response(listing_markup({"1001": "A", "1002": "B"})),
        html_response(listing_markup({"1001": "A", "1003": "C"})),
    )
    for job_id in ("1001", "1002", "1003"):
        source.page(job_url(job_id), build_detail_page(job_id=job_id))

    comparison = collect(source).coverage["set_comparison"]

    assert comparison["discovery_unique"] == comparison["verification_unique"] == 2
    assert comparison["counts_match"] is True
    assert comparison["sets_match"] is False


def test_an_unsettled_disagreement_suppresses_absence_analysis_for_the_run(
    collect, db: Database
) -> None:
    source = FakeSource()
    source.add(
        LISTING_URL,
        html_response(listing_markup({"1001": "A", "1002": "B"})),
        html_response(listing_markup({"1001": "A"})),
        # The reconciliation pass cannot be retrieved at all.
        error_response(500),
    )
    for job_id in ("1001", "1002"):
        source.page(job_url(job_id), build_detail_page(job_id=job_id))

    result = collect(source)

    assert result.outcome == "partial"
    assert result.coverage["absence_analysis_supported"] is False
    gaps = {g["kind"] for g in db.query("SELECT kind FROM coverage_gaps")}
    assert "listing_set_unreconciled" in gaps
    assert events(db, "absent_qualified") == []


def test_verification_can_be_skipped_without_pretending_it_ran(collect) -> None:
    result = collect(make_source({"1001": "Only"}), skip_verification=True)

    assert result.coverage["verification_qualified"] is False
    assert result.coverage["set_comparison"] == {"performed": False}
    assert result.counts["listing_scans"] == 1


# -------------------------------------------------------------- absence rules


def test_a_qualified_scan_that_no_longer_lists_a_posting_records_an_absence(
    collect, db: Database
) -> None:
    collect(make_source({"1001": "Stays", "1002": "Goes away"}))

    second = make_source({"1001": "Stays"})
    second.page(job_url("1002"), build_detail_page(job_id="1002", title="Goes away"))
    result = collect(second)

    absences = events(db, "absent_qualified")
    assert [e["external_job_id"] for e in absences] == ["1002"]
    assert absences[0]["comparability_group"] == "v1-unfiltered-en-us"
    assert absences[0]["interval_end_utc"]
    evidence = json.loads(str(absences[0]["evidence_json"]))
    assert "two distinct qualifying daily observations" in evidence["note"]
    assert int(result.counts["events"]["absent_qualified"]) == 1


def test_a_partial_collection_records_a_coverage_gap_and_no_absence(
    collect, db: Database
) -> None:
    collect(make_source({"1001": "Stays", "1002": "Goes away"}))

    broken = FakeSource()
    broken.add(LISTING_URL, error_response(503))
    for job_id in ("1001", "1002"):
        broken.page(job_url(job_id), build_detail_page(job_id=job_id))
    result = collect(broken)

    assert result.outcome == "partial"
    assert events(db, "absent_qualified") == []
    gaps = {
        g["kind"]: g
        for g in db.query("SELECT * FROM coverage_gaps WHERE run_id = ?", (result.run_id,))
    }
    assert "no_qualified_discovery" in gaps
    assert "no absence conclusion may be drawn" in str(gaps["no_qualified_discovery"]["detail"])


def test_a_posting_that_returns_is_recorded_as_reappeared_not_rediscovered(
    collect, db: Database
) -> None:
    collect(make_source({"1001": "A", "1002": "B"}))
    gone = make_source({"1001": "A"})
    gone.page(job_url("1002"), build_detail_page(job_id="1002", title="B"))
    collect(gone)
    collect(make_source({"1001": "A", "1002": "B"}))

    kinds = [e["event_kind"] for e in events(db) if e["external_job_id"] == "1002"]
    assert kinds.count("first_observed") == 1
    assert "absent_qualified" in kinds
    assert "reappeared" in kinds
    assert int(db.scalar("SELECT COUNT(*) FROM postings WHERE external_job_id='1002'")) == 1


def test_a_delisted_but_still_served_detail_page_is_still_checked_and_captured(
    collect, db: Database
) -> None:
    collect(make_source({"1001": "Stays", "1002": "Delisted"}))

    second = make_source({"1001": "Stays"})
    second.page(job_url("1002"), build_detail_page(job_id="1002", title="Delisted"))
    result = collect(second)

    row = db.one(
        "SELECT * FROM posting_observations WHERE expected_external_job_id = '1002' "
        "AND run_id = ? ORDER BY observation_id DESC LIMIT 1",
        (result.run_id,),
    )
    assert row["availability_state"] == "content_captured"
    assert str(row["checked_because"]).startswith("historical")
    # Still absent from the listing, so the absence event stands alongside it.
    assert [e["external_job_id"] for e in events(db, "absent_qualified")] == ["1002"]


def test_a_past_deadline_on_a_still_listed_advertisement_invents_no_closure(
    collect, db: Database
) -> None:
    source = make_source(
        {"1001": "Still advertised"},
        applications_close=("Sep 1 2026 11:55 PM ", "2026-09-02T03:55:00Z"),
    )

    result = collect(source)

    assert result.outcome == "success"
    closes = db.one(
        "SELECT * FROM version_values WHERE field_key = 'applications_close' LIMIT 1"
    )
    assert closes["parsed_utc"] == "2026-09-02T03:55:00Z"
    observation = db.one("SELECT * FROM posting_observations LIMIT 1")
    assert observation["availability_state"] == "content_captured"
    kinds = {e["event_kind"] for e in events(db)}
    assert kinds == {"first_observed", "listed"}
    assert "absent_qualified" not in kinds
    assert int(db.scalar("SELECT COUNT(*) FROM coverage_gaps")) == 0


# ------------------------------------------------------------ content history


def test_content_history_a_b_a_keeps_two_versions_and_three_observations(
    collect, db: Database, advancing_clock
) -> None:
    body_a = "<p>Original wording.</p>"
    body_b = "<p>Edited wording.</p>"
    source = FakeSource()
    source.page(LISTING_URL, listing_markup({"1001": "Job"}))
    source.add(
        job_url("1001"),
        html_response(build_detail_page(job_id="1001", body_html=body_a)),
        html_response(build_detail_page(job_id="1001", body_html=body_b)),
        html_response(build_detail_page(job_id="1001", body_html=body_a)),
    )

    collect(source)
    collect(source)
    collect(source)

    versions = db.query("SELECT posting_version_id FROM posting_versions ORDER BY 1")
    assert len(versions) == 2
    observations = db.query(
        "SELECT posting_version_id FROM posting_observations ORDER BY observation_id"
    )
    assert len(observations) == 3
    assert observations[0]["posting_version_id"] == observations[2]["posting_version_id"]
    assert observations[1]["posting_version_id"] != observations[0]["posting_version_id"]
    changes = events(db, "content_changed")
    assert len(changes) == 2
    assert changes[0]["from_posting_version_id"] == observations[0]["posting_version_id"]
    assert changes[0]["to_posting_version_id"] == observations[1]["posting_version_id"]
    assert changes[0]["interval_start_utc"] < changes[0]["interval_end_utc"]


def test_re_collecting_an_unchanged_page_adds_observations_but_no_artifacts(
    collect, db: Database
) -> None:
    source = make_source({"1001": "Unchanged"})

    collect(source)
    artifacts_after_first = int(db.scalar("SELECT COUNT(*) FROM artifacts"))
    extractions_after_first = int(db.scalar("SELECT COUNT(*) FROM extractions"))
    collect(source)

    assert int(db.scalar("SELECT COUNT(*) FROM artifacts")) == artifacts_after_first
    assert int(db.scalar("SELECT COUNT(*) FROM extractions")) == extractions_after_first
    assert int(db.scalar("SELECT COUNT(*) FROM posting_observations")) == 2
    assert int(db.scalar("SELECT COUNT(*) FROM posting_versions")) == 1
    assert int(db.scalar("SELECT COUNT(*) FROM fetches")) > artifacts_after_first
    assert events(db, "content_changed") == []


# ------------------------------------------------------------------ recovery


def test_an_interrupted_run_is_marked_aborted_and_its_evidence_survives(
    cfg: Config, collect, repo: Repository, db: Database
) -> None:
    first = collect(make_source({"1001": "A"}))
    observations_before = int(db.scalar("SELECT COUNT(*) FROM posting_observations"))
    artifacts_before = int(db.scalar("SELECT COUNT(*) FROM artifacts"))

    # Simulate a killed collector: an open run with claimed work.
    source_id = repo.ensure_source(cfg)
    crashed_id, _uuid = repo.start_run(
        source_id=source_id,
        source_config_id=repo.ensure_source_config(source_id, cfg),
        run_kind="daily",
        scheduled_slot_utc=None,
        scheduled_slot_local_date="2026-09-15",
    )
    repo.enqueue(run_id=crashed_id, kind="posting_detail", work_key="1001", payload={})
    repo.enqueue(run_id=crashed_id, kind="posting_detail", work_key="9999", payload={})
    assert repo.claim_next(crashed_id, "posting_detail", "token") is not None

    result = collect(make_source({"1001": "A"}))

    crashed = db.one("SELECT * FROM collection_runs WHERE run_id = ?", (crashed_id,))
    assert crashed["outcome"] == "aborted"
    assert crashed["ended_at_utc"] is not None
    assert "Evidence already written is preserved" in str(crashed["outcome_detail"])
    states = {
        r["state"]: int(r["n"])
        for r in db.query(
            "SELECT state, COUNT(*) AS n FROM work_queue WHERE run_id = ? GROUP BY state",
            (crashed_id,),
        )
    }
    assert states == {"abandoned": 2}
    assert any(e["kind"] == "abandoned_runs" for e in result.errors)

    # Nothing from the first run was lost or duplicated.
    assert (
        int(db.scalar("SELECT COUNT(*) FROM posting_observations WHERE run_id = ?", (first.run_id,)))
        == observations_before
    )
    assert int(db.scalar("SELECT COUNT(*) FROM artifacts")) == artifacts_before
    assert result.outcome == "success"


def test_work_left_in_progress_by_the_same_run_is_reclaimed_before_draining(
    repo: Repository, cfg: Config, db: Database
) -> None:
    source_id = repo.ensure_source(cfg)
    run_id, _uuid = repo.start_run(
        source_id=source_id,
        source_config_id=repo.ensure_source_config(source_id, cfg),
        run_kind="manual",
        scheduled_slot_utc=None,
        scheduled_slot_local_date="2026-09-16",
    )
    repo.enqueue(run_id=run_id, kind="posting_detail", work_key="1001", payload={})
    repo.claim_next(run_id, "posting_detail", "token")
    assert repo.queue_summary(run_id) == {"in_progress": 1}

    assert repo.reclaim_orphans(run_id) == 1
    assert repo.queue_summary(run_id) == {"pending": 1}


def test_a_detail_failure_is_retried_then_recorded_without_failing_the_run(
    collect, db: Database
) -> None:
    source = FakeSource()
    source.page(LISTING_URL, listing_markup({"1001": "Flaky"}))
    source.add(job_url("1001"), error_response(500))

    result = collect(source)

    assert result.outcome == "partial"
    assert result.counts["detail_uncertain"] >= 1
    queue = db.query(
        "SELECT state, attempts FROM work_queue WHERE run_id = ?", (result.run_id,)
    )
    assert queue[0]["state"] == "failed"
    assert int(queue[0]["attempts"]) == 2
    assert int(db.scalar("SELECT COUNT(*) FROM posting_observations")) == 2
    assert {
        r["availability_state"] for r in db.query("SELECT availability_state FROM posting_observations")
    } == {"retrieval_failed"}


def test_detail_retrievals_can_be_bounded_for_one_run(collect, db: Database) -> None:
    source = make_source({"1001": "A", "1002": "B", "1003": "C"})

    result = collect(source, max_details=1)

    assert result.counts["detail_attempted"] == 1
    assert int(db.scalar("SELECT COUNT(*) FROM posting_observations")) == 1
    assert result.counts["queue"]["pending"] == 2


# ------------------------------------------------------------ recheck policy


def test_repeated_terminal_observations_move_a_posting_to_weekly_rechecking(
    collect, cfg: Config, db: Database
) -> None:
    cfg.collection.terminal_observations_before_weekly = 2
    collect(make_source({"1001": "A", "1002": "B"}))

    for _ in range(2):
        gone = make_source({"1001": "A"})
        gone.add(job_url("1002"), error_response(404, "gone"))
        collect(gone)

    row = db.one(
        "SELECT r.* FROM recheck_policy r JOIN postings p ON p.posting_id = r.posting_id "
        "WHERE p.external_job_id = '1002'"
    )
    assert row["tier"] == "weekly"
    assert int(row["consecutive_terminal_observations"]) >= 2
    assert row["next_due_at_utc"] is not None
    assert "weekly recheck tier" in str(row["reason"])


def test_an_inconclusive_check_never_advances_the_demotion_streak(
    collect, cfg: Config, db: Database
) -> None:
    cfg.collection.terminal_observations_before_weekly = 1
    collect(make_source({"1001": "A", "1002": "B"}))

    from .conftest import challenge_response

    gone = make_source({"1001": "A"})
    gone.add(job_url("1002"), challenge_response())
    collect(gone)

    row = db.one(
        "SELECT r.* FROM recheck_policy r JOIN postings p ON p.posting_id = r.posting_id "
        "WHERE p.external_job_id = '1002'"
    )
    assert row["tier"] == "daily"
    assert int(row["consecutive_terminal_observations"]) == 0
    assert "inconclusive" in str(row["reason"])


def test_a_relisted_posting_returns_to_daily_rechecking(
    collect, cfg: Config, db: Database
) -> None:
    cfg.collection.terminal_observations_before_weekly = 1
    collect(make_source({"1001": "A", "1002": "B"}))
    gone = make_source({"1001": "A"})
    gone.add(job_url("1002"), error_response(404))
    collect(gone)
    collect(make_source({"1001": "A", "1002": "B"}))

    row = db.one(
        "SELECT r.* FROM recheck_policy r JOIN postings p ON p.posting_id = r.posting_id "
        "WHERE p.external_job_id = '1002'"
    )
    assert row["tier"] == "daily"
    assert int(row["consecutive_terminal_observations"]) == 0
    assert "listed in the current qualified scan" in str(row["reason"])


# ------------------------------------------------------------------- budget


def test_the_run_reports_the_source_traffic_it_generated(collect) -> None:
    result = collect(make_source({"1001": "A"}))

    budget = result.counts["budget"]
    # two listing traversals plus one detail retrieval
    assert budget["requests"] == 3
    assert budget["challenges"] == 0
    assert budget["bytes_received"] > 0


@pytest.mark.parametrize("run_kind", ["daily", "retry", "manual", "verification"])
def test_every_documented_run_kind_is_accepted(collect, db: Database, run_kind: str) -> None:
    result = collect(make_source({"1001": "A"}), run_kind=run_kind)
    assert result.outcome == "success"
    assert db.scalar("SELECT run_kind FROM collection_runs WHERE run_id = ?", (result.run_id,)) == (
        run_kind
    )
