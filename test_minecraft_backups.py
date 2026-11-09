"""Deterministic unit tests for minecraft-backups (§10).

The runtime (minecraft-backups.py) is stdlib-only; these tests are a dev dependency
and use pytest. They exercise only the seams that are deterministic: argument
validation, path resolution, the age gate, and the flock. Every check maps to a
behavior in plan.md §3–§5. See agents.md "Validation".

The module is imported by file path because its name has a hyphen (`minecraft-backups`)
and is not importable by name.
"""
import os
import importlib.util

import fcntl
import pytest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "minecraft-backups", os.path.join(os.path.dirname(os.path.abspath(__file__)), "minecraft-backups.py")
)
mb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mb)


def _make_backup(tmp_path, mtime, monkeypatch):
    """A saves folder with one archive at `mtime`; now() is fixed to `mtime + 3600`.
    Returns the --folder path for main()."""
    saves = tmp_path / "saves"
    saves.mkdir()
    backups = tmp_path / "backups"  # backup_dir is the parent of --folder
    backups.mkdir()
    (backups / "backup.tar.zst").write_bytes(b"x")
    os.utime(str(backups / "backup.tar.zst"), (float(mtime), float(mtime)))
    monkeypatch.setattr(mb, "now", lambda: float(mtime) + 3600.0)
    return str(saves)


# --- Argument parsing (plan.md §3) -------------------------------------------


def test_parser_defaults(tmp_path):
    args = mb.build_parser().parse_args(["-f", str(tmp_path)])
    assert args.folder == str(tmp_path)
    assert args.max_age == 24.0
    assert args.keep == 5
    assert args.lock_timeout == 60.0


def test_parser_requires_folder():
    with pytest.raises(SystemExit):
        mb.build_parser().parse_args(["-f"])


def test_parser_rejects_unknown_flag(tmp_path):
    with pytest.raises(SystemExit):
        mb.build_parser().parse_args(["-f", str(tmp_path), "--nope"])


def test_parser_detach_dest_is_launch(tmp_path):
    assert mb.build_parser().parse_args(["-f", str(tmp_path), "--detach-worker"]).launch is True


# --- Value validation (plan.md §3/§5) ----------------------------------------


def test_keep_zero_rejected(tmp_path):
    args = mb.build_parser().parse_args(["-f", str(tmp_path), "-k", "0"])
    with pytest.raises(mb.SetupError):
        mb.validate_args(args)


def test_max_age_zero_rejected(tmp_path):
    args = mb.build_parser().parse_args(["-f", str(tmp_path), "-a", "0"])
    with pytest.raises(mb.SetupError):
        mb.validate_args(args)


def test_lock_timeout_negative_rejected(tmp_path):
    args = mb.build_parser().parse_args(["-f", str(tmp_path), "-t", "-1"])
    with pytest.raises(mb.SetupError):
        mb.validate_args(args)


def test_invalid_args_exit_2(tmp_path):
    # Bad value survives argparse (0 is a valid int) and is caught by setup -> 2.
    assert mb.main(["-f", str(tmp_path), "-k", "0"]) == 2


# --- Path resolution (plan.md §3) --------------------------------------------


def test_default_paths(tmp_path):
    # backup_dir is the PARENT of --folder, with a "backups" suffix (§2/§3).
    saves = tmp_path / "saves"
    saves.mkdir()
    paths = mb.setup(mb.build_parser().parse_args(["-f", str(saves)]))
    assert paths.folder == str(saves)
    assert paths.backup_dir == str(tmp_path / "backups")
    assert paths.log_file == str(tmp_path / "backups" / "backup.log")
    assert paths.lock_path == str(tmp_path / "backups" / ".lock")
    assert paths.pid_path == str(tmp_path / "backups" / ".pid")


def test_backup_dir_override(tmp_path):
    saves = tmp_path / "saves"
    saves.mkdir()
    paths = mb.setup(mb.build_parser().parse_args(["-f", str(saves), "-B", str(tmp_path / "custom")]))
    assert paths.backup_dir == str(tmp_path / "custom")


def test_missing_folder_exit_2(tmp_path):
    missing = tmp_path / "nope" / "deep" / "saves"
    assert mb.main(["-f", str(missing)]) == 2


# --- newest_backup (§4) ------------------------------------------------------


def test_newest_backup_picks_newest(tmp_path):
    bd = tmp_path / "backups"
    bd.mkdir()
    old = bd / "old.tar.zst"
    new = bd / "new.tar.zst"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    os.utime(str(old), (1000.0, 1000.0))
    assert mb.newest_backup(str(bd)).endswith("new.tar.zst")


# --- Age gate via main --detach-worker (§4) ----------------------------------


def test_no_backup_proceeds(tmp_path):
    # Fresh folder, real clock, no archives -> nothing to skip.
    saves = tmp_path / "saves"
    saves.mkdir()
    assert mb.main(["-f", str(saves), "--detach-worker", "-a", "24"]) == 0


def test_recent_backup_skips(tmp_path, monkeypatch):
    # 1h old < 24h max_age -> age gate skips.
    saves = _make_backup(tmp_path, 1_000_000.0, monkeypatch)
    assert mb.main(["-f", saves, "--detach-worker", "-a", "24"]) == 10


def test_old_backup_proceeds(tmp_path, monkeypatch):
    # ~27h old > 24h -> age gate does not skip.
    saves = _make_backup(tmp_path, 1_000_000.0, monkeypatch)
    monkeypatch.setattr(mb, "now", lambda: 1_000_000.0 + 100_000.0)
    assert mb.main(["-f", saves, "--detach-worker", "-a", "24"]) == 0


def test_age_exactly_max_age_proceeds(tmp_path, monkeypatch):
    # age == max_age is NOT "< max_age", so it proceeds (boundary of §4).
    saves = _make_backup(tmp_path, 500_000.0, monkeypatch)
    monkeypatch.setattr(mb, "now", lambda: 500_000.0 + 24 * 3600.0)
    assert mb.main(["-f", saves, "--detach-worker", "-a", "24"]) == 0


# --- Locking / skip-if-running (plan.md §5/§8) -------------------------------


def test_free_lock_is_released_and_writes_pidfile(tmp_path):
    saves = tmp_path / "saves"
    saves.mkdir()
    paths = mb.setup(mb.build_parser().parse_args(["-f", str(saves)]))
    assert mb.acquire_lock(paths, lock_timeout=1.0) == 0
    # A second acquire is also free (no leak), and a pid is recorded.
    assert mb.acquire_lock(paths, lock_timeout=1.0) == 0
    assert int(Path(paths.pid_path).read_text().strip()) > 0


def test_stale_pid_is_ignored(tmp_path):
    # flock, not the pidfile, decides; a dead pid must not block a fresh lock.
    saves = tmp_path / "saves"
    saves.mkdir()
    paths = mb.setup(mb.build_parser().parse_args(["-f", str(saves)]))
    Path(paths.pid_path).write_text("999999")
    assert mb.acquire_lock(paths, lock_timeout=1.0) == 0


def test_held_lock_exits_20(tmp_path):
    # A backup already running: the foreground pre-spawn check returns 20.
    saves = tmp_path / "saves"
    saves.mkdir()
    paths = mb.setup(mb.build_parser().parse_args(["-f", str(saves)]))
    lock_fd = os.open(paths.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        assert mb.main(["-f", str(saves), "-t", "0.2"]) == 20
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
