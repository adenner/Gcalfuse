"""Shared no-network fakes for FUSE tests."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from gcalfuse.cache import EventRecord


class FakeCalendarClient:
    """Stands in for gcalfuse.api.CalendarClient. Records every mutating call."""

    def __init__(self, tz: ZoneInfo, records: list[EventRecord] | None = None) -> None:
        self._tz = tz
        self._events: dict[str, EventRecord] = {r.event_id: r for r in (records or [])}
        self.insert_calls: list[dict] = []
        self.patch_calls: list[tuple[str, dict]] = []
        self.delete_calls: list[str] = []
        self._next_id = 1000

    def list_window(self, time_min, time_max):
        records = [
            r
            for r in self._events.values()
            if r.start <= time_max and (r.end or r.start) >= time_min
        ]
        return records, "fake-sync-token"

    def get(self, event_id: str) -> EventRecord | None:
        return self._events.get(event_id)

    def insert(self, body: dict) -> EventRecord:
        self.insert_calls.append(body)
        event_id = f"fake{self._next_id}"
        self._next_id += 1
        record = self._record_from_body(event_id, body)
        self._events[event_id] = record
        return record

    def patch(self, event_id: str, body: dict) -> EventRecord:
        self.patch_calls.append((event_id, body))
        existing = self._events[event_id]
        record = self._record_from_body(event_id, body, existing=existing)
        self._events[event_id] = record
        return record

    def delete(self, event_id: str) -> None:
        self.delete_calls.append(event_id)
        self._events.pop(event_id, None)

    def _record_from_body(
        self, event_id: str, body: dict, existing: EventRecord | None = None
    ) -> EventRecord:
        start_info = body.get("start")
        end_info = body.get("end")
        all_day = bool(start_info and "date" in start_info)

        if start_info is not None:
            start = self._parse_time(start_info)
            end = self._parse_time(end_info) if end_info else None
        elif existing is not None:
            start, end, all_day = existing.start, existing.end, existing.all_day
        else:
            raise ValueError("insert body must include a start time")

        recurring_event_id = existing.recurring_event_id if existing else None
        return EventRecord(
            event_id=event_id,
            summary=body.get("summary", existing.summary if existing else ""),
            description=body.get("description", existing.description if existing else ""),
            location=body.get("location", existing.location if existing else ""),
            all_day=all_day,
            start=start,
            end=end,
            recurring_event_id=recurring_event_id,
        )

    def _parse_time(self, info: dict) -> datetime:
        if "date" in info:
            return datetime.combine(
                date.fromisoformat(info["date"]), datetime.min.time(), tzinfo=self._tz
            )
        dt = datetime.fromisoformat(info["dateTime"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self._tz)
        return dt
