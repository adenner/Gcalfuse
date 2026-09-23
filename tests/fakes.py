"""Shared no-network fakes for tests.

FakeCalendarClient deliberately enforces the Calendar API rules that have
bitten us: it rejects a dateTime with neither an offset nor a timeZone, an
unknown timeZone name, a missing start/end on insert, and an end before the
start. Responses are built as Google-shaped JSON and mapped through the real
`api.map_event`, so tests exercise the same code path as production.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gcalfuse.api import CalendarApiError, map_event
from gcalfuse.cache import EventRecord


class FakeCalendarClient:
    """Stands in for gcalfuse.api.CalendarClient. Records every mutating call.

    Set `fail_next` to a CalendarApiError to make the next insert/patch/delete
    raise it (and not record a successful call).
    """

    def __init__(self, tz: ZoneInfo, records: list[EventRecord] | None = None) -> None:
        self._tz = tz
        self._events: dict[str, EventRecord] = {r.event_id: r for r in (records or [])}
        self.insert_calls: list[dict] = []
        self.patch_calls: list[tuple[str, dict]] = []
        self.delete_calls: list[str] = []
        self.fail_next: CalendarApiError | None = None
        self._next_id = 1000

    # -- reads -------------------------------------------------------------

    def list_window(self, time_min, time_max):
        records = [
            r
            for r in self._events.values()
            if r.start <= time_max and (r.end or r.start) >= time_min
        ]
        return records, "fake-sync-token"

    def get(self, event_id: str) -> EventRecord | None:
        return self._events.get(event_id)

    # -- writes ------------------------------------------------------------

    def insert(self, body: dict) -> EventRecord:
        self._maybe_fail()
        for key in ("start", "end"):
            if key not in body:
                raise CalendarApiError(f"400 Missing {key} time", status=400)
        self.insert_calls.append(body)
        event_id = f"fake{self._next_id}"
        self._next_id += 1
        return self._store(event_id, body, existing=None)

    def patch(self, event_id: str, body: dict) -> EventRecord:
        self._maybe_fail()
        existing = self._events.get(event_id)
        if existing is None:
            raise CalendarApiError("404 Not Found", status=404)
        self.patch_calls.append((event_id, body))
        return self._store(event_id, body, existing=existing)

    def delete(self, event_id: str) -> None:
        self._maybe_fail()
        if event_id not in self._events:
            raise CalendarApiError("410 Resource has been deleted", status=410)
        self.delete_calls.append(event_id)
        del self._events[event_id]

    # -- helpers ---------------------------------------------------------------

    def _maybe_fail(self) -> None:
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc

    def _store(self, event_id: str, body: dict, existing: EventRecord | None) -> EventRecord:
        raw = {
            "id": event_id,
            "summary": body.get("summary", existing.summary if existing else ""),
            "description": body.get("description", existing.description if existing else ""),
            "location": body.get("location", existing.location if existing else ""),
            "start": self._time(body["start"])
            if "start" in body
            else self._raw_time(existing, "start"),
            "end": self._time(body["end"]) if "end" in body else self._raw_time(existing, "end"),
            "updated": "2026-01-01T00:00:00Z",
        }
        if existing is not None and existing.recurring_event_id:
            raw["recurringEventId"] = existing.recurring_event_id
        self._check_order(raw)
        record = map_event(raw, self._tz)
        self._events[event_id] = record
        return record

    def _time(self, info: dict) -> dict:
        """Validate a start/end dict like Google and return it with an explicit offset."""
        if "date" in info:
            date.fromisoformat(info["date"])
            return {"date": info["date"]}
        dt = datetime.fromisoformat(info["dateTime"])
        tz_name = info.get("timeZone")
        if tz_name is not None:
            try:
                zone = ZoneInfo(tz_name)
            except (ZoneInfoNotFoundError, ValueError):
                raise CalendarApiError(
                    f"400 Invalid time zone definition: {tz_name}", status=400
                ) from None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=zone)
        if dt.tzinfo is None:
            raise CalendarApiError("400 Missing time zone definition for start time", status=400)
        return {"dateTime": dt.isoformat()}

    @staticmethod
    def _raw_time(existing: EventRecord, key: str) -> dict:
        value = getattr(existing, key)
        if existing.all_day:
            return {"date": value.date().isoformat()}
        return {"dateTime": value.isoformat()}

    @staticmethod
    def _check_order(raw: dict) -> None:
        def key(info: dict):
            if "date" in info:
                return datetime.combine(date.fromisoformat(info["date"]), datetime.min.time())
            return datetime.fromisoformat(info["dateTime"]).replace(tzinfo=None)

        start, end = raw["start"], raw["end"]
        if ("date" in start) != ("date" in end):
            raise CalendarApiError("400 Start and end must both be dates or dateTimes", status=400)
        if "date" in start and key(end) <= key(start):
            raise CalendarApiError("400 The specified time range is empty", status=400)
        if "dateTime" in start:
            s = datetime.fromisoformat(start["dateTime"])
            e = datetime.fromisoformat(end["dateTime"])
            if e < s:
                raise CalendarApiError("400 The specified time range is empty", status=400)


class FakeSyncClient(FakeCalendarClient):
    """A FakeCalendarClient that also supports syncToken deltas (list_updates)."""

    def __init__(self, tz: ZoneInfo, records: list[EventRecord] | None = None) -> None:
        super().__init__(tz, records)
        self.list_window_calls = 0
        self.next_delta: tuple[list[EventRecord], list[str]] = ([], [])
        self.expire_token = False

    def list_window(self, time_min, time_max):
        self.list_window_calls += 1
        return super().list_window(time_min, time_max)

    def list_updates(self, sync_token: str):
        from gcalfuse.api import SyncTokenExpiredError

        if self.expire_token:
            self.expire_token = False
            raise SyncTokenExpiredError("410 Sync token is no longer valid", status=410)
        records, deleted = self.next_delta
        self.next_delta = ([], [])
        return records, deleted, "fake-sync-token-2"


def sample_events(tz: ZoneInfo, today: date) -> list[EventRecord]:
    """A small, realistic calendar around `today`, used by scripts/dev_mount.py."""
    from datetime import timedelta

    def at(day_offset: int, hour: int, minute: int = 0) -> datetime:
        d = today + timedelta(days=day_offset)
        return datetime(d.year, d.month, d.day, hour, minute, tzinfo=tz)

    tomorrow = today + timedelta(days=1)
    return [
        EventRecord("standup01", "Standup", at(0, 9), end=at(0, 9, 30), description="Daily sync"),
        EventRecord("oneonone1", "1-on-1", at(0, 14), end=at(0, 15), location="Room 4"),
        EventRecord(
            "pto000001",
            "PTO",
            datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=tz),
            end=datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=tz)
            + timedelta(days=1),
            all_day=True,
        ),
        EventRecord(
            "weekly_20260925",
            "Weekly Sync",
            at(2, 11),
            end=at(2, 11, 30),
            recurring_event_id="weekly",
        ),
    ]
