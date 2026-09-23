"""Thin wrapper around the Google Calendar API.

Everything above this module (cache refresh, fs.py) sees only EventRecord
objects and CalendarApiError; googleapiclient/httplib2 exceptions never
escape. That matters because pyfuse3 tears down the whole mount if a FUSE
handler raises anything other than FUSEError.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError

from .cache import EventRecord

logger = logging.getLogger(__name__)

API_SERVICE_NAME = "calendar"
API_VERSION = "v3"
PAGE_SIZE = 250

# Retried: 429 and 5xx, plus the 403 "reasons" Google Calendar actually uses
# for rate limiting (it returns 403 rateLimitExceeded more often than 429).
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_RETRYABLE_403_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}
_MAX_RETRIES = 5
_BACKOFF_BASE_SECONDS = 1.0


class CalendarApiError(RuntimeError):
    """A Calendar API call failed after retries. `status` is the HTTP status, if any."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def not_found(self) -> bool:
        return self.status in (404, 410)


class SyncTokenExpiredError(CalendarApiError):
    """Google rejected a sync token (HTTP 410); the caller should full-refetch."""


class CalendarClientProtocol(Protocol):
    """What cache refresh and fs.py need from a Calendar client, real or fake.

    Implementations raise CalendarApiError (never a library-specific error).
    """

    def list_window(
        self, time_min: datetime, time_max: datetime
    ) -> tuple[list[EventRecord], str | None]: ...

    def get(self, event_id: str) -> EventRecord | None: ...

    def insert(self, body: dict) -> EventRecord: ...

    def patch(self, event_id: str, body: dict) -> EventRecord: ...

    def delete(self, event_id: str) -> None: ...


def map_event(raw: dict, default_tz: ZoneInfo) -> EventRecord:
    """Map a Google Calendar API event resource into an EventRecord."""
    start_info = raw.get("start", {})
    end_info = raw.get("end", {})
    all_day = "date" in start_info

    if all_day:
        start = _midnight(start_info["date"], default_tz)
        end = _midnight(end_info["date"], default_tz) if "date" in end_info else None
    else:
        start = _parse_datetime(start_info, default_tz)
        end = _parse_datetime(end_info, default_tz) if "dateTime" in end_info else None

    attendees = [a["email"] for a in raw.get("attendees", []) if a.get("email")]
    organizer = raw.get("organizer", {}).get("email")
    updated = _parse_iso(raw["updated"]) if raw.get("updated") else None

    return EventRecord(
        event_id=raw["id"],
        summary=raw.get("summary", ""),
        description=raw.get("description", ""),
        location=raw.get("location", ""),
        all_day=all_day,
        start=start,
        end=end,
        status=raw.get("status", "confirmed"),
        organizer=organizer,
        attendees=attendees,
        rrule=_rrule_from_recurrence(raw.get("recurrence", [])),
        recurring_event_id=raw.get("recurringEventId"),
        html_link=raw.get("htmlLink"),
        updated=updated,
    )


def _rrule_from_recurrence(lines: list[str]) -> str | None:
    """Google recurrence is a list of 'RRULE:...', 'EXDATE:...' lines; keep the RRULE value."""
    for line in lines:
        if line.startswith("RRULE:"):
            return line.removeprefix("RRULE:")
    return None


def _midnight(date_str: str, tz: ZoneInfo) -> datetime:
    return datetime.combine(date.fromisoformat(date_str), datetime.min.time(), tzinfo=tz)


def _parse_datetime(info: dict, default_tz: ZoneInfo) -> datetime:
    """Parse a Google dateTime and express it in the configured zone.

    Google returns fixed offsets ("-05:00"); keeping those would emit an ICS
    TZID like "UTC-05:00" that nothing (including Google) recognizes.
    """
    dt = datetime.fromisoformat(info["dateTime"])
    if dt.tzinfo is None:
        return dt.replace(tzinfo=default_tz)
    return dt.astimezone(default_tz)


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _http_status(exc: HttpError) -> int | None:
    return int(exc.resp.status) if exc.resp is not None else None


def _error_reasons(exc: HttpError) -> set[str]:
    try:
        payload = json.loads(exc.content)
    except (TypeError, ValueError):
        return set()
    errors = payload.get("error", {}).get("errors", []) if isinstance(payload, dict) else []
    return {e.get("reason", "") for e in errors if isinstance(e, dict)}


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, OSError):  # socket timeouts, connection resets
        return True
    if isinstance(exc, HttpError):
        status = _http_status(exc)
        if status in _RETRYABLE_STATUSES:
            return True
        return status == 403 and bool(_error_reasons(exc) & _RETRYABLE_403_REASONS)
    return False


def _execute_with_backoff(request, sleep=None):
    """Run a googleapiclient request, retrying transient failures with backoff.

    Raises CalendarApiError once retries are exhausted or the error is permanent.
    """
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return request.execute()
        except (HttpError, OSError) as exc:
            status = _http_status(exc) if isinstance(exc, HttpError) else None
            if not _is_retryable(exc) or attempt == _MAX_RETRIES:
                raise CalendarApiError(f"Calendar API error: {exc}", status=status) from exc
            delay = _BACKOFF_BASE_SECONDS * (2**attempt)
            logger.warning(
                "Calendar API error (%s), retrying in %.1fs (attempt %d/%d)",
                status or type(exc).__name__,
                delay,
                attempt + 1,
                _MAX_RETRIES,
            )
            (sleep or time.sleep)(delay)
    raise AssertionError("unreachable")


class CalendarClient:
    """Wraps a google-api-python-client Calendar `service` object.

    httplib2 (under googleapiclient) is not thread-safe, and this client is
    shared by the FUSE thread (writes) and the background refresh thread, so
    every API call is serialized behind one lock.
    """

    def __init__(self, service, calendar_id: str, default_tz: ZoneInfo) -> None:
        self._service = service
        self._calendar_id = calendar_id
        self._tz = default_tz
        self._lock = threading.Lock()

    @classmethod
    def from_credentials(
        cls, credentials, calendar_id: str, default_tz: ZoneInfo
    ) -> CalendarClient:
        from googleapiclient.discovery import build

        service = build(
            API_SERVICE_NAME, API_VERSION, credentials=credentials, cache_discovery=False
        )
        return cls(service, calendar_id, default_tz)

    def _execute(self, request):
        with self._lock:
            return _execute_with_backoff(request)

    def _paginate(self, params: dict):
        """Yield each page of an events.list call, following nextPageToken."""
        page_token: str | None = None
        while True:
            page_params = dict(params, pageToken=page_token) if page_token else params
            response = self._execute(self._service.events().list(**page_params))
            yield response
            page_token = response.get("nextPageToken")
            if not page_token:
                return

    def list_window(
        self, time_min: datetime, time_max: datetime
    ) -> tuple[list[EventRecord], str | None]:
        """Full window fetch: singleEvents=True, orderBy=startTime, paginated."""
        params = {
            "calendarId": self._calendar_id,
            "timeMin": _to_utc_iso(time_min),
            "timeMax": _to_utc_iso(time_max),
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": PAGE_SIZE,
        }
        records: list[EventRecord] = []
        sync_token: str | None = None
        for response in self._paginate(params):
            for raw in response.get("items", []):
                if raw.get("status") != "cancelled":
                    records.append(map_event(raw, self._tz))
            sync_token = response.get("nextSyncToken", sync_token)
        return records, sync_token

    def list_updates(self, sync_token: str) -> tuple[list[EventRecord], list[str], str | None]:
        """Incremental sync via a previously returned nextSyncToken.

        Returns (updated_or_new_records, deleted_event_ids, next_sync_token).
        Raises SyncTokenExpiredError if Google rejects the token (HTTP 410).
        """
        params = {
            "calendarId": self._calendar_id,
            "syncToken": sync_token,
            "singleEvents": True,
            "maxResults": PAGE_SIZE,
        }
        records: list[EventRecord] = []
        deleted: list[str] = []
        next_sync_token: str | None = None
        try:
            for response in self._paginate(params):
                for raw in response.get("items", []):
                    if raw.get("status") == "cancelled":
                        deleted.append(raw["id"])
                    else:
                        records.append(map_event(raw, self._tz))
                next_sync_token = response.get("nextSyncToken", next_sync_token)
        except CalendarApiError as exc:
            if exc.status == 410:
                raise SyncTokenExpiredError(str(exc), status=410) from exc
            raise
        return records, deleted, next_sync_token

    def get(self, event_id: str) -> EventRecord | None:
        try:
            raw = self._execute(
                self._service.events().get(calendarId=self._calendar_id, eventId=event_id)
            )
        except CalendarApiError as exc:
            if exc.not_found:
                return None
            raise
        if raw.get("status") == "cancelled":
            return None
        return map_event(raw, self._tz)

    def insert(self, body: dict) -> EventRecord:
        raw = self._execute(self._service.events().insert(calendarId=self._calendar_id, body=body))
        logger.info("inserted event %s", raw["id"])
        return map_event(raw, self._tz)

    def patch(self, event_id: str, body: dict) -> EventRecord:
        raw = self._execute(
            self._service.events().patch(calendarId=self._calendar_id, eventId=event_id, body=body)
        )
        logger.info("patched event %s", event_id)
        return map_event(raw, self._tz)

    def delete(self, event_id: str) -> None:
        self._execute(self._service.events().delete(calendarId=self._calendar_id, eventId=event_id))
        logger.info("deleted event %s", event_id)
