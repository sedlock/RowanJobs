"""The release lifecycle's safety properties.

These tests exist because of one incident. On 2026-09-18 the systemd units
executed an editable install of this working tree, a configuration-schema
change landed without its config, and the next two scheduled collections died
before they could record anything. Nothing detected it.

So what is pinned here is not "the release tool works" but the narrower and
more important claim: **a bad candidate never becomes the running application,
and an interrupted preparation leaves the previous release in charge.**

The tool is standard-library-only and lives outside the package, so it is
loaded by path -- deliberately, since it must keep working when the package it
deploys does not.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

RELEASE_SCRIPT = Path(__file__).resolve().parents[1] / "ops" / "release.py"
FAKE_SHA = "a" * 40
OTHER_SHA = "b" * 40


def _load_release_module() -> Any:
    spec = importlib.util.spec_from_file_location("rowanjobs_release", RELEASE_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


release = _load_release_module()


@pytest.fixture
def layout(tmp_path: Path) -> Any:
    root = tmp_path / "app-releases" / "rowanjobs"
    instance = release.Layout(root)
    instance.ensure()
    return instance


def seal(path: Path, *, commit: str, schema_version: int = 7, ready: bool = True) -> Path:
    """A plausible release directory. Sealing is what makes it usable."""
    path.mkdir(parents=True, exist_ok=True)
    (path / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (path / ".venv" / "bin" / "rowanjobs").write_text("#!/bin/sh\nexit 0\n")
    manifest = {
        "schema": release.MANIFEST_SCHEMA,
        "app": "rowanjobs",
        "commit": commit,
        "schema_version": schema_version,
        "contained_in_origin_main": True,
        "build_status": "READY" if ready else "BUILDING",
    }
    (path / release.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    return path


# ------------------------------------------------------------------- sealing


def test_a_directory_without_a_ready_manifest_is_not_a_release(tmp_path: Path) -> None:
    rubble = tmp_path / FAKE_SHA
    rubble.mkdir()
    assert release.is_sealed(rubble) is False

    (rubble / release.MANIFEST_NAME).write_text("{not json")
    assert release.is_sealed(rubble) is False

    seal(rubble, commit=FAKE_SHA, ready=False)
    assert release.is_sealed(rubble) is False

    seal(rubble, commit=FAKE_SHA, ready=True)
    assert release.is_sealed(rubble) is True


def test_an_unsealed_release_can_never_be_activated(layout: Any, monkeypatch) -> None:
    """The interrupted-preparation case: a half-built tree is not a release."""
    target = layout.path_for(FAKE_SHA)
    seal(target, commit=FAKE_SHA, ready=False)
    monkeypatch.setattr(release, "systemctl", lambda *a, **k: None)

    with pytest.raises(release.ReleaseError, match="not a sealed READY release"):
        release.activate(layout, FAKE_SHA)

    # Nothing was pointed anywhere.
    assert layout.resolve(layout.current) is None


def test_a_build_that_fails_validation_leaves_no_release_behind(
    layout: Any, tmp_path: Path, monkeypatch
) -> None:
    """A candidate that cannot start must not survive as an activatable tree."""
    repo = tmp_path / "repo"
    repo.mkdir()

    monkeypatch.setattr(release, "resolve_commit", lambda *_a, **_k: FAKE_SHA)
    monkeypatch.setattr(release, "worktree_is_clean", lambda *_a: True)
    monkeypatch.setattr(release, "contained_in_remote", lambda *_a, **_k: True)

    def fake_export(argv, **kwargs):
        raise release.ReleaseError("export blew up half way through")

    monkeypatch.setattr(release, "run", fake_export)

    with pytest.raises(release.ReleaseError):
        release.build(layout, "HEAD", repo=repo)

    assert release.is_sealed(layout.path_for(FAKE_SHA)) is False
    assert not list(layout.root.glob(".building-*")), "staging debris was left behind"
    assert layout.resolve(layout.current) is None


# --------------------------------------------------------- the config check


def test_validation_rejects_a_candidate_that_cannot_read_the_deployed_config(
    tmp_path: Path, monkeypatch
) -> None:
    """The 2026-09-18 failure, as a test.

    New code met an old config file and exited 5 before recording anything.
    Nothing in the pipeline had ever asked whether the candidate could read the
    configuration the service would actually hand it.
    """
    stage = tmp_path / "candidate"
    (stage / ".venv" / "bin").mkdir(parents=True)
    binary = stage / release.BINARY_RELPATH
    binary.write_text("#!/bin/sh\nexit 0\n")

    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append([str(a) for a in argv])
        joined = " ".join(str(a) for a in argv)

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        if "load_config" in joined:
            Result.returncode = 1
            Result.stderr = "ValueError: unknown configuration key [notify] 'command'"
        elif "health" in joined:
            Result.stdout = json.dumps(
                {"schema_version": "controlpanel.status.v1", "overall": {"summary": "ok"}}
            )
        return Result()

    monkeypatch.setattr(release, "run", fake_run)
    report = release.validate_candidate(stage, tmp_path / "config.toml")

    assert report["ok"] is False
    failed = {c["name"] for c in report["checks"] if not c["ok"]}
    assert failed == {"deployed_config_loads"}
    assert any("load_config" in " ".join(c) for c in calls)


def test_validation_passes_only_when_every_check_passes(tmp_path: Path, monkeypatch) -> None:
    stage = tmp_path / "candidate"
    (stage / ".venv" / "bin").mkdir(parents=True)
    (stage / release.BINARY_RELPATH).write_text("#!/bin/sh\nexit 0\n")

    def fake_run(argv, **kwargs):
        class Result:
            returncode = 0
            stderr = ""
            stdout = json.dumps(
                {"schema_version": "controlpanel.status.v1", "overall": {"summary": "ok"}}
            )

        return Result()

    monkeypatch.setattr(release, "run", fake_run)
    report = release.validate_candidate(stage, tmp_path / "config.toml")
    assert report["ok"] is True
    assert {c["name"] for c in report["checks"]} == {
        "binary_present",
        "starts",
        "imports",
        "deployed_config_loads",
        "health_contract",
    }


# ------------------------------------------------------ schema compatibility


def test_activation_refuses_to_run_old_code_against_a_migrated_archive(
    layout: Any, monkeypatch
) -> None:
    """Rolling back code is not the same as rolling back a database.

    The archive only moves forward. Pointing the units at a release that
    predates the applied migrations would run code that cannot read its own
    evidence, which is worse than staying where we are.
    """
    good = seal(layout.path_for(OTHER_SHA), commit=OTHER_SHA, schema_version=7)
    release._swap(layout.current, good)
    old = seal(layout.path_for(FAKE_SHA), commit=FAKE_SHA, schema_version=5)

    monkeypatch.setattr(release, "systemctl", lambda *a, **k: None)
    monkeypatch.setattr(release, "_collection_in_flight", lambda *_a: False)
    monkeypatch.setattr(release, "live_schema_version", lambda *_a: 7)

    with pytest.raises(release.ReleaseError, match="schema v7"):
        release.activate(layout, FAKE_SHA)

    # Still on the working release.
    assert layout.resolve(layout.current) == OTHER_SHA
    assert old.exists()


def test_a_deliberate_schema_regression_is_possible_but_must_be_asked_for(
    layout: Any, monkeypatch
) -> None:
    seal(layout.path_for(FAKE_SHA), commit=FAKE_SHA, schema_version=5)
    monkeypatch.setattr(release, "systemctl", lambda *a, **k: None)
    monkeypatch.setattr(release, "_collection_in_flight", lambda *_a: False)
    monkeypatch.setattr(release, "live_schema_version", lambda *_a: 7)
    monkeypatch.setattr(release, "backup_database", lambda *a, **k: "backup-path")
    monkeypatch.setattr(release, "install_dropins", lambda *_a: [])
    monkeypatch.setattr(release, "remove_foreign_dropins", lambda: [])
    monkeypatch.setattr(release, "verify_units", lambda *_a: {"ok": True})
    monkeypatch.setattr(release, "timer_state", lambda: {})

    def fake_run(argv, **kwargs):
        class Result:
            returncode = 0
            stderr = ""
            stdout = json.dumps(
                {"schema_version": "controlpanel.status.v1", "overall": {"health": "healthy"}}
            )

        return Result()

    monkeypatch.setattr(release, "run", fake_run)
    entry = release.activate(layout, FAKE_SHA, allow_schema_regression=True)

    assert entry["result"] == "ok"
    assert layout.resolve(layout.current) == FAKE_SHA


# ------------------------------------------------------ failure and rollback


def test_a_failed_activation_restores_the_previous_release(layout: Any, monkeypatch) -> None:
    """An activation that breaks half way must not leave a broken runtime."""
    good = seal(layout.path_for(OTHER_SHA), commit=OTHER_SHA)
    release._swap(layout.current, good)
    seal(layout.path_for(FAKE_SHA), commit=FAKE_SHA)

    monkeypatch.setattr(release, "systemctl", lambda *a, **k: None)
    monkeypatch.setattr(release, "_collection_in_flight", lambda *_a: False)
    monkeypatch.setattr(release, "live_schema_version", lambda *_a: 7)
    monkeypatch.setattr(release, "backup_database", lambda *a, **k: None)
    monkeypatch.setattr(release, "remove_foreign_dropins", lambda: [])
    monkeypatch.setattr(release, "timer_state", lambda: {})
    installed: list[str] = []
    monkeypatch.setattr(release, "install_dropins", lambda path: installed.append(path.name) or [])
    # The new release migrates, gets wired up, and then fails verification.
    monkeypatch.setattr(release, "verify_units", lambda *_a: {"ok": False, "why": "wrong binary"})

    def fake_run(argv, **kwargs):
        class Result:
            returncode = 0
            stdout = "{}"
            stderr = ""

        return Result()

    monkeypatch.setattr(release, "run", fake_run)

    with pytest.raises(release.ReleaseError, match="unit verification failed"):
        release.activate(layout, FAKE_SHA)

    assert layout.resolve(layout.current) == OTHER_SHA, "did not restore the previous release"
    assert installed[-1] == OTHER_SHA, "drop-ins were not pointed back at the previous release"
    entries = [
        json.loads(line) for line in layout.journal_path.read_text().splitlines() if line.strip()
    ]
    assert entries[-1]["result"] == "failed"
    assert entries[-1]["restored_to"] == OTHER_SHA


def test_a_collection_in_flight_blocks_the_runtime_being_swapped(layout: Any, monkeypatch) -> None:
    seal(layout.path_for(FAKE_SHA), commit=FAKE_SHA)
    monkeypatch.setattr(release, "_collection_in_flight", lambda *_a: True)

    with pytest.raises(release.ReleaseError, match="refusing to switch the runtime"):
        release.activate(layout, FAKE_SHA, drain=0.0)

    assert layout.resolve(layout.current) is None


def test_the_collector_lock_is_what_decides_whether_a_run_is_in_flight(tmp_path: Path) -> None:
    import fcntl

    data_root = tmp_path / "data"
    (data_root / "runtime").mkdir(parents=True)
    lock = data_root / "runtime" / "collector.lock"

    assert release._collection_in_flight(data_root) is False  # no lock file yet

    lock.touch()
    assert release._collection_in_flight(data_root) is False  # present but unheld

    handle = os.open(str(lock), os.O_RDWR)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert release._collection_in_flight(data_root) is True
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)


# ------------------------------------------------------------------- basics


def test_repointing_current_never_leaves_it_missing(layout: Any) -> None:
    first = seal(layout.path_for(FAKE_SHA), commit=FAKE_SHA)
    second = seal(layout.path_for(OTHER_SHA), commit=OTHER_SHA)

    release._swap(layout.current, first)
    assert layout.current.resolve() == first.resolve()

    release._swap(layout.current, second)
    assert layout.current.resolve() == second.resolve()
    assert not list(layout.root.glob(".current.swap-*")), "a swap temporary was left behind"


def test_pruning_never_removes_what_is_running_or_what_we_would_roll_back_to(
    layout: Any,
) -> None:
    names = [f"{index:040x}" for index in range(1, 6)]
    for name in names:
        seal(layout.path_for(name), commit=name)
    release._swap(layout.current, layout.path_for(names[0]))
    release._swap(layout.previous, layout.path_for(names[1]))

    release.prune(layout, keep=1)

    assert layout.path_for(names[0]).exists(), "pruned the running release"
    assert layout.path_for(names[1]).exists(), "pruned the rollback target"


def test_an_unpushed_release_is_not_activated_by_accident(layout: Any, monkeypatch) -> None:
    target = layout.path_for(FAKE_SHA)
    seal(target, commit=FAKE_SHA)
    manifest = json.loads((target / release.MANIFEST_NAME).read_text())
    manifest["contained_in_origin_main"] = False
    (target / release.MANIFEST_NAME).write_text(json.dumps(manifest))

    with pytest.raises(release.ReleaseError, match="not contained in origin/main"):
        release.activate(layout, FAKE_SHA)
    assert layout.resolve(layout.current) is None


def test_status_reports_the_deployed_revision_separately_from_development(
    layout: Any, monkeypatch
) -> None:
    """What production runs is not what the working tree happens to contain."""
    seal(layout.path_for(FAKE_SHA), commit=FAKE_SHA)
    release._swap(layout.current, layout.path_for(FAKE_SHA))
    monkeypatch.setattr(release, "resolve_commit", lambda *_a: OTHER_SHA)
    monkeypatch.setattr(release, "worktree_is_clean", lambda *_a: False)
    monkeypatch.setattr(release, "verify_units", lambda *_a: {"ok": True})

    report = release.status(layout)

    assert report["deployed_commit"] == FAKE_SHA
    assert report["development_head"] == OTHER_SHA
    assert report["development_dirty"] is True
    assert report["development_matches_deployed"] is False
