#!/usr/bin/env python3
"""minecraft-backups — scheduled, self-limiting backups of a Minecraft saves folder.

Design (full spec in plan.md; build steps in agents.md):
  * The launcher runs this as a *pre-command*. It backgrounds the real work and
    returns immediately so minecraft can start while the backup finishes.
  * Backups are created only when none exists within ``max_age`` hours (default 24).
  * Archives use ``tar --zstd`` with an atomic write (temp ``.part`` file + rename);
    a failed or interrupted run never leaves a partial ``*.tar.zst`` behind.
  * Only the newest ``keep`` backups are retained (default 5); older ones are pruned.
  * On completion a ``notify-send`` is emitted, gated on the desktop session.

Standard library only. No network.

This file starts as a skeleton. The real logic for each plan.md step is added in
order; the ``_STEPS`` markers below indicate where each belongs.
"""
from __future__ import annotations

import argparse
import glob
import logging
from collections import namedtuple
import os
import sys
import time

# --- defaults (§3 of plan.md) -------------------------------------------------
TOOL_NAME = "minecraft-backups"
DEFAULT_MAX_AGE_HOURS = 24.0
DEFAULT_KEEP = 5
DEFAULT_LOCK_TIMEOUT_SECONDS = 60.0
DEFAULT_BACKUP_DIR_SUFFIX = "backups"
DEFAULT_LOG_FILE_NAME = "backup.log"

logger = logging.getLogger(TOOL_NAME)

# Where each plan.md step adds its logic.
_STEPS = [
    "1 skeleton + conventions",
    "2 argument parsing",
    "3 path helpers + exit codes",
    "4 age gate",
    "5 locking + skip-if-running",
    "6 background launch",
    "7 worker archive",
    "8 prune",
    "9 completion + logging",
    "10 smoke test",
    "11 docs + launcher integration",
]


def now() -> float:
    """Wall-clock time in seconds; injectable so age tests are deterministic (§4)."""
    return time.time()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="minecraft-backups.py",
        description="Back up a Minecraft saves folder. Returns immediately; the backup "
                    "finishes in the background.",
        epilog="Run this as your launcher's pre-command. It exits as soon as the "
               "backup job is launched, so minecraft can start while it runs.",
    )
    parser.add_argument(
        "-f", "--folder", required=True, metavar="DIR",
        help="directory whose *contents* are archived (e.g. ~/.minecraft/saves)",
    )
    # --backup-dir and --log-file intentionally have no argparse default here: they are
    # resolved from --folder / --backup-dir in Step 3 (plan §3). The help text documents
    # their effective default so the parser matches plan.md and README.
    parser.add_argument(

        "-B", "--backup-dir", metavar="DIR",
        help="directory that receives archives; defaults to the parent of --folder "
             "with a 'backups' suffix",
    )
    parser.add_argument(
        "-a", "--max-age", type=float, metavar="HOURS",
        default=DEFAULT_MAX_AGE_HOURS,
        help="create a backup only if none is younger than this many hours (default 24)",
    )
    parser.add_argument(
        "-k", "--keep", type=int, metavar="N",
        default=DEFAULT_KEEP,
        help="number of newest backups to retain (default 5)",
    )
    parser.add_argument(
        "-t", "--lock-timeout", type=float, metavar="SECONDS",
        default=DEFAULT_LOCK_TIMEOUT_SECONDS,
        help="max seconds to wait for an already-running backup before skipping "
             "(default 60)",
    )
    parser.add_argument(
        "-l", "--log-file", metavar="PATH",
        help="log file path; defaults to <backup-dir>/backup.log",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="emit extra diagnostic lines on stderr",
    )
    return parser


def configure_logging(args: argparse.Namespace) -> None:
    handlers = [logging.StreamHandler(sys.stderr)]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file))
    logging.basicConfig(
        level=logging.INFO if not args.verbose else logging.DEBUG,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=handlers,
    )
class SetupError(Exception):
    """A foreground setup failure; the launcher sees a non-zero exit (§5)."""


def validate_args(args: argparse.Namespace) -> None:
    """Reject invalid argument *values*; argparse already rejects invalid *types*.

    This enforces the *range* so a value can actually be used, and raises
    SetupError on failure so the caller logs it and exits non-zero (§5).
    """
    if args.keep < 1:
        raise SetupError(f"--keep must be >= 1, got {args.keep}")
    if args.max_age <= 0:
        raise SetupError(f"--max-age must be > 0, got {args.max_age}")
    if args.lock_timeout < 0:
        raise SetupError(f"--lock-timeout must be >= 0, got {args.lock_timeout}")


Paths = namedtuple("Paths", "folder backup_dir log_file lock_path pid_path")


def resolve_paths(args: argparse.Namespace) -> Paths:
    """Resolve filesystem paths for this run (§3).

    Computes backup_dir/log_file/lock/pid and raises SetupError (§5) when the
    folder is unusable or the backup dir cannot be created.
    """
    folder = os.path.abspath(args.folder)
    if not os.path.isdir(folder):
        raise SetupError(f"folder is not a directory: {args.folder}")

    if args.backup_dir:
        backup_dir = os.path.abspath(args.backup_dir)
    else:
        parent = os.path.dirname(folder) or "."
        backup_dir = os.path.join(parent, DEFAULT_BACKUP_DIR_SUFFIX)

    if args.log_file:
        log_file = os.path.abspath(args.log_file)
    else:
        log_file = os.path.join(backup_dir, DEFAULT_LOG_FILE_NAME)

    lock_path = os.path.join(backup_dir, ".lock")
    pid_path = os.path.join(backup_dir, ".pid")

    try:
        os.makedirs(backup_dir, exist_ok=True)
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
    except OSError as exc:
        raise SetupError(f"cannot create backup directory {backup_dir!r}: {exc}") from exc
    return Paths(folder=folder, backup_dir=backup_dir, log_file=log_file, lock_path=lock_path, pid_path=pid_path)


def newest_backup(backup_dir: str) -> str | None:
    """Path of the newest `*.tar.zst` in backup_dir by mtime, else None.

    Empty/missing/unreadable backup_dir yields None so the caller proceeds.
    """
    try:
        names = glob.glob(os.path.join(backup_dir, "*.tar.zst"))
    except OSError:
        return None
    if not names:
        return None
    return max(names, key=os.path.getmtime)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args)
    try:
        validate_args(args)
    except SetupError as exc:
        logger.error("invalid arguments: %s", exc)
        return 2
    try:
        paths = resolve_paths(args)
    except SetupError as exc:
        logger.error("setup failed: %s", exc)
        return 2
    logger.info("resolved: folder=%s backup_dir=%s log_file=%s",
                paths.folder, paths.backup_dir, paths.log_file)

    # Step 4 (agents.md): age gate — skip if the newest backup is younger than max_age.
    newest = newest_backup(paths.backup_dir)
    if newest is not None and now() - os.path.getmtime(newest) < DEFAULT_MAX_AGE_HOURS * 3600.0:
        logger.info("skip recent backup %s", newest)
        return 0
    logger.info("skeleton: wiring plan.md steps %s; see agents.md for the sequence",
                ", ".join(_STEPS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
