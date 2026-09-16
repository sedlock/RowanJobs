"""Backups, verification and restore.

Archive integrity, local backup health and off-host protection fail
independently and are reported separately. A backup failure never rolls back
ingestion, and a restore is proved by querying the restored copy.
"""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rowanjobs.config import Config
from rowanjobs.db import Database, open_db, open_readonly
from rowanjobs.ops.backup import BackupManager, sha256_file

from .conftest import LISTING_URL, FakeSource, build_detail_page, build_listing_page, listing_job


@pytest.fixture
def populated(collect, db: Database) -> Database:
    """A database with one real collection in it."""
    source = FakeSource()
    source.page(LISTING_URL, build_listing_page([listing_job("1001", "Archivist", slug="a")]))
    source.page(
        "https://jobs.rowan.edu/en-us/job/1001/a",
        build_detail_page(job_id="1001", title="Archivist"),
    )
    result = collect(source)
    assert result.outcome == "success"
    return db


def test_a_snapshot_is_created_verified_and_recorded(
    cfg: Config, populated: Database, tmp_path: Path
) -> None:
    manager = BackupManager(cfg)

    result = manager.create(populated, kind="manual")

    assert result.state == "VERIFIED"
    assert result.ok is True
    assert result.path is not None
    assert result.path.exists()
    assert result.path.parent == cfg.layout.backups_dir
    assert stat.S_IMODE(result.path.stat().st_mode) == 0o600

    manifest_path = result.path.with_suffix(".db.manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["sha256"] == sha256_file(result.path)
    assert manifest["artifact_count"] == int(populated.scalar("SELECT COUNT(*) FROM artifacts"))
    assert manifest["posting_count"] == 1
    assert manifest["verification"]["state"] == "OK"
    assert manifest["offhost"]["state"] == "UNCONFIGURED"

    row = populated.one("SELECT * FROM backups WHERE backup_id = ?", (result.backup_id,))
    assert row["verification_state"] == "OK"
    assert row["kind"] == "manual"
    assert row["sha256"] == manifest["sha256"]
    # No leftover partial file.
    assert list(cfg.layout.backups_dir.glob("*.partial")) == []


def test_the_snapshot_is_a_self_contained_queryable_copy(cfg: Config, populated: Database) -> None:
    result = BackupManager(cfg).create(populated)
    assert result.path is not None

    restored = open_readonly(result.path)
    try:
        assert restored.integrity_check() == ["ok"]
        assert int(restored.scalar("SELECT COUNT(*) FROM postings")) == 1
        assert restored.scalar("SELECT title FROM posting_versions LIMIT 1") == "Archivist"
    finally:
        restored.close()
    assert not Path(str(result.path) + "-wal").exists()


def test_an_unwritable_backup_destination_fails_without_touching_the_archive(
    cfg: Config, populated: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    postings_before = int(populated.scalar("SELECT COUNT(*) FROM postings"))
    artifacts_before = int(populated.scalar("SELECT COUNT(*) FROM artifacts"))

    def unwritable(_self: BackupManager, _db: Database, destination: Path) -> None:
        raise OSError(28, "No space left on device", str(destination))

    monkeypatch.setattr(BackupManager, "_snapshot", unwritable)

    result = BackupManager(cfg).create(populated, kind="daily")

    assert result.state == "FAILED"
    assert result.ok is False
    assert "snapshot failed" in result.detail
    assert "No space left on device" in result.detail
    assert result.backup_id is None
    # Ingested evidence is untouched and still queryable.
    assert int(populated.scalar("SELECT COUNT(*) FROM postings")) == postings_before
    assert int(populated.scalar("SELECT COUNT(*) FROM artifacts")) == artifacts_before
    assert int(populated.scalar("SELECT COUNT(*) FROM backups")) == 0
    assert populated.integrity_check() == ["ok"]
    assert list(cfg.layout.backups_dir.glob("*")) == []


def test_a_snapshot_that_fails_verification_is_never_offered_as_a_backup(
    cfg: Config, populated: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        BackupManager,
        "_verify_file",
        staticmethod(lambda _path: {"state": "FAILED", "detail": "integrity_check: ['corrupt']"}),
    )

    result = BackupManager(cfg).create(populated, kind="daily")

    assert result.state == "FAILED"
    assert "did not verify" in result.detail
    assert result.path is not None
    assert result.path.name.endswith(".db.unverified")
    assert int(populated.scalar("SELECT COUNT(*) FROM backups")) == 0
    assert BackupManager(cfg).latest(populated) is None


def test_create_restores_the_expected_permissions_on_the_backup_tree(
    cfg: Config, populated: Database
) -> None:
    """The layout is self-healing, which is why a chmod alone cannot break it."""
    cfg.layout.ensure()
    cfg.layout.backups_dir.chmod(0o500)

    result = BackupManager(cfg).create(populated)

    assert result.state == "VERIFIED"
    assert stat.S_IMODE(cfg.layout.backups_dir.stat().st_mode) == 0o700


def test_backups_can_be_switched_off_without_pretending_to_have_run(
    cfg: Config, populated: Database
) -> None:
    cfg.backup.enabled = False
    result = BackupManager(cfg).create(populated)
    assert result.state == "UNCONFIGURED"
    assert result.path is None
    assert "disabled" in result.detail


def test_restore_check_proves_the_archive_survived(
    cfg: Config, populated: Database, tmp_path: Path
) -> None:
    manager = BackupManager(cfg)
    created = manager.create(populated)
    assert created.path is not None

    check = manager.restore_check(created.path, tmp_path / "restore-work")

    assert check.ok is True, check.checks
    names = {c["name"]: c for c in check.checks}
    assert set(names) == {
        "snapshot_checksum",
        "integrity_check",
        "foreign_key_check",
        "artifact_payload_hashes",
        "posting_histories_queryable",
        "descriptions_survived",
        "resource_references_survived",
    }
    assert all(c["passed"] for c in check.checks)
    assert check.source == str(created.path)
    # The live archive was never opened for writing by the check.
    assert (tmp_path / "restore-work" / f"restored-{created.path.name}").exists()


def test_restore_check_fails_loudly_when_the_snapshot_does_not_match_its_manifest(
    cfg: Config, populated: Database, tmp_path: Path
) -> None:
    manager = BackupManager(cfg)
    created = manager.create(populated)
    assert created.path is not None
    manifest_path = created.path.with_suffix(".db.manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    check = manager.restore_check(created.path, tmp_path / "restore-work")

    assert check.ok is False
    checksum = next(c for c in check.checks if c["name"] == "snapshot_checksum")
    assert checksum["passed"] is False


def test_restore_to_refuses_to_overwrite_an_existing_file(
    cfg: Config, populated: Database, tmp_path: Path
) -> None:
    manager = BackupManager(cfg)
    created = manager.create(populated)
    assert created.path is not None
    destination = tmp_path / "restored.db"

    restored = manager.restore_to(created.path, destination)
    assert restored == destination
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        manager.restore_to(created.path, destination)


def test_restore_to_produces_a_working_archive(
    cfg: Config, populated: Database, tmp_path: Path
) -> None:
    manager = BackupManager(cfg)
    created = manager.create(populated)
    assert created.path is not None

    restored = manager.restore_to(created.path, tmp_path / "elsewhere" / "rowanjobs.db")

    handle = open_db(restored, migrate=False)
    try:
        assert int(handle.scalar("SELECT COUNT(*) FROM postings")) == 1
        assert handle.integrity_check() == ["ok"]
    finally:
        handle.close()


def test_pruning_never_removes_the_only_verified_snapshot(cfg: Config, populated: Database) -> None:
    manager = BackupManager(cfg)
    created = manager.create(populated)
    assert manager.prune(populated) == []
    assert created.path is not None
    assert created.path.exists()


def test_status_separates_local_backup_health_from_offhost_protection(
    cfg: Config, populated: Database
) -> None:
    manager = BackupManager(cfg)
    before = manager.status(populated)
    assert before["local"]["state"] == "UNPROTECTED"
    assert before["local"]["latest"] is None
    assert before["offhost"]["state"] == "UNCONFIGURED"
    assert "would not survive losing this host" in before["offhost"]["detail"]

    manager.create(populated)
    after = manager.status(populated)
    assert after["local"]["state"] == "VERIFIED"
    assert after["local"]["snapshot_count"] == 1
    assert after["local"]["latest"]["verification_state"] == "OK"
    # A local snapshot never upgrades off-host protection.
    assert after["offhost"]["state"] == "UNCONFIGURED"


def test_an_unknown_offhost_kind_is_reported_as_failed_not_ignored(
    cfg: Config, populated: Database, tmp_path: Path
) -> None:
    cfg.backup.offhost_kind = "carrier-pigeon"
    cfg.backup.offhost_target = "somewhere:/backups"
    result = BackupManager(cfg).push_offhost(tmp_path / "snapshot.db")
    assert result["state"] == "FAILED"
    assert "carrier-pigeon" in result["detail"]


def test_offhost_push_uses_the_configured_command_and_reports_its_failure(
    cfg: Config, tmp_path: Path
) -> None:
    cfg.backup.offhost_kind = "command"
    cfg.backup.offhost_target = "vault:/rowanjobs"
    cfg.backup.offhost_command = ("false", "{src}", "{dst}")
    result = BackupManager(cfg).push_offhost(tmp_path / "snapshot.db")
    assert result["state"] == "FAILED"
    assert result["target"].endswith("snapshot.db")


def test_restore_check_is_due_when_it_has_never_run(cfg: Config) -> None:
    cfg.layout.ensure()
    assert BackupManager(cfg).restore_check_due() is True


def test_pruning_rotates_older_snapshots_once_a_newer_verified_one_exists(
    cfg: Config, populated: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg.backup.keep_daily = 1
    cfg.backup.keep_weekly = 1
    cfg.backup.keep_monthly = 1
    manager = BackupManager(cfg)
    created = []
    for month in (7, 8, 9):
        stamp = datetime(2026, month, 1, 6, 15, 0, tzinfo=UTC)
        monkeypatch.setattr("rowanjobs.ops.backup.now_utc", lambda stamp=stamp: stamp)
        result = manager.create(populated, kind="daily")
        assert result.state == "VERIFIED"
        created.append(result.path)

    newest = created[-1]
    assert newest is not None
    assert newest.exists()
    for older in created[:-1]:
        assert older is not None
        assert not older.exists()
        assert not older.with_suffix(".db.manifest.json").exists()
    rows = {
        Path(str(r["path"])).name: r["pruned_at_utc"]
        for r in populated.query("SELECT path, pruned_at_utc FROM backups")
    }
    assert rows[newest.name] is None
    assert all(rows[p.name] is not None for p in created[:-1] if p)
    assert manager.latest(populated)["path"] == str(newest)
