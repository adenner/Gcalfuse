"""Path <-> event mapping for the virtual /YYYY/MM/DD/<file>.ics tree.

Pure functions only: no cache, no network, no FUSE.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date, datetime, tzinfo
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

# Editor junk filenames we should never treat as a real event commit.
_JUNK_PATTERNS = [
    re.compile(r"^\..*\.swp$"),  # vim swap
    re.compile(r"^\.#.*$"),  # emacs lock file
    re.compile(r"^.*~$"),  # backup files
    re.compile(r"^.*\.tmp$"),  # generic temp files
    re.compile(r"^\.goutputstream-.*$"),  # gedit/gio temp files
]

_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")
_SLUG_SQUEEZE_RE = re.compile(r"_+")
_SLUG_MAX_LEN = 60

_YEAR_RE = re.compile(r"^\d{4}$")
_MONTH_DAY_RE = re.compile(r"^\d{2}$")


class EventView(Protocol):
    """Structural type describing what paths.py needs from an event record."""

    event_id: str
    summary: str
    all_day: bool
    start: datetime
    end: datetime | None


class InvalidPathError(ValueError):
    """Raised when a virtual path does not match the /YYYY/MM/DD/<file>.ics layout."""


@dataclass(frozen=True)
class RootDir:
    pass


@dataclass(frozen=True)
class YearDir:
    year: int


@dataclass(frozen=True)
class MonthDir:
    year: int
    month: int


@dataclass(frozen=True)
class DayDir:
    year: int
    month: int
    day: int


@dataclass(frozen=True)
class EventFile:
    year: int
    month: int
    day: int
    filename: str


ParsedPath = RootDir | YearDir | MonthDir | DayDir | EventFile


def slugify(summary: str) -> str:
    """Lowercase, replace non-alnum runs with `_`, squeeze, cap at 60 chars."""
    if not summary:
        return "untitled"
    lowered = summary.lower()
    replaced = _SLUG_STRIP_RE.sub("_", lowered)
    squeezed = _SLUG_SQUEEZE_RE.sub("_", replaced).strip("_")
    if not squeezed:
        return "untitled"
    truncated = squeezed[:_SLUG_MAX_LEN].strip("_")
    return truncated or "untitled"


def filename_for(event_view: EventView) -> str:
    """Build the filename for an event, assuming start/end are already localized."""
    slug = slugify(event_view.summary)
    if event_view.all_day:
        return f"0000_{slug}.ics"

    start_hhmm = event_view.start.strftime("%H%M")
    if event_view.end is not None:
        end_hhmm = event_view.end.strftime("%H%M")
        return f"{start_hhmm}-{end_hhmm}_{slug}.ics"
    return f"{start_hhmm}_{slug}.ics"


def path_for(event_view: EventView, tz: tzinfo | ZoneInfo) -> PurePosixPath:
    """Compute the /YYYY/MM/DD/<file>.ics path for an event in the given timezone.

    All-day events are not timezone-converted: their date is used as given,
    since Google reports them as a plain calendar date with no time component.
    """
    if event_view.all_day:
        local_view = event_view
        local_start = event_view.start
    else:
        local_start = event_view.start.astimezone(tz)
        local_end = event_view.end.astimezone(tz) if event_view.end is not None else None
        local_view = replace(event_view, start=local_start, end=local_end)

    filename = filename_for(local_view)
    return PurePosixPath(
        f"/{local_start.year:04d}/{local_start.month:02d}/{local_start.day:02d}/{filename}"
    )


def with_collision_suffix(path: PurePosixPath, event_id: str) -> PurePosixPath:
    """Disambiguate a colliding path by appending __<first 8 chars of event id>."""
    stem = path.name[: -len(".ics")] if path.name.endswith(".ics") else path.name
    suffix = event_id[:8]
    return path.with_name(f"{stem}__{suffix}.ics")


def is_editor_junk(filename: str) -> bool:
    """True if filename looks like an editor's temp/swap/backup file, not a real commit."""
    return any(pattern.match(filename) for pattern in _JUNK_PATTERNS)


def parse_path(path: str | PurePosixPath) -> ParsedPath:
    """Parse a virtual path into Root/Year/Month/Day/EventFile, or raise InvalidPathError."""
    p = PurePosixPath(path) if not isinstance(path, PurePosixPath) else path
    if not str(p).startswith("/"):
        raise InvalidPathError(f"path must be absolute: {path!r}")

    parts = p.parts[1:]  # drop leading "/"

    if len(parts) == 0:
        return RootDir()

    year_str = parts[0]
    if not _YEAR_RE.match(year_str):
        raise InvalidPathError(f"invalid year segment: {year_str!r}")
    year = int(year_str)

    if len(parts) == 1:
        return YearDir(year=year)

    month_str = parts[1]
    if not _MONTH_DAY_RE.match(month_str) or not (1 <= int(month_str) <= 12):
        raise InvalidPathError(f"invalid month segment: {month_str!r}")
    month = int(month_str)

    if len(parts) == 2:
        return MonthDir(year=year, month=month)

    day_str = parts[2]
    if not _MONTH_DAY_RE.match(day_str):
        raise InvalidPathError(f"invalid day segment: {day_str!r}")
    day = int(day_str)
    try:
        date(year, month, day)
    except ValueError as exc:
        raise InvalidPathError(f"invalid calendar date: {year}-{month}-{day}") from exc

    if len(parts) == 3:
        return DayDir(year=year, month=month, day=day)

    if len(parts) == 4:
        filename = parts[3]
        if not filename.endswith(".ics"):
            raise InvalidPathError(f"expected a .ics file: {filename!r}")
        return EventFile(year=year, month=month, day=day, filename=filename)

    raise InvalidPathError(f"path too deep: {path!r}")
