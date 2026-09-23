from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo

import pytest

from gcalfuse.paths import (
    DayDir,
    EventFile,
    InvalidPathError,
    MonthDir,
    RootDir,
    YearDir,
    filename_for,
    is_editor_junk,
    parse_path,
    path_for,
    slugify,
    with_collision_suffix,
)

CHICAGO = ZoneInfo("America/Chicago")


@dataclass
class FakeEvent:
    event_id: str
    summary: str
    start: datetime
    all_day: bool = False
    end: datetime | None = None


def test_slugify_spaces():
    assert slugify("Team Standup") == "team_standup"


def test_slugify_slashes():
    assert slugify("Q3/Q4 Planning") == "q3_q4_planning"


def test_slugify_emoji():
    assert slugify("Party 🎉 Time") == "party_time"


def test_slugify_empty_is_untitled():
    assert slugify("") == "untitled"


def test_slugify_only_symbols_is_untitled():
    assert slugify("!!!") == "untitled"


def test_slugify_length_cap():
    long_summary = "a" * 100
    result = slugify(long_summary)
    assert len(result) <= 60


def test_slugify_strips_slash_and_nul():
    result = slugify("Meeting/Room \x00 Booking")
    assert "/" not in result
    assert "\x00" not in result
    assert result == "meeting_room_booking"


def test_collision_suffix_distinct_names():
    base = PurePosixPath("/2026/09/23/0900-0930_standup.ics")
    p1 = with_collision_suffix(base, "eventid1abcdef")
    p2 = with_collision_suffix(base, "eventid2ghijkl")
    assert p1 != p2
    assert p1.name == "0900-0930_standup__eventid1.ics"
    assert p2.name == "0900-0930_standup__eventid2.ics"


def test_timed_event_near_midnight_lands_on_correct_local_date():
    # 23:45 Chicago (CDT, UTC-5) on Sept 23 is 04:45 UTC on Sept 24.
    # path_for must use the local date (23rd), not the raw UTC date (24th).
    utc = ZoneInfo("UTC")
    start = datetime(2026, 9, 24, 4, 45, tzinfo=utc)
    end = datetime(2026, 9, 24, 4, 59, tzinfo=utc)
    event = FakeEvent(event_id="abc123", summary="Late night", start=start, end=end)
    path = path_for(event, CHICAGO)
    assert path == PurePosixPath("/2026/09/23/2345-2359_late_night.ics")


def test_all_day_event_filename_prefix():
    start = datetime(2026, 9, 24, 0, 0, tzinfo=CHICAGO)
    event = FakeEvent(event_id="allday1", summary="PTO", start=start, all_day=True)
    path = path_for(event, CHICAGO)
    assert path == PurePosixPath("/2026/09/24/0000_pto.ics")


def test_filename_for_timed_with_end():
    start = datetime(2026, 9, 23, 9, 0, tzinfo=CHICAGO)
    end = datetime(2026, 9, 23, 9, 30, tzinfo=CHICAGO)
    event = FakeEvent(event_id="x", summary="Standup", start=start, end=end)
    assert filename_for(event) == "0900-0930_standup.ics"


def test_filename_for_timed_without_end():
    start = datetime(2026, 9, 23, 9, 0, tzinfo=CHICAGO)
    event = FakeEvent(event_id="x", summary="Standup", start=start, end=None)
    assert filename_for(event) == "0900_standup.ics"


def test_parse_path_accepts_event_file():
    result = parse_path("/2026/09/23/0900-0930_standup.ics")
    assert result == EventFile(year=2026, month=9, day=23, filename="0900-0930_standup.ics")


def test_parse_path_accepts_root():
    assert parse_path("/") == RootDir()


def test_parse_path_accepts_year():
    assert parse_path("/2026") == YearDir(year=2026)


def test_parse_path_accepts_month():
    assert parse_path("/2026/09") == MonthDir(year=2026, month=9)


def test_parse_path_accepts_day():
    assert parse_path("/2026/09/23") == DayDir(year=2026, month=9, day=23)


def test_parse_path_rejects_bad_root_file():
    with pytest.raises(InvalidPathError):
        parse_path("/foo")


def test_parse_path_rejects_invalid_month():
    with pytest.raises(InvalidPathError):
        parse_path("/2026/13/01/x.ics")


def test_parse_path_rejects_non_padded_month():
    with pytest.raises(InvalidPathError):
        parse_path("/2026/9/23/x.ics")


def test_parse_path_rejects_invalid_day():
    with pytest.raises(InvalidPathError):
        parse_path("/2026/02/30/x.ics")


def test_parse_path_rejects_too_deep():
    with pytest.raises(InvalidPathError):
        parse_path("/2026/09/23/x.ics/extra")


def test_parse_path_rejects_non_ics_file():
    with pytest.raises(InvalidPathError):
        parse_path("/2026/09/23/notes.txt")


def test_editor_junk_detected():
    assert is_editor_junk(".standup.ics.swp")
    assert is_editor_junk(".#standup.ics")
    assert is_editor_junk("standup.ics~")
    assert is_editor_junk("standup.ics.tmp")
    assert is_editor_junk(".goutputstream-XYZ123")


def test_editor_junk_not_flagged_for_real_file():
    assert not is_editor_junk("0900-0930_standup.ics")
