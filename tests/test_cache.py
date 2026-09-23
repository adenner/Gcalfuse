from datetime import datetime
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo

from gcalfuse.cache import EventIndex, EventRecord

CHICAGO = ZoneInfo("America/Chicago")


def make_event(event_id, summary, day, hour=9, minute=0, duration_minutes=30):
    start = datetime(2026, 9, day, hour, minute, tzinfo=CHICAGO)
    end = start.replace(minute=(minute + duration_minutes) % 60)
    return EventRecord(event_id=event_id, summary=summary, start=start, end=end)


def test_add_and_get_by_id():
    index = EventIndex(CHICAGO)
    event = make_event("e1", "Standup", day=23)
    index.add(event)
    assert index.get("e1") is event
    assert index.get("missing") is None


def test_get_by_path():
    index = EventIndex(CHICAGO)
    event = make_event("e1", "Standup", day=23, hour=9, minute=0)
    index.add(event)
    path = PurePosixPath("/2026/09/23/0900-0930_standup.ics")
    assert index.get_by_path(path) is event
    assert index.path_for_id("e1") == path


def test_remove_event():
    index = EventIndex(CHICAGO)
    event = make_event("e1", "Standup", day=23)
    index.add(event)
    index.remove("e1")
    assert index.get("e1") is None
    assert index.path_for_id("e1") is None


def test_list_years_months_days_files_across_two_days():
    index = EventIndex(CHICAGO)
    e1 = make_event("e1", "Standup", day=23, hour=9)
    e2 = make_event("e2", "Dentist", day=24, hour=15)
    index.add(e1)
    index.add(e2)

    assert index.years() == [2026]
    assert index.months(2026) == [9]
    assert index.days(2026, 9) == [23, 24]
    assert index.files(2026, 9, 23) == ["0900-0930_standup.ics"]
    assert index.files(2026, 9, 24) == ["1500-1530_dentist.ics"]


def test_collision_appends_event_id_suffix():
    index = EventIndex(CHICAGO)
    e1 = make_event("aaaaaaaa1111", "Standup", day=23, hour=9)
    e2 = make_event("bbbbbbbb2222", "Standup", day=23, hour=9)
    index.add(e1)
    index.add(e2)

    files = index.files(2026, 9, 23)
    assert len(files) == 2
    assert "0900-0930_standup__aaaaaaaa.ics" in files
    assert "0900-0930_standup__bbbbbbbb.ics" in files


def test_replace_all_rebuilds_index():
    index = EventIndex(CHICAGO)
    index.add(make_event("e1", "Old", day=23))

    new_events = [make_event("e2", "New", day=24)]
    index.replace_all(new_events)

    assert index.get("e1") is None
    assert index.get("e2") is not None
    assert index.days(2026, 9) == [24]
