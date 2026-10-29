"""Main application window."""

from __future__ import annotations

import functools
import inspect
import os
import traceback
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
from . import dbus_notify
from .manager import DownloadManager
from .settings_dialog import SettingsDialog
from .state import _item_from_dict, load_state, save_state
from .worker import DownloadItem, format_seconds

APP_TITLE = "YouTube Downloader"
DEFAULT_OUTPUT_DIR = os.path.expanduser("~/Downloads")


def _safe_slot(fn):
    """Run a Qt slot so an exception can never escape into PyQt6.

    PyQt6 routes exceptions raised inside a slot through sys.excepthook and
    aborts the process (core dump). A plain Python error in any button
    handler or signal slot therefore looked like "the application
    terminates". Log it and keep the event loop alive instead.

    Two subtleties handled here:

    * ``functools.wraps`` leaves the wrapper taking ``*args``, and PyQt
      passes the *whole* signal payload to such a callable (it only trims
      arguments for plain signatures) — e.g. ``item_finished(item, ok, msg,
      dest)`` handed four arguments to ``_save_state()``. The wrapper
      therefore trims to the wrapped function's declared parameters.
    * Anything still wrong (a genuine bug) is printed instead of being
      allowed to reach Qt, where it would take the app down.
    """
    try:
        params = list(inspect.signature(fn).parameters.values())
        n_pos = len([
            p for p in params
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ])
        has_varargs = any(p.kind == p.VAR_POSITIONAL for p in params)
    except (TypeError, ValueError):
        n_pos, has_varargs = 0, True
    # Qt calls the *bound* method, so `self` is already bound: keep at most
    # (n_pos - 1) of the signal's own arguments.
    limit = None if has_varargs else max(n_pos - 1, 0)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if limit is not None and len(args) > limit + 1:
            args = args[: limit + 1]
        try:
            return fn(*args, **kwargs)
        except Exception:
            traceback.print_exc()
            return None
    return wrapper


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
        # Completed rows show just the tick and the final file name.
        dest = getattr(item, "destination", "")
        filename = os.path.basename(dest) if dest else ""
        return f"✓ {filename}" if filename else f"✓ {display}"
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

    def update_item(self, item: DownloadItem) -> None:
        """Light update: refresh just one row's widgets (progress path)."""
        row = self._rows.get(id(item))
        if row is not None:
            self._update_row(item, row)

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

        # Title label (stretched to fill remaining space).
        # Ignored horizontal policy is critical: a QLabel's minimum width
        # is normally its FULL text width, which would blow the row's
        # minimum width past the scroll viewport and push the progress
        # bar out of view. Ignored lets the row shrink to viewport width.
        title = QLabel()
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        title.setMinimumWidth(40)
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

        # Per-row Pause/Resume button
        pause_btn = QPushButton("Pause")
        pause_btn.setFixedWidth(78)
        pause_btn.setFixedHeight(26)
        pause_btn.clicked.connect(lambda _=False, r=row: self._row_pause(r))
        layout.addWidget(pause_btn)

        # Per-row Remove (cancel) button
        remove_btn = QPushButton("✕")
        remove_btn.setFixedWidth(26)
        remove_btn.setFixedHeight(26)
        remove_btn.setToolTip("Remove this download")
        remove_btn.clicked.connect(lambda _=False, r=row: self._row_cancel(r))
        layout.addWidget(remove_btn)

        # Store references on the widget for later updates
        row._title = title        # type: ignore[attr-defined]
        row._bar = bar            # type: ignore[attr-defined]
        row._eta = eta            # type: ignore[attr-defined]
        row._pause_btn = pause_btn    # type: ignore[attr-defined]
        row._remove_btn = remove_btn  # type: ignore[attr-defined]
        # Re-elide whenever the label gets its real width after layout
        title.installEventFilter(self)
        return row

    # ---- per-row button handlers -----------------------------------------
    def _sync_global_pause_btn(self) -> None:
        """Tell the main window to re-evaluate the global pause button
        and persist the new state."""
        win = self.window()
        sync = getattr(win, "_sync_controls", None)
        if callable(sync):
            sync()
        save = getattr(win, "_save_state", None)
        if callable(save):
            save()

    @_safe_slot
    def _row_pause(self, row: QWidget) -> None:
        item = getattr(row, "_item", None)
        if item is None:
            return
        if item.paused:
            self._manager.resume_item(item)
        else:
            self._manager.pause_item(item)
        self.refresh()
        self._sync_global_pause_btn()

    @_safe_slot
    def _row_cancel(self, row: QWidget) -> None:
        item = getattr(row, "_item", None)
        if item is None:
            return
        self._manager.remove_item(item)
        self.refresh()
        self._sync_global_pause_btn()

    def eventFilter(self, obj, event) -> bool:
        from PyQt6.QtCore import QEvent
        if isinstance(obj, QLabel) and event.type() == QEvent.Type.Resize:
            row = obj.parentWidget()
            caption = getattr(row, "_caption", None) if row is not None else None
            if caption:
                obj.setText(self._elide(obj, caption))
                return False
        return super().eventFilter(obj, event)

    def _update_row(self, item: DownloadItem, row: QWidget) -> None:
        """Refresh a row's widgets with the item's current state."""
        state = _row_state(item, self._manager)
        pct = _row_pct(item, self._manager)
        eta_text = _row_eta(item, self._manager)
        caption = _caption(item, self._manager)

        # Bind the item so the row's buttons act on it
        row._item = item  # type: ignore[attr-defined]

        # Store caption so resize can re-elide
        row._caption = caption  # type: ignore[attr-defined]

        # Title — elide to current label width so long titles never force
        # the row wider than the visible canvas
        row._title.setText(self._elide(row._title, caption))  # type: ignore[attr-defined]
        # Completed rows show only "✓ filename", so point the tooltip at
        # the full path; other rows keep the source URL.
        if state == "completed" and getattr(item, "destination", ""):
            row._title.setToolTip(item.destination)  # type: ignore[attr-defined]
        else:
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

        # Per-row buttons: Pause/Resume hidden once finished; ✕ always
        # available (removes the row from the list).
        pause_btn = row._pause_btn  # type: ignore[attr-defined]
        remove_btn = row._remove_btn  # type: ignore[attr-defined]
        if state in ("completed", "failed"):
            pause_btn.setVisible(False)
            remove_btn.setToolTip("Remove from list")
        else:
            pause_btn.setVisible(True)
            pause_btn.setText("Resume" if item.paused else "Pause")
            pause_btn.setToolTip(
                "Resume this download" if item.paused else "Pause this download"
            )
            remove_btn.setToolTip("Cancel and remove this download")

    @staticmethod
    def _elide(label: QLabel, text: str) -> str:
        w = label.width()
        if w <= 20:
            return text
        return label.fontMetrics().elidedText(
            text, Qt.TextElideMode.ElideRight, w - 4
        )

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override name
        super().resizeEvent(event)
        # Re-elide titles with the new width
        for item in self._items:
            row = self._rows.get(id(item))
            if row is not None:
                caption = getattr(row, "_caption", None)
                if caption:
                    row._title.setText(self._elide(row._title, caption))  # type: ignore[attr-defined]


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
            # Desktop toasts (Settings -> Preferences -> Notifications).
            "notify_completed": True,
            "notify_all_done": True,
        }

        self._validated_url: Optional[str] = None
        self._validated_title: Optional[str] = None
        self._closing = False
        self._force_quit = False

        # Toast bookkeeping: is a batch of downloads outstanding, and how
        # many of it finished (to phrase the "all done" toast)?
        self._batch_pending = False
        self._batch_completed = 0
        self._batch_failed = 0

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
            "YouTube/Invidious URL or video ID (e.g. dQw4w9WgXcQ)"
        )
        src_row.addWidget(self.url_input, stretch=1)
        layout.addLayout(src_row)

        self.validation_label = QLabel("Enter a URL or ID to validate")
        self.validation_label.setStyleSheet("color: #666; font-style: italic;")
        self.validation_label.setWordWrap(True)
        # Titles can be very long; never let this label's sizeHint widen
        # the window (yt-dlp status/title text once grew the top-level
        # window to 1969px, pushing the progress bar off-screen).
        self.validation_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
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
        self.pause_all_btn = QPushButton("Pause all downloads")
        self.pause_all_btn.setFixedWidth(180)
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
        # CRITICAL: this label receives long yt-dlp lines (warnings can be
        # ~200 chars / 1950px). An unwrapped QLabel's sizeHint is its FULL
        # text width; Qt's propagateSizeHints then GROWS the top-level
        # window far beyond the real compositor size, laying content out
        # off the right edge (progress bar pushed off-screen). Ignored
        # horizontal policy removes the label from width calculations
        # entirely. Text is elided to the real width by _set_status().
        self.status_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
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
        # Toast when the queue drains (Preferences -> Notifications).
        self._manager.queue_changed.connect(self._on_queue_changed)
        # State persistence.
        self._manager.queue_changed.connect(self._save_state)
        self._manager.item_started.connect(self._save_state)
        self._manager.item_finished.connect(self._save_state)

    # ------------------------------------------------------------------ settings
    @_safe_slot
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
    @_safe_slot
    def _on_url_changed(self, text: str) -> None:
        self._validated_url = None
        self._validated_title = None
        self.validation_label.setText("Enter a URL or ID to validate")
        self.validation_label.setStyleSheet("color: #666; font-style: italic;")
        self.download_btn.setEnabled(False)
        if text.strip():
            self._validation_timer.start()

    @_safe_slot
    def _run_validation(self) -> None:
        text = self.url_input.text().strip()
        if not text:
            return
        self.validation_label.setText("Validating…")
        self.validation_label.setStyleSheet("color: #666; font-style: italic;")
        self._manager.validate(text)

    @_safe_slot
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
    @_safe_slot
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

    @_safe_slot
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

    @_safe_slot
    def _on_pause_all(self) -> None:
        if self._has_anything_paused():
            self._manager.resume_all()
        else:
            self._manager.pause_all()
        self._save_state()
        self._refresh_downloads()

    def _has_anything_paused(self) -> bool:
        return bool(
            self._manager._paused_items
            or any(it.paused for it in self._manager.queue_items())
            or any(
                it.paused for it in getattr(self._manager, "_scheduled_retries", [])
            )
        )

    @_safe_slot
    def _on_clear_clicked(self) -> None:
        self._manager.clear_downloaded()
        self._set_status("Completed and failed items cleared")

    # ------------------------------------------------------------------ canvas refresh
    @_safe_slot
    def _refresh_downloads(self) -> None:
        """One atomic canvas repaint — every row, every control, every time."""
        self._canvas.refresh()
        self._sync_controls()

    def _sync_controls(self) -> None:
        """Update the global buttons' labels and enable state.

        - "Pause all downloads" / "Resume all downloads": greyed out when
          no live download exists (only completed/failed rows left) —
          there would be nothing to pause or resume.
        - "Clear downloaded": greyed out when the list holds no
          completed or failed items to clear.
        """
        mgr = self._manager
        anything_paused = self._has_anything_paused()
        self.pause_all_btn.setText(
            "Resume all downloads" if anything_paused else "Pause all downloads"
        )
        has_live = bool(
            mgr.active_items()
            or mgr.queue_items()
            or mgr._paused_items
            or getattr(mgr, "_scheduled_retries", [])
        )
        self.pause_all_btn.setEnabled(has_live)

        self.clear_btn.setEnabled(
            bool(mgr.completed_items() or mgr.failed_items())
        )

    def _ensure_fresh_paint(self) -> None:
        """Watchdog: request a normal paint of the downloads area.
        With standard Qt widgets, update() is all that is needed —
        Qt manages frame submission to the Wayland compositor."""
        if self.isVisible():
            self._canvas.update()

    def _set_status(self, text: str) -> None:
        """Set status text, elided to the label's real width.

        Long yt-dlp lines (e.g. 200-char warnings) must never inflate the
        label's sizeHint — combined with the Ignored size policy this
        guarantees the top-level window can never be grown by text.
        """
        w = self.status_label.width()
        if w > 40:
            display = self.status_label.fontMetrics().elidedText(
                text, Qt.TextElideMode.ElideRight, w - 4
            )
        else:
            display = text
        self.status_label.setToolTip(text)  # full message always in tooltip
        self.status_label.setText(display)

    # ------------------------------------------------------------------ worker events
    @_safe_slot
    def _on_item_started(self, item: DownloadItem) -> None:
        self._batch_pending = True   # a batch is outstanding: may warrant a toast
        self._canvas.refresh()
        self._set_status(f"Downloading: {item.title or item.url}")

    @_safe_slot
    def _on_item_progress(self, item: DownloadItem, percent: int, eta_seconds: int) -> None:
        setattr(item, "last_pct", percent)
        setattr(item, "last_eta", eta_seconds)
        # Widget-based row: push new values into the row's own widgets.
        self._canvas.update_item(item)

    @_safe_slot
    def _on_item_status(self, item: DownloadItem, message: str) -> None:
        self._set_status(message)

    @_safe_slot
    def _on_item_finished(
        self, item: DownloadItem, success: bool, message: str, destination: str
    ) -> None:
        if success:
            setattr(item, "destination", destination or getattr(item, "destination", ""))
            setattr(item, "last_pct", 100)
            self._set_status(
                f"Finished: {item.title or item.url}"
                + (f" [saved to {destination}]" if destination else "")
            )
        else:
            setattr(item, "fail_message", message)
            self._batch_failed += 1
            self._set_status(f"FAILED: {item.title or item.url} — {message}")

    @_safe_slot
    def _on_item_completed(self, item: DownloadItem, destination: str) -> None:
        self._batch_completed += 1
        self._refresh_downloads()
        # Toast for this one download (Settings -> Preferences -> Notifications).
        if self._settings.get("notify_completed"):
            name = os.path.basename(destination) if destination else ""
            dbus_notify.notify_async("Download complete", name or item.title or item.url)

    @_safe_slot
    def _on_queue_changed(self) -> None:
        """Fire the batch toast once nothing is queued/active/paused/retrying."""
        mgr = self._manager
        live = bool(
            mgr.active_items()
            or mgr.queue_items()
            or mgr._paused_items
            or mgr._scheduled_retries
        )
        if live:
            self._batch_pending = True
            return
        if not self._batch_pending:
            return
        self._batch_pending = False
        done, failed = self._batch_completed, self._batch_failed
        self._batch_completed = 0
        self._batch_failed = 0
        if not (done or failed) or not self._settings.get("notify_all_done"):
            return
        parts = [f"{done} finished"]
        if failed:
            parts.append(f"{failed} failed")
        summary = "All downloads finished" if done else "All downloads failed"
        dbus_notify.notify_async(summary, ", ".join(parts))

    # ------------------------------------------------------------------ state persistence
    @_safe_slot
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
                    "max_backoff", "default_output_dir",
                    "notify_completed", "notify_all_done"):
            if key in saved:
                self._settings[key] = saved[key]
        self.output_input.setText(self._settings["default_output_dir"])

        queue_items = [_item_from_dict(d) for d in payload.get("queue", [])]
        active_items = [_item_from_dict(d) for d in payload.get("active", [])]
        # Completed downloads are intentionally NOT restored.
        self._manager.restore(self._settings, queue_items, active_items)

        self._refresh_downloads()
        self._sync_controls()
        self._state_save_timer.start()
        self._repaint_watchdog.start()

    # ------------------------------------------------------------------ exit
    def _exit_without_prompting(self) -> None:
        """Quit immediately (no prompt) — state is saved for auto-resume."""
        self._force_quit = True
        self.close()

    @_safe_slot
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