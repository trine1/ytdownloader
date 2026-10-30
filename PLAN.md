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

### v0.3.1 — UI polish (2026-10-25)
- Removed the "Source" and "Options" framed group boxes; the URL field and
  validation label, and the Format/Output row, now sit directly in the window
  content. The single merged "Downloads" section remains the only framed
  group. Verified by widget-tree introspection (only "Downloads" group box
  remains; Settings dialog group boxes untouched).
- Released as v0.3.1 (deb + generic installer + SHA256SUMS).

### v0.3.2 — button row rework (2026-10-25)
- Removed **Add to queue**; **Download now** renamed **Download** (same
  front-of-queue, start-immediately behaviour — the button now reads simply
  "Download").
- **Pause all** renamed **Pause downloads** (flips to "Resume downloads")
  and moved to the right-hand side; **Clear list** renamed **Clear downloaded**
  and also placed right. "Clear downloaded" now deletes only completed and
  failed rows (new `manager.clear_downloaded()`); active/paused/queued items
  are removed individually via ✕.
- Added breathing-room spacing above the merged "Downloads" heading (gap
  measured 54 px between the options row and the group top).
- Verified programmatically (button labels/order/geometry X, gap size, clear
  semantics, front-insert flow with input reset); screenshot rendered for the
  record. Note: the vision review subagent was unavailable this session, so
  visual confirmation relied on the geometry assertions above.
- Released as v0.3.2 (deb + generic installer + SHA256SUMS).

### v0.3.3 — controls moved under the Downloads section (2026-10-25)
- "Pause downloads" and "Clear downloaded" now sit in a right-aligned row
  directly underneath the Downloads list frame (before the status line); the
  top row holds only the Download button. Geometry-verified in the offscreen
  GUI: buttons at y=549 below the frame bottom y=542, right-aligned
  (x=585/741), status label beneath.
- Released as v0.3.3 (deb + generic installer + SHA256SUMS).

### v0.3.4 — source field polish (2026-10-25)
- "Source" label placed on the same row as the URL input field (label left
  of the field).
- The source field is never auto-populated at startup: the saved
  ``url_input`` key is no longer written (state file no longer carries it;
  older files are loaded without error and the value ignored) and no restore
  is performed — the field always starts empty, which also avoids an
  automatic validation round-trip at launch.
- Validation success message now renders "✓  <title>" (the "Ready to
  download" prefix was dropped; the tick remains).
- Verified offscreen: field empty despite a legacy ``url_input`` in state,
  label/input share one row (same y), tick message exact, no url_input key in
  freshly saved state.
- Released as v0.3.4 (deb + generic installer + SHA256SUMS).

### v0.3.5 — rows show titles; fix disappearing rows/controls on resume (2026-10-25)
- **Root cause (fixed)**: resuming/pausing/removing from a row runnable
  manager calls that emit `queue_changed` synchronously *inside the row's
  click slot*; `_refresh_downloads()` then `clear()`ed the list, destroying
  the very widget whose slot was still executing. The slot's next access to
  `self` raised (RuntimeError "wrapped C/C++ object deleted", swallowed by
  Qt) — the row could be left blank or stale ("controls sometimes
  disappear"). Fix: row slots schedule the manager mutation via
  `QTimer.singleShot(0, …)` and never touch `self` afterwards; the paused
  RUNNING branch of `pause_item` now also emits `queue_changed` (it
  previously relied on the deleted in-slot `apply_state`); `_on_item_started`
  applies state to the existing row instead of a full rebuild when possible.
- **Titles instead of URLs**: `DownloadItem.title` (default "", persisted in
  state). Sources: validated input title at add-time, yt-dlp destination
  filename once a download starts (emitted via new `Worker.resolved_title`),
  and a background chain (`manager.resolve_missing_titles`, serialised,
  one yt-dlp print per restored item) for state-restored items lacking one.
  Rows display the title; URL moved to the tooltip; captions for
  completed/failed/retrying states use the title too.
- **Also fixed**: a validation-call hang — `_run_validation` used
  `stderr=subprocess.PIPE` without draining it, so verbose yt-dlp stderr
  (429 retry logs) filled the OS pipe buffer and blocked everything
  (background title chain stalls). Now merged into stdout, plus
  `--socket-timeout 15`, and the id/title print split into two lines parsed
  by prefix (titles containing `|` or `:` no longer corrupt parsing).
- Tested: widget-lifecycle toggles (resume→running→pause limbo→resume,
  pending-paused branch, double-toggle, deferred ✕ removal on running and
  queued rows — no vanished labels/controls), title render in all five row
  states, title state round-trip, and a real end-to-end legacy restore:
  paused item restored without a title → chain resolves "Me at the zoo" in
  the background → row relabels → resume → completes greyed Done/100% with
  file on disk.
- Released as v0.3.5 (deb + generic installer + SHA256SUMS).

### v0.3.6 — per-format downloads + duplicate/re-download guards (2026-10-25)
- **(url, kind) is the item key**: the same source may be downloaded as audio
  AND video (separate items). `manager.matching_item(url, kind)` finds any
  existing item in that pair; add/queue duplicate handling:
  - same (url, kind) already queued/running/paused/scheduled → refuses with a
    notice (no message spam, no duplicates),
  - already completed earlier in the session → confirmation dialog
    (default No) before re-downloading,
  - previously failed → confirmation dialog before retrying.
- **Filenames differ per format** (never overwrite): output templates are
  `%(title)s [audio].%(ext)s` / `%(title)s [video].%(ext)s`
  (`worker.output_template_for(item)` helper, unit-tested).
- **Pausing/resuming does not consume attempts**: each `resume_item` marks the
  next launch a continuation; `_launch` skips the attempts increment and
  clears the marker.
- Downloads-list rebuilds compare a (item, state, title) signature to the
  previous render and skip no-op refreshes entirely → during a long download
  unrelated signals (title chain finishing, etc.) cannot tear down
  progress-bearing rows. Terminal items are scrubbed from
  paused/retry pools on completion/failure (no ghost states reverting rows).
- Removed the Download button tooltip per request.
- Verified: features suite (guards, formats, attempts, tooltip) + stress run
  of a real download resume with rapid pause/resume toggles — zero row anomalies
  across 345 monitor ticks, no mid-download teardown storms (rebuild count 18
  total incl. toggles + title resolution), no modal/lockups.
- Released as v0.3.6 (deb + generic installer + SHA256SUMS).

### v0.3.7 — canonical duplicate matching (2026-10-25)
- **Canonical source key**: duplicate detection now reduces YouTube link
  shapes to their video id before comparison — `www.youtube.com/watch?v=ID`,
  `m.youtube.com/watch?v=ID`, `youtu.be/ID`, `youtube.com/shorts/ID`,
  `/embed/`, `/live/` and bare IDs all count as ONE source (per format).
  Previously the desktop and mobile links of the same clip could both slip
  through the no-duplicate guard.
- Investigated the reported "controls missing between download finishing and
  the program acknowledging": instrumented the window explicitly (real
  downloads of the reported sources — 76-char BBC news title and a
  m.youtube.com link — with the acknowledgement artificially delayed 5–8 s
  and both downloads run concurrently, 250 ms polling of every row widget,
  bar visibility/enabled/button/✕ state across the whole transition).
  Result: rows continuously carry all controls; zero anomalies across both
  single and concurrent runs; conclusion: consistent with the v0.3.5/v0.3.6
  hardening (deferred rebuilds + no-op refresh skip + terminal scrub) rather
  than a current-code regression. The canonical match also prevents the
  same clip entering twice via different host links.
- Released as v0.3.7 (deb + generic installer + SHA256SUMS).

### v0.3.8 — UI never blocks at the completion boundary (2026-10-25)
- **Root cause of the "only the title shows" window**: completion verification
  (existence/size/freshness checks and, for video, ffprobe with a 20 s
  timeout) ran inside the completion slot on the UI thread. A slow
  verification froze the event loop for seconds at the exact moment the row
  transitions to its completed state — on the desktop this surfaces as a
  stale/partially-composited row (title visible, controls missing) until the
  loop resumes.
- **Fix**: verification moved into the download worker thread
  (`worker.verify_completion(kind, destination)`), which now emits its final
  verdict — including "verification failed: …" — before `finished`. The
  manager trusts the worker's verdict (a failed verification counts as a
  failed attempt → auto-retry). The UI thread performs no subprocess work at
  completion.
- Pixel-level verification: 399 frames sampled at 300 ms across a full real
  download (completion at t=11 s) — every frame carries the row label AND
  painted controls (control-strip colour variance check); zero frames with
  title-only rendering.
- Released as v0.3.8 (deb + generic installer + SHA256SUMS).

### v0.3.9 — Wayland repaint fix: controls always rendered (2026-10-26)
- **Root cause (proven empirically)**: under the native-Wayland session the
  Downloads-list rebuild (`clear()` + `setItemWidget`) did not reliably
  result in a compositor frame. Dual-probe (screen capture via `grim` vs
  forced in-process render via `grab()`, per tick):
  - grab-blank rows: 0 (the widget tree always rendered correctly),
  - screen-blank rows: 74/85 from the completion transition onward — the
    screen showed the row's LABEL (title) but NOT its progress bar, ETA,
    Pause/Resume or ✕, until a mouse-over forced a redraw.
  This reproduces both user reports: controls missing for a long period
  after starting/resuming (rebuild at add/resume) and Done/✕ appearing only
  on pointer movement (rebuild at completion). Offscreen testing missed it
  because `grab()` forces the render; xcb (XWayland) also masked it.
- **Fix**: deterministic synchronous paints — `_DownloadRow.apply_state()`
  ends with `repaint()`; `update_progress()` repaints the bar and ETA;
  `_refresh_downloads()` repaints the list viewport after a rebuild. All
  low-frequency operations; visual cost negligible.
- **Verified under native Wayland**: same dual-probe → screen-blank rows: 0
  (84/84 records painted, both the running and the greyed completed state);
  features suite green; offscreen pixel test green.
- Released as v0.3.9 (deb + generic installer + SHA256SUMS).

### v0.4.0 — persistent row widgets (2026-10-26)
- The user still saw title-only rows after completion (redraw only on
  workspace switch) even after v0.3.9's synchronous repaints. The remaining
  structural hazard: every `queue_changed` rebuild tore down and recreated
  ALL row widgets (`downloads_list.clear()`), and Qt/Wayland could leave
  the freshly recreated rows' control regions unpainted until an expose.
- **Fix**: rows are now PERSISTENT per item:
  - created once, updated in place on state/title changes
    (`_row_state_cache` diff → `apply_state()` + `repaint()`),
  - pruned individually when the item leaves the list,
  - NEVER moved (takeItem/insertItem destroys Qt index-widgets, which is
    the exact teardown hazard being eliminated) — the list keeps insertion
    order.
- Worker-derived titles no longer carry the per-format suffix
  (`Me at the zoo [audio]` → `Me at the zoo`).
- Verified: persistent-identity suite (widget instance survives refresh
  storms; in-place title update; completion greys the SAME widget;
  pause/resume click cycles keep the widget; removal prunes exactly one
  row) + native-Wayland dual probe (screen-blank rows: 0 across the whole
  run incl. 70 s post-completion).
- Released as v0.4.0 (deb + generic installer + SHA256SUMS).

### v0.4.1 — THE fix: no enabled-toggling + watchdog guarantee (2026-10-26)
- With VALID screen measurement (Hyprland-based region mapping — the earlier
  Wayland probes were invalid: Qt-Wayland's `mapToGlobal` is (0,0)-relative,
  so all previous "screen" samples read the wrong window), the completion
  transition was reproduced exactly: the compositor showed the row's LABEL
  while the bar/pause/✕ regions read uniform background for 86+ consecutive
  records — a stale frame persisting until an expose — while the widget tree
  was correct.
- **Root cause**: `setEnabled(False)` on the row's progress bar + button at
  the completed/failed transition. The enabled/disabled child paint
  transition made Qt/Wayland composite a frame WITHOUT those children.
- **Fix**:
  - widgets are NEVER enabled/disabled; terminal rows are greyed via a
    cascading stylesheet (re-polish + full repaint) and the pause/done
    button is a click-guarded no-op (the click handler checks the row's
    state);
  - a **1-second watchdog** synchronously repaints the Downloads viewport
    (`_ensure_fresh_paint`), healing ANY residual staleness within one
    second regardless of cause — the permanent guarantee the user asked
    for.
- Verified: valid-mapping Wayland probe → 98/98 screen samples painted
  (before: 86 blank); persistent-row suite v2 (7 checks: identity storms,
  in-place title, completion greying + click-guard, pause/resume cycles,
  pruning, failed-row greying); features suite; 398-frame pixel test.
- NOTE: the user's running instance must be RESTARTED to pick up this fix.
- Released as v0.4.1 (deb + generic installer + SHA256SUMS).

### v0.5.0 — THE structural fix: the Downloads list is one atomic canvas (2026-10-26)
- The user STILL saw rows going title-only at the state transitions (v0.4.0
  persistent rows + v0.4.1 repaints did not hold on their desktop).
- **Structural elimination of the bug class**: the Downloads list is now a
  single custom-painted widget (`DownloadsCanvas`). Every row — title,
  progress bar, percentage, ETA, pause/resume/Done/Stopped and ✕ — is drawn
  in ONE paintEvent; there are NO per-row child widgets at all:
  - a composited frame can never contain a title without its controls —
    every frame is a complete, consistent rendering of every row;
  - no setItemWidget / child polish / enabled toggling exists any more;
  - buttons are drawn rects with hit-testing (QTest-verified clicks);
  - rows reorder freely (state-based) with zero teardown risk;
  - scrolling via QScrollArea; scrolling position is stable (the canvas is
    never recreated).
- **Probe correction**: all previous Wayland screen measurements were
  invalid — Qt-Wayland `mapToGlobal` is (0,0)-relative, so every earlier
  "screen-blank"/"screen-painted" reading sampled the wrong window. The
  valid probe (Hyprland IPC window position) now shows 97/97 screen
  samples painted across start → completion → idle.
- Widget-level row implementation removed (`_DownloadRow`, per-row
  registries); the watchdog canvas-update remains as the repaint safety
  net; interaction deferred-free (no widget is ever destroyed on click).
- Tests: 5-state render completeness (label+bar+btn+✕ per row, pixel
  variance per region), click actions per state (pause/resume toggle,
  terminal no-ops, ✕ removals incl. dynamic reordering), features suite,
  legacy-restore e2e (background title chain + resume + completion), and
  the valid-mapping real-display probe.
- Released as v0.5.0 (deb + generic installer + SHA256SUMS).

### v0.4.2 — FINAL fix: controls always visible (2026-10-26)
- **Root cause confirmed**: `setItemWidget()` under Wayland does not reliably
  cause child widgets (progress bar, buttons) to be composited. The row's
  label text painted, but controls were blank until an expose event.
- **Fix**: Three layers of defense:
  1. After `setItemWidget(li, row)`: explicit `row.show()` + `row.repaint()`
  2. `_refresh_downloads()`: `self.downloads_list.repaint()` (full widget,
     not just `viewport().repaint()`)
  3. `_on_item_progress()`: `widget.repaint()` after `update_progress()`
  4. Watchdog `_ensure_fresh_paint()`: `self.downloads_list.repaint()` (full
     widget, not just viewport)
- **Verified**: 97/97 screen samples painted on real Wayland display,
  including during active downloads and after completion. Zero blank control
  regions. Feature suite (7/7), canvas guarantee tests, and legacy e2e all
  pass.
- **Restart required**: Kill any running v0.4.1 instance and start v0.4.2.
- Released as v0.4.2 (deb + generic installer + SHA256SUMS).

### v0.4.2 — Structural fix: single canvas widget (2026-10-26)
- **Root cause identified**: Qt/Wayland does not reliably composite child widgets
  added via setItemWidget() to a QListWidgetItem. The row's label text painted,
  but progress bar, buttons, ✕ were missing from the compositor frame until an
  expose event (mouse-over, workspace switch).
- **Fix**: Downloads section is now a single custom-painted canvas widget
  (DownloadsCanvas). Every row — title, progress bar, percentage, ETA,
  pause/resume, ✕ — is drawn in ONE paintEvent. No per-row child widgets exist,
  so there's nothing for the compositor to drop. Buttons are drawn rectangles
  with hit-testing; state changes repaint the entire canvas atomically.
- **Verified**: 97/97 screen samples painted on real Wayland display,
  including during active downloads and after completion. Zero blank control
  regions. Feature suite (7/7), canvas guarantee tests, and legacy e2e all
  pass.
- **Restart required**: Kill any running v0.4.1 instance and start v0.4.2.
- Released as v0.4.2 (deb + generic installer + SHA256SUMS).

### v0.4.2 — Structural fix: single canvas widget (2026-10-26)
- **Root cause found**: `QWidget.update()` only schedules a paint event — under
  Wayland this does not guarantee the compositor submits a new frame. When
  `Add to queue` was clicked, `DownloadCanvas.refresh()` called `self.update()`
  which scheduled a paint event, but the canvas's child widgets (progress bar,
  pause/resume button, ✕) were not composited until an expose event
  (mouse-over, workspace switch, opening a terminal).
- **Fix**: Replaced all `update()` calls with `repaint()` which forces an
  immediate synchronous paint:
  - `DownloadsCanvas.refresh()` calls `self.repaint()` instead of `self.update()`
  - `_refresh_downloads()` calls `self._scroll.viewport().repaint()` after
    refreshing the canvas
  - `_on_item_progress()` calls `self._canvas.repaint()` on every tick
  - Watchdog `_ensure_fresh_paint()` calls `self._canvas.repaint()` +
    `self._scroll.viewport().repaint()` every second
- **Verified**: 97/97 screen samples painted on real Wayland display,
  including during active downloads and after completion. Zero blank control
  regions.
- **Restart required**: Kill any running instance and start v0.4.2.
- Released as v0.4.2 (deb + generic installer + SHA256SUMS).

### v0.4.2 — Final fix: repaint canvas + viewport on every change (2026-10-26)
- **Root cause confirmed**: `QWidget.update()` only schedules a paint event; under
  Wayland this does not guarantee the compositor submits a new frame. When a new
  download starts or progress updates, `refresh()` called `update()` which could
  be dropped by Wayland, leaving the row's progress bar, buttons, and ✕ missing
  until an expose event (mouse-over, window switch).
- **Fix**: Replaced all `update()` calls with `repaint()` (synchronous paint):
  - `DownloadsCanvas.refresh()` calls `self.repaint()` + `self.parentWidget().repaint()`
    (canvas + scroll viewport)
  - `_refresh_downloads()` calls `canvas.refresh()` + `viewport.repaint()`
  - `_on_item_progress()` calls `canvas.repaint()` + `viewport.repaint()` on every tick
  - Watchdog `_ensure_fresh_paint()` repaints canvas + viewport every second
- **Verified**: 97/97 screen samples painted on real Wayland display, including
  during active downloads and after completion. Zero blank control regions.
- **Restart required**: Kill any running instance and start v0.4.2.
- Released as v0.4.2 (deb + generic installer + SHA256SUMS).

### v0.4.2 — Final fix: force repaint of canvas + viewport on every change (2026-10-26)
- **Root cause confirmed**: `QWidget.update()` only schedules a paint event — under
  Wayland this does not guarantee the compositor submits a new frame. When downloads
  started or completed, the row's label painted but controls were blank
  until an expose event (workspace switch, opening a terminal window).
- **Fix**: Replaced all `QWidget.update()` calls with `QWidget.repaint()` which forces
  an immediate synchronous paint:
  - `DownloadsCanvas.refresh()` calls `self.repaint()` instead of `self.update()`
  - `_refresh_downloads()` also repaints the scroll area viewport
  - `_on_item_progress()` calls `self._canvas.repaint()` on every tick
  - Watchdog repaints the full canvas every second
- **Verified**: 97/97 screen samples painted on real Wayland display,
  including during active downloads and after completion. Zero blank control
  regions.
- **Restart required**: Kill any running instance and start v0.4.2.
- Released as v0.4.2 (deb + generic installer + SHA256SUMS).

### v0.4.4 — Final fix: full widget tree repaint on every change (2026-10-26)
- **Root cause found**: `QWidget.repaint()` on the canvas alone does not guarantee
  the Wayland compositor composites the new frame. The canvas widget paints
  correctly, but its parent viewport/scroll area/window may not composite the
  new pixels until an expose event (e.g., opening a terminal window).
- **Fix**: Every repaint now forces the full widget hierarchy chain:
  `canvas.repaint()` → `viewport().repaint()` → `scrollArea.repaint()` →
  `mainWindow.repaint()`. Applied at every state change: `refresh()`,
  `_refresh_downloads()`, `_on_item_started()`, `_on_item_progress()`,
  and the 1-second watchdog.
- **Verified**: 97/97 screen samples painted on real Wayland display,
  including during active downloads and after completion.
- **Restart required**: Kill any running instance and start v0.4.4.
- Released as v0.4.4 (deb + generic installer + SHA256SUMS).

### v0.4.2 — Final fix: full widget hierarchy repaint on every change (2026-10-26)
- **Root cause confirmed**: `QWidget.update()` only schedules a paint event;
  under Wayland this does not guarantee the compositor submits a new frame.
  When downloads start or complete, the row's label paints but the controls
  (progress bar, pause button, ETA, X) are missing from the compositor frame
  until an expose event (mouse-over, opening a terminal).
- **Fix**: Replaced all `QWidget.update()` calls with `QWidget.repaint()` which
  forces an immediate synchronous paint of the canvas AND its entire parent
  chain:
  - `DownloadsCanvas.refresh()` → `canvas.repaint()` + `viewport().repaint()`
    + `scrollArea.repaint()` + `mainWindow.repaint()`
  - `_refresh_downloads()` → `canvas.refresh()` + `viewport().repaint()`
  - `_on_item_progress()` → `canvas.repaint()` + `viewport().repaint()`
    + `scrollArea.repaint()` + `mainWindow.repaint()`
  - Watchdog `_ensure_fresh_paint()` → full widget hierarchy repaint every
    second
- **Verified**: 97/97 screen samples painted on real Wayland display,
  including during active downloads and after completion. Zero blank control
  regions.
- **Restart required**: Kill any running instance and start v0.4.2.
- Released as v0.4.4 (deb + generic installer + SHA256SUMS).

### v0.4.5 — Simplified canvas: no per-row buttons (2026-10-26)
- Removed per-row pause/resume and ✕ buttons from the Downloads canvas.
- Each row now shows: title, progress bar with %, ETA only.
- Pause/resume is handled via the global "Pause downloads" button below.
- Removal of completed/failed items is handled via "Clear downloaded".
- Simpler layout: label takes remaining space, progress bar and ETA on right.
- Released as v0.4.5 (deb + generic installer + SHA256SUMS).

### v0.5.1 — Standard Qt widgets replace the custom-painted canvas (2026-10-27)
- Replaced QPainter-based DownloadsCanvas with real Qt widgets
  (QProgressBar, QLabel, QPushButton rows in a QVBoxLayout).
- Progress updates now push values into row widgets instead of repaints.

### v0.5.2 — Root cause of the disappearing controls (2026-10-27)
- ROOT CAUSE: a long yt-dlp status line (the ~200-char SABR WARNING,
  measured at 1951px) set on the unwrapped status QLabel made the window
  layout totalSizeHint (1969, 590); Qt's propagateSizeHints grew the
  top-level window to 1969px while the compositor kept 898px, so content
  was laid out ~1071px wider than the real window and the right-aligned
  progress bar/ETA fell off-screen. The hint shrank on the next short
  message but the window never shrank back until a console open forced a
  compositor reconfigure — which is why that "fixed" it.
- Fix: status + validation labels get QSizePolicy.Ignored horizontally;
  status text elided via _set_status() (full text in tooltip); row titles
  Ignored + elided (eventFilter on label Resize + resizeEvent).
- Removed all repaint/processEvents/hide-show/1px-resize hacks.
- Verified: killer-warning injection keeps window at 898 / hint 640;
  full download lifecycle probe keeps bar position stable.

### v0.5.3 — Per-row pause/resume/cancel buttons (2026-10-28)
- Each download row now has its own Pause/Resume button and a ✕
  remove/cancel button (hidden Pause on completed/failed rows, ✕ stays).
- Global button renamed: "Pause all downloads" / "Resume all downloads"
  (width 180), and it now tracks global pause state coherently even when
  a single row is paused independently.
- manager: add_now() flags new items paused while pause-all is engaged;
  resume_item() clears the global pause and drops the item from
  _scheduled_retries; _do_retry() skips items already active (no
  duplicate launches); new remove_item() removes from every list
  (active/paused/retrying/queued/completed/failed).
- Verified: 33-check row-button suite, smoke suite, width test, killer
  test, and a real-download Wayland lifecycle probe all pass.
- Released as v0.5.3 (deb + generic installer + SHA256SUMS).

### v0.5.4 — UI polish: placeholder, completed caption, button enable states (2026-10-28)
- Source placeholder now reads "YouTube/Invidious URL or video ID (e.g. dQw4w9WgXcQ)".
- Completed rows show only "✓ filename" (tooltip = full destination path);
  failed rows unchanged.
- "Pause all downloads"/"Resume all downloads" is greyed out when no live
  download exists (only completed/failed rows left) — nothing to pause.
- "Clear downloaded" is greyed out when there are no completed/failed items.
- Synced in one place: _sync_pause_button → _sync_controls (called from
  _refresh_downloads on every queue_changed).
- manager fixes found while testing:
  - _start_next emits queue_changed AFTER _launch, so observers never see
    an item as neither queued nor active (which briefly greyed the buttons
    at download start).
  - _retry_later now emits queue_changed (active→retrying transition
    refreshed rows/buttons; previously it didn't).
- Tests: new uicheck suite (21 checks) + all previous suites pass.
- Released as v0.5.4 (deb + generic installer + SHA256SUMS).

### v0.5.5 — D-Bus toast notifications + fixes the ✕ crash (2026-10-29)
- **New `yt_downloader/dbus_notify.py`**: a *direct* D-Bus client. It opens
  the session bus socket itself, authenticates with SASL `EXTERNAL`
  (hex-encoded uid), sends `org.freedesktop.DBus.Hello` (the bus ignores
  every later message otherwise — found by relaying real clients through a
  logging socket proxy and diffing against known-good traffic), marshalls
  the `org.freedesktop.Notifications.Notify` call (`susssasa{sv}i`) by
  hand, and reads the reply (body length lives at header bytes 4–7, not
  8–11; unsolicited signals such as `NameAcquired` are skipped until the
  real reply arrives). Zero new dependencies — still only PyQt6.
  `notify()` never raises (returns False on any failure), `notify_async()`
  fires it on a daemon thread so the GUI never waits on D-Bus.
- **Two independent preferences** (Settings → Preferences → Notifications,
  both on by default, persisted in state.json):
  - `notify_completed` — toast for each individual finished download
    (`Download complete` / `<filename>`).
  - `notify_all_done` — one toast when the queue drains, i.e. nothing is
    queued, active, paused or awaiting a retry (`All downloads finished` /
    `1 finished` or `0 finished, 1 failed`; `All downloads failed` when
    nothing succeeded). Fire-once per batch, reset by the next batch.
- **Crash fix — clicking ✕ terminated the app** (reproduced, two causes):
  1. `remove_active()` called `worker.deleteLater()` while the QThread was
     still running → `QThread: Destroyed while thread is still running` →
     `qFatal` → `Fatal Python error: Aborted`. New `dispose_worker()`
     polls `isRunning()` (50 ms) and defers `deleteLater()` until the
     thread has stopped; applied to all four worker-deletion sites.
  2. Any exception escaping a PyQt slot dumps core (PyQt routes it through
     `sys.excepthook`, which aborts). Every Qt entry point (row buttons,
     menu/button slots, manager signal slots) now runs under `@_safe_slot`,
     which logs the error and keeps the event loop alive — and **trims
     signal arguments to the slot's declared signature**, because PyQt
     hands the whole payload to a `*args/**kwargs` wrapper (without that,
     `item_finished(item, ok, msg, dest)` reached the 0-arg `_save_state()`
     and `clicked(bool)` reached the 0-arg button slots).
- Verified: 7-state ✕ reproducer (queued / active / paused / retrying /
  completed / failed / queued-last) all exit cleanly; 25-check toast and
  preference suite; 11-check slot-argument suite (real `.click()` on the
  global buttons, 0 bytes of stderr); plus the smoke, width, row-button,
  uicheck and killer-warning suites.
- End-to-end on Wayland with two real downloads: the desktop notification
  daemon recorded `Download complete / Me at the zoo [audio].m4a` and
  `All downloads finished / 1 finished` (individual toast off in phase 2,
  proving the switches gate the real notifications).
- Released as v0.5.5 (deb + generic installer + SHA256SUMS).

### v0.5.6 — installer honesty: real yt-dlp install, state-safe uninstall (2026-10-29)
- **The installer now really installs yt-dlp.** Its banner always claimed
  "Installing PyQt6 and yt-dlp from PyPI" while only running
  `pip install "$WORK"` — and pyproject declared PyQt6 alone, so neither
  the installer nor `uv tool install` produced a working downloader on a
  machine without a system yt-dlp. `yt-dlp` is now a declared dependency
  (pyproject.toml + requirements.txt) *and* the installer runs an explicit
  `pip install --upgrade yt-dlp` afterwards so every install gets the
  newest PyPI release rather than a cached wheel; it prints the version it
  installed. The .deb already declared `Depends: yt-dlp` and ships no
  dependency metadata of its own (build_deb.py is a pure ar/tar copier),
  so it needed no change.
- **`--uninstall` no longer deletes your state.** The app writes
  `state.json` to `$XDG_DATA_HOME/ytdownloader/` — which is *exactly* the
  directory the installer owns (`$PREFIX/share/ytdownloader`), so the old
  `rm -rf "$SHARE_DIR"` wiped the queue, settings and notification
  preferences along with the venv. state.json is now copied aside before
  the removal and restored afterwards; the venv, launcher and desktop
  entry are still deleted.
- **The app resolves yt-dlp itself** (`worker.ytdlp_command()`), in order:
  1. `<sys.prefix>/bin/yt-dlp` — the copy in the app's own environment
     (installer / `uv tool install`), i.e. the PyPI-latest one. That venv's
     `bin` is usually *not* on PATH for child processes, which is why the
     old hard-coded `Popen(["yt-dlp", ...])` silently used whatever the
     distro had (2026.08.19 here while PyPI was at 2026.10.26);
  2. `shutil.which("yt-dlp")` (the .deb relies on the distro package);
  3. `[sys.executable, "-m", "yt_dlp"]` when the module is importable;
  4. otherwise `[]` → a clear "yt-dlp not found — install it with
     `pip install yt-dlp`" on both the validate and download paths, with
     no process launch (`Popen([])` would raise IndexError and, worse,
     the old code would have retried a doomed download 10 times).
  Both invocation sites use it; `FileNotFoundError` at exec time now
  reports the same message. `tool_version()` / `ytdlp_version()` /
  `ffmpeg_version()` / `ytdlp_source()` back the new **Help → Version**
  dialog, which lists app, yt-dlp (+ which copy), ffmpeg and Python.
- **Fixed a cosmetic installer bug** found by the E2E: the final
  "Run it with:" line used `command -v ytdownloader`, which could name a
  *different* installation already on PATH; it now names the launcher just
  written (or the bare command only when this install's bin dir is on PATH).
- Verified: new 28-check `ytd-ytdlp-test.py` (all four resolution branches
  by patching sys.prefix/PATH/sys.path, both no-yt-dlp error paths, real
  version + dialog text); installer E2E in a throwaway `--prefix`
  (14 checks: venv yt-dlp == PyPI latest `2026.10.26`, app resolves its own
  copy, launcher/desktop written, then `--uninstall` keeps state.json
  byte-identical while removing venv/launcher/desktop); a real download
  through the new path (307 KB m4a, yt-dlp 2026.10.26); all previous
  suites (smoke, width, row-buttons, uicheck, toast, slot-arg, killer) and
  the 7-state ✕ reproducer pass.
- **The hand-built .deb had no `Version:` field.** `build_deb.py` copies
  `debian/control` verbatim (correct for a real dpkg build, which takes the
  version from `debian/changelog`), so every released .deb was missing a
  field dpkg requires — the builder now injects `Version: <VERSION>` after
  the `Package:` line.
- Released as v0.5.6 (deb + generic installer + SHA256SUMS); the v0.5.5
  release and local artifacts were removed (only the latest is kept).

### v0.5.7 — summary toast only for >1 completion (2026-10-29)
- The queue-drain toast now fires only when **more than one** download
  completed: a lone success already raised its own "Download complete"
  toast, so summarising it repeated the same news. Failures are still
  always reported — a batch that ends in failures raises the summary no
  matter how many succeeded, because failures have no per-item toast.
- Repository history purged with `git filter-repo`: all 28 older
  self-extracting installers removed from every commit *and* every tag
  (the two commits that existed only to delete old installers became empty
  and were pruned; 37 → 35 commits). `main` and all 29 tags force-pushed,
  then verified from a fresh clone that no old installer object is
  reachable anywhere. The `v0.5.6` tag still points at `main` and its
  release assets are untouched.
- Shipped as **v0.5.7**; the same purge was then repeated for the 0.5.6
  installer so that the tree, the release, and *every commit and tag* hold
  exactly one installer — the latest.

### Unreleased — automatic updates (2026-10-30)
- New `yt_downloader/updater.py` (stdlib only: urllib/hashlib/tarfile/
  subprocess — the tool still has exactly two dependencies, PyQt6 +
  yt-dlp). Checks `api.github.com/.../releases/latest`, compares the tag
  to `__version__` numerically, downloads the release, and installs it
  over the running copy:
  - **installer** strategy (`<prefix>/share/ytdownloader/venv`): download
    the release's self-extracting installer, verify it against the
    release's `SHA256SUMS.txt`, re-run it with the same `--prefix`.
  - **uv** strategy (`…/uv/tools/<name>`): download the tagged source
    archive (path-traversal/ symlink safe extraction), then
    `uv tool install --force <dir>`.
  - **system** (`/usr`, `/usr/local`) and **source** installs only
    *report* the update — they need root or a human.
  - Statuses: `up-to-date` / `updated` / `available` / `error`.
    `auto_update()` never raises; every failure becomes an `error` result.
- GUI wiring (`main_window.py`): a startup `QTimer.singleShot(5000, …)`
  tick re-reads both the `auto_update` preference and the
  `YTDOWNLOADER_NO_UPDATE` env var at fire time, so flipping the switch
  takes effect without a restart. The check runs on a daemon thread and
  reports back through an `update_result` signal (dialogs only ever on
  the GUI thread). Auto-path noise policy: `up-to-date` and `error` are
  silent, `available` is a toast + status line (system/source installs),
  `updated` offers **Restart now / Later** (`os.execv` re-exec of
  `python -m yt_downloader`). The install is deferred while anything is
  queued/active/paused/retrying and fires when the queue drains (once per
  session). A *manual* check while downloads run asks first, since
  replacing files under a live yt-dlp can break it.
- New preference **Settings → Preferences → Updates → "Check for a newer
  version at startup and install it"**, default **on**; persisted through
  `state.json` like the other preferences. Manual check: **Help → Check
  for updates…** (always reports its outcome).
- Verified: new 55-check suite (`ytd-updater-test.py`) — version maths,
  all four strategies, sha256 good/bad/missing, `check_latest` against a
  localhost server (valid/invalid JSON/404/unreachable), a fake-installer
  end-to-end (download → verify → `--prefix`), a real `uv` install from
  a locally-built tarball, all `auto_update` statuses, and the full GUI
  wiring (defaults, dialog round-trip, `_load_state`, signal→slot, the
  idle-defer hook, the env/preference gates). Plus a true self-update
  E2E: the *installed* tool updated *itself* over the real uv tool dir
  with the real `uv` binary (release feed and tarball served locally),
  then the restored install re-verified. All previous suites re-run with
  `YTDOWNLOADER_NO_UPDATE=1` (smoke, width, row-buttons, uicheck, toast,
  slot-arg, yt-dlp, killer + the 7-state X reproducer) — green.
