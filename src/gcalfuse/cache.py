"""In-memory event cache and bidirectional path <-> event_id index."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo

from . import paths as pathsmod


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
    """Holds EventRecords and a rebuildable bidirectional path <-> event_id index."""

    def __init__(self, tz: ZoneInfo) -> None:
        self._tz = tz
        self._by_id: dict[str, EventRecord] = {}
        self._path_to_id: dict[PurePosixPath, str] = {}
        self._id_to_path: dict[str, PurePosixPath] = {}

    def add(self, record: EventRecord) -> None:
        self._by_id[record.event_id] = record
        self._rebuild_paths()

    def remove(self, event_id: str) -> None:
        self._by_id.pop(event_id, None)
        self._rebuild_paths()

    def get(self, event_id: str) -> EventRecord | None:
        return self._by_id.get(event_id)

    def get_by_path(self, path: PurePosixPath) -> EventRecord | None:
        event_id = self._path_to_id.get(path)
        return self._by_id.get(event_id) if event_id is not None else None

    def path_for_id(self, event_id: str) -> PurePosixPath | None:
        return self._id_to_path.get(event_id)

    def replace_all(self, records: Iterable[EventRecord]) -> None:
        """Replace the whole cache contents, e.g. after a full window refetch."""
        self._by_id = {record.event_id: record for record in records}
        self._rebuild_paths()

    def years(self) -> list[int]:
        return sorted({int(p.parts[1]) for p in self._path_to_id})

    def months(self, year: int) -> list[int]:
        return sorted(
            {int(p.parts[2]) for p in self._path_to_id if int(p.parts[1]) == year}
        )

    def days(self, year: int, month: int) -> list[int]:
        return sorted(
            {
                int(p.parts[3])
                for p in self._path_to_id
                if int(p.parts[1]) == year and int(p.parts[2]) == month
            }
        )

    def files(self, year: int, month: int, day: int) -> list[str]:
        return sorted(
            p.parts[4]
            for p in self._path_to_id
            if int(p.parts[1]) == year and int(p.parts[2]) == month and int(p.parts[3]) == day
        )

    def _rebuild_paths(self) -> None:
        groups: dict[PurePosixPath, list[EventRecord]] = {}
        for record in self._by_id.values():
            base_path = pathsmod.path_for(record, self._tz)
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
