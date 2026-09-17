"""The command line interface.

Exit codes are part of the contract: a timer and an operator both read them.
Reporting commands open the archive read-only, so running a report can never
migrate or mutate production data.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from rowanjobs.cli import (
    EXIT_DEGRADED,
    EXIT_LOCKED,
    EXIT_OK,
    EXIT_PROTECTION,
    EXIT_USAGE,
    main,
)
from rowanjobs.collect.lock import CollectorLock
from rowanjobs.collect.runner import Collector
from rowanjobs.config import Config
from rowanjobs.db import open_readonly

from .conftest import (
    LISTING_URL,
    FakeSource,
    build_detail_page,
    build_listing_page,
    error_response,
    html_response,
    listing_job,
)

DETAIL_URL = "https://jobs.rowan.edu/en-us/job/1001/job-1001"
FORMULA_TITLE = "=cmd|'/C calc'!A0"


@pytest.fixture
def cli(cfg: Config) -> Callable[..., int]:
    def run(*args: str) -> int:
        return main(["--data-root", str(cfg.data_root), *args])

    return run


@pytest.fixture
def source(monkeypatch: pytest.MonkeyPatch, make_client) -> FakeSource:
    """Give the CLI's own collector a mock transport instead of the network."""
    fake = FakeSource()
    fake.page(
        LISTING_URL, build_listing_page([listing_job("1001", FORMULA_TITLE, slug="job-1001")])
    )
    fake.page(DETAIL_URL, build_detail_page(job_id="1001", title=FORMULA_TITLE))
    monkeypatch.setattr(Collector, "_make_client", lambda _self: make_client(fake))
    return fake


def captured_json(capsys: pytest.CaptureFixture[str]) -> Any:
    return json.loads(capsys.readouterr().out)


# ------------------------------------------------------------------ migrate


def test_migrate_creates_the_schema_and_reports_the_versions(
    cli, cfg: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli("--json", "migrate") == EXIT_OK

    payload = captured_json(capsys)
    assert payload["version_before"] == 0
    assert payload["version_after"] == payload["expected"]
    # Every migration, in order, rather than a hard-coded list that has to be
    # edited each time one is added.
    assert payload["applied"] == list(range(1, payload["expected"] + 1))
    assert Path(payload["database"]) == cfg.layout.db_path
    assert cfg.layout.db_path.exists()

    # Running it again is a no-op.
    assert cli("--json", "migrate") == EXIT_OK
    assert captured_json(capsys)["applied"] == []


# ------------------------------------------------------------------ collect


def test_collect_runs_a_whole_collection_and_reports_it_as_json(
    cli, source: FakeSource, cfg: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli("--json", "collect", "--kind", "manual")

    payload = captured_json(capsys)
    assert payload["outcome"] == "success"
    assert payload["counts"]["final_qualified_listing_count"] == 1
    assert payload["coverage"]["absence_analysis_supported"] is True
    assert payload["backup"]["state"] == "VERIFIED"
    assert payload["notification"]["state"] == "SKIPPED"
    assert payload["duration"]
    assert code == EXIT_OK
    assert cfg.layout.health_path.exists()
    assert json.loads(cfg.layout.health_path.read_text())["collection"]["state"] == "HEALTHY"


def test_a_degraded_collection_exits_with_the_degraded_code(
    cli, monkeypatch: pytest.MonkeyPatch, make_client, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = FakeSource()
    broken.add(LISTING_URL, error_response(503))
    monkeypatch.setattr(Collector, "_make_client", lambda _self: make_client(broken))

    code = cli("--json", "collect", "--no-backup")

    assert code == EXIT_DEGRADED
    assert captured_json(capsys)["outcome"] == "partial"


def test_a_contended_collector_exits_four_without_recording_a_run(
    cli, cfg: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli("--json", "migrate") == EXIT_OK
    capsys.readouterr()
    cfg.layout.ensure()
    holder = CollectorLock(cfg.layout.lock_path).acquire(run_uuid="already-running")
    try:
        code = cli("--json", "collect")
    finally:
        holder.release()

    payload = captured_json(capsys)
    assert code == EXIT_LOCKED
    assert payload["outcome"] == "lock_contention"
    assert payload["run_id"] is None

    handle = open_readonly(cfg.layout.db_path)
    try:
        assert int(handle.scalar("SELECT COUNT(*) FROM collection_runs")) == 0
    finally:
        handle.close()


# ------------------------------------------------------------------- status


def test_status_json_returns_the_documented_health_contract(
    cli, source: FakeSource, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli("collect") == EXIT_OK
    capsys.readouterr()

    code = cli("--json", "status", "--no-timer")

    payload = captured_json(capsys)
    assert payload["health_schema_version"] == "1"
    assert set(payload) >= {
        "application",
        "app_version",
        "generated_at_utc",
        "versions",
        "collection",
        "archive",
        "backup",
        "notifications",
        "paths",
    }
    assert payload["collection"]["state"] == "HEALTHY"
    assert payload["archive"]["state"] == "VERIFIED"
    assert payload["backup"]["local"]["state"] == "VERIFIED"
    assert code == EXIT_OK


def test_status_never_migrates_or_creates_an_archive(
    cli, cfg: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli("--json", "status", "--no-timer")
    assert code == EXIT_USAGE
    assert "no archive at" in capsys.readouterr().out
    assert not cfg.layout.db_path.exists()


def test_status_text_output_names_the_separate_health_dimensions(
    cli, source: FakeSource, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli("collect") == EXIT_OK
    capsys.readouterr()

    cli("status", "--no-timer")

    out = capsys.readouterr().out
    assert "collection      :" in out
    assert "archive         :" in out
    assert "backup (local)  :" in out
    assert "backup (offhost):" in out
    assert "notifications   :" in out


# ------------------------------------------------------- reporting commands


def test_runs_lists_the_recorded_collection_runs(
    cli, source: FakeSource, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli("collect") == EXIT_OK
    capsys.readouterr()

    assert cli("--json", "runs", "--limit", "5") == EXIT_OK

    runs = captured_json(capsys)["runs"]
    assert len(runs) == 1
    assert runs[0]["outcome"] == "success"
    assert runs[0]["qualified_scans"] == 2


def test_show_reports_one_advertisement_with_its_absence_evidence(
    cli, source: FakeSource, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli("collect") == EXIT_OK
    capsys.readouterr()

    assert cli("--json", "show", "1001") == EXIT_OK

    payload = captured_json(capsys)
    assert payload["current"]["external_job_id"] == "1001"
    assert payload["current"]["content_freshness"] == "checked"
    assert payload["version"]["description_html_kind"] == "source-substring"
    assert {v["field_key"] for v in payload["values"]} >= {"location", "advertised"}
    assert payload["absence_evidence"]["count"] == 0
    assert any(u["role"] == "canonical-detail" for u in payload["urls"])


def test_show_for_an_unknown_job_is_a_usage_error(
    cli, source: FakeSource, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli("collect") == EXIT_OK
    capsys.readouterr()
    assert cli("show", "999999") == EXIT_USAGE
    assert "no posting with source job id" in capsys.readouterr().out


def test_history_shows_observations_events_and_the_two_day_rule(
    cli, source: FakeSource, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli("collect") == EXIT_OK
    capsys.readouterr()

    assert cli("--json", "history", "1001") == EXIT_OK

    payload = captured_json(capsys)
    assert payload["observations"][0]["availability_state"] == "content_captured"
    assert {e["event_kind"] for e in payload["events"]} == {"first_observed", "listed"}
    assert payload["absence_evidence"]["meets_two_day_rule"] is False


def test_history_for_an_unknown_job_is_a_usage_error(
    cli, source: FakeSource, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli("collect") == EXIT_OK
    capsys.readouterr()
    assert cli("history", "999999") == EXIT_USAGE


def test_diff_says_so_when_there_is_only_one_version(
    cli, source: FakeSource, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli("collect") == EXIT_OK
    capsys.readouterr()

    assert cli("--json", "diff", "1001") == EXIT_OK

    payload = captured_json(capsys)
    assert payload["changed"] is False
    assert "fewer than two" in payload["detail"]


# ------------------------------------------------------------------- export


def test_csv_export_neutralises_formula_like_values(
    cli, source: FakeSource, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli("collect") == EXIT_OK
    capsys.readouterr()

    assert cli("export", "versions", "--format", "csv") == EXIT_OK

    rows = list(csv.reader(io.StringIO(capsys.readouterr().out)))
    header, data = rows[1], rows[2]
    assert data[header.index("title")] == "'" + FORMULA_TITLE


def test_json_export_keeps_the_source_value_untouched(
    cli, source: FakeSource, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli("collect") == EXIT_OK
    capsys.readouterr()

    assert cli("export", "versions", "--format", "json") == EXIT_OK

    payload = json.loads(capsys.readouterr().out)
    assert payload["rows"][0]["title"] == FORMULA_TITLE
    assert payload["provenance"]["dataset"] == "versions"


def test_export_writes_to_a_file_when_asked(
    cli, source: FakeSource, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli("collect") == EXIT_OK
    capsys.readouterr()
    destination = tmp_path / "exports" / "current.json"

    assert cli("export", "current", "--output", str(destination)) == EXIT_OK

    assert "wrote 1 rows" in capsys.readouterr().out
    assert json.loads(destination.read_text())["rows"][0]["external_job_id"] == "1001"


def test_an_unknown_dataset_is_a_usage_error(
    cli, source: FakeSource, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli("collect") == EXIT_OK
    capsys.readouterr()
    assert cli("export", "everything") == EXIT_USAGE
    assert "unknown dataset" in capsys.readouterr().out


# ------------------------------------------------- verify, backup, restore


def test_verify_checks_integrity_payloads_and_a_real_restore(
    cli, source: FakeSource, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli("collect") == EXIT_OK
    capsys.readouterr()

    assert cli("--json", "verify", "--restore") == EXIT_OK

    payload = captured_json(capsys)
    assert payload["integrity_check"] == ["ok"]
    assert payload["foreign_key_check"] == 0
    assert payload["payloads"]["ok"] is True
    assert payload["restore"]["ok"] is True
    assert all(c["passed"] for c in payload["restore"]["checks"])
    assert payload["ok"] is True


def test_backup_creates_a_verified_snapshot(
    cli, source: FakeSource, cfg: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    # Collecting without a backup leaves the archive unprotected, which is its
    # own exit code rather than a collection failure.
    assert cli("collect", "--no-backup") == EXIT_PROTECTION
    capsys.readouterr()

    assert cli("--json", "backup", "--kind", "manual") == EXIT_OK

    payload = captured_json(capsys)
    assert payload["state"] == "VERIFIED"
    assert Path(payload["path"]).exists()
    assert payload["manifest"]["sha256"]
    assert payload["status"]["offhost"]["state"] == "UNCONFIGURED"


def test_restore_refuses_to_overwrite_the_live_archive(
    cli, source: FakeSource, cfg: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli("collect") == EXIT_OK
    capsys.readouterr()

    assert cli("restore", str(cfg.layout.db_path)) == EXIT_USAGE
    assert "refusing to restore over the live archive" in capsys.readouterr().out


def test_restore_materialises_a_verified_copy_elsewhere(
    cli, source: FakeSource, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli("collect") == EXIT_OK
    capsys.readouterr()
    destination = tmp_path / "restored" / "rowanjobs.db"

    assert cli("--json", "restore", str(destination)) == EXIT_OK

    payload = captured_json(capsys)
    assert Path(payload["restored_to"]) == destination
    assert payload["verification"]["ok"] is True
    assert destination.exists()

    # A second restore to the same place refuses rather than overwriting.
    assert cli("restore", str(destination)) == EXIT_USAGE


# ---------------------------------------------------------------- reprocess


def test_reprocess_reports_offline_reinterpretation(
    cli, source: FakeSource, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli("collect") == EXIT_OK
    capsys.readouterr()
    requests_before = len(source.requests)

    assert cli("--json", "reprocess", "all") == EXIT_OK

    reports = captured_json(capsys)["reports"]
    assert {r["parser"] for r in reports} == {"pageup_detail", "pageup_listing"}
    assert all(r["extractions_created"] == 0 for r in reports)
    assert all("no network requests were made" in r["note"] for r in reports)
    assert len(source.requests) == requests_before


# ------------------------------------------------------------------- doctor


def test_doctor_reports_each_environment_check_consistently(
    cli, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli("--json", "doctor")

    payload = captured_json(capsys)
    names = {c["name"] for c in payload["checks"]}
    assert {"sqlite_runtime", "sqlite_wal_safe", "data_root", "schema", "foreign_keys"} <= names
    assert payload["ok"] == (not payload["failures"])
    assert code in (EXIT_OK, 1)


def test_the_version_flag_prints_and_exits_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert "rowanjobs 1.0.0" in capsys.readouterr().out


def test_a_missing_subcommand_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2  # argparse's own usage exit


def test_diff_reports_what_changed_between_two_content_versions(
    cli, source: FakeSource, capsys: pytest.CaptureFixture[str]
) -> None:
    source.add(
        DETAIL_URL,
        html_response(
            build_detail_page(
                job_id="1001", title=FORMULA_TITLE, body_html="<p>Edited wording.</p>"
            )
        ),
    )
    assert cli("collect") == EXIT_OK
    assert cli("collect") == EXIT_OK
    capsys.readouterr()

    assert cli("--json", "diff", "1001") == EXIT_OK

    payload = captured_json(capsys)
    assert payload["changed"]["description_text"] is True
    assert payload["changed"]["title"] is False
    import rowanjobs

    assert payload["comparison_lineage"] == {
        "parser_version": rowanjobs.PARSER_VERSION,
        "contract_version": rowanjobs.CONTRACT_VERSION,
        "text_contract_version": rowanjobs.TEXT_CONTRACT_VERSION,
    }
    assert any(line.startswith("+Edited wording.") for line in payload["unified_diff"])
    assert "cannot appear here as a source edit" in payload["note"]
    assert "comparison lineage" in payload["note"]


def test_doctor_reports_a_rejected_configuration_instead_of_crashing(tmp_path, capsys) -> None:
    """Finding a bad config is doctor's job, so it must survive one.

    A key removed or renamed by an upgrade would otherwise make every command,
    including the scheduled collection, fail with a traceback-shaped exit.
    """
    bad = tmp_path / "config.toml"
    bad.write_text("[network]\nnot_a_real_key = 1\n", encoding="utf-8")

    code = main(["--config", str(bad), "doctor"])

    assert code == 5
    out = capsys.readouterr().out
    assert "configuration: FAILED" in out
    assert "not_a_real_key" in out


def test_doctor_reports_a_rejected_configuration_as_json(tmp_path, capsys) -> None:
    bad = tmp_path / "config.toml"
    bad.write_text("[collection]\nmystery = true\n", encoding="utf-8")

    code = main(["doctor", "--config", str(bad), "--json"])

    assert code == 5
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["failures"] == ["configuration"]
    assert payload["checks"][0]["name"] == "configuration"


def test_every_command_in_the_documented_interface_is_reachable() -> None:
    """A subcommand that exists in the module but is never registered is invisible.

    cmd_diagnostics shipped unregistered once; this keeps the parser and the
    implemented commands in step.
    """
    import argparse

    import rowanjobs.cli as cli_module

    parser = cli_module.build_parser()
    registered: set[str] = set()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            registered |= set(action.choices)

    implemented = {
        name[len("cmd_") :].replace("_", "-") for name in dir(cli_module) if name.startswith("cmd_")
    }
    assert implemented <= registered, f"unregistered commands: {sorted(implemented - registered)}"
