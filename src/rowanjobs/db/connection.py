"""Database handle.

Guarantees provided here:

* ``PRAGMA foreign_keys=ON`` on *every* connection (SQLite defaults to off and
  the setting is per-connection, not per-database).
* ``PRAGMA synchronous=FULL``.
* WAL only on a runtime verified free of the WAL-reset corruption bug, and only
  on a local filesystem. Otherwise a rollback journal, recorded as a deviation.
* A busy timeout, so concurrent readers do not fail instantly.
* Explicit, short write transactions via :meth:`Database.write`. Network calls
  must never happen inside one.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import apsw

from ..timeutil import utc_str
from .runtime import RuntimeInfo, runtime_info

# Filesystems where WAL's shared-memory index is unreliable.
_NETWORK_FS = {"nfs", "nfs4", "cifs", "smbfs", "smb3", "fuse.sshfs", "afs", "9p"}


def _dict_row(cursor: apsw.Cursor, row: tuple[Any, ...]) -> dict[str, Any]:
    cols = [d[0] for d in cursor.get_description()]
    return dict(zip(cols, row, strict=True))


class ReadOnlyError(RuntimeError):
    """Raised when a read-only handle is asked to write or migrate."""


def _filesystem_type(path: Path) -> str:
    """Best-effort filesystem type for ``path`` (the nearest existing parent)."""
    target = path
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        dev = target.stat().st_dev
    except OSError:
        return "unknown"
    best = ("", "unknown")
    try:
        with Path("/proc/mounts").open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mount, fstype = parts[1], parts[2]
                mount = mount.replace("\\040", " ")
                try:
                    if Path(mount).stat().st_dev != dev:
                        continue
                except OSError:
                    continue
                if len(mount) >= len(best[0]):
                    best = (mount, fstype)
    except OSError:
        return "unknown"
    return best[1]


class Database:
    """A single SQLite connection with RowanJobs' durability policy applied."""

    def __init__(self, path: Path, *, readonly: bool = False, busy_timeout_ms: int = 15000):
        self.path = Path(path)
        self.readonly = readonly
        self.runtime: RuntimeInfo = runtime_info()
        self.journal_mode: str = "unknown"
        self.wal_deviation: str | None = None
        self.filesystem: str = _filesystem_type(self.path)

        flags = (
            apsw.SQLITE_OPEN_READONLY
            if readonly
            else (apsw.SQLITE_OPEN_READWRITE | apsw.SQLITE_OPEN_CREATE)
        )
        self.conn = apsw.Connection(str(self.path), flags=flags)
        self.conn.set_busy_timeout(busy_timeout_ms)
        # Per-connection and mandatory: SQLite ships with FK enforcement off.
        self.conn.pragma("foreign_keys", True)
        if int(self.conn.pragma("foreign_keys") or 0) != 1:
            raise RuntimeError("could not enable foreign key enforcement")

        if readonly:
            self.journal_mode = str(self.conn.pragma("journal_mode") or "unknown")
            return

        self.conn.pragma("synchronous", "FULL")
        self._configure_journal()
        self.conn.pragma("temp_store", "MEMORY")
        # Keep the WAL from growing without bound between checkpoints.
        self.conn.pragma("journal_size_limit", 64 * 1024 * 1024)
        self.conn.pragma("wal_autocheckpoint", 512)

    # ---------------------------------------------------------------- journal

    def _configure_journal(self) -> None:
        fs = self.filesystem
        if not self.runtime.wal_safe:
            self.wal_deviation = self.runtime.wal_evidence
        elif fs in _NETWORK_FS:
            self.wal_deviation = (
                f"database is on a {fs} filesystem; WAL shared memory is unreliable "
                "there, so a rollback journal is used instead"
            )
        if self.wal_deviation:
            self.conn.pragma("journal_mode", "DELETE")
        else:
            self.conn.pragma("journal_mode", "WAL")
        self.journal_mode = str(self.conn.pragma("journal_mode") or "unknown").lower()

    # ------------------------------------------------------------------ query

    def query(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> list[dict[str, Any]]:
        """Run a SELECT and return a list of column-name dicts.

        The row trace runs while execution is still active, which is the only
        point at which APSW will hand over column names; asking afterwards
        raises once the statement is exhausted.
        """
        cur = self.conn.cursor()
        cur.row_trace = _dict_row
        return list(cur.execute(sql, params))

    def one(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> Any | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return None if row is None else row[0]

    # ------------------------------------------------------------------ write

    def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> Any:
        if self.readonly:
            raise ReadOnlyError("this handle is read-only")
        return self.conn.execute(sql, params)

    def insert(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> int:
        self.execute(sql, params)
        return int(self.conn.last_insert_rowid())

    @contextmanager
    def write(self) -> Iterator[Database]:
        """A short IMMEDIATE write transaction.

        IMMEDIATE (rather than DEFERRED) takes the write lock up front, so two
        collectors cannot both read, then both try to upgrade, then deadlock.

        Never perform network I/O inside this block.
        """
        if self.readonly:
            raise ReadOnlyError("this handle is read-only")
        if self.conn.in_transaction:
            # Nested use is a programming error; savepoints would hide long
            # transactions, which is exactly what we are guarding against.
            raise RuntimeError("nested Database.write() is not allowed")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self
        except BaseException:
            with contextlib.suppress(apsw.Error):  # pragma: no cover - dead txn
                self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # -------------------------------------------------------------- lifecycle

    def checkpoint(self, mode: str = "PASSIVE") -> tuple[int, int]:
        if self.readonly or self.journal_mode != "wal":
            return (0, 0)
        modes = {
            "PASSIVE": apsw.SQLITE_CHECKPOINT_PASSIVE,
            "FULL": apsw.SQLITE_CHECKPOINT_FULL,
            "RESTART": apsw.SQLITE_CHECKPOINT_RESTART,
            "TRUNCATE": apsw.SQLITE_CHECKPOINT_TRUNCATE,
        }
        return self.conn.wal_checkpoint(mode=modes[mode.upper()])

    def integrity_check(self) -> list[str]:
        rows = [r[0] for r in self.conn.execute("PRAGMA integrity_check")]
        return [str(r) for r in rows]

    def foreign_key_check(self) -> list[tuple[Any, ...]]:
        return list(self.conn.execute("PRAGMA foreign_key_check"))

    def page_bytes(self) -> int:
        page_size = int(self.conn.pragma("page_size") or 0)
        page_count = int(self.conn.pragma("page_count") or 0)
        return page_size * page_count

    def describe(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "readonly": self.readonly,
            "journal_mode": self.journal_mode,
            "filesystem": self.filesystem,
            "wal_deviation": self.wal_deviation,
            "runtime": self.runtime.as_dict(),
            "inspected_at_utc": utc_str(),
        }

    def close(self) -> None:
        with contextlib.suppress(apsw.Error):  # pragma: no cover - best effort
            if not self.readonly and self.journal_mode == "wal":
                self.checkpoint("TRUNCATE")
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_db(path: Path, *, migrate: bool = True, busy_timeout_ms: int = 15000) -> Database:
    """Open (creating if needed) a read-write handle, applying migrations."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    db = Database(path, readonly=False, busy_timeout_ms=busy_timeout_ms)
    if not existed:
        with contextlib.suppress(OSError):  # pragma: no cover
            path.chmod(0o600)
    if migrate:
        from .migrations import apply_migrations

        apply_migrations(db)
    return db


def open_readonly(path: Path, busy_timeout_ms: int = 15000) -> Database:
    """Open a strictly read-only handle.

    Reporting commands use this so they can never migrate or mutate production
    data as a side effect of being run.
    """
    if not Path(path).exists():
        raise FileNotFoundError(f"no archive at {path}")
    return Database(Path(path), readonly=True, busy_timeout_ms=busy_timeout_ms)
