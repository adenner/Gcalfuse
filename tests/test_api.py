"""api.py: Google JSON mapping, pagination, sync tokens, error mapping, backoff."""

import json
from datetime import UTC, datetime

import httplib2
import pytest
from googleapiclient.errors import HttpError

from gcalfuse import api
from gcalfuse.api import (
    CalendarApiError,
    CalendarClient,
    SyncTokenExpiredError,
    _execute_with_backoff,
    map_event,
)

from .helpers import CHICAGO


def http_error(status: int, reason: str | None = None) -> HttpError:
    content = {"error": {"code": status, "errors": [{"reason": reason}] if reason else []}}
    return HttpError(httplib2.Response({"status": status}), json.dumps(content).encode())


class Request:
    """A googleapiclient request: .execute() returns or raises the next outcome."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def execute(self):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeEvents:
    def __init__(self):
        self.list_calls: list[dict] = []
        self.pages: list = []
        self.responses: dict[str, object] = {}

    def list(self, **params):
        self.list_calls.append(params)
        return Request(self.pages.pop(0))

    def _one(self, name, **params):
        self.responses.setdefault(name + "_params", []).append(params)
        return Request(self.responses[name])

    def get(self, **params):
        return self._one("get", **params)

    def insert(self, **params):
        return self._one("insert", **params)

    def patch(self, **params):
        return self._one("patch", **params)

    def delete(self, **params):
        return self._one("delete", **params)


class FakeService:
    def __init__(self):
        self.events_resource = FakeEvents()

    def events(self):
        return self.events_resource


def client_with_service():
    service = FakeService()
    return CalendarClient(service, "primary", CHICAGO), service.events_resource


def raw_event(event_id="e1", **extra):
    raw = {
        "id": event_id,
        "summary": "Standup",
        "start": {"dateTime": "2026-09-23T14:00:00Z"},
        "end": {"dateTime": "2026-09-23T14:30:00Z"},
        "updated": "2026-09-01T12:00:00.000Z",
    }
    raw.update(extra)
    return raw


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda s: None)


# -- map_event -----------------------------------------------------------------


def test_map_timed_event_is_expressed_in_configured_zone():
    record = map_event(raw_event(), CHICAGO)
    assert record.start == datetime(2026, 9, 23, 9, 0, tzinfo=CHICAGO)
    assert record.start.tzinfo is CHICAGO  # not a fixed "-05:00" offset
    assert record.end.hour == 9 and record.end.minute == 30
    assert record.updated == datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    assert not record.all_day and not record.is_recurring_instance


def test_map_all_day_event():
    record = map_event(raw_event(start={"date": "2026-09-24"}, end={"date": "2026-09-26"}), CHICAGO)
    assert record.all_day
    assert record.start.date().isoformat() == "2026-09-24"
    assert record.end.date().isoformat() == "2026-09-26"


def test_map_recurring_instance_and_people():
    record = map_event(
        raw_event(
            recurringEventId="series1",
            attendees=[{"email": "a@example.com"}, {"displayName": "no email"}],
            organizer={"email": "o@example.com"},
            htmlLink="https://calendar.google.com/x",
        ),
        CHICAGO,
    )
    assert record.is_recurring_instance
    assert record.attendees == ["a@example.com"]
    assert record.organizer == "o@example.com"
    assert record.html_link == "https://calendar.google.com/x"


def test_map_recurrence_keeps_only_rrule_value():
    record = map_event(
        raw_event(recurrence=["EXDATE:20260930T140000Z", "RRULE:FREQ=WEEKLY;BYDAY=WE"]), CHICAGO
    )
    assert record.rrule == "FREQ=WEEKLY;BYDAY=WE"


def test_map_event_without_summary_or_end():
    raw = raw_event()
    del raw["summary"], raw["end"]
    record = map_event(raw, CHICAGO)
    assert record.summary == "" and record.end is None


# -- list_window / list_updates --------------------------------------------------


def test_list_window_paginates_skips_cancelled_and_returns_sync_token():
    client, events = client_with_service()
    events.pages = [
        {"items": [raw_event("a"), raw_event("x", status="cancelled")], "nextPageToken": "p2"},
        {"items": [raw_event("b")], "nextSyncToken": "sync1"},
    ]
    records, token = client.list_window(
        datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)
    )
    assert [r.event_id for r in records] == ["a", "b"]
    assert token == "sync1"
    first, second = events.list_calls
    assert first["singleEvents"] is True and first["orderBy"] == "startTime"
    assert first["timeMin"] == "2026-09-01T00:00:00+00:00"
    assert "pageToken" not in first and second["pageToken"] == "p2"


def test_list_updates_reports_cancellations_as_deletions():
    client, events = client_with_service()
    events.pages = [
        {"items": [raw_event("a"), {"id": "gone", "status": "cancelled"}], "nextSyncToken": "s2"}
    ]
    records, deleted, token = client.list_updates("s1")
    assert [r.event_id for r in records] == ["a"]
    assert deleted == ["gone"] and token == "s2"
    assert events.list_calls[0]["syncToken"] == "s1"
    assert "timeMin" not in events.list_calls[0]  # not allowed with syncToken


def test_list_updates_expired_token_raises_sync_token_expired():
    client, events = client_with_service()

    def expired(**params):
        return Request(http_error(410))

    events.list = expired
    with pytest.raises(SyncTokenExpiredError):
        client.list_updates("old")


# -- get / insert / patch / delete ---------------------------------------------------


def test_get_missing_event_returns_none():
    client, events = client_with_service()
    events.responses["get"] = http_error(404)
    assert client.get("nope") is None


def test_get_cancelled_event_returns_none():
    client, events = client_with_service()
    events.responses["get"] = raw_event(status="cancelled")
    assert client.get("e1") is None


def test_insert_patch_delete_pass_calendar_id_and_body():
    client, events = client_with_service()
    events.responses.update(insert=raw_event("new"), patch=raw_event("e1"), delete="")
    assert client.insert({"summary": "x"}).event_id == "new"
    assert client.patch("e1", {"summary": "y"}).event_id == "e1"
    client.delete("e1")
    assert events.responses["insert_params"] == [
        {"calendarId": "primary", "body": {"summary": "x"}}
    ]
    assert events.responses["patch_params"][0]["eventId"] == "e1"
    assert events.responses["delete_params"] == [{"calendarId": "primary", "eventId": "e1"}]


def test_permanent_error_is_wrapped_with_status():
    client, events = client_with_service()
    events.responses["delete"] = http_error(404)
    with pytest.raises(CalendarApiError) as exc_info:
        client.delete("e1")
    assert exc_info.value.status == 404 and exc_info.value.not_found


# -- backoff ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        http_error(429),
        http_error(503),
        http_error(403, "rateLimitExceeded"),
        ConnectionResetError(),
    ],
)
def test_transient_errors_are_retried(error):
    request = Request(error, error, {"ok": True})
    assert _execute_with_backoff(request) == {"ok": True}
    assert request.calls == 3


@pytest.mark.parametrize(
    "error", [http_error(400), http_error(401), http_error(403, "forbidden"), http_error(404)]
)
def test_permanent_errors_are_not_retried(error):
    request = Request(error, {"ok": True})
    with pytest.raises(CalendarApiError):
        _execute_with_backoff(request)
    assert request.calls == 1


def test_backoff_gives_up_after_max_retries_with_exponential_delays():
    delays = []
    request = Request(*[http_error(429)] * (api._MAX_RETRIES + 1))
    with pytest.raises(CalendarApiError) as exc_info:
        _execute_with_backoff(request, sleep=delays.append)
    assert exc_info.value.status == 429
    assert request.calls == api._MAX_RETRIES + 1
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0]
