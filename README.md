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
- **Phase 3 (done):** writes (create, edit, delete, rename) for non-recurring events.
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

## Auth and mount

    gcalfuse auth
    gcalfuse mount ~/Cal          # or: gcalfuse mount --read-only ~/Cal
    ls ~/Cal/$(date +%Y/%m/%d)
    cat ~/Cal/$(date +%Y/%m/%d)/*.ics
    fusermount -u ~/Cal

`gcalfuse ls-days` prints the cached dates and filenames without mounting,
useful for debugging what's in the window.

## Writes

Creating, editing, deleting, and rescheduling non-recurring events works:

    cat > ~/Cal/2026/09/24/1500-1530_dentist.ics <<'EOF'
    BEGIN:VCALENDAR
    VERSION:2.0
    BEGIN:VEVENT
    SUMMARY:Dentist
    DTSTART:20260924T150000
    DTEND:20260924T153000
    END:VEVENT
    END:VCALENDAR
    EOF

    mv ~/Cal/2026/09/24/1500-1530_dentist.ics ~/Cal/2026/09/25/
    rm ~/Cal/2026/09/25/1500-1530_dentist.ics

Notes and v1 limitations:

- **A day only exists as a directory once it has at least one event.**
  There's no `mkdir` (returns `EPERM`) — dates exist only because events
  exist. This means `mv`/`cat >` into a day with *zero* current events
  will fail at the shell/kernel level (the parent directory itself won't
  resolve). Reschedule onto a day that already has something on it, or
  onto today/tomorrow if your window includes an anchor event.
- **Recurring event instances are read-only.** Opening one for write,
  unlinking it, or renaming it returns `EPERM` and makes no Google API
  call at all; `v1 does not edit recurring instances` is logged.
- **The commit happens on close (`release()`), once per write session** —
  not on every `write()` call, and not on `flush()` (some editors call
  `flush()` more than once per session via `fsync`; committing only on
  `release()` keeps this to exactly one API call).
- **Invalid ICS on close never touches Google.** If the content doesn't
  parse into exactly one `VEVENT`, or an existing file's edit is missing
  `DTSTART`, the write fails with `EIO` and the previous Google state is
  untouched. A brand-new file with no `DTSTART` is *not* an error —
  it defaults to 09:00–09:30 local time on its folder's date, with
  `SUMMARY` taken from the filename if the ICS didn't set one.
- **`DTSTART` must land on the folder's date** (in the configured
  timezone) or the write is rejected with `EINVAL` and no Google call is
  made. Move the event by renaming it into the correct day folder instead.
- **Editor temp files are supported.** `vim`-style write-to-swapfile-then-
  rename (`.foo.ics.swp`, `foo.ics.tmp`, etc.) is buffered in memory and
  only actually committed to Google at the `rename()` that lands it on a
  real, non-junk `/YYYY/MM/DD/<slug>.ics` name. Deleting a temp file
  before it's ever renamed onto a real name is a pure no-op locally —
  nothing was ever sent to Google.
- Renaming within the same day changes only the title (`SUMMARY`, derived
  from the new filename); renaming to a different day reschedules the
  event, keeping its local clock time.
