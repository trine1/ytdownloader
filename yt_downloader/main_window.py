"""Main application window."""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from PyQt6.QtCore import QTimer
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


class _XButton(QPushButton):
    """Small ✕ remove button."""

    def __init__(self, on_click, parent=None) -> None:
        super().__init__("✕", parent)
        self.setFixedWidth(28)
        self.setToolTip("Remove this item from the list")
        self.clicked.connect(on_click)


class _ActiveRow(QWidget):
    """A single row shown in the active-downloads list."""

    def __init__(self, item: DownloadItem, manager: DownloadManager, parent=None) -> None:
        super().__init__(parent)
        self.item = item
        self.manager = manager
        self._label = QLabel()
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setFixedWidth(110)
        self._eta = QLabel("ETA: --:--")
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setFixedWidth(70)
        self.pause_btn.clicked.connect(self._on_toggle_pause)
        self.remove_btn = _XButton(self._on_remove)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.addWidget(self._label, stretch=1)
        layout.addWidget(self._progress)
        layout.addWidget(self._eta)
        layout.addWidget(self.pause_btn)
        layout.addWidget(self.remove_btn)
        self._refresh_label()

    def _refresh_label(self) -> None:
        state = " [Paused]" if self.item.paused else ""
        self._label.setText(f"[{self.item.kind}] {self.item.url}{state}")

    def _on_toggle_pause(self) -> None:
        if self.item.paused:
            self.manager.resume_item(self.item)
            self.pause_btn.setText("Pause")
        else:
            self.manager.pause_item(self.item)
            self.pause_btn.setText("Resume")
        self._refresh_label()

    def _on_remove(self) -> None:
        self.manager.remove_active(self.item)

    def set_paused(self, paused: bool) -> None:
        self.item.paused = paused
        self.pause_btn.setText("Resume" if paused else "Pause")
        self._refresh_label()

    def update_progress(self, percent: int, eta_seconds: int) -> None:
        self._progress.setValue(percent)
        self._eta.setText(f"ETA: {format_seconds(eta_seconds)}")


class _QueueRow(QWidget):
    """A single row shown in the waiting-queue list, with a remove button."""

    def __init__(self, item: DownloadItem, parent=None) -> None:
        super().__init__(parent)
        self.item = item
        paused = " [Paused]" if item.paused else ""
        retry = f" — retrying (attempt {item.attempts}/{item.max_attempts})" if item.attempts else ""
        self._label = QLabel(f"[{item.kind}] {item.url}{paused}{retry}")
        self.remove_btn = _XButton(self._on_remove)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.addWidget(self._label, stretch=1)
        layout.addWidget(self.remove_btn)

    def _on_remove(self) -> None:
        if self.parentWidget() is None:
            return
        window = self.window()
        remove = getattr(window, "_remove_queued_item", None)
        if remove:
            remove(self.item)


class _CompletedRow(QWidget):
    """A single row shown in the completed-downloads list, with a remove button."""

    def __init__(self, item: DownloadItem, parent=None) -> None:
        super().__init__(parent)
        self.item = item
        self._label = QLabel(f"✓ [{item.kind}] {item.url}")
        self._label.setStyleSheet("color: #0a0;")
        self.remove_btn = _XButton(self._on_remove)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.addWidget(self._label, stretch=1)
        layout.addWidget(self.remove_btn)

    def _on_remove(self) -> None:
        if self.parentWidget() is None:
            return
        window = self.window()
        remove = getattr(window, "_remove_completed_item", None)
        if remove:
            remove(self.item)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_TITLE} v{APP_VERSION}")
        self.resize(860, 700)

        # Default settings (overridden by Settings dialog / saved state).
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

        # Maps active item -> QListWidgetItem row.
        self._active_rows: Dict[DownloadItem, QListWidgetItem] = {}

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

        # Load persisted state (settings, queue, active downloads).
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
        src = QGroupBox("Source")
        src_layout = QVBoxLayout(src)
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText(
            "YouTube URL or video ID (e.g. dQw4w9WgXcQ); invidious URLs accepted"
        )
        self.validation_label = QLabel("Enter a URL or ID to validate")
        self.validation_label.setStyleSheet("color: #666; font-style: italic;")
        self.validation_label.setWordWrap(True)
        src_layout.addWidget(self.url_input)
        src_layout.addWidget(self.validation_label)
        layout.addWidget(src)

        # ---- Options ------------------------------------------------------
        opt = QGroupBox("Options")
        opt_layout = QHBoxLayout(opt)

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
        layout.addWidget(opt)

        # ---- Actions ------------------------------------------------------
        actions = QHBoxLayout()
        self.download_now_btn = QPushButton("Download now")
        self.download_now_btn.setFixedWidth(120)
        self.download_now_btn.setEnabled(False)
        self.download_now_btn.setToolTip("Start this download immediately (ahead of queued items)")
        self.add_queue_btn = QPushButton("Add to queue")
        self.add_queue_btn.setFixedWidth(110)
        self.add_queue_btn.setEnabled(False)
        self.add_queue_btn.setToolTip("Queue this download; it starts when a slot is free")
        self.pause_all_btn = QPushButton("Pause all")
        self.pause_all_btn.setFixedWidth(100)
        self.pause_all_btn.setToolTip("Pause every active and pending download (survives restart)")
        self.clear_btn = QPushButton("Clear list")
        self.clear_btn.setFixedWidth(90)
        self.clear_btn.setToolTip("Remove everything from all lists")
        for b in (self.download_now_btn, self.add_queue_btn, self.pause_all_btn, self.clear_btn):
            actions.addWidget(b)
        actions.addStretch(1)
        layout.addLayout(actions)

        # ---- Active downloads ---------------------------------------------
        active = QGroupBox("Active downloads")
        active_layout = QVBoxLayout(active)
        self.active_list = QListWidget()
        self.active_list.setFixedHeight(105)
        active_layout.addWidget(self.active_list)
        layout.addWidget(active)

        # ---- Queue --------------------------------------------------------
        queue = QGroupBox("Queue (waiting to download)")
        queue_layout = QVBoxLayout(queue)
        self.queue_list = QListWidget()
        self.queue_list.setFixedHeight(100)
        queue_layout.addWidget(self.queue_list)
        layout.addWidget(queue)

        # ---- Completed ----------------------------------------------------
        completed = QGroupBox("Completed downloads")
        completed_layout = QVBoxLayout(completed)
        self.completed_list = QListWidget()
        self.completed_list.setFixedHeight(90)
        completed_layout.addWidget(self.completed_list)
        layout.addWidget(completed)

        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("color: #333;")
        layout.addWidget(self.status_label)

    # ------------------------------------------------------------------ signals
    def _connect_signals(self) -> None:
        self.url_input.textChanged.connect(self._on_url_changed)
        self.browse_btn.clicked.connect(self._on_browse)
        self.download_now_btn.clicked.connect(self._on_download_now)
        self.add_queue_btn.clicked.connect(self._on_add_to_queue)
        self.pause_all_btn.clicked.connect(self._on_pause_all)
        self.clear_btn.clicked.connect(self._on_clear_clicked)

        # Manager signals.
        self._manager.item_validated.connect(self._on_validated)
        self._manager.item_started.connect(self._on_item_started)
        self._manager.item_progress.connect(self._on_item_progress)
        self._manager.item_status.connect(self._on_item_status)
        self._manager.item_finished.connect(self._on_item_finished)
        self._manager.queue_changed.connect(self._refresh_lists)
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
        self.download_now_btn.setEnabled(False)
        self.add_queue_btn.setEnabled(False)
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
            self.validation_label.setText(
                f"✓  Ready to download:  {title_or_error}"
            )
            self.validation_label.setStyleSheet("color: #0a0; font-size: 13px; font-weight: bold;")
        else:
            self._validated_url = None
            self._validated_title = None
            self.validation_label.setText(f"✗  Invalid: {title_or_error}")
            self.validation_label.setStyleSheet("color: #a00; font-weight: bold;")
        enabled = bool(self.url_input.text().strip()) and ok
        # Downloads never commence on validation — only via an explicit button.
        self.download_now_btn.setEnabled(enabled)
        self.add_queue_btn.setEnabled(enabled)

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
        self._manager.add_now(*target)
        self._reset_validation_input()

    def _on_add_to_queue(self) -> None:
        target = self._validated_target()
        if target is None:
            return
        self._manager.add(*target)
        self._reset_validation_input()

    def _reset_validation_input(self) -> None:
        self.url_input.clear()
        self._validated_url = None
        self._validated_title = None
        self.download_now_btn.setEnabled(False)
        self.add_queue_btn.setEnabled(False)
        self.validation_label.setText("Enter a URL or ID to validate")
        self.validation_label.setStyleSheet("color: #666; font-style: italic;")

    def _on_pause_all(self) -> None:
        if self._has_anything_paused():
            self._manager.resume_all()
            self.pause_all_btn.setText("Pause all")
        else:
            self._manager.pause_all()
            self.pause_all_btn.setText("Resume all")
        self._save_state()

    def _has_anything_paused(self) -> bool:
        return bool(
            self._manager._paused_items
            or any(it.paused for it in self._manager.queue_items())
        )

    def _on_clear_clicked(self) -> None:
        self._manager.clear_queue()
        self._active_rows.clear()
        self.active_list.clear()
        self.pause_all_btn.setText("Pause all")
        self.status_label.setText("Ready")

    def _remove_queued_item(self, item: DownloadItem) -> None:
        items = self._manager.queue_items()
        for i, it in enumerate(items):
            if it is item:
                self._manager.remove_queued(i)
                return
        # Paused previously-active items may still live in the paused set.
        self._manager.remove_active(item)

    def _remove_completed_item(self, item: DownloadItem) -> None:
        self._manager.remove_completed(item)

    # ------------------------------------------------------------------ lists
    def _refresh_lists(self) -> None:
        # Waiting queue (rebuild; rows carry their own ✕).
        self.queue_list.clear()
        for item in self._manager.queue_items():
            row = _QueueRow(item)
            li = QListWidgetItem()
            li.setSizeHint(row.sizeHint())
            self.queue_list.addItem(li)
            self.queue_list.setItemWidget(li, row)
        # Completed list (rebuild; cleared on startup since state never
        # persists completed downloads).
        self.completed_list.clear()
        for item in self._manager.completed_items():
            row = _CompletedRow(item)
            li = QListWidgetItem()
            li.setSizeHint(row.sizeHint())
            self.completed_list.addItem(li)
            self.completed_list.setItemWidget(li, row)
        if self._has_anything_paused():
            self.pause_all_btn.setText("Resume all")
        elif not self._manager._paused_items:
            self.pause_all_btn.setText("Pause all")

    # ------------------------------------------------------------------ state persistence
    def _save_state(self) -> None:
        """Persist current settings, queue, active downloads, and URL input."""
        if self._closing:
            return
        save_state(
            settings=self._settings,
            queue_items=self._manager.queue_items(),
            active_items=self._manager.active_items(),
            url_input=self.url_input.text(),
        )

    def _load_state(self) -> None:
        """Restore settings, queue, and active downloads from the state file."""
        payload = load_state()
        if payload is None:
            self._state_save_timer.start()
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

        url_input = payload.get("url_input", "")
        if url_input:
            self.url_input.setText(url_input)

        if self._has_anything_paused():
            self.pause_all_btn.setText("Resume all")
        self._state_save_timer.start()

    # ------------------------------------------------------------------ active downloads
    def _on_item_started(self, item: DownloadItem) -> None:
        list_item = self._active_rows.get(item)
        if list_item is None:
            row_widget = _ActiveRow(item, self._manager)
            list_item = QListWidgetItem()
            list_item.setSizeHint(row_widget.sizeHint())
            self.active_list.addItem(list_item)
            self.active_list.setItemWidget(list_item, row_widget)
            self._active_rows[item] = list_item
        else:
            widget = self.active_list.itemWidget(list_item)
            if isinstance(widget, _ActiveRow):
                widget.set_paused(False)
                widget.update_progress(0, 0)
                widget._eta.setText("ETA: --:--")
        if item.paused:
            widget = self.active_list.itemWidget(list_item)
            if isinstance(widget, _ActiveRow) and widget.pause_btn.text() != "Resume":
                widget.set_paused(True)
        self.status_label.setText(f"Downloading: {item.url}")

    def _on_item_progress(self, item: DownloadItem, percent: int, eta_seconds: int) -> None:
        list_item = self._active_rows.get(item)
        if list_item is None:
            return
        widget = self.active_list.itemWidget(list_item)
        if isinstance(widget, _ActiveRow):
            widget.update_progress(percent, eta_seconds)

    def _on_item_status(self, item: DownloadItem, message: str) -> None:
        self.status_label.setText(message)

    def _on_item_finished(
        self, item: DownloadItem, success: bool, message: str, destination: str
    ) -> None:
        status = "Finished" if success else "FAILED"
        self.status_label.setText(
            f"{status}: {item.url} — {message}"
            + (f" [saved to {destination}]" if success and destination else "")
        )
        if success:
            self._remove_active_row(item)   # moves to Completed list
        else:
            self._remove_active_row(item)

    def _remove_active_row(self, item: DownloadItem) -> None:
        list_item = self._active_rows.pop(item, None)
        if list_item is not None:
            row = self.active_list.row(list_item)
            if row >= 0:
                self.active_list.takeItem(row)

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
        # Always persist state before exiting (graceful shutdown saves the
        # queue and active downloads so they auto-resume on next start).
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
