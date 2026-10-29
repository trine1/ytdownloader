"""Settings dialog: retry backoff, parallel downloads, default output dir."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)


class SettingsDialog(QDialog):
    """User-configurable download behaviour.

    Returns a dict with keys:
        max_parallel, max_attempts, initial_backoff, max_backoff,
        default_output_dir, notify_completed, notify_all_done
    """

    def __init__(self, settings: dict, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(420)
        self._settings = dict(settings)

        self._build_ui()
        self._load_settings()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        retry = QGroupBox("Auto-retry (exponential backoff)")
        retry_grid = QGridLayout(retry)

        self.max_attempts = QSpinBox()
        self.max_attempts.setRange(1, 50)
        self.max_attempts.setSuffix(" attempts")
        retry_grid.addWidget(QLabel("Max attempts per download:"), 0, 0)
        retry_grid.addWidget(self.max_attempts, 0, 1)

        self.initial_backoff = QSpinBox()
        self.initial_backoff.setRange(1, 3600)
        self.initial_backoff.setSuffix(" s")
        retry_grid.addWidget(QLabel("Initial retry delay:"), 1, 0)
        retry_grid.addWidget(self.initial_backoff, 1, 1)

        self.max_backoff = QSpinBox()
        self.max_backoff.setRange(1, 3600)
        self.max_backoff.setSuffix(" s")
        retry_grid.addWidget(QLabel("Max retry delay (cap):"), 2, 0)
        retry_grid.addWidget(self.max_backoff, 2, 1)

        layout.addWidget(retry)

        parallel = QGroupBox("Parallel downloads")
        parallel_grid = QGridLayout(parallel)

        self.max_parallel = QSpinBox()
        self.max_parallel.setRange(1, 16)
        self.max_parallel.setSuffix(" concurrent")
        parallel_grid.addWidget(QLabel("Downloads in parallel:"), 0, 0)
        parallel_grid.addWidget(self.max_parallel, 0, 1)

        layout.addWidget(parallel)

        outdir = QGroupBox("Default output directory")
        outdir_layout = QHBoxLayout(outdir)
        self.output_dir = QLineEdit()
        self.browse_btn = QPushButton("Browse…")
        self.browse_btn.setFixedWidth(80)
        outdir_layout.addWidget(self.output_dir, stretch=1)
        outdir_layout.addWidget(self.browse_btn)
        layout.addWidget(outdir)

        notify = QGroupBox("Notifications (D-Bus toasts)")
        notify_layout = QVBoxLayout(notify)
        self.notify_completed = QCheckBox(
            "When an individual download completes"
        )
        self.notify_completed.setToolTip(
            "Show a desktop toast (org.freedesktop.Notifications) for every\n"
            "download that finishes successfully."
        )
        self.notify_all_done = QCheckBox(
            "When all queued downloads have finished"
        )
        self.notify_all_done.setToolTip(
            "Show one desktop toast when the queue drains — i.e. nothing is\n"
            "queued, active, paused or waiting for a retry any more."
        )
        notify_layout.addWidget(self.notify_completed)
        notify_layout.addWidget(self.notify_all_done)
        layout.addWidget(notify)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.browse_btn.clicked.connect(self._on_browse)

    # ------------------------------------------------------------------ load / save
    def _load_settings(self) -> None:
        self.max_parallel.setValue(self._settings.get("max_parallel", 1))
        self.max_attempts.setValue(self._settings.get("max_attempts", 10))
        self.initial_backoff.setValue(self._settings.get("initial_backoff", 5))
        self.max_backoff.setValue(self._settings.get("max_backoff", 300))
        self.output_dir.setText(self._settings.get("default_output_dir", ""))
        self.notify_completed.setChecked(
            bool(self._settings.get("notify_completed", True))
        )
        self.notify_all_done.setChecked(
            bool(self._settings.get("notify_all_done", True))
        )

    def _on_browse(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Select default output directory", self.output_dir.text()
        )
        if directory:
            self.output_dir.setText(directory)

    def settings(self) -> dict:
        return {
            "max_parallel": self.max_parallel.value(),
            "max_attempts": self.max_attempts.value(),
            "initial_backoff": self.initial_backoff.value(),
            "max_backoff": self.max_backoff.value(),
            "default_output_dir": self.output_dir.text().strip(),
            "notify_completed": self.notify_completed.isChecked(),
            "notify_all_done": self.notify_all_done.isChecked(),
        }
