from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from gcalfuse.cache import EventRecord
from gcalfuse.icsutil import IcsValidationError, event_to_ics, ics_to_event_patch

CHICAGO = ZoneInfo("America/Chicago")


def test_ics_roundtrip_timed_event():
    record = EventRecord(
        event_id="event123",
        summary="Team Standup",
        description="Daily sync",
        location="Room 4",
        start=datetime(2026, 9, 23, 9, 0, tzinfo=CHICAGO),
        end=datetime(2026, 9, 23, 9, 30, tzinfo=CHICAGO),
    )

    ics_bytes = event_to_ics(record)
    assert ics_bytes.startswith(b"BEGIN:VCALENDAR")

    patch = ics_to_event_patch(ics_bytes)
    assert patch["summary"] == "Team Standup"
    assert patch["description"] == "Daily sync"
    assert patch["location"] == "Room 4"
    assert patch["start"]["dateTime"].startswith("2026-09-23T09:00:00")
    assert patch["end"]["dateTime"].startswith("2026-09-23T09:30:00")


def test_ics_parse_all_day_date():
    record = EventRecord(
        event_id="pto1",
        summary="PTO",
        start=datetime(2026, 9, 24, 0, 0, tzinfo=CHICAGO),
        end=datetime(2026, 9, 25, 0, 0, tzinfo=CHICAGO),
        all_day=True,
    )

    ics_bytes = event_to_ics(record)
    patch = ics_to_event_patch(ics_bytes)
    assert patch["start"] == {"date": "2026-09-24"}
    assert patch["end"] == {"date": "2026-09-25"}


def test_ics_with_two_vevents_fails():
    ics_bytes = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//gcalfuse//EN
BEGIN:VEVENT
UID:one@gcalfuse
DTSTAMP:20260101T000000Z
DTSTART:20260923T090000
SUMMARY:One
END:VEVENT
BEGIN:VEVENT
UID:two@gcalfuse
DTSTAMP:20260101T000000Z
DTSTART:20260923T100000
SUMMARY:Two
END:VEVENT
END:VCALENDAR
"""
    with pytest.raises(IcsValidationError):
        ics_to_event_patch(ics_bytes)


def test_ics_with_zero_vevents_fails():
    ics_bytes = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//gcalfuse//EN
END:VCALENDAR
"""
    with pytest.raises(IcsValidationError):
        ics_to_event_patch(ics_bytes)


def test_event_to_ics_contains_google_event_id():
    record = EventRecord(
        event_id="event456",
        summary="1:1",
        start=datetime(2026, 9, 23, 14, 0, tzinfo=CHICAGO),
        end=datetime(2026, 9, 23, 15, 0, tzinfo=CHICAGO),
    )
    ics_bytes = event_to_ics(record)
    text = ics_bytes.decode()
    assert "X-GOOGLE-EVENT-ID:event456" in text
    assert "UID:event456@gcalfuse" in text
