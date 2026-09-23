"""ICS (RFC 5545) emit/parse for single-VEVENT calendar files."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta, tzinfo

from icalendar import Calendar, vCalAddress, vRecur
from icalendar import Event as ICalEvent

from .cache import EventRecord

logger = logging.getLogger(__name__)

PRODID = "-//gcalfuse//EN"
_UTF8_BOM = b"\xef\xbb\xbf"


class IcsValidationError(ValueError):
    """Raised when ICS bytes are unparseable or don't hold exactly one usable VEVENT."""


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
        vevent.add("organizer", vCalAddress(f"mailto:{record.organizer}"))
    for attendee in record.attendees:
        vevent.add("attendee", vCalAddress(f"mailto:{attendee}"))
    if record.rrule:
        vevent.add("rrule", vRecur.from_ical(record.rrule))

    vevent.add("x-google-event-id", record.event_id)
    if record.html_link:
        vevent.add("x-google-html-link", record.html_link)

    cal.add_component(vevent)
    return cal.to_ical()


def ics_to_event_patch(ics_bytes: bytes, default_tz: tzinfo | None = None) -> dict:
    """Parse ICS bytes into a Calendar API-shaped insert/patch body.

    - SUMMARY/DESCRIPTION/LOCATION are always present (possibly ""), so that
      deleting a line from the file clears that field in Google on patch.
    - A floating DTSTART/DTEND (no TZID, no Z) is interpreted in `default_tz`;
      Google rejects a dateTime that has neither an offset nor a timeZone.
    - DURATION is converted to an end time when DTEND is absent.
    - RRULE is ignored (v1 never creates or edits recurrence) with a warning.

    Raises IcsValidationError unless the input holds exactly one VEVENT whose
    date properties are well-formed.
    """
    try:
        cal = Calendar.from_ical(ics_bytes.removeprefix(_UTF8_BOM))
    except Exception as exc:  # icalendar raises assorted types on garbage input
        raise IcsValidationError(f"could not parse ICS: {exc}") from exc

    vevents = [component for component in cal.walk() if component.name == "VEVENT"]
    if len(vevents) != 1:
        raise IcsValidationError(f"expected exactly one VEVENT, found {len(vevents)}")
    vevent = vevents[0]

    body: dict = {
        "summary": _text(vevent, "summary"),
        "description": _text(vevent, "description"),
        "location": _text(vevent, "location"),
    }

    if vevent.get("rrule") is not None:
        logger.warning("RRULE ignored: v1 only creates and edits non-recurring events")

    dtstart = _single(vevent, "dtstart")
    if dtstart is None:
        return body
    start_value = _localize(dtstart.dt, default_tz)
    body["start"] = _to_api_time(start_value)

    dtend = _single(vevent, "dtend")
    duration = _single(vevent, "duration")
    if dtend is not None:
        body["end"] = _to_api_time(_localize(dtend.dt, default_tz))
    elif duration is not None:
        if not isinstance(duration.dt, timedelta):
            raise IcsValidationError(f"unsupported DURATION value: {duration.dt!r}")
        body["end"] = _to_api_time(start_value + duration.dt)

    return body


def _text(vevent, name: str) -> str:
    value = vevent.get(name)
    if isinstance(value, list):
        raise IcsValidationError(f"{name.upper()} appears more than once")
    return str(value) if value else ""


def _single(vevent, name: str):
    """Return a date-ish property, rejecting duplicates and unparseable values."""
    prop = vevent.get(name)
    if prop is None:
        return None
    if isinstance(prop, list):
        raise IcsValidationError(f"{name.upper()} appears more than once")
    try:
        prop.dt  # noqa: B018 -- icalendar defers parse errors until .dt is read
    except Exception as exc:
        raise IcsValidationError(f"could not parse {name.upper()}: {exc}") from exc
    return prop


def _localize(value, default_tz: tzinfo | None):
    if isinstance(value, datetime) and value.tzinfo is None and default_tz is not None:
        return value.replace(tzinfo=default_tz)
    return value


def _to_api_time(value) -> dict:
    """Convert a date/datetime into a Google Calendar API start/end dict."""
    if isinstance(value, datetime):
        body = {"dateTime": value.isoformat()}
        tz_name = _iana_name(value.tzinfo)
        if tz_name:
            body["timeZone"] = tz_name
        return body
    if isinstance(value, date):
        return {"date": value.isoformat()}
    raise IcsValidationError(f"unsupported date/time value: {value!r}")


def _iana_name(tz: tzinfo | None) -> str | None:
    """IANA zone name for timeZone, or None when the offset alone must suffice.

    Only real zone names are valid for Google's timeZone field; a fixed
    offset like 'UTC-05:00' is rejected, so it's left to the dateTime suffix.
    """
    if tz is None:
        return None
    key = getattr(tz, "key", None) or getattr(tz, "zone", None)
    if key:
        return key
    if tz is UTC:
        return "UTC"
    return None
