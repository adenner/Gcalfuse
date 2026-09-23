"""config.py: defaults, loading, and validation."""

import logging
from pathlib import Path

import pytest

from gcalfuse.config import Config, ConfigError


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_missing_file_gives_defaults(tmp_path):
    config = Config.load(tmp_path / "nope.toml")
    assert config == Config()
    assert config.calendar_id == "primary"
    assert config.poll_seconds == 60
    assert config.mountpoint == Path("~/Cal").expanduser()


def test_values_are_loaded_and_mountpoint_tilde_expanded(tmp_path):
    config = Config.load(
        write(
            tmp_path,
            'calendar_id = "work@example.com"\n'
            'mountpoint = "~/WorkCal"\n'
            'timezone = "Europe/Berlin"\n'
            "window_past_days = 7\n"
            "window_future_days = 14\n"
            "poll_seconds = 120\n"
            "read_only = true\n",
        )
    )
    assert config.calendar_id == "work@example.com"
    assert config.mountpoint == Path("~/WorkCal").expanduser()
    assert config.tz.key == "Europe/Berlin"
    assert (config.window_past_days, config.window_future_days) == (7, 14)
    assert config.poll_seconds == 120 and config.read_only is True


def test_token_and_credentials_paths_are_fixed(tmp_path):
    config = Config.load(write(tmp_path, ""))
    assert config.token_path.name == "token.json"
    assert config.credentials_path.name == "credentials.json"
    assert config.token_path.parent == config.credentials_path.parent


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('timezone = "Mars/Olympus"', "unknown IANA timezone"),
        ('poll_seconds = "60"', "poll_seconds must be a int"),
        ("poll_seconds = 0", "poll_seconds must be > 0"),
        ("poll_seconds = true", "poll_seconds must be a int"),
        ("window_past_days = -1", "must be >= 0"),
        ('read_only = "yes"', "read_only must be a bool"),
        ('calendar_id = ""', "calendar_id must not be empty"),
        ('filename_style = "title_first"', 'only "time_title"'),
        ("this is not toml", "config.toml"),
    ],
)
def test_invalid_values_raise_config_error_naming_the_problem(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        Config.load(write(tmp_path, text))


def test_unknown_key_is_ignored_with_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        config = Config.load(write(tmp_path, 'calender_id = "typo"\n'))
    assert config.calendar_id == "primary"
    assert "calender_id" in caplog.text
