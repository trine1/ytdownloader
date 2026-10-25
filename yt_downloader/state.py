"""State persistence for ytdownloader.

Saves and loads the application state (settings, queued downloads, active
downloads, and the last source text) to a JSON file so that pending and
in-progress downloads survive restarts, crashes, and system shutdowns.

State file location::

    $XDG_DATA_HOME/ytdownloader/state.json
    (~/.local/share/ytdownloader/state.json by default)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from .worker import DownloadItem

STATE_DIR = Path(
    os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
) / "ytdownloader"
STATE_FILE = STATE_DIR / "state.json"
STATE_VERSION = 1


def _item_to_dict(item: DownloadItem) -> dict[str, Any]:
    """Serialise a :class:`DownloadItem` to a plain dict."""
    return {
        "url": item.url,
        "kind": item.kind,
        "output_dir": item.output_dir,
        "attempts": item.attempts,
        "max_attempts": item.max_attempts,
        "initial_backoff": item.initial_backoff,
        "max_backoff": item.max_backoff,
        "paused": item.paused,
        "resume": item.resume,
    }


def _item_from_dict(data: dict[str, Any]) -> DownloadItem:
    """Deserialise a plain dict back into a :class:`DownloadItem`."""
    return DownloadItem(
        url=data["url"],
        kind=data["kind"],
        output_dir=data.get("output_dir", ""),
        attempts=data.get("attempts", 0),
        max_attempts=data.get("max_attempts", 10),
        initial_backoff=data.get("initial_backoff", 5),
        max_backoff=data.get("max_backoff", 300),
        paused=data.get("paused", False),
        resume=data.get("resume", False),
    )


def save_state(
    settings: dict[str, Any],
    queue_items: list[DownloadItem],
    active_items: list[DownloadItem],
    url_input: str = "",
) -> None:
    """Persist the current application state to disk.

    Writes to a temporary file and atomically renames it so that a crash
    mid-write never leaves a corrupt state file.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": STATE_VERSION,
        "settings": settings,
        "queue": [_item_to_dict(it) for it in queue_items],
        "active": [_item_to_dict(it) for it in active_items],
        "url_input": url_input,
    }
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, STATE_FILE)


def load_state() -> Optional[dict[str, Any]]:
    """Load persisted state from disk.

    Returns a dict with keys ``settings``, ``queue`` (list of dicts),
    ``active`` (list of dicts), and ``url_input``, or ``None`` if no valid
    state file exists.
    """
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if payload.get("version") != STATE_VERSION:
        return None
    return payload
