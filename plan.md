# Minecraft saves backup — implementation plan

Status: **plan only**. No code will be written until you approve a step.
Language: **Python 3 (standard library only)** + Linux tools (`tar --zstd`).
Target: Fedora 44, unprivileged user, GUI desktop (notify-send).

---

## 0. Decisions locked before coding

| Requirement | Decision | Why |
|---|---|---|
| Language | Python 3, stdlib only | Clean age math, locking, path handling, signals; user allows deps but they're not needed |
| Archiving | `tar --zstd` (Linux tool) | User's explicit preference; GNU tar 1.35 on F44 supports it (verified) |
| "Don't block minecraft" | Foreground script **backgrounds** the heavy work with `setsid` + `nohup`, writes pidfile, returns 0 immediately | Launchers expect the pre-command to finish fast. Detach = survives launcher killing the process group when the pre-command returns |
| Age gate | Newest backup mtime vs `now - max-age`; else skip | Meets the "no backup if <24h" rule |
| Retention | Keep the `keep` newest, ranked by name-stamp prefix (fixed-width); also clear stale `.part` | Ranks by filename, not mtime — so a reshuffled dir can't pick the wrong set (§8) |
| Backup dir | `<parent-of-folder>/backups`; override with `--backup-dir` | Matches "one level up, called backups" |
| Return semantics | 0 if *setup* succeeded (backup created/skipped/already-running); non-zero only if setup itself failed | Backup runs in background regardless; blocking on archive outcome would defeat the whole point |
| Notification & hygiene | `notify-send` on completion; auto-gated by `XDG_SESSION_DESKTOP` + binary presence; archives atomic (never partial) |
| Concurrency | `flock`-guarded worker + pidfile; a second concurrent run skips | Launcher clicks twice, or a second run skips while the worker holds the lock |

---

## 1. Verified environment

- `tar (GNU tar) 1.35`, `--zstd` works → `/usr/bin/tar`
- `/usr/bin/zstd`, `/usr/bin/notify-send`, `/usr/bin/flock`, `/usr/bin/setsid`, `/usr/bin/nohup` all present
- Python 3.14.10 at `/usr/bin/python3`
- **Nothing needs `dnf` today.** (If `tar --zstd` were missing on another host, package = `tar`.)

---

## 2. Requirements → design (short)

- Launcher pre-command = our script. Script must **return quickly** so the launcher starts minecraft.
- The real work (age-check, `tar --zstd`, prune, `notify-send`) runs in a **detached** session that survives the launcher terminating the pre-command's process group.
- Background and foreground share the SAME `_run` code (only setsid-detached vs inline differ).
 The background worker is the **authority** on skip-vs-create; the lock serializes real work.

---

## 3. CLI spec

| Option | Default | Meaning |
|---|---|---|
| `-f, --folder` | *(required)* | Directory whose **contents** are archived |
| `-B, --backup-dir` | `<parent-of-folder>/backups` | Where archives are written |
| `-a, --max-age` | `24` (hours) | Only create if no backup is younger than this |
| `-k, --keep` | `5` | Number of newest backups to retain |
| `-t, --lock-timeout` | `60` (s) | How long to wait for a running backup before skipping |
| `-l, --log-file` | `<backup-dir>/backup.log` | Where logs go |
| `-v, --verbose` | off | Extra log lines to stderr |
| `-h, --help` | | Usage |
Note on notifications (no CLI flag): the detached worker emits `notify-send` on completion. It runs only when the desktop session is detected (`XDG_SESSION_DESKTOP` set) and the `notify-send` binary exists; otherwise it is skipped silently (no error). Archives are written atomically (Steps 7-8), so a failed backup leaves no `*.tar.zst` behind.

---

## 4. File layout

```
minecraft-backups/
  minecraft-backups.py        # the whole tool (one file, stdlib only)
  plan.md                     # this document
  agents.md                   # agent instructions + step list
```

---

## 5. Exit-code contract

**Foreground (launcher-visible):** `0` = setup OK (backup created, skipped, or not needed); non-zero = setup failed (folder missing, no binaries, cannot create backup dir).

**Detached worker** (logged to file; surfaced to user via notify-send):
- `0` created backup | `10` skipped (recent backup exists) | `20` skipped (a backup is already running) | `1` hard error (tar failed, etc.) | `2` path/usage error
- Hygiene: a failed/partial archive leaves **no** `*.tar.zst` behind (Steps 7-8).

---

## 6. Flow (foreground → background)

```mermaid
sequenceDiagram
    autonumber
    participant L as Launcher
    participant S as minecraft-backups.py (foreground)
    participant W as setsid worker — runs _run
    participant F as filesystem

    L->>S run script --folder <dir>
    S->>S resolve paths / args
    S->>S check lock briefly: another run in progress → return 20
    alt lock free
        S->>W setsid nohup python3 … --detach-worker &
        S->>F write pidfile
        S-->>L return 0     # launches, returns immediately so minecraft starts
    end

    Note over W: ONE shared _run — this is also what `--detach-worker` runs inline
    W->>W age gate: newest vs now - max_age → skip (exit 10) if recent
    alt no recent backup
        W->>F tar --zstd -> archive
        W->>W prune to keep N
        W->>F notify-send result
        W-->>L done (exit 0)
    end
```

---

## 7. Step list (drive one at a time)

Each step is small, self-contained, verifiable, and safe to commit before the next.

### Step 1 — Skeleton + project conventions
Create `minecraft-backups.py`, `plan.md`, `agents.md`, a `README.md`.
- `#!/usr/bin/env python3`, stdlib only, no network.
- Module-level constants: defaults from §3.
- Small README with a **manual run example** for the launcher.

### Step 2 — Argument parsing (§3)
- `argparse` in `minecraft-backups.py`; required `-f/--folder`.
- Returns parsed options; unknown args fail cleanly.

### Step 3 — Path helpers + exit-code contract
- Compute `backup_dir` (parent + `/backups`), fall back to `--backup-dir`.
- Compute `log_file` (parent of backup_dir).
- `mkdir -p` backup_dir/log_file's parents. Fail (non-zero) if impossible.
- Lock-file path = backup_dir/.lock; pid-file path = backup_dir/.pid.

### Step 4 — Age gate (inside the shared `_run`)
- Find newest backup in `backup_dir` (`*.tar.zst`, mtime).
- If `now - mtime < max_age` → log skip, exit `10`; else proceed. The SAME `_run` is the background worker's real work, so the foreground launcher never checks age — it spawns `_run` and returns `0`.
- Unit: deterministic by overriding the clock (inject `now` via a module-level function defaulting to `time.time()`).

### Step 5 — Locking + skip-if-running
- `flock`-guarded critical section (see §8).
- If a worker is running (pidfile alive, or lock held) and older than `lock-timeout` → log skip, exit `20`.

### Step 6 — Background launch (foreground returns fast)
- Spawn the worker **detached**: `setsid` into a new session + `nohup`, redirect to log_file, `start_new_session=True`. The spawned worker runs the SAME `_run` that `--detach-worker` runs inline.
- Write pidfile with child PID.
- **Return 0 immediately**, do not `wait()`.
- This is the crux of "don't block minecraft." Verify it returns at once.

### Step 7 — Worker: re-check age + archive — DONE (verified)
- Inside the locked critical section: re-check age (the ONE shared check — the same code the foreground relies on via the spawned worker). If recent → exit `10`.
- Build the archive name `<YYYY-mm-dd_HH-MM-SS>.<pid>.<ts>.part`, then run `tar --zstd -C <folder> -cf <part> .` via `subprocess`.
- Flush + close the archive (fsync the file if supported), then `os.replace(part, archive)` — an atomic rename, so readers never see a partial file.
- On any error: log, remove the `.part` file, exit `1`. On success: exit `0`, then continue to prune.
- **Verified**: `_run` via `--detach-worker` (large max-age) → rc `0`, exactly one `<YYYY-mm-dd_HH-MM-SS>.<pid>.<ts>.tar.zst`, no `.part`; archive parses as a valid zstd tar with `./player.dat`, `./level.dat`. Tar failure (nonexistent folder) → rc `1`, no `.tar.zst`, no `.part`. `make_archive` wired into `_run` after the age gate; Step 8 is implemented and verified (see below).

### Step 8 — Worker: prune to keep N — DONE (verified)
- Rank `*.tar.zst` by name-stamp prefix `YYYY-mm-dd_HH-MM-SS` (fixed-width, so lexical == chronological), never by mtime — a reshuffled/copy-reshuffled dir can't fool the selection (§8).
- Keep the `keep` newest; delete the rest, guarding the just-created archive (`exclude`) by name so the atomic `os.replace` can't evict it.
- Also clear stale `.part` files, matched by the exact `stamp.pid.ts.part` shape (never a same-prefix `.zst.stale` stray — anchored regex).
- Best-effort: a failed removal is logged; prune always returns 0 so it never blocks a backup.
- **Verified**: 5 archives with mtimes in REVERSE name order — `prune_dir(keep=3)` keeps the 3 newest BY NAME plus the name-excluded oldest, deletes only the middle one, clears a planted `.part`, leaves a same-prefix `.zst.stale` untouched. Guarded by permanent test `test_prune_keeps_newest_by_name_not_mtime`.

### Step 9 — Worker: completion + logging
- Emit `notify-send` summary (created/skipped/failed) only when the desktop session is detected (`XDG_SESSION_DESKTOP` set) and the `notify-send` binary exists; otherwise skip silently (no error).
- Always append a structured line to log_file; exit with §5 worker code.
- Cleanup pidfile at the end.

### Step 10 — Smoke test (no real minecraft)
Drive the script end-to-end with a fake slow folder:
1. Return-immediately: launch, confirm foreground returns fast while worker runs.
2. Create: after worker logs, confirm `*.tar.zst` exists, verify with `tar --zstd -t` and `zstd -t`.
3. Skip by age: `--max-age 0` should skip when a recent backup exists.
4. Prune: create 5 archives with name-stamps spanning a range, set their mtimes in REVERSE name order (oldest name = newest mtime), then `--keep 3` keeps the 3 newest BY NAME (n3,n4,n5) plus the name-excluded n1 and deletes only n2 — proving name ranking, not mtime; also clears a planted `.part` and leaves a same-prefix `.zst.stale` stray untouched.
5. Detach survives: launch parent, then kill parent — child still alive and worker done.
6. Desktop + notify: confirm `notify-send` fires only when `XDG_SESSION_DESKTOP` is set and the `notify-send` binary exists (unset the var → no notification, no error; missing binary → skip).
6b. Hygiene: a failed `tar` leaves **no** `*.tar.zst` behind — only a removed `.part`/nothing.

### Step 11 — Docs + launcher integration
- Extend README: exact pre-command string, folder path, troubleshooting.
- Note the assumption: launcher must let the pre-command return; `setsid` covers launchers that kill the pre-command's process group.
   - Document the atomic-write guarantee in README: archives appear atomically, so a crash mid-archive leaves no corrupt `*.tar.zst`; at worst an incomplete `.part` is cleaned.

---

## 8. Locking detail (for Step 5/Step 7)

Use the `flock` binary or Python's `fcntl.flock` on the lock-file fd:
- Acquire the lock **inside the worker** (and in the foreground before spawning, briefly).
- If held by another PID that is still alive → skip (`20`).
- If held but that PID is dead (stale) → `flock` auto-releases on close/exit, so it's safe to take.
- `lock-timeout` bounds how long the foreground waits before reporting skip.

---

## 9. Known tradeoffs / open questions (you decide)

1. **Decided**: desktop notifications are automatic — `notify-send` on completion runs only when `XDG_SESSION_DESKTOP` is set and the `notify-send` binary exists, otherwise silently skipped (no error). There is **no** `--no-notify` option.
2. **Decided**: backup dir is the **sibling** of the saves folder (`<parent>/backups`); archive the folder's *contents* (so the folder can be renamed later).
3. **Decided**: archive failure → log + `notify-send`, and we still do **not** block minecraft.
4. Open: should each archive be checksummed (sha256) and recorded in log_file as optional hardening? (Not required for this round.)

Approve the step that interests you first; I'll implement exactly that step, verify it, then stop.
