"""Thin wrapper around the Google Calendar API."""

from __future__ import annotations

import logging
import time
from datetime import UTC, date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError

from .cache import EventRecord

logger = logging.getLogger(__name__)

API_SERVICE_NAME = "calendar"
API_VERSION = "v3"
PAGE_SIZE = 250

# Retried status codes: 429 (rate limit) and 5xx (transient server errors).
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 5
_BACKOFF_BASE_SECONDS = 1.0


def _execute_with_backoff(request):
    """Run a googleapiclient request, retrying 429/5xx with exponential backoff."""
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return request.execute()
        except HttpError as exc:
            status = exc.resp.status if exc.resp is not None else None
            if status not in _RETRYABLE_STATUSES or attempt == _MAX_RETRIES:
                raise
            delay = _BACKOFF_BASE_SECONDS * (2**attempt)
            logger.warning(
                "Calendar API returned %s, retrying in %.1fs (attempt %d/%d)",
                status,
                delay,
                attempt + 1,
                _MAX_RETRIES,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")  # loop always returns or raises


class SyncTokenExpiredError(RuntimeError):
    """Raised when Google rejects a sync token (HTTP 410); caller should full-refetch."""


class CalendarClientProtocol(Protocol):
    """What cache refresh and fs.py need from a Calendar client, real or fake."""

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
        end = _parse_datetime(end_info, default_tz) if end_info else None

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
        rrule=";".join(raw["recurrence"]) if raw.get("recurrence") else None,
        recurring_event_id=raw.get("recurringEventId"),
        html_link=raw.get("htmlLink"),
        updated=updated,
    )


def _midnight(date_str: str, tz: ZoneInfo) -> datetime:
    return datetime.combine(date.fromisoformat(date_str), datetime.min.time(), tzinfo=tz)


def _parse_datetime(info: dict, default_tz: ZoneInfo) -> datetime:
    dt = datetime.fromisoformat(info["dateTime"])
    if dt.tzinfo is None:
        return dt.replace(tzinfo=default_tz)
    return dt


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


class CalendarClient:
    """Wraps a google-api-python-client Calendar service. Never called in tests."""

    def __init__(self, credentials: Credentials, calendar_id: str, default_tz: ZoneInfo) -> None:
        self._service: Resource = build(
            API_SERVICE_NAME, API_VERSION, credentials=credentials, cache_discovery=False
        )
        self._calendar_id = calendar_id
        self._tz = default_tz

    def list_window(
        self, time_min: datetime, time_max: datetime
    ) -> tuple[list[EventRecord], str | None]:
        """Full window fetch: singleEvents=True, orderBy=startTime, paginated."""
        records: list[EventRecord] = []
        page_token: str | None = None
        sync_token: str | None = None
        while True:
            params = {
                "calendarId": self._calendar_id,
                "timeMin": _to_utc_iso(time_min),
                "timeMax": _to_utc_iso(time_max),
                "singleEvents": True,
                "orderBy": "startTime",
                "maxResults": PAGE_SIZE,
            }
            if page_token:
                params["pageToken"] = page_token
            response = _execute_with_backoff(self._service.events().list(**params))
            for raw in response.get("items", []):
                if raw.get("status") != "cancelled":
                    records.append(map_event(raw, self._tz))
            page_token = response.get("nextPageToken")
            sync_token = response.get("nextSyncToken", sync_token)
            if not page_token:
                break
        return records, sync_token

    def list_updates(self, sync_token: str) -> tuple[list[EventRecord], list[str], str | None]:
        """Incremental sync via a previously returned nextSyncToken.

        Returns (updated_or_new_records, deleted_event_ids, next_sync_token).
        Raises SyncTokenExpiredError if Google rejects the token (HTTP 410).
        """
        records: list[EventRecord] = []
        deleted: list[str] = []
        page_token: str | None = None
        next_sync_token: str | None = None
        try:
            while True:
                params = {
                    "calendarId": self._calendar_id,
                    "syncToken": sync_token,
                    "singleEvents": True,
                    "maxResults": PAGE_SIZE,
                }
                if page_token:
                    params["pageToken"] = page_token
                response = _execute_with_backoff(self._service.events().list(**params))
                for raw in response.get("items", []):
                    if raw.get("status") == "cancelled":
                        deleted.append(raw["id"])
                    else:
                        records.append(map_event(raw, self._tz))
                page_token = response.get("nextPageToken")
                next_sync_token = response.get("nextSyncToken", next_sync_token)
                if not page_token:
                    break
        except HttpError as exc:
            if exc.resp is not None and exc.resp.status == 410:
                raise SyncTokenExpiredError(str(exc)) from exc
            raise
        return records, deleted, next_sync_token

    def get(self, event_id: str) -> EventRecord | None:
        try:
            raw = _execute_with_backoff(
                self._service.events().get(calendarId=self._calendar_id, eventId=event_id)
            )
        except HttpError as exc:
            if exc.resp is not None and exc.resp.status == 404:
                return None
            raise
        if raw.get("status") == "cancelled":
            return None
        return map_event(raw, self._tz)

    def insert(self, body: dict) -> EventRecord:
        raw = _execute_with_backoff(
            self._service.events().insert(calendarId=self._calendar_id, body=body)
        )
        logger.info("inserted event %s", raw["id"])
        return map_event(raw, self._tz)

    def patch(self, event_id: str, body: dict) -> EventRecord:
        raw = _execute_with_backoff(
            self._service.events().patch(calendarId=self._calendar_id, eventId=event_id, body=body)
        )
        logger.info("patched event %s", event_id)
        return map_event(raw, self._tz)

    def delete(self, event_id: str) -> None:
        _execute_with_backoff(
            self._service.events().delete(calendarId=self._calendar_id, eventId=event_id)
        )
        logger.info("deleted event %s", event_id)
