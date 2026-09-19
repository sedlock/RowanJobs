#!/usr/bin/env python3
"""RowanJobs' immutable release lifecycle.

Until now the systemd units executed `/mnt/bench/src/RowanJobs/.venv/bin/rowanjobs`,
an *editable* install pointing at the working tree. Every half-finished edit in
this checkout was instantly the production application, and the daily 06:15
collection would run whatever happened to be on disk at 06:15. On 2026-09-18
that is exactly what went wrong: a configuration-schema change landed in the
tree without the deployed config, and the next two scheduled activations died
before they could record anything.

So: the working tree is for development, and production runs a sealed,
read-only release directory named for its commit.

    /mnt/bench/app-releases/rowanjobs/<sha>/     sealed tree + its own .venv
    /mnt/bench/app-releases/rowanjobs/current    -> the release the units run
    /mnt/bench/app-releases/rowanjobs/previous   -> the rollback target
    .activation-journal.jsonl                    what was activated, when, why

This is the same shape Feeder uses on this host, deliberately, so there is one
pattern to understand rather than two. RowanJobs owns this: ControlPanel
observes the result and never repoints the symlink.

Standard library only, so the release tool never depends on the environment it
is building -- a broken candidate must still be able to roll back.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

APP = "rowanjobs"
CANONICAL_REPO = Path("/mnt/bench/src/RowanJobs")
DEFAULT_ROOT = Path(os.environ.get("ROWANJOBS_RELEASE_ROOT", "/mnt/bench/app-releases/rowanjobs"))

MANIFEST_SCHEMA = "rowanjobs.release.v1"
MANIFEST_NAME = "release.json"
VENV_RELPATH = ".venv"
BINARY_RELPATH = f"{VENV_RELPATH}/bin/{APP}"

#: Units whose ExecStart must point into the release.
EXEC_SERVICES = {
    "rowanjobs": "--json collect --kind daily",
    "rowanjobs-retry": "--json retry",
    "rowanjobs-notify": "--json notify",
}
TIMERS = ("rowanjobs.timer", "rowanjobs-retry.timer", "rowanjobs-notify.timer")
FAILURE_UNIT = "rowanjobs-failure@.service"

#: Written by RowanJobs. Sorts after any foreign drop-in so a stale one cannot
#: silently choose which code runs.
APP_DROPIN = "50-rowanjobs-app-owned.conf"
FOREIGN_DROPINS = ("10-controlpanel-release.conf",)

USER_UNIT_DIR = Path.home() / ".config/systemd/user"
DEFAULT_CONFIG = Path(
    os.environ.get("ROWANJOBS_CONFIG", Path.home() / ".config/rowanjobs/config.toml")
)
DEFAULT_DATA_ROOT = Path(
    os.environ.get("ROWANJOBS_DATA_ROOT", Path.home() / ".local/share/rowanjobs")
)

REQUIRED_TREE = (
    "pyproject.toml",
    "uv.lock",
    "src/rowanjobs/cli.py",
    "src/rowanjobs/db/migrations",
    "ops/onfailure.py",
    "ops/systemd",
)

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DEFAULT_KEEP = 8
DEFAULT_DRAIN_SECONDS = 4 * 3600  # a full harvest paces itself for ~12 minutes


class ReleaseError(RuntimeError):
    """Anything that must stop a build, an activation or a rollback."""


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message: str) -> None:
    print(f"[{utcnow()}] {message}", flush=True)


def run(argv, cwd=None, timeout=2400, check=True, env=None):
    """A bounded subprocess with an explicit argv. Never a shell string."""
    proc = subprocess.run(  # noqa: S603 - explicit argv lists, never a shell string
        argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env
    )
    if check and proc.returncode != 0:
        raise ReleaseError(
            f"{' '.join(str(a) for a in argv)} exited {proc.returncode}\n"
            f"{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
        )
    return proc


def git(repo: Path, *args, check=True):
    return run(["git", "-C", str(repo), *args], check=check, timeout=300)


def resolve_commit(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", ref).stdout.strip()


def worktree_is_clean(repo: Path) -> bool:
    return not git(repo, "status", "--porcelain").stdout.strip()


def contained_in_remote(repo: Path, commit: str, branch: str = "origin/main") -> bool:
    proc = git(repo, "merge-base", "--is-ancestor", commit, branch, check=False)
    return proc.returncode == 0


class Layout:
    def __init__(self, root: Path):
        self.root = root
        self.current = root / "current"
        self.previous = root / "previous"
        self.journal_path = root / ".activation-journal.jsonl"
        self.lock_path = root / ".deploy.lock"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, commit: str) -> Path:
        if not SHA_RE.match(commit):
            raise ReleaseError(f"{commit!r} is not a 40-character commit sha")
        return self.root / commit

    def resolve(self, link: Path) -> str | None:
        if not link.is_symlink():
            return None
        return link.readlink().name

    def protected(self) -> set[str]:
        return {n for n in (self.resolve(self.current), self.resolve(self.previous)) if n}


class DeployLock:
    """One deployment at a time, released by the kernel if the process dies."""

    def __init__(self, path: Path, timeout: float = 120.0):
        self.path = path
        self.timeout = timeout
        self.handle: int | None = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(self.handle)
                    raise ReleaseError(f"another deployment holds {self.path}") from None
                time.sleep(1.0)
        os.ftruncate(self.handle, 0)
        os.write(self.handle, f"pid={os.getpid()} {utcnow()}\n".encode())
        return self

    def __exit__(self, *_exc):
        if self.handle is not None:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            os.close(self.handle)


# ------------------------------------------------------------------- building


def is_sealed(release: Path) -> bool:
    """A release is a directory with a READY manifest, written last."""
    manifest = release / MANIFEST_NAME
    if not manifest.is_file():
        return False
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return data.get("build_status") == "READY" and data.get("schema") == MANIFEST_SCHEMA


def _make_writable(path: Path) -> None:
    for item in [path, *path.rglob("*")]:
        try:
            item.chmod(item.stat().st_mode | 0o200)
        except OSError:
            continue


def _discard(path: Path) -> None:
    _make_writable(path)
    shutil.rmtree(path, ignore_errors=True)


def _seal(target: Path) -> None:
    """Make the release read-only. Rubble cannot masquerade as a release."""
    for path in sorted(target.rglob("*"), reverse=True):
        try:
            if path.is_symlink():
                continue
            path.chmod(path.stat().st_mode & ~0o222)
        except OSError:
            continue
    with contextlib.suppress(OSError):
        target.chmod(target.stat().st_mode & ~0o222)


def schema_version_of(binary: Path) -> int | None:
    """The schema version the candidate code expects."""
    proc = run(
        [
            str(binary.parent / "python"),
            "-c",
            "from rowanjobs.db.migrations import SCHEMA_VERSION; print(SCHEMA_VERSION)",
        ],
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


def live_schema_version(binary: Path, data_root: Path) -> int | None:
    """The schema version the production archive is actually at."""
    proc = run(
        [
            str(binary.parent / "python"),
            "-c",
            "import sys, json;"
            "from pathlib import Path;"
            "from rowanjobs.db import open_readonly;"
            "from rowanjobs.db.migrations import current_version;"
            "db = open_readonly(Path(sys.argv[1]));"
            "print(current_version(db))",
            str(data_root / f"{APP}.db"),
        ],
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


def validate_candidate(release: Path, config: Path) -> dict:
    """Prove the candidate can start before anything points at it.

    Every check here exists because its absence has already cost a day of
    collection. In particular the configuration is loaded *by the candidate
    code*: the 2026-09-18 outage was new code meeting an old config file, and
    nothing in the pipeline had ever asked whether those two agreed.
    """
    binary = release / BINARY_RELPATH
    python = binary.parent / "python"
    checks: list[dict] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    record("binary_present", binary.is_file(), str(binary))
    if not binary.is_file():
        return {"ok": False, "checks": checks}

    version = run([str(binary), "--version"], check=False, timeout=120)
    record("starts", version.returncode == 0, version.stdout.strip() or version.stderr[-200:])

    imports = run(
        [str(python), "-c", "import rowanjobs, rowanjobs.cli, rowanjobs.collect.runner"],
        check=False,
        timeout=120,
    )
    record("imports", imports.returncode == 0, imports.stderr[-300:] or "ok")

    # The check that would have caught 2026-09-18.
    config_probe = run(
        [
            str(python),
            "-c",
            "import sys;"
            "from pathlib import Path;"
            "from rowanjobs.config import load_config;"
            "cfg = load_config(Path(sys.argv[1]));"
            "print(cfg.notify.kind or 'notify-disabled')",
            str(config),
        ],
        check=False,
        timeout=120,
    )
    record(
        "deployed_config_loads",
        config_probe.returncode == 0,
        (config_probe.stderr.strip().splitlines() or ["ok"])[-1][:300],
    )

    health = run([str(binary), "health", "--no-timer"], check=False, timeout=180)
    document = None
    if health.returncode in (0, 2):
        try:
            document = json.loads(health.stdout)
        except ValueError:
            document = None
    record(
        "health_contract",
        bool(document) and document.get("schema_version") == "controlpanel.status.v1",
        (document or {}).get("overall", {}).get("summary", health.stderr[-200:]),
    )

    return {"ok": all(c["ok"] for c in checks), "checks": checks}


def build(
    layout: Layout,
    ref: str = "HEAD",
    *,
    repo: Path = CANONICAL_REPO,
    config: Path = DEFAULT_CONFIG,
    allow_dirty: bool = False,
    allow_unpushed: bool = False,
    force: bool = False,
) -> dict:
    """Export a commit into a sealed release with its own locked environment."""
    layout.ensure()
    commit = resolve_commit(repo, ref)
    target = layout.path_for(commit)

    if not allow_dirty and not worktree_is_clean(repo):
        raise ReleaseError(
            "the working tree has uncommitted changes; commit them or pass --allow-dirty "
            "(a release is named for a commit, so it must be one)"
        )
    pushed = contained_in_remote(repo, commit)
    if not pushed and not allow_unpushed:
        raise ReleaseError(
            f"{commit[:12]} is not contained in origin/main; push it or pass --allow-unpushed"
        )

    if is_sealed(target) and not force:
        log(f"release {commit[:12]} is already sealed; reusing it")
        return json.loads((target / MANIFEST_NAME).read_text(encoding="utf-8"))
    if target.exists():
        log(f"discarding an unsealed or forced rebuild at {target}")
        _discard(target)

    staging = layout.root / f".building-{commit[:12]}-{os.getpid()}"
    _discard(staging)
    staging.mkdir(parents=True)
    try:
        log(f"exporting {commit[:12]}")
        # `git archive` writes a binary tar to stdout, so it is piped straight
        # into tar rather than captured as text. Exporting from the commit --
        # not copying the working tree -- is what makes the release reproducible
        # and immune to whatever is half-edited on disk right now.
        with subprocess.Popen(  # noqa: S603
            ["git", "-C", str(repo), "archive", "--format=tar", commit],  # noqa: S607
            stdout=subprocess.PIPE,
        ) as source:
            extract = subprocess.run(  # noqa: S603
                ["tar", "-x", "-C", str(staging)],  # noqa: S607
                stdin=source.stdout,
                timeout=600,
            )
        if source.returncode != 0 or extract.returncode != 0:
            raise ReleaseError(f"could not export {commit[:12]} into {staging}")

        missing = [item for item in REQUIRED_TREE if not (staging / item).exists()]
        if missing:
            raise ReleaseError(f"the exported tree is missing {missing}")

        log("building the locked environment (no editable install)")
        env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(staging / VENV_RELPATH)}
        run(
            ["uv", "sync", "--frozen", "--no-dev", "--extra", "browser", "--no-editable"],
            cwd=str(staging),
            env=env,
            timeout=2400,
        )
        binary = staging / BINARY_RELPATH
        if not binary.is_file():
            raise ReleaseError(f"no {APP} entry point in the built environment")

        report = validate_candidate(staging, config)
        if not report["ok"]:
            broken = [c for c in report["checks"] if not c["ok"]]
            raise ReleaseError(f"candidate validation failed: {broken}")

        manifest = {
            "schema": MANIFEST_SCHEMA,
            "app": APP,
            "commit": commit,
            "commit_subject": git(repo, "log", "-1", "--format=%s", commit).stdout.strip(),
            "built_at_utc": utcnow(),
            "built_from": str(repo),
            "contained_in_origin_main": pushed,
            "allow_dirty": allow_dirty,
            "schema_version": schema_version_of(staging / BINARY_RELPATH),
            "validation": report,
            "build_status": "READY",
        }
        (staging / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        _seal(staging)
        staging.rename(target)
        log(f"sealed release {commit[:12]}")
        return manifest
    except BaseException:
        _discard(staging)
        raise


# -------------------------------------------------------------------- systemd


def systemctl(*args, check=True):
    return run(["systemctl", "--user", *args], check=check, timeout=180)


def render_dropin(release: Path, service: str) -> str:
    binary = release / BINARY_RELPATH
    arguments = EXEC_SERVICES[service]
    return f"""[Service]
# Written by RowanJobs' own release tool. RowanJobs owns this runtime;
# ControlPanel observes it and never repoints it. Sorts after any
# 10-controlpanel-release.conf so a stale foreign drop-in cannot choose the code.
ExecStart=
ExecStart={binary} {arguments}
"""


def render_failure_dropin(release: Path) -> str:
    return f"""[Service]
# The handler must come from a sealed release too -- but run by the system
# interpreter, never the application's own, so it still works when the
# application is what is broken.
ExecStart=
ExecStart=/usr/bin/python3 {release}/ops/onfailure.py %i
"""


def install_dropins(release: Path) -> list[str]:
    written = []
    for service in EXEC_SERVICES:
        directory = USER_UNIT_DIR / f"{service}.service.d"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / APP_DROPIN
        path.write_text(render_dropin(release, service), encoding="utf-8")
        written.append(str(path))
    directory = USER_UNIT_DIR / f"{FAILURE_UNIT}.d"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / APP_DROPIN
    path.write_text(render_failure_dropin(release), encoding="utf-8")
    written.append(str(path))
    return written


def remove_foreign_dropins() -> list[str]:
    removed = []
    for service in EXEC_SERVICES:
        directory = USER_UNIT_DIR / f"{service}.service.d"
        for name in FOREIGN_DROPINS:
            path = directory / name
            if path.exists():
                path.unlink()
                removed.append(str(path))
    return removed


def effective_exec(unit: str) -> dict:
    probe = systemctl(
        "show",
        unit,
        "--property=ExecStart,UnitFileState,ActiveState,NextElapseUSecRealtime,LastTriggerUSec",
        check=False,
    )
    return dict(line.split("=", 1) for line in probe.stdout.strip().splitlines() if "=" in line)


def verify_units(release: Path | None = None) -> dict:
    """Prove the units actually execute the release we think they do."""
    expected = str(release / BINARY_RELPATH) if release else None
    units: dict[str, dict] = {}
    ok = True
    for service in EXEC_SERVICES:
        fields = effective_exec(f"{service}.service")
        exec_start = fields.get("ExecStart", "")
        matches = expected is None or expected in exec_start
        units[f"{service}.service"] = {
            "exec_start": exec_start[:400],
            "matches_release": matches,
        }
        ok = ok and matches
    for timer in TIMERS:
        fields = effective_exec(timer)
        units[timer] = {
            "unit_file_state": fields.get("UnitFileState"),
            "active_state": fields.get("ActiveState"),
        }
    return {"ok": ok, "expected_binary": expected, "units": units}


def timer_state() -> dict:
    state = {}
    for timer in TIMERS:
        fields = effective_exec(timer)
        state[timer] = {
            "enabled": fields.get("UnitFileState"),
            "active": fields.get("ActiveState"),
        }
    return state


def _restore_timers(state: dict) -> None:
    for timer, fields in state.items():
        if fields.get("enabled") == "enabled":
            systemctl("enable", "--now", timer, check=False)


def _collection_in_flight(data_root: Path) -> bool:
    """True while a collector holds its exclusive lock.

    Switching the runtime under a running harvest would leave half the day
    collected by one revision and half by another. The lock is the collector's
    own, so this asks the same question the collector answers.
    """
    lock = data_root / "runtime" / "collector.lock"
    if not lock.exists():
        return False
    try:
        handle = os.open(str(lock), os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(handle)


def _swap(link: Path, destination: Path) -> None:
    """Repoint a symlink atomically: no window with no link at all."""
    temporary = link.parent / f".{link.name}.swap-{os.getpid()}"
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(destination)
    temporary.replace(link)


def journal(layout: Layout, entry: dict) -> None:
    entry = {"at_utc": utcnow(), **entry}
    with layout.journal_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def backup_database(binary: Path, config: Path, data_root: Path, label: str) -> str | None:
    if not (data_root / f"{APP}.db").exists():
        return None
    proc = run(
        [
            str(binary),
            "--config",
            str(config),
            "--data-root",
            str(data_root),
            "--json",
            "backup",
            "--kind",
            "predeploy",
        ],
        check=False,
        timeout=900,
    )
    if proc.returncode != 0:
        raise ReleaseError(f"pre-activation backup failed: {proc.stdout[-600:]}")
    try:
        return json.loads(proc.stdout).get("path")
    except ValueError:
        return label


def activate(
    layout: Layout,
    commit: str,
    *,
    config: Path = DEFAULT_CONFIG,
    data_root: Path = DEFAULT_DATA_ROOT,
    drain: float = DEFAULT_DRAIN_SECONDS,
    reason: str = "",
    allow_unpushed: bool = False,
    allow_schema_regression: bool = False,
) -> dict:
    """Point the units at a sealed release, rolling back on any failure."""
    release = layout.path_for(commit)
    if not is_sealed(release):
        raise ReleaseError(f"{release} is not a sealed READY release")
    manifest = json.loads((release / MANIFEST_NAME).read_text(encoding="utf-8"))
    if not manifest.get("contained_in_origin_main") and not allow_unpushed:
        raise ReleaseError(
            "refusing to activate a release that is not contained in origin/main "
            "(pass --allow-unpushed only when you mean it)"
        )

    binary = release / BINARY_RELPATH
    previous = layout.resolve(layout.current)

    with DeployLock(layout.lock_path):
        # Never switch the runtime out from under a harvest in progress.
        deadline = time.monotonic() + drain
        while _collection_in_flight(data_root):
            if time.monotonic() > deadline:
                raise ReleaseError(
                    f"a collection has held the collector lock for more than {drain:g}s; "
                    "refusing to switch the runtime underneath it"
                )
            log("a collection is running; waiting for it to finish")
            time.sleep(15)

        # Refusing to move the archive backwards into code that cannot read it.
        candidate_schema = manifest.get("schema_version")
        live_schema = live_schema_version(binary, data_root)
        if (
            candidate_schema is not None
            and live_schema is not None
            and candidate_schema < live_schema
            and not allow_schema_regression
        ):
            raise ReleaseError(
                f"the archive is at schema v{live_schema} but {commit[:12]} only knows "
                f"v{candidate_schema}; restoring it would run old code against a migrated "
                "database. Pass --allow-schema-regression only with a restore plan."
            )

        timers = timer_state()
        log("stopping timers")
        for timer in TIMERS:
            systemctl("stop", timer, check=False)

        backup_path = None
        try:
            backup_path = backup_database(binary, config, data_root, f"pre-{commit[:12]}")
            log(f"pre-activation backup: {backup_path or 'no archive yet'}")

            log("applying migrations from the candidate")
            run(
                [str(binary), "--config", str(config), "--data-root", str(data_root), "migrate"],
                timeout=900,
            )

            if previous and previous != commit:
                _swap(layout.previous, layout.path_for(previous))
            _swap(layout.current, release)
            log(f"current -> {commit[:12]}")

            removed = remove_foreign_dropins()
            written = install_dropins(release)
            systemctl("daemon-reload")
            for timer in TIMERS:
                systemctl("enable", "--now", timer)

            report = verify_units(release)
            if not report["ok"]:
                raise ReleaseError(f"unit verification failed: {report}")

            smoke = run([str(binary), "health"], check=False, timeout=180)
            if smoke.returncode not in (0, 2):
                raise ReleaseError(f"health check failed: {smoke.stderr[-400:]}")
            document = json.loads(smoke.stdout)
            if document.get("schema_version") != "controlpanel.status.v1":
                raise ReleaseError("health did not emit the expected status contract")

            entry = {
                "action": "activate",
                "commit": commit,
                "previous": previous,
                "reason": reason,
                "backup": backup_path,
                "dropins_written": written,
                "foreign_dropins_removed": removed,
                "schema": {"candidate": candidate_schema, "live_before": live_schema},
                "health": document["overall"],
                "timers": timer_state(),
                "result": "ok",
            }
            journal(layout, entry)
            log(f"activated {commit[:12]}")
            return entry
        except BaseException as failure:
            log(f"activation failed: {failure}; restoring the previous release")
            if previous:
                _swap(layout.current, layout.path_for(previous))
                install_dropins(layout.path_for(previous))
                systemctl("daemon-reload", check=False)
            _restore_timers(timers)
            journal(
                layout,
                {
                    "action": "activate",
                    "commit": commit,
                    "previous": previous,
                    "result": "failed",
                    "error": str(failure)[:800],
                    "restored_to": previous,
                },
            )
            raise


def rollback(layout: Layout, to: str | None = None, **kwargs) -> dict:
    target = to or layout.resolve(layout.previous)
    if not target:
        raise ReleaseError("there is no previous release to roll back to")
    log(f"rolling back to {target[:12]}")
    return activate(layout, target, reason="rollback", **kwargs)


def prune(layout: Layout, keep: int = DEFAULT_KEEP) -> list[str]:
    protected = layout.protected()
    releases = sorted(
        (layout.root / name for name in _release_names(layout)),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = []
    for release in releases[keep:]:
        if release.name in protected:
            continue
        _discard(release)
        removed.append(release.name)
    return removed


def _release_names(layout: Layout) -> list[str]:
    if not layout.root.is_dir():
        return []
    return sorted(p.name for p in layout.root.iterdir() if p.is_dir() and SHA_RE.match(p.name))


def status(layout: Layout) -> dict:
    current = layout.resolve(layout.current)
    manifest = {}
    if current:
        path = layout.path_for(current) / MANIFEST_NAME
        if path.is_file():
            manifest = json.loads(path.read_text(encoding="utf-8"))
    try:
        head = resolve_commit(CANONICAL_REPO, "HEAD")
        dirty = not worktree_is_clean(CANONICAL_REPO)
    except ReleaseError:
        head, dirty = None, None
    return {
        "root": str(layout.root),
        "deployed_commit": current,
        "deployed_subject": manifest.get("commit_subject"),
        "deployed_at": manifest.get("built_at_utc"),
        "deployed_schema_version": manifest.get("schema_version"),
        "previous_commit": layout.resolve(layout.previous),
        # Deliberately separate: what production runs is not what the working
        # tree happens to contain.
        "development_head": head,
        "development_dirty": dirty,
        "development_matches_deployed": bool(head and head == current and dirty is False),
        "units": verify_units(layout.path_for(current) if current else None),
        "releases": _release_names(layout),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="rowanjobs-release", description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("build", help="build and seal a release from a commit")
    p.add_argument("ref", nargs="?", default="HEAD")
    p.add_argument("--allow-dirty", action="store_true")
    p.add_argument("--allow-unpushed", action="store_true")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("activate", help="point the units at a sealed release")
    p.add_argument("commit")
    p.add_argument("--reason", default="")
    p.add_argument("--drain", type=float, default=DEFAULT_DRAIN_SECONDS)
    p.add_argument("--allow-unpushed", action="store_true")
    p.add_argument("--allow-schema-regression", action="store_true")

    p = sub.add_parser("deploy", help="build then activate")
    p.add_argument("ref", nargs="?", default="HEAD")
    p.add_argument("--reason", default="")
    p.add_argument("--allow-dirty", action="store_true")
    p.add_argument("--allow-unpushed", action="store_true")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("rollback", help="return to the previous release")
    p.add_argument("--to")
    p.add_argument("--allow-schema-regression", action="store_true")

    sub.add_parser("status", help="what is deployed, and what the units run")
    sub.add_parser("verify", help="prove the units execute the current release")
    p = sub.add_parser("prune", help="remove old releases")
    p.add_argument("--keep", type=int, default=DEFAULT_KEEP)

    args = parser.parse_args(argv)
    layout = Layout(args.root)

    try:
        if args.command == "build":
            result = build(
                layout,
                args.ref,
                config=args.config,
                allow_dirty=args.allow_dirty,
                allow_unpushed=args.allow_unpushed,
                force=args.force,
            )
        elif args.command == "activate":
            result = activate(
                layout,
                resolve_commit(CANONICAL_REPO, args.commit),
                config=args.config,
                data_root=args.data_root,
                drain=args.drain,
                reason=args.reason,
                allow_unpushed=args.allow_unpushed,
                allow_schema_regression=args.allow_schema_regression,
            )
        elif args.command == "deploy":
            manifest = build(
                layout,
                args.ref,
                config=args.config,
                allow_dirty=args.allow_dirty,
                allow_unpushed=args.allow_unpushed,
                force=args.force,
            )
            result = activate(
                layout,
                manifest["commit"],
                config=args.config,
                data_root=args.data_root,
                reason=args.reason or "deploy",
                allow_unpushed=args.allow_unpushed,
            )
        elif args.command == "rollback":
            result = rollback(
                layout,
                args.to,
                config=args.config,
                data_root=args.data_root,
                allow_schema_regression=args.allow_schema_regression,
            )
        elif args.command == "prune":
            result = {"removed": prune(layout, args.keep)}
        elif args.command == "verify":
            current = layout.resolve(layout.current)
            result = verify_units(layout.path_for(current) if current else None)
        else:
            result = status(layout)
    except ReleaseError as failure:
        print(f"error: {failure}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
