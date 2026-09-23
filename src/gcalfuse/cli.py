"""gcalfuse command-line interface."""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from . import auth
from .config import Config

logger = logging.getLogger(__name__)


class MountError(RuntimeError):
    """Raised when a mount is refused for safety reasons (see _check_mountpoint)."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gcalfuse",
        description="Mount Google Calendar as a filesystem of .ics files.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging, including every FUSE operation.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("auth", help="Run the Google OAuth installed-app flow.")

    mount_parser = subparsers.add_parser("mount", help="Mount the calendar filesystem.")
    mount_parser.add_argument(
        "mountpoint", nargs="?", default=None, help="Where to mount (default: config mountpoint)"
    )
    mount_parser.add_argument(
        "--read-only", action="store_true", help="Force a read-only mount."
    )

    umount_parser = subparsers.add_parser("umount", help="Unmount the calendar filesystem.")
    umount_parser.add_argument(
        "mountpoint",
        nargs="?",
        default=None,
        help="Where it's mounted (default: config mountpoint)",
    )

    subparsers.add_parser("ls-days", help="List cached dates and filenames (debug).")

    return parser


def _cmd_auth(args: argparse.Namespace) -> int:
    config = Config.load()
    auth.run_auth_flow(config)
    print(f"Saved token to {config.token_path}")
    return 0


def _build_cache(config: Config):
    from .api import CalendarClient
    from .cache import CalendarCache

    tz = ZoneInfo(config.timezone)
    creds = auth.load_credentials(config)
    client = CalendarClient(creds, config.calendar_id, tz)
    cache = CalendarCache(
        client, tz, config.window_past_days, config.window_future_days, config.poll_seconds
    )
    return cache, client


def _is_gcalfuse_mount(mountpoint: Path) -> bool:
    """Best-effort check for an existing gcalfuse mount at this path."""
    try:
        lines = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return False
    target = str(mountpoint.resolve())
    for line in lines:
        fields = line.split()
        if len(fields) >= 3 and fields[1] == target and "gcalfuse" in fields[0]:
            return True
    return False


def _check_mountpoint(mountpoint: Path) -> None:
    if not mountpoint.exists():
        return
    if _is_gcalfuse_mount(mountpoint):
        raise MountError(f"{mountpoint} is already mounted by gcalfuse.")
    if any(mountpoint.iterdir()):
        raise MountError(
            f"{mountpoint} is not empty. Refusing to mount over existing files; "
            "pick an empty directory."
        )


def _cmd_mount(args: argparse.Namespace) -> int:
    import pyfuse3
    import trio

    from .fs import GcalfuseFS

    config = Config.load()
    read_only = args.read_only or config.read_only
    mountpoint = Path(args.mountpoint).expanduser() if args.mountpoint else config.mountpoint
    mountpoint.mkdir(parents=True, exist_ok=True)
    _check_mountpoint(mountpoint)

    cache, client = _build_cache(config)
    cache.refresh_full()
    cache.start_background_refresh()

    fs_ops = GcalfuseFS(cache.index, client, read_only=read_only)
    fuse_options = set(pyfuse3.default_options)
    fuse_options.add("fsname=gcalfuse")
    if read_only:
        fuse_options.add("ro")

    logger.info("mounting %s (read_only=%s)", mountpoint, read_only)
    pyfuse3.init(fs_ops, str(mountpoint), fuse_options)
    try:
        trio.run(pyfuse3.main)
    except KeyboardInterrupt:
        pass
    finally:
        cache.stop_background_refresh()
        pyfuse3.close(unmount=True)
    return 0


def _cmd_umount(args: argparse.Namespace) -> int:
    config = Config.load()
    mountpoint = Path(args.mountpoint).expanduser() if args.mountpoint else config.mountpoint
    subprocess.run(["fusermount", "-u", str(mountpoint)], check=True)
    return 0


def _cmd_ls_days(args: argparse.Namespace) -> int:
    config = Config.load()
    cache, _client = _build_cache(config)
    cache.refresh_full()
    index = cache.index
    for year in index.years():
        for month in index.months(year):
            for day in index.days(year, month):
                print(f"{year:04d}/{month:02d}/{day:02d}")
                for filename in index.files(year, month, day):
                    print(f"  {filename}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    handlers = {
        "auth": _cmd_auth,
        "mount": _cmd_mount,
        "umount": _cmd_umount,
        "ls-days": _cmd_ls_days,
    }
    handler = handlers.get(args.command)
    if handler is None:
        parser.error(f"unknown command {args.command!r}")
        return 2

    try:
        return handler(args)
    except (auth.MissingCredentialsError, MountError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
