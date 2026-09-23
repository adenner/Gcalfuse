"""Read path: listing, lookup, getattr, open/read, and read-only enforcement."""

import errno
import os
import stat
from pathlib import PurePosixPath

import pyfuse3
import trio

from .helpers import (
    build_fs,
    dir_inode,
    expect_errno,
    lookup_path,
    make_all_day,
    make_event,
    setattr_fields,
)

THREE_EVENTS = [
    make_event("e1", "Standup", day=23, hour=9),
    make_event("e2", "Dentist", day=24, hour=15),
    make_event("e3", "Planning", day=24, hour=10),
]


def names(fs, path):
    return [name for name, _, _ in fs._children(PurePosixPath(path))]


# -- listing -----------------------------------------------------------------


def test_readdir_root_lists_years():
    fs, _ = build_fs(THREE_EVENTS)
    assert names(fs, "/") == ["2026"]


def test_readdir_year_and_month_list_only_populated_entries():
    fs, _ = build_fs(THREE_EVENTS + [make_event("e4", "Retro", day=2, month=11)])
    assert names(fs, "/2026") == ["09", "11"]
    assert names(fs, "/2026/09") == ["23", "24"]


def test_readdir_day_lists_expected_filenames_sorted():
    fs, _ = build_fs(THREE_EVENTS)
    assert names(fs, "/2026/09/24") == ["1000-1030_planning.ics", "1500-1530_dentist.ics"]


def test_all_day_event_listed_with_0000_prefix():
    fs, _ = build_fs([make_all_day("pto", "PTO", day=24)])
    assert names(fs, "/2026/09/24") == ["0000_pto.ics"]


def test_multi_day_timed_event_listed_only_on_start_date():
    fs, _ = build_fs([make_event("trip", "Offsite", day=24, hour=20, minutes=60 * 24)])
    assert names(fs, "/2026/09") == ["24"]


def test_children_of_file_path_is_enotdir():
    fs, _ = build_fs(THREE_EVENTS)

    async def scenario():
        attr = await lookup_path(fs, "2026", "09", "23", "0900-0930_standup.ics")
        await expect_errno(errno.ENOTDIR, fs.opendir(attr.st_ino))

    trio.run(scenario)


# -- lookup / getattr --------------------------------------------------------


def test_lookup_missing_entries_raise_enoent():
    fs, _ = build_fs(THREE_EVENTS)

    async def scenario():
        await expect_errno(errno.ENOENT, fs.lookup(pyfuse3.ROOT_INODE, b"2099"))
        await expect_errno(errno.ENOENT, fs.lookup(pyfuse3.ROOT_INODE, b".git"))
        month = await lookup_path(fs, "2026", "09")
        await expect_errno(errno.ENOENT, fs.lookup(month.st_ino, b"25"))  # no events that day

    trio.run(scenario)


def test_failed_lookup_does_not_allocate_an_inode():
    fs, _ = build_fs(THREE_EVENTS)
    before = len(fs._inode_to_path)

    async def scenario():
        for name in (b".git", b"HEAD", b"desktop.ini", b"2099"):
            await expect_errno(errno.ENOENT, fs.lookup(pyfuse3.ROOT_INODE, name))

    trio.run(scenario)
    assert len(fs._inode_to_path) == before


def test_lookup_returns_stable_inodes():
    fs, _ = build_fs(THREE_EVENTS)

    async def scenario():
        a = await lookup_path(fs, "2026", "09", "23", "0900-0930_standup.ics")
        b = await lookup_path(fs, "2026", "09", "23", "0900-0930_standup.ics")
        return a.st_ino, b.st_ino

    first, second = trio.run(scenario)
    assert first == second


def test_getattr_directory_and_file_modes():
    fs, _ = build_fs(THREE_EVENTS)

    async def scenario():
        root = await fs.getattr(pyfuse3.ROOT_INODE)
        day = await lookup_path(fs, "2026", "09", "23")
        f = await lookup_path(fs, "2026", "09", "23", "0900-0930_standup.ics")
        return root, day, f

    root, day, f = trio.run(scenario)
    assert stat.S_ISDIR(root.st_mode) and stat.S_ISDIR(day.st_mode)
    assert stat.S_ISREG(f.st_mode)
    assert f.st_mode & 0o777 == 0o644


def test_getattr_size_matches_read_length():
    fs, _ = build_fs(THREE_EVENTS)

    async def scenario():
        attr = await lookup_path(fs, "2026", "09", "24", "1500-1530_dentist.ics")
        info = await fs.open(attr.st_ino, os.O_RDONLY)
        data = await fs.read(info.fh, 0, 1 << 20)
        return attr.st_size, len(data)

    size, length = trio.run(scenario)
    assert size == length > 0


def test_file_mtime_is_event_updated_time():
    from datetime import UTC, datetime

    updated = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    fs, _ = build_fs([make_event("e1", "Standup", day=23, updated=updated)])

    async def scenario():
        return await lookup_path(fs, "2026", "09", "23", "0900-0930_standup.ics")

    attr = trio.run(scenario)
    assert attr.st_mtime_ns == int(updated.timestamp() * 1e9)


# -- open / read ---------------------------------------------------------------


def test_read_file_returns_valid_ics():
    fs, _ = build_fs(THREE_EVENTS)

    async def scenario():
        attr = await lookup_path(fs, "2026", "09", "23", "0900-0930_standup.ics")
        info = await fs.open(attr.st_ino, os.O_RDONLY)
        return await fs.read(info.fh, 0, 65536)

    data = trio.run(scenario)
    assert data.startswith(b"BEGIN:VCALENDAR")
    assert b"SUMMARY:Standup" in data
    assert b"DTSTART;TZID=America/Chicago:20260923T090000" in data


def test_read_honours_offset_and_size():
    fs, _ = build_fs(THREE_EVENTS)

    async def scenario():
        attr = await lookup_path(fs, "2026", "09", "23", "0900-0930_standup.ics")
        info = await fs.open(attr.st_ino, os.O_RDONLY)
        whole = await fs.read(info.fh, 0, 65536)
        middle = await fs.read(info.fh, 5, 10)
        past_end = await fs.read(info.fh, len(whole) + 100, 10)
        return whole, middle, past_end

    whole, middle, past_end = trio.run(scenario)
    assert middle == whole[5:15]
    assert past_end == b""


def test_open_disables_kernel_page_cache():
    """Content changes under us (refresh, canonical re-render after a save)."""
    fs, _ = build_fs(THREE_EVENTS)

    async def scenario():
        attr = await lookup_path(fs, "2026", "09", "23", "0900-0930_standup.ics")
        return await fs.open(attr.st_ino, os.O_RDONLY)

    assert trio.run(scenario).keep_cache is False


def test_open_directory_is_eisdir():
    fs, _ = build_fs(THREE_EVENTS)

    async def scenario():
        day = await lookup_path(fs, "2026", "09", "23")
        await expect_errno(errno.EISDIR, fs.open(day.st_ino, os.O_RDONLY))

    trio.run(scenario)


def test_recurring_instance_is_still_readable():
    fs, _ = build_fs([make_event("e4", "Weekly Sync", day=25, hour=11, recurring_event_id="s1")])
    assert names(fs, "/2026/09/25") == ["1100-1130_weekly_sync.ics"]

    async def scenario():
        attr = await lookup_path(fs, "2026", "09", "25", "1100-1130_weekly_sync.ics")
        info = await fs.open(attr.st_ino, os.O_RDONLY)
        return await fs.read(info.fh, 0, 65536)

    assert b"SUMMARY:Weekly Sync" in trio.run(scenario)


def test_statfs_succeeds():
    fs, _ = build_fs(THREE_EVENTS)
    stats = trio.run(fs.statfs)
    assert stats.f_namemax == 255


# -- read-only mount ---------------------------------------------------------


def test_read_only_mount_rejects_every_mutation_with_erofs():
    fs, client = build_fs(THREE_EVENTS, read_only=True)

    async def scenario():
        day = await lookup_path(fs, "2026", "09", "23")
        f = await lookup_path(fs, "2026", "09", "23", "0900-0930_standup.ics")
        flags = os.O_CREAT | os.O_WRONLY
        await expect_errno(errno.EROFS, fs.create(day.st_ino, b"new.ics", 0o644, flags))
        await expect_errno(errno.EROFS, fs.open(f.st_ino, os.O_WRONLY))
        await expect_errno(errno.EROFS, fs.open(f.st_ino, os.O_RDWR))
        await expect_errno(errno.EROFS, fs.write(f.st_ino, 0, b"x"))
        await expect_errno(errno.EROFS, fs.unlink(day.st_ino, b"0900-0930_standup.ics"))
        other_day = dir_inode(fs, "2026", "09", "24")
        await expect_errno(
            errno.EROFS,
            fs.rename(day.st_ino, b"0900-0930_standup.ics", other_day, b"0900-0930_standup.ics", 0),
        )
        fields = setattr_fields(size=True)
        await expect_errno(errno.EROFS, fs.setattr(f.st_ino, f, fields, None))

    trio.run(scenario)
    assert client.insert_calls == client.patch_calls == client.delete_calls == []


def test_chmod_is_a_successful_no_op_even_read_only():
    fs, _ = build_fs(THREE_EVENTS, read_only=True)

    async def scenario():
        f = await lookup_path(fs, "2026", "09", "23", "0900-0930_standup.ics")
        fields = setattr_fields(mode=True)
        f.st_mode = stat.S_IFREG | 0o600
        return await fs.setattr(f.st_ino, f, fields, None)

    assert trio.run(scenario).st_mode & 0o777 == 0o644


def test_mkdir_and_rmdir_are_eperm():
    fs, _ = build_fs(THREE_EVENTS)

    async def scenario():
        month = await lookup_path(fs, "2026", "09")
        await expect_errno(errno.EPERM, fs.mkdir(month.st_ino, b"30", 0o755))
        await expect_errno(errno.EPERM, fs.rmdir(month.st_ino, b"23"))

    trio.run(scenario)
