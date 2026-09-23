"""icsutil.py: ICS emission and parsing into Calendar API bodies."""

from datetime import UTC, datetime

import pytest
from icalendar import Calendar

from gcalfuse.cache import EventRecord
from gcalfuse.icsutil import IcsValidationError, event_to_ics, ics_to_event_patch

from .helpers import CHICAGO, ics, make_all_day, make_event


def vevent_of(data: bytes):
    return next(c for c in Calendar.from_ical(data).walk() if c.name == "VEVENT")


# -- emit ---------------------------------------------------------------------


def test_emitted_file_has_required_structure():
    data = event_to_ics(make_event("event123", "Team Standup", day=23))
    text = data.decode()
    assert text.startswith("BEGIN:VCALENDAR\r\n")
    for line in (
        "VERSION:2.0",
        "PRODID:-//gcalfuse//EN",
        "UID:event123@gcalfuse",
        "X-GOOGLE-EVENT-ID:event123",
        "DTSTART;TZID=America/Chicago:20260923T090000",
        "DTEND;TZID=America/Chicago:20260923T093000",
        "STATUS:CONFIRMED",
    ):
        assert line in text
    assert "DTSTAMP:" in text
    assert len([c for c in Calendar.from_ical(data).walk() if c.name == "VEVENT"]) == 1


def test_emit_all_day_uses_date_values():
    text = event_to_ics(make_all_day("pto", "PTO", day=24, days=2)).decode()
    assert "DTSTART;VALUE=DATE:20260924" in text
    assert "DTEND;VALUE=DATE:20260926" in text


def test_emit_optional_fields_only_when_present():
    bare = event_to_ics(make_event("e", "x", day=23)).decode()
    for prop in ("DESCRIPTION", "LOCATION", "ORGANIZER", "ATTENDEE", "RRULE", "X-GOOGLE-HTML-LINK"):
        assert prop not in bare


def test_emit_people_as_mailto_uris():
    record = make_event("e", "x", day=23, organizer="o@example.com", attendees=["a@example.com"])
    text = event_to_ics(record).decode()
    assert "ORGANIZER:mailto:o@example.com" in text
    assert "ATTENDEE:mailto:a@example.com" in text


def test_emit_rrule_and_html_link():
    record = make_event(
        "e", "x", day=23, rrule="FREQ=WEEKLY;BYDAY=WE", html_link="https://x.test/e"
    )
    text = event_to_ics(record).decode()
    assert "RRULE:FREQ=WEEKLY;BYDAY=WE" in text
    assert "X-GOOGLE-HTML-LINK:https://x.test/e" in text


def test_emit_dtstamp_uses_updated_time_so_output_is_stable():
    record = make_event("e", "x", day=23, updated=datetime(2026, 8, 1, tzinfo=UTC))
    assert event_to_ics(record) == event_to_ics(record)
    assert b"DTSTAMP:20260801T000000Z" in event_to_ics(record)


def test_emit_escapes_special_characters_and_newlines():
    record = make_event(
        "e", "Lunch; tacos, maybe", day=23, description="line one\nline two", location="A, B"
    )
    body = ics_to_event_patch(event_to_ics(record), CHICAGO)
    assert body["summary"] == "Lunch; tacos, maybe"
    assert body["description"] == "line one\nline two"
    assert body["location"] == "A, B"


def test_emit_unicode_summary_roundtrips():
    record = make_event("e", "Café ☕ – Zürich", day=23)
    assert ics_to_event_patch(event_to_ics(record), CHICAGO)["summary"] == "Café ☕ – Zürich"


# -- parse --------------------------------------------------------------------


def test_roundtrip_timed_event():
    record = make_event("e", "Team Standup", day=23, description="Daily sync", location="Room 4")
    body = ics_to_event_patch(event_to_ics(record), CHICAGO)
    assert body == {
        "summary": "Team Standup",
        "description": "Daily sync",
        "location": "Room 4",
        "start": {"dateTime": "2026-09-23T09:00:00-05:00", "timeZone": "America/Chicago"},
        "end": {"dateTime": "2026-09-23T09:30:00-05:00", "timeZone": "America/Chicago"},
    }


def test_roundtrip_all_day_event():
    body = ics_to_event_patch(event_to_ics(make_all_day("pto", "PTO", day=24)), CHICAGO)
    assert body["start"] == {"date": "2026-09-24"}
    assert body["end"] == {"date": "2026-09-25"}


def test_absent_text_fields_are_empty_strings_so_patch_clears_them():
    body = ics_to_event_patch(ics("DTSTART:20260924T150000"), CHICAGO)
    assert body["summary"] == body["description"] == body["location"] == ""


def test_floating_time_is_interpreted_in_default_zone():
    body = ics_to_event_patch(ics("DTSTART:20260924T150000"), CHICAGO)
    assert body["start"] == {"dateTime": "2026-09-24T15:00:00-05:00", "timeZone": "America/Chicago"}


def test_utc_time_keeps_utc():
    body = ics_to_event_patch(ics("DTSTART:20260924T200000Z"), CHICAGO)
    assert body["start"] == {"dateTime": "2026-09-24T20:00:00+00:00", "timeZone": "UTC"}


def test_explicit_tzid_is_preserved():
    body = ics_to_event_patch(ics("DTSTART;TZID=Asia/Tokyo:20260925T010000"), CHICAGO)
    assert body["start"] == {"dateTime": "2026-09-25T01:00:00+09:00", "timeZone": "Asia/Tokyo"}


def test_duration_becomes_end():
    body = ics_to_event_patch(ics("DTSTART:20260924T150000", "DURATION:PT90M"), CHICAGO)
    assert body["end"]["dateTime"] == "2026-09-24T16:30:00-05:00"


def test_date_duration_becomes_end_date():
    body = ics_to_event_patch(ics("DTSTART;VALUE=DATE:20260924", "DURATION:P3D"), CHICAGO)
    assert body["end"] == {"date": "2026-09-27"}


def test_missing_dtstart_yields_no_start_key():
    assert "start" not in ics_to_event_patch(ics("SUMMARY:x"), CHICAGO)


def test_bare_vevent_without_vcalendar_wrapper_is_accepted():
    data = b"BEGIN:VEVENT\r\nSUMMARY:x\r\nDTSTART:20260924T150000\r\nEND:VEVENT\r\n"
    assert ics_to_event_patch(data, CHICAGO)["summary"] == "x"


def test_utf8_bom_and_lf_line_endings_are_accepted():
    data = b"\xef\xbb\xbf" + ics("SUMMARY:x", "DTSTART:20260924T150000").replace(b"\r\n", b"\n")
    assert ics_to_event_patch(data, CHICAGO)["summary"] == "x"


def test_rrule_is_ignored_with_warning(caplog):
    body = ics_to_event_patch(ics("DTSTART:20260924T150000", "RRULE:FREQ=DAILY"), CHICAGO)
    assert "recurrence" not in body
    assert "RRULE ignored" in caplog.text


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not an ics file",
        b"\xff\xfe\x00garbage",
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n",
        ics("SUMMARY:one") + ics("SUMMARY:two"),
        ics("SUMMARY:a", "END:VEVENT", "BEGIN:VEVENT", "SUMMARY:b"),
        ics("DTSTART:20260924T150000Z", "DTSTART:20260925T150000Z"),
        ics("DTSTART:not-a-date"),
        ics("DTSTART:20260924T150000", "DTEND:tomorrow"),
        ics("DTSTART:20260924T150000", "DURATION:about an hour"),
        ics("SUMMARY:a", "SUMMARY:b"),
    ],
    ids=[
        "empty",
        "plain-text",
        "binary",
        "no-vevent",
        "two-calendars",
        "two-vevents",
        "duplicate-dtstart",
        "bad-dtstart",
        "bad-dtend",
        "bad-duration",
        "duplicate-summary",
    ],
)
def test_malformed_input_raises_validation_error(data):
    with pytest.raises(IcsValidationError):
        ics_to_event_patch(data, CHICAGO)


def test_emitted_file_for_every_field_parses_back():
    """Everything event_to_ics can produce must be acceptable ics_to_event_patch input."""
    record = EventRecord(
        event_id="full",
        summary="Everything",
        start=datetime(2026, 9, 23, 9, tzinfo=CHICAGO),
        end=datetime(2026, 9, 23, 10, tzinfo=CHICAGO),
        description="d",
        location="l",
        status="tentative",
        organizer="o@example.com",
        attendees=["a@example.com", "b@example.com"],
        rrule="FREQ=WEEKLY",
        recurring_event_id="series",
        html_link="https://x.test",
        updated=datetime(2026, 1, 1, tzinfo=UTC),
    )
    body = ics_to_event_patch(event_to_ics(record), CHICAGO)
    assert body["summary"] == "Everything"
    assert vevent_of(event_to_ics(record))["STATUS"] == "TENTATIVE"
