"""The machine-readable health contract.

Collection health, archive integrity, local backup health and off-host
protection are reported separately, because they fail independently and
collapsing them would hide the failure an operator needs to see.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from rowanjobs import HEALTH_SCHEMA_VERSION
from rowanjobs.config import Config
from rowanjobs.db import Database
from rowanjobs.db.migrations import SCHEMA_VERSION
from rowanjobs.ops.backup import BackupManager
from rowanjobs.ops.health import build_health, exit_code_for, write_health

from .conftest import (
    LISTING_URL,
    FakeSource,
    build_detail_page,
    build_listing_page,
    error_response,
    listing_job,
)

DETAIL_URL = "https://jobs.rowan.edu/en-us/job/1001/job-1001"


def good_source() -> FakeSource:
    source = FakeSource()
    source.page(
        LISTING_URL, build_listing_page([listing_job("1001", "Archivist", slug="job-1001")])
    )
    source.page(DETAIL_URL, build_detail_page(job_id="1001", title="Archivist"))
    return source


def health(cfg: Config, db: Database) -> dict[str, Any]:
    return build_health(cfg, db, include_timer=False)


def test_health_reports_the_documented_top_level_contract(cfg: Config, db: Database) -> None:
    payload = health(cfg, db)

    assert payload["health_schema_version"] == HEALTH_SCHEMA_VERSION == "2"
    assert set(payload) == {
        "health_schema_version",
        "application",
        "app_version",
        "generated_at_utc",
        "generated_at_local",
        "operational_timezone",
        "versions",
        "collection",
        "archive",
        "backup",
        "notifications",
        "schedule",
        "paths",
        "notes",
    }
    assert payload["operational_timezone"] == "America/New_York"
    assert payload["versions"]["schema"] == payload["versions"]["schema_expected"] == SCHEMA_VERSION
    assert set(payload["versions"]) == {
        "schema",
        "schema_expected",
        "parser",
        "comparison_contract",
        "text_contract",
        "qualification_rules",
        "event_rules",
    }
    assert payload["schedule"] is None  # timer inspection was skipped


def test_an_archive_with_no_runs_reports_never_run_rather_than_healthy(
    cfg: Config, db: Database
) -> None:
    payload = health(cfg, db)
    assert payload["collection"]["state"] == "NEVER_RUN"
    assert payload["collection"]["last_attempt"] is None
    assert payload["collection"]["last_qualified_discovery"] is None
    assert payload["collection"]["counts"]["postings_total"] == 0
    # Nothing has been collected and nothing is backed up yet.
    assert exit_code_for(payload) == 3


def test_a_successful_collection_reports_healthy_with_its_evidence(
    cfg: Config, db: Database, collect
) -> None:
    result = collect(good_source())

    payload = health(cfg, db)
    collection = payload["collection"]
    assert collection["state"] == "HEALTHY"
    assert collection["last_attempt"]["run_id"] == result.run_id
    assert collection["last_attempt"]["outcome"] == "success"
    assert collection["last_attempt"]["duration"]
    assert collection["last_qualified_discovery"]["final_qualified_listing_count"] == 1
    assert collection["last_qualified_discovery"]["duplicate_occurrences"] == 1
    assert collection["last_reconciled_content_capture"]["total_captured_observations"] == 1
    assert collection["union_encountered_last_run"] == 1
    assert collection["counts"]["postings_total"] == 1
    assert collection["counts"]["qualified_scans"] == 2
    assert collection["failures"]["retrieval"] == 0
    assert collection["coverage_gaps"] == []
    assert collection["run_in_progress"] is None


def test_a_partial_collection_is_degraded_not_healthy(cfg: Config, db: Database, collect) -> None:
    broken = FakeSource()
    broken.add(LISTING_URL, error_response(503))
    collect(broken)

    payload = health(cfg, db)

    assert payload["collection"]["state"] == "DEGRADED"
    assert payload["collection"]["last_attempt"]["outcome"] == "partial"
    assert any(
        g["kind"] == "no_qualified_discovery" for g in payload["collection"]["coverage_gaps"]
    )
    assert exit_code_for(payload) == 2


def test_archive_health_reports_the_runtime_and_payload_accounting(
    cfg: Config, db: Database, collect
) -> None:
    collect(good_source())

    archive = health(cfg, db)["archive"]

    assert archive["state"] == "VERIFIED"
    assert archive["database_path"] == str(cfg.layout.db_path)
    assert archive["database_bytes"] > 0
    assert archive["journal_mode"] == db.journal_mode
    assert archive["sqlite_runtime"]["provider"] == "apsw"
    assert archive["payload_bytes_uncompressed"] > 0
    assert archive["compression_ratio"] > 0
    assert archive["artifact_deduplication"]["artifacts"] >= 1
    assert archive["disk"]["free_bytes"] > 0


def test_backup_protection_is_reported_separately_from_collection(
    cfg: Config, db: Database, collect
) -> None:
    collect(good_source())
    payload = health(cfg, db)

    assert payload["collection"]["state"] == "HEALTHY"
    assert payload["backup"]["local"]["state"] == "UNPROTECTED"
    assert payload["backup"]["offhost"]["state"] == "UNCONFIGURED"
    # Healthy collection plus unprotected archive is its own exit code.
    assert exit_code_for(payload) == 3

    BackupManager(cfg).create(db)
    protected = health(cfg, db)
    assert protected["backup"]["local"]["state"] == "VERIFIED"
    # An unconfigured off-host destination stays visible without failing the
    # exit code; only a failed push does that.
    assert protected["backup"]["offhost"]["state"] == "UNCONFIGURED"
    assert exit_code_for(protected) == 0


def test_notifications_report_unconfigured_rather_than_pretending_to_alert(
    cfg: Config, db: Database
) -> None:
    payload = health(cfg, db)
    assert payload["notifications"]["state"] == "UNCONFIGURED"
    assert "no notification destination" in payload["notifications"]["detail"]


@pytest.mark.parametrize(
    ("state", "expected"),
    [("FAILED", 1), ("DEGRADED", 2), ("UNKNOWN", 2), ("RUNNING", 0), ("HEALTHY", 0)],
)
def test_exit_codes_follow_the_documented_mapping(state: str, expected: int) -> None:
    payload = {
        "collection": {"state": state},
        "backup": {"local": {"state": "VERIFIED"}, "offhost": {"state": "VERIFIED"}},
    }
    assert exit_code_for(payload) == expected


def test_a_failed_offhost_push_is_reported_as_a_protection_problem() -> None:
    payload = {
        "collection": {"state": "HEALTHY"},
        "backup": {"local": {"state": "VERIFIED"}, "offhost": {"state": "FAILED"}},
    }
    assert exit_code_for(payload) == 3


def test_health_is_written_atomically_with_restrictive_permissions(
    cfg: Config, db: Database, collect
) -> None:
    collect(good_source())

    path = write_health(cfg, db)

    assert path == cfg.layout.health_path
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["health_schema_version"] == HEALTH_SCHEMA_VERSION
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert not list(path.parent.glob(".health.json.*.tmp"))


def test_paths_section_names_every_location_the_operator_needs(cfg: Config, db: Database) -> None:
    paths = health(cfg, db)["paths"]
    assert paths["data_root"] == str(cfg.layout.data_root)
    assert paths["database"] == str(cfg.layout.db_path)
    assert paths["backups"] == str(cfg.layout.backups_dir)
    assert paths["health"] == str(cfg.layout.health_path)


def test_an_open_run_is_reported_as_running(cfg: Config, db: Database, repo) -> None:
    source_id = repo.ensure_source(cfg)
    repo.start_run(
        source_id=source_id,
        source_config_id=repo.ensure_source_config(source_id, cfg),
        run_kind="daily",
        scheduled_slot_utc=None,
        scheduled_slot_local_date="2026-09-16",
    )

    payload = health(cfg, db)

    assert payload["collection"]["state"] == "RUNNING"
    assert payload["collection"]["run_in_progress"] is not None
    assert exit_code_for(payload) == 3  # running, but still no verified backup


def test_collection_health_is_judged_on_the_scheduled_run_not_a_manual_one(
    cfg: Config, db: Database, collect
) -> None:
    """A bounded manual run must not make a working timer look degraded.

    Health answers "is the daily collection working?". An operator running a
    capped verification pass has not broken anything.
    """
    from .test_runner import make_source

    source = make_source({"1001": "A", "1002": "B"})
    assert collect(source, run_kind="daily").outcome == "success"
    manual = collect(source, run_kind="verification", max_details=1, skip_verification=True)
    assert manual.outcome == "partial"

    payload = build_health(cfg, db, include_timer=False)

    assert payload["collection"]["state"] == "HEALTHY"
    assert payload["collection"]["last_attempt"]["run_kind"] == "verification"
    assert payload["collection"]["last_scheduled_attempt"]["run_kind"] == "daily"
    assert exit_code_for(payload) in (0, 3)


def test_a_failing_scheduled_run_is_not_masked_by_a_manual_success(
    cfg: Config, db: Database, collect
) -> None:
    """The reverse must hold too, or health would be trivially gameable."""
    from .conftest import LISTING_URL, FakeSource, error_response
    from .test_runner import make_source

    broken = FakeSource()
    broken.add(LISTING_URL, error_response(500), error_response(500))
    assert collect(broken, run_kind="daily").outcome in ("partial", "failed")

    collect(make_source({"1001": "A"}), run_kind="manual")

    payload = build_health(cfg, db, include_timer=False)
    assert payload["collection"]["state"] in ("DEGRADED", "FAILED", "STALE")
