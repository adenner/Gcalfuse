"""gcalfuse command-line interface."""

from __future__ import annotations

import argparse
import errno
import logging
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from . import auth
from .config import Config, ConfigError

logger = logging.getLogger(__name__)


class MountError(RuntimeError):
    """A mount/unmount was refused or failed for a reason the user can fix."""


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
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=None,
        help="Config file (default: ~/.config/gcalfuse/config.toml).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth_parser = subparsers.add_parser("auth", help="Run the Google OAuth installed-app flow.")
    auth_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the consent URL instead of opening a browser (e.g. over SSH).",
    )

    mount_parser = subparsers.add_parser("mount", help="Mount the calendar filesystem.")
    mount_parser.add_argument(
        "mountpoint", nargs="?", default=None, help="Where to mount (default: config mountpoint)"
    )
    mount_parser.add_argument("--read-only", action="store_true", help="Force a read-only mount.")

    umount_parser = subparsers.add_parser("umount", help="Unmount the calendar filesystem.")
    umount_parser.add_argument(
        "mountpoint",
        nargs="?",
        default=None,
        help="Where it's mounted (default: config mountpoint)",
    )

    subparsers.add_parser("ls-days", help="List cached dates and filenames (debug).")

    return parser


def _mountpoint(args: argparse.Namespace, config: Config) -> Path:
    return Path(args.mountpoint).expanduser() if args.mountpoint else config.mountpoint


def _cmd_auth(args: argparse.Namespace, config: Config) -> int:
    auth.run_auth_flow(config, open_browser=not args.no_browser)
    print(f"Saved token to {config.token_path}")
    return 0


def _build_cache(config: Config):
    from .api import CalendarClient
    from .cache import CalendarCache

    creds = auth.load_credentials(config)
    client = CalendarClient.from_credentials(creds, config.calendar_id, config.tz)
    cache = CalendarCache(
        client, config.tz, config.window_past_days, config.window_future_days, config.poll_seconds
    )
    return cache, client


def _initial_refresh(cache) -> None:
    from .api import CalendarApiError

    try:
        cache.refresh_full()
    except CalendarApiError as exc:
        hint = " Run `gcalfuse auth` again." if exc.status in (401, 403) else ""
        raise MountError(f"Could not fetch events from Google Calendar: {exc}.{hint}") from exc


def _is_gcalfuse_mount(mountpoint: Path, mounts_file: Path = Path("/proc/mounts")) -> bool:
    """Best-effort check for an existing gcalfuse mount at this path."""
    try:
        lines = mounts_file.read_text().splitlines()
    except OSError:
        return False
    target = str(mountpoint.absolute())
    for line in lines:
        fields = line.split()
        # /proc/mounts escapes spaces in paths as \040.
        if (
            len(fields) >= 3
            and fields[1].replace("\\040", " ") == target
            and "gcalfuse" in fields[0]
        ):
            return True
    return False


def _check_mountpoint(mountpoint: Path) -> None:
    """Refuse (best effort) to mount somewhere that would hide or clash with data."""
    try:
        if not mountpoint.exists():
            return
        if _is_gcalfuse_mount(mountpoint):
            raise MountError(f"{mountpoint} is already mounted by gcalfuse.")
        if not mountpoint.is_dir():
            raise MountError(f"{mountpoint} exists and is not a directory.")
        if any(mountpoint.iterdir()):
            raise MountError(
                f"{mountpoint} is not empty. Refusing to mount over existing files; "
                "pick an empty directory."
            )
    except OSError as exc:
        if exc.errno == errno.ENOTCONN:
            raise MountError(
                f"{mountpoint} is a stale FUSE mount (a previous gcalfuse exited "
                f"uncleanly). Run `gcalfuse umount {mountpoint}` first."
            ) from exc
        raise MountError(f"Cannot use {mountpoint} as a mountpoint: {exc}") from exc


async def _serve_until_unmounted_or_signalled(pyfuse3, trio) -> None:
    """Run the FUSE loop; exit cleanly on SIGTERM/SIGHUP as well as SIGINT.

    Without this, `systemctl stop` or closing a terminal kills the process
    and leaves a stale "Transport endpoint is not connected" mount behind.
    """
    async with trio.open_nursery() as nursery:

        async def main_then_stop() -> None:
            await pyfuse3.main()
            nursery.cancel_scope.cancel()

        async def stop_on_signal() -> None:
            with trio.open_signal_receiver(signal.SIGTERM, signal.SIGHUP) as signals:
                async for signum in signals:
                    logger.info("received %s, unmounting", signal.Signals(signum).name)
                    nursery.cancel_scope.cancel()
                    return

        nursery.start_soon(main_then_stop)
        nursery.start_soon(stop_on_signal)


def _cmd_mount(args: argparse.Namespace, config: Config) -> int:
    import pyfuse3
    import trio

    from .fs import GcalfuseFS

    read_only = args.read_only or config.read_only
    mountpoint = _mountpoint(args, config)
    _check_mountpoint(mountpoint)
    mountpoint.mkdir(parents=True, exist_ok=True)

    cache, client = _build_cache(config)
    _initial_refresh(cache)

    fs_ops = GcalfuseFS(cache.index, client, read_only=read_only)
    fuse_options = set(pyfuse3.default_options)
    fuse_options.add("fsname=gcalfuse")
    if read_only:
        fuse_options.add("ro")

    try:
        pyfuse3.init(fs_ops, str(mountpoint), fuse_options)
    except RuntimeError as exc:
        raise MountError(
            f"Could not mount {mountpoint}: {exc}. Is FUSE available (/dev/fuse, "
            "the fuse3 package) and are you allowed to use it?"
        ) from exc

    logger.info("mounted %s (read_only=%s)", mountpoint, read_only)
    cache.start_background_refresh()
    try:
        trio.run(_serve_until_unmounted_or_signalled, pyfuse3, trio)
    except KeyboardInterrupt:
        pass
    finally:
        cache.stop_background_refresh()
        pyfuse3.close(unmount=True)
        logger.info("unmounted %s", mountpoint)
        for path in fs_ops.unsaved_scratch_files():
            logger.warning(
                "discarded %s: it was never renamed to a .ics event name, so it was not "
                "sent to Google",
                path,
            )
    return 0


def _cmd_umount(args: argparse.Namespace, config: Config) -> int:
    mountpoint = _mountpoint(args, config)
    # fuse3 ships `fusermount3`; many distros also provide `fusermount`.
    for tool in ("fusermount3", "fusermount"):
        if shutil.which(tool):
            break
    else:
        raise MountError("Neither fusermount3 nor fusermount is installed (package: fuse3).")

    result = subprocess.run([tool, "-u", str(mountpoint)], capture_output=True, text=True)
    if result.returncode != 0:
        raise MountError(f"{tool} -u {mountpoint} failed: {result.stderr.strip()}")
    return 0


def _cmd_ls_days(args: argparse.Namespace, config: Config) -> int:
    cache, _client = _build_cache(config)
    _initial_refresh(cache)
    index = cache.index
    for year in index.years():
        for month in index.months(year):
            for day in index.days(year, month):
                print(f"{year:04d}/{month:02d}/{day:02d}")
                for filename in index.files(year, month, day):
                    print(f"  {filename}")
    return 0


_HANDLERS = {
    "auth": _cmd_auth,
    "mount": _cmd_mount,
    "umount": _cmd_umount,
    "ls-days": _cmd_ls_days,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = Config.load(args.config)
        return _HANDLERS[args.command](args, config)
    except (auth.AuthError, ConfigError, MountError) as exc:
        print(f"gcalfuse: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
