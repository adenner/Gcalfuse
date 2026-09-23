"""FUSE filesystem operations for the /YYYY/MM/DD/<file>.ics tree.

Read path only in this phase: getattr, readdir, lookup, open, read.
Every mutation returns EROFS until phase 3 adds write-on-close.
"""

from __future__ import annotations

import errno
import logging
import os
import stat
import threading
import time
from pathlib import PurePosixPath

import pyfuse3

from .cache import EventIndex, EventRecord
from .icsutil import event_to_ics
from .paths import (
    DayDir,
    EventFile,
    InvalidPathError,
    MonthDir,
    RootDir,
    YearDir,
    parse_path,
)

logger = logging.getLogger(__name__)

ROOT_PATH = PurePosixPath("/")
_ENTRY_TIMEOUT = 1.0


class GcalfuseFS(pyfuse3.Operations):
    """Maps the virtual calendar path tree onto an EventIndex."""

    supports_dot_lookup = True

    def __init__(self, index: EventIndex, read_only: bool) -> None:
        super().__init__()
        self._index = index
        self._read_only = read_only
        self._inode_lock = threading.Lock()
        self._inode_to_path: dict[int, PurePosixPath] = {pyfuse3.ROOT_INODE: ROOT_PATH}
        self._path_to_inode: dict[PurePosixPath, int] = {ROOT_PATH: pyfuse3.ROOT_INODE}
        self._next_inode = pyfuse3.ROOT_INODE + 1

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

    def _file_attrs(self, inode: int, record: EventRecord) -> pyfuse3.EntryAttributes:
        entry = pyfuse3.EntryAttributes()
        data = event_to_ics(record)
        entry.st_ino = inode
        entry.st_mode = stat.S_IFREG | 0o644
        entry.st_nlink = 1
        entry.st_size = len(data)
        mtime = record.updated or record.start
        mtime_ns = int(mtime.timestamp() * 1_000_000_000)
        entry.st_atime_ns = mtime_ns
        entry.st_mtime_ns = mtime_ns
        entry.st_ctime_ns = mtime_ns
        entry.st_uid = os.getuid()
        entry.st_gid = os.getgid()
        entry.st_blksize = 512
        entry.st_blocks = max(1, (entry.st_size + 511) // 512)
        entry.entry_timeout = _ENTRY_TIMEOUT
        entry.attr_timeout = _ENTRY_TIMEOUT
        return entry

    def _attrs_for(self, inode: int, kind: str, record: EventRecord | None):
        return self._dir_attrs(inode) if kind == "dir" else self._file_attrs(inode, record)

    # -- read-only operations -------------------------------------------------

    async def getattr(self, inode, ctx=None):
        path = self._path_for_inode(inode)
        kind, record = self._resolve(path)
        return self._attrs_for(inode, kind, record)

    async def lookup(self, parent_inode, name, ctx=None):
        parent_path = self._path_for_inode(parent_inode)
        name_str = os.fsdecode(name)
        child_path = parent_path / name_str
        kind, record = self._resolve(child_path)
        inode = self._inode_for_path(child_path)
        return self._attrs_for(inode, kind, record)

    async def opendir(self, inode, ctx=None):
        path = self._path_for_inode(inode)
        kind, _ = self._resolve(path)
        if kind != "dir":
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        return inode

    async def readdir(self, fh, start_id, token):
        path = self._path_for_inode(fh)
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
        kind, _ = self._resolve(path)
        if kind != "file":
            raise pyfuse3.FUSEError(errno.EISDIR)
        if flags & (os.O_WRONLY | os.O_RDWR):
            raise pyfuse3.FUSEError(errno.EROFS)
        return pyfuse3.FileInfo(fh=inode)

    async def read(self, fh, off, size):
        path = self._path_for_inode(fh)
        record = self._index.get_by_path(path)
        if record is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        data = event_to_ics(record)
        return data[off : off + size]

    async def release(self, fh):
        return None

    # -- mutations: read-only in this phase -----------------------------------

    async def setattr(self, inode, attr, fields, fh, ctx=None):
        if fields.update_size:
            # No write-on-close support yet; truncate would silently lose data.
            raise pyfuse3.FUSEError(errno.EROFS)
        # chmod/chown/utimens: succeed as a no-op so editors don't fail outright.
        return await self.getattr(inode, ctx)

    async def create(self, parent_inode, name, mode, flags, ctx=None):
        raise pyfuse3.FUSEError(errno.EROFS)

    async def write(self, fh, off, buf):
        raise pyfuse3.FUSEError(errno.EROFS)

    async def unlink(self, parent_inode, name, ctx=None):
        raise pyfuse3.FUSEError(errno.EROFS)

    async def rename(self, parent_inode_old, name_old, parent_inode_new, name_new, flags, ctx=None):
        raise pyfuse3.FUSEError(errno.EROFS)

    async def mkdir(self, parent_inode, name, mode, ctx=None):
        raise pyfuse3.FUSEError(errno.EPERM)

    async def rmdir(self, parent_inode, name, ctx=None):
        raise pyfuse3.FUSEError(errno.EPERM)

    async def flush(self, fh):
        return None
