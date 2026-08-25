# pCloud Sync

Backup to pCloud that recognizes moved files.

> **Unofficial tool.** Not affiliated with, nor endorsed by, pCloud AG.
> "pCloud" is a trademark of pCloud AG; this project simply talks to their
> service through [rclone](https://rclone.org/).

**Windows-first.** Developed and used in production on Windows 10/11. The
core also runs on Linux/macOS (`python run.py`), but the launchers, the
service install and the drive browser are Windows work.

Nothing in the engine is pCloud-specific: any rclone backend that exposes
file hashes works the same way — pCloud is simply the one this tool was
built and tested against.

The pCloud client does not detect moves: when you reorganise a folder, it
sees deletions followed by creations and re-uploads everything. On a 100 GB
photo library, that means days of upload for files already present on the
server.

This application relies on rclone and its `--track-renames` option: moved
or renamed files are repositioned **by the pCloud server**, without a
single byte leaving your machine. Only genuinely new content is uploaded.

Measured on a 100 GB reorganisation: **3.95 GB transferred instead of 99 GB.**

---

## Installation

**1.** Download the latest release zip from the GitHub **Releases** page
and unzip it wherever you like, for example `C:\Apps\pcloud-sync`.

**2.** Double-click **`pCloud Sync.bat`**.

On first launch it prepares everything in a `runtime` sub-folder — a few
minutes. After that, the application starts directly.

**3.** Double-click **`Create desktop shortcut.bat`** to get the icon on
your Desktop.

**Updating**: when a new release exists, the header shows an **Update**
button — one click downloads and installs it (your settings and data are
never touched), then restart the application. Manual alternative: extract
the new release zip over the installation folder.

To start the application with Windows, double-click
**`Start with Windows.bat`**. The same file undoes the setting if you
change your mind.

### If Python or rclone are missing

The launcher will tell you. In PowerShell:

```powershell
winget install Python.Python.3.12
winget install Rclone.Rclone
```

Close and reopen PowerShell after each install, then run
`pCloud Sync.bat` again.

### Connect your pCloud account

```powershell
rclone config
```

Answers: `n` → name `pcloud` → type `pcloud` → Enter → Enter → **`y`** at
*Edit advanced config* → at `hostname`, pick **`2` (eapi.pcloud.com)** for a
European account → Enter for the rest → `y` for browser authentication.

The hostname is the classic trap: with the wrong choice, the remote shows
up empty. If the authorisation page does not load, replace `my.pcloud.com`
with `e.pcloud.com` in the displayed URL, changing nothing else.

Check:

```powershell
rclone lsd pcloud:
```

---

## Day-to-day use

An icon stays in the notification area, next to the clock. Right-click to
reopen the window or quit. Closing the window does not interrupt running
transfers.

Running `pCloud Sync.bat` while the application is already up does not
start a second copy: the existing window comes back to the front.

## Create a backup

Everything happens in the interface, in three steps.

**1. What to back up.** You browse your drives. Tick a whole drive, or
drill down the tree to take a single folder. Several ticked boxes create
several backups at once.

**2. Where to send it.** You browse pCloud the same way. The application
then suggests destinations that mirror your local tree:

```
D:\Photos  +  pcloud:pCloud Backup/MY-PC
    ->  pcloud:pCloud Backup/MY-PC/Drive D/Photos
```

This is the pCloud client's own convention. By picking your machine's
folder under `pCloud Backup`, you plug into an existing backup: files
already online are recognised, nothing is re-uploaded.

**3. Settings.** Name, final destination, automatic start time, and two
settings that work together:

| | |
|---|---|
| Transfer without confirmation | the analysis chains into the transfer instead of stopping |
| Deletion threshold | above it, the automatic transfer is refused and waits for your validation |

The threshold is the safety net. A synchronisation deletes on the cloud
side whatever disappeared locally: if a drive half-unmounts or a folder is
moved by mistake, it stops the backup from propagating the loss. Start low
and adjust after a few cycles, once you have seen what is normal for you.

Profiles are stored in `profiles.json`, next to the history database. The
**Edit** and **Delete** buttons on each card let you come back to them.
Deleting a profile does not touch the files already on pCloud.

### General settings

`config.yaml` is created from `config.example.yaml` on first launch and is
then yours: updates never touch it. It only contains technical settings:
listening port, number of parallel transfers, bandwidth limit, excluded
files, hidden-item skipping (`skip_hidden`, off by default — the pCloud
client skips hidden files silently; enable it to match), optional data
paths. After a change, restart the application (or
`nssm restart PCloudSync` in service mode).

To validate the configuration without starting anything:

```powershell
python run.py --check
```

---

## Usage

Interface at **http://127.0.0.1:8477**.

The cycle is always the same:

**Analyze** simulates the operation without modifying anything and produces
a numbered summary: server-side moves, files to upload, network volume,
deletions. When it concludes there is nothing to transfer, that claim is
itself verified by an independent listing comparison before being shown —
anything found missing becomes the plan instead.

During an analysis the progress counts every local file examined —
compared, moved or queued for upload — so it keeps advancing even when the
backup has a large gap and there is nothing left to compare.

After an update, the banner offers **Restart now**: the application
closes and relaunches itself. At startup the window opens immediately on a
status screen naming the phase actually in progress (starting the rclone
engine, loading profiles, …) instead of staying invisible until everything
is ready.

Between transfers, a periodic **spot check** (every `drift_check_hours`,
24 by default) runs the same read-only comparison in the background and
keeps each profile's card honest: "spot check <when>: in sync", or the
number of files missing on the destination, in amber. It changes nothing
and never runs while an operation is active.

**Review the plan.** The two-track bar shows the proportion between what
the server handles for free (green) and what has to cross your uplink
(amber). If deletions are planned, click the figure to open the detailed
list, folder by folder.

**Start the transfer.** Progress shows live: volume, speed, time left,
files in flight. The synchronisation ends with an automatic completeness
verification: both sides are re-listed and compared, and the run is only
recorded as a success once every source file is proven present on the
destination. If anything is missing, the run is marked **Incomplete**, the
missing files appear as a ready-to-review plan, and one more validated
transfer completes the backup.
The transfer is **bounded by the validated plan**: it re-verifies the whole
tree (a plan is a forecast, not a script), and it will never delete more
files than the analysis showed — if the tree changed beyond that since the
analysis, the transfer stops with an error instead of widening the
perimeter, and you simply run a fresh analysis.

An analysis plan survives closing the application: it is saved on disk and
recovered at the next start, with its age shown. A thirty-minute analysis
is therefore never lost because you closed the window. The plan disappears
once the transfer is done, when you discard it, or when a new analysis
replaces it.

In the runs table, hovering a row reveals two buttons: rerun the same
operation on that profile, or delete the row. "Clear history" empties the
whole table without touching backups or files.

The **Analyze then transfer** button chains both, within the limits set by
`auto` and `max_deletes`.

> Pause the pCloud client's own backup before starting a transfer. Both
> tools working on the same folders at the same time produces conflicts.

---

## What the application does not do

- **It does not replace pCloud Drive.** The `P:` drive stays with the
  pCloud client. For an rclone equivalent:
  `rclone mount pcloud: P: --vfs-cache-mode full` (requires WinFsp).
- **It synchronises one way only**, local to cloud. Changes made on the
  pCloud side do not come back down.
- **It does not handle the encrypted folder** or pCloud shares.

---

## Security model

- The interface listens on **127.0.0.1 only** by default and has **no
  authentication**: it trusts the local user. Requests whose `Host` or
  `Origin` headers do not match the expected local origin are refused,
  which keeps malicious web pages and DNS-rebinding tricks from driving
  the API from your browser.
- Setting `host: 0.0.0.0` in `config.yaml` exposes the unauthenticated
  interface to your network: anyone who can reach the port can start
  transfers — and the deletions they imply. Only do it behind something
  you trust (a VPN such as Tailscale, or firewall rules).
- The rclone engine daemon runs on 127.0.0.1:5577 without authentication
  (`--rc-no-auth`): any local process can drive it, with your user's
  rights. This matches rclone's own threat model for a local daemon.
- Once every few hours the application asks `api.github.com` for the
  latest release number, to offer the Update button. Nothing about you or
  your data is sent; `update_check: false` disables it.
- Your pCloud credentials live in rclone's own configuration
  (`%APPDATA%
clone
clone.conf`), never in this application's files.

---

## Troubleshooting

**"rclone was not found"** — close and reopen PowerShell after installing;
the PATH is not refreshed in an already-open console.

**"The remote does not exist"** — `rclone listremotes` shows the known
remotes. The name used in the destination must match exactly, before the
colon.

**"Local folder not found"** — external drive unplugged, or a drive letter
that changed.

**"Port already taken"** — another program uses that port. Open
`config.yaml`, change the `port:` line to another number, and restart.
The application verifies the identity of whatever answers before starting:
it will never open another program's interface for you.

**Nothing happens on double-click** — run `Diagnostic.bat`. It opens a
console that stays visible and shows the exact error. Also look for a
`startup-error.log` file in the application folder.

**The window does not open but the application runs** — it falls back to
your default browser, at `http://127.0.0.1:8477`. The log in
`C:\ProgramData\PCloudSync\logs\app.log` says why.

**An interrupted analysis does not resume** — an rclone limit, not a
missing feature. The analysis is a single job that reads files to compute
their checksums, with no intermediate checkpoint: interrupting it loses all
the hashing already done. The rerun button in the history starts from
scratch.

**The analysis is long** — normal. rclone reads the full content of the
files to compute their checksums, the only way to recognise a renamed
file. Expect 15 to 30 minutes for 100 GB, depending on the drive.

**The transfer seems frozen** — in a Windows console, selecting text pauses
the process. Press Escape to release it. In service mode, this does not
happen.

**Many more deletions than expected** — discard the plan and check. This
usually signals a moved local folder or a partially mounted drive, not a
deliberate clean-up.

---

## File layout

```
pcloud-sync/
├── pCloud Sync.bat              ← double-click here
├── Create desktop shortcut.bat
├── Start with Windows.bat
├── Diagnostic.bat               run when double-clicking does nothing
├── desktop.py                   launcher: window and tray icon
├── run.py                       windowless start (service, troubleshooting)
├── config.example.yaml          settings template (copied to config.yaml)
├── install.ps1                  Windows service install (optional)
├── LICENSE                      MIT
├── requirements.txt
├── runtime/                     created on first launch
└── app/
    ├── config.py                settings loading and validation
    ├── store.py                 profiles, created from the interface
    ├── browse.py                drive and pCloud browsing
    ├── rclone.py                rclone engine control (rcd API)
    ├── engine.py                analysis → validation → transfer cycle
    ├── db.py                    SQLite history
    ├── scheduler.py             scheduled starts
    ├── server.py                HTTP API
    └── static/                  web interface
```

Data outside the installation folder, in `C:\ProgramData\PCloudSync\`:
`profiles.json`, `history.db`, `scheduler.json` and `logs/`. Reinstalling
does not erase them.

---

## How it works inside

The application does not run rclone on the command line and re-read a log
file afterwards. It starts `rclone rcd`, the daemon mode, and talks to it
over HTTP on a local port. The statistics — bytes, speed, time left,
server-side moves, files in flight — come straight from the engine.

Every operation is an identified asynchronous job that can be queried and
interrupted. The planned deletions are extracted from the simulation log,
the only information the API does not detail.
