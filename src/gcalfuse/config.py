"""Configuration loading for gcalfuse."""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

CONFIG_DIR = Path("~/.config/gcalfuse").expanduser()
DEFAULT_CONFIG_PATH = CONFIG_DIR / "config.toml"

# key -> (expected type, validator returning an error message or None)
_FIELDS: dict[str, tuple[type, object]] = {
    "calendar_id": (str, lambda v: None if v else "must not be empty"),
    "mountpoint": (str, lambda v: None if v else "must not be empty"),
    "timezone": (str, lambda v: _check_timezone(v)),
    "window_past_days": (int, lambda v: None if v >= 0 else "must be >= 0"),
    "window_future_days": (int, lambda v: None if v >= 0 else "must be >= 0"),
    "poll_seconds": (int, lambda v: None if v > 0 else "must be > 0"),
    "read_only": (bool, lambda v: None),
    "filename_style": (
        str,
        lambda v: None if v == "time_title" else 'only "time_title" is supported',
    ),
}


class ConfigError(ValueError):
    """The config file is unreadable or has an invalid value."""


def _check_timezone(name: str) -> str | None:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return f"unknown IANA timezone {name!r} (e.g. 'America/Chicago')"
    return None


@dataclass
class Config:
    calendar_id: str = "primary"
    mountpoint: Path = Path("~/Cal").expanduser()
    timezone: str = "America/Chicago"
    window_past_days: int = 30
    window_future_days: int = 90
    poll_seconds: int = 60
    read_only: bool = False
    filename_style: str = "time_title"

    @property
    def token_path(self) -> Path:
        return CONFIG_DIR / "token.json"

    @property
    def credentials_path(self) -> Path:
        return CONFIG_DIR / "credentials.json"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        """Load and validate config from TOML; missing file or keys use defaults.

        Raises ConfigError with a message naming the bad key.
        """
        toml_path = Path(path).expanduser() if path is not None else DEFAULT_CONFIG_PATH
        data: dict = {}
        if toml_path.exists():
            try:
                with open(toml_path, "rb") as fh:
                    data = tomllib.load(fh)
            except (tomllib.TOMLDecodeError, OSError) as exc:
                raise ConfigError(f"{toml_path}: {exc}") from exc

        kwargs: dict = {}
        for key, value in data.items():
            if key not in _FIELDS:
                logger.warning("%s: ignoring unknown key %r", toml_path, key)
                continue
            expected, validate = _FIELDS[key]
            # bool is a subclass of int; don't accept `poll_seconds = true`.
            if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
                raise ConfigError(
                    f"{toml_path}: {key} must be a {expected.__name__}, got {value!r}"
                )
            problem = validate(value)
            if problem:
                raise ConfigError(f"{toml_path}: {key} {problem}")
            kwargs[key] = Path(value).expanduser() if key == "mountpoint" else value
        return cls(**kwargs)
