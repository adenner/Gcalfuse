"""Write path: create, edit-on-close, unlink, rename, editor save patterns, API failures."""

import errno
import logging
import os

import pyfuse3
import trio

from gcalfuse.api import CalendarApiError

from .helpers import (
    CHICAGO,
    build_fs,
    dir_inode,
    expect_errno,
    ics,
    lookup_path,
    make_all_day,
    make_event,
    setattr_fields,
)

CREATE = os.O_CREAT | os.O_WRONLY | os.O_TRUNC

DENTIST_ICS = ics("SUMMARY:Dentist", "DTSTART:20260924T150000", "DTEND:20260924T153000")


async def close(fs, fh) -> None:
    """What the kernel sends on close(2): FLUSH (errors reach the app), then RELEASE."""
    await fs.flush(fh)
    await fs.release(fh)


async def write_file(fs, parent_inode, name: bytes, data: bytes) -> None:
    """create + write + close, the way `cat > file` does it."""
    info, _ = await fs.create(parent_inode, name, 0o644, CREATE)
    if data:
        await fs.write(info.fh, 0, data)
    await close(fs, info.fh)


async def overwrite(fs, *path: str, data: bytes) -> None:
    """open(O_TRUNC) + write + close on an existing file."""
    attr = await lookup_path(fs, *path)
    info = await fs.open(attr.st_ino, os.O_WRONLY | os.O_TRUNC)
    await fs.write(info.fh, 0, data)
    await close(fs, info.fh)


async def read_file(fs, *path: str) -> bytes:
    attr = await lookup_path(fs, *path)
    info = await fs.open(attr.st_ino, os.O_RDONLY)
    return await fs.read(info.fh, 0, 1 << 20)


# -- create ----------------------------------------------------------------


def test_multiple_writes_then_release_is_exactly_one_insert():
    fs, client = build_fs()

    async def scenario():
        day = dir_inode(fs, "2026", "09", "24")
        info, _ = await fs.create(day, b"1500-1530_dentist.ics", 0o644, CREATE)
        for i in range(0, len(DENTIST_ICS), 7):
            await fs.write(info.fh, i, DENTIST_ICS[i : i + 7])
        await fs.release(info.fh)

    trio.run(scenario)
    assert len(client.insert_calls) == 1
    assert client.insert_calls[0]["summary"] == "Dentist"


def test_created_event_appears_in_listing_under_canonical_name():
    fs, _ = build_fs()

    async def scenario():
        await write_file(fs, dir_inode(fs, "2026", "09", "24"), b"whatever.ics", DENTIST_ICS)
        return await read_file(fs, "2026", "09", "24", "1500-1530_dentist.ics")

    assert b"SUMMARY:Dentist" in trio.run(scenario)


def test_floating_dtstart_is_sent_in_configured_timezone():
    """Google rejects a dateTime without an offset or timeZone."""
    fs, client = build_fs()
    trio.run(write_file, fs, dir_inode(fs, "2026", "09", "24"), b"x.ics", DENTIST_ICS)
    start = client.insert_calls[0]["start"]
    assert start == {"dateTime": "2026-09-24T15:00:00-05:00", "timeZone": "America/Chicago"}


def test_create_with_summary_only_uses_default_time_and_duration():
    fs, client = build_fs()
    trio.run(
        write_file, fs, dir_inode(fs, "2026", "09", "24"), b"quick.ics", ics("SUMMARY:Quick Chat")
    )
    body = client.insert_calls[0]
    assert body["summary"] == "Quick Chat"
    assert body["start"]["dateTime"] == "2026-09-24T09:00:00-05:00"
    assert body["end"]["dateTime"] == "2026-09-24T09:30:00-05:00"


def test_touch_of_new_file_creates_default_event_titled_from_filename():
    fs, client = build_fs()
    trio.run(write_file, fs, dir_inode(fs, "2026", "09", "24"), b"team_lunch.ics", b"")
    assert len(client.insert_calls) == 1
    assert client.insert_calls[0]["summary"] == "team lunch"


def test_create_with_dtstart_but_no_dtend_gets_default_duration():
    fs, client = build_fs()
    data = ics("SUMMARY:Call", "DTSTART:20260924T150000")
    trio.run(write_file, fs, dir_inode(fs, "2026", "09", "24"), b"call.ics", data)
    assert client.insert_calls[0]["end"]["dateTime"] == "2026-09-24T15:30:00-05:00"


def test_create_with_duration_instead_of_dtend():
    fs, client = build_fs()
    data = ics("SUMMARY:Call", "DTSTART:20260924T150000", "DURATION:PT1H15M")
    trio.run(write_file, fs, dir_inode(fs, "2026", "09", "24"), b"call.ics", data)
    assert client.insert_calls[0]["end"]["dateTime"] == "2026-09-24T16:15:00-05:00"


def test_create_all_day_event_from_date_value():
    fs, client = build_fs()
    data = ics("SUMMARY:PTO", "DTSTART;VALUE=DATE:20260924")
    trio.run(write_file, fs, dir_inode(fs, "2026", "09", "24"), b"pto.ics", data)
    body = client.insert_calls[0]
    assert body["start"] == {"date": "2026-09-24"}
    assert body["end"] == {"date": "2026-09-25"}
    assert (
        fs._children(fs._path_for_inode(dir_inode(fs, "2026", "09", "24")))[0][0] == "0000_pto.ics"
    )


def test_create_with_rrule_inserts_non_recurring_event_and_warns(caplog):
    fs, client = build_fs()
    data = ics("SUMMARY:Gym", "DTSTART:20260924T070000", "RRULE:FREQ=DAILY")
    with caplog.at_level(logging.WARNING):
        trio.run(write_file, fs, dir_inode(fs, "2026", "09", "24"), b"gym.ics", data)
    assert "recurrence" not in client.insert_calls[0]
    assert "RRULE ignored" in caplog.text


def test_dtstart_on_a_different_day_than_folder_is_einval_with_no_api_call():
    fs, client = build_fs()

    async def scenario():
        day = dir_inode(fs, "2026", "09", "25")  # DENTIST_ICS is on the 24th
        await expect_errno(errno.EINVAL, write_file(fs, day, b"dentist.ics", DENTIST_ICS))

    trio.run(scenario)
    assert client.insert_calls == []


def test_dtstart_in_other_zone_is_checked_against_local_folder_date():
    """01:00 in Tokyo on the 25th is still the 24th in Chicago."""
    fs, client = build_fs()
    data = ics(
        "SUMMARY:Sync",
        "DTSTART;TZID=Asia/Tokyo:20260925T010000",
        "DTEND;TZID=Asia/Tokyo:20260925T013000",
    )
    trio.run(write_file, fs, dir_inode(fs, "2026", "09", "24"), b"sync.ics", data)
    assert client.insert_calls[0]["start"]["timeZone"] == "Asia/Tokyo"


def test_invalid_ics_on_new_file_is_eio_and_file_vanishes():
    fs, client = build_fs()

    async def scenario():
        day = dir_inode(fs, "2026", "09", "24")
        await expect_errno(errno.EIO, write_file(fs, day, b"bad.ics", b"not an ics file"))
        await expect_errno(errno.ENOENT, fs.lookup(day, b"bad.ics"))

    trio.run(scenario)
    assert client.insert_calls == []


def test_two_vevents_rejected():
    fs, client = build_fs()
    two = DENTIST_ICS.replace(
        b"END:VCALENDAR", b"BEGIN:VEVENT\r\nSUMMARY:x\r\nEND:VEVENT\r\nEND:VCALENDAR"
    )

    async def scenario():
        await expect_errno(
            errno.EIO, write_file(fs, dir_inode(fs, "2026", "09", "24"), b"x.ics", two)
        )

    trio.run(scenario)
    assert client.insert_calls == []


def test_create_outside_a_day_folder_is_eperm():
    """Otherwise `cat > ~/Cal/note.ics` would 'succeed' and silently vanish."""
    fs, client = build_fs([make_event("e1", "Standup", day=23)])

    async def scenario():
        for parts in ((), ("2026",), ("2026", "09")):
            await expect_errno(
                errno.EPERM, fs.create(dir_inode(fs, *parts), b"x.ics", 0o644, CREATE)
            )

    trio.run(scenario)
    assert client.insert_calls == []


def test_sed_style_arbitrary_temp_name_then_rename_over_patches():
    """`sed -i` writes ./sedXXXXXX then renames it over the original."""
    fs, client = build_fs([make_event("e1", "Standup", day=23)])

    async def scenario():
        original = await read_file(fs, "2026", "09", "23", "0900-0930_standup.ics")
        day = (await lookup_path(fs, "2026", "09", "23")).st_ino
        await write_file(fs, day, b"sedAbC123", original.replace(b"Standup", b"Standup!"))
        assert client.patch_calls == []
        await fs.rename(day, b"sedAbC123", day, b"0900-0930_standup.ics", 0)

    trio.run(scenario)
    assert [(eid, b["summary"]) for eid, b in client.patch_calls] == [("e1", "Standup!")]
    assert client.insert_calls == []


def test_non_event_names_are_scratch_and_reported_if_never_renamed():
    fs, client = build_fs()

    async def scenario():
        day = dir_inode(fs, "2026", "09", "24")
        await write_file(fs, day, b"notes.txt", b"remember milk")
        await write_file(fs, day, b"4913", b"")  # vim's writability probe
        await fs.unlink(day, b"4913")
        await write_file(fs, day, b".x.ics.swp", b"swap")

    trio.run(scenario)
    assert client.insert_calls == []
    assert [p.name for p in fs.unsaved_scratch_files()] == ["notes.txt"]


def test_pending_file_is_visible_to_lookup_and_read_before_commit():
    fs, _ = build_fs()

    async def scenario():
        day = dir_inode(fs, "2026", "09", "24")
        info, _ = await fs.create(day, b"draft.ics", 0o644, CREATE)
        await fs.write(info.fh, 0, b"BEGIN:")
        attr = await fs.lookup(day, b"draft.ics")
        data = await fs.read(info.fh, 0, 100)
        return attr.st_size, data

    size, data = trio.run(scenario)
    assert (size, data) == (6, b"BEGIN:")


def test_write_past_end_zero_fills():
    fs, _ = build_fs()

    async def scenario():
        info, _ = await fs.create(dir_inode(fs, "2026", "09", "24"), b"a.ics~", 0o644, CREATE)
        await fs.write(info.fh, 4, b"xy")
        return await fs.read(info.fh, 0, 100)

    assert trio.run(scenario) == b"\x00\x00\x00\x00xy"


# -- edit existing -------------------------------------------------------------


def test_edit_existing_file_patches_once():
    fs, client = build_fs([make_event("e1", "Standup", day=23)])
    new = ics("SUMMARY:Daily Standup", "DTSTART:20260923T091500", "DTEND:20260923T094500")
    trio.run(lambda: overwrite(fs, "2026", "09", "23", "0900-0930_standup.ics", data=new))
    assert [(eid, body["summary"]) for eid, body in client.patch_calls] == [("e1", "Daily Standup")]
    assert client.insert_calls == []


def test_removing_description_line_clears_it_in_google():
    fs, client = build_fs([make_event("e1", "Standup", day=23, description="agenda")])
    new = ics("SUMMARY:Standup", "DTSTART:20260923T090000", "DTEND:20260923T093000")
    trio.run(lambda: overwrite(fs, "2026", "09", "23", "0900-0930_standup.ics", data=new))
    assert client.patch_calls[0][1]["description"] == ""


def test_editing_what_cat_returned_roundtrips_cleanly():
    """The emitted ICS (TZID, mailto:, DTSTAMP...) must be acceptable input."""
    fs, client = build_fs([make_event("e1", "Standup", day=23, attendees=["a@example.com"])])

    async def scenario():
        original = await read_file(fs, "2026", "09", "23", "0900-0930_standup.ics")
        edited = original.replace(b"SUMMARY:Standup", b"SUMMARY:Stand-up")
        await overwrite(fs, "2026", "09", "23", "0900-0930_standup.ics", data=edited)

    trio.run(scenario)
    body = client.patch_calls[0][1]
    assert body["summary"] == "Stand-up"
    assert body["start"]["timeZone"] == "America/Chicago"


def test_edit_without_dtend_keeps_original_duration():
    fs, client = build_fs([make_event("e1", "Standup", day=23, minutes=45)])
    new = ics("SUMMARY:Standup", "DTSTART:20260923T100000")
    trio.run(lambda: overwrite(fs, "2026", "09", "23", "0900-0945_standup.ics", data=new))
    assert client.patch_calls[0][1]["end"]["dateTime"] == "2026-09-23T10:45:00-05:00"


def test_invalid_ics_on_edit_is_eio_and_previous_content_is_kept():
    fs, client = build_fs([make_event("e1", "Standup", day=23)])

    async def scenario():
        await expect_errno(
            errno.EIO, overwrite(fs, "2026", "09", "23", "0900-0930_standup.ics", data=b"garbage")
        )
        return await read_file(fs, "2026", "09", "23", "0900-0930_standup.ics")

    assert b"SUMMARY:Standup" in trio.run(scenario)
    assert client.patch_calls == []


def test_edit_missing_dtstart_is_eio():
    fs, client = build_fs([make_event("e1", "Standup", day=23)])

    async def scenario():
        await expect_errno(
            errno.EIO,
            overwrite(fs, "2026", "09", "23", "0900-0930_standup.ics", data=ics("SUMMARY:x")),
        )

    trio.run(scenario)
    assert client.patch_calls == []


def test_truncating_existing_file_to_empty_is_eio_not_a_delete():
    fs, client = build_fs([make_event("e1", "Standup", day=23)])

    async def scenario():
        attr = await lookup_path(fs, "2026", "09", "23", "0900-0930_standup.ics")
        info = await fs.open(attr.st_ino, os.O_WRONLY | os.O_TRUNC)
        await expect_errno(errno.EIO, fs.release(info.fh))

    trio.run(scenario)
    assert client.patch_calls == client.delete_calls == []


def test_truncate_via_setattr_on_open_file_then_write():
    """Kernels without atomic O_TRUNC send open() then setattr(size=0)."""
    fs, client = build_fs([make_event("e1", "Standup", day=23)])
    new = ics("SUMMARY:Short", "DTSTART:20260923T090000", "DTEND:20260923T091000")

    async def scenario():
        attr = await lookup_path(fs, "2026", "09", "23", "0900-0930_standup.ics")
        info = await fs.open(attr.st_ino, os.O_WRONLY)
        fields = setattr_fields(size=True)
        attr.st_size = 0
        truncated = await fs.setattr(attr.st_ino, attr, fields, info.fh)
        await fs.write(info.fh, 0, new)
        await fs.release(info.fh)
        return truncated.st_size

    assert trio.run(scenario) == 0
    assert client.patch_calls[0][1]["summary"] == "Short"


def test_path_truncate_without_open_handle_is_eperm():
    fs, client = build_fs([make_event("e1", "Standup", day=23)])

    async def scenario():
        attr = await lookup_path(fs, "2026", "09", "23", "0900-0930_standup.ics")
        fields = setattr_fields(size=True)
        await expect_errno(errno.EPERM, fs.setattr(attr.st_ino, attr, fields, None))

    trio.run(scenario)
    assert client.patch_calls == []


def test_open_for_write_without_writing_makes_no_call_and_leaves_no_state():
    fs, client = build_fs([make_event("e1", "Standup", day=23)])

    async def scenario():
        attr = await lookup_path(fs, "2026", "09", "23", "0900-0930_standup.ics")
        info = await fs.open(attr.st_ino, os.O_WRONLY)
        await fs.release(info.fh)

    trio.run(scenario)
    assert client.patch_calls == []
    assert fs._pending == {}


def test_write_to_recurring_instance_rejected_at_open():
    fs, client = build_fs([make_event("e1", "Weekly", day=23, recurring_event_id="s1")])

    async def scenario():
        attr = await lookup_path(fs, "2026", "09", "23", "0900-0930_weekly.ics")
        await expect_errno(errno.EPERM, fs.open(attr.st_ino, os.O_WRONLY))
        await expect_errno(errno.EPERM, fs.open(attr.st_ino, os.O_RDWR | os.O_TRUNC))

    trio.run(scenario)
    assert client.patch_calls == []


def test_recurring_rejection_is_logged(caplog):
    fs, _ = build_fs([make_event("e1", "Weekly", day=23, recurring_event_id="s1")])

    async def scenario():
        attr = await lookup_path(fs, "2026", "09", "23", "0900-0930_weekly.ics")
        await expect_errno(errno.EPERM, fs.open(attr.st_ino, os.O_WRONLY))

    with caplog.at_level(logging.WARNING):
        trio.run(scenario)
    assert "v1 does not edit recurring instances" in caplog.text


# -- API failures ----------------------------------------------------------------


def test_api_error_on_commit_is_eio_and_does_not_crash():
    fs, client = build_fs()
    client.fail_next = CalendarApiError("503 backend error", status=503)

    async def scenario():
        day = dir_inode(fs, "2026", "09", "24")
        await expect_errno(errno.EIO, write_file(fs, day, b"x.ics", DENTIST_ICS))
        # The filesystem keeps working afterwards.
        await write_file(fs, day, b"x.ics", DENTIST_ICS)

    trio.run(scenario)
    assert len(client.insert_calls) == 1


def test_patch_of_remotely_deleted_event_is_enoent_and_drops_it():
    fs, client = build_fs([make_event("e1", "Standup", day=23)])
    del client._events["e1"]  # someone deleted it in the Google UI
    new = ics("SUMMARY:x", "DTSTART:20260923T090000", "DTEND:20260923T093000")

    async def scenario():
        await expect_errno(
            errno.ENOENT, overwrite(fs, "2026", "09", "23", "0900-0930_standup.ics", data=new)
        )
        await expect_errno(
            errno.ENOENT, lookup_path(fs, "2026", "09", "23", "0900-0930_standup.ics")
        )

    trio.run(scenario)


def test_unexpected_exception_in_handler_becomes_eio(monkeypatch):
    """pyfuse3 kills the whole mount on a non-FUSEError exception."""
    fs, client = build_fs()

    def boom(body):
        raise RuntimeError("bug")

    monkeypatch.setattr(client, "insert", boom)

    async def scenario():
        await expect_errno(
            errno.EIO, write_file(fs, dir_inode(fs, "2026", "09", "24"), b"x.ics", DENTIST_ICS)
        )

    trio.run(scenario)


# -- unlink ----------------------------------------------------------------------


def test_unlink_non_recurring_calls_delete_once():
    fs, client = build_fs([make_event("e1", "Standup", day=23)])

    async def scenario():
        day = await lookup_path(fs, "2026", "09", "23")
        await fs.unlink(day.st_ino, b"0900-0930_standup.ics")
        await expect_errno(errno.ENOENT, fs.lookup(day.st_ino, b"0900-0930_standup.ics"))

    trio.run(scenario)
    assert client.delete_calls == ["e1"]
    assert fs._children(fs._path_for_inode(pyfuse3.ROOT_INODE)) == []  # no empty year listed


def test_unlink_recurring_instance_is_eperm_with_no_delete():
    fs, client = build_fs([make_event("e1", "Weekly", day=23, recurring_event_id="s1")])

    async def scenario():
        day = await lookup_path(fs, "2026", "09", "23")
        await expect_errno(errno.EPERM, fs.unlink(day.st_ino, b"0900-0930_weekly.ics"))

    trio.run(scenario)
    assert client.delete_calls == []


def test_unlink_of_event_already_deleted_remotely_succeeds():
    fs, client = build_fs([make_event("e1", "Standup", day=23)])
    del client._events["e1"]

    async def scenario():
        day = await lookup_path(fs, "2026", "09", "23")
        await fs.unlink(day.st_ino, b"0900-0930_standup.ics")

    trio.run(scenario)
    assert fs._index.get("e1") is None


def test_unlink_missing_file_is_enoent():
    fs, _ = build_fs([make_event("e1", "Standup", day=23)])

    async def scenario():
        day = await lookup_path(fs, "2026", "09", "23")
        await expect_errno(errno.ENOENT, fs.unlink(day.st_ino, b"nope.ics"))

    trio.run(scenario)


def test_unlink_api_failure_is_eio_and_event_stays():
    fs, client = build_fs([make_event("e1", "Standup", day=23)])
    client.fail_next = CalendarApiError("500", status=500)

    async def scenario():
        day = await lookup_path(fs, "2026", "09", "23")
        await expect_errno(errno.EIO, fs.unlink(day.st_ino, b"0900-0930_standup.ics"))

    trio.run(scenario)
    assert fs._index.get("e1") is not None


# -- rename ----------------------------------------------------------------------


async def rename(fs, old: tuple, new: tuple, flags: int = 0) -> None:
    await fs.rename(
        dir_inode(fs, *old[:-1]),
        old[-1].encode(),
        dir_inode(fs, *new[:-1]),
        new[-1].encode(),
        flags,
    )


def test_rename_across_days_patches_new_date_same_time():
    fs, client = build_fs([make_event("e1", "Dentist", day=24, hour=15)])
    trio.run(
        rename,
        fs,
        ("2026", "09", "24", "1500-1530_dentist.ics"),
        ("2026", "09", "25", "1500-1530_dentist.ics"),
    )
    event_id, body = client.patch_calls[0]
    assert event_id == "e1"
    assert body["start"]["dateTime"] == "2026-09-25T15:00:00-05:00"
    assert body["end"]["dateTime"] == "2026-09-25T15:30:00-05:00"
    assert "summary" not in body


def test_rename_across_dst_change_keeps_local_clock_time():
    fs, client = build_fs([make_event("e1", "Review", day=30, hour=15, month=10)])
    trio.run(
        rename,
        fs,
        ("2026", "10", "30", "1500-1530_review.ics"),
        ("2026", "11", "03", "1500-1530_review.ics"),
    )
    body = client.patch_calls[0][1]
    assert body["start"]["dateTime"] == "2026-11-03T15:00:00-06:00"  # CST, not CDT


def test_rename_multi_day_all_day_event_keeps_its_span():
    fs, client = build_fs([make_all_day("trip", "Trip", day=24, days=3)])
    trio.run(
        rename, fs, ("2026", "09", "24", "0000_trip.ics"), ("2026", "10", "01", "0000_trip.ics")
    )
    body = client.patch_calls[0][1]
    assert body["start"] == {"date": "2026-10-01"}
    assert body["end"] == {"date": "2026-10-04"}


def test_rename_same_day_new_slug_retitles():
    fs, client = build_fs([make_event("e1", "Standup", day=23)])
    trio.run(
        rename,
        fs,
        ("2026", "09", "23", "0900-0930_standup.ics"),
        ("2026", "09", "23", "0900-0930_daily_sync.ics"),
    )
    assert client.patch_calls == [("e1", {"summary": "daily sync"})]


def test_rename_cross_day_without_slug_change_preserves_title_casing():
    fs, client = build_fs([make_event("e1", "Team Standup", day=23)])
    trio.run(
        rename,
        fs,
        ("2026", "09", "23", "0900-0930_team_standup.ics"),
        ("2026", "09", "24", "0900-0930_team_standup.ics"),
    )
    assert "summary" not in client.patch_calls[0][1]


def test_rename_of_collision_suffixed_file_ignores_the_suffix():
    fs, client = build_fs(
        [make_event("aaaaaaaa1", "Standup", day=23), make_event("bbbbbbbb2", "Standup", day=23)]
    )
    trio.run(
        rename,
        fs,
        ("2026", "09", "23", "0900-0930_standup__aaaaaaaa.ics"),
        ("2026", "09", "24", "0900-0930_standup__aaaaaaaa.ics"),
    )
    assert "summary" not in client.patch_calls[0][1]


def test_rename_cannot_change_the_time():
    fs, client = build_fs([make_event("e1", "Standup", day=23)])

    async def scenario():
        await expect_errno(
            errno.EINVAL,
            rename(
                fs,
                ("2026", "09", "23", "0900-0930_standup.ics"),
                ("2026", "09", "23", "1000-1030_standup.ics"),
            ),
        )

    trio.run(scenario)
    assert client.patch_calls == []


def test_rename_onto_another_existing_event_is_eexist():
    fs, client = build_fs([make_event("e1", "A", day=23), make_event("e2", "B", day=24)])

    async def scenario():
        await expect_errno(
            errno.EEXIST,
            rename(
                fs, ("2026", "09", "23", "0900-0930_a.ics"), ("2026", "09", "24", "0900-0930_b.ics")
            ),
        )

    trio.run(scenario)
    assert client.patch_calls == client.delete_calls == []


def test_rename_recurring_instance_is_eperm():
    fs, client = build_fs([make_event("e1", "Weekly", day=23, recurring_event_id="s1")])

    async def scenario():
        await expect_errno(
            errno.EPERM,
            rename(
                fs,
                ("2026", "09", "23", "0900-0930_weekly.ics"),
                ("2026", "09", "24", "0900-0930_weekly.ics"),
            ),
        )

    trio.run(scenario)
    assert client.patch_calls == []


def test_rename_directory_is_eperm():
    fs, _ = build_fs([make_event("e1", "Standup", day=23)])

    async def scenario():
        await expect_errno(errno.EPERM, rename(fs, ("2026", "09", "23"), ("2026", "09", "24")))

    trio.run(scenario)


def test_rename_exchange_is_einval():
    fs, _ = build_fs([make_event("e1", "A", day=23), make_event("e2", "B", day=23, hour=10)])

    async def scenario():
        await expect_errno(
            errno.EINVAL,
            rename(
                fs,
                ("2026", "09", "23", "0900-0930_a.ics"),
                ("2026", "09", "23", "1000-1030_b.ics"),
                flags=pyfuse3.RENAME_EXCHANGE,
            ),
        )

    trio.run(scenario)


def test_rename_to_same_name_is_a_no_op():
    fs, client = build_fs([make_event("e1", "Standup", day=23)])
    path = ("2026", "09", "23", "0900-0930_standup.ics")
    trio.run(rename, fs, path, path)
    assert client.patch_calls == []


# -- editor save patterns ------------------------------------------------------


def test_scratch_files_never_call_google():
    fs, client = build_fs()

    async def scenario():
        day = dir_inode(fs, "2026", "09", "24")
        for name in (
            b".foo.ics.swp",
            b".foo.ics.swo",
            b"foo.ics.tmp",
            b".#foo.ics",
            b"foo.ics~",
            b"#foo.ics#",
        ):
            await write_file(fs, day, name, DENTIST_ICS)
            await fs.unlink(day, name)

    trio.run(scenario)
    assert client.insert_calls == client.patch_calls == client.delete_calls == []
    assert fs._pending == {}


def test_write_temp_then_rename_to_new_name_inserts_once():
    fs, client = build_fs()

    async def scenario():
        day = dir_inode(fs, "2026", "09", "24")
        await write_file(fs, day, b"dentist.ics.tmp", DENTIST_ICS)
        assert client.insert_calls == []
        await fs.rename(day, b"dentist.ics.tmp", day, b"1500-1530_dentist.ics", 0)

    trio.run(scenario)
    assert len(client.insert_calls) == 1


def test_write_temp_then_rename_over_existing_file_patches_not_duplicates():
    """JetBrains/Emacs-style atomic save: write a temp file, rename it over the original."""
    fs, client = build_fs([make_event("e1", "Standup", day=23)])
    new = ics("SUMMARY:Standup v2", "DTSTART:20260923T090000", "DTEND:20260923T093000")

    async def scenario():
        day = await lookup_path(fs, "2026", "09", "23")
        await write_file(fs, day.st_ino, b"0900-0930_standup.ics___jb_tmp___", new)
        await fs.rename(
            day.st_ino,
            b"0900-0930_standup.ics___jb_tmp___",
            day.st_ino,
            b"0900-0930_standup.ics",
            0,
        )

    trio.run(scenario)
    assert client.insert_calls == []
    assert [(eid, b["summary"]) for eid, b in client.patch_calls] == [("e1", "Standup v2")]


def test_vim_default_backup_rename_save_patches_not_duplicates():
    """vim (backupcopy=auto): rename original to foo~, write new foo, delete foo~."""
    fs, client = build_fs([make_event("e1", "Standup", day=23)])
    new = ics("SUMMARY:Standup (moved room)", "DTSTART:20260923T090000", "DTEND:20260923T093000")

    async def scenario():
        day = (await lookup_path(fs, "2026", "09", "23")).st_ino
        name = b"0900-0930_standup.ics"
        await fs.rename(day, name, day, name + b"~", 0)
        await write_file(fs, day, name, new)
        await fs.unlink(day, name + b"~")
        return await read_file(fs, "2026", "09", "23", "0900-0930_standup_moved_room.ics")

    data = trio.run(scenario)
    assert client.insert_calls == [] and client.delete_calls == []
    assert [eid for eid, _ in client.patch_calls] == ["e1"]
    assert b"SUMMARY:Standup (moved room)" in data
    assert fs._pending == {}


def test_parked_event_deleted_as_junk_is_restored_not_deleted():
    """`mv foo.ics foo.ics~ && rm foo.ics~` must never delete the Google event."""
    fs, client = build_fs([make_event("e1", "Standup", day=23)])

    async def scenario():
        day = (await lookup_path(fs, "2026", "09", "23")).st_ino
        await fs.rename(day, b"0900-0930_standup.ics", day, b"0900-0930_standup.ics~", 0)
        await fs.unlink(day, b"0900-0930_standup.ics~")

    trio.run(scenario)
    assert client.delete_calls == []
    assert fs._index.path_for_id("e1") is not None


def test_parked_event_renamed_back_is_restored_without_api_call():
    fs, client = build_fs([make_event("e1", "Standup", day=23)])

    async def scenario():
        day = (await lookup_path(fs, "2026", "09", "23")).st_ino
        await fs.rename(day, b"0900-0930_standup.ics", day, b"0900-0930_standup.ics~", 0)
        await fs.rename(day, b"0900-0930_standup.ics~", day, b"0900-0930_standup.ics", 0)

    trio.run(scenario)
    assert client.patch_calls == client.insert_calls == []
    assert fs._pending == {}
    assert fs._index.get("e1") is not None


def test_reopening_scratch_file_with_o_trunc_discards_old_content():
    fs, _ = build_fs()

    async def scenario():
        day = dir_inode(fs, "2026", "09", "24")
        await write_file(fs, day, b"x.tmp", b"a long first version")
        attr = await fs.lookup(day, b"x.tmp")
        info = await fs.open(attr.st_ino, os.O_WRONLY | os.O_TRUNC)
        await fs.write(info.fh, 0, b"short")
        return await fs.read(info.fh, 0, 100)

    assert trio.run(scenario) == b"short"


def test_created_event_keeps_working_after_being_canonicalized(tmp_path):
    """After commit the file is re-listed under its canonical name; re-edit it there."""
    fs, client = build_fs()

    async def scenario():
        day = dir_inode(fs, "2026", "09", "24")
        await write_file(fs, day, b"dentist.ics", DENTIST_ICS)
        edited = DENTIST_ICS.replace(b"Dentist", b"Dentist (confirmed)")
        await overwrite(fs, "2026", "09", "24", "1500-1530_dentist.ics", data=edited)

    trio.run(scenario)
    assert len(client.insert_calls) == 1
    assert client.patch_calls[0][1]["summary"] == "Dentist (confirmed)"
    assert client._events[client.patch_calls[0][0]].start.tzinfo == CHICAGO


# -- close semantics & canonical names ----------------------------------------


def test_flush_commits_and_reports_errors_to_close():
    """FLUSH is synchronous with close(2); RELEASE errors never reach the app."""
    fs, client = build_fs([make_event("e1", "Standup", day=23)])

    async def scenario():
        attr = await lookup_path(fs, "2026", "09", "23", "0900-0930_standup.ics")
        info = await fs.open(attr.st_ino, os.O_WRONLY | os.O_TRUNC)
        await fs.write(info.fh, 0, b"garbage")
        await expect_errno(errno.EIO, fs.flush(info.fh))
        await fs.release(info.fh)  # nothing left to commit, must not raise

    trio.run(scenario)
    assert client.patch_calls == []


def test_repeated_flush_without_new_writes_commits_once():
    fs, client = build_fs()

    async def scenario():
        info, _ = await fs.create(dir_inode(fs, "2026", "09", "24"), b"x.ics", 0o644, CREATE)
        await fs.write(info.fh, 0, DENTIST_ICS)
        await fs.flush(info.fh)
        await fs.flush(info.fh)  # e.g. a dup'd fd being closed
        await fs.release(info.fh)

    trio.run(scenario)
    assert len(client.insert_calls) == 1


def test_name_used_to_save_keeps_resolving_after_canonicalization():
    """`touch lunch.ics` closes, then utime()s lunch.ics; that must not ENOENT."""
    fs, _ = build_fs()

    async def scenario():
        day = dir_inode(fs, "2026", "09", "24")
        await write_file(fs, day, b"lunch.ics", b"")
        attr = await fs.lookup(day, b"lunch.ics")
        again = await fs.getattr(attr.st_ino)
        return attr.st_size, again.st_size

    size, again = trio.run(scenario)
    assert size == again > 0
    listing = [n for n, _, _ in fs._children(fs._path_for_inode(dir_inode(fs, "2026", "09", "24")))]
    assert listing == ["0900-0930_lunch.ics"]  # alias is not listed


def test_alias_expires(monkeypatch):
    from gcalfuse import fs as fs_mod

    fs, _ = build_fs()
    clock = [100.0]
    monkeypatch.setattr(fs_mod.time, "monotonic", lambda: clock[0])

    async def scenario():
        day = dir_inode(fs, "2026", "09", "24")
        await write_file(fs, day, b"lunch.ics", b"")
        await fs.lookup(day, b"lunch.ics")
        clock[0] += fs_mod.ALIAS_SECONDS + 1
        await expect_errno(errno.ENOENT, fs.lookup(day, b"lunch.ics"))

    trio.run(scenario)


def test_rm_via_alias_deletes_the_event():
    fs, client = build_fs()

    async def scenario():
        day = dir_inode(fs, "2026", "09", "24")
        await write_file(fs, day, b"lunch.ics", b"")
        await fs.unlink(day, b"lunch.ics")

    trio.run(scenario)
    assert len(client.delete_calls) == 1


def test_file_attributes_are_never_cached_by_the_kernel():
    """Sizes change behind the kernel's back; it truncates reads to cached sizes."""
    fs, _ = build_fs([make_event("e1", "Standup", day=23)])

    async def scenario():
        day = await lookup_path(fs, "2026", "09", "23")
        f = await lookup_path(fs, "2026", "09", "23", "0900-0930_standup.ics")
        return day.attr_timeout, f.attr_timeout

    day_timeout, file_timeout = trio.run(scenario)
    assert file_timeout == 0 and day_timeout > 0


def test_rejected_vim_style_save_leaves_original_event_in_place():
    fs, client = build_fs([make_event("e1", "Standup", day=23)])

    async def scenario():
        day = (await lookup_path(fs, "2026", "09", "23")).st_ino
        name = b"0900-0930_standup.ics"
        await fs.rename(day, name, day, name + b"~", 0)
        await expect_errno(errno.EIO, write_file(fs, day, name, b"not a calendar"))
        await fs.unlink(day, name + b"~")
        return await read_file(fs, "2026", "09", "23", "0900-0930_standup.ics")

    assert b"SUMMARY:Standup" in trio.run(scenario)
    assert client.patch_calls == client.insert_calls == client.delete_calls == []


def test_vim_recovery_after_rejected_save_never_deletes_the_event():
    """Exact sequence traced from real vim (backupcopy=auto) when a save fails:
    rename foo -> foo~, create foo, write, close fails, unlink foo, rename foo~ -> foo."""
    fs, client = build_fs([make_event("e1", "Standup", day=23)])

    async def scenario():
        day = (await lookup_path(fs, "2026", "09", "23")).st_ino
        name = b"0900-0930_standup.ics"
        await fs.rename(day, name, day, name + b"~", 0)
        info, _ = await fs.create(day, name, 0o644, CREATE)
        await fs.write(info.fh, 0, b"oops\n")
        await expect_errno(errno.EIO, fs.flush(info.fh))
        await fs.release(info.fh)
        await expect_errno(errno.ENOENT, fs.unlink(day, name))
        await fs.rename(day, name + b"~", day, name, 0)
        return await read_file(fs, "2026", "09", "23", "0900-0930_standup.ics")

    assert b"SUMMARY:Standup" in trio.run(scenario)
    assert client.delete_calls == client.patch_calls == client.insert_calls == []
    assert fs._pending == {}


def test_saving_unchanged_content_makes_no_api_call():
    fs, client = build_fs([make_event("e1", "Standup", day=23, description="agenda")])

    async def scenario():
        original = await read_file(fs, "2026", "09", "23", "0900-0930_standup.ics")
        await overwrite(fs, "2026", "09", "23", "0900-0930_standup.ics", data=original)
        return await read_file(fs, "2026", "09", "23", "0900-0930_standup.ics")

    assert b"SUMMARY:Standup" in trio.run(scenario)
    assert client.patch_calls == []
