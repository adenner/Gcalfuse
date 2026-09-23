"""Configuration loading for gcalfuse."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_DIR = Path("~/.config/gcalfuse").expanduser()
DEFAULT_CONFIG_PATH = CONFIG_DIR / "config.toml"

_FIELD_KEYS = (
    "calendar_id",
    "timezone",
    "window_past_days",
    "window_future_days",
    "poll_seconds",
    "read_only",
    "filename_style",
)


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

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        """Load config from a TOML file, falling back to defaults for missing keys."""
        toml_path = Path(path).expanduser() if path is not None else DEFAULT_CONFIG_PATH
        data: dict = {}
        if toml_path.exists():
            with open(toml_path, "rb") as fh:
                data = tomllib.load(fh)

        kwargs = {key: data[key] for key in _FIELD_KEYS if key in data}
        if "mountpoint" in data:
            kwargs["mountpoint"] = Path(data["mountpoint"]).expanduser()
        return cls(**kwargs)
