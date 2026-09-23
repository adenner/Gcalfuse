"""End-to-end tests through the real kernel FUSE path, with real editors.

Each test mounts scripts/dev_mount.py (real GcalfuseFS, fake Calendar API) in
a subprocess and drives it with ordinary file operations and real `vim`,
`sed -i` and `perl -i`. This covers what the unit tests can't: readdir via
the kernel, the exact syscall sequences editors issue, and signal handling.

Skipped automatically when FUSE isn't usable (no /dev/fuse access or no
fusermount). Deselect explicitly with `pytest -m "not fuse"`.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "dev_mount.py"
FUSERMOUNT = shutil.which("fusermount3") or shutil.which("fusermount")
TODAY = "2026-09-23"  # passed to dev_mount.py so paths are deterministic

pytestmark = [
    pytest.mark.fuse,
    pytest.mark.skipif(
        not (os.access("/dev/fuse", os.R_OK | os.W_OK) and FUSERMOUNT),
        reason="FUSE not available (/dev/fuse or fusermount missing)",
    ),
]


@dataclass
class Mount:
    root: Path
    call_log: Path
    proc: subprocess.Popen

    def calls(self) -> list[dict]:
        if not self.call_log.exists():
            return []
        return [json.loads(line) for line in self.call_log.read_text().splitlines()]

    def ops(self) -> list[str]:
        return [c["op"] for c in self.calls()]

    @property
    def today(self) -> Path:
        return self.root / "2026" / "09" / "23"

    @property
    def tomorrow(self) -> Path:
        return self.root / "2026" / "09" / "24"


def _wait_for(predicate, timeout=10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _start(tmp_path: Path, *extra: str) -> Mount:
    mountpoint = tmp_path / "cal"
    mountpoint.mkdir()
    call_log = tmp_path / "calls.jsonl"
    proc = subprocess.Popen(
        [
            sys.executable,
            str(SCRIPT),
            str(mountpoint),
            "--today",
            TODAY,
            "--call-log",
            str(call_log),
            *extra,
        ],
        stderr=subprocess.PIPE,
        text=True,
    )
    if not _wait_for(lambda: os.path.ismount(mountpoint) or proc.poll() is not None):
        proc.kill()
        pytest.fail("mount did not come up")
    if proc.poll() is not None:
        pytest.fail(f"dev_mount.py exited early:\n{proc.stderr.read()}")
    return Mount(mountpoint, call_log, proc)


def _stop(mount: Mount) -> str:
    if os.path.ismount(mount.root):
        subprocess.run([FUSERMOUNT, "-u", str(mount.root)], check=False)
    try:
        _, stderr = mount.proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        mount.proc.kill()
        _, stderr = mount.proc.communicate()
    return stderr


@pytest.fixture
def mount(tmp_path):
    m = _start(tmp_path)
    yield m
    stderr = _stop(m)
    # Any handler crash would show up as a traceback from the FUSE loop.
    assert "Traceback" not in stderr, stderr


# -- read path ---------------------------------------------------------------


def test_ls_and_cat(mount):
    assert sorted(os.listdir(mount.root)) == ["2026"]
    assert sorted(os.listdir(mount.today)) == ["0900-0930_standup.ics", "1400-1500_1_on_1.ics"]
    assert os.listdir(mount.tomorrow) == ["0000_pto.ics"]
    text = (mount.today / "0900-0930_standup.ics").read_text()
    assert text.startswith("BEGIN:VCALENDAR")
    assert "DTSTART;TZID=America/Chicago:20260923T090000" in text
    assert "DESCRIPTION:Daily sync" in text


def test_stat_size_matches_content(mount):
    path = mount.today / "0900-0930_standup.ics"
    assert path.stat().st_size == len(path.read_bytes())


def test_find_walks_the_whole_tree(mount):
    found = subprocess.run(
        ["find", str(mount.root), "-name", "*.ics"], capture_output=True, text=True, check=True
    ).stdout.split()
    assert len(found) == 4


# -- write path -------------------------------------------------------------


def test_shell_redirect_creates_event(mount):
    target = mount.today / "1600-1630_dentist.ics"
    target.write_text(
        "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nSUMMARY:Dentist\n"
        "DTSTART:20260923T160000\nDTEND:20260923T163000\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    assert mount.ops() == ["insert"]
    # Read back immediately under the name we wrote: the canonical re-render is
    # longer than what we wrote, and must not be truncated to the old size.
    text = target.read_text()
    assert "SUMMARY:Dentist" in text and "PRODID:-//gcalfuse//EN" in text
    assert text.rstrip().endswith("END:VCALENDAR")


def test_touch_creates_default_event(mount):
    # touch creates, closes, then utime()s the name it created; that name must
    # still resolve even though the event is now listed as 0900-0930_lunch.ics.
    subprocess.run(["touch", str(mount.today / "lunch.ics")], check=True)
    assert mount.ops() == ["insert"]
    assert "0900-0930_lunch.ics" in os.listdir(mount.today)
    assert "lunch.ics" not in os.listdir(mount.today)


def test_mv_to_another_day_reschedules(mount):
    subprocess.run(
        ["mv", str(mount.today / "1400-1500_1_on_1.ics"), str(mount.tomorrow)], check=True
    )
    [call] = mount.calls()
    assert call["op"] == "patch"
    assert call["body"]["start"]["dateTime"].startswith("2026-09-24T14:00:00")
    assert "1400-1500_1_on_1.ics" in os.listdir(mount.tomorrow)


def test_rm_deletes_event(mount):
    (mount.today / "0900-0930_standup.ics").unlink()
    assert mount.ops() == ["delete"]
    assert "0900-0930_standup.ics" not in os.listdir(mount.today)


def test_invalid_write_fails_and_keeps_old_content(mount):
    path = mount.today / "0900-0930_standup.ics"
    with pytest.raises(OSError) as exc_info:
        path.write_text("this is not a calendar")
    assert exc_info.value.errno == errno.EIO
    assert mount.ops() == []
    assert "SUMMARY:Standup" in path.read_text()


def test_recurring_instance_rejects_write_and_rm(mount):
    day = mount.root / "2026" / "09" / "25"
    path = day / "1100-1130_weekly_sync.ics"
    assert "SUMMARY:Weekly Sync" in path.read_text()
    with pytest.raises(PermissionError):
        path.write_text("x")
    with pytest.raises(PermissionError):
        path.unlink()
    assert mount.ops() == []


def test_mkdir_is_refused(mount):
    with pytest.raises(PermissionError):
        (mount.root / "2026" / "09" / "notes").mkdir()


def test_create_and_move_onto_a_day_with_no_events(mount):
    empty = mount.root / "2026" / "09" / "30"
    assert "30" not in os.listdir(mount.root / "2026" / "09")
    assert os.listdir(empty) == []
    subprocess.run(["mv", str(mount.today / "1400-1500_1_on_1.ics"), str(empty)], check=True)
    assert os.listdir(empty) == ["1400-1500_1_on_1.ics"]
    assert "30" in os.listdir(mount.root / "2026" / "09")
    (mount.root / "2026" / "10" / "02" / "retro.ics").write_text("")
    assert mount.ops() == ["patch", "insert"]
    assert sorted(os.listdir(mount.root / "2026")) == ["09", "10"]


# -- real editors --------------------------------------------------------------


def vim(path: Path, *commands: str, backupcopy: str = "auto") -> subprocess.CompletedProcess:
    """Run real vim non-interactively with its normal (nocompatible) save behavior.

    backupskip is cleared because its default skips backups under /tmp, which
    is where pytest's tmp_path lives; we want the real backup/rename dance.
    """
    args = ["vim", "-N", "-Es", "-u", "NONE", "-i", "NONE"]
    args += ["-c", f"set backupcopy={backupcopy} writebackup backupskip="]
    for command in commands:
        args += ["-c", command]
    return subprocess.run(
        [*args, str(path)], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20
    )


needs_vim = pytest.mark.skipif(not shutil.which("vim"), reason="vim not installed")


@needs_vim
@pytest.mark.parametrize(
    "backupcopy",
    [
        "yes",  # copy to foo~, overwrite foo in place
        "no",  # rename foo -> foo~, write a new foo
        "auto",  # probe with a "4913" file, then rename like "no"
    ],
)
def test_vim_edit_is_a_single_patch_for_every_backup_strategy(mount, backupcopy):
    path = mount.today / "0900-0930_standup.ics"
    result = vim(path, "%s/SUMMARY:Standup/SUMMARY:Team Standup/", "wq", backupcopy=backupcopy)
    assert result.returncode == 0, result.stderr
    assert mount.ops() == ["patch"], mount.calls()
    assert mount.calls()[0]["body"]["summary"] == "Team Standup"
    # Renamed to the new title; no swap, backup or probe files left behind.
    assert sorted(os.listdir(mount.today)) == ["0900-0930_team_standup.ics", "1400-1500_1_on_1.ics"]


@needs_vim
def test_vim_writing_a_new_file_is_a_single_insert(mount):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "BEGIN:VEVENT",
        "SUMMARY:Gym",
        "DTSTART:20260923T180000",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    result = vim(mount.today / "gym.ics", f"call setline(1, {lines!r})", "wq")
    assert result.returncode == 0, result.stderr
    assert mount.ops() == ["insert"], mount.calls()
    assert "1800-1830_gym.ics" in os.listdir(mount.today)


@needs_vim
@pytest.mark.parametrize("backupcopy", ["yes", "no", "auto"])
def test_vim_is_told_when_a_save_is_rejected_and_nothing_is_lost(mount, backupcopy):
    """The EIO must reach vim at close(), and vim's recovery must not delete anything."""
    path = mount.today / "0900-0930_standup.ics"
    result = vim(path, "%d", "call setline(1, 'oops')", "wq", backupcopy=backupcopy)
    assert result.returncode != 0
    assert mount.ops() == [], mount.calls()
    assert "SUMMARY:Standup" in path.read_text()
    assert sorted(os.listdir(mount.today)) == ["0900-0930_standup.ics", "1400-1500_1_on_1.ics"]


def test_sed_in_place_is_a_single_patch(mount):
    path = mount.today / "1400-1500_1_on_1.ics"
    subprocess.run(["sed", "-i", "s/LOCATION:Room 4/LOCATION:Room 5/", str(path)], check=True)
    assert mount.ops() == ["patch"], mount.calls()
    assert mount.calls()[0]["body"]["location"] == "Room 5"


@pytest.mark.skipif(not shutil.which("perl"), reason="perl not installed")
def test_perl_in_place_is_a_single_patch(mount):
    path = mount.today / "1400-1500_1_on_1.ics"
    subprocess.run(["perl", "-pi", "-e", "s/Room 4/Room 6/", str(path)], check=True)
    assert mount.ops() == ["patch"], mount.calls()


# -- lifecycle -----------------------------------------------------------------


def test_read_only_mount_rejects_writes(tmp_path):
    m = _start(tmp_path, "--read-only")
    try:
        with pytest.raises(OSError) as exc_info:
            (m.today / "new.ics").write_text("x")
        assert exc_info.value.errno == errno.EROFS
        assert "SUMMARY:Standup" in (m.today / "0900-0930_standup.ics").read_text()
    finally:
        _stop(m)
    assert m.ops() == []


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_signal_unmounts_cleanly(tmp_path, sig):
    """No stale 'Transport endpoint is not connected' mount after systemctl stop."""
    m = _start(tmp_path)
    m.proc.send_signal(sig)
    assert m.proc.wait(timeout=10) == 0
    assert not os.path.ismount(m.root)
    assert os.listdir(m.root) == []
