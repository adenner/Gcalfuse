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
- **Phase 4 (done):** hardening and full docs.

## What this is not

- Not a blob-storage filesystem (calendar events stay small, structured
  meetings, not arbitrary files).
- Not a CalDAV server, not multi-user, not multi-calendar in v1.
- No Google Meet creation UI, no encryption of local data, no TUI/GUI.
- Recurring event **instances** are read-only in v1: creating/editing/
  deleting a single occurrence of a recurring series is rejected. There's
  no THISANDFOLLOWING or exception-event support.
- Linux only (uses `pyfuse3`/`libfuse3`); no Windows/macOS support.

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

## Config file reference

Default path: `~/.config/gcalfuse/config.toml`. Every key is optional; shown
values are the defaults.

```toml
calendar_id = "primary"           # one calendar in v1
mountpoint = "~/Cal"
timezone = "America/Chicago"      # IANA zone; folders/filenames use this
window_past_days = 30             # cache window: how far back
window_future_days = 90           # cache window: how far ahead
poll_seconds = 60                 # background refresh interval
read_only = false
filename_style = "time_title"     # only style implemented in v1
```

Related fixed paths (not configurable, per the OAuth setup below):

- OAuth client secret: `~/.config/gcalfuse/credentials.json` (you provide this)
- OAuth token: `~/.config/gcalfuse/token.json` (written by `gcalfuse auth`,
  and kept `chmod 600`)

## Auth, mount, unmount

    gcalfuse auth
    gcalfuse mount ~/Cal          # or: gcalfuse mount --read-only ~/Cal
    ls ~/Cal/$(date +%Y/%m/%d)
    cat ~/Cal/$(date +%Y/%m/%d)/*.ics
    gcalfuse umount ~/Cal         # or: fusermount -u ~/Cal

`gcalfuse ls-days` prints the cached dates and filenames without mounting,
useful for debugging what's in the window.

Pass `-v`/`--verbose` before the subcommand (e.g. `gcalfuse -v mount ~/Cal`)
for debug-level logging, including a line for every FUSE operation.

`gcalfuse mount` refuses to start (best effort) if the mountpoint already
has files in it and isn't already a gcalfuse mount — this is a safety net
against mounting over a directory you actually use for something else.

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

## Editor quirks

Most editors don't write files the naive way (`open`, `write`, `close`).
`paths.py`'s `is_editor_junk()` recognizes the common temp-file patterns
below and `fs.py` buffers them without ever calling the Google API, so
these all work:

- **vim**: writes a swap file (`.foo.ics.swp`) while editing, then on save
  either writes-in-place or writes a new file and renames it over the
  original. Both paths are handled; the swap file itself never commits.
- **VS Code**: writes to a temp file in the same directory (matching
  `*.tmp`) and renames it over the target on save.
- **gedit / GNOME text editor**: uses GIO's atomic-save temp files, named
  `.goutputstream-XXXXXX`.
- **emacs**: lock files named `.#foo.ics` are ignored the same way.

If your editor of choice uses a pattern not in this list, saves may
silently create a permanently-pending, never-committed file (harmless,
but the edit won't reach Google) — `paths.is_editor_junk()` is the one
place to extend.

## Permissions and fusermount

- `gcalfuse mount` and `gcalfuse umount` shell out to `fusermount -u` to
  unmount; there's no need for `sudo` as long as your user is allowed to
  use FUSE (true by default on most desktop Linux distros; some minimal/
  server distros require `user_allow_other` in `/etc/fuse.conf` or being
  in a `fuse` group).
- `chmod`/`chown` on files under the mount succeed as no-ops rather than
  actually changing anything — this exists purely so editors that
  defensively `chmod` a file before writing don't fail outright. Real
  Google Calendar ACLs are unaffected by local file permissions.
- The saved OAuth token (`~/.config/gcalfuse/token.json`) is written
  `chmod 600` (owner read/write only) since it's a live credential.

## Logging

Logs go to stderr with a timestamp and level. Roughly:

- `INFO`: mount/unmount, cache refresh counts, and every insert/patch/
  delete with its Google event id.
- `WARNING`: a rejected recurring-instance write/unlink, an ICS parse
  failure on close, a Calendar API auth error (401/403) while serving a
  stale cache.
- `DEBUG` (behind `-v`): every FUSE operation (`getattr`, `lookup`,
  `readdir`, `open`, `read`, `create`, `write`, `unlink`, `rename`,
  `release`) with the path involved.

## Robustness notes

- Calendar API calls retry with exponential backoff on `429` (rate
  limit) and `5xx` responses, up to 5 attempts.
- If a background cache refresh fails for any reason (network blip, an
  auth error, a rate limit that exhausted retries), the filesystem keeps
  serving the last-known-good cache rather than going empty or crashing.
- Event summaries are always slugified before becoming part of a
  filename, so a summary containing `/` or other path-hostile characters
  can never produce an invalid or escaping filename.
