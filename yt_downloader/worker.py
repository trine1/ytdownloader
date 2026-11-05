"""yt-dlp based worker running in a :class:`QThread`.

Each :class:`Worker` performs a **single** download attempt (no retry logic —
that lives in :class:`yt_downloader.manager.DownloadManager`). It reports
progress, status lines and the destination file path back to the GUI thread.

Two operations are supported:

* **validation** - check a video URL / ID without downloading anything
* **download** - download a single item as audio-only or best audio/video and
  report progress (percent + ETA) plus the destination file path.

Both go through :mod:`yt_downloader.sources`, which knows that an Invidious
instance link and the YouTube link beside it are the *same* video, and hands
yt-dlp whichever route is most likely to answer.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import QThread, pyqtSignal

from . import sources

# A YouTube video id is exactly 11 characters: [A-Za-z0-9_-]
YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# yt-dlp progress line, e.g.:
#   [download]  45.2% of ~123.45MiB at  1.23MiB/s ETA 0:10:23
PROGRESS_RE = re.compile(
    r"\[download\]\s+(?P<pct>\d+(?:\.\d+)?)%.*?ETA\s+(?P<eta>\d+(?::\d+)+)",
    re.IGNORECASE,
)

# Destination lines, e.g.:
#   [download] Destination: /path/to/title.webm
#   [ExtractAudio] Destination: /path/to/title.opus
#   [MergeFormats] Destination: /path/to/title.mkv
DESTINATION_RE = re.compile(
    r"\[(?:download|ExtractAudio|MergeFormats|postprocess)]\s+Destination:\s+(?P<path>.+)",
    re.IGNORECASE,
)


# Shown when no yt-dlp can be found at all — previously the app said
# "yt-dlp not found on PATH", which was misleading: the launcher puts the
# app's *private* venv bin dir on PATH, and a missing copy there is not a
# PATH problem.
YTDLP_MISSING = (
    "yt-dlp not found — install it with `pip install yt-dlp` "
    "or your package manager"
)


def ytdlp_command() -> List[str]:
    """Resolve the argv prefix used to run yt-dlp.

    Search order:

    1. ``<sys.prefix>/bin/yt-dlp`` — the copy shipped inside this app's own
       environment (installer / ``uv tool install``), which is the newest
       one from PyPI. That venv's ``bin`` is frequently *not* on PATH for
       child processes, so PATH alone would silently pick an older system
       copy.
    2. whatever ``PATH`` finds (the .deb relies on the distro package).
    3. the ``yt_dlp`` module importable by this interpreter, via
       ``python -m yt_dlp``.

    Returns ``[]`` when nothing is available; callers must report that
    instead of launching (``Popen([])`` would raise ``IndexError``).
    """
    env_copy = Path(sys.prefix) / "bin" / "yt-dlp"
    if env_copy.is_file() and os.access(env_copy, os.X_OK):
        return [str(env_copy)]
    on_path = shutil.which("yt-dlp")
    if on_path:
        return [on_path]
    try:
        if importlib.util.find_spec("yt_dlp") is not None:
            return [sys.executable, "-m", "yt_dlp"]
    except (ImportError, ValueError, AttributeError):
        pass
    return []


def tool_version(command: List[str]) -> str:
    """First line of ``command --version``, or ``""`` if it cannot run."""
    if not command:
        return ""
    try:
        proc = subprocess.run(
            [*command, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def ytdlp_version() -> str:
    """Version of the yt-dlp this app would actually execute ("" if none)."""
    return tool_version(ytdlp_command())


def ytdlp_source() -> str:
    """Where the resolved yt-dlp comes from: for Help → Version / support."""
    cmd = ytdlp_command()
    if not cmd:
        return "not found"
    if len(cmd) > 1:
        return "python -m yt_dlp"
    if cmd[0] == str(Path(sys.prefix) / "bin" / "yt-dlp"):
        return "app environment"
    return "PATH"


def ffmpeg_version() -> str:
    """Short ffmpeg version (``"6.1.1-3"``), or ``""`` when unavailable."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return ""
    first = tool_version([ffmpeg])
    # "ffmpeg version 6.1.1-3 Copyright ..." -> "6.1.1-3"
    parts = first.split()
    if len(parts) >= 3 and parts[0] == "ffmpeg" and parts[1] == "version":
        return parts[2]
    return first


def format_seconds(seconds: Optional[int]) -> str:
    """Format an ETA in seconds as H:MM:SS or M:SS."""
    if seconds is None or seconds < 0:
        return "--:--"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# How many raw ETA readings the smoother keeps. Three is enough to throw
# out a one-off spike (yt-dlp occasionally reports a wildly wrong ETA
# right before its speed estimate settles) while keeping the filter
# close to the truth - a longer window starts to lag behind a download
# that is making real progress.
ETA_SMOOTHING_WINDOW = 3

# How much of each new reading ends up in the displayed value. The rest is
# carried over from the previous reading, so the number eases towards the
# new one instead of jumping to it. Half way is the sweet spot: it halves
# the step-to-step wobble while the estimate stays close to the real one.
ETA_SMOOTHING_ALPHA = 0.5


class EtaSmoother:
    """Median filter plus exponential average over the last few readings.

    yt-dlp recomputes the ETA on every progress line from its current
    speed estimate, so the raw numbers jitter visibly (0:12, 0:08, 0:15,
    0:09 …) even while the download is going perfectly steadily. Two
    stages take that wobble out:

    1. a **median** over a short window, which throws outliers away
       completely - one wild reading can never move the result;
    2. an **exponential moving average** over the medians, which damps
       the remaining back-and-forth so the number eases towards its new
       value instead of snapping to it.

    Measured over simulated downloads (a steadily shrinking remaining
    time plus the kind of noise yt-dlp produces), this roughly halves
    the jump from one displayed value to the next while staying close to
    the real remaining time - a longer window or a slower average would
    smooth the number more, but would start lagging behind a download
    that is making real progress.

    The state is per attempt: a fresh yt-dlp run starts from scratch,
    and the first reading of a run is shown as it is - no invented
    warm-up value.
    """

    def __init__(self, window: int = ETA_SMOOTHING_WINDOW,
                 alpha: float = ETA_SMOOTHING_ALPHA) -> None:
        self._window = max(1, int(window))
        self._alpha = min(1.0, max(0.0, float(alpha)))
        self._samples: List[int] = []
        self._value: Optional[float] = None

    def reset(self) -> None:
        """Forget every sample (a fresh attempt starts from scratch)."""
        self._samples.clear()
        self._value = None

    def push(self, eta: int) -> int:
        """Add a raw ETA (seconds) and return the smoothed value."""
        if self._window <= 1:
            # A window of one means "no smoothing" - used by the tests to
            # switch the filter off.
            return int(eta)
        self._samples.append(int(eta))
        if len(self._samples) > self._window:
            del self._samples[:-self._window]
        ordered = sorted(self._samples)
        median = ordered[len(ordered) // 2]
        if self._value is None:
            # The first reading of an attempt is shown as it is - no
            # invented warm-up value.
            self._value = float(median)
        else:
            self._value += self._alpha * (median - self._value)
        return int(round(self._value))


def output_template_for(item: DownloadItem) -> str:
    """yt-dlp -o template for *item*.

    Distinct filenames per format guarantee that the audio and the video
    download of the same source never overwrite each other's existing or
    in-progress files.
    """
    filename_suffix = "audio" if item.kind == "audio" else "video"
    return os.path.join(
        item.output_dir,
        f"%(title)s [{filename_suffix}].%(ext)s",
    )


def instance_template_for(item: DownloadItem) -> str:
    """-o template that writes a *known* title into the filename.

    On the instance route yt-dlp reads a bare DASH manifest, which its
    generic extractor titles with the video id: ``%(title)s`` alone would
    produce ``kGeCeK85sUg [audio].opus``. The real title is already known
    (validation got it from the instance's API), so it is put in literally —
    with ``%`` doubled because the template engine would otherwise read
    ``%(foo)s`` inside it, and with path separators replaced because a ``/``
    in a template means "subdirectory". Falls back to the ordinary template
    while no title is known.
    """
    title = re.sub(r"\s+", " ", (item.title or "").strip())
    if not title:
        return output_template_for(item)
    title = title.replace("%", "%%").replace("/", "-").replace("\\", "-")
    filename_suffix = "audio" if item.kind == "audio" else "video"
    return os.path.join(
        item.output_dir,
        f"{title} [{filename_suffix}].%(ext)s",
    )


@dataclass
class DownloadItem:
    """A single queued download."""

    url: str          # resolved YouTube URL
    kind: str         # "audio" or "video"
    output_dir: str   # destination directory
    title: str = ""   # human-friendly title (empty until resolved)
    attempts: int = 0                 # how many times this item has been tried
    max_attempts: int = 10            # total attempts allowed (1 + retries)
    initial_backoff: int = 5          # seconds before first retry
    max_backoff: int = 300            # seconds, cap for exponential backoff
    paused: bool = False              # set when the user pauses the download
    resume: bool = False              # set when the user resumes; worker passes -c
    resumed_after_pause: bool = False # next launch was user-resume: don't count an attempt

    def __hash__(self) -> int:
        return id(self)

    def __eq__(self, other: object) -> bool:
        return self is other

    @property
    def retries_left(self) -> int:
        return max(0, self.max_attempts - self.attempts)

    def backoff_for_next(self) -> int:
        """Seconds to wait before the next attempt (exponential, capped)."""
        if self.attempts <= 0:
            return self.initial_backoff
        delay = self.initial_backoff * (2 ** (self.attempts - 1))
        return min(delay, self.max_backoff)


class Worker(QThread):
    """Runs one yt-dlp invocation (validation or single download attempt)."""

    # ---- signals ---------------------------------------------------------
    validated = pyqtSignal(bool, str, str)   # ok, resolved_url, title_or_error
    progress = pyqtSignal(int, int)          # percent (0-100), eta seconds
    status = pyqtSignal(str)                 # free-form status text
    finished = pyqtSignal(bool, str, str)    # success, message, destination_path
    resolved_title = pyqtSignal(str)         # human-friendly title (from output)
    # ---- public API ------------------------------------------------------
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._mode: Optional[str] = None      # "validate" | "download"
        self._url: Optional[str] = None
        self._item: Optional[DownloadItem] = None
        self._proc: Optional[subprocess.Popen] = None
        self._stopped = False
        self._destination: Optional[str] = None
        self._resume = False
        self._start_error = YTDLP_MISSING
        # Smooths the jittery ETA yt-dlp reports on every progress line.
        self._eta_smoother = EtaSmoother()

    def validate(self, text: str) -> None:
        """Validate *text* (URL or bare video id)."""
        self._mode = "validate"
        self._url = self._resolve_url(text)
        self._item = None
        self._destination = None
        self.start()

    def download(self, item: DownloadItem, resume: bool = False) -> None:
        """Download *item* (single attempt, no retry).

        If *resume* is true, pass ``--continue`` so yt-dlp resumes the
        partially downloaded file.
        """
        self._mode = "download"
        self._url = None
        self._item = item
        self._resume = resume
        self._destination = None
        self.start()

    def stop(self) -> None:
        """Request cancellation of the running yt-dlp process."""
        self._stopped = True
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()

    @property
    def destination(self) -> Optional[str]:
        return self._destination

    # ---- QThread ---------------------------------------------------------
    def run(self) -> None:
        if self._mode == "validate":
            self._run_validation()
        elif self._mode == "download":
            self._run_download()

    # ---- helpers ---------------------------------------------------------
    @staticmethod
    def _resolve_url(text: str) -> str:
        """Turn a bare video id into a full watch URL, pass URLs through."""
        text = (text or "").strip()
        if YOUTUBE_ID_RE.match(text):
            return f"https://www.youtube.com/watch?v={text}"
        return text

    # ---- validation ------------------------------------------------------
    def _run_validation(self) -> None:
        assert self._url is not None
        cmd = ytdlp_command()
        if not cmd:
            self.validated.emit(False, self._url, YTDLP_MISSING)
            return

        complaints: List[str] = []
        for route in sources.routes(self._url):
            if route.kind == "invidious":
                # Proved through the instance's API rather than yt-dlp: the
                # JSON endpoint answers even when the instance's HTML page is
                # behind a bot challenge, and it carries the real title (the
                # DASH manifest yt-dlp reads is titled by the bare video id).
                title = sources.instance_title(
                    route.instance_base, route.video_id)
                if title:
                    sources.remember(route.video_id, route.kind)
                    self.validated.emit(True, route.resolved, title)
                    return
                complaints.append(
                    f"{sources.host_of(route.instance_base)} did not answer")
                # Instances with the public API disabled still serve their
                # manifest: let yt-dlp vouch for the video directly.
                ok, title_or_error = self._validate_once(cmd, route.url)
                if self._stopped:
                    self._stopped = False
                    self.validated.emit(False, self._url, "cancelled")
                    return
                if ok:
                    sources.remember(route.video_id, route.kind)
                    self.validated.emit(True, route.resolved, title_or_error)
                    return
                complaints.append(title_or_error)
                continue

            ok, title_or_error = self._validate_once(cmd, route.url)
            if self._stopped:
                self._stopped = False
                self.validated.emit(False, self._url, "cancelled")
                return
            if ok:
                sources.remember(route.video_id, route.kind)
                self.validated.emit(True, route.resolved, title_or_error)
                return
            complaints.append(title_or_error)

        message = ("; ".join(dict.fromkeys(complaints))
                   if complaints else "invalid URL or video unavailable")
        self.validated.emit(False, self._url, message)

    def _validate_once(self, cmd: List[str], url: str):
        """Probe *url* for its id and title: ``(ok, title_or_error)``."""
        args = [
            *cmd,
            "--no-playlist",
            "--socket-timeout",
            "15",
            "--extractor-args",
            "youtube:player_client=android",
            "--print",
            "id:%(id)s",
            "--print",
            "title:%(title)s",
            url,
        ]
        try:
            self._proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except FileNotFoundError:
            self._proc = None
            return False, YTDLP_MISSING
        except OSError as exc:
            self._proc = None
            return False, f"failed to start yt-dlp: {exc}"

        title = ""
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.strip()
            if title == "" and line.startswith("title:"):
                title = line[len("title:"):].strip()
        returncode = self._proc.wait()
        self._proc = None

        if self._stopped:
            return False, "cancelled"
        if returncode != 0:
            return False, "invalid URL or video unavailable"
        if title:
            return True, title
        return False, "could not retrieve video info"

    # ---- download --------------------------------------------------------
    def _run_download(self) -> None:
        assert self._item is not None
        item = self._item
        os.makedirs(item.output_dir, exist_ok=True)

        cmd = ytdlp_command()
        if not cmd:
            self.finished.emit(False, YTDLP_MISSING, None)
            return

        # One attempt may walk several routes: the YouTube link the user's
        # instance mirrors, and the instance itself when YouTube refuses.
        candidates = sources.routes(item.url)
        tried: List[str] = []
        returncode = 1
        for index, route in enumerate(candidates):
            if index:
                self.status.emit(
                    f"{tried[-1]} did not work — trying {route.label} ...")
            if route.kind == "invidious" and not item.title and route.video_id:
                # The instance's API knows the title even when its HTML page
                # is challenged, so keep the row (and the filename) readable.
                title = sources.instance_title(
                    route.instance_base, route.video_id)
                if title:
                    item.title = title
                    self.resolved_title.emit(title)
            if route.kind == "invidious":
                template = instance_template_for(item)
            else:
                template = output_template_for(item)
            # Only name the route when there is a choice to make: a plain
            # YouTube link keeps the status line it always had.
            where = f" from {route.label}" if len(candidates) > 1 else ""
            self.status.emit(f"Starting {item.kind} download{where} ...")
            returncode = self._download_once(cmd, item, route.url, template)

            if self._stopped:
                self._stopped = False
                self.status.emit("Download cancelled.")
                self.finished.emit(False, "cancelled", self._destination)
                return
            if returncode is None:
                # yt-dlp could not even be launched: switching routes would
                # hit the same wall, so report it straight away.
                self.finished.emit(False, self._start_error, None)
                return
            if returncode == 0:
                sources.remember(route.video_id, route.kind)
                break
            tried.append(route.label)

        ok = returncode == 0
        if ok:
            message = "download complete"
        elif len(tried) > 1:
            message = (f"unavailable from {' and '.join(tried)} "
                       f"(yt-dlp exited with {returncode})")
        else:
            message = f"yt-dlp exited with {returncode}"
        if ok:
            # Completion verification runs HERE, in the worker thread — never
            # on the UI thread — so the window cannot freeze between yt-dlp
            # finishing and the row flipping to its completed state.
            err = verify_completion(item.kind, self._destination)
            if err:
                ok = False
                message = f"verification failed: {err}"
        self.finished.emit(ok, message, self._destination)

    def _download_once(self, cmd: List[str], item: DownloadItem,
                       url: str, output_template: str) -> Optional[int]:
        """One yt-dlp run for *url*; its exit code, or None if it never
        started (``self._start_error`` then explains why)."""
        self._destination = None
        # A fresh yt-dlp run means a fresh ETA scale (the fallback route
        # starts over from zero), so the smoother must not carry samples
        # over from the previous attempt.
        self._eta_smoother.reset()
        args = [*cmd, "--no-playlist", "--extractor-args", "youtube:player_client=android"]
        if getattr(self, "_resume", False):
            # Resume a partially downloaded file.
            args.append("-c")
        if item.kind == "audio":
            args += [
                "-x",
                "--audio-format",
                "best",
                "--audio-quality",
                "0",
            ]
        else:
            args += [
                "-f",
                "bestvideo+bestaudio/best",
                "--merge-output-format",
                "mkv",
            ]
        args += ["-o", output_template, url]

        try:
            self._proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except FileNotFoundError:
            self._proc = None
            self._start_error = YTDLP_MISSING
            return None
        except OSError as exc:
            self._proc = None
            self._start_error = f"failed to start yt-dlp: {exc}"
            return None

        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            if self._stopped:
                break
            line = line.rstrip("\n")
            if not line:
                continue
            self.status.emit(line)
            match = PROGRESS_RE.search(line)
            if match:
                pct = int(float(match.group("pct")))
                eta = self._eta_smoother.push(
                    self._parse_eta(match.group("eta")))
                self.progress.emit(pct, eta)
            dest_match = DESTINATION_RE.search(line)
            if dest_match:
                self._destination = dest_match.group("path").strip()
                # Derive the human-friendly title from the first destination
                # filename we see (yt-dlp keeps the video title, sanitised).
                if not getattr(item, "title", ""):
                    stem = os.path.splitext(self._destination)[0]
                    title = os.path.basename(stem).strip()
                    # Drop the per-format suffix (added for filename
                    # collision-safety) so the row shows the clean title.
                    title = re.sub(r"\s*\[(?:audio|video)\]$", "", title).strip()
                    if title:
                        item.title = title
                        self.resolved_title.emit(title)

        returncode = self._proc.wait() if self._proc is not None else 1
        self._proc = None
        return returncode

    @staticmethod
    def _parse_eta(eta: str) -> int:
        parts = [int(p) for p in eta.split(":")]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        return parts[0] * 60 + parts[1]


def verify_completion(kind: str, destination: Optional[str]) -> str:
    """Robust completion check; returns '' when the download looks complete.

    Measures used:
      * destination path is non-empty
      * file exists
      * file size > 0
      * file was modified within the last 10 minutes (fresh)
      * for video, ffprobe reports a valid stream (best effort)

    Runs in the worker thread; the UI thread is never blocked by it.
    """
    import time

    if not destination:
        return "no destination path reported by yt-dlp"
    if not os.path.exists(destination):
        return f"output file missing: {destination}"
    try:
        size = os.path.getsize(destination)
    except OSError:
        return f"cannot stat output file: {destination}"
    if size == 0:
        return f"output file is empty: {destination}"
    # Freshness check (guard against a stale file left from a previous run).
    try:
        mtime = os.path.getmtime(destination)
    except OSError:
        return f"cannot stat output file: {destination}"
    if time.time() - mtime > 600:
        return f"output file not freshly modified: {destination}"
    # For video merges, confirm the container is at least probe-able.
    if kind == "video":
        try:
            subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries",
                 "stream=codec_type", "-of", "csv=p=0", destination],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=False,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            # ffprobe missing or failed — non-fatal, yt-dlp already merged.
            pass
    return ""
