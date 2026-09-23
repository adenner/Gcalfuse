"""FUSE filesystem operations for the /YYYY/MM/DD/<file>.ics tree.

Read path (getattr, lookup, readdir, open, read) is served entirely from the
in-memory EventIndex and never touches the network.

Write path: writes are buffered in memory per path (a PendingWrite) and sent
to Google once, when the file is closed (flush) or when a rename lands a
buffered file on a real event name. See docs/DEVELOPMENT.md for the full
state machine; the short version:

- A name that is not a real event name (editor swap/backup/temp files) never
  causes a Google call. Its content just sits in memory.
- Renaming a committed event to such a name "parks" it (Google untouched).
  If a new file is then created at the event's old name (vim's default save
  strategy), it adopts the parked event's id, so the save becomes a patch
  rather than a duplicate insert. If that save is rejected, the event is
  parked again so the editor's recovery can't delete it.
- Every handler converts unexpected exceptions to EIO, because pyfuse3
  tears down the whole mount on any exception that isn't a FUSEError.
"""

from __future__ import annotations

import errno
import functools
import logging
import os
import re
import stat
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

import pyfuse3
import trio

from .api import CalendarApiError
from .cache import EventIndex, EventRecord
from .icsutil import IcsValidationError, event_to_ics, ics_to_event_patch
from .paths import (
    DayDir,
    EventFile,
    InvalidPathError,
    MonthDir,
    RootDir,
    YearDir,
    is_editor_junk,
    parse_path,
    slugify,
)

if TYPE_CHECKING:
    from .api import CalendarClientProtocol

logger = logging.getLogger(__name__)

ROOT_PATH = PurePosixPath("/")
_ENTRY_TIMEOUT = 1.0
_DEFAULT_START = dtime(9, 0)
_DEFAULT_DURATION = timedelta(minutes=30)
# "HHMM-HHMM_slug.ics", "HHMM_slug.ics"; group 1 is the time prefix.
_FILENAME_RE = re.compile(r"^(\d{4}(?:-\d{4})?)_(.+)\.ics$")
_RECURRING_MSG = "v1 does not edit recurring instances"
# After a save, an event is re-listed under its canonical name. The name the
# program actually wrote keeps resolving (hidden from ls) for this long, so
# `touch x.ics` or vim can still stat/utime the file they just closed.
ALIAS_SECONDS = 60.0


@dataclass
class PendingWrite:
    """In-memory content for a path that has not been (or can't yet be) committed.

    event_id:    None for a brand-new file (commit = insert); otherwise the
                 Google event the content belongs to (commit = patch).
    parked:      the committed record this entry was renamed away from, when
                 an event was renamed onto a junk name. Lets us restore it
                 locally, or let a new file at `parked_from` adopt its id.
    adopted_from: for a file that adopted a parked event, the backup entry it
                 took the event from. If the save fails, the event is parked
                 on that backup again so the editor's recovery (delete the new
                 file, rename the backup back) can't delete it in Google.
    """

    event_id: str | None
    buffer: bytearray = field(default_factory=bytearray)
    dirty: bool = False
    parked: EventRecord | None = None
    parked_from: PurePosixPath | None = None
    adopted_from: PendingWrite | None = field(default=None, repr=False)


def _fuse_op(fn):
    """Turn any non-FUSEError exception into EIO instead of killing the mount."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except pyfuse3.FUSEError:
            raise
        except Exception:
            logger.exception("unexpected error in FUSE %s", fn.__name__)
            raise pyfuse3.FUSEError(errno.EIO) from None

    return wrapper


def is_committable_name(name: str) -> bool:
    """True if a file with this name commits to Google (vs. an editor scratch file)."""
    return name.endswith(".ics") and not is_editor_junk(name)


def _split_filename(filename: str, event_id: str | None = None) -> tuple[str | None, str]:
    """Split "HHMM-HHMM_slug[__id8].ics" into (time_prefix_or_None, slug)."""
    match = _FILENAME_RE.match(filename)
    prefix, slug = (match.group(1), match.group(2)) if match else (None, filename[:-4])
    if event_id:
        slug = slug.removesuffix(f"__{event_id[:8]}")
    return prefix, slug


def _desluggify(filename: str) -> str:
    """Best-effort SUMMARY from a filename, for files whose ICS doesn't set one."""
    _prefix, slug = _split_filename(filename)
    return slug.replace("_", " ").strip() or "Untitled"


def _start_date_in_tz(start_info: dict, tz) -> date:
    if "date" in start_info:
        return date.fromisoformat(start_info["date"])
    dt = datetime.fromisoformat(start_info["dateTime"])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz).date()


def _with_default_end(body: dict, duration: timedelta) -> None:
    """Fill a missing end (Google requires one) as start + duration."""
    start = body["start"]
    if "date" in start:
        days = max(1, duration.days)
        body["end"] = {
            "date": (date.fromisoformat(start["date"]) + timedelta(days=days)).isoformat()
        }
        return
    end = datetime.fromisoformat(start["dateTime"]) + duration
    body["end"] = {"dateTime": end.isoformat()}
    if "timeZone" in start:
        body["end"]["timeZone"] = start["timeZone"]


def _reschedule_body(record: EventRecord, new_date: date, tz) -> dict:
    """Move an event to new_date, keeping local clock time (and multi-day spans)."""
    if record.all_day:
        span = (record.end.date() - record.start.date()).days if record.end else 1
        return {
            "start": {"date": new_date.isoformat()},
            "end": {"date": (new_date + timedelta(days=max(1, span))).isoformat()},
        }
    tz_name = getattr(tz, "key", None)
    local_start = record.start.astimezone(tz)
    # replace() on a ZoneInfo-aware datetime recomputes the UTC offset for the
    # new date, so 15:00 stays 15:00 across a DST change.
    new_start = local_start.replace(year=new_date.year, month=new_date.month, day=new_date.day)
    body: dict = {"start": {"dateTime": new_start.isoformat()}}
    if record.end is not None:
        local_end = record.end.astimezone(tz)
        end_date = new_date + (local_end.date() - local_start.date())
        new_end = local_end.replace(year=end_date.year, month=end_date.month, day=end_date.day)
        body["end"] = {"dateTime": new_end.isoformat()}
    if tz_name:
        for key in body:
            body[key]["timeZone"] = tz_name
    return body


class GcalfuseFS(pyfuse3.Operations):
    """Maps the virtual calendar path tree onto an EventIndex, with writes."""

    supports_dot_lookup = True

    def __init__(self, index: EventIndex, client: CalendarClientProtocol, read_only: bool) -> None:
        super().__init__()
        self._index = index
        self._client = client
        self._read_only = read_only
        self._tz = index.tz
        self._inode_lock = threading.Lock()
        self._inode_to_path: dict[int, PurePosixPath] = {pyfuse3.ROOT_INODE: ROOT_PATH}
        self._path_to_inode: dict[PurePosixPath, int] = {ROOT_PATH: pyfuse3.ROOT_INODE}
        self._next_inode = pyfuse3.ROOT_INODE + 1
        self._pending: dict[PurePosixPath, PendingWrite] = {}
        self._aliases: dict[PurePosixPath, tuple[str, float]] = {}

    # -- inode bookkeeping ------------------------------------------------

    def _inode_for_path(self, path: PurePosixPath) -> int:
        with self._inode_lock:
            inode = self._path_to_inode.get(path)
            if inode is not None:
                return inode
            inode = self._next_inode
            self._next_inode += 1
            self._path_to_inode[path] = inode
            self._inode_to_path[inode] = path
            return inode

    def _path_for_inode(self, inode: int) -> PurePosixPath:
        path = self._inode_to_path.get(inode)
        if path is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        return path

    def _migrate_inode(self, old_path: PurePosixPath, new_path: PurePosixPath) -> None:
        with self._inode_lock:
            inode = self._path_to_inode.pop(old_path, None)
            if inode is not None:
                self._inode_to_path[inode] = new_path
                self._path_to_inode[new_path] = inode
        self._inode_for_path(new_path)

    # -- path resolution against the cache ---------------------------------

    def _record_at(self, path: PurePosixPath) -> EventRecord | None:
        """The event listed at `path`, or the one a recent save aliased there."""
        record = self._index.get_by_path(path)
        if record is not None:
            return record
        alias = self._aliases.get(path)
        if alias is None:
            return None
        event_id, expires = alias
        if time.monotonic() > expires:
            del self._aliases[path]
            return None
        return self._index.get(event_id)

    def _alias_if_renamed(self, path: PurePosixPath, event_id: str) -> None:
        if self._index.path_for_id(event_id) != path:
            self._aliases[path] = (event_id, time.monotonic() + ALIAS_SECONDS)

    def _resolve(self, path: PurePosixPath) -> tuple[str, EventRecord | None]:
        """Return ("dir"|"file", record_or_None), or raise FUSEError(ENOENT)."""
        if path == ROOT_PATH:
            return "dir", None
        try:
            parsed = parse_path(path)
        except InvalidPathError as exc:
            raise pyfuse3.FUSEError(errno.ENOENT) from exc

        if isinstance(parsed, YearDir | MonthDir | DayDir):
            # Every valid date resolves, even with no events, so you can
            # `cat >` or `mv` an event onto an empty day. Listings (readdir)
            # still show only populated years/months/days.
            return "dir", None
        if isinstance(parsed, EventFile):
            record = self._record_at(path)
            if record is not None:
                return "file", record
        raise pyfuse3.FUSEError(errno.ENOENT)

    def _children(self, path: PurePosixPath) -> list[tuple[str, str, EventRecord | None]]:
        """List (name, "dir"|"file", record_or_None) children of a directory path."""
        parsed = RootDir() if path == ROOT_PATH else parse_path(path)
        if isinstance(parsed, RootDir):
            return [(f"{year:04d}", "dir", None) for year in self._index.years()]
        if isinstance(parsed, YearDir):
            return [(f"{m:02d}", "dir", None) for m in self._index.months(parsed.year)]
        if isinstance(parsed, MonthDir):
            return [(f"{d:02d}", "dir", None) for d in self._index.days(parsed.year, parsed.month)]
        if isinstance(parsed, DayDir):
            return [
                (name, "file", self._index.get_by_path(path / name))
                for name in self._index.files(parsed.year, parsed.month, parsed.day)
            ]
        raise pyfuse3.FUSEError(errno.ENOTDIR)

    # -- attribute building -------------------------------------------------

    def _base_attrs(
        self, inode: int, mode: int, size: int, mtime_ns: int
    ) -> pyfuse3.EntryAttributes:
        entry = pyfuse3.EntryAttributes()
        entry.st_ino = inode
        entry.st_mode = mode
        entry.st_nlink = 2 if stat.S_ISDIR(mode) else 1
        entry.st_size = size
        entry.st_atime_ns = mtime_ns
        entry.st_mtime_ns = mtime_ns
        entry.st_ctime_ns = mtime_ns
        entry.st_uid = os.getuid()
        entry.st_gid = os.getgid()
        entry.st_blksize = 512
        entry.st_blocks = max(1, (size + 511) // 512)
        entry.entry_timeout = _ENTRY_TIMEOUT
        # A file's size changes behind the kernel's back (a rejected save
        # reverts it, a successful one re-renders it canonically, a refresh
        # updates it), and the kernel truncates reads to its cached size. So
        # never cache file attributes; they're cheap to recompute.
        entry.attr_timeout = _ENTRY_TIMEOUT if stat.S_ISDIR(mode) else 0
        return entry

    def _dir_attrs(self, inode: int) -> pyfuse3.EntryAttributes:
        return self._base_attrs(inode, stat.S_IFDIR | 0o755, 0, time.time_ns())

    def _file_attrs(self, inode: int, record: EventRecord) -> pyfuse3.EntryAttributes:
        mtime = record.updated or record.start
        mtime_ns = int(mtime.timestamp() * 1_000_000_000)
        return self._base_attrs(inode, stat.S_IFREG | 0o644, len(event_to_ics(record)), mtime_ns)

    def _pending_attrs(self, inode: int, pending: PendingWrite) -> pyfuse3.EntryAttributes:
        return self._base_attrs(inode, stat.S_IFREG | 0o644, len(pending.buffer), time.time_ns())

    def _attrs_for(self, inode: int, kind: str, record: EventRecord | None):
        return self._dir_attrs(inode) if kind == "dir" else self._file_attrs(inode, record)

    # -- Google calls ---------------------------------------------------------

    async def _call_api(self, fn, *args, missing_ok: bool = False):
        """Run a blocking client call off the trio loop; map failures to errnos.

        Running it in a worker thread keeps reads of *other* files responsive
        while a save is in flight.
        """
        try:
            return await trio.to_thread.run_sync(fn, *args)
        except CalendarApiError as exc:
            if exc.not_found and missing_ok:
                return None
            logger.error("Calendar API call %s failed: %s", fn.__name__, exc)
            raise pyfuse3.FUSEError(errno.ENOENT if exc.not_found else errno.EIO) from None

    def _reject_recurring(self, record: EventRecord | None) -> None:
        if record is not None and record.is_recurring_instance:
            logger.warning(_RECURRING_MSG)
            raise pyfuse3.FUSEError(errno.EPERM)

    def _require_writable(self) -> None:
        if self._read_only:
            raise pyfuse3.FUSEError(errno.EROFS)

    # -- read path ------------------------------------------------------------

    @_fuse_op
    async def getattr(self, inode, ctx=None):
        path = self._path_for_inode(inode)
        logger.debug("getattr %s", path)
        pending = self._pending.get(path)
        if pending is not None:
            return self._pending_attrs(inode, pending)
        kind, record = self._resolve(path)
        return self._attrs_for(inode, kind, record)

    @_fuse_op
    async def lookup(self, parent_inode, name, ctx=None):
        child_path = self._path_for_inode(parent_inode) / os.fsdecode(name)
        logger.debug("lookup %s", child_path)
        pending = self._pending.get(child_path)
        if pending is not None:
            return self._pending_attrs(self._inode_for_path(child_path), pending)
        # Resolve first so failed lookups (shell completion, .git probes) don't
        # allocate inodes.
        kind, record = self._resolve(child_path)
        return self._attrs_for(self._inode_for_path(child_path), kind, record)

    @_fuse_op
    async def opendir(self, inode, ctx=None):
        kind, _ = self._resolve(self._path_for_inode(inode))
        if kind != "dir":
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        return inode

    @_fuse_op
    async def readdir(self, fh, start_id, token):
        path = self._path_for_inode(fh)
        logger.debug("readdir %s (start_id=%d)", path, start_id)
        for i, (name, kind, record) in enumerate(self._children(path)):
            if i < start_id:
                continue
            attr = self._attrs_for(self._inode_for_path(path / name), kind, record)
            if not pyfuse3.readdir_reply(token, os.fsencode(name), attr, i + 1):
                break

    async def releasedir(self, fh):
        return None

    @_fuse_op
    async def open(self, inode, flags, ctx=None):
        path = self._path_for_inode(inode)
        logger.debug("open %s (flags=%s)", path, oct(flags))
        wants_write = bool(flags & (os.O_WRONLY | os.O_RDWR))
        # Content can change under us (background refresh, a commit re-rendering
        # the file), so never let the kernel keep stale pages across opens.
        info = pyfuse3.FileInfo(fh=inode, keep_cache=False)

        pending = self._pending.get(path)
        if pending is not None:
            if wants_write:
                self._require_writable()
                if flags & os.O_TRUNC:
                    pending.buffer.clear()
                    pending.dirty = True
            return info

        kind, record = self._resolve(path)
        if kind != "file":
            raise pyfuse3.FUSEError(errno.EISDIR)
        if wants_write:
            self._require_writable()
            self._reject_recurring(record)
            truncated = bool(flags & os.O_TRUNC)
            self._pending[path] = PendingWrite(
                event_id=record.event_id,
                buffer=bytearray(b"" if truncated else event_to_ics(record)),
                dirty=truncated,
            )
        return info

    @_fuse_op
    async def read(self, fh, off, size):
        path = self._path_for_inode(fh)
        logger.debug("read %s (off=%d, size=%d)", path, off, size)
        pending = self._pending.get(path)
        if pending is not None:
            return bytes(pending.buffer[off : off + size])
        record = self._record_at(path)
        if record is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        return event_to_ics(record)[off : off + size]

    @_fuse_op
    async def flush(self, fh):
        """Commit on close(2).

        FLUSH is the only close-time request the kernel waits for: its errno
        becomes close()'s return value. RELEASE arrives asynchronously after
        close() has returned and its errors are discarded, so committing there
        would silently swallow EIO/EINVAL/EPERM. flush can fire more than once
        per open (dup'd fds); the dirty flag makes repeat calls no-ops.
        """
        path = self._inode_to_path.get(fh)
        logger.debug("flush %s", path)
        if path is not None:
            await self._commit_pending(path)

    @_fuse_op
    async def release(self, fh):
        path = self._inode_to_path.get(fh)
        logger.debug("release %s", path)
        if path is None:
            return
        await self._commit_pending(path)  # normally already done by flush()
        pending = self._pending.get(path)
        if pending is not None and not pending.dirty and is_committable_name(path.name):
            # Opened for write but never written (e.g. `touch` on an existing
            # file): nothing to send, and the index is the source of truth again.
            del self._pending[path]

    @_fuse_op
    async def statfs(self, ctx=None):
        stats = pyfuse3.StatvfsData()
        stats.f_bsize = 512
        stats.f_frsize = 512
        stats.f_namemax = 255
        return stats

    # -- write path -------------------------------------------------------

    @_fuse_op
    async def create(self, parent_inode, name, mode, flags, ctx=None):
        self._require_writable()
        parent_path = self._path_for_inode(parent_inode)
        child_path = parent_path / os.fsdecode(name)
        logger.debug("create %s", child_path)

        # Events live only in day folders. Any name is allowed there: non-event
        # names are in-memory scratch files, because atomic-save tools use
        # arbitrary temp names (`sed -i` writes "sedXXXXXX", then renames it).
        try:
            parent = RootDir() if parent_path == ROOT_PATH else parse_path(parent_path)
        except InvalidPathError:
            parent = None
        if not isinstance(parent, DayDir):
            raise pyfuse3.FUSEError(errno.EPERM)

        existing = self._record_at(child_path)
        self._reject_recurring(existing)
        # dirty=True: a newly created real-named file commits even with no
        # writes (`touch 2026/09/24/lunch.ics` creates a default event).
        pending = PendingWrite(event_id=existing.event_id if existing else None, dirty=True)
        if existing is None:
            self._adopt_parked(child_path, pending)
        self._pending[child_path] = pending
        inode = self._inode_for_path(child_path)
        return pyfuse3.FileInfo(fh=inode, keep_cache=False), self._pending_attrs(inode, pending)

    def _adopt_parked(self, path: PurePosixPath, pending: PendingWrite) -> None:
        """If an event was parked (renamed to a junk name) from `path`, give it to `pending`.

        This is vim's default save: rename foo.ics -> foo.ics~, write a new
        foo.ics, delete foo.ics~. Without adoption that would insert a
        duplicate event.
        """
        for backup in self._pending.values():
            if backup.parked is not None and backup.parked_from == path:
                record = backup.parked
                self._reject_recurring(record)
                # Back in the index so its duration is known when the save
                # builds a patch body.
                self._index.add(record)
                backup.parked = None
                backup.parked_from = None
                backup.event_id = None  # the backup is now plain scratch
                pending.event_id = record.event_id
                pending.adopted_from = backup
                return

    def _repark(self, pending: PendingWrite, path: PurePosixPath) -> None:
        """Undo an adoption after a failed save: the event goes back on the backup."""
        backup = pending.adopted_from
        record = self._index.get(pending.event_id) if pending.event_id else None
        if record is None or not any(p is backup for p in self._pending.values()):
            return
        backup.parked = record
        backup.parked_from = path
        backup.event_id = record.event_id
        self._index.remove(record.event_id)

    @_fuse_op
    async def write(self, fh, off, buf):
        self._require_writable()
        pending = self._pending.get(self._path_for_inode(fh))
        if pending is None:
            raise pyfuse3.FUSEError(errno.EBADF)
        end = off + len(buf)
        if len(pending.buffer) < end:
            pending.buffer.extend(b"\x00" * (end - len(pending.buffer)))
        pending.buffer[off:end] = buf
        pending.dirty = True
        return len(buf)

    @_fuse_op
    async def setattr(self, inode, attr, fields, fh, ctx=None):
        path = self._path_for_inode(inode)
        if fields.update_size:
            self._require_writable()
            pending = self._pending.get(path)
            if pending is None:
                # Path-based truncate(2) with no open write handle: there's no
                # close to commit on, so refuse rather than silently drop it.
                raise pyfuse3.FUSEError(errno.EPERM)
            new_size = attr.st_size
            if new_size < len(pending.buffer):
                del pending.buffer[new_size:]
            else:
                pending.buffer.extend(b"\x00" * (new_size - len(pending.buffer)))
            pending.dirty = True
            return self._pending_attrs(inode, pending)
        # chmod/chown/utimens: succeed as a no-op so editors don't fail outright.
        return await self.getattr(inode, ctx)

    @_fuse_op
    async def unlink(self, parent_inode, name, ctx=None):
        self._require_writable()
        path = self._path_for_inode(parent_inode) / os.fsdecode(name)
        logger.debug("unlink %s", path)

        pending = self._pending.get(path)
        if pending is not None and not is_committable_name(path.name):
            # Scratch file. If it was a parked event nobody adopted, put the
            # event back where it was: junk names never cause Google writes.
            del self._pending[path]
            if pending.parked is not None:
                self._index.add(pending.parked)
            return
        if pending is not None and pending.event_id is None:
            del self._pending[path]  # created but not yet committed
            return

        record = self._record_at(path)
        if record is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        self._reject_recurring(record)

        await self._call_api(self._client.delete, record.event_id, missing_ok=True)
        self._index.remove(record.event_id)
        self._pending.pop(path, None)
        self._aliases.pop(path, None)

    @_fuse_op
    async def rename(self, parent_inode_old, name_old, parent_inode_new, name_new, flags, ctx=None):
        self._require_writable()
        if flags & pyfuse3.RENAME_EXCHANGE:
            raise pyfuse3.FUSEError(errno.EINVAL)

        old_path = self._path_for_inode(parent_inode_old) / os.fsdecode(name_old)
        new_path = self._path_for_inode(parent_inode_new) / os.fsdecode(name_new)
        logger.debug("rename %s -> %s", old_path, new_path)
        if old_path == new_path:
            return

        pending = self._pending.get(old_path)
        if pending is not None:
            await self._rename_pending(pending, old_path, new_path)
            return

        kind, record = self._resolve(old_path)
        if kind == "dir":
            raise pyfuse3.FUSEError(errno.EPERM)
        self._reject_recurring(record)
        await self._rename_committed(record, old_path, new_path)

    async def _rename_pending(
        self, pending: PendingWrite, old_path: PurePosixPath, new_path: PurePosixPath
    ) -> None:
        if pending.parked is not None and not pending.dirty and is_committable_name(new_path.name):
            # A parked event renamed back to a real name, unmodified: restore
            # it, then treat the rename as a normal move of that event.
            record = pending.parked
            del self._pending[old_path]
            self._index.add(record)
            restored_from = self._index.path_for_id(record.event_id)
            await self._rename_committed(record, restored_from, new_path)
            self._migrate_inode(old_path, new_path)
            return

        target = self._record_at(new_path)
        if target is not None:
            # Write-temp-then-rename-over: the temp content replaces the target.
            self._reject_recurring(target)
            if pending.event_id not in (None, target.event_id):
                raise pyfuse3.FUSEError(errno.EEXIST)
            pending.event_id = target.event_id
            pending.dirty = True
        elif pending.event_id is None and is_committable_name(new_path.name):
            self._adopt_parked(new_path, pending)

        del self._pending[old_path]
        self._pending[new_path] = pending
        self._migrate_inode(old_path, new_path)
        await self._commit_pending(new_path)

    async def _rename_committed(
        self, record: EventRecord, old_path: PurePosixPath, new_path: PurePosixPath
    ) -> None:
        if old_path == new_path:
            return
        target = self._record_at(new_path)
        if (target is not None and target.event_id != record.event_id) or new_path in self._pending:
            # POSIX would replace (delete) the target event. Too destructive to
            # do implicitly; the user can rm it first.
            logger.warning("refusing to rename %s over existing %s", old_path, new_path)
            raise pyfuse3.FUSEError(errno.EEXIST)

        if not is_committable_name(new_path.name):
            # Renamed onto a scratch name: leave Google alone and park the
            # event until a real name shows up (see _adopt_parked).
            self._pending[new_path] = PendingWrite(
                event_id=record.event_id,
                buffer=bytearray(event_to_ics(record)),
                parked=record,
                parked_from=old_path,
            )
            self._index.remove(record.event_id)
            self._migrate_inode(old_path, new_path)
            return

        try:
            new_parsed = parse_path(new_path)
        except InvalidPathError as exc:
            raise pyfuse3.FUSEError(errno.EINVAL) from exc
        if not isinstance(new_parsed, EventFile):
            raise pyfuse3.FUSEError(errno.EINVAL)

        body = self._rename_body(record, old_path, new_parsed)
        if body:
            updated = await self._call_api(self._client.patch, record.event_id, body)
            self._index.add(updated)
        self._aliases.pop(old_path, None)
        self._alias_if_renamed(new_path, record.event_id)
        self._migrate_inode(old_path, new_path)

    def _rename_body(self, record: EventRecord, old_path: PurePosixPath, new: EventFile) -> dict:
        """Patch body for a rename: new date -> reschedule, new slug -> retitle."""
        new_prefix, new_slug = _split_filename(new.filename, record.event_id)
        canonical = self._index.path_for_id(record.event_id) or old_path
        canonical_prefix, _ = _split_filename(canonical.name, record.event_id)
        if new_prefix is not None and new_prefix != canonical_prefix:
            # The time in a filename is derived from DTSTART/DTEND, not an input.
            logger.warning(
                "rename cannot change an event's time (%s -> %s); edit DTSTART instead",
                canonical_prefix,
                new_prefix,
            )
            raise pyfuse3.FUSEError(errno.EINVAL)

        body: dict = {}
        old_parsed = parse_path(old_path)
        new_date = date(new.year, new.month, new.day)
        if new_date != date(old_parsed.year, old_parsed.month, old_parsed.day):
            body.update(_reschedule_body(record, new_date, self._tz))
        if new_slug != slugify(record.summary):
            body["summary"] = new_slug.replace("_", " ").strip() or "Untitled"
        return body

    @_fuse_op
    async def mkdir(self, parent_inode, name, mode, ctx=None):
        raise pyfuse3.FUSEError(errno.EPERM)

    @_fuse_op
    async def rmdir(self, parent_inode, name, ctx=None):
        raise pyfuse3.FUSEError(errno.EPERM)

    def unsaved_scratch_files(self) -> list[PurePosixPath]:
        """Scratch files holding content that never reached Google (lost on unmount)."""
        return sorted(
            path
            for path, pending in self._pending.items()
            if pending.parked is None and pending.buffer and not is_editor_junk(path.name)
        )

    # -- commit-on-close ----------------------------------------------------

    async def _commit_pending(self, path: PurePosixPath) -> None:
        pending = self._pending.get(path)
        if pending is None or not pending.dirty or not is_committable_name(path.name):
            return  # nothing to send, or still an editor scratch name
        try:
            parsed = parse_path(path)
        except InvalidPathError:
            return
        if not isinstance(parsed, EventFile):
            return

        existing = self._index.get(pending.event_id) if pending.event_id else None
        try:
            self._reject_recurring(existing)
            body = self._build_body(pending, parsed, existing)
            if pending.event_id is None:
                record = await self._call_api(self._client.insert, body)
            elif existing is not None and body == self._body_for(existing):
                # Saved unchanged (`:w` with no edits, an editor restoring its
                # backup after a failed save): nothing to send.
                logger.debug("unchanged save of %s; no API call", path)
                record = existing
            else:
                record = await self._call_api(self._client.patch, pending.event_id, body)
        except pyfuse3.FUSEError as exc:
            # Drop the rejected buffer: reads fall back to the last good
            # content (or nothing, for a failed create). Google is untouched
            # unless the failure came from Google itself.
            self._pending.pop(path, None)
            if pending.adopted_from is not None:
                self._repark(pending, path)
            elif exc.errno == errno.ENOENT and pending.event_id:
                self._index.remove(pending.event_id)  # deleted remotely
            raise

        self._index.add(record)
        self._pending.pop(path, None)
        self._alias_if_renamed(path, record.event_id)

    def _body_for(self, record: EventRecord) -> dict:
        """The patch body that saving `record`'s own rendering would produce."""
        return ics_to_event_patch(event_to_ics(record), default_tz=self._tz)

    def _build_body(
        self, pending: PendingWrite, parsed: EventFile, existing: EventRecord | None
    ) -> dict:
        is_create = pending.event_id is None
        ics_bytes = bytes(pending.buffer).strip()
        folder_date = date(parsed.year, parsed.month, parsed.day)

        if not ics_bytes:
            if not is_create:
                logger.warning(
                    "empty file on close for %s; keeping previous content", parsed.filename
                )
                raise pyfuse3.FUSEError(errno.EIO)
            body: dict = {"summary": ""}
        else:
            try:
                body = ics_to_event_patch(ics_bytes, default_tz=self._tz)
            except IcsValidationError as exc:
                logger.warning(
                    "invalid ICS on close for %s (%s); keeping previous content",
                    parsed.filename,
                    exc,
                )
                raise pyfuse3.FUSEError(errno.EIO) from None

        if is_create and not body.get("summary"):
            body["summary"] = _desluggify(parsed.filename)

        if "start" not in body:
            if not is_create:
                logger.warning(
                    "missing DTSTART on close for %s; keeping previous content", parsed.filename
                )
                raise pyfuse3.FUSEError(errno.EIO)
            start = datetime.combine(folder_date, _DEFAULT_START, tzinfo=self._tz)
            body["start"] = {"dateTime": start.isoformat()}
            if getattr(self._tz, "key", None):
                body["start"]["timeZone"] = self._tz.key
            body.pop("end", None)

        start_date = _start_date_in_tz(body["start"], self._tz)
        if start_date != folder_date:
            logger.warning(
                "DTSTART %s does not match folder date %s for %s",
                start_date,
                folder_date,
                parsed.filename,
            )
            raise pyfuse3.FUSEError(errno.EINVAL)

        if "end" not in body:
            duration = _DEFAULT_DURATION
            if existing is not None and existing.end is not None:
                duration = existing.end - existing.start
            elif "date" in body["start"]:
                duration = timedelta(days=1)
            _with_default_end(body, duration)
        return body
