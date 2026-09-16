"""Time handling.

Rules enforced here:

* Every timestamp written to the database is UTC, ISO-8601, ``Z``-suffixed,
  second precision.
* Operational display uses America/New_York with an explicit offset label.
* A date-only source value stays date-only. We never invent a midnight
  timestamp for it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

OPERATIONAL_TZ = ZoneInfo("America/New_York")
OPERATIONAL_TZ_NAME = "America/New_York"


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def utc_str(dt: datetime | None = None) -> str:
    """Canonical storage representation: ``2026-09-16T21:41:08Z``."""
    if dt is None:
        dt = now_utc()
    if dt.tzinfo is None:
        raise ValueError("naive datetime rejected; RowanJobs stores UTC only")
    return dt.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def to_local(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    dt = parse_utc(value) if isinstance(value, str) else value
    return dt.astimezone(OPERATIONAL_TZ)


def local_str(value: str | datetime | None) -> str | None:
    """Display string with an explicit zone label, e.g. ``2026-09-16 17:41:08 EDT``."""
    dt = to_local(value)
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def local_date_str(value: str | datetime | None = None) -> str:
    """The America/New_York calendar date. This is the daily-confirmation key."""
    dt = to_local(value) if value is not None else datetime.now(OPERATIONAL_TZ)
    assert dt is not None
    return dt.strftime("%Y-%m-%d")


def slot_for(value: str | datetime | None, hour: int, minute: int) -> tuple[str, str]:
    """Return ``(scheduled_slot_utc, scheduled_slot_local_date)`` for a wall time.

    The slot a run belongs to is the configured daily time on the run's local
    calendar date. A retry at 09:00 for the 06:15 slot keeps the 06:15 slot, so
    it never manufactures an extra daily observation.
    """
    dt = to_local(value) if value is not None else datetime.now(OPERATIONAL_TZ)
    assert dt is not None
    slot_local = dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return utc_str(slot_local.astimezone(UTC)), slot_local.strftime("%Y-%m-%d")


def date_only_str(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def duration_str(start: str | None, end: str | None) -> str | None:
    if not start or not end:
        return None
    delta = parse_utc(end) - parse_utc(start)
    total = int(delta.total_seconds())
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"
