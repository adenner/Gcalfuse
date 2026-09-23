"""FUSE filesystem operations for the /YYYY/MM/DD/<file>.ics tree.

Read path: getattr, readdir, lookup, open, read — served entirely from the
cache, never blocking on the network.

Write path: write()/truncate() buffer into memory per open path; the actual
Google API call (insert/patch/delete) happens once, on release(). A file
whose current name doesn't look like a real event yet (editor swap/temp
names) is held as a buffered-but-uncommitted "pending" entry until a
rename() lands it on a real, non-junk /YYYY/MM/DD/<slug>.ics path — that
rename is what actually commits it. This is what lets `vim` (write-temp,
rename over the target) and `truncate+write+close` editors both work.
"""

from __future__ import annotations

import errno
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
)

if TYPE_CHECKING:
    from .api import CalendarClientProtocol

logger = logging.getLogger(__name__)

ROOT_PATH = PurePosixPath("/")
_ENTRY_TIMEOUT = 1.0
_DEFAULT_START = dtime(9, 0)
_DEFAULT_END = dtime(9, 30)
_FILENAME_SLUG_RE = re.compile(r"^\d{4}(?:-\d{4})?_(.+)\.ics$")


@dataclass
class PendingWrite:
    """Buffered, not-yet-committed content for a path with an active write."""

    event_id: str | None  # None: not-yet-inserted new event. Set: patching this event.
    buffer: bytearray = field(default_factory=bytearray)
    dirty: bool = False


def _desluggify(filename: str) -> str:
    """Best-effort recovery of a SUMMARY from a filename when the ICS has none."""
    match = _FILENAME_SLUG_RE.match(filename)
    slug = match.group(1) if match else filename.removesuffix(".ics")
    return slug.replace("_", " ").strip() or "Untitled"


def _start_date_in_tz(start_info: dict, tz) -> date:
    if "date" in start_info:
        return date.fromisoformat(start_info["date"])
    dt = datetime.fromisoformat(start_info["dateTime"])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz).date()


def _reschedule_body(record: EventRecord, new_date: date, tz) -> dict:
    """Move a non-all-day event to new_date, keeping its local clock time."""
    if record.all_day:
        return {
            "start": {"date": new_date.isoformat()},
            "end": {"date": (new_date + timedelta(days=1)).isoformat()},
        }
    local_start = record.start.astimezone(tz)
    new_start = local_start.replace(year=new_date.year, month=new_date.month, day=new_date.day)
    body: dict = {"start": {"dateTime": new_start.isoformat()}}
    if record.end is not None:
        local_end = record.end.astimezone(tz)
        day_delta = (local_end.date() - local_start.date()).days
        end_date = new_date + timedelta(days=day_delta)
        new_end = local_end.replace(year=end_date.year, month=end_date.month, day=end_date.day)
        body["end"] = {"dateTime": new_end.isoformat()}
    return body


class GcalfuseFS(pyfuse3.Operations):
    """Maps the virtual calendar path tree onto an EventIndex, with writes."""

    supports_dot_lookup = True

    def __init__(
        self, index: EventIndex, client: CalendarClientProtocol, read_only: bool
    ) -> None:
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

    def _resolve(self, path: PurePosixPath) -> tuple[str, EventRecord | None]:
        """Return ("dir"|"file", record_or_None), or raise FUSEError(ENOENT)."""
        if path == ROOT_PATH:
            return "dir", None
        try:
            parsed = parse_path(str(path))
        except InvalidPathError as exc:
            raise pyfuse3.FUSEError(errno.ENOENT) from exc

        if isinstance(parsed, RootDir):
            return "dir", None
        if isinstance(parsed, YearDir):
            if parsed.year in self._index.years():
                return "dir", None
        elif isinstance(parsed, MonthDir):
            if parsed.month in self._index.months(parsed.year):
                return "dir", None
        elif isinstance(parsed, DayDir):
            if parsed.day in self._index.days(parsed.year, parsed.month):
                return "dir", None
        elif isinstance(parsed, EventFile):
            record = self._index.get_by_path(path)
            if record is not None:
                return "file", record
        raise pyfuse3.FUSEError(errno.ENOENT)

    def _children(self, path: PurePosixPath) -> list[tuple[str, str, EventRecord | None]]:
        """List (name, "dir"|"file", record_or_None) children of a directory path."""
        parsed = RootDir() if path == ROOT_PATH else parse_path(str(path))
        if isinstance(parsed, RootDir):
            return [(f"{year:04d}", "dir", None) for year in self._index.years()]
        if isinstance(parsed, YearDir):
            return [
                (f"{month:02d}", "dir", None) for month in self._index.months(parsed.year)
            ]
        if isinstance(parsed, MonthDir):
            return [
                (f"{day:02d}", "dir", None)
                for day in self._index.days(parsed.year, parsed.month)
            ]
        if isinstance(parsed, DayDir):
            children = []
            for filename in self._index.files(parsed.year, parsed.month, parsed.day):
                record = self._index.get_by_path(path / filename)
                children.append((filename, "file", record))
            return children
        raise pyfuse3.FUSEError(errno.ENOTDIR)

    # -- attribute building -------------------------------------------------

    def _dir_attrs(self, inode: int) -> pyfuse3.EntryAttributes:
        entry = pyfuse3.EntryAttributes()
        entry.st_ino = inode
        entry.st_mode = stat.S_IFDIR | 0o755
        entry.st_nlink = 2
        entry.st_size = 0
        now_ns = time.time_ns()
        entry.st_atime_ns = now_ns
        entry.st_mtime_ns = now_ns
        entry.st_ctime_ns = now_ns
        entry.st_uid = os.getuid()
        entry.st_gid = os.getgid()
        entry.st_blksize = 512
        entry.st_blocks = 1
        entry.entry_timeout = _ENTRY_TIMEOUT
        entry.attr_timeout = _ENTRY_TIMEOUT
        return entry

    def _size_attrs(self, inode: int, size: int, mtime: datetime) -> pyfuse3.EntryAttributes:
        entry = pyfuse3.EntryAttributes()
        entry.st_ino = inode
        entry.st_mode = stat.S_IFREG | 0o644
        entry.st_nlink = 1
        entry.st_size = size
        mtime_ns = int(mtime.timestamp() * 1_000_000_000)
        entry.st_atime_ns = mtime_ns
        entry.st_mtime_ns = mtime_ns
        entry.st_ctime_ns = mtime_ns
        entry.st_uid = os.getuid()
        entry.st_gid = os.getgid()
        entry.st_blksize = 512
        entry.st_blocks = max(1, (size + 511) // 512)
        entry.entry_timeout = _ENTRY_TIMEOUT
        entry.attr_timeout = _ENTRY_TIMEOUT
        return entry

    def _file_attrs(self, inode: int, record: EventRecord) -> pyfuse3.EntryAttributes:
        data = event_to_ics(record)
        return self._size_attrs(inode, len(data), record.updated or record.start)

    def _pending_attrs(self, inode: int, pending: PendingWrite) -> pyfuse3.EntryAttributes:
        return self._size_attrs(inode, len(pending.buffer), datetime.now(self._tz))

    def _attrs_for(self, inode: int, kind: str, record: EventRecord | None):
        return self._dir_attrs(inode) if kind == "dir" else self._file_attrs(inode, record)

    # -- read path ------------------------------------------------------------

    async def getattr(self, inode, ctx=None):
        path = self._path_for_inode(inode)
        logger.debug("getattr %s", path)
        pending = self._pending.get(path)
        if pending is not None:
            return self._pending_attrs(inode, pending)
        kind, record = self._resolve(path)
        return self._attrs_for(inode, kind, record)

    async def lookup(self, parent_inode, name, ctx=None):
        parent_path = self._path_for_inode(parent_inode)
        child_path = parent_path / os.fsdecode(name)
        logger.debug("lookup %s", child_path)
        pending = self._pending.get(child_path)
        inode = self._inode_for_path(child_path)
        if pending is not None:
            return self._pending_attrs(inode, pending)
        kind, record = self._resolve(child_path)
        return self._attrs_for(inode, kind, record)

    async def opendir(self, inode, ctx=None):
        path = self._path_for_inode(inode)
        kind, _ = self._resolve(path)
        if kind != "dir":
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        return inode

    async def readdir(self, fh, start_id, token):
        path = self._path_for_inode(fh)
        logger.debug("readdir %s (start_id=%d)", path, start_id)
        children = self._children(path)
        for i, (name, kind, record) in enumerate(children):
            if i < start_id:
                continue
            child_path = path / name
            inode = self._inode_for_path(child_path)
            attr = self._attrs_for(inode, kind, record)
            if not pyfuse3.readdir_reply(token, os.fsencode(name), attr, i + 1):
                break

    async def releasedir(self, fh):
        return None

    async def open(self, inode, flags, ctx=None):
        path = self._path_for_inode(inode)
        logger.debug("open %s (flags=%s)", path, oct(flags))
        pending = self._pending.get(path)
        if pending is not None:
            return pyfuse3.FileInfo(fh=inode)

        kind, record = self._resolve(path)
        if kind != "file":
            raise pyfuse3.FUSEError(errno.EISDIR)

        wants_write = bool(flags & (os.O_WRONLY | os.O_RDWR))
        if wants_write:
            if self._read_only:
                raise pyfuse3.FUSEError(errno.EROFS)
            if record.is_recurring_instance:
                logger.warning("v1 does not edit recurring instances")
                raise pyfuse3.FUSEError(errno.EPERM)
            initial = b"" if flags & os.O_TRUNC else event_to_ics(record)
            self._pending[path] = PendingWrite(event_id=record.event_id, buffer=bytearray(initial))
        return pyfuse3.FileInfo(fh=inode)

    async def read(self, fh, off, size):
        path = self._path_for_inode(fh)
        logger.debug("read %s (off=%d, size=%d)", path, off, size)
        pending = self._pending.get(path)
        if pending is not None:
            return bytes(pending.buffer[off : off + size])
        record = self._index.get_by_path(path)
        if record is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        return event_to_ics(record)[off : off + size]

    async def release(self, fh):
        path = self._inode_to_path.get(fh)
        logger.debug("release %s", path)
        if path is not None:
            await self._commit_pending(path)

    async def flush(self, fh):
        return None

    # -- write path -------------------------------------------------------

    async def create(self, parent_inode, name, mode, flags, ctx=None):
        if self._read_only:
            raise pyfuse3.FUSEError(errno.EROFS)
        parent_path = self._path_for_inode(parent_inode)
        child_path = parent_path / os.fsdecode(name)
        logger.debug("create %s", child_path)

        existing = self._index.get_by_path(child_path)
        if existing is not None and existing.is_recurring_instance:
            logger.warning("v1 does not edit recurring instances")
            raise pyfuse3.FUSEError(errno.EPERM)

        event_id = existing.event_id if existing is not None else None
        pending = PendingWrite(event_id=event_id, buffer=bytearray())
        self._pending[child_path] = pending
        inode = self._inode_for_path(child_path)
        return pyfuse3.FileInfo(fh=inode), self._pending_attrs(inode, pending)

    async def write(self, fh, off, buf):
        if self._read_only:
            raise pyfuse3.FUSEError(errno.EROFS)
        path = self._path_for_inode(fh)
        pending = self._pending.get(path)
        if pending is None:
            raise pyfuse3.FUSEError(errno.EBADF)
        end = off + len(buf)
        if len(pending.buffer) < end:
            pending.buffer.extend(b"\x00" * (end - len(pending.buffer)))
        pending.buffer[off:end] = buf
        pending.dirty = True
        return len(buf)

    async def setattr(self, inode, attr, fields, fh, ctx=None):
        path = self._path_for_inode(inode)
        if fields.update_size:
            if self._read_only:
                raise pyfuse3.FUSEError(errno.EROFS)
            pending = self._pending.get(path)
            if pending is None:
                # Truncating a file we don't have an open write buffer for.
                raise pyfuse3.FUSEError(errno.EROFS)
            new_size = attr.st_size
            if new_size < len(pending.buffer):
                del pending.buffer[new_size:]
            else:
                pending.buffer.extend(b"\x00" * (new_size - len(pending.buffer)))
            pending.dirty = True
            return self._pending_attrs(inode, pending)
        # chmod/chown/utimens: succeed as a no-op so editors don't fail outright.
        return await self.getattr(inode, ctx)

    async def unlink(self, parent_inode, name, ctx=None):
        if self._read_only:
            raise pyfuse3.FUSEError(errno.EROFS)
        parent_path = self._path_for_inode(parent_inode)
        path = parent_path / os.fsdecode(name)
        logger.debug("unlink %s", path)

        pending = self._pending.get(path)
        if pending is not None and pending.event_id is None:
            # Never-committed scratch/temp file (e.g. an editor swap file).
            del self._pending[path]
            return

        record = self._index.get_by_path(path)
        if record is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        if record.is_recurring_instance:
            logger.warning("v1 does not edit recurring instances")
            raise pyfuse3.FUSEError(errno.EPERM)

        self._client.delete(record.event_id)
        self._index.remove(record.event_id)
        self._pending.pop(path, None)

    async def rename(
        self, parent_inode_old, name_old, parent_inode_new, name_new, flags, ctx=None
    ):
        if self._read_only:
            raise pyfuse3.FUSEError(errno.EROFS)

        old_path = self._path_for_inode(parent_inode_old) / os.fsdecode(name_old)
        new_path = self._path_for_inode(parent_inode_new) / os.fsdecode(name_new)
        logger.debug("rename %s -> %s", old_path, new_path)

        pending = self._pending.pop(old_path, None)
        if pending is not None:
            # Carry buffered-but-uncommitted content onto the new name. If the
            # new name is a real /YYYY/MM/DD/<slug>.ics path, this rename is
            # the actual commit (the vim write-temp-then-rename pattern).
            self._pending[new_path] = pending
            self._migrate_inode(old_path, new_path)
            await self._commit_pending(new_path)
            return

        record = self._index.get_by_path(old_path)
        if record is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        if record.is_recurring_instance:
            logger.warning("v1 does not edit recurring instances")
            raise pyfuse3.FUSEError(errno.EPERM)

        new_name = new_path.name
        if is_editor_junk(new_name) or not new_name.endswith(".ics"):
            # Renamed to a scratch/junk name: leave Google alone, park it as
            # pending until a real .ics name shows up.
            self._pending[new_path] = PendingWrite(
                event_id=record.event_id, buffer=bytearray(event_to_ics(record))
            )
            self._index.remove(record.event_id)
            self._migrate_inode(old_path, new_path)
            return

        try:
            new_parsed = parse_path(str(new_path))
        except InvalidPathError as exc:
            raise pyfuse3.FUSEError(errno.EINVAL) from exc
        if not isinstance(new_parsed, EventFile):
            raise pyfuse3.FUSEError(errno.EINVAL)

        old_parsed = parse_path(str(old_path))
        same_day = (old_parsed.year, old_parsed.month, old_parsed.day) == (
            new_parsed.year,
            new_parsed.month,
            new_parsed.day,
        )
        if same_day:
            body = {"summary": _desluggify(new_name)}
        else:
            new_date = date(new_parsed.year, new_parsed.month, new_parsed.day)
            body = _reschedule_body(record, new_date, self._tz)

        updated = self._client.patch(record.event_id, body)
        self._index.add(updated)
        self._migrate_inode(old_path, new_path)

    async def mkdir(self, parent_inode, name, mode, ctx=None):
        raise pyfuse3.FUSEError(errno.EPERM)

    async def rmdir(self, parent_inode, name, ctx=None):
        raise pyfuse3.FUSEError(errno.EPERM)

    # -- commit-on-close ----------------------------------------------------

    async def _commit_pending(self, path: PurePosixPath) -> None:
        pending = self._pending.get(path)
        if pending is None or not pending.dirty:
            return

        name = path.name
        if not name.endswith(".ics") or is_editor_junk(name):
            return  # still just a temp/scratch name; nothing to commit yet

        try:
            parsed = parse_path(str(path))
        except InvalidPathError:
            return
        if not isinstance(parsed, EventFile):
            return

        is_create = pending.event_id is None
        try:
            if not is_create:
                existing = self._index.get(pending.event_id)
                if existing is not None and existing.is_recurring_instance:
                    logger.warning("v1 does not edit recurring instances")
                    raise pyfuse3.FUSEError(errno.EPERM)

            body = self._build_patch_body(pending, parsed, is_create)
            if is_create:
                record = self._client.insert(body)
            else:
                record = self._client.patch(pending.event_id, body)
        except pyfuse3.FUSEError:
            # Parse/validation/permission failure: drop the buffer so the next
            # read falls through to whatever was there before (or nothing, for
            # a failed create). Never touch Google on this path.
            self._pending.pop(path, None)
            raise

        self._index.add(record)
        self._pending.pop(path, None)

    def _build_patch_body(self, pending: PendingWrite, parsed: EventFile, is_create: bool) -> dict:
        ics_bytes = bytes(pending.buffer).strip()
        folder_date = date(parsed.year, parsed.month, parsed.day)

        if not ics_bytes:
            body: dict = {}
        else:
            try:
                body = ics_to_event_patch(ics_bytes)
            except IcsValidationError:
                logger.warning(
                    "invalid ICS on close for %s; keeping previous content", parsed.filename
                )
                raise pyfuse3.FUSEError(errno.EIO) from None

        if not body.get("start"):
            if not is_create:
                logger.warning(
                    "missing DTSTART on close for %s; keeping previous content", parsed.filename
                )
                raise pyfuse3.FUSEError(errno.EIO)
            default_start = datetime.combine(folder_date, _DEFAULT_START, tzinfo=self._tz)
            default_end = datetime.combine(folder_date, _DEFAULT_END, tzinfo=self._tz)
            body["start"] = {"dateTime": default_start.isoformat()}
            body["end"] = {"dateTime": default_end.isoformat()}
            if not body.get("summary"):
                body["summary"] = _desluggify(parsed.filename)
            return body

        start_date = _start_date_in_tz(body["start"], self._tz)
        if start_date != folder_date:
            logger.warning(
                "DTSTART %s does not match folder date %s for %s",
                start_date,
                folder_date,
                parsed.filename,
            )
            raise pyfuse3.FUSEError(errno.EINVAL)
        return body
