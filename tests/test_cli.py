"""cli.py: argument parsing, clean error exits, mountpoint safety, umount."""

import errno
import subprocess
from pathlib import Path

import pytest

from gcalfuse import cli
from gcalfuse.api import CalendarApiError
from gcalfuse.cli import MountError, _check_mountpoint, _is_gcalfuse_mount, main


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """Point the fixed ~/.config/gcalfuse paths at an empty temp dir."""
    monkeypatch.setattr("gcalfuse.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("gcalfuse.config.DEFAULT_CONFIG_PATH", tmp_path / "config.toml")
    return tmp_path


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
    assert "mount" in capsys.readouterr().out


def test_subcommand_is_required():
    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code == 2


def test_mount_without_token_exits_1_with_hint(config_dir, capsys):
    assert main(["mount", str(config_dir / "mnt")]) == 1
    assert "gcalfuse auth" in capsys.readouterr().err


def test_auth_without_credentials_json_explains_setup(config_dir, capsys):
    assert main(["auth"]) == 1
    err = capsys.readouterr().err
    assert "credentials.json" in err and "Desktop app" in err


def test_bad_config_exits_1_with_message(config_dir, capsys):
    (config_dir / "config.toml").write_text('timezone = "Nowhere/Land"\n')
    assert main(["ls-days"]) == 1
    assert "timezone" in capsys.readouterr().err


def test_explicit_config_flag_is_used(tmp_path, config_dir, capsys):
    other = tmp_path / "other.toml"
    other.write_text("poll_seconds = -5\n")
    assert main(["--config", str(other), "ls-days"]) == 1
    assert "other.toml" in capsys.readouterr().err


def test_mount_reports_unreachable_google_cleanly(config_dir, monkeypatch, capsys):
    class BrokenCache:
        def refresh_full(self):
            raise CalendarApiError("Calendar API error: timed out")

    monkeypatch.setattr(cli, "_build_cache", lambda config: (BrokenCache(), None))
    assert main(["mount", str(config_dir / "mnt")]) == 1
    assert "Could not fetch events" in capsys.readouterr().err


# -- mountpoint safety -------------------------------------------------------------


def test_check_mountpoint_accepts_missing_or_empty_dir(tmp_path):
    _check_mountpoint(tmp_path / "does-not-exist")
    _check_mountpoint(tmp_path)


def test_check_mountpoint_refuses_non_empty_dir(tmp_path):
    (tmp_path / "keep.txt").write_text("data")
    with pytest.raises(MountError, match="not empty"):
        _check_mountpoint(tmp_path)


def test_check_mountpoint_refuses_a_file(tmp_path):
    target = tmp_path / "file"
    target.write_text("x")
    with pytest.raises(MountError, match="not a directory"):
        _check_mountpoint(target)


def test_check_mountpoint_explains_stale_fuse_mount(tmp_path, monkeypatch):
    def not_connected(self, *args, **kwargs):
        raise OSError(errno.ENOTCONN, "Transport endpoint is not connected")

    monkeypatch.setattr(Path, "exists", not_connected)
    with pytest.raises(MountError, match="stale FUSE mount.*gcalfuse umount"):
        _check_mountpoint(tmp_path)


def test_is_gcalfuse_mount_reads_proc_mounts_format(tmp_path):
    mounts = tmp_path / "mounts"
    target = tmp_path / "My Cal"
    mounts.write_text(
        "proc /proc proc rw 0 0\n"
        f"gcalfuse {str(target).replace(' ', chr(92) + '040')} fuse rw,nosuid 0 0\n"
    )
    assert _is_gcalfuse_mount(target, mounts)
    assert not _is_gcalfuse_mount(tmp_path / "Other", mounts)
    assert not _is_gcalfuse_mount(target, tmp_path / "missing")


# -- umount ----------------------------------------------------------------------


def fake_run(returncode=0, stderr=""):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    return run, calls


@pytest.mark.parametrize(
    ("installed", "expected"),
    [({"fusermount3", "fusermount"}, "fusermount3"), ({"fusermount"}, "fusermount")],
)
def test_umount_prefers_fusermount3(config_dir, monkeypatch, installed, expected):
    run, calls = fake_run()
    monkeypatch.setattr(cli.shutil, "which", lambda tool: tool if tool in installed else None)
    monkeypatch.setattr(cli.subprocess, "run", run)
    assert main(["umount", "/tmp/mnt"]) == 0
    assert calls == [[expected, "-u", "/tmp/mnt"]]


def test_umount_without_any_fusermount(config_dir, monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda tool: None)
    assert main(["umount", "/tmp/mnt"]) == 1
    assert "fuse3" in capsys.readouterr().err


def test_umount_failure_is_reported_not_raised(config_dir, monkeypatch, capsys):
    run, _ = fake_run(returncode=1, stderr="fusermount3: entry for /tmp/mnt not found")
    monkeypatch.setattr(cli.shutil, "which", lambda tool: tool)
    monkeypatch.setattr(cli.subprocess, "run", run)
    assert main(["umount", "/tmp/mnt"]) == 1
    assert "not found" in capsys.readouterr().err
