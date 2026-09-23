"""Shared test helpers: event builders and a way to drive GcalfuseFS without a mount.

The async FUSE handlers are plain coroutines, so tests call them directly
under `trio.run`. The one exception is readdir, whose `readdir_reply` needs a
kernel-issued token; tests use `GcalfuseFS._children` for listings instead,
and tests/test_integration_mount.py covers the real readdir path.
"""

from __future__ import annotations

import errno
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pyfuse3
import pytest

from gcalfuse.cache import CalendarCache, EventRecord
from gcalfuse.fs import GcalfuseFS

from .fakes import FakeCalendarClient

CHICAGO = ZoneInfo("America/Chicago")


def make_event(
    event_id: str,
    summary: str,
    day: int,
    hour: int = 9,
    minute: int = 0,
    minutes: int = 30,
    month: int = 9,
    recurring_event_id: str | None = None,
    **fields,
) -> EventRecord:
    start = datetime(2026, month, day, hour, minute, tzinfo=CHICAGO)
    return EventRecord(
        event_id=event_id,
        summary=summary,
        start=start,
        end=start + timedelta(minutes=minutes),
        recurring_event_id=recurring_event_id,
        **fields,
    )


def make_all_day(
    event_id: str, summary: str, day: int, days: int = 1, month: int = 9
) -> EventRecord:
    start = datetime(2026, month, day, tzinfo=CHICAGO)
    return EventRecord(
        event_id=event_id,
        summary=summary,
        start=start,
        end=start + timedelta(days=days),
        all_day=True,
    )


def build_fs(records=None, read_only=False, client=None):
    """A GcalfuseFS over a FakeCalendarClient seeded with `records`."""
    client = client or FakeCalendarClient(CHICAGO, records or [])
    cache = CalendarCache(
        client, CHICAGO, window_past_days=3650, window_future_days=3650, poll_seconds=60
    )
    cache.refresh_full()
    return GcalfuseFS(cache.index, client, read_only=read_only), client


async def lookup_path(fs: GcalfuseFS, *parts: str):
    """Walk lookup() one segment at a time, the way the kernel resolves a path."""
    inode = pyfuse3.ROOT_INODE
    attr = None
    for part in parts:
        attr = await fs.lookup(inode, part.encode())
        inode = attr.st_ino
    return attr


def dir_inode(fs: GcalfuseFS, *parts: str) -> int:
    """Inode for a directory path, whether or not it currently exists.

    The kernel only hands a handler an inode it previously got from lookup();
    this skips that step so tests can target e.g. a day with no events.
    """
    path = fs._path_for_inode(pyfuse3.ROOT_INODE)
    for part in parts:
        path = path / part
    return fs._inode_for_path(path)


async def expect_errno(expected: int, coro) -> None:
    with pytest.raises(pyfuse3.FUSEError) as exc_info:
        await coro
    assert exc_info.value.errno == expected, (
        f"expected {errno.errorcode[expected]}, got {errno.errorcode.get(exc_info.value.errno)}"
    )


def setattr_fields(**updates: bool) -> SimpleNamespace:
    """Stand-in for pyfuse3.SetattrFields, whose attributes are read-only from Python."""
    names = ("atime", "ctime", "gid", "mode", "mtime", "size", "uid")
    return SimpleNamespace(**{f"update_{n}": updates.get(n, False) for n in names})


def ics(*lines: str) -> bytes:
    """Wrap VEVENT property lines in a minimal VCALENDAR."""
    body = "\r\n".join(
        ["BEGIN:VCALENDAR", "VERSION:2.0", "BEGIN:VEVENT", *lines, "END:VEVENT", "END:VCALENDAR"]
    )
    return (body + "\r\n").encode()
