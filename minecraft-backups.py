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
import logging
import sys
import time

# --- defaults (§3 of plan.md) -------------------------------------------------
TOOL_NAME = "minecraft-backups"
DEFAULT_MAX_AGE_HOURS = 24.0
DEFAULT_KEEP = 5
DEFAULT_LOCK_TIMEOUT_SECONDS = 60.0
DEFAULT_BACKUP_DIR_SUFFIX = "backups"

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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args)
    logger.info("skeleton: wiring plan.md steps %s; see agents.md for the sequence",
                ", ".join(_STEPS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
