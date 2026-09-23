# gcalfuse

A FUSE filesystem that exposes Google Calendar events as `.ics` files in a
`YYYY/MM/DD/` tree.

    ~/Cal/
      2026/
        09/
          23/
            0900-0930_standup.ics
            1400-1500_1-on-1.ics
          24/
            0000_all-day_pto.ics

This project is under active phased development. See the phase notes below
for what currently works.

## Status

- **Phase 1 (done):** pure path/ICS/cache logic, no FUSE, no network.
- **Phase 2:** auth, cache refresh, read-only FUSE mount.
- **Phase 3:** writes (create, edit, delete, rename) for non-recurring events.
- **Phase 4:** hardening and full docs.

## What this is not

- Not a blob-storage filesystem (calendar events stay small, structured
  meetings, not arbitrary files).
- Not a CalDAV server, not multi-user, not multi-calendar in v1.
- Recurring event **instances** are read-only in v1: creating/editing/
  deleting a single occurrence of a recurring series is rejected.

## Install (development)

    python3.12 -m venv .venv
    source .venv/bin/activate
    pip install -e '.[dev]'

## Run tests

    pytest
    ruff check .
