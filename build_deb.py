"""Build a .deb package for ytdownloader without requiring dpkg-deb.

Produces a Debian binary package installable on Linux Mint LMDE 7
(Debian 12 bookworm) and other Debian-based systems.

Usage:
    python3 build_deb.py
"""

from __future__ import annotations

import io
import os
import stat
import tarfile
import time

PACKAGE = "ytdownloader"
VERSION = "0.5.1-1"
ARCH = "amd64"
OUTPUT = f"{PACKAGE}_{VERSION.split('-')[0]}_{ARCH}.deb"

ROOT = os.path.dirname(os.path.abspath(__file__))
DEBIAN_DIR = os.path.join(ROOT, "debian")
SRC_PKG = os.path.join(ROOT, "yt_downloader")

UNIX_EPOCH = int(time.time())


def _ar_header(name: str, size: int, mode: int = 0o644) -> bytes:
    """Build a 60-byte ar member header."""
    # name (16), date (12), uid (6), gid (6), mode (8), size (10), end (2)
    name_b = name.encode("ascii")
    if len(name_b) > 16:
        raise ValueError(f"ar member name too long: {name}")
    return (
        name_b.ljust(16, b" ")
        + f"{UNIX_EPOCH:<12}".encode("ascii")
        + f"{0:<6}".encode("ascii")
        + f"{0:<6}".encode("ascii")
        + f"{mode:<8o}".encode("ascii")
        + f"{size:<10}".encode("ascii")
        + b"`\n"
    )


def _pad_to_even(data: bytes) -> bytes:
    return data + (b"\n" if len(data) % 2 else b"")


def build_control_tar() -> bytes:
    """control.tar.gz containing the Debian control metadata."""
    buf = io.BytesIO()
    control_path = os.path.join(DEBIAN_DIR, "control")
    with open(control_path) as f:
        control_bytes = f.read().encode("utf-8")

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="./control")
        info.size = len(control_bytes)
        info.mode = 0o644
        info.mtime = UNIX_EPOCH
        tar.addfile(info, io.BytesIO(control_bytes))
    return buf.getvalue()


def build_data_tar() -> bytes:
    """data.tar.gz containing the files to install."""
    buf = io.BytesIO()
    entries: list[tuple[str, str, int]] = []  # (src, arcname, mode)

    # Launcher in /usr/bin
    entries.append(
        (os.path.join(ROOT, "yt_downloader/launcher.sh"), "usr/bin/ytdownloader", 0o755)
    )
    # Desktop file
    entries.append(
        (os.path.join(ROOT, "yt_downloader.desktop"), "usr/share/applications/ytdownloader.desktop", 0o644)
    )
    # Python package modules
    py_dest = "usr/lib/python3/dist-packages/yt_downloader"
    for fname in sorted(os.listdir(SRC_PKG)):
        if fname.endswith(".py"):
            entries.append(
                (
                    os.path.join(SRC_PKG, fname),
                    f"{py_dest}/{fname}",
                    0o644,
                )
            )

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for src, arcname, mode in entries:
            with open(src, "rb") as f:
                data = f.read()
            info = tarfile.TarInfo(name="./" + arcname)
            info.size = len(data)
            info.mode = mode
            info.mtime = UNIX_EPOCH
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def build_deb() -> None:
    control_tar = build_control_tar()
    data_tar = build_data_tar()

    debian_binary = b"2.0\n"

    members = [
        ("debian-binary", debian_binary, 0o644),
        ("control.tar.gz", control_tar, 0o644),
        ("data.tar.gz", data_tar, 0o644),
    ]

    out = io.BytesIO()
    out.write(b"!<arch>\n")
    for name, data, mode in members:
        out.write(_ar_header(name, len(data), mode))
        out.write(_pad_to_even(data))

    out_path = os.path.join(ROOT, OUTPUT)
    with open(out_path, "wb") as f:
        f.write(out.getvalue())

    size = os.path.getsize(out_path)
    print(f"Built {out_path} ({size} bytes)")


if __name__ == "__main__":
    build_deb()
