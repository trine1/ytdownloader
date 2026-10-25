# Plan

# Goal
Develop a Linux-based graphical tool for downloading YouTube files as audio or audio/video in the highest supported resolution.

# Basic requirements
- Download using yt-dlp (from https://github.com/yt-dlp/yt-dlp).
- Source files: The interface should accept both complete Youtube URLs and unique identifers (usually specified by the v= parameter).
- The interface is based on Qt.
- Create a Debian package that supports Linux Mint LMDE 7.

# Features
- Validation: Any file URL / id is validated immediately.
- Queue: If a file is currently being downloaded then any new file is added to a queue.
- Progress for current download: Show a progress bar, % complete and estimated time-to-completion for the currently downloaded file.
- Auto-retry: If a file download fails, auto-resume after N seconds. Use an exponential backoff up to 5 minutes max and retry for up to 10 times. These settings should be configurable from within the user interface via a Settings menu option.
- Parallel download: Download up to N files in parallel. The value of N should be configurable from the same Settings menu.
- Robust completion verification: Use all reasonable measures to ensure that a download has completed successfully.

# Progress
Maintain a record in this file of our progress, decisions and other relevant findings.

- We have an outline plan.

# Progress / Decisions

## 2026-10-25 — Initial implementation

### Stack
- Language: Python 3.14 (system python, venv at `./.venv`)
- GUI: PyQt6 6.11.0 (Qt 6.11.2) — installed into the project venv
- Downloader: `yt-dlp` 2026.8.19 (already on system PATH) + `ffmpeg` (merge/extract)
- Runs on Omarchy Linux (Hyprland/Wayland); tested with `QT_QPA_PLATFORM=xcb`

### Files created
- `requirements.txt` — PyQt6 pin
- `yt_downloader/__init__.py`, `__main__.py` — package + `python -m yt_downloader` entry
- `yt_downloader/main.py` — `main()` / `QApplication` bootstrap
- `yt_downloader/main_window.py` — MainWindow (Qt UI)
- `yt_downloader/worker.py` — `Worker` (QThread) + `DownloadItem`
- `README.md` — usage notes

### UI (MainWindow)
- Source: URL/video-ID line edit + live validation label (green valid / red invalid)
- Options: format combo ("Audio only (best quality)" / "Video + audio (highest resolution)"), output dir + Browse
- Actions: Add to queue / Remove selected / Clear queue
- Queue list (waiting items)
- Current download: label, progress bar, % complete, ETA, status line

### Worker behavior
- `validate(text)` — runs `yt-dlp --no-playlist --print "id:%(id)s|title:%(title)s"`; bare 11-char IDs are expanded to `https://www.youtube.com/watch?v=<id>`; emits `validated(ok, resolved_url, title_or_error)`
- `download(item)` — runs `yt-dlp` subprocess:
  - audio: `-x --audio-format best --audio-quality 0`
  - video: `-f bestvideo+bestaudio/best --merge-output-format mkv`
  - parses `[download] ...% ... ETA ...` lines from stdout (stderr merged) → `progress(percent, eta_seconds)`
  - emits `status(...)` and `finished(success, message)`
- Queue: single `Worker` reused; while a download runs, new items are queued and started automatically when the current one finishes (`_maybe_next`)

### Verified
- `py_compile` passes for all modules
- App launches and runs without crashing
- Validation: bare ID, full URL, `youtu.be/` link, and invalid string all behave correctly
- End-to-end download of a short video (audio, best quality) completes; progress/status/finished signals fire; file written to output dir

### 2026-10-25 — New requirements implemented

Added to PLAN.md (Basic requirements + Features):
- Debian package supporting Linux Mint LMDE 7 (Debian 12 bookworm)
- Auto-retry: exponential backoff, up to 5 min max, up to 10 retries, configurable via Settings
- Parallel downloads: up to N concurrent, N configurable via Settings
- Robust completion verification
- Settings menu option in the UI

### New/changed files
- `yt_downloader/manager.py` — DownloadManager: queue, N parallel workers, per-item retry with exponential backoff, completion verification (file exists / size>0 / freshly modified / ffprobe probe for video)
- `yt_downloader/settings_dialog.py` — SettingsDialog (max_parallel, max_attempts, initial_backoff, max_backoff, default_output_dir)
- `yt_downloader/main_window.py` — rewritten: QMainWindow with Settings menu, queue list + active-downloads list (parallel progress rows), state save/load on exit and periodically, uses DownloadManager
- `yt_downloader/worker.py` — now reports destination path; single attempt (no retry — retry lives in manager)
- `yt_downloader/state.py` — NEW: state persistence (JSON state file at `$XDG_DATA_HOME/ytdownloader/state.json`), save_state/load_state functions
- `debian/` — changelog, control, compat, rules, install, copyright, source/format
- `yt_downloader/launcher.sh`, `yt_downloader.desktop`
- `build_deb.py` — builds a .deb without dpkg-deb

### Worker behavior (single attempt)
- `validate(text)` — `yt-dlp --no-playlist --print "id:%(id)s|title:%(title)s"`; bare 11-char IDs → watch URL; emits `validated(ok, url, title)`
- `download(item)` — audio: `-x --audio-format best --audio-quality 0`; video: `-f bestvideo+bestaudio/best --merge-output-format mkv`; parses progress + `[...] Destination:` lines; emits `finished(success, message, destination)`

### DownloadManager behavior
- `configure(max_parallel, max_attempts, initial_backoff, max_backoff)`
- `add(url, kind, output_dir)` → queues + starts up to max_parallel in parallel
- `validate(text)` → one-off validation
- On attempt success: verifies completion (destination non-empty, file exists, size>0, mtime within 10 min, ffprobe for video). If verification fails → treated as failure → retry.
- On attempt failure: if attempts < max_attempts, schedules retry after `min(initial*2^(attempts-1), max_backoff)` seconds; else emits `item_finished(success=False)`.
- When a slot frees, starts next queued item.

### Verified
- `py_compile` passes for all modules
- App launches and runs without crashing; Settings dialog opens
- Validation: bare ID, full URL, `youtu.be/`, invalid string
- End-to-end successful download (audio best quality): finished + destination + file written
- Parallel downloads (2 concurrent) work
- Auto-retry with exponential backoff works (bot-protected video retried then exhausted max_attempts)
- Completion verification passes for successful downloads
- `.deb` built: `ytdownloader_0.1.0_amd64.deb` (format 2.0), `file` recognizes it, ar members + control + data payload verified, modules import from extracted install location

### Building the .deb on LMDE 7 (preferred, uses debhelper)
    sudo apt install debhelper dh-python python3-pyqt6 yt-dlp ffmpeg
    dpkg-buildpackage -us -uc
# On this Arch/Omarchy box dpkg tooling isn't installable, so build_deb.py
# constructs a format-2.0 .deb manually (verified with `file` and payload listing).

### Known limitations / next steps
- Progress regex matches yt-dlp's `ETA` format; very fast downloads show `in ...` instead of `ETA`
- No download history / persistent store yet
- Output filename uses yt-dlp's default `%(title)s.%(ext)s` sanitization
- YouTube bot-protection can cause download failures (outside our control); retry handles transient cases

### Local install (this system, 2026-10-25)
- Project packaged with `pyproject.toml` (hatchling build backend, console script `ytdownloader = yt_downloader.main:main`)
- Installed via `uv tool install --force .` → isolated env at `~/.local/share/uv/tools/ytdownloader/`, executable symlinked to `~/.local/bin/ytdownloader` (on PATH)
- `ytdownloader` launches and runs correctly (verified with `QT_QPA_PLATFORM=xcb`)

### Bug fix: bare YouTube IDs failing with 429 / bot-check (2026-10-25)
- Symptom: valid video IDs (e.g. `wW7oTRyqXLE`) failed validation with `ERROR: Sign in to confirm you're not a bot` / HTTP 429.
- Root cause: yt-dlp's default web player client is rate-limited / bot-checked by YouTube; the video itself is valid.
- Fix: pass `--extractor-args "youtube:player_client=android"` to all yt-dlp invocations (validation + download). The Android client is not rate-limited and resolves the video.
- `wW7oTRyqXLE` now validates (`Dorothy Moore - Misty Blue`) and downloads end-to-end.
- Note: `inv.nadeko.net` (Invidious frontend) is currently down (502 Bad Gateway, API disabled); full Invidious URLs are still accepted as-is since yt-dlp supports them natively via URL passthrough, but instances are unreliable.
- Regression: regular YouTube IDs/URLs still work with the Android client.

### Feature: pause/resume per active download (2026-10-25)
- Each active-download row now has a **Pause** / **Resume** button (70px wide, right-aligned).
- Pause: stops the yt-dlp worker; the item is *not* treated as failed — it stays active marked `paused`, no retry is scheduled, and the slot is freed for queued items.
- Resume: re-queues the item at the front and relaunches yt-dlp with `-c`/`--continue` so the partially downloaded file is resumed rather than restarted.
- UI: button text toggles between "Pause" and "Resume"; label shows " [Paused]" when paused; progress bar retains its last value while paused.
- `DownloadItem` gained `paused` and `resume` fields; `Worker.download(item, resume=...)` passes `-c` to yt-dlp when resuming.
- `DownloadManager` gained `pause_item(item)` / `resume_item(item)` and tracks `_paused_workers` so intentional stops aren't confused with failures.
- Verified end-to-end: pause `wW7oTRyqXLE` mid-download → resume → completes with `download complete` and file written.

### Feature: automatic state save/load (2026-10-25)
- The program periodically saves its state to a JSON file (every 60 seconds) and always when exiting gracefully (`closeEvent`).
- State file: `$XDG_DATA_HOME/ytdownloader/state.json` (`~/.local/share/ytdownloader/state.json` by default). Written atomically (temp file + rename) so a crash mid-write never corrupts it.
- Saved state includes: Settings (max_parallel, max_attempts, initial_backoff, max_backoff, default_output_dir), the waiting queue, active downloads (with attempt counts, paused/resume flags), and the last source text typed in the URL input.
- On startup, the app loads the state file and restores:
  - Settings to the DownloadManager.
  - Previously-active downloads at the front of the queue with `resume=True` (yt-dlp uses `--continue` to pick up partial files).
  - Paused downloads stay paused and are not auto-started.
  - Attempt counts are preserved (not incremented on restore).
  - The last typed URL is restored to the input field.
- This lets users shut down their computer knowing downloads resume where they left off on next startup, and protects against crashes.
- `DownloadManager.restore(settings, queue_items, active_items)` orchestrates the restore; `_launch(item, increment_attempts=False)` avoids double-counting attempts on restored items.

### Feature batch: invidious URLs, explicit download buttons, completed list, ✕ removals, quit reassurance, force-quit, Help/About, Pause all (2026-10-25)
- **Invidious URLs accepted**: `https://inv.nadeko.net/watch?v=ID`, `https://invidious.tiekoetter.com/watch?v=ID`, etc. Full URLs (any host) are passed through to yt-dlp untouched; only bare 11-char IDs are wrapped into youtube watch URLs.
- **Title confirmation with explicit start**: on validation success the source box shows `✓ Ready to download: <full title>`; downloads commence only when the user presses **Download now** (inserts at the front of the queue and starts) or **Add to queue** (appends behind pending items). Validation alone never starts a download.
- **Completed downloads**: successful downloads move to a Completed section with a green ✓; rows carry ✕ buttons. Completed items are never saved to state, so startup never shows them (in-session only).
- **✕ remove buttons** on every queued, active, and completed row; removing an active download cancels its worker and is explicitly excluded from auto-retry (`_cancelled_workers`).
- **Quit dialog reassurance**: quitting with active downloads states that pending and in-progress downloads have been saved and will be automatically resumed at next start.
- **Shift+Ctrl+Q**: File → *Exit without prompting* quits immediately, skipping the confirmation; state is still saved first.
- **Help menu** → *Version* opens an About dialog with the current version (`yt_downloader.__version__`, bumped to 0.2.0).
- **Pause all** button: flags every pending queue item `paused` and moves active items into the paused set; `_start_next` skips paused items (no tight-loop). Because paused flags and paused limbo items are part of saved state, the pause persists across restarts (button reads "Resume all"); Resume all un-pauses everything, previously-downloading items first, resuming partial files with `-c`.
- Also fixed: `clear_queue` no longer permanently blocks starting (`_stop_all` reset shortly after clear); retry-scheduled items are honoured by Pause all; restoring paused previously-active items emits rows so the UI shows them.
- Verified: manager unit tests (skip-paused scheduling, no infinite loop, removal without retry, add_now priority), pause-all + save/restore + resume-all round trip, in-session completed shown vs not restored, invidious URL passthrough, GUI launch.

### GitHub Releases (2026-10-25)
- New generic, distribution-neutral installer builder: `build_installer.py`
  produces a single self-extracting bash script
  `ytdownloader-<version>-linux-installer.sh`.
  - Payload: pyproject.toml + yt_downloader/*.py + .desktop (base64 tar.gz
    appended after a marker, extracted with awk/tail/base64/tar).
  - Installs to `~/.local/share/ytdownloader` (isolated venv, pip installs
    PyQt6 + yt-dlp from PyPI), creates `~/.local/bin/ytdownloader` (exports
    app bin/venv bin onto PATH), writes a desktop entry, supports `--prefix`,
    `--no-ffmpeg`, `--uninstall`, `--help`.
  - If ffmpeg is missing system-wide on x86_64, downloads a static
    ffmpeg/ffprobe into the app bin dir (skippable).
  - Tested full install round-trip in a sandboxed HOME (venv + pip + launcher
    arg handling + uninstall); fixed heredoc `$@` expansion and `set -u`
    marker lookup during development.
- Release `v0.2.0` published on GitHub with:
  - `ytdownloader_0.2.0_amd64.deb` (Debian/LMDE 7)
  - `ytdownloader-0.2.0-linux-installer.sh` (any distro)
  - `SHA256SUMS.txt` (sha256 checksums)

### v0.3.0 — merged Downloads list + two UI bugs fixed (2026-10-25)
- **Bug (fixed)**: a completed download previously vanished — it was removed
  from Active rows via `item_finished`, but the completed list only refreshed
  on `queue_changed`, which the manager did not emit on terminal states, and
  `item_completed` was never connected in the window. Now: the manager emits
  `queue_changed` on both terminal paths (success ⇒ `_completed`, exhausted
  failure ⇒ new `_failed` list) and the window connects both signals;
  terminal rows stay listed, greyed out.
- **Bug (fixed)**: active rows could show only the label (URL + kind) with the
  progress bar/ETA/buttons invisible for long URLs: the label's width hint
  (long URL) inflated the item's size hint beyond the viewport, so the row
  widget was clipped on the right. The label now uses
  `QSizePolicy.Ignored` horizontally (URL in the row tooltip) and every row
  enforces a minimum height.
- **Merged "Downloads" section** replaces Queue / Active / Completed: one
  list, ordered running → paused → queued → retrying → completed → failed.
  Every row: progress bar 0–100%, ETA/status text, pause/resume (greyed "Done"
  / "Stopped" for terminal states) and ✕ removal. Pending rows pause too;
  manager gained queued-item pause, `_failed` list, `display_items()` ordering
  with identity-based de-duplication.
- Thorough testing: manager unit tests (ordering, dedup, pending pause,
  retry-list pause/removal, failed removal), GUI lifecycle test (live progress
  ticks, pause-all/resume-all row states, completion -> greyed 100% row in the
  merged list, failed row keeps last %), offscreen screenshots reviewed
  (running 75% + queued + paused; completed ✓ greyed + failed ✗ greyed), and a
  real end-to-end yt-dlp download (pause mid-run at attempt boundary, resume
  with `-c`, completion, file on disk, row greyed with destination name).
- Released as v0.3.0 (deb + generic installer + SHA256SUMS).
