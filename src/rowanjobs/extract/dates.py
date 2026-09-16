"""Source date handling.

Every displayed date is preserved four ways:

* ``value_text``            the string exactly as displayed ("Sep 29 2026 11:55 PM")
* ``source_machine_value``  the machine value the page supplied, if any
                            (``<time datetime="2026-09-30T03:55:00Z">``)
* ``source_tz_text``        the timezone wording next to it ("Eastern Daylight Time")
* ``parsed_utc`` / ``parsed_local_date`` a best-effort interpretation, clearly
  marked as derived

``source_precision`` records what the *display* actually committed to. Rowan's
"Advertised" line shows a bare date while the ``datetime`` attribute carries a
12:00Z placeholder; recording precision ``date`` keeps us from later reporting a
posting as advertised at 08:00 Eastern when the source never said so.

Nothing here invents a midnight. A date-only value stays date-only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from ..timeutil import OPERATIONAL_TZ

# "Sep 29 2026 11:55 PM", "Sep 15 2026", "29 Sep 2026"
_DISPLAY_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"^([A-Za-z]{3,9})\s+(\d{1,2})\s+(\d{4})\s+(\d{1,2}):(\d{2})\s*([AaPp][Mm])$"),
        "%b %d %Y %I:%M %p",
        "minute",
    ),
    (
        re.compile(r"^(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})\s+(\d{1,2}):(\d{2})\s*([AaPp][Mm])$"),
        "%d %b %Y %I:%M %p",
        "minute",
    ),
    (re.compile(r"^([A-Za-z]{3,9})\s+(\d{1,2})\s+(\d{4})$"), "%b %d %Y", "date"),
    (re.compile(r"^(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})$"), "%d %b %Y", "date"),
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})$"), "%Y-%m-%d", "date"),
)

_ISO_Z = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?"
    r"(Z|z|[+-]\d{2}:?\d{2})?$"
)


@dataclass(slots=True)
class ParsedDate:
    display_text: str | None
    machine_value: str | None
    tz_text: str | None
    parse_state: str
    precision: str
    parsed_utc: str | None
    parsed_local_date: str | None
    detail: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "display_text": self.display_text,
            "machine_value": self.machine_value,
            "tz_text": self.tz_text,
            "parse_state": self.parse_state,
            "precision": self.precision,
            "parsed_utc": self.parsed_utc,
            "parsed_local_date": self.parsed_local_date,
            "detail": self.detail,
        }


def _parse_machine(value: str) -> datetime | None:
    match = _ISO_Z.match(value.strip())
    if not match:
        return None
    y, mo, d, h, mi, s, off = match.groups()
    try:
        # Deliberately naive here; the offset from the source string is attached
        # below so a missing offset is never silently treated as UTC.
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0))  # noqa: DTZ001
    except ValueError:
        return None
    if off in (None, "", "Z", "z"):
        return dt.replace(tzinfo=UTC)
    sign = 1 if off[0] == "+" else -1
    body = off[1:].replace(":", "")
    try:
        hours, minutes = int(body[:2]), int(body[2:4])
    except ValueError:
        return None
    from datetime import timedelta, timezone

    return dt.replace(tzinfo=timezone(sign * timedelta(hours=hours, minutes=minutes)))


def _display_precision(text: str) -> tuple[str, str | None]:
    cleaned = " ".join(text.split())
    for pattern, fmt, precision in _DISPLAY_PATTERNS:
        if pattern.match(cleaned):
            return precision, fmt
    return "unknown", None


def parse_source_date(
    display_text: str | None,
    machine_value: str | None = None,
    tz_text: str | None = None,
) -> ParsedDate:
    """Interpret a displayed date without ever over-committing its precision."""
    display = display_text.strip() if display_text else None
    machine = machine_value.strip() if machine_value else None

    if display is None and machine is None:
        return ParsedDate(None, None, tz_text, "absent", "unknown", None, None)
    if display is not None and display == "":
        return ParsedDate("", machine, tz_text, "absent", "unknown", None, None)

    precision, fmt = _display_precision(display) if display else ("unknown", None)

    dt = _parse_machine(machine) if machine else None
    if dt is not None:
        utc = dt.astimezone(UTC)
        local = dt.astimezone(OPERATIONAL_TZ)
        detail = None
        if precision == "date":
            detail = (
                "display is date-only; the machine value is the source's own "
                "placeholder time and must not be reported as a published time"
            )
        if precision == "unknown":
            # The page showed something we cannot read ("Ongoing", "September
            # 2026"). The machine value may be a placeholder, so publishing a
            # precise instant from it would invent a deadline the source never
            # displayed.
            return ParsedDate(
                display_text=display,
                machine_value=machine,
                tz_text=tz_text,
                parse_state="unparsed",
                precision="unknown",
                parsed_utc=None,
                parsed_local_date=None,
                detail="the displayed value matches no known date format, so the "
                "machine value is not interpretable as a published time",
            )
        return ParsedDate(
            display_text=display,
            machine_value=machine,
            tz_text=tz_text,
            parse_state="parsed",
            precision=precision,
            parsed_utc=utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            parsed_local_date=local.strftime("%Y-%m-%d"),
            detail=detail,
        )

    if display and fmt:
        try:
            # The display text carries no offset; the operational zone is
            # attached explicitly on the next line.
            naive = datetime.strptime(" ".join(display.split()), fmt)  # noqa: DTZ007
        except ValueError:
            return ParsedDate(
                display,
                machine,
                tz_text,
                "invalid",
                precision,
                None,
                None,
                detail=f"display text did not match its own pattern {fmt!r}",
            )
        # No machine value and no timezone: interpret in the operational zone
        # for the *date* only. We do not publish a synthetic instant.
        local = naive.replace(tzinfo=OPERATIONAL_TZ)
        return ParsedDate(
            display_text=display,
            machine_value=machine,
            tz_text=tz_text,
            parse_state="parsed",
            precision=precision,
            parsed_utc=None
            if precision == "date"
            else local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            parsed_local_date=local.strftime("%Y-%m-%d"),
            detail="no machine value on the page; interpreted from display text"
            + (" as a date only" if precision == "date" else ""),
        )

    if machine:
        return ParsedDate(
            display,
            machine,
            tz_text,
            "invalid",
            precision,
            None,
            None,
            detail="machine value present but not an interpretable timestamp",
        )
    return ParsedDate(
        display,
        machine,
        tz_text,
        "unparsed",
        precision,
        None,
        None,
        detail="no recognised date pattern",
    )
