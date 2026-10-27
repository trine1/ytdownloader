"""Main application window."""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
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


def _row_state(item: DownloadItem, manager: DownloadManager) -> str:
    """Which visual state a row for *item* should show."""
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


def _caption(item: DownloadItem, manager: DownloadManager) -> str:
    state = _row_state(item, manager)
    display = getattr(item, "title", "") or item.url
    broken = "; ".join(
        s for s in (
            " [Paused]" if state == "paused" else "",
            f" — retrying (attempt {item.attempts}/{item.max_attempts})"
            if state == "retrying" else "",
        ) if s
    )
    if state == "completed":
        dest = getattr(item, "destination", "")
        tail = f" → {os.path.basename(dest)}" if dest else ""
        return f"✓ Completed: [{item.kind}] {display}{tail}"
    if state == "failed":
        msg = getattr(item, "fail_message", "") or "download failed"
        return f"✗ FAILED: [{item.kind}] {display} — {msg}"
    return f"[{item.kind}] {display}{broken}"


def _row_pct(item: DownloadItem, manager: DownloadManager) -> int:
    if item in manager.completed_items():
        return 100
    last = getattr(item, "last_pct", None)
    if state := _row_state(item, manager) == "failed" and last is not None:
        return last
    if state == "pending":
        return 0
    return last if last is not None else 0


def _row_eta(item: DownloadItem, manager: DownloadManager) -> str:
    state = _row_state(item, manager)
    if state == "completed":
        return "✓"
    if state == "failed":
        return "✗"
    if state == "paused":
        return "Paused"
    if state == "retrying":
        return "Retry…"
    if state == "pending":
        return "Queued"
    last_eta = getattr(item, "last_eta", None)
    return f"ETA: {format_seconds(last_eta)}" if last_eta is not None else "ETA: --:--"


class DownloadsCanvas(QWidget):
    """Scrollable container for download rows using standard Qt widgets.

    Each row is a QWidget with QHBoxLayout containing:
    - QLabel (title, stretched)
    - QProgressBar (fixed width, shows %)
    - QLabel (ETA/status, fixed width)

    No custom painting — all rendering is handled by Qt's built-in widget
    system, which works correctly under Wayland compositors.
    """

    def __init__(self, manager: DownloadManager, parent=None) -> None:
        super().__init__(parent)
        self._manager = manager
        self._items: List[DownloadItem] = []
        self._rows: Dict[int, QWidget] = {}  # keyed by id(item)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(8, 8, 8, 8)
        self._layout.setSpacing(4)
        self._layout.addStretch()  # push rows to top

        self.setMinimumHeight(60)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    # ---- public API -----------------------------------------------------
    def refresh(self) -> None:
        """Sync with manager and update row widgets."""
        self._items = self._manager.display_items()
        self._sync_rows()
        self.repaint()
        if self.parentWidget():
            self.parentWidget().repaint()

    def item_count(self) -> int:
        return len(self._items)

    # ---- row management --------------------------------------------------
    def _sync_rows(self) -> None:
        """Create new rows, update existing rows, remove stale rows."""
        current_ids = set()

        for item in self._items:
            key = id(item)
            current_ids.add(key)

            if key not in self._rows:
                # Insert before the trailing stretch
                row = self._create_row(item)
                self._rows[key] = row
                self._layout.insertWidget(self._layout.count() - 1, row)

            self._update_row(item, self._rows[key])

        # Remove rows for items no longer displayed
        for key in list(self._rows.keys()):
            if key not in current_ids:
                row = self._rows.pop(key)
                self._layout.removeWidget(row)
                row.deleteLater()

    def _create_row(self, item: DownloadItem) -> QWidget:
        """Create a single row widget with title, progress bar, and ETA."""
        row = QWidget()
        row.setFixedHeight(36)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(8)

        # Title label (stretched to fill remaining space)
        title = QLabel()
        title.setMinimumWidth(100)
        layout.addWidget(title, stretch=1)

        # Progress bar (fixed width)
        bar = QProgressBar()
        bar.setFixedWidth(170)
        bar.setFixedHeight(24)
        bar.setRange(0, 100)
        bar.setTextVisible(True)
        layout.addWidget(bar)

        # ETA / status label (fixed width)
        eta = QLabel()
        eta.setFixedWidth(90)
        eta.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        layout.addWidget(eta)

        # Store references on the widget for later updates
        row._title = title   # type: ignore[attr-defined]
        row._bar = bar       # type: ignore[attr-defined]
        row._eta = eta       # type: ignore[attr-defined]
        return row

    def _update_row(self, item: DownloadItem, row: QWidget) -> None:
        """Refresh a row's widgets with the item's current state."""
        state = _row_state(item, self._manager)
        pct = _row_pct(item, self._manager)
        eta_text = _row_eta(item, self._manager)
        caption = _caption(item, self._manager)

        # Title
        row._title.setText(caption)  # type: ignore[attr-defined]
        row._title.setToolTip(item.url)  # type: ignore[attr-defined]

        # Title colour by state
        if state == "completed":
            row._title.setStyleSheet("color: #0a0;")  # type: ignore[attr-defined]
        elif state == "failed":
            row._title.setStyleSheet("color: #a00;")  # type: ignore[attr-defined]
        elif state in ("paused", "retrying"):
            row._title.setStyleSheet("color: #b60;")  # type: ignore[attr-defined]
        else:
            row._title.setStyleSheet("")  # type: ignore[attr-defined]

        # Progress bar
        row._bar.setValue(pct)  # type: ignore[attr-defined]

        # ETA
        row._eta.setText(eta_text)  # type: ignore[attr-defined]


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

        # Watchdog repaint timer — guarantees the Downloads canvas always
        # shows fresh content even under Wayland compositor quirks.
        self._repaint_watchdog = QTimer(self)
        self._repaint_watchdog.setInterval(1_000)
        self._repaint_watchdog.timeout.connect(self._ensure_fresh_paint)

        # Load persisted state.
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

        # ---- Source -------------------------------------------------------
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

        # ---- Options ------------------------------------------------------
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
        layout.addSpacing(14)

        # ---- Downloads (single merged canvas) -----------------------------
        downloads = QGroupBox("Downloads")
        downloads_layout = QVBoxLayout(downloads)
        self._canvas = DownloadsCanvas(self._manager)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._canvas)
        self._scroll.setMinimumHeight(300)
        # Keep canvas width synced with viewport on resize
        pass
        downloads_layout.addWidget(self._scroll)
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
        # Every manager change refreshes the canvas (one atomic repaint).
        self._manager.queue_changed.connect(self._refresh_downloads)
        # State persistence.
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

    # ------------------------------------------------------------------ canvas refresh
    def _refresh_downloads(self) -> None:
        """One atomic canvas repaint — every row, every control, every time."""
        from PyQt6.QtWidgets import QApplication
        self._canvas.refresh()
        QApplication.processEvents()
        self._scroll.viewport().repaint()
        self._scroll.repaint()
        self.repaint()
        QApplication.processEvents()
        self._sync_pause_button()

    def _sync_pause_button(self) -> None:
        if self._has_anything_paused():
            self.pause_all_btn.setText("Resume downloads")
        elif not self._manager._paused_items:
            self.pause_all_btn.setText("Pause downloads")

    def _ensure_fresh_paint(self) -> None:
        """Watchdog: repaint the canvas every second to guarantee controls
        are always visible, even under Wayland compositor quirks."""
        from PyQt6.QtWidgets import QApplication
        if self.isVisible():
            self._canvas.repaint()
            QApplication.processEvents()
            self._scroll.viewport().repaint()
            self._scroll.repaint()
            self.repaint()
            QApplication.processEvents()

    # ------------------------------------------------------------------ worker events
    def _on_item_started(self, item: DownloadItem) -> None:
        from PyQt6.QtWidgets import QApplication
        # Don't set fixed width — let scroll area manage it
        self._canvas.refresh()
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()
        self._scroll.viewport().repaint()
        self._scroll.repaint()
        self.repaint()
        QApplication.processEvents()
        # Aggressive Wayland compositor frame submission
        wh = self.windowHandle()
        # Hide and show canvas to force full repaint
        self._canvas.setVisible(False)
        QApplication.processEvents()
        self._canvas.setVisible(True)
        QApplication.processEvents()
        if wh is not None:
            wh.requestUpdate()
        QApplication.processEvents()
        sz = self.size()
        self.resize(sz.width() + 1, sz.height())
        QApplication.processEvents()
        self.resize(sz.width(), sz.height())
        QApplication.processEvents()
        if wh is not None:
            wh.requestUpdate()
        QApplication.processEvents()
        self.status_label.setText(f"Downloading: {item.title or item.url}")

    def _on_item_progress(self, item: DownloadItem, percent: int, eta_seconds: int) -> None:
        setattr(item, "last_pct", percent)
        setattr(item, "last_eta", eta_seconds)
        from PyQt6.QtWidgets import QApplication
        # Atomic canvas repaint — every row always renders complete.
        self._canvas.repaint()
        QApplication.processEvents()
        self._scroll.viewport().repaint()
        self._scroll.repaint()
        self.repaint()
        QApplication.processEvents()

    def _on_item_status(self, item: DownloadItem, message: str) -> None:
        self.status_label.setText(message)

    def _on_item_finished(
        self, item: DownloadItem, success: bool, message: str, destination: str
    ) -> None:
        if success:
            setattr(item, "destination", destination or getattr(item, "destination", ""))
            setattr(item, "last_pct", 100)
            self.status_label.setText(
                f"Finished: {item.title or item.url}"
                + (f" [saved to {destination}]" if destination else "")
            )
        else:
            setattr(item, "fail_message", message)
            self.status_label.setText(f"FAILED: {item.title or item.url} — {message}")

    def _on_item_completed(self, item: DownloadItem, destination: str) -> None:
        self._refresh_downloads()

    # ------------------------------------------------------------------ state persistence
    def _save_state(self) -> None:
        """Persist current settings, queue, active downloads."""
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

        self._refresh_downloads()
        if self._has_anything_paused():
            self.pause_all_btn.setText("Resume downloads")
        self._state_save_timer.start()
        self._repaint_watchdog.start()

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