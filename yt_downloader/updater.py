"""Automatic updates: check GitHub Releases, download, install over the app.

Deliberately **stdlib only** (``urllib`` / ``hashlib`` / ``tarfile`` /
``subprocess``) so the tool keeps exactly two dependencies: PyQt6 and
yt-dlp.

Two installation strategies self-update:

``installer``
    The app runs from ``<prefix>/share/ytdownloader/venv`` (the standalone
    installer). We download the release's self-extracting installer,
    verify it against ``SHA256SUMS.txt``, and re-run it with the *same*
    ``--prefix`` — it rewrites the venv in place and keeps ``state.json``.

``uv``
    The app runs from a uv tool environment (``…/uv/tools/<name>``). We
    download the tagged source archive from GitHub, extract it, and run
    ``uv tool install --force <dir>`` — the same command that installed it.

System-wide installs (``/usr``, i.e. the .deb) and source checkouts need
privileges or a human, so they only ever *report* that an update is
available (status ``available``) with a hint.

Every public entry point either returns a :class:`UpdateResult` or raises
:class:`UpdateError`; nothing here is allowed to take the GUI down.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import __version__

RELEASES_API = (
    "https://api.github.com/repos/trine1/ytdownloader/releases/latest"
)
ARCHIVE_URL = (
    "https://github.com/trine1/ytdownloader/archive/refs/tags/{tag}.tar.gz"
)
INSTALLER_NAME = "ytdownloader-{version}-linux-installer.sh"
SUMS_NAME = "SHA256SUMS.txt"

CHECK_TIMEOUT = 15.0
DOWNLOAD_TIMEOUT = 180.0
INSTALL_TIMEOUT = 1800.0
USER_AGENT = f"ytdownloader-updater/{__version__}"


class UpdateError(Exception):
    """A recoverable update problem, always surfaced to the user as text."""


@dataclass
class Release:
    tag: str                      # "v0.5.8"
    version: str                  # "0.5.8"
    assets: Dict[str, str] = field(default_factory=dict)  # name -> URL
    html_url: str = ""


@dataclass
class Strategy:
    kind: str                     # "installer" | "uv" | "system" | "source"
    prefix: str = ""              # installer: <prefix> to pass to --prefix
    tool_dir: str = ""            # uv: the tool environment directory
    hint: str = ""                # manual guidance when kind can't self-update


@dataclass
class UpdateResult:
    """Outcome of a check (and, when applicable, an install).

    status:
        ``up-to-date``  newest release is not newer than the running version
        ``updated``     downloaded *and* installed successfully
        ``available``   newer release exists but this install can't self-update
        ``error``       check/download/verification/install failed
    """

    status: str
    message: str = ""
    version: str = ""


# ----------------------------------------------------------------- versions
def parse_version(text: str) -> tuple:
    """``"v0.5.8"`` / ``"0.5.8-1"`` -> ``(0, 5, 8)`` (non-digits ignored)."""
    return tuple(int(n) for n in re.findall(r"\d+", text or "")) or (0,)


def is_newer(candidate: str, current: str) -> bool:
    """True when *candidate* is a strictly newer version than *current*."""
    a, b = parse_version(candidate), parse_version(current)
    width = max(len(a), len(b))
    a += (0,) * (width - len(a))
    b += (0,) * (width - len(b))
    return a > b


# ------------------------------------------------------------------- HTTP
def _read(url: str, timeout: float) -> bytes:
    """GET *url* and return the body. Raises :class:`UpdateError`."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json, */*",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:          # 403 rate limit, 404, …
        raise UpdateError(f"the server answered HTTP {exc.code} for {url}")
    except urllib.error.URLError as exc:
        raise UpdateError(f"cannot reach {url}: {exc.reason}")
    except (TimeoutError, OSError) as exc:
        raise UpdateError(f"cannot reach {url}: {exc}")


def check_latest(timeout: float = CHECK_TIMEOUT) -> Release:
    """Fetch the newest published release. Raises :class:`UpdateError`."""
    try:
        data = json.loads(_read(RELEASES_API, timeout))
    except ValueError:
        raise UpdateError("the release feed was not valid JSON")
    if not isinstance(data, dict):
        raise UpdateError("the release feed had an unexpected shape")
    tag = str(data.get("tag_name") or "").strip()
    if not tag:
        raise UpdateError("the release feed did not name a release")
    assets: Dict[str, str] = {}
    for asset in data.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        name, url = asset.get("name"), asset.get("browser_download_url")
        if name and url:
            assets[str(name)] = str(url)
    return Release(
        tag=tag,
        version=tag.lstrip("vV"),
        assets=assets,
        html_url=str(data.get("html_url") or ""),
    )


# ---------------------------------------------------------------- strategy
def detect_strategy(prefix: Optional[Path] = None) -> Strategy:
    """Work out how *this* copy of the app was installed.

    *prefix* overrides ``sys.prefix`` (tests use it to model other hosts).
    """
    p = Path(prefix) if prefix is not None else Path(sys.prefix)

    # standalone installer: <prefix>/share/ytdownloader/venv
    if (
        p.name == "venv"
        and p.parent.name == "ytdownloader"
        and p.parent.parent.name == "share"
    ):
        return Strategy("installer", prefix=str(p.parent.parent.parent))

    # uv tool environment: .../uv/tools/<name>
    if p.parent.name == "tools" and p.parent.parent.name == "uv":
        return Strategy("uv", tool_dir=str(p))

    # system-wide (.deb / make install): root-owned, needs a human
    if str(p) in ("/usr", "/usr/local"):
        return Strategy(
            "system",
            hint="this copy is installed system-wide — install the new .deb "
                 "with your package manager",
        )

    return Strategy(
        "source",
        hint="this copy runs from a source checkout — update it with "
             "`git pull` (then `uv tool install --force .`)",
    )


# ----------------------------------------------------------- download/verify
def download(url: str, dest_dir: Path, timeout: float = DOWNLOAD_TIMEOUT) -> Path:
    """Download *url* into *dest_dir* under its own file name."""
    name = url.rstrip("/").rsplit("/", 1)[-1] or "download.bin"
    dest = Path(dest_dir) / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(_read(url, timeout))
    return dest


def verify_sha256(path: Path, sums_path: Path) -> None:
    """Check *path* against a sha256sum-style *sums_path* (raises)."""
    wanted = None
    try:
        lines = sums_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise UpdateError(f"cannot read checksums: {exc}")
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == path.name:
            wanted = parts[0].lower()
            break
    if not wanted:
        raise UpdateError(f"{path.name} is not listed in {sums_path.name}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != wanted:
        raise UpdateError(f"checksum mismatch for {path.name} — download corrupted")


def _safe_extract(archive: Path, dest: Path) -> Path:
    """Extract a single-rooted tarball, refusing traversal/symlinks.

    Returns the top-level directory it produced.
    """
    try:
        tar = tarfile.open(archive, "r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise UpdateError(f"cannot open the downloaded archive: {exc}")
    dest = Path(dest).resolve()
    with tar:
        for member in tar.getmembers():
            target = (dest / member.name).resolve()
            if target != dest and dest not in target.parents:
                raise UpdateError(f"unsafe path in archive: {member.name}")
            if member.issym() or member.islnk():
                raise UpdateError(f"refusing link in archive: {member.name}")
        try:
            tar.extractall(dest, filter="data")   # Python ≥ 3.12
        except TypeError:
            tar.extractall(dest)                  # older Pythons (checked above)
    roots = [p for p in dest.iterdir() if p.is_dir()]
    if len(roots) != 1:
        raise UpdateError("the downloaded archive had an unexpected layout")
    return roots[0]


def _find_uv() -> str:
    found = shutil.which("uv")
    if found:
        return found
    for candidate in (
        Path.home() / ".local" / "bin" / "uv",
        Path.home() / ".cargo" / "bin" / "uv",
    ):
        if candidate.is_file():
            return str(candidate)
    raise UpdateError("uv was not found on PATH (needed to update a uv install)")


def _run(cmd: List[str], timeout: float, label: str) -> None:
    """Run *cmd*, raising :class:`UpdateError` on any failure."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        raise UpdateError(f"{label} could not be started: {cmd[0]} not found")
    except subprocess.TimeoutExpired:
        raise UpdateError(f"{label} timed out after {int(timeout)} s")
    except OSError as exc:
        raise UpdateError(f"{label} could not be started: {exc}")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        raise UpdateError(
            f"{label} failed (exit {proc.returncode})" + (f": {tail}" if tail else "")
        )


# ------------------------------------------------------------------ install
def install_release(release: Release, strategy: Strategy, workdir: Path) -> str:
    """Download and install *release* for *strategy*. Raises UpdateError."""
    if strategy.kind == "installer":
        name = INSTALLER_NAME.format(version=release.version)
        url = release.assets.get(name)
        if not url:
            raise UpdateError(f"release {release.tag} does not ship {name}")
        installer = download(url, Path(workdir))
        sums_url = release.assets.get(SUMS_NAME)
        if sums_url:
            # The sums come from the same release over the same TLS
            # connection — it catches truncated/mismatched downloads.
            verify_sha256(installer, download(sums_url, Path(workdir)))
        _run(
            ["bash", str(installer), "--prefix", strategy.prefix],
            timeout=INSTALL_TIMEOUT,
            label="the installer",
        )
        return f"installed {release.tag} over {strategy.prefix}"

    if strategy.kind == "uv":
        archive = download(ARCHIVE_URL.format(tag=release.tag), Path(workdir))
        source = _safe_extract(archive, Path(workdir))
        _run(
            [_find_uv(), "tool", "install", "--force", str(source)],
            timeout=INSTALL_TIMEOUT,
            label="uv tool install",
        )
        return f"installed {release.tag} into {strategy.tool_dir}"

    raise UpdateError(strategy.hint or f"this install ({strategy.kind}) cannot self-update")


def auto_update(
    current: Optional[str] = None,
    strategy: Optional[Strategy] = None,
    perform_install: bool = True,
    timeout: float = CHECK_TIMEOUT,
) -> UpdateResult:
    """Check for a newer release and install it. **Never raises.**

    Returns an :class:`UpdateResult`; see its docstring for the statuses.
    """
    current = current or __version__
    strategy = strategy or detect_strategy()
    try:
        latest = check_latest(timeout=timeout)
    except UpdateError as exc:
        return UpdateResult("error", str(exc))
    except Exception as exc:                       # noqa: BLE001 - belt & braces
        return UpdateResult("error", f"update check failed: {exc}")

    if not is_newer(latest.version, current):
        return UpdateResult(
            "up-to-date", f"already running the newest version ({current})", current
        )

    if not perform_install or strategy.kind not in ("installer", "uv"):
        return UpdateResult(
            "available",
            f"{latest.tag} is available — {strategy.hint or 'install it manually'}"
            + (f" ({latest.html_url})" if latest.html_url else ""),
            latest.version,
        )

    try:
        with tempfile.TemporaryDirectory(prefix="ytd-update-") as tmp:
            message = install_release(latest, strategy, Path(tmp))
    except UpdateError as exc:
        return UpdateResult("error", str(exc), latest.version)
    except Exception as exc:                       # noqa: BLE001
        return UpdateResult("error", f"update failed: {exc}", latest.version)
    return UpdateResult("updated", message, latest.version)
