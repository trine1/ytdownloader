#!/usr/bin/env python3
"""Batch-summary toast suite for the queue-drained notification.

Pins down the rules agreed for the summary toast:

  * it fires once the queue has drained AND a batch is pending;
  * a lone success is skipped — it already raised its own per-item toast;
  * MORE than one completion is summarised;
  * **a failure-only batch is NEVER skipped**: failures have no toast of
    their own, so the summary is their only announcement. (This is the
    open question, resolved: do not suppress it.)
  * the title must match the body — a mixed batch did not "all finish";
  * `notify_all_done = False` silences everything.

Hermetic: state and history are redirected to a temp dir, urllib is wired
to raise, and dbus_notify.notify_async is replaced by a recorder, so no
real D-Bus call is ever made.

Run:
    QT_QPA_PLATFORM=offscreen YTDOWNLOADER_NO_UPDATE=1 python tests/test_toast.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = Path(tempfile.mkdtemp(prefix="ytd-toast-"))

import yt_downloader.state as st                # noqa: E402
st.STATE_DIR = TMP / "state"
st.STATE_FILE = st.STATE_DIR / "state.json"

import yt_downloader.history as hist            # noqa: E402
hist.HISTORY_DIR = st.STATE_DIR
hist.HISTORY_FILE = st.STATE_DIR / "downloads.json"

from PyQt6.QtWidgets import QApplication        # noqa: E402

app = QApplication(sys.argv[:1])

from yt_downloader import dbus_notify           # noqa: E402
from yt_downloader import main_window as mw     # noqa: E402
from yt_downloader import sources as src        # noqa: E402

PASS, FAIL = [], []
TOASTS = []


def check(cond, what):
    print(("PASS " if cond else "FAIL ") + str(what))
    (PASS if cond else FAIL).append(what)


# ---- hermetic guards -----------------------------------------------------
def _no_network(req, timeout=None):
    raise AssertionError(f"TEST REACHED THE NETWORK: {getattr(req, 'full_url', req)}")


src.urllib.request.urlopen = _no_network
src.fetch_json = lambda url, timeout=10.0: (_ for _ in ()).throw(
    AssertionError("instance API used from the toast suite"))

_real_notify = dbus_notify.notify_async


def _record(title, body="", *a, **k):
    TOASTS.append((title, body))


dbus_notify.notify_async = _record


def new_window():
    win = mw.MainWindow()
    win._validation_timer.stop()
    win._manager.validate = lambda text: None
    win._manager._start_next = lambda *a, **k: None
    win._settings["notify_completed"] = True
    win._settings["notify_all_done"] = True
    return win


def drain(win, done=0, failed=0, pending=True):
    """Simulate the queue going idle with a recorded batch outcome."""
    TOASTS.clear()
    win._batch_completed = done
    win._batch_failed = failed
    win._batch_pending = pending
    win._on_queue_changed()
    return list(TOASTS)


win = new_window()

# ============ 1. the queue must actually be idle ==========================
item = win._manager._make_item("https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                               "audio", str(TMP), title="x")
fake_worker = object()                        # _active is keyed by the Worker
win._manager._active[fake_worker] = item      # a worker still running
t = drain(win, done=3, failed=1)
check(t == [], "a running download defers the summary")
check(win._batch_pending is True, "and the batch stays pending for later")
del win._manager._active[fake_worker]

t = drain(win, done=3, failed=1, pending=False)
check(t == [], "no pending batch means no toast (e.g. a single quiet item)")

t = drain(win, done=0, failed=0)
check(t == [], "an empty batch raises nothing")

# ============ 2. a lone success is skipped ===============================
# ...because _on_item_completed already raised its own per-item toast.
TOASTS.clear()
win._settings["notify_completed"] = True
fake_item = win._manager._make_item(
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "audio", str(TMP),
    title="Some Song")
dest = str(TMP / "Some Song [audio].m4a")
Path(dest).write_bytes(b"data")
win._on_item_completed(fake_item, dest)
check(TOASTS and TOASTS[0][0] == "Download complete",
      f"a finished download toasts on its own: {TOASTS}")

t = drain(win, done=1, failed=0)
check(t == [], "so the summary skips a lone success (no duplicate news)")

# ============ 3. more than one completion is summarised ==================
t = drain(win, done=2, failed=0)
check(len(t) == 1, "two completions raise exactly one summary")
check(t and t[0] == ("All downloads finished", "2 finished"),
      f"worded as before: {t}")

t = drain(win, done=5, failed=0)
check(t and t[0][1] == "5 finished", f"counts every completion: {t}")

# ============ 4. THE OPEN QUESTION: failure-only batches =================
t = drain(win, done=0, failed=3)
check(len(t) == 1, "a failure-only batch DOES raise the summary")
check(t and t[0][0] == "All downloads failed",
      f"titled by what actually happened: {t}")
check(t and t[0][1] == "3 failed",
      f"and without the noise of '0 finished': {t}")

t = drain(win, done=0, failed=1)
check(len(t) == 1, "even a single failure is announced (it has no toast of its own)")
check(t and t[0][1] == "1 failed", f"worded in the singular: {t}")

# ============ 5. a mixed batch must not claim it "all finished" ==========
t = drain(win, done=2, failed=1)
check(len(t) == 1, "a mixed batch raises a summary")
check(t and t[0][0] == "Downloads finished with errors",
      f"the title admits the failures: {t}")
check(t and t[0][1] == "2 finished, 1 failed", f"body counts both: {t}")

# ============ 6. the preference silences everything ======================
win._settings["notify_all_done"] = False
for d, f in ((2, 0), (0, 3), (1, 1)):
    t = drain(win, done=d, failed=f)
    check(t == [], f"notify_all_done=False suppresses the ({d} finished, {f} failed) summary")
win._settings["notify_all_done"] = True

# ============ 7. counters reset, so nothing toasts twice =================
t = drain(win, done=2, failed=1)
check(len(t) == 1, "first drain toasts")
t = drain(win)                      # queue goes idle again, nothing new
check(t == [], "and a second idle pass is silent (counters were reset)")
check(win._batch_completed == 0 and win._batch_failed == 0,
      "both counters really are back to zero")

# ============ 8. the per-item switch does not gate the summary ===========
win._settings["notify_completed"] = False
t = drain(win, done=2, failed=0)
check(len(t) == 1, "per-item toasts off does not hide the batch summary")
win._settings["notify_completed"] = True

# ============ 9. the deferred batch fires once the queue drains ==========
win._manager._queue.append(
    win._manager._make_item("https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                            "audio", str(TMP), title="x"))
TOASTS.clear()
win._batch_completed = 2
win._batch_failed = 0
win._batch_pending = True
win._on_queue_changed()
check(TOASTS == [] and win._batch_pending is True, "still busy: held back")
win._manager._queue.clear()
win._on_queue_changed()
check(len(TOASTS) == 1, "the moment it drains, the held-back summary fires")

dbus_notify.notify_async = _real_notify

print()
print(f"checks: {len(PASS) + len(FAIL)}   passed: {len(PASS)}   failed: {len(FAIL)}")
print("RESULT: ALL PASS" if not FAIL else f"RESULT: {len(FAIL)} FAILURES")
sys.exit(0 if not FAIL else 1)
