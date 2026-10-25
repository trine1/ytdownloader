# YouTube Downloader

A Linux graphical tool (Qt) for downloading YouTube files as audio or
audio/video in the highest supported resolution, using [yt-dlp](https://github.com/yt-dlp/yt-dlp).

## Requirements

- Python 3
- [PyQt6](https://pypi.org/project/PyQt6/) (installed automatically by uv)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) (on `PATH`)
- [ffmpeg](https://ffmpeg.org/) (for audio extraction and video merging)

Developed on Omarchy Linux (Hyprland/Wayland), Python 3.14.

## Usage

### Install locally with uv (recommended)

From the project directory:

```bash
uv tool install --force .
```

This creates an isolated environment with PyQt6 and installs a `ytdownloader`
executable on your PATH (`~/.local/bin/ytdownloader`). Then:

```bash
ytdownloader
```

### Run from source (without installing)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m yt_downloader
```

## Input formats

- Complete YouTube URLs (`https://www.youtube.com/watch?v=ID`, `https://youtu.be/ID`).
- Bare video IDs (the `v=` value, exactly 11 characters).
- **Invidious front-end URLs**, e.g.
  `https://inv.nadeko.net/watch?v=wW7oTRyqXLE` or
  `https://invidious.tiekoetter.com/watch?v=wW7oTRyqXLE`
  (any instance works; passed straight to yt-dlp).

## Features

- **Validation before download**: each URL/ID is validated immediately as you
  type and, once confirmed, the video's **full title** is shown so you can
  verify it is the file you want.
- **Explicit start only**: nothing downloads until you press **Download**
  (starts immediately, ahead of items already waiting).
- **Pause downloads / Clear downloaded** sit underneath the Downloads list
  (right-aligned): pause pauses every active and pending item at once
  (persists across restart); clear removes only completed/failed items —
  per-row ✕ removes anything else individually.
- **Audio only (best quality)** or **video + audio (highest resolution,
  merged with ffmpeg)**.
- **Queueing with parallel downloads**: up to N concurrent downloads (N set in
  Settings).
- **Progress bar, percent complete, and ETA** for every active download.
- **Pause / Resume** on each active download: pause stops the worker without
  counting as a failure; resume continues the partial file
  (`yt-dlp --continue`).
- **Pause downloads**: pauses every active download *and* every pending item
  at once (button top-right; label flips to "Resume downloads"). The paused
  state is saved and restored, so it survives a restart — press the button
  again to continue everything.
- **Auto-retry with exponential backoff**: failed attempts are retried after
  N seconds doubling up to a 5-minute cap, up to 10 attempts (customisable via
  **Settings → Preferences**).
- **Robust completion verification**: destination exists, non-zero size,
  recently modified, and (for video) ffprobe-able.
- **Downloads section (merged view)**: a single "Downloads" list shows every
  item — running (live progress bar, ETA, pause/resume), paused, queued,
  awaiting-retry, completed (green ✓) and failed (red ✗). Each item is a row
  with its own progress bar (0–100%), an ETA/status text, pause/resume
  controls and an ✕ remove button. Rows show the **video title**, not the
  URL (the URL stays available as the row tooltip); titles come from the
  validation step when you add the download, or are picked up from yt-dlp
  once it starts running — items restored from saved state get their titles
  resolved automatically in the background. Fully downloaded and failed items
  appear greyed out (progress/controls disabled) but stay listed until
  removed with their ✕. Completed downloads are *not* restored on startup
  (they show only in the session that downloaded them).
- **✕ remove button** next to every queued, active, and completed entry, so
  any item can be dropped from any list at any time.
- **Automatic state save**: settings, the queue, and active downloads are
  saved periodically (every 60 s) and always when exiting, then restored on
  start-up — in-progress files resume where they left off. If you quit while
  downloads run, the confirmation dialog reminds you that they will be
  automatically resumed next time. **Shift+Ctrl+Q** exits immediately without
  prompting (state is still saved).
- **Help menu** shows the current version of the tool.

### Keyboard shortcuts

| Action | Shortcut |
|--------|----------|
| Quit (asks about active downloads) | `Ctrl+Q` |
| Exit without prompting (state still saved) | `Shift+Ctrl+Q` |

## State file

State is saved to:

```
$XDG_DATA_HOME/ytdownloader/state.json
```

(`~/.local/share/ytdownloader/state.json` by default). The file is written
atomically (temp file + atomic rename) so a crash during saving never leaves a
corrupt state file.

## Packages

- Debian package for Linux Mint LMDE 7 (Debian 12 bookworm):
  build with `dpkg-buildpackage -us -uc` after installing `debhelper`, or run
  `python build_deb.py` (needs only Python) to produce
  `ytdownloader_<version>_amd64.deb`.
- Standalone distribution-neutral installer for any distro:
  run `python build_installer.py` to produce a single self-extracting script
  `ytdownloader-<version>-linux-installer.sh`:

  ```bash
  bash ytdownloader-0.2.0-linux-installer.sh            # install to ~/.local
  bash ytdownloader-0.2.0-linux-installer.sh --help     # options
  bash ytdownloader-0.2.0-linux-installer.sh --uninstall
  ```

  It installs into `~/.local/share/ytdownloader` (isolated venv), pip-installs
  PyQt6 and yt-dlp from PyPI (internet needed once at install time), creates
  `~/.local/bin/ytdownloader` and a desktop entry, and — if ffmpeg is missing
  system-wide — downloads a static x86_64 build automatically (this step can
  be skipped with `--no-ffmpeg`). Requires Python ≥ 3.10 and a graphical
  desktop session. Existing state (saved downloads) is preserved across
  installs and upgrades.

## Releases

Signed-hash artifacts are published on GitHub Releases
(<https://github.com/trine1/ytdownloader/releases>) per version:

| Artifact | For |
|----------|-----|
| `ytdownloader_<version>_amd64.deb` | Debian/Linux Mint LMDE 7 (apt/dpkg) |
| `ytdownloader-<version>-linux-installer.sh` | Any distribution (self-extracting, no package manager needed) |
| `SHA256SUMS.txt` | Checksums for verifying the above files |

## Project status

See [`PLAN.md`](PLAN.md) for the plan and detailed progress notes.
