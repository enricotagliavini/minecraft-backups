# agents.md — instructions for the coding agent (me)

This file drives the implementation. Read `plan.md` for the full spec. This file is the
agent-facing subset: how to work, what's allowed, and the exact step sequence.

---

## Environment (verified on this machine)

- Fedora 44, user `enrico`, **no root**.
- `tar` (GNU tar 1.35, `--zstd` works), `zstd`, `notify-send`, `flock`, `setsid`, `nohup` present.
- `python3` 3.14.10, `/usr/bin/python3`.
- **No third-party packages are required.** Do not install anything via `dnf`. If a binary
  were missing, the only Fedora package that could matter is `tar` (for `--zstd`); note it,
  do not install.

## Hard constraints

- Python **standard library only**. No pip, no `import` that requires a dnf package.
- One file does the whole job: `minecraft-backups.py`.
- The launcher command must return quickly. Never block the foreground on the archive.
- The detached worker is the authority on skip-vs-create; the foreground fast-path must agree.
- Deterministic tests: inject the clock (e.g. `now()` module-level with default `time.time()`)
  so age tests don't depend on real time.

## Conventions

- Exit codes: foreground `0`=ok(non-zero=setup failed); worker `0`=created, `10`=skipped by age,
  `20`=skipped because running, `1`=hard error, `2`=path/usage error. Keep these frozen.
- Backup name: `<YYYYmmdd-HHMMSS>.<pid>.tar.zst`. Newest by mtime.
- Log format: one line per action, e.g. `INFO skip recent backup /path` / `INFO created /path (<bytes>)` / `INFO prune removed /path`.
- Keep files minimal and boring. No abstractions beyond what each step needs.

## Step sequence

Execute steps 1..11 in `plan.md` **one step at a time**, in order. Stop after each
successful, verified step and wait for the next instruction.

Per step:
1. Do the minimal work for that step only.
2. Run a throwaway smoke test for the step's acceptance criteria before marking it done.
3. If a step fails to verify, stop and report the exact failure; do not guess a fix.
4. Do not implement, refactor, or expand scope beyond the current step.
5. When a step's changes make an earlier helper obsolete, remove it (no dead code).

### Step-by-step

- **1** Skeleton + conventions. Create `minecraft-backups.py` (with a `main()`), `README.md`.
  Verify: `python3 minecraft-backups.py --help` prints usage listing all §3 options.
- **2** argparse. Add all §3 options; `-f/--folder` required.
  Verify: missing `-f` exits non-zero with a message; `--help` lists all flags/defaults.
- **3** Path helpers + exit codes. `backup_dir`, `log_file`, `mkdir` parents, lock/pid paths.
  Verify: wrong/missing folder → non-zero exit with a clear message; `backup_dir` computed as
  `<parent>/backups`; `--backup-dir` overrides it.
- **4** Age gate. Newest-backup vs `now - max_age`. Clock injectable for tests.
  Verify: with a backup older than max_age → proceeds; with a recent backup → exits `0` (skip).
  Inject a file mtime in the future/past to force both cases deterministically.
- **5** Lock + skip-if-running. `fcntl.flock` on lock-file fd; stale-pid handling.
  Verify: two foreground runs don't both create workers; second logs skip/`20`.
- **6** Background launch. `setsid` + `nohup`, redirect to log, `start_new_session=True`,
  pidfile, **return 0 without waiting**.
  Verify: `time python3 ... ` returns in well under a second while a `zstd`/slow worker is still running.
- **7** Worker archive. Re-check age inside the lock; build `<ts>.<pid>.<n>.part`, run `tar --zstd -C <folder> -cf <part> .` via `subprocess`; flush/close (fsync if supported); `os.replace(part, archive)` (atomic write — readers never see a partial file). Exit `0` created / `1` hard error.
  Verify: after the foreground returns, the worker produces a valid archive (`tar --zstd -t` and `zstd -t`). On failure, the `.part` is removed and NO `*.tar.zst` is left behind.
- **8** Prune. List `*.tar.zst` newest-first; delete older than `keep`; also remove stale `.part` files; never delete the current archive by mtime (guard it by name).
  Verify: create 7 (via injected mtimes), `--keep 5` deletes exactly 2; no `.part`/partial files remain.
- **9** Completion. Emit `notify-send` only when the desktop session is detected (`XDG_SESSION_DESKTOP` set) AND the `notify-send` binary exists; otherwise skip silently (no error). No `--no-notify` option exists. Append a structured line to log_file; cleanup pidfile.
  Verify: `XDG_SESSION_DESKTOP` set + binary present → `notify-send` fires; var unset → no notification, no error; missing binary → skip without error.
- **10** Smoke test all paths (§7 in plan.md).
  Verify the behaviors: return-fast, create, skip-by-age, prune, survive-parent-kill, desktop-gate.
- **11** Docs. Final README pre-command string + troubleshooting.

## How to drive

- Report each step's result with concrete evidence (command + observed output), not adjectives.
- Ask (via `ask`) only for the open questions in `plan.md §9` if the user needs to choose; otherwise proceed with the default/boring choice.
- Never write code for a step the user hasn't approved; wait for the next prompt.
