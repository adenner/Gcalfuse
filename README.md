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
- **Phase 2 (done):** auth, cache refresh, read-only FUSE mount.
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

This needs `libfuse3` at runtime (Debian/Ubuntu: `apt install fuse3
libfuse3-dev`) since `pyfuse3` is a compiled extension against it.

## Run tests

    pytest
    ruff check .

## Google Cloud setup (one-time)

1. Create or pick a Google Cloud project.
2. Enable the **Google Calendar API** for it.
3. Create an OAuth client ID of type **Desktop app**.
4. Download its JSON and save it to `~/.config/gcalfuse/credentials.json`.

## Auth and mount (read-only so far)

    gcalfuse auth
    gcalfuse mount --read-only ~/Cal
    ls ~/Cal/$(date +%Y/%m/%d)
    cat ~/Cal/$(date +%Y/%m/%d)/*.ics
    fusermount -u ~/Cal

`gcalfuse ls-days` prints the cached dates and filenames without mounting,
useful for debugging what's in the window.

Writes (`create`, edit, `rm`, `mv`) are not implemented yet — every mutating
FUSE call currently returns `EROFS`/`EPERM`. That's Phase 3.
