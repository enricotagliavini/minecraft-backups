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
import re
import shutil
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
    "8 prune (done)",
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

def setup(args: argparse.Namespace) -> Paths | None:
    """Validate args and resolve paths for this run (§3 + §5).

    Shared by the foreground launcher and the detached worker's `_run` (§6) so both
    modes run identical setup. Returns Paths on success, or None if a SetupError was
    caught (already logged by the caller, which exits 2).
    """
    try:
        validate_args(args)
    except SetupError as exc:
        logger.error("setup failed: %s", exc)
        return None
    try:
        paths = resolve_paths(args)
    except SetupError as exc:
        logger.error("setup failed: %s", exc)
        return None
    logger.info("resolved: folder=%s backup_dir=%s log_file=%s",
                paths.folder, paths.backup_dir, paths.log_file)
    return paths


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
    (the lock stays held past ``lock_timeout``); 2 when it is a path/usage error:
    the lock file (backup_dir/.lock) cannot be created because the backup dir is not
    writable. The holder records its PID in backup_dir/.pid.
    """
    try:
        lock_fd = os.open(paths.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        logger.error("cannot open lock file %s: %s", paths.lock_path, exc)
        return 2

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
    # Written here (setup) and checked here — the single place that resolves run_paths.
    # Passed to _run(run_paths, args) so neither launch mode duplicates setup/check.
    run_paths = setup(args)
    if run_paths is None:
        return 2
    # --detach-worker / --background: this process is the setsid child the launcher
    # spawned; run the shared backup path inline. Its exit code is meaningless — no one
    # reads it (§5); the foreground path below keeps the real codes.
    if getattr(args, "launch", False):
        return _run(run_paths, args)
    # Skip-if-running (§8): briefly hold the flock. If a backup is already running, return
    # 20 without spawning a second worker. Otherwise launch the detached worker (Step 6)
    # and return immediately so the launcher can start minecraft.
    rc = acquire_lock(run_paths, args.lock_timeout)
    if rc != 0:
        return rc
    if not _launch_worker(run_paths, args):
        return 1
    return 0


def _launch_worker(paths: Paths, args: argparse.Namespace) -> bool:
    """Spawn the detached worker and return without waiting.

    The child is a new session (start_new_session, the Python equivalent of setsid) with
    stdin from /dev/null and stdout/stderr appended to log_file (the nohup role). It runs
    the SAME _run(args) path as this foreground process, so the two launch modes stay
    identical apart from the setsid; only this foreground process's exit code matters."""
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

def _fsync_file(path: str) -> None:
    """Flush a file's contents to disk before publishing it (Step 7); best-effort."""
    try:
        fd = os.open(path, os.O_WRONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass  # not all filesystems support fsync; skipping is harmless
    finally:
        os.close(fd)


def _fsync_dir(path: str) -> None:
    """fsync a directory so a rename-in is durable; best-effort."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _safe_remove(path: str) -> bool:
    """Best-effort remove: True if absent or removed, False on another error."""
    try:
        os.remove(path)
    except FileNotFoundError:
        return True
    except OSError as exc:
        logger.warning("cannot remove %s: %s", path, exc)
        return False
    return True


def make_archive(run_paths: Paths, args: argparse.Namespace) -> tuple[int, str]:
    """Create one archive with ``tar --zstd``, then publish it atomically (Step 7).

    The archive is named ``<YYYY-mm-dd_HH-MM-SS>.<pid>.<ts>.part`` (written by tar) and then
    ``os.replace``d to ``<...>.tar.zst`` — an atomic rename within the same directory, so a
    reader never observes a partial ``*.tar.zst`` (§5 hygiene). On any failure the ``.part``
    is removed first. Returns `(0, archive_path)` on success, `(1, None)` on failure.
    """
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    ts = int(time.time())
    base = f"{stamp}.{os.getpid()}.{ts}"
    part = os.path.join(run_paths.backup_dir, f"{base}.part")
    archive = os.path.join(run_paths.backup_dir, f"{base}.tar.zst")

    try:
        proc = subprocess.run(
            ["tar", "--zstd", "-C", run_paths.folder, "-cf", part, "."],
            check=False,
        )
    except OSError as exc:
        logger.error("cannot run tar --zstd: %s", exc)
        _safe_remove(part)
        return 1, None

    if proc.returncode != 0:
        logger.error("tar --zstd failed (exit %s) for folder %s",
                     proc.returncode, run_paths.folder)
        _safe_remove(part)
        return 1, None

    # tar wrote the .part; flush it before the atomic rename so the published
    # archive is complete and durable on disk.
    _fsync_file(part)
    try:
        os.replace(part, archive)  # atomic rename within the same directory
        _fsync_dir(run_paths.backup_dir)
    except OSError as exc:
        logger.error("cannot finalize archive %s: %s", archive, exc)
        _safe_remove(part)
        return 1, None

    logger.info("archive created: %s", archive)
    return 0, archive



# Archive/part names embed their creation time: <YYYY-mm-dd_HH-MM-SS>.<pid>.<ts>,
# then .tar.zst or .part. Matching only these exact shapes means prune deletes
# nothing but our own archives (never a stray file of the same glob prefix).
_ARCHIVE_RE = re.compile(r"^(?P<stamp>\d{4}-\d{2}-\d{2}_(?:\d{2}-\d{2}-\d{2}))\.\d+\.\d+\.tar\.zst$")
_PART_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_(?:\d{2}-\d{2}-\d{2})\.\d+\.\d+\.part$")


def _list_matches(bucket: str, regex: re.Pattern) -> list[str]:
    """List full paths of `bucket` entries whose basename matches `regex` (anchored) — never a bare glob."""
    return [os.path.join(bucket, n) for n in os.listdir(bucket) if regex.match(n)]


def _archive_stamp(name: str) -> str | None:
    """The `YYYY-mm-dd_HH-MM-SS` prefix of an archive name, or None for a stray.

    Fields are fixed-width and zero-padded, so this string's lexical order IS
    its chronological order; prune ranks by it and never by mtime (§8).
    """
    m = _ARCHIVE_RE.match(os.path.basename(name))
    return m.group("stamp") if m else None


def prune_dir(backup_dir: str, keep: int, exclude: str | None = None) -> int:
    """Keep the `keep` newest ``*.tar.zst`` archives and clear stale ``*.part`` files.

    Deletions are matched by name pattern only, so a stray file of the same glob prefix
    is left alone. The current archive (`exclude`) is never removed, even if a rename
    left it with a stale mtime (§8 hygiene). Best-effort: a failed removal is logged and
    prune continues; the function always returns 0 so it never blocks a backup.
    """
    if keep < 1:
        keep = 1  # never prune the just-created archive
    if exclude is not None:
        exclude = os.path.normpath(exclude)

    removed = 0
    archives = _list_matches(backup_dir, _ARCHIVE_RE)
    archives.sort(key=_archive_stamp, reverse=True)
    for old in archives[keep:]:
        if old != exclude and _safe_remove(old):
            removed += 1
    for part in _list_matches(backup_dir, _PART_RE):
        if _safe_remove(part):
            removed += 1
    if removed:
        kept = min(keep, len(archives))
        logger.info("pruned %d archive(s); keeping %d newest (%s)",
                    removed, kept, newest_backup(backup_dir))
    return 0


def _completion_summary(rc: int) -> str:
    """Human label for the §5 worker code, for notify-send and log lines."""
    return {
        0: "backup created",
        10: "skipped (recent backup)",
        20: "skipped (a backup is already running)",
        1: "hard error",
        2: "path/usage error",
    }.get(rc, f"exited with code {rc}")


def notify_completion(rc: int, archive: str | None, newest: str | None) -> None:
    """Gated desktop notify-send on completion (§9).

    Fires only when the desktop session is detected (XDG_SESSION_DESKTOP set) and the
    notify-send binary exists; otherwise the summary is skipped silently (no error).
    """
    binary = shutil.which("notify-send")
    if binary is None or not os.environ.get("XDG_SESSION_DESKTOP"):
        return
    message = f"{TOOL_NAME}: {_completion_summary(rc)}"
    if archive:
        message += f" - {archive}"
    elif newest:
        message += f" - {newest}"
    try:
        subprocess.run([binary, "-t", "10", message], check=False, capture_output=True)
    except OSError as exc:
        logger.warning("cannot notify-send: %s", exc)


def _cleanup_pidfile(pid_path: str) -> None:
    """Remove this worker's pidfile at the end of the run (§5). Harmless if absent."""
    _safe_remove(pid_path)


def _run(run_paths: Paths, args: argparse.Namespace) -> int:
    """The single shared backup path (§6 + Steps 7-9).

    Runs in the foreground of THIS process. With --detach-worker this process is the
    setsid child the foreground spawned, so a background and a foreground invocation
    share this exact code apart from the setsid. Holds the flock for the whole job; a
    second caller (or the foreground's pre-spawn check) is skipped via acquire_lock."""
    rc = acquire_lock(run_paths, args.lock_timeout)
    if rc != 0:
        return rc  # 20: a backup is already running
    archive = None  # assigned by make_archive; stays None on the skip or hard-error paths
    # Age gate — authoritative (§4). Skip (rc=10) if a backup exists within max_age.
    newest = newest_backup(run_paths.backup_dir)
    if newest is not None and now() - os.path.getmtime(newest) < args.max_age * 3600.0:
        logger.info("skip recent backup %s", newest)
        rc = 10
    else:
        # Steps 7-8: build the archive (rc=1 on hard error; the .part was removed on
        # failure, so rc=1 leaves no stray archive). Prune runs only when it succeeded.
        arc_rc, archive = make_archive(run_paths, args)
        if arc_rc != 0:
            rc = 1
        else:
            logger.info("backup created: %s", newest_backup(run_paths.backup_dir))
            prune_dir(run_paths.backup_dir, args.keep, exclude=archive)
            rc = 0

    # Step 9 completion: gated notify-send, the structured log line, pidfile cleanup;
    # return the §5 worker code.
    logger.info("completed rc=%d: %s", rc, _completion_summary(rc))
    notify_completion(rc, archive, newest)
    _cleanup_pidfile(run_paths.pid_path)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
