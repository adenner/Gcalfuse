"""gcalfuse command-line interface."""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gcalfuse",
        description="Mount Google Calendar as a filesystem of .ics files.",
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

    subparsers.add_parser("umount", help="Unmount the calendar filesystem.")
    subparsers.add_parser("ls-days", help="List cached dates and filenames (debug).")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "auth":
        raise NotImplementedError("gcalfuse auth is implemented in phase 2")
    if args.command == "mount":
        raise NotImplementedError("gcalfuse mount is implemented in phase 2")
    if args.command == "umount":
        raise NotImplementedError("gcalfuse umount is implemented in phase 2")
    if args.command == "ls-days":
        raise NotImplementedError("gcalfuse ls-days is implemented in phase 2")

    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
