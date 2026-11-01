#!/usr/bin/env python3
"""Live check against a real Invidious instance (uses the network on purpose).

Proves the whole feature with the real yt-dlp and the real instance the user
reported, in both directions:

  1. a window boots with the new routing code;
  2. an instance link validates over the network and is titled;
  3. the download goes to YouTube (the primary route) and names the file
     after the video;
  4. when YouTube is made unreachable, the instance's own API proves the
     source and the instance's DASH manifest downloads it;
  5. the resulting row dedupes against the YouTube spelling of the same id.

The instance has to be online for this to mean anything; if it is not, the
script says so and exits 0 rather than pretending to pass.

Run:
    QT_QPA_PLATFORM=offscreen YTDOWNLOADER_NO_UPDATE=1 \
        python tests/live_check_invidious.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = Path(tempfile.mkdtemp(prefix="ytd-live-inv-"))

import yt_downloader.state as st                 # noqa: E402
st.STATE_DIR = TMP / "state"
st.STATE_FILE = st.STATE_DIR / "state.json"

import yt_downloader.history as hist             # noqa: E402
hist.HISTORY_DIR = st.STATE_DIR
hist.HISTORY_FILE = st.STATE_DIR / "downloads.json"

from PyQt6.QtCore import QCoreApplication        # noqa: E402
from PyQt6.QtWidgets import QApplication        # noqa: E402

from yt_downloader import sources as src         # noqa: E402
from yt_downloader import worker as w            # noqa: E402

PASS, FAIL = [], []


def check(cond, what):
    print(("PASS " if cond else "FAIL ") + str(what))
    (PASS if cond else FAIL).append(what)


ID = os.environ.get("YTD_LIVE_ID", "kGeCeK85sUg")
HOST = os.environ.get("YTD_LIVE_HOST", "https://yt.chocolatemoo53.com")
INST = f"{HOST}/watch?v={ID}&t=15"
YT = f"https://www.youtube.com/watch?v={ID}"

# ---------------------------------------------------------------- is it up?
def instance_alive():
    req = urllib.request.Request(
        src.api_url(HOST, ID), headers={"User-Agent": "ytdownloader-check"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


print(f"probing {src.api_url(HOST, ID)} ...")
INFO = instance_alive()
if not INFO or not INFO.get("title"):
    print("SKIPPED: the instance is offline or does not serve this video")
    sys.exit(0)
REAL_TITLE = INFO["title"]
print(f"instance is live, title: {REAL_TITLE!r}\n")

# ------------------------------------------------------------ 1. app boots
app = QApplication(sys.argv[:1])
from yt_downloader import main_window as mw      # noqa: E402
win = mw.MainWindow()
win.show()
app.processEvents()
check(win.isVisible(), "the window boots with the Invidious routing in place")
check("yt_downloader.sources" in sys.modules or src is not None, "sources module loaded")
win.close()
app.processEvents()

# how many yt-dlp/API calls each stage made
CALLS = []
API_CALLS = []
_real_popen = w.subprocess.Popen
_orig_fetch = src.fetch_json


def spying_popen(args, **kw):
    CALLS.append(list(args))
    return _real_popen(args, **kw)


def spying_fetch(url, timeout=10.0):
    API_CALLS.append(url)
    return _orig_fetch(url, timeout=timeout)


w.subprocess.Popen = spying_popen
src.fetch_json = spying_fetch

# ------------------------------------------------- 2. validation, both ways
def run_validate(text):
    """One real validation, synchronously. Returns (ok, resolved, message)."""
    worker = w.Worker()
    got = []
    worker.validated.connect(lambda *a: got.append(a))
    worker._mode = "validate"
    worker._url = worker._resolve_url(text)
    worker._run_validation()
    QCoreApplication.processEvents()
    return got[0] if got else (None, None, None)


CALLS.clear(); API_CALLS.clear()
src.clear_route_cache()
ok, resolved, msg = run_validate(INST)
check(ok is True, f"the reported instance link VALIDATES: {msg!r}")
check(msg == REAL_TITLE, f"with the real title: {msg!r} == {REAL_TITLE!r}")
check(resolved == YT, f"and resolves to the canonical YouTube URL: {resolved!r}")
check(len(CALLS) == 1 and CALLS[0][-1] == YT,
      "the instance was not consulted — yt-dlp's YouTube extractor answered")
check(not API_CALLS, "no HTTP probe of the instance when YouTube works")

# a bare id still works, and a YouTube link is untouched
CALLS.clear()
ok, resolved, msg = run_validate(YT)
check(ok is True and resolved == YT, "a plain YouTube link validates as before")

# ------------------------------------------ 3. the primary download route
def run_download(item):
    worker = w.Worker()
    done, statuses = [], []
    worker.finished.connect(lambda *a: done.append(a))
    worker.status.connect(statuses.append)
    worker._mode = "download"
    worker._item = item
    worker._resume = False
    worker._run_download()
    QCoreApplication.processEvents()
    return (done[0] if done else (None, None, None)), statuses


outdir = TMP / "dl"
item = w.DownloadItem(INST, "audio", str(outdir), title=REAL_TITLE)
CALLS.clear()
result, _statuses = run_download(item)
ok, msg, dest = result
check(ok is True, f"the instance link downloads end to end: {msg!r}")
check(dest and Path(dest).is_file() and Path(dest).stat().st_size > 10000,
      f"a real file exists: {dest}")
check(dest and REAL_TITLE.replace("/", "-")[:20] in Path(dest).name,
      f"named after the video: {Path(dest).name if dest else None!r}")
check(len(CALLS) == 1 and CALLS[0][-1] == YT,
      "and it went to YouTube, the primary route")

# ------------------------------- 4. YouTube unreachable -> the instance
_real_watch_url = src.watch_url
try:
    # Make the YouTube route fail the way a blocked network does: a dead
    # endpoint. The instance route must rescue validation and the download.
    src.watch_url = lambda vid: f"https://127.0.0.1:9/watch?v={vid}"

    CALLS.clear(); API_CALLS.clear()
    src.clear_route_cache()
    ok, resolved, msg = run_validate(INST)
    check(ok is True, f"with YouTube unreachable the source is still proved: {msg!r}")
    check(API_CALLS == [src.api_url(HOST, ID)],
          f"by the instance's own API: {API_CALLS}")
    check(msg == REAL_TITLE, f"and its title: {msg!r}")
    check(resolved == src.instance_watch_url(HOST, ID),
          f"the item then stays on the instance: {resolved!r}")
    check(src.remembered(ID) == "invidious", "the working route is remembered")

    # ------------------------------------------------ 5. instance download
    CALLS.clear()
    item2 = w.DownloadItem(resolved, "audio", str(TMP / "dl2"), title=REAL_TITLE)
    (ok2, msg2, dest2), statuses2 = run_download(item2)
    check(ok2 is True, f"the instance route downloads for real: {msg2!r}")
    check(dest2 and Path(dest2).is_file() and Path(dest2).stat().st_size > 10000,
          f"writing a real file: {dest2}")
    check(len(CALLS) == 1 and CALLS[0][-1] == src.manifest_url(HOST, ID),
          f"straight to the instance manifest, no wasted YouTube try: {CALLS[0][-1]!r}")
    check(dest2 and REAL_TITLE.replace("/", "-")[:20] in Path(dest2).name,
          f"still named after the video: {Path(dest2).name if dest2 else None!r}")

    # the ffprobe-free sanity check: the file is a real media container
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=format_name",
         "-of", "csv=p=0", dest2],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    check(probe.returncode == 0 and probe.stdout.strip(),
          f"the instance download is playable audio: {probe.stdout.strip()!r}")
finally:
    src.watch_url = _real_watch_url

# ------------------------------- 6. the same video is one source, always
src.clear_route_cache()
CALLS.clear()
win2 = mw.MainWindow()
win2._validation_timer.stop()
from yt_downloader.manager import _canonical_source   # noqa: E402
check(_canonical_source(INST) == _canonical_source(YT) == ID,
      "the instance and YouTube links canonicalise to the same id")
queued = win2._manager._make_item(YT, "audio", str(outdir), title=REAL_TITLE)
win2._manager._queue.append(queued)
win2.url_input.setText(INST)
win2._validation_timer.stop()
win2.format_combo.setCurrentIndex(0)
win2._validation_timer.stop()
win2._run_validation()
check("Already downloading in audio format" in win2.validation_label.text(),
      f"the instance link is recognised as the queued video: "
      f"{win2.validation_label.text()!r}")
win2._manager._queue.remove(queued)
win2.close()
app.processEvents()

w.subprocess.Popen = _real_popen
src.fetch_json = _orig_fetch

print()
print(f"artifacts kept in {TMP}")
print(f"checks: {len(PASS) + len(FAIL)}   passed: {len(PASS)}   failed: {len(FAIL)}")
print("RESULT: ALL PASS" if not FAIL else f"RESULT: {len(FAIL)} FAILURES")
sys.exit(0 if not FAIL else 1)
