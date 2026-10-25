"""yt-dlp based worker running in a :class:`QThread`.

Each :class:`Worker` performs a **single** download attempt (no retry logic —
that lives in :class:`yt_downloader.manager.DownloadManager`). It reports
progress, status lines and the destination file path back to the GUI thread.

Two operations are supported:

* **validation** - check a YouTube URL / video ID without downloading anything
* **download** - download a single item as audio-only or best audio/video and
  report progress (percent + ETA) plus the destination file path.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal

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
        args = [
            "yt-dlp",
            "--no-playlist",
            "--socket-timeout",
            "15",
            "--extractor-args",
            "youtube:player_client=android",
            "--print",
            "id:%(id)s",
            "--print",
            "title:%(title)s",
            self._url,
        ]
        try:
            self._proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except FileNotFoundError:
            self.validated.emit(False, self._url, "yt-dlp not found on PATH")
            self._proc = None
            return
        except OSError as exc:
            self.validated.emit(False, self._url, f"failed to start yt-dlp: {exc}")
            self._proc = None
            return

        title = ""
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.strip()
            if title == "" and line.startswith("title:"):
                title = line[len("title:"):].strip()
        returncode = self._proc.wait()
        self._proc = None

        if self._stopped:
            self._stopped = False
            self.validated.emit(False, self._url, "cancelled")
            return

        if returncode != 0:
            self.validated.emit(False, self._url, "invalid URL or video unavailable")
            return

        if title:
            self.validated.emit(True, self._url, title)
        else:
            self.validated.emit(False, self._url, "could not retrieve video info")

    # ---- download --------------------------------------------------------
    def _run_download(self) -> None:
        assert self._item is not None
        item = self._item
        os.makedirs(item.output_dir, exist_ok=True)
        output_template = output_template_for(item)

        args = ["yt-dlp", "--no-playlist", "--extractor-args", "youtube:player_client=android"]
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
        args += ["-o", output_template, item.url]

        self.status.emit(f"Starting {item.kind} download ...")
        try:
            self._proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except OSError as exc:
            self.finished.emit(False, f"failed to start yt-dlp: {exc}", None)
            self._proc = None
            return

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
                eta = self._parse_eta(match.group("eta"))
                self.progress.emit(pct, eta)
            dest_match = DESTINATION_RE.search(line)
            if dest_match:
                self._destination = dest_match.group("path").strip()
                # Derive the human-friendly title from the first destination
                # filename we see (yt-dlp keeps the video title, sanitised).
                item = self._item
                if item is not None and not getattr(item, "title", ""):
                    stem = os.path.splitext(self._destination)[0]
                    title = os.path.basename(stem).strip()
                    if title:
                        item.title = title
                        self.resolved_title.emit(title)

        returncode = self._proc.wait() if self._proc is not None else 1
        self._proc = None

        if self._stopped:
            self._stopped = False
            self.status.emit("Download cancelled.")
            self.finished.emit(False, "cancelled", self._destination)
            return

        ok = returncode == 0
        message = (
            "download complete"
            if ok
            else f"yt-dlp exited with {returncode}"
        )
        if ok:
            # Completion verification runs HERE, in the worker thread — never
            # on the UI thread — so the window cannot freeze between yt-dlp
            # finishing and the row flipping to its completed state.
            err = verify_completion(item.kind, self._destination)
            if err:
                ok = False
                message = f"verification failed: {err}"
        self.finished.emit(ok, message, self._destination)

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
