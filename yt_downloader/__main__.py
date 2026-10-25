"""Allow ``python -m yt_downloader`` to launch the app."""

from __future__ import annotations

import sys

from .main import main

if __name__ == "__main__":
    raise SystemExit(main())
