# gcalfuse

Mount your Google Calendar as a folder of `.ics` files, one file per event,
arranged by date:

    ~/Cal/
      2026/
        09/
          23/
            0900-0930_standup.ics
            1400-1500_1_on_1.ics
          24/
            0000_pto.ics

`cat` a file to see the event, edit it to change the event, `mv` it to
another day to reschedule, `rm` it to delete it. Changes go to Google
Calendar when you save (close) the file.

Linux only. One calendar at a time.

- [Quick start](#quick-start)
- [Google Cloud setup](#google-cloud-setup-one-time)
- [Everyday use](#everyday-use)
- [How files map to events](#how-files-map-to-events)
- [Editing rules and limitations](#editing-rules-and-limitations)
- [Editors](#editors)
- [Configuration](#configuration)
- [Command reference](#command-reference)
- [Troubleshooting](#troubleshooting)
- [What this is not](#what-this-is-not)
- [Development](#development)

## Quick start

    sudo apt install fuse3 libfuse3-dev        # Debian/Ubuntu; needed by pyfuse3
    python3.12 -m venv .venv && source .venv/bin/activate
    pip install -e .

    # one-time: create ~/.config/gcalfuse/credentials.json (next section)
    gcalfuse auth
    gcalfuse mount ~/Cal                        # runs in the foreground; Ctrl-C to stop
    ls ~/Cal/$(date +%Y/%m/%d)                  # in another terminal

Use `gcalfuse mount --read-only ~/Cal` if you only want to look.

## Google Cloud setup (one-time)

gcalfuse talks to Google with your own OAuth client, so you need to create
one:

1. Open the [Google Cloud console](https://console.cloud.google.com/) and
   create (or pick) a project.
2. **APIs & Services → Library**: enable the **Google Calendar API**.
3. **APIs & Services → OAuth consent screen**: choose *External*, fill in the
   app name and your email, and add yourself as a **test user**.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**,
   application type **Desktop app**.
5. Download the JSON and save it as `~/.config/gcalfuse/credentials.json`.
6. Run `gcalfuse auth`. A browser opens; approve access. (On a machine
   without a browser, use `gcalfuse auth --no-browser` and open the printed
   URL on any machine; the redirect goes to a local port, so over SSH forward
   that port or run `auth` locally and copy `token.json` over.)

gcalfuse asks only for the `calendar.events` scope: it can read and change
events, not calendar sharing settings. The token is saved to
`~/.config/gcalfuse/token.json`, readable only by you (mode 600).

## Everyday use

    gcalfuse mount ~/Cal
    ls ~/Cal/2026/09/23
    cat ~/Cal/2026/09/23/0900-0930_standup.ics

**Create** an event by writing a file into a day folder:

    cat > ~/Cal/2026/09/24/dentist.ics <<'EOF'
    BEGIN:VCALENDAR
    VERSION:2.0
    BEGIN:VEVENT
    SUMMARY:Dentist
    DTSTART:20260924T150000
    DTEND:20260924T153000
    END:VEVENT
    END:VCALENDAR
    EOF

Times without a timezone (like above) are in your configured `timezone`.
After saving, the file is listed under its canonical name,
`1500-1530_dentist.ics`.

The quickest way: `touch ~/Cal/2026/09/24/team_lunch.ics` creates a 09:00–09:30
event titled "team lunch" that you can then edit.

**Edit** a file in any editor and save it; the event is updated.

**Reschedule** by moving the file to another day. The local clock time is
kept:

    mv ~/Cal/2026/09/24/1500-1530_dentist.ics ~/Cal/2026/09/25/

**Retitle** by renaming within the day (`mv 1500-1530_dentist.ics
1500-1530_orthodontist.ics`), or by editing `SUMMARY`.

**Delete** with `rm`.

**Unmount** with Ctrl-C in the mounting terminal, `gcalfuse umount ~/Cal`,
or by stopping the process (SIGTERM/SIGHUP also unmount cleanly).

## How files map to events

| Event | Filename |
|---|---|
| Timed, 09:00–09:30 | `0900-0930_<title>.ics` |
| All-day | `0000_<title>.ics` |
| Two events with the same time and title | `0900-0930_<title>__<first 8 chars of event id>.ics` |

- `<title>` is the event title lowercased, accents removed, every other
  non-letter/digit replaced by `_`, capped at 60 characters. An empty or
  non-Latin title becomes `untitled`.
- An event is filed under its **start date in your configured timezone**. A
  multi-day event appears only on its first day.
- Only days, months and years that contain events are *listed*; every valid
  date path can still be opened, so you can add events to empty days.
- The window of events loaded is `window_past_days` before today to
  `window_future_days` after it (30 and 90 by default), refreshed every
  `poll_seconds`. Changes made in Google Calendar show up within that
  interval.
- Each file is a standard iCalendar file with one `VEVENT`: `SUMMARY`,
  `DTSTART`/`DTEND`, `DESCRIPTION`, `LOCATION`, `STATUS`, `ORGANIZER`,
  `ATTENDEE`, plus `X-GOOGLE-EVENT-ID` and `X-GOOGLE-HTML-LINK`.
- Recurring events appear as one file per occurrence.

## Editing rules and limitations

What a save does:

- **The file is the event.** Saving sends `SUMMARY`, `DESCRIPTION`,
  `LOCATION`, `DTSTART` and `DTEND` (or `DURATION`) to Google. Deleting a
  `DESCRIPTION` or `LOCATION` line clears it. Other properties
  (attendees, status, ...) are shown but not sent back.
- **One save, one API call.** Nothing is sent while you type or on each
  `write()`; the change goes out once, when the file is closed. Saving
  without changing anything sends nothing.
- **Bad content never reaches Google.** If a save isn't valid iCalendar
  with exactly one `VEVENT`, or an edited event has no `DTSTART`, the save
  fails with an I/O error (your editor will say the write failed) and the
  event is unchanged.
- **`DTSTART` must be on the folder's date** (in your timezone), otherwise
  the save fails with "Invalid argument". To move an event to another day,
  move the file.
- **New files get sensible defaults:** no `DTSTART` → 09:00 on the folder's
  date; no `DTEND` → 30 minutes (1 day for all-day events); no `SUMMARY` →
  taken from the filename.
- **The time in a filename is output, not input.** It is derived from
  `DTSTART`/`DTEND`. Renaming `0900-0930_x.ics` to `1000-1030_x.ics` fails
  with "Invalid argument"; edit `DTSTART` instead.
- **`mv` won't overwrite another event.** Renaming onto an existing event's
  file fails with "File exists"; `rm` the other one first if that's what
  you mean.

Recurring events (v1 limitation):

- Occurrences of a recurring event are **read-only**. Editing, deleting or
  renaming one fails with "Operation not permitted" and nothing is sent to
  Google (the log says `v1 does not edit recurring instances`). Change
  recurring events in Google Calendar itself.
- An `RRULE` in a file you create is ignored with a warning: you get a
  single, non-recurring event.

Other limitations:

- **Empty days aren't listed, but you can still use them.** `ls` shows only
  years, months and days that have events, yet any valid date path works:
  `cat > ~/Cal/2026/10/02/retro.ics` or `mv ... ~/Cal/2026/10/02/` on a day
  with nothing on it yet. `mkdir` is refused (there's nothing to create).
  Events you create outside the loaded window disappear from the mount at
  the next refresh; they're still in Google.
- After a save that changes the title or time, the file moves to its new
  canonical name. The old name keeps working for about a minute (so editors
  don't complain) but isn't listed.
- Files whose names aren't `.ics` (like `notes.txt`) can be created in a day
  folder but are only kept in memory; they're discarded at unmount with a
  warning in the log. They exist so tools that save via a temporary file
  work.
- `chmod`/`chown` succeed but do nothing. Local permissions don't affect who
  can see events in Google.
- Truncating a file by path without opening it (`truncate -s 0 file.ics`)
  is refused; open-and-overwrite (`>`) works.

## Editors

Most editors don't simply overwrite a file: they write a swap/backup/temp
file and rename things around. gcalfuse keeps those scratch files in memory
and never sends them to Google; the event is only updated when the real
`.ics` name is saved.

Tested against a real mount (see `tests/test_integration_mount.py`):

- **vim**, with each `backupcopy` strategy (`yes`: copy then overwrite;
  `no`/`auto`: rename the original to `name~` and write a new file): one
  update per changed `:w`, nothing sent for an unchanged `:w`, and a rejected
  save is reported by vim with the event left intact.
- **`sed -i`** and **`perl -i`** (write a temp file, rename it over the
  original): one update.
- Shell redirects (`>`), `cp`, `touch`, `mv`, `rm`, `find`.

Recognized scratch-file names, so the patterns these editors use should also
work, though they aren't covered by automated tests: vim swap files
(`.name.swp`, `.swo`, ...), `name~` backups, emacs lock/auto-save files
(`.#name`, `#name#`), `*.tmp`, GNOME/GIO `.goutputstream-*`, kate
`*.kate-swp`, JetBrains `*___jb_tmp___`/`*___jb_old___`. Any other temp name
also works as long as the editor renames it onto the `.ics` name when done.

If your editor reports that a save failed, that's gcalfuse rejecting the
content (see [Editing rules](#editing-rules-and-limitations)); run the mount
with `-v` to see why.

## Configuration

`~/.config/gcalfuse/config.toml` (or pass `--config PATH`). Every key is
optional; these are the defaults:

```toml
calendar_id = "primary"           # or a calendar's ID from its Google settings page
mountpoint = "~/Cal"              # used when `mount`/`umount` get no path
timezone = "America/Chicago"      # IANA name; decides folders, filename times, floating times
window_past_days = 30             # how far back to load events
window_future_days = 90           # how far ahead
poll_seconds = 60                 # how often to pull changes from Google
read_only = false                 # true: same as always passing --read-only
filename_style = "time_title"     # the only style in v1
```

Invalid values (an unknown timezone, `poll_seconds = 0`, a string where a
number belongs...) stop gcalfuse with a message naming the key. Unknown keys
are ignored with a warning.

Fixed locations (not configurable):

| File | Purpose |
|---|---|
| `~/.config/gcalfuse/credentials.json` | Your OAuth client (you download it) |
| `~/.config/gcalfuse/token.json` | Saved login, written by `gcalfuse auth` (mode 600) |

## Command reference

    gcalfuse [-v] [-c CONFIG] auth [--no-browser]
    gcalfuse [-v] [-c CONFIG] mount [MOUNTPOINT] [--read-only]
    gcalfuse [-v] [-c CONFIG] umount [MOUNTPOINT]
    gcalfuse [-v] [-c CONFIG] ls-days

- `-v` / `--verbose` (before the subcommand): debug logging, including one
  line per filesystem operation.
- `mount` runs in the foreground until unmounted. It refuses to mount over a
  non-empty directory.
- `ls-days` fetches the window and prints each day and filename without
  mounting, useful for checking auth and config.
- Exit status is 1, with a one-line explanation, for auth, config and mount
  problems.

### Running at login (systemd user service)

    # ~/.config/systemd/user/gcalfuse.service
    [Unit]
    Description=Google Calendar as files
    After=network-online.target

    [Service]
    ExecStart=%h/path/to/.venv/bin/gcalfuse mount %h/Cal
    Restart=on-failure

    [Install]
    WantedBy=default.target

    systemctl --user enable --now gcalfuse

`systemctl --user stop gcalfuse` unmounts cleanly.

## Logging

Logs go to stderr (the journal, under systemd):

- **INFO**: mount/unmount, refresh counts, and every insert/patch/delete
  with its event id.
- **WARNING**: rejected saves and why, rejected recurring edits, API retries,
  ignored `RRULE`s, scratch files discarded at unmount.
- **ERROR**: failed API calls, auth errors while serving the last good copy.
- **DEBUG** (`-v`): every filesystem operation.

## Troubleshooting

**"Transport endpoint is not connected"**: a previous gcalfuse was killed
without unmounting. Run `gcalfuse umount ~/Cal` (or `fusermount3 -u ~/Cal`),
then mount again. `gcalfuse mount` detects this and tells you.

**"is not empty. Refusing to mount"**: the mountpoint has files in it. Pick
an empty directory; gcalfuse won't hide your files behind a mount.

**"No saved token" / "revoked or expired"**: run `gcalfuse auth` again.
Tokens for apps in *Testing* mode on the OAuth consent screen expire after 7
days; publish the app (it stays private to you) to avoid that.

**Log says "Calendar API auth error (401/403)"**: the token was revoked or
lacks the Calendar scope. The mount keeps serving what it last fetched; run
`gcalfuse auth` and remount.

**A save fails with "Input/output error"**: the file wasn't valid iCalendar,
or Google rejected the change (the log has the reason). The event is
unchanged; `cat` the file to see its current content.

**A save fails with "Invalid argument"**: `DTSTART` isn't on the folder's
date, or you renamed a file to change its time.

**A day I expect isn't in `ls`**: only days with events are listed. You can
still `cd` into it or write a file there; see
[limitations](#editing-rules-and-limitations).

**Changes made in Google don't show up**: they appear within `poll_seconds`
(60 s by default). Events outside the window aren't loaded at all.

**Permission denied mounting**: your user needs access to `/dev/fuse`. Most
desktop distributions allow this by default; otherwise add yourself to the
`fuse` group or check your distribution's FUSE setup.

## What this is not

- Not a file store: only calendar events live here, not arbitrary files.
- Not a CalDAV server, not multi-user, not multi-calendar (v1 mounts one
  `calendar_id`).
- Not a recurring-event editor (v1).
- Not offline-capable: the mount shows the last fetched events if Google is
  unreachable, but saves fail until it's back.
- Not Windows or macOS.

## Development

See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for the architecture, the
write-path state machine, how the tests are structured, and how to try the
filesystem without a Google account:

    pip install -e '.[dev]'
    pytest                       # unit + real-FUSE integration tests (~10 s)
    ruff check . && ruff format --check .
    python scripts/dev_mount.py /tmp/cal -v     # mount a fake calendar
