"""Entry point for the YouTube Downloader application."""

from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication

from .main_window import APP_TITLE, MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
