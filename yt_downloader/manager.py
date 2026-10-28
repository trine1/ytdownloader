"""Download manager: queue, parallel workers, auto-retry and completion checks."""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from .worker import DownloadItem, Worker, format_seconds

# Canonical source key for duplicate detection: most YouTube link shapes can
# be reduced to their 11-character video id.
_SOURCE_ID_RES = tuple(
    re.compile(pattern) for pattern in (
        r"[?&]v=([A-Za-z0-9_-]{11})(?:&|$)",
        r"youtu\.be/([A-Za-z0-9_-]{11})",
        r"/(?:shorts|embed|live)/([A-Za-z0-9_-]{11})",
    )
)


def _canonical_source(url: str) -> str:
    """Reduce a YouTube URL of any shape to its video id, else pass through."""
    for res in _SOURCE_ID_RES:
        m = res.search(url or "")
        if m:
            return m.group(1)
    return url or ""


class DownloadManager(QObject):
    """Manages a queue of downloads with parallelism and auto-retry.

    Signals
    -------
    item_validated(ok, url, title_or_error)
        Emitted when a validation finishes.
    item_started(item)
        Emitted when an attempt begins for *item*.
    item_progress(item, percent, eta_seconds)
        Progress update for an active item.
    item_status(item, message)
        Free-form status update for an item.
    item_finished(item, success, message, destination)
        Emitted when *item* is done (success, or failure after exhausting retries).
    item_completed(item, destination)
        Emitted when *item* completes successfully (also added to completed list).
    queue_changed()
        Emitted whenever the waiting queue or active set changes.
    """

    item_validated = pyqtSignal(bool, str, str)
    item_started = pyqtSignal(object)        # DownloadItem
    item_progress = pyqtSignal(object, int, int)  # DownloadItem, pct, eta_s
    item_status = pyqtSignal(object, str)    # DownloadItem, message
    item_finished = pyqtSignal(object, bool, str, str)  # item, ok, msg, dest
    item_completed = pyqtSignal(object, str)  # item, destination
    queue_changed = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._queue: List[DownloadItem] = []
        self._active: Dict[Worker, DownloadItem] = {}
        # Paused items that were being downloaded (no worker now, in limbo).
        self._paused_items: List[DownloadItem] = []
        # Failed items awaiting a scheduled retry (no worker yet).
        self._scheduled_retries: List[DownloadItem] = []
        self._completed: List[DownloadItem] = []
        self._failed: List[DownloadItem] = []
        self._validation_workers: set = set()
        self._title_workers: set = set()
        self._paused_workers: set = set()   # workers stopped by user pause
        self._cancelled_workers: set = set()  # workers stopped for removal
        self._global_paused = False         # set by pause_all()
        self._max_parallel = 1
        self._default_max_attempts = 10
        self._default_initial_backoff = 5
        self._default_max_backoff = 300
        self._stop_all = False

    # ---- settings --------------------------------------------------------
    def configure(
        self,
        max_parallel: int = 1,
        max_attempts: int = 10,
        initial_backoff: int = 5,
        max_backoff: int = 300,
    ) -> None:
        self._max_parallel = max(1, int(max_parallel))
        self._default_max_attempts = max(1, int(max_attempts))
        self._default_initial_backoff = max(1, int(initial_backoff))
        self._default_max_backoff = max(1, int(max_backoff))

    # ---- public API ------------------------------------------------------
    def validate(self, text: str) -> None:
        """Run a one-off validation (not affected by queue/parallelism)."""
        worker = Worker()
        self._validation_workers.add(worker)
        worker.validated.connect(self.item_validated)
        worker.finished.connect(
            lambda *_, w=worker: self._validation_workers.discard(w)
        )
        worker.finished.connect(lambda *_: worker.deleteLater())
        worker.validate(text)

    def matching_item(self, url: str, kind: str) -> Optional[DownloadItem]:
        """An existing item for the same (source, format) pair, in any state.

        Sources are compared canonically: watch?v=ID, youtu.be/ID,
        m.youtube.com/watch?v=ID and /shorts/ID all count as the same video,
        so e.g. the desktop and mobile links of one clip are recognised as
        duplicates. Completed and failed items are included so callers can
        offer to re-download. The same source in a DIFFERENT format is a
        separate item (the filenames differ on disk).
        """
        def same(it: DownloadItem) -> bool:
            return (it.url == url
                    or _canonical_source(it.url) == _canonical_source(url)) \
                and it.kind == kind
        for pool in (list(self._active.values()), self._paused_items,
                     self._queue, self._scheduled_retries,
                     self._completed, self._failed):
            for it in pool:
                if same(it):
                    return it
        return None

    def _make_item(self, url: str, kind: str, output_dir: str,
                   title: str = "") -> DownloadItem:
        return DownloadItem(
            url=url,
            kind=kind,
            output_dir=output_dir,
            title=title or "",
            max_attempts=self._default_max_attempts,
            initial_backoff=self._default_initial_backoff,
            max_backoff=self._default_max_backoff,
        )

    def add(self, url: str, kind: str, output_dir: str, title: str = "") -> DownloadItem:
        """Append *url* to the end of the waiting queue ("Download")."""
        item = self._make_item(url, kind, output_dir, title)
        self._queue.append(item)
        self.queue_changed.emit()
        self._start_next()
        return item

    def add_now(self, url: str, kind: str, output_dir: str, title: str = "") -> DownloadItem:
        """Insert *url* at the front of the queue ("Download")."""
        item = self._make_item(url, kind, output_dir, title)
        # While pause-all is active, a new download joins the paused state
        # instead of slipping past it (its row then offers "Resume").
        if self._global_paused:
            item.paused = True
        self._queue.insert(0, item)
        self.queue_changed.emit()
        self._start_next()
        return item

    def remove_queued(self, index: int) -> bool:
        """Remove a waiting item by queue index. Returns True if removed."""
        if 0 <= index < len(self._queue):
            self._queue.pop(index)
            self.queue_changed.emit()
            return True
        return False

    def clear_queue(self) -> None:
        """Clear waiting items and stop all active downloads."""
        self._stop_all = True
        for worker in list(self._active.keys()):
            worker.stop()
        self._queue.clear()
        self._paused_items.clear()
        self._scheduled_retries.clear()
        self._completed.clear()
        self._failed.clear()
        self.queue_changed.emit()
        # Re-enable launching shortly after stopped workers settle, so a fresh
        # session (or post-clear adds) can start downloading again.
        QTimer.singleShot(500, self._reenable_after_clear)

    def _reenable_after_clear(self) -> None:
        self._stop_all = False
        self.queue_changed.emit()
        self._start_next()

    def queue_items(self) -> List[DownloadItem]:
        return list(self._queue)

    def active_items(self) -> List[DownloadItem]:
        """Active workers' items plus paused items kept in limbo."""
        result: List[DownloadItem] = []
        for it in list(self._active.values()) + self._paused_items:
            if all(it is not other for other in result):
                result.append(it)
        return result

    def completed_items(self) -> List[DownloadItem]:
        return list(self._completed)

    def failed_items(self) -> List[DownloadItem]:
        return list(self._failed)

    def closed_items(self) -> List[DownloadItem]:
        """Finished downloads (successful first, then failed)."""
        return list(self._completed) + list(self._failed)

    def is_retry_scheduled(self, item: DownloadItem) -> bool:
        return item in self._scheduled_retries

    def display_items(self) -> List[DownloadItem]:
        """Every item the Downloads list should show, in display order.

        Order: running, paused (limbo), queued,.awaiting-retry, completed,
        failed. Duplicates (a paused item briefly still in _active, etc.) are
        de-duplicated by identity.
        """
        result: List[DownloadItem] = []
        for it in (
            list(self._active.values())
            + list(self._paused_items)
            + list(self._queue)
            + list(self._scheduled_retries)
            + list(self._completed)
            + list(self._failed)
        ):
            if all(it is not other for other in result):
                result.append(it)
        return result

    def remove_active(self, item: DownloadItem) -> bool:
        """Stop and remove an active (or paused) download. True if removed."""
        for worker, it in list(self._active.items()):
            if it is item:
                self._paused_workers.discard(worker)
                self._cancelled_workers.add(worker)
                worker.stop()
                self._active.pop(worker, None)
                worker.deleteLater()
                self.item_status.emit(item, "Removed by user")
                self.queue_changed.emit()
                QTimer.singleShot(0, self._start_next)
                return True
        if item in self._paused_items:
            self._paused_items.remove(item)
            self.item_status.emit(item, "Removed by user")
            self.queue_changed.emit()
            return True
        if item in self._scheduled_retries:
            self._scheduled_retries.remove(item)
            self.item_status.emit(item, "Removed by user")
            self.queue_changed.emit()
            return True
        return False

    def remove_completed(self, item: DownloadItem) -> bool:
        """Remove a finished (completed or failed) download from the list."""
        if item in self._completed:
            self._completed.remove(item)
            self.queue_changed.emit()
            return True
        if item in self._failed:
            self._failed.remove(item)
            self.queue_changed.emit()
            return True
        return False

    def remove_item(self, item: DownloadItem) -> bool:
        """Remove *item* everywhere it lives: active/paused/retrying, queued,
        or finished. Used by the per-row ✕ (cancel/remove) button.

        Every list is checked (not just the first hit) so an item that is
        briefly present in two states can never leave a ghost row behind.
        """
        removed = False
        if self.remove_active(item):
            removed = True
        if self.remove_completed(item):
            removed = True
        if item in self._scheduled_retries:
            self._scheduled_retries.remove(item)
            removed = True
        for i, it in enumerate(self._queue):
            if it is item:
                self.remove_queued(i)
                removed = True
                break
        if removed:
            item.paused = False
            self.queue_changed.emit()
        return removed

    def clear_completed(self) -> None:
        """Drop every completed download from the completed list."""
        if self._completed:
            self._completed.clear()
            self.queue_changed.emit()

    def clear_downloaded(self) -> None:
        """Clear completed and failed items from the Downloads list.

        Active, paused, and queued items are untouched — use the per-row ✕
        to remove those individually.
        """
        if self._completed or self._failed:
            self._completed.clear()
            self._failed.clear()
            self.queue_changed.emit()

    # ---- pause / resume --------------------------------------------------
    def pause_item(self, item: DownloadItem) -> None:
        """Pause a download (running, retry-scheduled, or pending).

        An active worker is stopped; the item moves to the paused set so a
        later resume continues the partial file. Pending items are simply
        flagged ``paused`` (they stay queued and keep their order).
        """
        for worker, it in list(self._active.items()):
            if it is item:
                self._paused_workers.add(worker)
                item.paused = True
                item.resume = True   # remember so resume uses --continue
                if item not in self._paused_items:
                    self._paused_items.append(item)
                worker.stop()
                self.item_status.emit(item, "Paused")
                self.queue_changed.emit()
                return
        if item in self._scheduled_retries:
            item.paused = True
            self.queue_changed.emit()
            self.item_status.emit(item, "Paused")
            return
        if item in self._queue:
            item.paused = True
            self.queue_changed.emit()
            self.item_status.emit(item, "Paused")

    def resume_item(self, item: DownloadItem) -> None:
        """Resume a paused or stopped download by re-queueing it."""
        item.paused = False
        item.resume = True            # continue the partial file with -c
        item.resumed_after_pause = True   # next launch is not a new attempt
        if item in self._paused_items:
            self._paused_items.remove(item)
        # A per-row resume is explicit user intent: stop holding retries,
        # and take the item off the retry timer so _do_retry cannot queue
        # a duplicate once it fires.
        self._global_paused = False
        if item in self._scheduled_retries:
            self._scheduled_retries.remove(item)
        if item not in self._queue:
            self._queue.insert(0, item)
        self.queue_changed.emit()
        self._start_next()

    def pause_all(self) -> None:
        """Pause every active and pending download.

        Paused state persists: active items move to the paused set and every
        pending queue item is flagged ``paused``, which both state-saving and
        _start_next honour, so the pause survives a restart.
        """
        self._global_paused = True
        for item in list(self._active.values()):
            self.pause_item(item)
        for item in self._scheduled_retries:
            item.paused = True
        for item in self._queue:
            item.paused = True
        self.queue_changed.emit()

    def resume_all(self) -> None:
        """Resume every paused download (global pause off)."""
        self._global_paused = False
        resumed = list(self._paused_items)
        self._paused_items.clear()
        for item in resumed:
            item.paused = False
            item.resume = True
        for item in self._queue:
            item.paused = False
        for item in self._scheduled_retries:
            item.paused = False
        # Previously-downloading items go to the front in their old order.
        self._queue = resumed + self._queue
        self.queue_changed.emit()
        self._start_next()

    # ---- state restore ---------------------------------------------------
    def restore(
        self,
        settings: dict[str, Any],
        queue_items: list[DownloadItem],
        active_items: list[DownloadItem],
    ) -> None:
        """Restore settings, queue, and active items from persisted state.

        Previously-active non-paused downloads are placed at the front of the
        queue with ``resume=True`` so yt-dlp uses ``--continue`` to pick up
        partial files. Previously-active paused downloads reappear as paused
        rows without starting. Paused pending items keep their paused flag and
        are never auto-started. Attempt counts are preserved.
        """
        self.configure(
            max_parallel=settings.get("max_parallel", 1),
            max_attempts=settings.get("max_attempts", 10),
            initial_backoff=settings.get("initial_backoff", 5),
            max_backoff=settings.get("max_backoff", 300),
        )
        # Split saved active items into paused vs still-running.
        paused_active: List[DownloadItem] = []
        running_active: List[DownloadItem] = []
        for item in active_items:
            if item.paused:
                paused_active.append(item)
            else:
                item.resume = True
                running_active.append(item)
        # Limbo set for paused previously-active items; rows appear as paused.
        self._paused_items = paused_active
        # Running ones were in progress; queue them at the front to resume.
        for item in running_active:
            item.resume = True
        self._queue = running_active + list(queue_items)
        self.queue_changed.emit()
        # Show rows for paused previously-active downloads.
        for item in paused_active:
            self.item_started.emit(item)
            self.item_status.emit(item, "Paused")
        # Start non-paused items up to max_parallel without incrementing attempts.
        self._start_next(increment_attempts=False)
        # Announce still-paused pending items.
        for item in self._queue:
            if item.paused:
                self.item_status.emit(item, "Paused")
        # Fetch friendly titles for restored items that lack one (background).
        items_without_title = [
            it
            for it in list(self._queue) + list(self._paused_items)
            if not getattr(it, "title", "")
        ]
        if items_without_title:
            QTimer.singleShot(200, lambda: self.resolve_missing_titles(items_without_title))

    # ---- internal: starting / retrying -----------------------------------
    def _start_next(self, increment_attempts: bool = True) -> None:
        """Launch queued items while free slots exist.

        Paused queue items are skipped, not popped, so they keep their order
        and can be resumed later (pause-all persists through this path).
        """
        if self._stop_all:
            return
        i = 0
        while len(self._active) < self._max_parallel and i < len(self._queue):
            item = self._queue[i]
            if item.paused:
                i += 1
                continue
            self._queue.pop(i)
            self._launch(item, increment_attempts=increment_attempts)
            # Emit AFTER the launch so observers see the item as live
            # (emitting while it is neither queued nor active would make
            # UI enable-state checks briefly see an empty download list).
            self.queue_changed.emit()

    def _launch(self, item: DownloadItem, increment_attempts: bool = True) -> None:
        if increment_attempts and not getattr(item, "resumed_after_pause", False):
            item.attempts += 1
        item.resumed_after_pause = False
        worker = Worker()
        self._active[worker] = item
        worker.progress.connect(
            lambda pct, eta, w=worker, it=item: self.item_progress.emit(it, pct, eta)
        )
        worker.status.connect(
            lambda msg, w=worker, it=item: self.item_status.emit(it, msg)
        )
        worker.finished.connect(
            lambda ok, msg, dest, w=worker, it=item: self._on_attempt_finished(
                w, it, ok, msg, dest
            )
        )
        worker.resolved_title.connect(
            lambda title, w=worker, it=item: self._on_worker_title(it, title)
        )
        self.item_started.emit(item)
        worker.download(item, resume=item.resume)

    def _on_worker_title(self, item: DownloadItem, title: str) -> None:
        """Adopt a title yt-dlp derived while downloading; refresh rows once."""
        if not title:
            return
        if not getattr(item, "title", ""):
            item.title = title
            self.queue_changed.emit()

    # ---- background title resolution (state-restored items) ---------------
    def resolve_missing_titles(self, items: List[DownloadItem]) -> None:
        """Fetch titles for *items* one at a time in the background.

        Only used for items restored from state that have no title yet. Runs
        one lightweight yt-dlp print for each, spaced out; failed lookups are
        skipped silently (rows fall back to showing the URL).
        """
        if self._stop_all:
            return
        missing = [it for it in items if not getattr(it, "title", "")]
        self._fetch_title_chain(missing)

    def _fetch_title_chain(self, items: List[DownloadItem]) -> None:
        if self._stop_all or not items:
            return
        target, rest = items[0], items[1:]
        if getattr(target, "title", ""):
            self._fetch_title_chain(rest)
            return
        worker = Worker()
        self._title_workers.add(worker)

        def done(ok: bool, url: str, title_or_error: str, w=worker, it=target) -> None:
            self._title_workers.discard(w)
            try:
                w.deleteLater()
            except RuntimeError:
                pass
            if ok and title_or_error and not getattr(it, "title", ""):
                it.title = title_or_error
                self.queue_changed.emit()
            QTimer.singleShot(150, lambda: self._fetch_title_chain(rest))

        worker.validated.connect(done)
        worker.finished.connect(
            lambda *_, w=worker: self._title_workers.discard(w)
        )
        worker.validate(target.url)

    def _retry_later(self, item: DownloadItem) -> None:
        """Schedule a retry of *item* after exponential backoff."""
        if item not in self._scheduled_retries:
            self._scheduled_retries.append(item)
        delay_ms = item.backoff_for_next() * 1000
        # The item just left the active set for the retry pool: refresh rows
        # and global button states (an active->retrying transition must not
        # leave the UI showing a stale "running" row).
        self.queue_changed.emit()
        self.item_status.emit(
            item,
            f"Attempt {item.attempts}/{item.max_attempts} failed. "
            f"Retrying in {format_seconds(delay_ms // 1000)}...",
        )
        QTimer.singleShot(delay_ms, lambda: self._do_retry(item))

    def _do_retry(self, item: DownloadItem) -> None:
        if item in self._scheduled_retries:
            self._scheduled_retries.remove(item)
        if self._stop_all:
            return
        # Already running again (e.g. per-row Resume beat the retry timer):
        # never queue a duplicate launch.
        if any(it is item for it in self._active.values()):
            return
        if self._global_paused:
            item.paused = True   # hold the retry while pause-all is active
        # Re-queue at the front so it is picked up by a free slot.
        if item not in self._queue:
            self._queue.insert(0, item)
        self.queue_changed.emit()
        self._start_next()

    # ---- worker finished callback ----------------------------------------
    def _on_attempt_finished(
        self,
        worker: Worker,
        item: DownloadItem,
        success: bool,
        message: str,
        destination: Optional[str],
    ) -> None:
        self._active.pop(worker, None)
        worker.deleteLater()

        # Removals are intentional, not failures: never retry, never mark done.
        if worker in self._cancelled_workers:
            self._cancelled_workers.discard(worker)
            return

        # A pause is an intentional stop, not a failure.
        if worker in self._paused_workers:
            self._paused_workers.discard(worker)
            QTimer.singleShot(0, self._start_next)
            return

        # Terminal verdict comes from the worker (which verified completion in
        # its own thread); the UI thread never blocks here.
        if success:
            self.item_finished.emit(item, True, message, destination or "")
            # Track as completed so the Downloads list shows it (greyed out).
            self._completed.append(item)
            self.item_completed.emit(item, destination or "")
            self.queue_changed.emit()
            # Slot freed; start more queued items.
            QTimer.singleShot(0, self._start_next)
            self._scrub_terminal(item)
        elif item.attempts < item.max_attempts:
            # Schedule a retry (item stays visible via _scheduled_retries).
            self._retry_later(item)
        else:
            self.item_finished.emit(item, False, message, destination or "")
            # Terminal failure: keep it visible in the Downloads list, greyed out.
            self._failed.append(item)
            self.queue_changed.emit()
            QTimer.singleShot(0, self._start_next)
            self._scrub_terminal(item)

    def _scrub_terminal(self, item: DownloadItem) -> None:
        """Terminal bookkeeping: scrub limbo/retry ghosts so a finished item
        can never linger in a paused/retry pool, and reset its flags."""
        if item in self._paused_items:
            self._paused_items.remove(item)
        if item in self._scheduled_retries:
            self._scheduled_retries.remove(item)
        item.resume = False
        item.resumed_after_pause = False
