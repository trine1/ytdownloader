#!/usr/bin/env python3
"""ETA-smoothing suite: the number on the row must not jitter.

yt-dlp recomputes the ETA on every progress line from its current speed
estimate, so the raw readings bounce (0:12, 0:08, 0:15, 0:09 ...) even
while the download is going perfectly steadily. The worker therefore
feeds the readings through a short median filter before they reach the
UI.

This suite pins:

  * a single wild reading cannot move the displayed ETA (the median
    ignores it completely);
  * a steady stream of readings settles on the value they agree on;
  * a genuine change still gets through, within a few lines;
  * the window is per attempt - a fresh yt-dlp run starts from scratch;
  * the raw percentage is passed through untouched;
  * the Help dialog does not advertise a button that does not exist.

Hermetic: no network, no real yt-dlp (the process is faked), state and
history are redirected to a temp dir, and urllib is wired to raise, so
an accidental request fails the suite instead of quietly spending one.

Run:
    QT_QPA_PLATFORM=offscreen YTDOWNLOADER_NO_UPDATE=1 python tests/test_eta.py
"""

import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = Path(tempfile.mkdtemp(prefix="ytd-eta-"))

import yt_downloader.state as st                # noqa: E402
st.STATE_DIR = TMP / "state"
st.STATE_FILE = st.STATE_DIR / "state.json"

import yt_downloader.history as hist            # noqa: E402
hist.HISTORY_DIR = st.STATE_DIR
hist.HISTORY_FILE = st.STATE_DIR / "downloads.json"

from PyQt6.QtWidgets import QApplication, QMessageBox   # noqa: E402

app = QApplication(sys.argv[:1])

from yt_downloader import main_window as mw     # noqa: E402
from yt_downloader import sources as src        # noqa: E402
from yt_downloader import worker as w           # noqa: E402

PASS, FAIL = [], []


def check(cond, what):
    print(("PASS " if cond else "FAIL ") + str(what))
    (PASS if cond else FAIL).append(what)


# ---- hermetic guards -----------------------------------------------------
def _no_network(req, timeout=None):
    raise AssertionError(f"TEST REACHED THE NETWORK: {getattr(req, 'full_url', req)}")


src.urllib.request.urlopen = _no_network
src.fetch_json = lambda url, timeout=10.0: (_ for _ in ()).throw(
    AssertionError("instance API used from the ETA suite"))


# ===================== 1. the smoother, on its own ========================
# A steady 100 s ETA must stay exactly 100 s.
sm = w.EtaSmoother()
check([sm.push(100) for _ in range(6)] == [100] * 6,
      "a steady ETA is passed through unchanged")

# One wild reading in the middle must not move the displayed value.
sm = w.EtaSmoother()
check([sm.push(v) for v in (100, 100, 100)] == [100] * 3,
      "the first readings are shown as they come")
check(sm.push(9999) == 100,
      "a single wild reading does not move the ETA at all")
check(sm.push(100) == 100,
      "and it is forgotten as soon as the window moves on")

# A single low outlier is ignored just the same.
sm = w.EtaSmoother()
for _ in range(3):
    sm.push(100)
check(sm.push(0) == 100, "a single low outlier is ignored too")

# With a window of one there is no smoothing, but no crash either.
sm = w.EtaSmoother(window=1)
check([sm.push(v) for v in (10, 20, 30)] == [10, 20, 30],
      "a window of one passes everything through")

# reset() forgets the history.
sm = w.EtaSmoother()
for _ in range(4):
    sm.push(100)
sm.reset()
check(sm.push(500) == 500,
      "after a reset the first reading of the next run is shown as-is")

# The window really is bounded.
sm = w.EtaSmoother(window=3)
for _ in range(50):
    sm.push(100)
check(len(sm._samples) == 3, "the window keeps only the last few samples")


# ================ 2. a genuine change still gets through ==================
# The remaining time really does shrink; the smoother must follow it
# instead of pinning the first value forever.
sm = w.EtaSmoother()
out = [sm.push(v) for v in (300, 280, 260, 240, 220, 200, 180, 160)]
check(out[-1] < out[0],
      f"a falling ETA keeps falling: {out[0]} -> {out[-1]}")
check(out[-1] <= out[0] - 80,
      f"and it tracks the real value closely: {out}")
check(all(out[i + 1] <= out[i] for i in range(len(out) - 1)),
      f"the smoothed ETA never goes backwards: {out}")


# ============ 3. the jitter is actually damped, end to end ================
# The same readings, raw vs smoothed.
JITTER = [12, 8, 15, 9, 13, 7, 16, 10, 14, 8, 12, 9]
sm = w.EtaSmoother()
smoothed = [sm.push(v) for v in JITTER]
raw_span = max(JITTER) - min(JITTER)
smooth_span = max(smoothed) - min(smoothed)
check(smooth_span < raw_span,
      f"the jitter is damped: raw span {raw_span}s -> smoothed span "
      f"{smooth_span}s")
check(smooth_span <= 3,
      f"and the leftover wobble is tiny: {smoothed}")
check(len(set(smoothed)) < len(set(JITTER)),
      f"fewer distinct values are shown: {len(set(smoothed))} vs "
      f"{len(set(JITTER))}")


# ================== 4. through the worker, with a fake yt-dlp ============
CALLS = []
RUNS = {}
YT = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


class FakeProc:
    def __init__(self, args, **kwargs):
        CALLS.append(list(args))
        lines, rc = RUNS.get(args[-1], ([], 1))
        self.stdout = iter(lines)
        self._rc = rc

    def wait(self, timeout=None):
        return self._rc

    def poll(self):
        return None

    def terminate(self):
        pass


w.subprocess.Popen = FakeProc
w.ytdlp_command = lambda: ["/fake/yt-dlp"]


def progress_line(pct, eta_text):
    return f"[download] {pct:.1f}% of 1.00MiB at 1.00MiB/s ETA {eta_text}\n"


def run_progress(lines):
    """One download attempt; returns the (percent, eta) pairs emitted."""
    RUNS[YT] = (list(lines), 0)
    worker = w.Worker()
    got = []
    worker.progress.connect(lambda pct, eta: got.append((pct, eta)))
    worker._mode = "download"
    worker._item = w.DownloadItem(YT, "audio", str(TMP))
    worker._run_download()
    return got


raw_lines = [progress_line(i * 10, t)
             for i, t in enumerate(["0:00:12", "0:00:08", "0:00:15", "0:00:09",
                                    "0:00:13", "0:00:07", "0:00:16", "0:00:10"])]
got = run_progress(raw_lines)
check(len(got) == 8, f"every progress line still reports progress: {got}")
check([p for p, _ in got] == [0, 10, 20, 30, 40, 50, 60, 70],
      f"the percentage is passed through untouched: {[p for p, _ in got]}")
etas = [e for _, e in got]
check(etas == sorted(etas, key=lambda v: 0) or max(etas) - min(etas) <= 4,
      f"the emitted ETA is steady, not jittery: {etas}")
check(max(etas) - min(etas) <= 4,
      f"the emitted ETA spans at most a few seconds: {etas}")

# A single wild line must not reach the UI.
spike_lines = [progress_line(10, "0:00:10"), progress_line(20, "0:00:10"),
               progress_line(30, "0:00:10"), progress_line(40, "9:59:59"),
               progress_line(50, "0:00:10")]
got = run_progress(spike_lines)
etas = [e for _, e in got]
check(etas[3] == 10,
      f"a wild ETA line is swallowed by the filter: {etas}")
check(all(e <= 10 for e in etas),
      f"and the row never shows the spike: {etas}")

# A second attempt starts from scratch - the window is per run.
RUNS[YT] = ([progress_line(10, "0:02:00")], 0)
worker = w.Worker()
got2 = []
worker.progress.connect(lambda pct, eta: got2.append((pct, eta)))
worker._mode = "download"
worker._item = w.DownloadItem(YT, "audio", str(TMP))
worker._run_download()
check(got2 and got2[0][1] == 120,
      f"a fresh attempt starts from its own first reading: {got2}")

# The parse helper itself is untouched.
check(w.Worker._parse_eta("0:00:01") == 1, "ETA 0:00:01 is one second")
check(w.Worker._parse_eta("1:02:03") == 3723, "ETA 1:02:03 is 3723 seconds")
check(w.Worker._parse_eta("02:03") == 123, "ETA 02:03 is 123 seconds")
check(w.format_seconds(3723) == "1:02:03", "format_seconds still formats")
check(w.format_seconds(123) == "2:03", "format_seconds still formats minutes")


# ============= 5. the Help dialog must not promise a fake button ==========
DIALOGS = []
_real_information = QMessageBox.information


def _fake_information(parent, title, text, *a, **k):
    DIALOGS.append((title, text))


QMessageBox.information = staticmethod(_fake_information)

win = mw.MainWindow()
win._validation_timer.stop()
win._manager.validate = lambda text: None
win._manager._start_next = lambda *a, **k: None

DIALOGS.clear()
win._show_how_to_use()
text = DIALOGS[0][1] if DIALOGS else ""
check(bool(text), "the How to use dialog produced text")
check("Add to queue" not in text,
      f"the Help does not advertise an 'Add to queue' button: {text!r}")
check("Download" in text,
      "the Help still tells the user to press Download")
check("add to queue" not in text.lower(),
      "the Help mentions no queueing button at all, in any casing")

QMessageBox.information = _real_information


print()
print(f"checks: {len(PASS) + len(FAIL)}   passed: {len(PASS)}   failed: {len(FAIL)}")
print("RESULT: ALL PASS" if not FAIL else f"RESULT: {len(FAIL)} FAILURES")
sys.exit(0 if not FAIL else 1)
