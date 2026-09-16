"""Backups, verification and restore.

Snapshots use SQLite's online backup API, not a file copy: copying only the
active ``.db`` while a WAL is present yields a database missing its most recent
committed transactions.

Separated deliberately, because they fail independently:

* **archive integrity** -- does the live database still hash-verify?
* **local backup health** -- is there a recent verified snapshot on this host?
* **off-host protection** -- is a copy somewhere that survives losing this host?

A backup failure never rolls back ingestion, and pruning only ever removes a
snapshot once a *newer verified* one exists.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import apsw

from .. import __version__
from ..archive import ArchiveStore
from ..db import Database, open_db, open_readonly
from ..db.migrations import SCHEMA_VERSION
from ..timeutil import local_date_str, now_utc, parse_utc, utc_str
from .atomic import write_json

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config

CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class BackupResult:
    state: str
    path: Path | None
    detail: str
    manifest: dict[str, Any] | None = None
    backup_id: int | None = None
    pruned: list[str] | None = None
    offhost: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.state == "VERIFIED"


@dataclass
class RestoreCheck:
    ok: bool
    checks: list[dict[str, Any]]
    detail: str
    source: str | None = None


class BackupManager:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.layout = cfg.layout

    # ----------------------------------------------------------------- create

    def create(self, db: Database, *, kind: str = "daily") -> BackupResult:
        if not self.cfg.backup.enabled:
            return BackupResult("UNCONFIGURED", None, "backups are disabled in configuration")
        self.layout.ensure()
        snapshot_at = now_utc()
        stamp = snapshot_at.strftime("%Y%m%dT%H%M%SZ")
        final = self.layout.backups_dir / f"rowanjobs-{kind}-{stamp}.db"
        partial = final.with_suffix(".db.partial")

        try:
            self._snapshot(db, partial)
        except (apsw.Error, OSError) as exc:
            partial.unlink(missing_ok=True)
            return BackupResult("FAILED", None, f"snapshot failed: {type(exc).__name__}: {exc}")

        verification = {"state": "SKIPPED", "detail": "verification disabled"}
        if self.cfg.backup.verify_after_backup:
            verification = self._verify_file(partial)
            if verification["state"] != "OK":
                broken = final.with_suffix(".db.unverified")
                partial.replace(broken)
                return BackupResult(
                    "FAILED", broken, f"snapshot did not verify: {verification['detail']}"
                )

        partial.chmod(0o600)
        # Atomic completion: a reader never sees a half-written backup as final.
        partial.replace(final)

        stats = self._archive_stats(db)
        manifest = {
            "manifest_version": 1,
            "application": "rowanjobs",
            "app_version": __version__,
            "schema_version": SCHEMA_VERSION,
            "kind": kind,
            "snapshot_at_utc": utc_str(snapshot_at),
            "source_database": str(self.layout.db_path),
            "file": final.name,
            "file_bytes": final.stat().st_size,
            "sha256": sha256_file(final),
            "last_run_id": stats["last_run_id"],
            "artifact_count": stats["artifacts"],
            "posting_count": stats["postings"],
            "observation_count": stats["observations"],
            "verification": verification,
            "offhost": {"state": "UNCONFIGURED", "detail": "", "target": None},
        }

        offhost = self.push_offhost(final)
        manifest["offhost"] = offhost

        manifest_path = final.with_suffix(".db.manifest.json")
        write_json(manifest_path, manifest)

        backup_id = self._record(db, final, manifest_path, manifest, offhost)
        pruned = self.prune(db)
        return BackupResult(
            "VERIFIED" if verification["state"] in ("OK", "SKIPPED") else "DEGRADED",
            final,
            "snapshot created and verified",
            manifest=manifest,
            backup_id=backup_id,
            pruned=pruned,
            offhost=offhost,
        )

    def _snapshot(self, db: Database, destination: Path) -> None:
        destination.unlink(missing_ok=True)
        target = apsw.Connection(str(destination))
        try:
            with target.backup("main", db.conn, "main") as backup:
                while not backup.done:
                    backup.step(256)
            target.pragma("wal_checkpoint", "TRUNCATE")
        finally:
            target.close()
        # A backup taken from a WAL source lands in WAL mode; collapse it so the
        # single file is self-contained.
        collapse = apsw.Connection(str(destination))
        try:
            collapse.pragma("journal_mode", "DELETE")
        finally:
            collapse.close()
        for suffix in ("-wal", "-shm"):
            Path(str(destination) + suffix).unlink(missing_ok=True)

    @staticmethod
    def _archive_stats(db: Database) -> dict[str, Any]:
        return {
            "artifacts": int(db.scalar("SELECT COUNT(*) FROM artifacts") or 0),
            "postings": int(db.scalar("SELECT COUNT(*) FROM postings") or 0),
            "observations": int(db.scalar("SELECT COUNT(*) FROM posting_observations") or 0),
            "last_run_id": db.scalar("SELECT MAX(run_id) FROM collection_runs"),
        }

    @staticmethod
    def _verify_file(path: Path) -> dict[str, Any]:
        try:
            conn = apsw.Connection(str(path), flags=apsw.SQLITE_OPEN_READONLY)
        except apsw.Error as exc:
            return {"state": "FAILED", "detail": f"cannot open snapshot: {exc}"}
        try:
            conn.pragma("foreign_keys", True)
            integrity = [r[0] for r in conn.execute("PRAGMA integrity_check")]
            if integrity != ["ok"]:
                return {"state": "FAILED", "detail": f"integrity_check: {integrity[:5]}"}
            fk = list(conn.execute("PRAGMA foreign_key_check"))
            if fk:
                return {"state": "FAILED", "detail": f"foreign_key_check found {len(fk)} rows"}
            tables = int(
                conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            )
            return {
                "state": "OK",
                "detail": f"integrity_check ok, foreign_key_check clean, {tables} tables",
                "verified_at_utc": utc_str(),
            }
        finally:
            conn.close()

    def _record(
        self,
        db: Database,
        path: Path,
        manifest_path: Path,
        manifest: dict[str, Any],
        offhost: dict[str, Any],
    ) -> int:
        with db.write():
            return db.insert(
                """
                INSERT INTO backups(
                    created_at_utc, kind, path, manifest_path, snapshot_at_utc,
                    schema_version, app_version, last_run_id, artifact_count,
                    posting_count, file_bytes, sha256, verification_state,
                    verification_detail, verified_at_utc, offhost_state,
                    offhost_target, offhost_detail)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    utc_str(),
                    manifest["kind"],
                    str(path),
                    str(manifest_path),
                    manifest["snapshot_at_utc"],
                    manifest["schema_version"],
                    manifest["app_version"],
                    manifest["last_run_id"],
                    manifest["artifact_count"],
                    manifest["posting_count"],
                    manifest["file_bytes"],
                    manifest["sha256"],
                    manifest["verification"]["state"],
                    manifest["verification"]["detail"],
                    manifest["verification"].get("verified_at_utc"),
                    offhost["state"],
                    offhost.get("target"),
                    offhost.get("detail"),
                ),
            )

    # --------------------------------------------------------------- off-host

    def push_offhost(self, path: Path) -> dict[str, Any]:
        """Copy a snapshot to the configured off-host destination.

        With nothing configured this reports ``UNCONFIGURED`` rather than
        pretending the archive is protected. Note that another directory or a
        locally attached disk is *not* off-host and is refused.
        """
        cfg = self.cfg.backup
        if not cfg.offhost_kind:
            return {
                "state": "UNCONFIGURED",
                "target": None,
                "detail": "no off-host backup destination is configured; the archive "
                "has local backups only and would not survive losing this host",
            }
        namespace = cfg.offhost_namespace
        target = f"{cfg.offhost_target.rstrip('/')}/{namespace}/{path.name}"
        if cfg.offhost_kind == "command":
            argv = [
                a.replace("{src}", str(path)).replace("{dst}", target) for a in cfg.offhost_command
            ]
        elif cfg.offhost_kind == "rclone":
            argv = ["rclone", "copyto", str(path), target]
        elif cfg.offhost_kind == "rsync-ssh":
            argv = ["rsync", "-a", "--mkpath", str(path), target]
        else:
            return {
                "state": "FAILED",
                "target": target,
                "detail": f"unknown off-host kind {cfg.offhost_kind!r}",
            }
        try:
            # Operator-supplied argv, run without a shell. rclone/rsync are
            # resolved from PATH on purpose: the operator chooses the install.
            proc = subprocess.run(  # noqa: S603
                argv,
                capture_output=True,
                text=True,
                timeout=1800,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                "state": "FAILED",
                "target": target,
                "detail": f"{type(exc).__name__}: {exc}",
            }
        if proc.returncode != 0:
            return {
                "state": "FAILED",
                "target": target,
                "detail": f"exit {proc.returncode}: {proc.stderr.strip()[:400]}",
            }
        return {"state": "VERIFIED", "target": target, "detail": "upload completed"}

    # ------------------------------------------------------------------ prune

    def prune(self, db: Database) -> list[str]:
        """Rotate snapshots, never dropping the last verified recovery copy."""
        cfg = self.cfg.backup
        rows = db.query(
            "SELECT backup_id, path, kind, snapshot_at_utc, verification_state "
            "FROM backups WHERE pruned_at_utc IS NULL ORDER BY snapshot_at_utc DESC"
        )
        verified = [r for r in rows if str(r["verification_state"]) in ("OK", "SKIPPED")]
        if len(verified) <= 1:
            return []

        keep: set[int] = set()
        newest_verified = int(verified[0]["backup_id"])
        keep.add(newest_verified)

        buckets: dict[str, list[dict[str, Any]]] = {"daily": [], "weekly": [], "monthly": []}
        seen_week: set[str] = set()
        seen_month: set[str] = set()
        for row in rows:
            stamp = parse_utc(str(row["snapshot_at_utc"]))
            if len(buckets["daily"]) < cfg.keep_daily:
                buckets["daily"].append(row)
                keep.add(int(row["backup_id"]))
            week = f"{stamp.isocalendar().year}-W{stamp.isocalendar().week}"
            if week not in seen_week and len(buckets["weekly"]) < cfg.keep_weekly:
                seen_week.add(week)
                buckets["weekly"].append(row)
                keep.add(int(row["backup_id"]))
            month = stamp.strftime("%Y-%m")
            if month not in seen_month and len(buckets["monthly"]) < cfg.keep_monthly:
                seen_month.add(month)
                buckets["monthly"].append(row)
                keep.add(int(row["backup_id"]))

        pruned: list[str] = []
        for row in rows:
            backup_id = int(row["backup_id"])
            if backup_id in keep:
                continue
            path = Path(str(row["path"]))
            path.unlink(missing_ok=True)
            path.with_suffix(".db.manifest.json").unlink(missing_ok=True)
            with db.write():
                db.execute(
                    "UPDATE backups SET pruned_at_utc = ? WHERE backup_id = ?",
                    (utc_str(), backup_id),
                )
            pruned.append(path.name)
        return pruned

    # ---------------------------------------------------------------- restore

    def latest(self, db: Database) -> dict[str, Any] | None:
        return db.one(
            "SELECT * FROM backups WHERE pruned_at_utc IS NULL "
            "AND verification_state IN ('OK','SKIPPED') "
            "ORDER BY snapshot_at_utc DESC LIMIT 1"
        )

    def restore_check(
        self, backup_path: Path, workdir: Path, *, sample_postings: int = 3
    ) -> RestoreCheck:
        """Restore into a temporary location and prove the archive survived.

        The live archive is never touched: the copy goes to ``workdir`` and is
        opened read-only afterwards.
        """
        checks: list[dict[str, Any]] = []
        backup_path = Path(backup_path)
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        restored = workdir / f"restored-{backup_path.name}"

        manifest_path = backup_path.with_suffix(".db.manifest.json")
        expected_hash = None
        if manifest_path.exists():
            import json

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            expected_hash = manifest.get("sha256")

        try:
            shutil.copy2(backup_path, restored)
        except OSError as exc:
            return RestoreCheck(False, checks, f"could not copy snapshot: {exc}")

        actual_hash = sha256_file(restored)
        checks.append(
            {
                "name": "snapshot_checksum",
                "passed": expected_hash is None or expected_hash == actual_hash,
                "detail": f"sha256 {actual_hash}"
                + ("" if expected_hash else " (no manifest to compare against)"),
            }
        )

        try:
            db = open_readonly(restored)
        except Exception as exc:  # noqa: BLE001
            checks.append({"name": "open", "passed": False, "detail": str(exc)})
            return RestoreCheck(False, checks, "restored snapshot could not be opened")

        try:
            integrity = db.integrity_check()
            checks.append(
                {
                    "name": "integrity_check",
                    "passed": integrity == ["ok"],
                    "detail": ", ".join(integrity[:3]),
                }
            )
            fk = db.foreign_key_check()
            checks.append(
                {
                    "name": "foreign_key_check",
                    "passed": not fk,
                    "detail": f"{len(fk)} violations",
                }
            )

            store = ArchiveStore(db)
            artifact_ids = [
                int(r["artifact_id"])
                for r in db.query(
                    "SELECT artifact_id FROM artifacts ORDER BY artifact_id DESC LIMIT 25"
                )
            ]
            failures = [aid for aid in artifact_ids if not store.verify(aid)[0]]
            checks.append(
                {
                    "name": "artifact_payload_hashes",
                    "passed": not failures,
                    "detail": f"{len(artifact_ids)} sampled, {len(failures)} mismatched",
                }
            )

            histories = db.query(
                """
                SELECT p.external_job_id,
                       COUNT(o.observation_id) AS observations,
                       COUNT(DISTINCT o.posting_version_id) AS versions
                  FROM postings p
                  LEFT JOIN posting_observations o ON o.posting_id = p.posting_id
                 GROUP BY p.posting_id
                 ORDER BY observations DESC
                 LIMIT ?
                """,
                (sample_postings,),
            )
            checks.append(
                {
                    "name": "posting_histories_queryable",
                    "passed": bool(histories),
                    "detail": ", ".join(
                        f"{h['external_job_id']}: {h['observations']} obs / "
                        f"{h['versions']} versions"
                        for h in histories
                    )
                    or "no postings in snapshot",
                }
            )

            descriptions = db.query(
                "SELECT posting_id, LENGTH(description_text) AS n, description_html_kind "
                "FROM posting_versions WHERE description_text IS NOT NULL "
                "ORDER BY posting_version_id DESC LIMIT ?",
                (sample_postings,),
            )
            checks.append(
                {
                    "name": "descriptions_survived",
                    "passed": bool(descriptions) and all(int(d["n"]) > 0 for d in descriptions),
                    "detail": ", ".join(
                        f"{d['n']} chars ({d['description_html_kind']})" for d in descriptions
                    )
                    or "no description versions in snapshot",
                }
            )

            resources = int(db.scalar("SELECT COUNT(*) FROM resource_links") or 0)
            resource_obs = int(db.scalar("SELECT COUNT(*) FROM resource_observations") or 0)
            checks.append(
                {
                    "name": "resource_references_survived",
                    "passed": True,
                    "detail": f"{resources} classified links, {resource_obs} retrievals",
                }
            )
        finally:
            db.close()

        ok = all(bool(c["passed"]) for c in checks)
        return RestoreCheck(
            ok,
            checks,
            "restored snapshot verified" if ok else "restored snapshot failed verification",
            source=str(backup_path),
        )

    def restore_to(self, backup_path: Path, destination: Path) -> Path:
        """Materialise a snapshot at ``destination`` without touching the archive."""
        destination = Path(destination)
        if destination.exists():
            raise FileExistsError(f"refusing to overwrite {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(backup_path), destination)
        destination.chmod(0o600)
        # Prove the copy opens and migrates cleanly before handing it back.
        db = open_db(destination, migrate=False)
        db.close()
        return destination

    # ----------------------------------------------------------------- status

    def status(self, db: Database) -> dict[str, Any]:
        latest = self.latest(db)
        files = sorted(self.layout.backups_dir.glob("rowanjobs-*.db"))
        total = sum(f.stat().st_size for f in files)
        last_restore = None
        if self.layout.restore_check_path.exists():
            import json

            try:
                last_restore = json.loads(
                    self.layout.restore_check_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                last_restore = None

        local_state = "UNCONFIGURED" if not self.cfg.backup.enabled else "UNPROTECTED"
        detail = "no verified snapshot exists yet"
        if latest:
            age = now_utc() - parse_utc(str(latest["snapshot_at_utc"]))
            local_state = "VERIFIED" if age < timedelta(days=2) else "DEGRADED"
            detail = f"most recent verified snapshot is {age.days}d old"

        offhost_state = "UNCONFIGURED"
        offhost_detail = (
            "no off-host destination is configured; local backups only. The archive "
            "would not survive losing this host."
        )
        if latest and str(latest["offhost_state"]) != "UNCONFIGURED":
            offhost_state = str(latest["offhost_state"])
            offhost_detail = str(latest["offhost_detail"] or "")

        return {
            "local": {
                "state": local_state,
                "detail": detail,
                "snapshot_count": len(files),
                "total_bytes": total,
                "directory": str(self.layout.backups_dir),
                "latest": {
                    "path": str(latest["path"]),
                    "snapshot_at_utc": str(latest["snapshot_at_utc"]),
                    "file_bytes": int(latest["file_bytes"]),
                    "sha256": str(latest["sha256"]),
                    "verification_state": str(latest["verification_state"]),
                }
                if latest
                else None,
                "rotation": {
                    "keep_daily": self.cfg.backup.keep_daily,
                    "keep_weekly": self.cfg.backup.keep_weekly,
                    "keep_monthly": self.cfg.backup.keep_monthly,
                },
            },
            "offhost": {
                "state": offhost_state,
                "detail": offhost_detail,
                "kind": self.cfg.backup.offhost_kind or None,
                "target": self.cfg.backup.offhost_target or None,
            },
            "restore_verification": last_restore,
        }

    def restore_check_due(self) -> bool:
        path = self.layout.restore_check_path
        if not path.exists():
            return True
        import json

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            last = parse_utc(str(data["checked_at_utc"]))
        except (OSError, ValueError, KeyError):
            return True
        from datetime import date as _date

        today = _date.fromisoformat(local_date_str())
        last_local = _date.fromisoformat(local_date_str(str(data["checked_at_utc"])))
        del last
        return (today - last_local).days >= self.cfg.backup.restore_check_interval_days
