"""On-disk memory of validated and completed downloads.

Validating a pasted URL costs a network round-trip: yt-dlp has to resolve
the source and fetch its title. That work is wasted once the same SOURCE
has been proved — a title is true for every format, so validating audio
also proves video. This module remembers which sources the app has already
validated (or downloaded), and since the memory lives on disk it survives
restarts.

This module answers exactly one question, locally and instantly::

    has this source been proved recently, and what is its title?

It is deliberately *not* the downloads list: clearing completed rows in
the UI does not forget the history, and the history does not restore rows.

Writes are atomic (temp file + rename), the same pattern as state.json.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .manager import _canonical_source

HISTORY_DIR = Path(
    os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
) / "ytdownloader"
HISTORY_FILE = HISTORY_DIR / "downloads.json"
HISTORY_VERSION = 1

MAX_ENTRIES = 500      # hard cap; the oldest entries are dropped first
MAX_AGE_DAYS = 30      # entries older than this are pruned when saving


@dataclass
class HistoryEntry:
    """One completed download, as remembered for validation."""

    url: str
    kind: str            # "audio" | "video"
    title: str
    at: float            # unix timestamp of the completed download
    destination: str = ""


class DownloadHistory:
    """Persistent record of proved sources, queried by source alone.

    ``kind`` is carried on every entry (it labels future messages), but
    identity and matching are per SOURCE: one proof covers every format.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        # Resolved at call time (not as a default argument) so tests can
        # redirect HISTORY_FILE before a window is constructed.
        self._path = Path(path) if path is not None else HISTORY_FILE
        self._entries: List[HistoryEntry] = []
        self.load()

    # ---------------------------------------------------------------- queries
    def recent_match(
        self,
        url: str,
        within_seconds: float,
        now: Optional[float] = None,
    ) -> Optional[HistoryEntry]:
        """Entry for the same source recorded within the window.

        Sources compare canonically (watch?v=ID, youtu.be/ID, /shorts/ID
        and m.youtube.com all count as the same video). Format does NOT
        matter: what this answers is "has the app already proved this
        source and fetched its title recently?" — a fact true for every
        format. A non-positive window disables the cache: nothing ever
        matches.
        """
        if within_seconds <= 0:
            return None
        now = time.time() if now is None else now
        needle = _canonical_source(url)
        best: Optional[HistoryEntry] = None
        for entry in self._entries:
            if not (entry.url == url or _canonical_source(entry.url) == needle):
                continue
            if now - entry.at > within_seconds:
                continue
            if best is None or entry.at > best.at:
                best = entry
        return best

    # -------------------------------------------------------------- mutation
    def record(
        self,
        url: str,
        kind: str,
        title: str = "",
        destination: str = "",
        now: Optional[float] = None,
    ) -> HistoryEntry:
        """Remember that *url* was validated or downloaded, and persist.

        One entry per source (any format): the newest record replaces the
        previous one, keeping the freshest title and timestamp. The kind
        is retained only so callers can word their messages precisely.
        """
        now = time.time() if now is None else now
        needle = _canonical_source(url)
        self._entries = [
            e for e in self._entries
            if not (e.url == url or _canonical_source(e.url) == needle)
        ]
        entry = HistoryEntry(url=url, kind=kind, title=title or "", at=now,
                             destination=destination or "")
        self._entries.append(entry)
        self._prune()
        self.save()
        return entry

    def _prune(self) -> None:
        # Age is measured against the wall clock, not against the entry's
        # own timestamp: an entry is stale once it is older than
        # MAX_AGE_DAYS, whenever it was recorded.
        cutoff = time.time() - MAX_AGE_DAYS * 86400
        self._entries = [e for e in self._entries if e.at >= cutoff]
        if len(self._entries) > MAX_ENTRIES:
            self._entries.sort(key=lambda e: e.at)
            self._entries = self._entries[-MAX_ENTRIES:]

    # ----------------------------------------------------------- persistence
    def load(self) -> None:
        """Read the cache; a missing/corrupt/old file means an empty one."""
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self._entries = []
            return
        if not isinstance(payload, dict) or payload.get("version") != HISTORY_VERSION:
            self._entries = []
            return
        entries: List[HistoryEntry] = []
        for raw in payload.get("entries", []):
            try:
                entries.append(HistoryEntry(
                    url=str(raw["url"]),
                    kind=str(raw["kind"]),
                    title=str(raw.get("title", "")),
                    at=float(raw["at"]),
                    destination=str(raw.get("destination", "")),
                ))
            except (KeyError, TypeError, ValueError):
                continue                     # skip a damaged row, keep the rest
        self._entries = entries

    def save(self) -> None:
        """Write the cache atomically (temp file + rename)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": HISTORY_VERSION,
            "entries": [
                {"url": e.url, "kind": e.kind, "title": e.title,
                 "at": e.at, "destination": e.destination}
                for e in sorted(self._entries, key=lambda e: e.at)
            ],
        }
        tmp = self._path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, self._path)

    def __len__(self) -> int:
        return len(self._entries)
