"""ICS (RFC 5545) emit/parse for single-VEVENT calendar files."""

from __future__ import annotations

from datetime import UTC, date, datetime

from icalendar import Calendar
from icalendar import Event as ICalEvent

from .cache import EventRecord

PRODID = "-//gcalfuse//EN"


class IcsValidationError(ValueError):
    """Raised when ICS bytes do not contain exactly one VEVENT."""


def event_to_ics(record: EventRecord) -> bytes:
    """Render an EventRecord as a minimal single-VEVENT .ics file."""
    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")

    vevent = ICalEvent()
    vevent.add("uid", f"{record.event_id}@gcalfuse")
    vevent.add("dtstamp", record.updated or datetime.now(UTC))

    if record.all_day:
        vevent.add("dtstart", record.start.date())
        if record.end is not None:
            vevent.add("dtend", record.end.date())
    else:
        vevent.add("dtstart", record.start)
        if record.end is not None:
            vevent.add("dtend", record.end)

    vevent.add("summary", record.summary)
    if record.description:
        vevent.add("description", record.description)
    if record.location:
        vevent.add("location", record.location)
    if record.status:
        vevent.add("status", record.status.upper())
    if record.organizer:
        vevent.add("organizer", record.organizer)
    for attendee in record.attendees:
        vevent.add("attendee", attendee)
    if record.rrule:
        vevent.add("rrule", record.rrule)

    vevent.add("x-google-event-id", record.event_id)
    if record.html_link:
        vevent.add("x-google-html-link", record.html_link)

    cal.add_component(vevent)
    return cal.to_ical()


def ics_to_event_patch(ics_bytes: bytes) -> dict:
    """Parse ICS bytes into a Calendar API-shaped insert/patch body.

    Raises IcsValidationError unless the calendar contains exactly one VEVENT.
    """
    try:
        cal = Calendar.from_ical(ics_bytes)
    except ValueError as exc:
        raise IcsValidationError(f"could not parse ICS: {exc}") from exc

    vevents = [component for component in cal.walk() if component.name == "VEVENT"]
    if len(vevents) != 1:
        raise IcsValidationError(f"expected exactly one VEVENT, found {len(vevents)}")
    vevent = vevents[0]

    body: dict = {"summary": str(vevent.get("summary", "") or "")}

    description = vevent.get("description")
    if description:
        body["description"] = str(description)

    location = vevent.get("location")
    if location:
        body["location"] = str(location)

    dtstart = vevent.get("dtstart")
    if dtstart is not None:
        body["start"] = _prop_to_api_time(dtstart)

    dtend = vevent.get("dtend")
    if dtend is not None:
        body["end"] = _prop_to_api_time(dtend)

    return body


def _prop_to_api_time(prop) -> dict:
    """Convert an icalendar dt property to a Google Calendar API start/end dict."""
    value = prop.dt
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            tz_name = getattr(value.tzinfo, "key", None) or str(value.tzinfo)
            return {"dateTime": value.isoformat(), "timeZone": tz_name}
        return {"dateTime": value.isoformat()}
    if isinstance(value, date):
        return {"date": value.isoformat()}
    raise IcsValidationError(f"unsupported dtstart/dtend value: {value!r}")
