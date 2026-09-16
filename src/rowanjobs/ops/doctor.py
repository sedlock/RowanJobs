"""Pre-flight and environment diagnosis."""

from __future__ import annotations

import os
import shutil
import socket
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import __version__
from ..db import Database, open_db
from ..db.migrations import SCHEMA_VERSION, current_version, pending
from ..db.runtime import runtime_info
from ..net.browser import ChallengeSolver
from ..timeutil import local_str, utc_str
from .backup import BackupManager
from .notify import Notifier
from .schedule import calendar_expression, lingering_enabled, timer_status, validate_calendar

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config


def _check(name: str, ok: bool | None, detail: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail, **extra}


def run_doctor(cfg: Config) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    layout = cfg.layout

    checks.append(
        _check(
            "host",
            True,
            f"{socket.gethostname()} as {os.environ.get('USER') or os.getuid()}",
        )
    )
    checks.append(_check("python", True, sys.version.split()[0], executable=sys.executable))

    rt = runtime_info()
    checks.append(
        _check(
            "sqlite_runtime",
            True,
            f"{rt.provider} {rt.provider_version} with SQLite {rt.sqlite_version}",
            source_id=rt.sqlite_source_id,
        )
    )
    checks.append(_check("sqlite_wal_safe", rt.wal_safe, rt.wal_evidence))

    try:
        layout.ensure()
        checks.append(_check("data_root", True, str(layout.data_root)))
    except OSError as exc:
        checks.append(_check("data_root", False, f"{layout.data_root}: {exc}"))

    mode = None
    if layout.data_root.exists():
        mode = oct(layout.data_root.stat().st_mode & 0o777)
        checks.append(
            _check(
                "data_root_permissions",
                mode == "0o700",
                f"{layout.data_root} is {mode} (expected 0o700)",
            )
        )

    disk = shutil.disk_usage(layout.data_root)
    free_gb = disk.free / 1024**3
    checks.append(
        _check(
            "disk_space",
            free_gb > 1.0,
            f"{free_gb:.1f} GiB free on {layout.data_root}",
            free_bytes=disk.free,
        )
    )

    db: Database | None = None
    try:
        db = open_db(layout.db_path, migrate=False)
        version = current_version(db)
        outstanding = [m[0] for m in pending(db)]
        checks.append(
            _check(
                "schema",
                not outstanding,
                f"schema at v{version}, expected v{SCHEMA_VERSION}"
                + (f"; pending migrations {outstanding}" if outstanding else ""),
            )
        )
        checks.append(
            _check(
                "journal_mode",
                db.journal_mode == ("wal" if rt.wal_safe else "delete"),
                f"{db.journal_mode} on {db.filesystem}"
                + (f" ({db.wal_deviation})" if db.wal_deviation else ""),
            )
        )
        fk = int(db.conn.pragma("foreign_keys") or 0)
        checks.append(_check("foreign_keys", fk == 1, f"PRAGMA foreign_keys = {fk}"))
        sync = int(db.conn.pragma("synchronous") or -1)
        checks.append(_check("synchronous", sync == 2, f"PRAGMA synchronous = {sync} (2 = FULL)"))
        integrity = db.integrity_check()
        checks.append(_check("integrity_check", integrity == ["ok"], ", ".join(integrity[:3])))
        backups = BackupManager(cfg)
        status = backups.status(db)
        checks.append(
            _check(
                "local_backup",
                status["local"]["state"] in ("VERIFIED", "UNCONFIGURED"),
                f"{status['local']['state']}: {status['local']['detail']}",
            )
        )
        checks.append(
            _check(
                "offhost_backup",
                None
                if status["offhost"]["state"] == "UNCONFIGURED"
                else status["offhost"]["state"] == "VERIFIED",
                f"{status['offhost']['state']}: {status['offhost']['detail']}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("database", False, f"{type(exc).__name__}: {exc}"))
    finally:
        if db is not None:
            db.close()

    lock = layout.lock_path
    checks.append(
        _check(
            "collector_lock",
            True,
            f"{lock} ({'present' if lock.exists() else 'not yet created'})",
        )
    )

    expression = calendar_expression(cfg)
    validation = validate_calendar(expression)
    checks.append(
        _check(
            "calendar_expression",
            validation.get("valid"),
            f"{expression} -> {validation.get('detail', '')[:200]}",
        )
    )
    timer = timer_status(cfg)
    checks.append(
        _check(
            "timer_installed",
            timer["units"].get("rowanjobs.timer", {}).get("installed", False),
            f"next activation {timer.get('next_collection_local') or 'unknown'}",
        )
    )
    linger = lingering_enabled()
    checks.append(
        _check(
            "lingering",
            linger["enabled"],
            linger["detail"]
            + ("" if linger["enabled"] else "; unattended execution would not work"),
        )
    )

    solver = ChallengeSolver(enabled=cfg.browser.enabled)
    checks.append(
        _check(
            "browser_fallback",
            None,
            "available" if solver.available() else (solver.last_error or "disabled"),
        )
    )

    notify = Notifier(cfg.notify).status()
    checks.append(
        _check(
            "notifications",
            None if notify["state"] == "UNCONFIGURED" else True,
            f"{notify['state']}: {notify['detail']}",
        )
    )

    failures = [c for c in checks if c["ok"] is False]
    return {
        "app_version": __version__,
        "checked_at_utc": utc_str(),
        "checked_at_local": local_str(utc_str()),
        "config_path": str(cfg.config_path) if cfg.config_path else None,
        "ok": not failures,
        "failures": [c["name"] for c in failures],
        "checks": checks,
    }


def deployment_manifest(cfg: Config, *, revision: str | None, dirty: bool | None) -> dict[str, Any]:
    layout = cfg.layout
    return {
        "recorded_at_utc": utc_str(),
        "recorded_at_local": local_str(utc_str()),
        "app_version": __version__,
        "app_revision": revision,
        "git_dirty": dirty,
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "sqlite_runtime": runtime_info().as_dict(),
        "host": socket.gethostname(),
        "os_user": os.environ.get("USER") or os.environ.get("LOGNAME") or "",
        "install_path": str(Path(__file__).resolve().parents[3]),
        "data_root": str(layout.data_root),
        "database": str(layout.db_path),
        "config": str(cfg.config_path) if cfg.config_path else None,
        "units": ["rowanjobs.service", "rowanjobs.timer", "rowanjobs-retry.timer"],
        "schedule": calendar_expression(cfg),
    }
