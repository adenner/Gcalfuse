import errno
import os
from datetime import datetime
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo

import pyfuse3
import pytest
import trio

from gcalfuse.cache import CalendarCache, EventRecord
from gcalfuse.fs import GcalfuseFS

from .fakes import FakeCalendarClient

CHICAGO = ZoneInfo("America/Chicago")


def make_event(event_id, summary, day, hour=9, minute=0, recurring_event_id=None):
    start = datetime(2026, 9, day, hour, minute, tzinfo=CHICAGO)
    end = datetime(2026, 9, day, hour, minute + 30, tzinfo=CHICAGO)
    return EventRecord(
        event_id=event_id,
        summary=summary,
        start=start,
        end=end,
        recurring_event_id=recurring_event_id,
    )


def build_fs(records, read_only=True):
    client = FakeCalendarClient(CHICAGO, records)
    cache = CalendarCache(
        client, CHICAGO, window_past_days=30, window_future_days=90, poll_seconds=60
    )
    cache.refresh_full()
    return GcalfuseFS(cache.index, client, read_only=read_only), client


THREE_EVENTS = [
    make_event("e1", "Standup", day=23, hour=9),
    make_event("e2", "Dentist", day=24, hour=15),
    make_event("e3", "Planning", day=24, hour=10),
]


def test_readdir_root_lists_years():
    fs, _ = build_fs(THREE_EVENTS)
    children = fs._children(PurePosixPath("/"))
    assert [name for name, _, _ in children] == ["2026"]


def test_readdir_day_lists_expected_filenames():
    fs, _ = build_fs(THREE_EVENTS)
    children = fs._children(PurePosixPath("/2026/09/24"))
    names = sorted(name for name, _, _ in children)
    assert names == ["1000-1030_planning.ics", "1500-1530_dentist.ics"]


def test_read_file_returns_valid_ics():
    fs, _ = build_fs(THREE_EVENTS)

    async def scenario():
        attr = await fs.lookup(pyfuse3.ROOT_INODE, b"2026")
        attr = await fs.lookup(attr.st_ino, b"09")
        attr = await fs.lookup(attr.st_ino, b"23")
        attr = await fs.lookup(attr.st_ino, b"0900-0930_standup.ics")
        file_info = await fs.open(attr.st_ino, os.O_RDONLY)
        data = await fs.read(file_info.fh, 0, 65536)
        return data

    data = trio.run(scenario)
    assert data.startswith(b"BEGIN:VCALENDAR")
    assert b"SUMMARY:Standup" in data


def test_lookup_missing_file_raises_enoent():
    fs, _ = build_fs(THREE_EVENTS)

    async def scenario():
        with pytest.raises(pyfuse3.FUSEError) as exc_info:
            await fs.lookup(pyfuse3.ROOT_INODE, b"2099")
        assert exc_info.value.errno == errno.ENOENT

    trio.run(scenario)


def test_write_and_create_return_error():
    fs, _ = build_fs(THREE_EVENTS)

    async def scenario():
        with pytest.raises(pyfuse3.FUSEError) as exc_info:
            await fs.create(pyfuse3.ROOT_INODE, b"new.ics", 0o644, os.O_CREAT | os.O_WRONLY)
        assert exc_info.value.errno == errno.EROFS

        with pytest.raises(pyfuse3.FUSEError) as exc_info:
            await fs.write(1, 0, b"data")
        assert exc_info.value.errno == errno.EROFS

        attr = await fs.lookup(pyfuse3.ROOT_INODE, b"2026")
        attr = await fs.lookup(attr.st_ino, b"09")
        attr = await fs.lookup(attr.st_ino, b"23")
        attr = await fs.lookup(attr.st_ino, b"0900-0930_standup.ics")
        with pytest.raises(pyfuse3.FUSEError) as exc_info:
            await fs.open(attr.st_ino, os.O_WRONLY)
        assert exc_info.value.errno == errno.EROFS

        with pytest.raises(pyfuse3.FUSEError) as exc_info:
            await fs.unlink(pyfuse3.ROOT_INODE, b"whatever.ics")
        assert exc_info.value.errno == errno.EROFS

        with pytest.raises(pyfuse3.FUSEError) as exc_info:
            await fs.rename(
                pyfuse3.ROOT_INODE, b"a.ics", pyfuse3.ROOT_INODE, b"b.ics", 0
            )
        assert exc_info.value.errno == errno.EROFS

    trio.run(scenario)


def test_recurring_instance_is_still_readable():
    records = THREE_EVENTS + [
        make_event("e4", "Weekly Sync", day=25, hour=11, recurring_event_id="series1")
    ]
    fs, _ = build_fs(records)

    children = fs._children(PurePosixPath("/2026/09/25"))
    assert [name for name, _, _ in children] == ["1100-1130_weekly_sync.ics"]

    async def scenario():
        attr = await fs.lookup(pyfuse3.ROOT_INODE, b"2026")
        attr = await fs.lookup(attr.st_ino, b"09")
        attr = await fs.lookup(attr.st_ino, b"25")
        attr = await fs.lookup(attr.st_ino, b"1100-1130_weekly_sync.ics")
        file_info = await fs.open(attr.st_ino, os.O_RDONLY)
        return await fs.read(file_info.fh, 0, 65536)

    data = trio.run(scenario)
    assert data.startswith(b"BEGIN:VCALENDAR")
    assert b"SUMMARY:Weekly Sync" in data
