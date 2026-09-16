"""Filesystem layout.

Everything mutable lives outside the Git working tree:

    ~/.config/rowanjobs/config.toml      configuration
    ~/.local/share/rowanjobs/            data root
        rowanjobs.db                     active SQLite archive (payloads live inside)
        backups/                         rotated verified snapshots + manifests
        logs/                            structured collector logs
        runtime/                         lock file, health.json, deployment.json
        source-audit/                    manual audit captures (not production history)
        exports/                         CLI export output
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "rowanjobs"

ENV_CONFIG = "ROWANJOBS_CONFIG"
ENV_DATA_ROOT = "ROWANJOBS_DATA_ROOT"
ENV_DB = "ROWANJOBS_DB"


def _xdg(env_var: str, default: Path) -> Path:
    raw = os.environ.get(env_var)
    if raw:
        return Path(raw).expanduser()
    return default


def default_config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", Path.home() / ".config") / APP_NAME


def default_config_file() -> Path:
    override = os.environ.get(ENV_CONFIG)
    if override:
        return Path(override).expanduser().resolve()
    return default_config_dir() / "config.toml"


def default_data_root() -> Path:
    override = os.environ.get(ENV_DATA_ROOT)
    if override:
        return Path(override).expanduser()
    return _xdg("XDG_DATA_HOME", Path.home() / ".local" / "share") / APP_NAME


class Layout:
    """Resolved absolute paths for one data root."""

    def __init__(self, data_root: Path, db_path: Path | None = None) -> None:
        self.data_root = Path(data_root).expanduser().resolve()
        env_db = os.environ.get(ENV_DB)
        if db_path is not None:
            self.db_path = Path(db_path).expanduser().resolve()
        elif env_db:
            self.db_path = Path(env_db).expanduser().resolve()
        else:
            self.db_path = self.data_root / "rowanjobs.db"

    @property
    def backups_dir(self) -> Path:
        return self.data_root / "backups"

    @property
    def logs_dir(self) -> Path:
        return self.data_root / "logs"

    @property
    def runtime_dir(self) -> Path:
        return self.data_root / "runtime"

    @property
    def audit_dir(self) -> Path:
        return self.data_root / "source-audit"

    @property
    def exports_dir(self) -> Path:
        return self.data_root / "exports"

    @property
    def lock_path(self) -> Path:
        return self.runtime_dir / "collector.lock"

    @property
    def health_path(self) -> Path:
        return self.runtime_dir / "health.json"

    @property
    def deployment_manifest(self) -> Path:
        return self.runtime_dir / "deployment.json"

    @property
    def restore_check_path(self) -> Path:
        return self.runtime_dir / "restore-verification.json"

    def ensure(self) -> None:
        """Create the data tree with restrictive permissions."""
        self.data_root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.data_root, 0o700)
        for d in (
            self.backups_dir,
            self.logs_dir,
            self.runtime_dir,
            self.audit_dir,
            self.exports_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Layout(data_root={self.data_root}, db={self.db_path})"
