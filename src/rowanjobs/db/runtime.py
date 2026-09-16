"""SQLite runtime selection and WAL safety verification.

RowanJobs deliberately does not use the interpreter's bundled ``sqlite3``
module. Ubuntu 24.04 ships SQLite 3.45.1, which is inside the range affected by
the WAL-reset database corruption bug (present 3.7.0 .. 3.51.2, fixed in 3.51.3
and 3.53.0; backports exist for 3.44.6 and 3.50.7 -- https://www.sqlite.org/wal.html#walresetbug).

Instead we depend on ``apsw``, whose wheels embed a SQLite amalgamation. The
check below is evidence-based: it reads the version actually loaded into this
process and refuses to enable WAL unless that build is known-fixed.
"""

from __future__ import annotations

from dataclasses import dataclass

import apsw

# Releases that contain the WAL-reset fix. A build qualifies when its version is
# >= one of these on the same feature branch, per sqlite.org/wal.html#walresetbug.
_FIXED_FROM: tuple[tuple[int, int, int], ...] = (
    (3, 44, 6),
    (3, 50, 7),
    (3, 51, 3),
)


def _parse(version: str) -> tuple[int, int, int]:
    parts = version.split(".")
    nums = [int(p) for p in parts[:3]]
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2]


def wal_reset_bug_fixed(version: str) -> bool:
    """True when ``version`` is documented as containing the WAL-reset fix.

    ``3.44.6``/``3.50.7`` are backport releases: only the exact patch series
    that received the backport qualifies below its successor minor line.
    ``3.51.3`` and anything newer on 3.51+ qualifies outright.
    """
    v = _parse(version)
    if v >= (3, 51, 3):
        return True
    return any(v[0] == fixed[0] and v[1] == fixed[1] and v[2] >= fixed[2] for fixed in _FIXED_FROM)


@dataclass(frozen=True, slots=True)
class RuntimeInfo:
    provider: str
    provider_version: str
    sqlite_version: str
    sqlite_source_id: str
    using_amalgamation: bool
    wal_safe: bool
    wal_evidence: str

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "provider_version": self.provider_version,
            "sqlite_version": self.sqlite_version,
            "sqlite_source_id": self.sqlite_source_id,
            "using_amalgamation": self.using_amalgamation,
            "wal_safe": self.wal_safe,
            "wal_evidence": self.wal_evidence,
        }


_cached: RuntimeInfo | None = None


def runtime_info() -> RuntimeInfo:
    """Inspect the SQLite library this process actually loaded."""
    global _cached
    if _cached is not None:
        return _cached

    version = apsw.sqlite_lib_version()
    probe = apsw.Connection(":memory:")
    try:
        row = probe.execute("select sqlite_source_id()").fetchone()
        source_id = str(row[0]) if row else "unknown"
    finally:
        probe.close()

    safe = wal_reset_bug_fixed(version)
    if safe:
        evidence = (
            f"SQLite {version} (source id {source_id.split()[0]}) is at or after the "
            "WAL-reset corruption fix documented at "
            "https://www.sqlite.org/wal.html#walresetbug (fixed in 3.51.3 / 3.53.0; "
            "backports 3.44.6, 3.50.7). WAL enabled."
        )
    else:
        evidence = (
            f"SQLite {version} is inside the range affected by the WAL-reset "
            "corruption bug (3.7.0 .. 3.51.2, see "
            "https://www.sqlite.org/wal.html#walresetbug). WAL refused; falling back "
            "to a rollback journal with synchronous=FULL."
        )

    _cached = RuntimeInfo(
        provider="apsw",
        provider_version=apsw.apswversion(),
        sqlite_version=version,
        sqlite_source_id=source_id,
        using_amalgamation=bool(apsw.using_amalgamation),
        wal_safe=safe,
        wal_evidence=evidence,
    )
    return _cached
