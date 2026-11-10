# agents.md — instructions for the coding agent (me)

This file drives the implementation. Read `plan.md` for the full spec. This file is the
agent-facing subset: how to work, what's allowed, and the exact step sequence.

---

## Environment (verified on this machine)

- Fedora 44, as user, **no root**.
- `tar` (GNU tar 1.35, `--zstd` works), `zstd`, `notify-send`, `flock`, `setsid`, `nohup` present.
- `python3` 3.14.10, `/usr/bin/python3`.
- **If additional packages are required ask the user.** Do not install anything without prompting the user first.

## Hard constraints

- Python **standard library only**. If optional extra python libraries are required, prompt the user.
- One file does the whole job: `minecraft-backups.py`.
- The launcher command must return quickly. Never block the foreground on the archive.
- Background and foreground share the SAME `_run` code; only setsid-detached vs
 inline differ, and the background (worker) return code is never read.
- Deterministic tests: inject the clock (e.g. `now()` module-level with default `time.time()`)
  so age tests don't depend on real time.

## Conventions

- Exit codes are frozen — see plan.md §5 (single source of truth); change only with a coordinated edit to both files.
    - Backup name: ``<YYYY-mm-dd_HH-MM-SS>.<pid>.<ts>.tar.zst`` (``.part`` during tar).
      Newest by the name-stamp prefix (fixed-width, lexically == chronologically), never mtime.
    - Log format: one line per action, e.g. `INFO skip recent backup /path` / `INFO created /path (<bytes>)` /
      `INFO pruned N archive(s); keeping M newest (name)`.
- Keep files minimal and efficient. No abstractions beyond what each step needs.

## Step sequence

Execute steps 1..11 in `plan.md` **one step at a time**, in order. Stop after each
successful, verified step and wait for the next instruction.

Per step:
1. Do the minimal work for that step only.
2. Run a throwaway smoke test for the step's acceptance criteria before marking it done.
3. If a step fails to verify, stop and report the exact failure; do not guess a fix.
4. Do not implement, refactor, or expand scope beyond the current step.
5. When a step's changes make an earlier helper obsolete, remove it (no dead code).
6. If you find bug / defects from previous steps, report them to the user.

### Step-by-step
The authoritative step list, descriptions, and acceptance criteria live in `plan.md`
§7 — the single source of truth. Agents.md only drives *how* each step is executed;
it does not redefine what any step does.

## How to drive

- Report each step's result with concrete evidence (command + observed output), not adjectives.
- Never write code for a step the user hasn't approved; wait for the next prompt.
