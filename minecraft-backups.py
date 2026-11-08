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
import fcntl
import glob
import logging
from collections import namedtuple
import os
import sys
import subprocess
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
        "--detach-worker", "--background", dest="launch", action="store_true",
        help="run as the detached worker (invoked by the launcher); the foreground path is skipped and the archive is built inside this session",
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


def _read_pid(pid_path: str) -> int | None:
    """Return the PID in pid_path, or None if unreadable/empty."""
    try:
        with open(pid_path) as fh:
            return int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return None


def _write_pid(pid_path: str, pid: int) -> None:
    """Record pid in pid_path, logging (once) if it cannot be written."""
    try:
        with open(pid_path, "w") as fh:
            fh.write(str(pid))
    except OSError as exc:
        logger.warning("cannot write pidfile %s: %s", pid_path, exc)


def acquire_lock(paths: Paths, lock_timeout: float) -> int:
    """Take the flock on backup_dir/.lock (plan.md §8, Step 5).

    Returns 0 when we now hold the lock; 20 when a backup is already running
    (the lock stays held past ``lock_timeout``); 8 if the lock file cannot be
    created. The holder records its PID in backup_dir/.pid.
    """
    try:
        lock_fd = os.open(paths.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        logger.error("cannot open lock file %s: %s", paths.lock_path, exc)
        return 8

    pid = _read_pid(paths.pid_path)

    deadline = time.time() + lock_timeout
    while True:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() >= deadline:
                break
            time.sleep(0.25)

    if time.time() > deadline:
        # A blocking timeout means a live process holds the lock; the OS
        # releases flock on exit, so flock would succeed if the owner were dead.
        logger.info("skip: backup already running (lock held %ss, pid=%s)",
                    lock_timeout, pid)
        os.close(lock_fd)
        return 20
    _write_pid(paths.pid_path, os.getpid())
    logger.info("lock taken by pid %s (timeout %ss)", os.getpid(), lock_timeout)
    os.close(lock_fd)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args)

    # Step 6 (agents.md): a detached worker is a separate process that re-parses to its
    # own code path below; the foreground launcher path is skipped when --launch is set.
    if getattr(args, "launch", False):
        return _worker(args)
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
    if newest is not None and now() - os.path.getmtime(newest) < args.max_age * 3600.0:
        logger.info("skip recent backup %s", newest)
        return 0

    # Step 5 (agents.md): lock + skip-if-running. Briefly take the flock; if a backup is
    # already running, the launcher returns 20 without spawning a worker (§8).
    rc = acquire_lock(paths, args.lock_timeout)
    if rc != 0:
        return rc
    # Step 6 (agents.md): launch the detached worker, then return immediately. The worker
    # (its own session, stdin from /dev/null, I/O to log_file) is the authority on
    # skip-vs-create; the foreground fast-path above must agree with it.
    if not _launch_worker(paths, args):
        return 1
    return 0


def _launch_worker(paths: Paths, args: argparse.Namespace) -> bool:
    """Step 6 (agents.md): spawn the detached worker and return 0 without waiting.

    The child is a new session (start_new_session, the Python equivalent of setsid) with
    stdin from /dev/null and stdout/stderr appended to log_file (the nohup role); it is the
    authority on the backup and survives the launcher terminating its process group."""
    script = sys.argv[0] if sys.argv[:1] else os.path.abspath(__file__)
    cmd = [sys.executable, "-B", script, *sys.argv[1:], "--detach-worker"]
    try:
        with open(paths.log_file, "ab") as log_stream:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
    except OSError as exc:
        logger.error("cannot launch worker: %s", exc)
        return False
    _write_pid(paths.pid_path, proc.pid)
    logger.info("launched worker pid=%s (skip-if-running logs to %s)", proc.pid, paths.log_file)
    return True


def _worker(args: argparse.Namespace) -> int:
    """Step 6 real work (agents.md §7 onward). Re-acquire the lock for the critical section,
    re-check the age gate (the worker is the authority), then build the archive. Steps 7-9
    add archive/prune/notify; until then this is a minimal placeholder that still performs the
    authoritative lock + age re-check so detach behavior is verifiable."""
    configure_logging(args)
    try:
        paths = resolve_paths(args)
    except SetupError as exc:
        logger.error("setup failed: %s", exc)
        return 2
    logger.info("worker: re-checking age for backup_dir=%s under lock", paths.backup_dir)
    rc = acquire_lock(paths, args.lock_timeout)
    if rc != 0:
        return rc  # 20: a backup is already running
    newest = newest_backup(paths.backup_dir)
    if newest is not None and now() - os.path.getmtime(newest) < args.max_age * 3600.0:
        logger.info("worker: skip recent backup %s", newest)
        return 10
    # Placeholder for the Steps 7-9 backup work (tar --zstd, prune, notify).
    logger.info("worker: placeholder backup work (Steps 7-9 not yet implemented); simulating ~3s")
    time.sleep(3.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
