import errno
import os
from datetime import datetime
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


def build_fs(records=None, read_only=False):
    client = FakeCalendarClient(CHICAGO, records or [])
    cache = CalendarCache(
        client, CHICAGO, window_past_days=30, window_future_days=90, poll_seconds=60
    )
    cache.refresh_full()
    return GcalfuseFS(cache.index, client, read_only=read_only), client


async def lookup_path(fs, *path_parts):
    """Walk lookup() one segment at a time, the way the kernel resolves a path."""
    attr = None
    inode = pyfuse3.ROOT_INODE
    for part in path_parts:
        attr = await fs.lookup(inode, part.encode())
        inode = attr.st_ino
    return attr


def child_inode(fs, parent_inode, name):
    """Allocate/reuse the inode for a not-yet-existing child, e.g. a new day
    directory a reschedule is about to create. Mirrors what lookup() would
    hand back once the child is real, without requiring it to exist yet."""
    parent_path = fs._path_for_inode(parent_inode)
    return fs._inode_for_path(parent_path / name)


def day_inode(fs, year, month, day):
    """Allocate the inode for /year/month/day, whether or not it exists yet."""
    y = child_inode(fs, pyfuse3.ROOT_INODE, year)
    m = child_inode(fs, y, month)
    return child_inode(fs, m, day)


SIMPLE_ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:Dentist
DTSTART:20260924T150000
DTEND:20260924T153000
END:VEVENT
END:VCALENDAR
"""

SUMMARY_ONLY_ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:Quick Chat
END:VEVENT
END:VCALENDAR
"""

INVALID_ICS = b"not an ics file at all"


def test_create_then_multiple_writes_and_release_is_one_insert():
    fs, client = build_fs()

    async def scenario():
        target_day = day_inode(fs, "2026", "09", "24")
        file_info, _attr = await fs.create(
            target_day, b"1500-1530_dentist.ics", 0o644, os.O_CREAT | os.O_WRONLY
        )
        half = len(SIMPLE_ICS) // 2
        await fs.write(file_info.fh, 0, SIMPLE_ICS[:half])
        await fs.write(file_info.fh, half, SIMPLE_ICS[half:])
        await fs.release(file_info.fh)

    trio.run(scenario)
    assert len(client.insert_calls) == 1
    assert client.insert_calls[0]["summary"] == "Dentist"


def test_invalid_ics_on_patch_close_returns_eio_and_writes_nothing():
    original = make_event("e1", "Standup", day=23, hour=9)
    fs, client = build_fs([original])

    async def scenario():
        attr = await lookup_path(fs, "2026", "09", "23", "0900-0930_standup.ics")
        file_info = await fs.open(attr.st_ino, os.O_WRONLY | os.O_TRUNC)
        await fs.write(file_info.fh, 0, INVALID_ICS)
        with pytest.raises(pyfuse3.FUSEError) as exc_info:
            await fs.release(file_info.fh)
        assert exc_info.value.errno == errno.EIO

    trio.run(scenario)
    assert client.patch_calls == []
    assert client.insert_calls == []


def test_unlink_non_recurring_calls_delete_once():
    original = make_event("e1", "Standup", day=23, hour=9)
    fs, client = build_fs([original])

    async def scenario():
        day_attr = await lookup_path(fs, "2026", "09", "23")
        await fs.unlink(day_attr.st_ino, b"0900-0930_standup.ics")

    trio.run(scenario)
    assert client.delete_calls == ["e1"]


def test_unlink_recurring_instance_is_rejected_with_no_delete():
    recurring = make_event("e1", "Weekly Sync", day=23, hour=9, recurring_event_id="series1")
    fs, client = build_fs([recurring])

    async def scenario():
        day_attr = await lookup_path(fs, "2026", "09", "23")
        with pytest.raises(pyfuse3.FUSEError) as exc_info:
            await fs.unlink(day_attr.st_ino, b"0900-0930_weekly_sync.ics")
        assert exc_info.value.errno == errno.EPERM

    trio.run(scenario)
    assert client.delete_calls == []


def test_write_recurring_instance_rejected_at_open_zero_patches():
    recurring = make_event("e1", "Weekly Sync", day=23, hour=9, recurring_event_id="series1")
    fs, client = build_fs([recurring])

    async def scenario():
        attr = await lookup_path(fs, "2026", "09", "23", "0900-0930_weekly_sync.ics")
        with pytest.raises(pyfuse3.FUSEError) as exc_info:
            await fs.open(attr.st_ino, os.O_WRONLY)
        assert exc_info.value.errno == errno.EPERM

    trio.run(scenario)
    assert client.patch_calls == []


def test_rename_across_days_patches_new_date_same_time():
    original = make_event("e1", "Dentist", day=24, hour=15, minute=0)
    fs, client = build_fs([original])

    async def scenario():
        old_day_attr = await lookup_path(fs, "2026", "09", "24")
        month_attr = await lookup_path(fs, "2026", "09")
        new_day_inode = child_inode(fs, month_attr.st_ino, "25")

        await fs.rename(
            old_day_attr.st_ino,
            b"1500-1530_dentist.ics",
            new_day_inode,
            b"1500-1530_dentist.ics",
            0,
        )

    trio.run(scenario)
    assert len(client.patch_calls) == 1
    event_id, body = client.patch_calls[0]
    assert event_id == "e1"
    assert body["start"]["dateTime"].startswith("2026-09-25T15:00:00")
    assert body["end"]["dateTime"].startswith("2026-09-25T15:30:00")


def test_create_under_day_with_summary_only_inserts_with_default_duration():
    fs, client = build_fs()

    async def scenario():
        target_day = day_inode(fs, "2026", "09", "24")
        file_info, _attr = await fs.create(
            target_day, b"quick.ics", 0o644, os.O_CREAT | os.O_WRONLY
        )
        await fs.write(file_info.fh, 0, SUMMARY_ONLY_ICS)
        await fs.release(file_info.fh)

    trio.run(scenario)
    assert len(client.insert_calls) == 1
    body = client.insert_calls[0]
    assert body["summary"] == "Quick Chat"
    assert body["start"]["dateTime"].startswith("2026-09-24T09:00:00")
    assert body["end"]["dateTime"].startswith("2026-09-24T09:30:00")


def test_read_only_mount_rejects_all_mutations():
    original = make_event("e1", "Standup", day=23, hour=9)
    fs, client = build_fs([original], read_only=True)

    async def scenario():
        with pytest.raises(pyfuse3.FUSEError) as exc_info:
            await fs.create(pyfuse3.ROOT_INODE, b"new.ics", 0o644, os.O_CREAT | os.O_WRONLY)
        assert exc_info.value.errno == errno.EROFS

        day_attr = await lookup_path(fs, "2026", "09", "23")
        with pytest.raises(pyfuse3.FUSEError) as exc_info:
            await fs.unlink(day_attr.st_ino, b"0900-0930_standup.ics")
        assert exc_info.value.errno == errno.EROFS

        file_attr = await lookup_path(fs, "2026", "09", "23", "0900-0930_standup.ics")
        with pytest.raises(pyfuse3.FUSEError) as exc_info:
            await fs.open(file_attr.st_ino, os.O_WRONLY)
        assert exc_info.value.errno == errno.EROFS

    trio.run(scenario)
    assert client.insert_calls == []
    assert client.delete_calls == []
    assert client.patch_calls == []


def test_write_temp_then_rename_commits_once():
    fs, client = build_fs()

    async def scenario():
        target_day = day_inode(fs, "2026", "09", "26")
        file_info, _attr = await fs.create(
            target_day, b".1500-1530_dentist.ics.swp", 0o644, os.O_CREAT | os.O_WRONLY
        )
        await fs.write(file_info.fh, 0, SIMPLE_ICS.replace(b"20260924", b"20260926"))
        await fs.release(file_info.fh)  # still a swap file name: no commit yet

        await fs.rename(
            target_day, b".1500-1530_dentist.ics.swp", target_day, b"1500-1530_dentist.ics", 0
        )

    trio.run(scenario)
    assert len(client.insert_calls) == 1
    assert client.insert_calls[0]["summary"] == "Dentist"


def test_rename_to_junk_name_does_not_delete_google_event():
    original = make_event("e1", "Standup", day=23, hour=9)
    fs, client = build_fs([original])

    async def scenario():
        day_attr = await lookup_path(fs, "2026", "09", "23")
        await fs.rename(
            day_attr.st_ino, b"0900-0930_standup.ics", day_attr.st_ino, b"0900-0930_standup.ics~", 0
        )

    trio.run(scenario)
    assert client.delete_calls == []
    assert client.patch_calls == []


def test_closing_an_editor_junk_named_file_makes_zero_api_calls():
    fs, client = build_fs()

    async def scenario():
        target_day = day_inode(fs, "2026", "09", "24")
        for junk_name in (b".foo.ics.swp", b"foo.ics.tmp", b".#foo.ics", b"foo.ics~"):
            file_info, _attr = await fs.create(
                target_day, junk_name, 0o644, os.O_CREAT | os.O_WRONLY
            )
            await fs.write(file_info.fh, 0, SIMPLE_ICS)
            await fs.release(file_info.fh)

    trio.run(scenario)
    assert client.insert_calls == []
    assert client.patch_calls == []
    assert client.delete_calls == []
