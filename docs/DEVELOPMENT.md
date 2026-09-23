# Developing gcalfuse

This guide is for people changing gcalfuse. For using it, see the
[README](../README.md).

- [Setup](#setup)
- [Architecture](#architecture)
- [The read path](#the-read-path)
- [The write path](#the-write-path)
- [FUSE decisions and why](#fuse-decisions-and-why)
- [Talking to Google](#talking-to-google)
- [Testing](#testing)
- [Trying it without a Google account](#trying-it-without-a-google-account)
- [Conventions](#conventions)
- [Common changes](#common-changes)
- [Known gaps](#known-gaps)

## Setup

    sudo apt install fuse3 libfuse3-dev      # pyfuse3 builds against libfuse3
    python3.12 -m venv .venv && source .venv/bin/activate
    pip install -e '.[dev]'

    pytest                                    # everything, ~10 s
    pytest -m "not fuse"                      # skip the real-mount tests, ~1 s
    ruff check . && ruff format --check .     # run both before committing

The real-mount tests need read/write access to `/dev/fuse` and `fusermount3`
(or `fusermount`). Without them they're skipped, not failed.

## Architecture

```
cli.py ──► auth.py            OAuth installed-app flow, token.json (0600)
   │
   ├──► config.py             TOML config, validated at load
   │
   ├──► api.py                CalendarClient: Google JSON <-> EventRecord,
   │       ▲                  pagination, syncToken, backoff, CalendarApiError
   │       │ CalendarClientProtocol (real client, or tests/fakes.py)
   │       │
   ├──► cache.py              EventRecord, EventIndex (path <-> event id),
   │       ▲                  CalendarCache (window refresh + poll thread)
   │       │
   └──► fs.py                 GcalfuseFS: pyfuse3 Operations; write buffering,
            │                 editor save patterns, commit-on-close
            ├──► paths.py     pure: slugify, filenames, path parsing, junk names
            └──► icsutil.py   pure: EventRecord -> .ics, .ics -> API body
```

Dependency direction is strictly downward: `paths` and `icsutil` know nothing
about FUSE or the network; `cache` knows nothing about FUSE; `fs` never
imports googleapiclient. `api.py` is the only module that touches Google's
libraries, and nothing it raises other than `CalendarApiError` escapes it.

### Threads

- **The trio thread** runs `pyfuse3.main()`; every FUSE handler is a
  coroutine on it.
- **The refresh thread** (`CalendarCache.start_background_refresh`) polls
  Google every `poll_seconds` and swaps new data into the `EventIndex`.
- **trio worker threads** run blocking Google calls made by FUSE handlers
  (`GcalfuseFS._call_api` uses `trio.to_thread.run_sync`) so a slow save
  doesn't freeze reads of other files.

`EventIndex` is guarded by an `RLock`. `CalendarClient` serializes all API
calls behind its own lock because httplib2 (inside googleapiclient) isn't
thread-safe. `GcalfuseFS._pending` and `_aliases` are only touched from
handlers on the trio thread.

## The read path

Everything is served from memory; no read ever waits on the network.

1. `CalendarCache.refresh_full()` fetches `[now - window_past_days, now +
   window_future_days]` with `singleEvents=true` (recurring series come back
   as individual instances) and replaces the `EventIndex` contents.
2. `EventIndex._rebuild_paths()` computes each record's path with
   `paths.path_for()` in the configured timezone and suffixes colliding
   names with `__<first 8 chars of the event id>`. The rebuild is O(n) and
   runs under the lock, so batch changes go through `EventIndex.apply()`.
3. Every `poll_seconds`, `refresh_once()` applies a syncToken delta, falling
   back to a full refetch when the token expired or every
   `FULL_REFRESH_SECONDS` (deltas report only *changed* events, so without
   that the window would never slide). Any failure is logged and the stale
   index is kept.
4. `GcalfuseFS` maps inodes to paths (allocated on first successful lookup,
   never reused) and answers `getattr`/`lookup`/`readdir`/`read` from the
   index, rendering ICS with `icsutil.event_to_ics()`.

Directory semantics: `readdir` lists only populated years, months and days,
but `_resolve()` treats every valid date path as an existing directory so
events can be created on or moved to empty days.

## The write path

Writes never go to Google as they happen. Each path being written has a
`PendingWrite` holding the whole file content in memory:

| Field | Meaning |
|---|---|
| `event_id` | `None`: committing inserts a new event. Set: committing patches that event. |
| `buffer` | Current file content. `read()`/`getattr()` serve this while it exists. |
| `dirty` | There's something to send. `create()` starts dirty so `touch` creates an event. |
| `parked`, `parked_from` | This entry is a real event that was renamed onto a scratch name (see below). |
| `adopted_from` | This file took over a parked event from that backup entry; used to re-park on failure. |

A name is **committable** if it ends in `.ics` and isn't editor junk
(`paths.is_editor_junk`). Anything else is a *scratch* file: kept in memory,
never sent anywhere.

### When a commit happens

`_commit_pending(path)` runs from `flush()` (and, as a fallback, `release()`)
and from `rename()` when a buffered file lands on a committable name. It does
nothing unless the entry is dirty, the name is committable, and the path is
`/YYYY/MM/DD/<name>.ics`. Otherwise it:

1. rejects recurring instances (EPERM),
2. builds a body (`_build_body`): parse with `icsutil.ics_to_event_patch`
   (floating times get the configured zone), apply create-time defaults,
   check `DTSTART` is on the folder's date (EINVAL), fill a missing end,
3. skips the API call entirely if the body equals what the current event
   would produce (`_body_for`): unchanged saves, or an editor restoring its
   backup after a failed save, send nothing;
4. otherwise calls `insert` or `patch` in a worker thread;
5. on success, updates the index, drops the pending entry, and records an
   **alias** if the event's canonical name differs from the name written;
6. on any failure, drops the pending entry (reads fall back to the last good
   content) and re-raises. If the entry had adopted a parked event, the
   event is parked on the backup again (`_repark`). `ENOENT` from Google
   (the event was deleted remotely) drops the event from the index.

### Editor save patterns

These sequences are what real editors send; each is covered by
`tests/test_fs_writes.py` and, for the ones marked *real*, by
`tests/test_integration_mount.py` with the actual tool.

**Overwrite in place** (`>`, `cp`, vim with `backupcopy=yes`), *real*:
`open(O_TRUNC)` → `write`… → `close`. `open` creates a pending entry with the
event id; `flush` patches. vim with `backupcopy=yes` first copies the file
to `name~` (a scratch file); if the save is rejected it copies the backup
back, which is an unchanged save and sends nothing.

**Write temp, rename over** (`sed -i`, `perl -i`, many "atomic save"
editors), *real*: `create(tmp)` → `write`… → `close` (scratch name: no
commit) → `rename(tmp, foo.ics)`. `_rename_pending` sees an existing event at
the target, takes its id, and commits a patch.

**Backup by rename** (vim with `backupcopy=no`, or `auto` after its `4913`
writability probe), *real*:
`rename(foo.ics, foo.ics~)` → `create(foo.ics)` → `write`… → `close` →
`unlink(foo.ics~)`.

- The first rename *parks* the event: a pending entry at `foo.ics~` with
  `parked=record, parked_from=foo.ics`, and the record is removed from the
  index. Google is untouched.
- `create(foo.ics)` calls `_adopt_parked`, which finds that entry, puts the
  record back in the index, and gives the new file its event id, so the
  commit is a **patch**, not a duplicate insert. The backup becomes plain
  scratch.
- `unlink(foo.ics~)` just drops the scratch entry.
- If the save is rejected, vim recovers with `unlink(foo.ics)` then
  `rename(foo.ics~, foo.ics)`. Because `_repark` put the event back on the
  backup when the save failed, the unlink finds nothing (ENOENT) and the
  rename restores the event: no API calls. (Before `_repark`, that unlink
  deleted the event in Google.)
- If nobody adopts it, unlinking a parked entry *restores* the event locally
  rather than deleting it (`mv foo.ics foo.ics~ && rm foo.ics~` never
  deletes anything in Google), and renaming it back to a real name restores
  it with no API call.

### Renaming a committed event

`_rename_committed` → `_rename_body`:

- different day folder → reschedule, keeping local clock time
  (`_reschedule_body`; DST-safe because `ZoneInfo`-aware `replace()`
  recomputes the offset; all-day events keep their span);
- different slug → retitle (`summary` = slug with `_` → space). The
  collision suffix of the source name is ignored, and a slug that matches the
  current title doesn't touch `summary`, so casing survives a pure move;
- different `HHMM-HHMM` prefix → EINVAL (times are derived, not input);
- target is another event → EEXIST (never delete implicitly);
- `RENAME_EXCHANGE` → EINVAL; directories → EPERM.

## FUSE decisions and why

These are the non-obvious choices; each one fixed a bug found on a real
mount.

- **Commit in `flush()`, not `release()`.** The kernel waits for FLUSH during
  `close(2)` and returns its errno from `close()`. RELEASE is sent after
  `close()` has already returned and its result is discarded, so errors
  raised there never reach the editor. `flush` can run several times per
  open (dup'd fds); the dirty flag makes the extra calls no-ops.
- **`attr_timeout = 0` for files.** The kernel truncates reads to the file
  size it has cached. Sizes change behind its back (a rejected save reverts
  the content; a successful one re-renders it canonically; a refresh updates
  it), so file attributes are never cached. Directories keep a 1 s timeout.
- **`keep_cache=False` on every open**, for the same reason: stale page
  cache would show old content.
- **`@_fuse_op` on every handler.** pyfuse3 tears down the whole mount when a
  handler raises anything other than `FUSEError`. The decorator logs the
  traceback and returns EIO instead.
- **Aliases.** After a save, the event is listed under its canonical name.
  Tools often stat or `utime` the name they just wrote (`touch`, vim), so
  that name keeps resolving, unlisted, for `ALIAS_SECONDS`
  (`_record_at` checks the index first, then aliases).
- **Inodes are path-based and never freed.** Simple and correct for a
  single-user mount; memory grows only with distinct paths actually looked
  up (failed lookups don't allocate).
- **Scratch files are allowed anywhere inside a day folder**, with any name,
  because atomic-save tools pick arbitrary temp names (`sed -i` uses
  `sedXXXXXX`). `create()` outside a day folder is EPERM. Scratch content
  still in memory at unmount is reported by `unsaved_scratch_files()`.
- **Path truncate without an open handle is EPERM**: there's no close to
  commit on.

## Talking to Google

- `CalendarClient` is constructed from a googleapiclient `service`
  (`from_credentials` builds the real one), so tests can pass a fake service.
- `_execute_with_backoff` retries 429, 5xx, 403 with reason
  `rateLimitExceeded`/`userRateLimitExceeded` (Calendar's usual rate-limit
  response), and `OSError` (timeouts, resets), with 1, 2, 4, 8, 16 s delays,
  then raises `CalendarApiError(status=...)`.
- `map_event` converts timed events into the configured zone. Google returns
  fixed offsets (`-05:00`); keeping those would emit a bogus
  `TZID="UTC-05:00"` that Google itself rejects on the next edit.
- `ics_to_event_patch` always includes `summary`/`description`/`location`
  (possibly `""`) so that deleting a line clears the field on patch, and only
  sets `timeZone` for real IANA names.

## Testing

| File | What it covers |
|---|---|
| `test_paths.py` | slugify, filenames, `parse_path`, collisions, junk names |
| `test_icsutil.py` | emit/parse, time zones, DURATION, malformed input |
| `test_api.py` | `map_event`, pagination, sync tokens, error mapping, backoff (fake `service`) |
| `test_cache.py` | `EventIndex`, window refresh, deltas, periodic full refresh, stale cache |
| `test_config.py` | defaults, loading, validation |
| `test_auth.py` | token file mode, every auth failure message (mocked OAuth) |
| `test_cli.py` | exit codes, mountpoint safety, stale mounts, umount |
| `test_fs_readonly.py` | listing, lookup, getattr, read, read-only enforcement |
| `test_fs_writes.py` | create/edit/unlink/rename, editor patterns, API failures, close semantics |
| `test_integration_mount.py` | real kernel mount driven by `ls`, `cat`, `mv`, `rm`, `touch`, **real vim, `sed -i`, `perl -i`**, signals |

vim notes for integration tests: run it as `vim -N -Es -u NONE -i NONE`
(`-u NONE` alone means Vi-compatible mode, where `backupcopy=yes` and there's
no write backup) with stdin from `/dev/null`, and clear `backupskip`, whose
default skips backups under `/tmp`, where pytest's `tmp_path` lives.

How the unit tests drive FUSE: handlers are plain coroutines, so tests call
them under `trio.run` with inodes from `lookup()` (see `tests/helpers.py`:
`lookup_path`, `dir_inode`, `expect_errno`). `readdir` can't be called this
way (`pyfuse3.readdir_reply` needs a kernel-issued token), so listing is
tested through `GcalfuseFS._children`, and the integration suite covers the
real `readdir`. `pyfuse3.SetattrFields` is read-only from Python; use
`helpers.setattr_fields(size=True)`.

`tests/fakes.py` has `FakeCalendarClient`, which **enforces Google's rules**:
it rejects a `dateTime` with no offset or `timeZone`, unknown zone names,
inserts without start/end, and empty time ranges, and it builds responses
through the real `map_event`. When you find a new way Google rejects
requests, teach the fake, so the unit tests catch it. Set `fail_next` to a
`CalendarApiError` to test error paths. `FakeSyncClient` adds syncToken
deltas.

Guidelines:

- Every bug fix gets a test that fails without the fix.
- Assert on the exact API calls made (`client.insert_calls`, `patch_calls`,
  `delete_calls`); "no API call" is usually the important assertion.
- If the behavior depends on what the kernel or a real editor does, add an
  integration test too. Several bugs above were only visible there.

## Trying it without a Google account

`scripts/dev_mount.py` mounts the real `GcalfuseFS` over a
`FakeCalendarClient` seeded with a few events around today:

    python scripts/dev_mount.py /tmp/cal -v --call-log /tmp/calls.jsonl
    # in another terminal:
    ls -R /tmp/cal
    vim /tmp/cal/$(date +%Y/%m/%d)/*standup.ics
    cat /tmp/calls.jsonl      # one JSON line per insert/patch/delete
    fusermount3 -u /tmp/cal

`-v` logs every FUSE operation, which is the quickest way to see what
syscall sequence a new editor uses. `--today YYYY-MM-DD` pins the sample
dates (the integration tests use it).

## Conventions

- Python 3.12+, `ruff format` (line length 100) and `ruff check`
  (E, F, I, UP, B). Run both before committing; CI isn't set up.
- Comments explain *why* (a constraint, a kernel behavior, a Google quirk),
  not what the code does.
- User-facing errors: raise `AuthError`, `ConfigError` or `MountError` with a
  message that says what to do; `cli.main` prints it and exits 1.
- FUSE handlers raise `pyfuse3.FUSEError(errno.X)` for expected failures and
  log at WARNING when a user action was rejected.

## Common changes

**Support another editor's temp-file names**: usually nothing to do, since
any name in a day folder works as scratch as long as the editor renames it
onto the `.ics` name. Add a pattern to `paths._JUNK_PATTERNS` only if the
editor leaves `.ics`-suffixed scratch files around, then add cases to
`test_paths.py` and, ideally, an integration test running the editor.

**Send another ICS property to Google**: extend `ics_to_event_patch`
(always include the key so it can be cleared), `event_to_ics`, `map_event`
and `EventRecord`, then add a roundtrip test in `test_icsutil.py` and an edit
test in `test_fs_writes.py`. Teach `FakeCalendarClient._store` the field.

**Change filename format**: `paths.filename_for` / `path_for`, plus
`fs._FILENAME_RE` and `_split_filename`, which parse names back into
(time prefix, slug) for renames.

## Known gaps

- Recurring instances are read-only (a v1 scope decision).
- Only one calendar per mount.
- Attendees, reminders, colors, and conference data aren't writable.
- Events outside the loaded window aren't visible, and events created
  outside it vanish from the mount at the next refresh (they stay in Google).
- A background refresh that finishes just after a local save can briefly
  show the pre-save state until the next poll.
- Inodes are never freed; fine for interactive use, not ideal for a mount
  running for months with heavy churn.
