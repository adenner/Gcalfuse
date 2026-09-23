#!/usr/bin/env python3
"""Mount gcalfuse against an in-memory fake calendar — no Google account needed.

Uses the real GcalfuseFS and kernel FUSE; only the Calendar API is faked (see
tests/fakes.py). Handy for trying editors against the mount, and used by
tests/test_integration_mount.py.

    python scripts/dev_mount.py /tmp/cal            # Ctrl-C or fusermount3 -u to stop
    python scripts/dev_mount.py /tmp/cal -v --call-log /tmp/calls.jsonl

--call-log appends one JSON line per successful insert/patch/delete, so you
can see exactly which Google calls a sequence of file operations would make.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

import pyfuse3  # noqa: E402
import trio  # noqa: E402

from gcalfuse.cache import CalendarCache  # noqa: E402
from gcalfuse.cli import _serve_until_unmounted_or_signalled  # noqa: E402
from gcalfuse.fs import GcalfuseFS  # noqa: E402
from tests.fakes import FakeCalendarClient, sample_events  # noqa: E402


class LoggingFakeClient(FakeCalendarClient):
    """FakeCalendarClient that appends each successful mutation to a JSONL file."""

    def __init__(self, tz, records, log_path: Path | None) -> None:
        super().__init__(tz, records)
        self._log_path = log_path

    def _log(self, **entry) -> None:
        if self._log_path is not None:
            with open(self._log_path, "a") as fh:
                fh.write(json.dumps(entry) + "\n")

    def insert(self, body):
        record = super().insert(body)
        self._log(op="insert", event_id=record.event_id, body=body)
        return record

    def patch(self, event_id, body):
        record = super().patch(event_id, body)
        self._log(op="patch", event_id=event_id, body=body)
        return record

    def delete(self, event_id):
        super().delete(event_id)
        self._log(op="delete", event_id=event_id)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mountpoint", type=Path)
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--call-log", type=Path, default=None)
    parser.add_argument("--timezone", default="America/Chicago")
    parser.add_argument("--today", type=date.fromisoformat, default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    tz = ZoneInfo(args.timezone)
    today = args.today or datetime.now(tz).date()
    client = LoggingFakeClient(tz, sample_events(tz, today), args.call_log)
    cache = CalendarCache(
        client, tz, window_past_days=3650, window_future_days=3650, poll_seconds=3600
    )
    cache.refresh_full()

    fs = GcalfuseFS(cache.index, client, read_only=args.read_only)
    options = set(pyfuse3.default_options) | {"fsname=gcalfuse-dev"}
    if args.read_only:
        options.add("ro")
    pyfuse3.init(fs, str(args.mountpoint), options)
    try:
        trio.run(_serve_until_unmounted_or_signalled, pyfuse3, trio)
    except KeyboardInterrupt:
        pass
    finally:
        pyfuse3.close(unmount=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
