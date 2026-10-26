"""Main application window."""

from __future__ import annotations

import os
from typing import Dict, Optional

from PyQt6.QtCore import QSize, QTimer
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import __version__ as APP_VERSION
from .manager import DownloadManager
from .settings_dialog import SettingsDialog
from .state import _item_from_dict, load_state, save_state
from .worker import DownloadItem, format_seconds

APP_TITLE = "YouTube Downloader"
DEFAULT_OUTPUT_DIR = os.path.expanduser("~/Downloads")

_ROW_MIN_HEIGHT = 38


class _XButton(QPushButton):
    """Small ✕ remove button."""

    def __init__(self, on_click, parent=None) -> None:
        super().__init__("✕", parent)
        self.setFixedWidth(28)
        self.setToolTip("Remove this item from the list")
        self.clicked.connect(on_click)


def _row_state(item: DownloadItem, manager: DownloadManager) -> str:
    """Which visual state a row for *item* should show.

    Use ordering: terminal states first, then paused (limbo/queue flag),
    running, scheduled-retry, pending.
    """
    if item in manager.completed_items():
        return "completed"
    if item in manager.failed_items():
        return "failed"
    if item.paused:
        return "paused"
    if item in manager.active_items():
        return "running"
    if manager.is_retry_scheduled(item):
        return "retrying"
    return "pending"


class _DownloadRow(QWidget):
    """One row in the Downloads list — used for every state of an item.

    Running items show a live progress bar, ETA, pause/resume and ✕.
    Pending items show 0% and (pause + ✕) so they can be paused/removed
    before they start. Paused and awaiting-retry items show their last
    progress with Resume available. Completed/failed items are greyed out:
    progress bar disabled (100% green ✓), pause/resume disabled, ✕ removes.
    """

    def __init__(self, item: DownloadItem, manager: DownloadManager, parent=None) -> None:
        super().__init__(parent)
        self.item = item
        self.manager = manager
        self.setMinimumHeight(_ROW_MIN_HEIGHT)

        self._label = QLabel()
        self._label.setWordWrap(False)
        # 'Ignored' keeps the row's width hint independent of URL length, so a
        # long URL can never widen the item past the viewport and clip the
        # progress bar / buttons out of view.
        self._label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setFixedWidth(130)

        self._eta = QLabel("")
        self._eta.setFixedWidth(88)

        self._pause_btn = QPushButton()
        self._pause_btn.setFixedWidth(76)
        self._pause_btn.clicked.connect(self._on_toggle_pause)

        self._remove_btn = _XButton(self._on_remove)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.addWidget(self._label, stretch=1)
        layout.addWidget(self._progress)
        layout.addWidget(self._eta)
        layout.addWidget(self._pause_btn)
        layout.addWidget(self._remove_btn)
        self.setSizeHint()
        self.apply_state()

    # ---- helpers ---------------------------------------------------------
    def _terminal_state(self) -> Optional[str]:
        if self.item in self.manager.completed_items():
            return "completed"
        if self.item in self.manager.failed_items():
            return "failed"
        return None

    def _state(self) -> str:
        return _row_state(self.item, self.manager)

    def _caption(self) -> str:
        item = self.item
        state = self._state()
        # Human-friendly title when we know it; URL only as fallback.
        display = getattr(item, "title", "") or item.url
        broken = "; ".join(
            segment for segment in (
                " [Paused]" if state in ("paused",) else "",
                f" — retrying (attempt {item.attempts}/{item.max_attempts})"
                if state == "retrying" else "",
            ) if segment
        )
        if state == "completed":
            dest = getattr(item, "destination", "")
            tail = f" → {os.path.basename(dest)}" if dest else ""
            return f"✓ Completed: [{item.kind}] {display}{tail}"
        if state == "failed":
            msg = getattr(item, "fail_message", "") or "download failed"
            return f"✗ FAILED: [{item.kind}] {display} — {msg}"
        return f"[{item.kind}] {display}{broken}"

    def setSizeHint(self, *_a) -> None:  # noqa: N802 - mirrors Qt naming
        self._item_height = max(_ROW_MIN_HEIGHT, self.sizeHint().height())

    def apply_state(self) -> None:
        item = self.item
        state = self._state()
        self._label.setText(self._caption())
        self._label.setToolTip(f"{item.url}\n{self._caption()}")

        last_pct = getattr(item, "last_pct", None)
        last_eta = getattr(item, "last_eta", None)

        if state == "completed":
            self._progress.setValue(100)
            self._progress.setEnabled(False)
            self._eta.setText("✓")
            self._pause_btn.setText("Done")
            self._pause_btn.setEnabled(False)
            self._label.setStyleSheet("color: #0a0;")
        elif state == "failed":
            self._progress.setValue(last_pct if last_pct is not None else 0)
            self._progress.setEnabled(False)
            self._eta.setText("✗")
            self._pause_btn.setText("Stopped")
            self._pause_btn.setEnabled(False)
            self._label.setStyleSheet("color: #a00;")
        elif state == "paused":
            self._progress.setEnabled(True)
            self._pause_btn.setText("Resume")
            self._pause_btn.setEnabled(True)
            if last_pct is not None:
                self._progress.setValue(last_pct)
            self._eta.setText("Paused")
            self._label.setStyleSheet("color: #b60; font-weight: normal;")
        elif state == "retrying":
            self._progress.setEnabled(True)
            self._pause_btn.setText("Pause")
            self._pause_btn.setEnabled(True)
            if last_pct is not None:
                self._progress.setValue(last_pct)
            self._eta.setText("Retry…")
            self._label.setStyleSheet("color: #b60;")
        elif state == "running":
            self._progress.setEnabled(True)
            self._pause_btn.setText("Pause")
            self._pause_btn.setEnabled(True)
            self._progress.setValue(last_pct if last_pct is not None else 0)
            self._eta.setText(
                f"ETA: {format_seconds(last_eta)}" if last_eta is not None else "ETA: --:--"
            )
            self._label.setStyleSheet("")
        else:  # pending
            self._progress.setEnabled(True)
            self._pause_btn.setText("Pause")
            self._pause_btn.setEnabled(True)
            self._progress.setValue(0)
            self._eta.setText("Queued")
            self._label.setStyleSheet("color: #333;")
        self.setSizeHint()
        # Force a synchronous paint: under Wayland, state changes that only
        # rely on scheduled updates may leave the compositor showing a stale
        # row (title visible, controls missing) until a mouse-over.
        self.repaint()

    def update_progress(self, percent: int, eta_seconds: int) -> None:
        setattr(self.item, "last_pct", percent)
        setattr(self.item, "last_eta", eta_seconds)
        if self._pause_btn.isEnabled():
            self._eta.setText(f"ETA: {format_seconds(eta_seconds)}")
        self._progress.setValue(percent)
        # Deterministic repaint of the touched controls (Wayland-safe).
        self._progress.repaint()
        self._eta.repaint()

    # ---- actions ---------------------------------------------------------
    def _on_toggle_pause(self) -> None:
        """Defer manager mutation: emitting queue_changed rebuilds the list,
        which would destroy *this* widget while its own slot is running."""
        item = self.item
        if item.paused:
            QTimer.singleShot(0, lambda: self.manager.resume_item(item))
        else:
            QTimer.singleShot(0, lambda: self.manager.pause_item(item))
        # Do NOT touch self from here on — the timer may rebuild the list.

    def _on_remove(self) -> None:
        item = self.item
        window = self.window()   # resolved before deferral
        QTimer.singleShot(0, lambda: window._remove_item(item))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_TITLE} v{APP_VERSION}")
        self.resize(900, 640)

        self._settings = {
            "max_parallel": 1,
            "max_attempts": 10,
            "initial_backoff": 5,
            "max_backoff": 300,
            "default_output_dir": DEFAULT_OUTPUT_DIR,
        }

        self._validated_url: Optional[str] = None
        self._validated_title: Optional[str] = None
        self._closing = False
        self._force_quit = False

        self._manager = DownloadManager()
        self._manager.configure(**{
            k: self._settings[k]
            for k in ("max_parallel", "max_attempts", "initial_backoff", "max_backoff")
        })

        # item -> row (list item) registry is rebuilt on queue changes.
        self._row_registry: Dict[DownloadItem, QListWidgetItem] = {}
        self._last_signature = None

        self._build_ui()
        self._connect_signals()

        # State save timer (persists queue/settings periodically).
        self._state_save_timer = QTimer(self)
        self._state_save_timer.setInterval(60_000)
        self._state_save_timer.timeout.connect(self._save_state)

        # Debounced validation timer.
        self._validation_timer = QTimer(self)
        self._validation_timer.setSingleShot(True)
        self._validation_timer.setInterval(400)
        self._validation_timer.timeout.connect(self._run_validation)

        # Load persisted state (settings, queued + paused/active downloads).
        self._load_state()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # ---- menus --------------------------------------------------------
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("&File")
        quit_action = file_menu.addAction("&Quit")
        quit_action.setShortcut("Ctrl+Q")
        quit_action.setStatusTip("Quit, saving downloads to resume next start")
        quit_action.triggered.connect(self.close)
        exit_action = file_menu.addAction("E&xit without prompting")
        exit_action.setShortcut("Shift+Ctrl+Q")
        exit_action.setToolTip("Quit immediately without asking about active downloads")
        exit_action.triggered.connect(self._exit_without_prompting)
        settings_menu = menu_bar.addMenu("&Settings")
        settings_action = settings_menu.addAction("&Preferences…")
        settings_action.triggered.connect(self._open_settings)
        help_menu = menu_bar.addMenu("&Help")
        version_action = help_menu.addAction("&Version")
        version_action.setStatusTip("Show the current version of this tool")
        version_action.triggered.connect(self._show_version)

        # ---- Source (label inline with the input) --------------------------
        src_row = QHBoxLayout()
        src_row.addWidget(QLabel("Source"))
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText(
            "YouTube URL or video ID (e.g. dQw4w9WgXcQ); invidious URLs accepted"
        )
        src_row.addWidget(self.url_input, stretch=1)
        layout.addLayout(src_row)

        self.validation_label = QLabel("Enter a URL or ID to validate")
        self.validation_label.setStyleSheet("color: #666; font-style: italic;")
        self.validation_label.setWordWrap(True)
        layout.addWidget(self.validation_label)

        # ---- Options (no heading) -----------------------------------------
        opt_layout = QHBoxLayout()

        self.format_combo = QComboBox()
        self.format_combo.addItem("Audio only (best quality)", "audio")
        self.format_combo.addItem("Video + audio (highest resolution)", "video")
        self.format_combo.setCurrentIndex(0)

        self.output_input = QLineEdit(self._settings["default_output_dir"])
        self.browse_btn = QPushButton("Browse…")
        self.browse_btn.setFixedWidth(80)

        opt_layout.addWidget(QLabel("Format:"))
        opt_layout.addWidget(self.format_combo, stretch=1)
        opt_layout.addWidget(QLabel("Output:"))
        opt_layout.addWidget(self.output_input, stretch=1)
        opt_layout.addWidget(self.browse_btn)
        layout.addLayout(opt_layout)

        # ---- Actions ------------------------------------------------------
        actions = QHBoxLayout()
        self.download_btn = QPushButton("Download")
        self.download_btn.setFixedWidth(110)
        self.download_btn.setEnabled(False)
        actions.addWidget(self.download_btn)
        actions.addStretch(1)
        layout.addLayout(actions)
        layout.addSpacing(14)   # breathing room above the Downloads heading

        # ---- Downloads (single merged section) -----------------------------
        downloads = QGroupBox("Downloads")
        downloads_layout = QVBoxLayout(downloads)
        self.downloads_list = QListWidget()
        self.downloads_list.setMinimumHeight(300)
        downloads_layout.addWidget(self.downloads_list)
        layout.addWidget(downloads)

        # ---- Downloads controls (right-aligned, under the section) ---------
        dl_controls = QHBoxLayout()
        dl_controls.addStretch(1)
        self.pause_all_btn = QPushButton("Pause downloads")
        self.pause_all_btn.setFixedWidth(150)
        self.pause_all_btn.setToolTip(
            "Pause every active and pending download (survives restart)"
        )
        dl_controls.addWidget(self.pause_all_btn)
        self.clear_btn = QPushButton("Clear downloaded")
        self.clear_btn.setFixedWidth(150)
        self.clear_btn.setToolTip("Remove completed and failed items from the list")
        dl_controls.addWidget(self.clear_btn)
        layout.addLayout(dl_controls)
        layout.addSpacing(6)

        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("color: #333;")
        layout.addWidget(self.status_label)

    # ------------------------------------------------------------------ signals
    def _connect_signals(self) -> None:
        self.url_input.textChanged.connect(self._on_url_changed)
        self.browse_btn.clicked.connect(self._on_browse)
        self.download_btn.clicked.connect(self._on_download_now)
        self.pause_all_btn.clicked.connect(self._on_pause_all)
        self.clear_btn.clicked.connect(self._on_clear_clicked)

        self._manager.item_validated.connect(self._on_validated)
        self._manager.item_started.connect(self._on_item_started)
        self._manager.item_progress.connect(self._on_item_progress)
        self._manager.item_status.connect(self._on_item_status)
        self._manager.item_finished.connect(self._on_item_finished)
        self._manager.item_completed.connect(self._on_item_completed)
        # Rebuild the merged Downloads list whenever the item set changes —
        # including on completion / terminal failure (queue_changed is emitted
        # by the manager there).
        self._manager.queue_changed.connect(self._refresh_downloads)
        # State persistence: save whenever the download set changes.
        self._manager.queue_changed.connect(self._save_state)
        self._manager.item_started.connect(self._save_state)
        self._manager.item_finished.connect(self._save_state)

    # ------------------------------------------------------------------ settings
    def _open_settings(self) -> None:
        dlg = SettingsDialog(self._settings, self)
        if dlg.exec():
            self._settings.update(dlg.settings())
            self.output_input.setText(
                self._settings["default_output_dir"] or DEFAULT_OUTPUT_DIR
            )
            self._manager.configure(
                max_parallel=self._settings["max_parallel"],
                max_attempts=self._settings["max_attempts"],
                initial_backoff=self._settings["initial_backoff"],
                max_backoff=self._settings["max_backoff"],
            )
            self._save_state()

    # ------------------------------------------------------------------ validation
    def _on_url_changed(self, text: str) -> None:
        self._validated_url = None
        self._validated_title = None
        self.validation_label.setText("Enter a URL or ID to validate")
        self.validation_label.setStyleSheet("color: #666; font-style: italic;")
        self.download_btn.setEnabled(False)
        if text.strip():
            self._validation_timer.start()

    def _run_validation(self) -> None:
        text = self.url_input.text().strip()
        if not text:
            return
        self.validation_label.setText("Validating…")
        self.validation_label.setStyleSheet("color: #666; font-style: italic;")
        self._manager.validate(text)

    def _on_validated(self, ok: bool, resolved_url: str, title_or_error: str) -> None:
        if ok:
            self._validated_url = resolved_url
            self._validated_title = title_or_error
            self.validation_label.setText(f"✓  {title_or_error}")
            self.validation_label.setStyleSheet("color: #0a0; font-size: 13px; font-weight: bold;")
        else:
            self._validated_url = None
            self._validated_title = None
            self.validation_label.setText(f"✗  Invalid: {title_or_error}")
            self.validation_label.setStyleSheet("color: #a00; font-weight: bold;")
        enabled = bool(self.url_input.text().strip()) and ok
        self.download_btn.setEnabled(enabled)

    # ------------------------------------------------------------------ queue / download
    def _on_browse(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Select output directory", self.output_input.text()
        )
        if directory:
            self.output_input.setText(directory)

    def _validated_target(self) -> Optional[tuple]:
        if not self._validated_url:
            QMessageBox.warning(
                self, "Nothing validated",
                "Enter a URL or ID and wait for validation before downloading.",
            )
            return None
        kind = self.format_combo.currentData()
        output_dir = self.output_input.text().strip() or DEFAULT_OUTPUT_DIR
        return (self._validated_url, kind, output_dir)

    def _on_download_now(self) -> None:
        target = self._validated_target()
        if target is None:
            return
        url, kind, output_dir = target
        if not self._confirm_download(url, kind):
            return
        self._manager.add_now(url, kind, output_dir,
                              title=self._validated_title or "")
        self._reset_validation_input()

    def _confirm_download(self, url: str, kind: str) -> bool:
        """Guard: reject live duplicates, confirm re-downloads.

        * Same source already queued/active/paused in the SAME format →
          message box, download refused (no duplicates can clutter the list).
        * Same source already downloaded earlier in this session → ask the
          user whether to download it again (the file may be wanted in a
          different format — that is a different item and needs no prompt).
        """
        existing = self._manager.matching_item(url, kind)
        if existing is None:
            return True
        if existing in self._manager.completed_items():
            reply = QMessageBox.question(
                self, "Already downloaded",
                f"This source has already been downloaded\n"
                f"({existing.title or existing.url}).\n\n"
                f"Download it again?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            return reply == QMessageBox.StandardButton.Yes
        if existing in self._manager.failed_items():
            reply = QMessageBox.question(
                self, "Previously failed",
                "This source failed to download earlier.\n\nTry again now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            return reply == QMessageBox.StandardButton.Yes
        QMessageBox.warning(
            self, "Already in Downloads",
            "That source (in this format) is already in the Downloads list:\n"
            f"{existing.title or existing.url}\n\n"
            "Same sources may be downloaded in different formats — switch the "
            "Format option if that is what you intend.",
        )
        return False

    def _reset_validation_input(self) -> None:
        self.url_input.clear()
        self._validated_url = None
        self._validated_title = None
        self.download_btn.setEnabled(False)
        self.validation_label.setText("Enter a URL or ID to validate")
        self.validation_label.setStyleSheet("color: #666; font-style: italic;")

    def _on_pause_all(self) -> None:
        if self._has_anything_paused():
            self._manager.resume_all()
        else:
            self._manager.pause_all()
        self._save_state()

    def _has_anything_paused(self) -> bool:
        return bool(
            self._manager._paused_items
            or any(it.paused for it in self._manager.queue_items())
        )

    def _on_clear_clicked(self) -> None:
        self._manager.clear_downloaded()
        self.status_label.setText("Completed and failed items cleared")

    # ------------------------------------------------------------------ merged Downloads list
    def _refresh_downloads(self) -> None:
        """Sync the single Downloads list with the manager's items.

        To keep live rows rock-solid, the list is rebuilt only when the
        item set or any item's state actually changed (signature comparison).
        Rebuilds fired from unrelated signals during a long download are
        ignored, so progress bars and controls are never torn down mid-run.
        """
        items = self._manager.display_items()
        signature = [
            (id(it), _row_state(it, self._manager), getattr(it, "title", ""))
            for it in items
        ]
        rows_intact = all(
            it in self._row_registry
            and self.downloads_list.itemWidget(self._row_registry[it]) is not None
            for it in items
        )
        if signature == self._last_signature and rows_intact:
            self._sync_pause_button()
            return
        self._last_signature = signature
        self.downloads_list.clear()
        self._row_registry.clear()
        for item in items:
            row = _DownloadRow(item, self._manager)
            li = QListWidgetItem()
            li.setSizeHint(QSize(row.sizeHint().width(), row._item_height))
            self.downloads_list.addItem(li)
            self.downloads_list.setItemWidget(li, row)
            self._row_registry[item] = li
        # Force the fresh rows onto the screen NOW: the rebuild's scheduled
        # updates are not reliably turned into a compositor frame under
        # Wayland (rows could show only their label until a mouse-over).
        self.downloads_list.viewport().repaint()
        self._sync_pause_button()

    def _sync_pause_button(self) -> None:
        if self._has_anything_paused():
            self.pause_all_btn.setText("Resume downloads")
        elif not self._manager._paused_items:
            self.pause_all_btn.setText("Pause downloads")

    def _row_widget(self, item: DownloadItem):
        li = self._row_registry.get(item)
        if li is None:
            return None
        return self.downloads_list.itemWidget(li)

    def _on_item_started(self, item: DownloadItem) -> None:
        # Prefer touching just this row; rebuild only if it isn't on screen.
        widget = self._row_widget(item)
        if isinstance(widget, _DownloadRow):
            widget.apply_state()
        else:
            self._refresh_downloads()
        self.status_label.setText(f"Downloading: {item.url}")

    def _on_item_progress(self, item: DownloadItem, percent: int, eta_seconds: int) -> None:
        setattr(item, "last_pct", percent)
        setattr(item, "last_eta", eta_seconds)
        widget = self._row_widget(item)
        if isinstance(widget, _DownloadRow):
            widget.update_progress(percent, eta_seconds)

    def _on_item_status(self, item: DownloadItem, message: str) -> None:
        self.status_label.setText(message)

    def _on_item_finished(
        self, item: DownloadItem, success: bool, message: str, destination: str
    ) -> None:
        if success:
            setattr(item, "destination", destination or getattr(item, "destination", ""))
            setattr(item, "last_pct", 100)
            self.status_label.setText(
                f"Finished: {item.url}" + (f" [saved to {destination}]" if destination else "")
            )
        else:
            setattr(item, "fail_message", message)
            self.status_label.setText(f"FAILED: {item.url} — {message}")
        self._remove_row_entry(item)

    def _on_item_completed(self, item: DownloadItem, destination: str) -> None:
        # Rebuild so the item reappears greyed-out inside the merged list.
        self._refresh_downloads()

    def _remove_row_entry(self, item: DownloadItem) -> None:
        self._row_registry.pop(item, None)

    def _remove_item(self, item: DownloadItem) -> None:
        """Route the ✕ press for whichever state the item is in."""
        if item in self._manager.completed_items() or item in self._manager.failed_items():
            self._manager.remove_completed(item)
            return
        items = self._manager.queue_items()
        for i, it in enumerate(items):
            if it is item:
                self._manager.remove_queued(i)
                return
        self._manager.remove_active(item)

    # ------------------------------------------------------------------ state persistence
    def _save_state(self) -> None:
        """Persist current settings, queue, active downloads, and URL input."""
        if self._closing:
            return
        save_state(
            settings=self._settings,
            queue_items=self._manager.queue_items(),
            active_items=self._manager.active_items(),
        )

    def _load_state(self) -> None:
        """Restore settings, queue, and active downloads from the state file."""
        payload = load_state()
        if payload is None:
            self._state_save_timer.start()
            self._refresh_downloads()
            return
        saved = payload.get("settings", {})
        for key in ("max_parallel", "max_attempts", "initial_backoff",
                    "max_backoff", "default_output_dir"):
            if key in saved:
                self._settings[key] = saved[key]
        self.output_input.setText(self._settings["default_output_dir"])

        queue_items = [_item_from_dict(d) for d in payload.get("queue", [])]
        active_items = [_item_from_dict(d) for d in payload.get("active", [])]
        # Completed downloads are intentionally NOT restored.
        self._manager.restore(self._settings, queue_items, active_items)

        # Note: the Source field is intentionally left empty on startup.

        self._refresh_downloads()
        if self._has_anything_paused():
            self.pause_all_btn.setText("Resume downloads")
        self._state_save_timer.start()

    # ------------------------------------------------------------------ exit
    def _exit_without_prompting(self) -> None:
        """Quit immediately (no prompt) — state is saved for auto-resume."""
        self._force_quit = True
        self.close()

    def _show_version(self) -> None:
        QMessageBox.information(
            self,
            f"{APP_TITLE} — Version",
            f"{APP_TITLE}\n\nVersion {APP_VERSION}\n\n"
            f"A Qt graphical YouTube downloader powered by yt-dlp.",
        )

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override name
        self._save_state()
        if self._force_quit or not self._manager.active_items():
            self._closing = True
            self._manager.clear_queue()
            super().closeEvent(event)
            return
        reply = QMessageBox.question(
            self,
            "Downloads in progress",
            "There are active downloads running.\n\n"
            "Quit anyway?\n\n"
            "Note: your pending and in-progress downloads have been saved and "
            "will be automatically resumed the next time you start "
            f"{APP_TITLE}.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.No:
            event.ignore()
            return
        self._closing = True
        self._manager.clear_queue()
        super().closeEvent(event)


def main() -> None:
    from PyQt6.QtWidgets import QApplication

    app = QApplication([])
    app.setApplicationName(APP_TITLE)
    window = MainWindow()
    window.show()
    app.exec()
