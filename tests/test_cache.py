"""cache.py: EventIndex path bookkeeping and CalendarCache refresh behaviour."""

import logging
import threading
from datetime import datetime, timedelta
from pathlib import PurePosixPath

import pytest

from gcalfuse import cache as cache_mod
from gcalfuse.api import CalendarApiError
from gcalfuse.cache import CalendarCache, EventIndex, EventRecord

from .fakes import FakeCalendarClient, FakeSyncClient
from .helpers import CHICAGO, make_all_day, make_event

# -- EventIndex ------------------------------------------------------------------


def test_add_and_get_by_id():
    index = EventIndex(CHICAGO)
    event = make_event("e1", "Standup", day=23)
    index.add(event)
    assert index.get("e1") is event
    assert index.get("missing") is None


def test_get_by_path_and_path_for_id_are_inverse():
    index = EventIndex(CHICAGO)
    index.add(make_event("e1", "Standup", day=23))
    path = PurePosixPath("/2026/09/23/0900-0930_standup.ics")
    assert index.get_by_path(path).event_id == "e1"
    assert index.path_for_id("e1") == path


def test_remove_event_and_remove_unknown_is_harmless():
    index = EventIndex(CHICAGO)
    index.add(make_event("e1", "Standup", day=23))
    index.remove("e1")
    index.remove("never-existed")
    assert index.get("e1") is None and index.path_for_id("e1") is None
    assert index.years() == []


def test_list_years_months_days_files_across_two_days():
    index = EventIndex(CHICAGO)
    index.add(make_event("e1", "Standup", day=23, hour=9))
    index.add(make_event("e2", "Dentist", day=24, hour=15))
    assert index.years() == [2026]
    assert index.months(2026) == [9]
    assert index.days(2026, 9) == [23, 24]
    assert index.files(2026, 9, 23) == ["0900-0930_standup.ics"]
    assert index.files(2026, 9, 24) == ["1500-1530_dentist.ics"]
    assert index.files(2026, 9, 25) == []


def test_collision_appends_event_id_suffix_to_every_colliding_event():
    index = EventIndex(CHICAGO)
    index.add(make_event("aaaaaaaa1111", "Standup", day=23))
    index.add(make_event("bbbbbbbb2222", "Standup", day=23))
    assert index.files(2026, 9, 23) == [
        "0900-0930_standup__aaaaaaaa.ics",
        "0900-0930_standup__bbbbbbbb.ics",
    ]


def test_collision_suffix_goes_away_when_the_other_event_is_removed():
    index = EventIndex(CHICAGO)
    index.add(make_event("aaaaaaaa1111", "Standup", day=23))
    index.add(make_event("bbbbbbbb2222", "Standup", day=23))
    index.remove("bbbbbbbb2222")
    assert index.files(2026, 9, 23) == ["0900-0930_standup.ics"]


def test_updating_a_record_moves_its_path():
    index = EventIndex(CHICAGO)
    index.add(make_event("e1", "Standup", day=23))
    index.add(make_event("e1", "Standup", day=24))
    assert index.days(2026, 9) == [24]


def test_same_title_different_times_do_not_collide():
    index = EventIndex(CHICAGO)
    index.add(make_event("e1", "Standup", day=23, hour=9))
    index.add(make_event("e2", "Standup", day=23, hour=10))
    assert index.files(2026, 9, 23) == ["0900-0930_standup.ics", "1000-1030_standup.ics"]


def test_all_day_event_uses_its_date_not_a_tz_shifted_one():
    index = EventIndex(CHICAGO)
    index.add(make_all_day("pto", "PTO", day=24))
    assert index.files(2026, 9, 24) == ["0000_pto.ics"]


def test_apply_upserts_and_removes_in_one_rebuild(monkeypatch):
    index = EventIndex(CHICAGO)
    index.replace_all([make_event("a", "A", day=1), make_event("b", "B", day=2)])
    rebuilds = []
    original = index._rebuild_paths
    monkeypatch.setattr(index, "_rebuild_paths", lambda: (rebuilds.append(1), original()))

    index.apply([make_event("a", "A2", day=3), make_event("c", "C", day=4)], ["b", "missing"])

    assert len(rebuilds) == 1
    assert index.get("a").summary == "A2" and index.get("b") is None and index.get("c")
    assert index.days(2026, 9) == [3, 4]


def test_replace_all_rebuilds_index():
    index = EventIndex(CHICAGO)
    index.add(make_event("e1", "Old", day=23))
    index.replace_all([make_event("e2", "New", day=24)])
    assert index.get("e1") is None and index.get("e2") is not None
    assert index.days(2026, 9) == [24]


def test_index_is_safe_under_concurrent_readers_and_writers():
    index = EventIndex(CHICAGO)
    errors = []

    def writer():
        for i in range(60):
            index.add(make_event(f"e{i}", "Standup", day=1 + i % 28))

    def reader():
        try:
            for _ in range(60):
                for day in index.days(2026, 9):
                    index.files(2026, 9, day)
        except Exception as exc:  # pragma: no cover - only on a real race
            errors.append(exc)

    threads = [threading.Thread(target=writer)] + [
        threading.Thread(target=reader) for _ in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


# -- CalendarCache -----------------------------------------------------------------


def make_cache(client, past=30, future=90):
    return CalendarCache(client, CHICAGO, past, future, poll_seconds=60)


def near_now(event_id, summary, days_from_now):
    start = (datetime.now(CHICAGO) + timedelta(days=days_from_now)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    return EventRecord(event_id, summary, start, end=start + timedelta(minutes=30))


def test_refresh_full_loads_only_the_configured_window():
    client = FakeCalendarClient(
        CHICAGO,
        [near_now("in", "Soon", 3), near_now("past", "Old", -60), near_now("far", "Later", 200)],
    )
    cache = make_cache(client)
    cache.refresh_full()
    assert cache.index.get("in") is not None
    assert cache.index.get("past") is None and cache.index.get("far") is None


def test_incremental_refresh_applies_updates_and_deletions():
    client = FakeSyncClient(CHICAGO, [near_now("a", "A", 1), near_now("b", "B", 2)])
    cache = make_cache(client)
    cache.refresh_full()

    client.next_delta = ([near_now("a", "A renamed", 1), near_now("c", "C", 3)], ["b"])
    cache.refresh_incremental()

    assert cache.index.get("a").summary == "A renamed"
    assert cache.index.get("b") is None
    assert cache.index.get("c") is not None
    assert client.list_window_calls == 1  # no second full fetch


def test_incremental_refresh_evicts_event_moved_outside_window():
    client = FakeSyncClient(CHICAGO, [near_now("a", "A", 1)])
    cache = make_cache(client)
    cache.refresh_full()
    client.next_delta = ([near_now("a", "A", 400)], [])
    cache.refresh_incremental()
    assert cache.index.get("a") is None


def test_expired_sync_token_falls_back_to_full_refetch(caplog):
    client = FakeSyncClient(CHICAGO, [near_now("a", "A", 1)])
    cache = make_cache(client)
    cache.refresh_full()
    client.expire_token = True
    with caplog.at_level(logging.WARNING):
        cache.refresh_incremental()
    assert client.list_window_calls == 2
    assert "sync token expired" in caplog.text


def test_client_without_list_updates_always_full_refetches():
    client = FakeCalendarClient(CHICAGO, [near_now("a", "A", 1)])
    cache = make_cache(client)
    cache.refresh_full()
    client._events["b"] = near_now("b", "B", 2)
    cache.refresh_incremental()
    assert cache.index.get("b") is not None


def test_periodic_full_refresh_catches_events_sliding_into_window(monkeypatch):
    """Deltas only report changes, so an old event entering the window needs a full fetch."""
    clock = [1000.0]
    monkeypatch.setattr(cache_mod.time, "monotonic", lambda: clock[0])
    client = FakeSyncClient(CHICAGO, [near_now("a", "A", 1)])
    cache = make_cache(client)
    cache.refresh_full()

    cache.refresh_incremental()
    assert client.list_window_calls == 1

    clock[0] += cache_mod.FULL_REFRESH_SECONDS
    cache.refresh_incremental()
    assert client.list_window_calls == 2


def test_refresh_once_keeps_stale_cache_on_failure(monkeypatch, caplog):
    client = FakeCalendarClient(CHICAGO, [near_now("a", "A", 1)])
    cache = make_cache(client)
    cache.refresh_full()

    def broken(*args):
        raise CalendarApiError("503", status=503)

    monkeypatch.setattr(client, "list_window", broken)
    with caplog.at_level(logging.ERROR):
        assert cache.refresh_once() is False
    assert cache.index.get("a") is not None
    assert "keeping stale cache" in caplog.text


@pytest.mark.parametrize("status", [401, 403])
def test_refresh_once_auth_error_logs_actionable_message(monkeypatch, caplog, status):
    client = FakeCalendarClient(CHICAGO, [near_now("a", "A", 1)])
    cache = make_cache(client)
    cache.refresh_full()
    monkeypatch.setattr(
        client,
        "list_window",
        lambda *a: (_ for _ in ()).throw(CalendarApiError("x", status=status)),
    )
    with caplog.at_level(logging.ERROR):
        cache.refresh_once()
    assert "gcalfuse auth" in caplog.text
    assert cache.index.get("a") is not None


def test_background_refresh_thread_starts_and_stops_promptly():
    client = FakeCalendarClient(CHICAGO, [])
    cache = CalendarCache(client, CHICAGO, 30, 90, poll_seconds=3600)
    cache.start_background_refresh()
    cache.stop_background_refresh()
    assert not cache._thread.is_alive()
