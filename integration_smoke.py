#!/usr/bin/env python3
"""Integration smoke test: verify the minecraft-backups detached-worker archive paths (checks 2/3/6).

Verified background (see investigation):
  * The foreground (no --detach-worker, no --log-file) spawns a detached worker
    (setsid) that runs _run inline. The foreground returns rc=0 within ~0.04s; the
    REAL backup timing is the worker's, read from its pidfile.
  * Worker logs land in <backup_dir>/backup.log because the worker's stdout+stderr are
    redirected (log_stream) into that file. Do NOT pass --log-file: configure_logging runs
    before setup() creates backup_dir, so a FileHandler would crash with FileNotFoundError.
  * The script path MUST be absolute (relative + cwd=folder fails to find it).
  * Each check uses a UNIQUE top-level parent (mktemp -d) so backup_dir=parent/backups is
    unique and clean; leftover archives never trigger the rc=10 "recent backup" skip.
  * A tar failure (unreadable file, uid 1000) makes tar exit 2 -> make_archive returns rc=1
    (hard error); the worker logs "completed rc=1" and make_archive removes the .part.

Usage: python3 -B integration_smoke.py
"""
import glob
import datetime
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time

# minecraft-backups.py lives in the repo root. Resolve it from here whether this test
# is promoted to the repo root or lives in a subdirectory (e.g. a tests/ dir).
HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = HERE if os.path.isfile(os.path.join(HERE, "minecraft-backups.py")) else os.path.dirname(HERE)
SCRIPT = os.path.abspath(os.path.join(REPO_ROOT, "minecraft-backups.py"))
PASS, FAIL = 0, 0
RESULTS = []


def ok(msg):
    global PASS
    PASS += 1
    RESULTS.append(("ok", msg))
    print(f"  [PASS] {msg}")


def bad(msg):
    global FAIL
    FAIL += 1
    RESULTS.append(("bad", msg))
    print(f"  [FAIL] {msg}")


def pkill_workers():
    """Kill any leftover detached workers so none holds a lock between checks."""
    subprocess.run(["pkill", "-9", "-f", "minecraft-backups"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)


def fresh_folder(n=600, real=True, sparse_mb=1):
    """Return (folder, backup_dir). Unique top-level parent per check (unique backup_dir).
    real -> real random bytes (exercises real read time); not real -> sparse (st_blocks=0).
    """
    parent = subprocess.check_output(["mktemp", "-d"]).decode().strip()
    folder = os.path.join(parent, "root")
    os.makedirs(folder)
    backup_dir = os.path.join(os.path.dirname(folder), "backups")
    for i in range(n):
        p = os.path.join(folder, f"f{i}.dat")
        if real:
            with open(p, "wb") as fh:
                fh.write(os.urandom(2 * 1024 * 1024))  # ~2MB real random -> st_blocks > 0
        else:
            with open(p, "wb") as fh:
                fh.write(b"\0" * (sparse_mb * 1024 * 1024))
            os.truncate(p, 0)  # sparse zero-fill: st_blocks == 0
    shutil.rmtree(backup_dir, ignore_errors=True)
    return folder, backup_dir


def _log_span_seconds(log):
    """Return the worker's own time (seconds) parsed from its log timestamps, or 0.0.
    (timestamp of the 'completed rc=N' line) minus (timestamp of the worker's first log line).
    Worker log lines are ISO-stamped like '2026-11-10 16:08:42,309 INFO    message'.
    """
    lines = [l for l in log.splitlines() if l.strip()]
    if not lines:
        return 0.0
    end_ts = None
    for l in lines:
        if "completed rc=" in l:
            end_ts = l
            break
    if end_ts is None:
        return 0.0
    fmt = "%Y-%m-%d %H:%M:%S,%f"
    ts_re = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},?\d+"
    try:
        end_dt = datetime.datetime.strptime(re.match(ts_re, end_ts).group(0), fmt)
        start_dt = datetime.datetime.strptime(re.match(ts_re, lines[0]).group(0), fmt)
        return (end_dt - start_dt).total_seconds()
    except Exception:
        return 0.0
def run_worker(folder, keep=None):
    """Run the foreground and wait for the detached worker.
    Return (worker_done, log, rc, dt, stderr).
    dt = the WORKER's own time, parsed from its log timestamps. The foreground exits
    rc=0 immediately after setsid-spawning the worker, so its wall time (~0.13s) is not
    the read time we care about; we instead use (timestamp of 'completed rc=N') minus
    (timestamp of the worker's first log line).
    """
    cmd = [sys.executable, SCRIPT, folder, "-t", "30"]
    bd = os.path.join(os.path.dirname(folder), "backups")
    if keep is not None:
        cmd += ["--keep", str(keep)]
    p = subprocess.run(
        cmd,
        cwd=folder, capture_output=True, text=True, timeout=600,
    )
    wpid = open(os.path.join(bd, ".pid")).read().strip() if os.path.exists(os.path.join(bd, ".pid")) else ""
    while wpid and os.path.exists(f"/proc/{wpid}"):
        time.sleep(0.02)
    with open(os.path.join(bd, "backup.log")) as fh:
        log = fh.read()
    dt = _log_span_seconds(log) or 0.0
    return (wpid == "" or not os.path.exists(f"/proc/{wpid}")), log, p.returncode, dt, p.stderr


def archives(bd):
    arts = glob.glob(os.path.join(bd, "*.tar.zst"))
    return sorted(arts), sum(os.path.getsize(a) for a in arts)


def verify_archive_list(bd, expected_n):
    """List the archive's member file names and confirm the set matches the source.
    Intentionally does NOT compare byte content: tar/zstd reliability is out of scope.
    """
    arts = glob.glob(os.path.join(bd, "*.tar.zst"))
    if len(arts) != 1:
        return False
    try:
        with tarfile.open(arts[0]) as tf:
            names = {n[2:] if n.startswith("./") else n for n in (m.name for m in tf.getmembers() if m.isfile())}
    except Exception:
        return False
    return names == {f"f{i}.dat" for i in range(expected_n)}


def check2_unreadable_file():
    print("check 2: unreadable file fails the worker -> rc=1, no archive, no .part")
    pkill_workers()
    folder, bd = fresh_folder(n=2, real=False)
    secret = os.path.join(folder, "secret.txt")
    open(secret, "wb").write(b"y" * 1024)
    os.chmod(secret, 0o000)
    done, log, rc, dt, stderr = run_worker(folder)
    loglines = (log + stderr).splitlines()
    assert any("completed rc=" in l for l in loglines), "worker did not complete/log: " + (log + stderr).strip()[-300:]
    assert "rc=1" in (log + stderr) or "rc=3" in (log + stderr), \
        f"expected hard error rc=1/3, tail: {(log + stderr).strip()[-300:]}"
    arts, _ = archives(bd)
    parts = glob.glob(os.path.join(bd, "*.part"))
    ok(f"worker hard-failed ({'rc=1' if 'rc=1' in (log + stderr) else 'rc=3'}); no archive ({len(arts)}), "
       f"no .part ({len(parts)}); worker_done={done}")


def check3_archive_member_set():
    print("check 3: worker creates an archive listing every source file (N files)")
    pkill_workers()
    n = 5  # minimal: 5 sparse files, st_blocks==0; enough to confirm the member set
    folder, bd = fresh_folder(n=n, real=False, sparse_mb=1)
    done, log, rc, dt, stderr = run_worker(folder)
    assert any("completed rc=0" in l for l in (log + stderr).splitlines()), \
        "worker did not complete rc=0: " + (log + stderr).strip()[-200:]
    arts = glob.glob(os.path.join(bd, "*.tar.zst"))
    assert len(arts) == 1, f"expected 1 archive, got {len(arts)}"
    assert verify_archive_list(bd, n), "archive member list did not match the source"
    ok(f"worker completed rc=0; archive lists exactly the {n} source files (member set verified, bytes out of scope)")


def check6_failure_cleanup_with_archive():
    print("check 6: failing backup cleans up (no stray .part) even with a prior archive")
    pkill_workers()
    folder, bd = fresh_folder(n=2, real=False)
    os.makedirs(bd, exist_ok=True)
    good = os.path.join(bd, "prior.tar.zst")
    open(good, "wb").write(b"prior")
    secret = os.path.join(folder, "secret.txt")
    open(secret, "wb").write(b"y" * 1024)
    os.chmod(secret, 0o000)
    done, log, rc, dt, stderr = run_worker(folder)
    loglines = (log + stderr).splitlines()
    assert any("completed rc=" in l for l in loglines), "worker did not complete/log"
    arts, _ = archives(bd)
    parts = glob.glob(os.path.join(bd, "*.part"))
    assert not parts, f"stray .part left behind: {parts}"
    assert any("prior.tar.zst" in a for a in arts), "prior archive was not preserved"
    ok(f"failing backup left no .part ({len(parts)}); no new archive ({len(arts)}); "
       f"prior archive intact")


def main():
    pkill_workers()
    try:
        check2_unreadable_file()
        check3_archive_member_set()

        check6_failure_cleanup_with_archive()
    except AssertionError as e:
        print(f"\n[ASSERT] {e}")
    except Exception as e:
        print(f"\n[ERROR] {type(e).__name__}: {e}")
    print(f"\n{PASS} passed, {FAIL} failed, {PASS + FAIL} checks")
    # clean up any workers/parents left behind
    pkill_workers()
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
