# minecraft-backups

Scheduled, self-limiting backups of a Minecraft **saves** folder.

Back up the folder you game in, then let your Minecraft launcher use this script as
its pre-launch command. The script **launches the backup and returns immediately**, so
minecraft starts while the archive is written in the background.

## What it does

- Creates a compressed archive (`tar --zstd`) of the saves folder **only if no backup
  exists from the last 24 hours** (`--max-age`).
- Keeps the **5 most recent** backups and prunes the rest (`--keep`).
- Writes archives **atomically** (a temp `.part` file + rename), so a failed or
  interrupted run never leaves a partial `*.tar.zst` behind.
- Notifies you (`notify-send`) when the backup completes — only on a desktop session,
  and only if the `notify-send` binary exists.
- Runs in the **background**: the launcher's command finishes as soon as the job is
  started, never while minecraft is starting.

## Requirements (Fedora 44)

- Python 3 with the **standard library only** (no third-party packages).
- System tools: `tar` (with `--zstd`), `zstd`, `flock`, `notify-send`.
- (On this host all of these are already installed; nothing needs `dnf`.)

## Usage

Run it directly once to see it work:

```
python3 minecraft-backups.py ~/.minecraft/saves
```

### As a launcher pre-command

Tell your launcher to run this command *before* starting minecraft:

```
python3 minecraft-backups.py ~/.minecraft/saves
```

The script exits as soon as the backup is launched. Because it uses `setsid`, the
backup keeps running even after the launcher moves on, so you can join the game while
it finishes. It assumes the launcher lets the pre-command return (never terminates the pre-command's process group); `setsid` covers launchers that do.

> Path note: `~/.minecraft/saves` is just an example — the folder is given as a
> positional argument, so point it at your actual saves directory. The backup is
> written to **`/backups` next to the saves folder** (one level up from the folder),
> so it never lives inside the world folders.

## Command-line options

| Option | Default | Meaning |
|---|---|---|
| DIR (positional, required) | *(required)* | Directory whose **contents** are archived |
| `-B, --backup-dir DIR` | `<parent-of-folder>/backups` | Where archives are written |
| `-a, --max-age HOURS` | `24` | Only create if no backup is younger than this |
| `-k, --keep N` | `5` | Number of newest backups to retain |
| `-t, --lock-timeout SECONDS` | `60` | Wait this long for a running backup before skipping |
| `-l, --log-file PATH` | `<backup-dir>/backup.log` | Where logs go |
| `-v, --verbose` | off | Extra diagnostic lines on stderr |
| `-h, --help` | | Usage |

There is **no** `--no-notify` option: desktop notifications are emitted on completion,
gated on `XDG_SESSION_DESKTOP` and the presence of the `notify-send` binary.

## Exit codes

| Code | Meaning |
|---|---|
| `0` (foreground) | Backup launched — returns immediately after spawning the detached worker (created or skipped) |
| `20` (foreground) | Skipped — a backup is already running (skip-if-running) |
| non-zero (foreground) | Setup failed (folder missing, tool/binaries unavailable, cannot create backup dir) |
| `0` (worker) | Created a backup |
| `10` (worker) | Skipped — a backup exists within `--max-age` |
| `20` (worker) | Skipped — a backup is already running |
| `1` (worker) | Hard error (e.g. `tar` failed); the `.part` file is removed, no partial archive left |
| `2` (worker) | Path/usage error |

## Troubleshooting

- **Nothing was backed up?** The worker logs everything to `<backup-dir>/backup.log`
  (e.g. `~/.minecraft/backups/backup.log`). Open it to see the outcome: created,
  skipped (a backup exists within `--max-age`), or failed.
- **Pre-command seems to hang?** It doesn't. The launcher-visible script returns `0`
  as soon as the worker is spawned; minecraft starts immediately. Only the background
  worker does the archiving.
- **Backup in the wrong place?** Archives are written one level up from the folder,
  inside a `backups/` folder (override with `--backup-dir`).
- **No desktop notification?** Notifications run only on a desktop session
  (`XDG_SESSION_DESKTOP` set) and only if `notify-send` exists; on a headless host
  they are skipped silently.

## See also

- `plan.md` — full specification.
- `agents.md` — build steps and agent instructions.
