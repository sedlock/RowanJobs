"""Single-collector mutual exclusion, backed by the OS.

``flock`` is used rather than a pid file: the kernel releases it when the
process dies, so a crashed or killed collector cannot leave a stale lock that
blocks every future run.

Contention is a first-class outcome. "Another collector was already running" is
recorded as ``lock_contention`` and must never be reported as a source failure
or, worse, as a successful collection that found nothing.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
from pathlib import Path
from types import TracebackType

from ..timeutil import utc_str


class LockContention(RuntimeError):
    """Another collector holds the lock."""

    def __init__(self, path: Path, holder: dict[str, object] | None) -> None:
        detail = ""
        if holder:
            detail = (
                f" (held by pid {holder.get('pid')} on {holder.get('host')} "
                f"since {holder.get('acquired_at_utc')})"
            )
        super().__init__(f"collector lock {path} is already held{detail}")
        self.path = path
        self.holder = holder


class CollectorLock:
    """Exclusive, non-blocking lock around a collection run."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None
        self.holder: dict[str, object] | None = None

    def read_holder(self) -> dict[str, object] | None:
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def acquire(self, *, run_uuid: str | None = None) -> CollectorLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            holder = self.read_holder()
            os.close(fd)
            raise LockContention(self.path, holder) from exc

        payload = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "user": os.environ.get("USER") or os.environ.get("LOGNAME") or "",
            "run_uuid": run_uuid,
            "acquired_at_utc": utc_str(),
        }
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps(payload).encode("utf-8"))
        os.fsync(fd)
        self._fd = fd
        self.holder = payload
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            os.ftruncate(self._fd, 0)
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:  # pragma: no cover - best effort
            pass
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> CollectorLock:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
