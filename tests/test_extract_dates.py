"""Source date handling.

A displayed date is preserved as displayed; any interpretation is marked as
derived and never commits to a precision the page did not show.
"""

from __future__ import annotations

from rowanjobs.constants import DATE_PARSE_STATES, DATE_PRECISIONS
from rowanjobs.extract.dates import parse_source_date


def test_date_only_display_with_placeholder_machine_value_records_date_precision() -> None:
    """Rowan's "Advertised" line shows a bare date behind a 12:00Z placeholder."""
    parsed = parse_source_date("Sep 15 2026 ", "2026-09-15T12:00:00Z", "Eastern Daylight Time")
    assert parsed.parse_state == "parsed"
    assert parsed.precision == "date"
    assert parsed.display_text == "Sep 15 2026"
    assert parsed.machine_value == "2026-09-15T12:00:00Z"
    assert parsed.tz_text == "Eastern Daylight Time"
    assert parsed.parsed_local_date == "2026-09-15"
    assert parsed.detail is not None
    assert "must not be reported as a published time" in parsed.detail


def test_minute_precision_deadline_keeps_the_machine_instant() -> None:
    parsed = parse_source_date(
        "Sep 29 2026 11:55 PM", "2026-09-30T03:55:00Z", "Eastern Daylight Time"
    )
    assert parsed.precision == "minute"
    assert parsed.parse_state == "parsed"
    assert parsed.parsed_utc == "2026-09-30T03:55:00Z"
    assert parsed.parsed_local_date == "2026-09-29"
    assert parsed.detail is None


def test_date_only_display_without_a_machine_value_publishes_no_instant() -> None:
    parsed = parse_source_date("Sep 15 2026")
    assert parsed.parse_state == "parsed"
    assert parsed.precision == "date"
    assert parsed.parsed_utc is None
    assert parsed.parsed_local_date == "2026-09-15"
    assert parsed.detail is not None
    assert "as a date only" in parsed.detail


def test_minute_display_without_a_machine_value_is_read_in_the_operational_zone() -> None:
    parsed = parse_source_date("Sep 29 2026 11:55 PM")
    assert parsed.precision == "minute"
    # 23:55 America/New_York on 2026-09-29 is 03:55Z the next day.
    assert parsed.parsed_utc == "2026-09-30T03:55:00Z"
    assert parsed.parsed_local_date == "2026-09-29"


def test_missing_date_field_is_absent_not_unparsed() -> None:
    parsed = parse_source_date(None, None)
    assert parsed.parse_state == "absent"
    assert parsed.precision == "unknown"
    assert parsed.parsed_utc is None


def test_blank_date_field_is_absent_and_keeps_the_empty_display() -> None:
    parsed = parse_source_date("   ", "2026-09-15T12:00:00Z")
    assert parsed.parse_state == "absent"
    assert parsed.display_text == ""
    assert parsed.machine_value == "2026-09-15T12:00:00Z"
    assert parsed.parsed_utc is None


def test_empty_date_field_is_absent() -> None:
    assert parse_source_date("").parse_state == "absent"


def test_impossible_calendar_date_is_invalid_not_silently_shifted() -> None:
    parsed = parse_source_date("Sep 31 2026")
    assert parsed.parse_state == "invalid"
    assert parsed.precision == "date"
    assert parsed.parsed_utc is None
    assert parsed.parsed_local_date is None
    assert parsed.detail is not None
    assert "did not match its own pattern" in parsed.detail


def test_unrecognised_wording_is_unparsed_and_keeps_the_display_text() -> None:
    parsed = parse_source_date("Closes when filled")
    assert parsed.parse_state == "unparsed"
    assert parsed.precision == "unknown"
    assert parsed.display_text == "Closes when filled"


def test_uninterpretable_machine_value_alone_is_invalid() -> None:
    parsed = parse_source_date(None, "later this year")
    assert parsed.parse_state == "invalid"
    assert parsed.detail == "machine value present but not an interpretable timestamp"


def test_unusable_machine_value_falls_back_to_the_display_text() -> None:
    parsed = parse_source_date("Sep 15 2026", "2026-13-45T00:00:00Z")
    assert parsed.parse_state == "parsed"
    assert parsed.precision == "date"
    assert parsed.machine_value == "2026-13-45T00:00:00Z"
    assert parsed.parsed_local_date == "2026-09-15"


def test_machine_value_offset_is_honoured_rather_than_assumed_utc() -> None:
    parsed = parse_source_date("Sep 15 2026 02:00 PM", "2026-09-15T14:00:00+02:00")
    assert parsed.parsed_utc == "2026-09-15T12:00:00Z"


def test_alternative_display_orders_are_recognised_as_date_precision() -> None:
    for text in ("15 Sep 2026", "2026-09-15"):
        parsed = parse_source_date(text)
        assert parsed.precision == "date", text
        assert parsed.parsed_local_date == "2026-09-15", text


def test_full_month_name_is_flagged_rather_than_guessed_at() -> None:
    """The adapter only claims the abbreviated forms PageUp actually publishes."""
    parsed = parse_source_date("September 15 2026")
    assert parsed.parse_state == "invalid"
    assert parsed.parsed_utc is None
    assert parsed.display_text == "September 15 2026"


def test_every_state_and_precision_is_one_the_schema_allows() -> None:
    samples = [
        parse_source_date("Sep 15 2026", "2026-09-15T12:00:00Z"),
        parse_source_date("Sep 29 2026 11:55 PM"),
        parse_source_date(None, None),
        parse_source_date("Sep 31 2026"),
        parse_source_date("Closes when filled"),
    ]
    assert {s.parse_state for s in samples} == {"parsed", "absent", "invalid", "unparsed"}
    for sample in samples:
        assert sample.parse_state in DATE_PARSE_STATES
        assert sample.precision in DATE_PRECISIONS


def test_as_dict_carries_display_machine_and_derived_values_side_by_side() -> None:
    payload = parse_source_date(
        "Sep 29 2026 11:55 PM", "2026-09-30T03:55:00Z", "Eastern Daylight Time"
    ).as_dict()
    assert payload["display_text"] == "Sep 29 2026 11:55 PM"
    assert payload["machine_value"] == "2026-09-30T03:55:00Z"
    assert payload["tz_text"] == "Eastern Daylight Time"
    assert payload["parsed_utc"] == "2026-09-30T03:55:00Z"
