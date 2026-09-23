"""In-memory event cache, path index, and Google Calendar refresh scheduling."""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from . import paths as pathsmod

if TYPE_CHECKING:
    from .api import CalendarClientProtocol

logger = logging.getLogger(__name__)


@dataclass
class EventRecord:
    event_id: str
    summary: str
    start: datetime
    all_day: bool = False
    description: str = ""
    location: str = ""
    end: datetime | None = None
    status: str = "confirmed"
    organizer: str | None = None
    attendees: list[str] = field(default_factory=list)
    rrule: str | None = None
    recurring_event_id: str | None = None
    html_link: str | None = None
    updated: datetime | None = None

    @property
    def is_recurring_instance(self) -> bool:
        return self.recurring_event_id is not None


class EventIndex:
    """Holds EventRecords and a rebuildable bidirectional path <-> event_id index.

    Thread-safe: readdir/getattr run on the FUSE thread while a background
    thread refreshes the cache from Google, so every access takes a lock.
    """

    def __init__(self, tz: ZoneInfo) -> None:
        self.tz = tz
        self._lock = threading.RLock()
        self._by_id: dict[str, EventRecord] = {}
        self._path_to_id: dict[PurePosixPath, str] = {}
        self._id_to_path: dict[str, PurePosixPath] = {}

    def add(self, record: EventRecord) -> None:
        with self._lock:
            self._by_id[record.event_id] = record
            self._rebuild_paths()

    def remove(self, event_id: str) -> None:
        with self._lock:
            self._by_id.pop(event_id, None)
            self._rebuild_paths()

    def get(self, event_id: str) -> EventRecord | None:
        with self._lock:
            return self._by_id.get(event_id)

    def get_by_path(self, path: PurePosixPath) -> EventRecord | None:
        with self._lock:
            event_id = self._path_to_id.get(path)
            return self._by_id.get(event_id) if event_id is not None else None

    def path_for_id(self, event_id: str) -> PurePosixPath | None:
        with self._lock:
            return self._id_to_path.get(event_id)

    def replace_all(self, records: Iterable[EventRecord]) -> None:
        """Replace the whole cache contents, e.g. after a full window refetch."""
        with self._lock:
            self._by_id = {record.event_id: record for record in records}
            self._rebuild_paths()

    def years(self) -> list[int]:
        with self._lock:
            return sorted({int(p.parts[1]) for p in self._path_to_id})

    def months(self, year: int) -> list[int]:
        with self._lock:
            return sorted(
                {int(p.parts[2]) for p in self._path_to_id if int(p.parts[1]) == year}
            )

    def days(self, year: int, month: int) -> list[int]:
        with self._lock:
            return sorted(
                {
                    int(p.parts[3])
                    for p in self._path_to_id
                    if int(p.parts[1]) == year and int(p.parts[2]) == month
                }
            )

    def files(self, year: int, month: int, day: int) -> list[str]:
        with self._lock:
            return sorted(
                p.parts[4]
                for p in self._path_to_id
                if int(p.parts[1]) == year
                and int(p.parts[2]) == month
                and int(p.parts[3]) == day
            )

    def _rebuild_paths(self) -> None:
        """Caller must hold self._lock."""
        groups: dict[PurePosixPath, list[EventRecord]] = {}
        for record in self._by_id.values():
            base_path = pathsmod.path_for(record, self.tz)
            groups.setdefault(base_path, []).append(record)

        path_to_id: dict[PurePosixPath, str] = {}
        for base_path, records in groups.items():
            if len(records) == 1:
                path_to_id[base_path] = records[0].event_id
            else:
                for record in records:
                    suffixed = pathsmod.with_collision_suffix(base_path, record.event_id)
                    path_to_id[suffixed] = record.event_id

        self._path_to_id = path_to_id
        self._id_to_path = {event_id: path for path, event_id in path_to_id.items()}


class CalendarCache:
    """Owns an EventIndex and keeps it in sync with Google on a poll interval."""

    def __init__(
        self,
        client: CalendarClientProtocol,
        tz: ZoneInfo,
        window_past_days: int,
        window_future_days: int,
        poll_seconds: int,
    ) -> None:
        self._client = client
        self._tz = tz
        self._window_past_days = window_past_days
        self._window_future_days = window_future_days
        self._poll_seconds = poll_seconds
        self.index = EventIndex(tz)
        self._sync_token: str | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _window(self) -> tuple[datetime, datetime]:
        now = datetime.now(self._tz)
        return (
            now - timedelta(days=self._window_past_days),
            now + timedelta(days=self._window_future_days),
        )

    def refresh_full(self) -> None:
        """Fetch the whole configured window and replace the cache contents."""
        time_min, time_max = self._window()
        records, sync_token = self._client.list_window(time_min, time_max)
        self.index.replace_all(records)
        self._sync_token = sync_token
        logger.info("full refresh: %d events in window", len(records))

    def refresh_incremental(self) -> None:
        """Apply a syncToken-based delta if available, else fall back to a full refetch."""
        list_updates = getattr(self._client, "list_updates", None)
        if self._sync_token is None or list_updates is None:
            self.refresh_full()
            return

        from .api import SyncTokenExpiredError

        try:
            records, deleted, next_token = list_updates(self._sync_token)
        except SyncTokenExpiredError:
            logger.warning("sync token expired, doing a full window refetch")
            self.refresh_full()
            return

        time_min, time_max = self._window()
        for event_id in deleted:
            self.index.remove(event_id)
        for record in records:
            record_end = record.end or record.start
            if record.start <= time_max and record_end >= time_min:
                self.index.add(record)
            else:
                self.index.remove(record.event_id)

        self._sync_token = next_token or self._sync_token
        logger.info(
            "incremental refresh: %d updated, %d deleted", len(records), len(deleted)
        )

    def start_background_refresh(self) -> None:
        def _loop() -> None:
            while not self._stop_event.wait(self._poll_seconds):
                try:
                    self.refresh_incremental()
                except Exception:
                    logger.exception("background refresh failed; keeping stale cache")

        self._thread = threading.Thread(target=_loop, daemon=True, name="gcalfuse-refresh")
        self._thread.start()

    def stop_background_refresh(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
