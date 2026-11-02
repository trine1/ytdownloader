#!/usr/bin/env python3
"""Validation-flow suite: what MainWindow does before it touches a network.

Covers the local-first order (completed row -> live row -> remembered
proof -> network), the source-level proof cache across formats, link shapes
and restarts, and the new Invidious behaviour: an instance link is one more
spelling of the same video id, so it shares every short-circuit.

Hermetic: state and history are redirected to a temp directory, the
manager's validation call is recorded instead of run, the queue pump is
stopped, dialogs are stubbed, and urllib is wired to explode — a test that
accidentally goes online fails instead of passing slowly.

Run:
    QT_QPA_PLATFORM=offscreen YTDOWNLOADER_NO_UPDATE=1 python tests/test_validation_flow.py
"""

import sys
import time
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = Path(tempfile.mkdtemp(prefix="ytd-flow-"))

import yt_downloader.state as st                # noqa: E402
st.STATE_DIR = TMP / "state"
st.STATE_FILE = st.STATE_DIR / "state.json"

import yt_downloader.history as hist            # noqa: E402
hist.HISTORY_DIR = st.STATE_DIR
hist.HISTORY_FILE = st.STATE_DIR / "downloads.json"

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

app = QApplication(sys.argv[:1])

from yt_downloader import main_window as mw     # noqa: E402
from yt_downloader import sources as src        # noqa: E402

PASS, FAIL = [], []


def check(cond, what):
    print(("PASS " if cond else "FAIL ") + str(what))
    (PASS if cond else FAIL).append(what)


# ---- hermetic guards -----------------------------------------------------
def _no_network(req, timeout=None):
    raise AssertionError(f"TEST REACHED THE NETWORK: {getattr(req, 'full_url', req)}")


def _no_probe(url, timeout=10.0):
    raise AssertionError(f"TEST PROBED AN INSTANCE: {url}")


# Nothing in this suite may touch the network: the manager's validate() is
# stubbed below, so a call reaching either of these is a bug in the app.
src.urllib.request.urlopen = _no_network
src.fetch_json = _no_probe


class _Dialogs:
    """Stand in for every message box: record it, answer it, never block."""

    seen = []

    @staticmethod
    def information(parent, title, text, *a, **k):
        _Dialogs.seen.append(("information", title, text))

    @staticmethod
    def warning(parent, title, text, *a, **k):
        _Dialogs.seen.append(("warning", title, text))

    @staticmethod
    def question(parent, title, text, *a, **k):
        _Dialogs.seen.append(("question", title, text))
        return QMessageBox.StandardButton.Yes      # "yes, download again"

    @staticmethod
    def critical(parent, title, text, *a, **k):
        _Dialogs.seen.append(("critical", title, text))


for _name in ("information", "warning", "question", "critical"):
    setattr(QMessageBox, _name, staticmethod(getattr(_Dialogs, _name)))

DIALOGS = _Dialogs.seen

ID = "kGeCeK85sUg"
HOST = "https://yt.chocolatemoo53.com"
INST = f"{HOST}/watch?v={ID}&t=15"
INST2 = f"https://inv.elsewhere.test/watch?v={ID}"
YT = f"https://www.youtube.com/watch?v={ID}"
VIMEO = "https://vimeo.com/76979871"
TITLE = "Jimmy Cliff - Many Rivers To Cross"

VALIDATED = []      # what the manager was asked to validate (never executed)


def new_window():
    """A MainWindow whose manager validates by recording, not by network."""
    win = mw.MainWindow()
    win._validation_timer.stop()

    def fake_validate(text):
        VALIDATED.append(text)

    win._manager.validate = fake_validate
    win._manager._start_next = lambda *a, **k: None      # never download
    return win


def enter(win, text, kind=None):
    """Type *text* (and optionally pick a format) without firing the timer."""
    win._validation_timer.stop()
    if kind is not None:
        win.format_combo.setCurrentIndex(0 if kind == "audio" else 1)
        win._validation_timer.stop()
    win.url_input.setText(text)
    win._validation_timer.stop()


def validate(win, text, kind=None):
    enter(win, text, kind)
    win._run_validation()
    win._validation_timer.stop()


def label(win):
    return win.validation_label.text()


win = new_window()

# ============ 1. a brand-new instance link IS validated (once) ============
validate(win, INST)
check(VALIDATED == [INST], f"an unknown source goes to the network once: {VALIDATED}")
check(label(win) == "Validating…", f"and the label waits: {label(win)!r}")
check(not win.download_btn.isEnabled(), "Download is disabled while validating")

win._on_validated(True, YT, TITLE)
check(win._validated_url == YT, "the worker's resolved URL is what gets queued")
check(win._validated_title == TITLE, "and its title is remembered")
check(label(win) == f"✓  {TITLE}",
      f"a YouTube answer shows the plain tick: {label(win)!r}")
check(win.download_btn.isEnabled(), "Download is offered")

# the proof is on disk, keyed by the video id
check(hist.DownloadHistory().recent_match(INST, 3600) is not None,
      "the instance link hits the remembered proof")
check(hist.DownloadHistory().recent_match(YT, 3600) is not None,
      "so does the canonical YouTube link")

# ============ 2. switching format must not re-validate ===================
VALIDATED.clear()
validate(win, INST, kind="video")
check(VALIDATED == [], "audio -> video after one validation costs nothing")
check("✓" in label(win) and "not re-validated" in label(win),
      f"the label says so: {label(win)!r}")
check("validated" in label(win),
      f"a proof from validation is called 'validated': {label(win)!r}")
validate(win, INST, kind="audio")
check(VALIDATED == [], "and switching back is free too")

# ============ 3. every spelling of the same id is the same source ========
for shape in (YT, f"https://youtu.be/{ID}", ID,
              f"https://m.youtube.com/shorts/{ID}",
              f"https://www.youtube.com/watch?v={ID}&list=PLx",
              INST2, f"{HOST}/watch/{ID}", f"{HOST}/embed/{ID}"):
    VALIDATED.clear()
    validate(win, shape)
    check(VALIDATED == [], f"{shape!r} is recognised as the same video")

# ============ 4. an answer that came from the instance is labelled ======
VALIDATED.clear()
OTHER = "https://yt.other.instance/watch?v=zzzzzzzzzzz"
validate(win, OTHER)
check(VALIDATED == [OTHER], "an unseen instance link validates")
win._on_validated(True, f"https://yt.other.instance/watch?v=zzzzzzzzzzz",
                  "Other Video")
check("(via yt.other.instance)" in label(win),
      f"the label says which route answered: {label(win)!r}")
check(win._validated_url == OTHER,
      "and the item keeps pointing at the instance that served it")

# ============ 5. a completed row in the same format asks to repeat ======
VALIDATED.clear()
item = win._manager._make_item(YT, "audio", str(TMP), title=TITLE)
win._manager._completed.append(item)
validate(win, INST, kind="audio")
check(VALIDATED == [], "a completed row needs no network")
check("Already downloaded in audio format" in label(win),
      f"and offers the re-download prompt: {label(win)!r}")
check(win.download_btn.isEnabled(), "Download is still offered (that is the yes)")
check(win._remembered_duplicate is item, "the click will not ask a second time")

# the OTHER format of a completed row uses the source proof, not the network
VALIDATED.clear()
validate(win, INST, kind="video")
check(VALIDATED == [], "the other format is accepted from the proof")
check("✓" in label(win), f"and reads as accepted: {label(win)!r}")

# ============ 6. a row that is still downloading is never re-validated ==
for pool_name in ("_queue", "_paused_items", "_scheduled_retries"):
    win._manager._completed.remove(item)
    live = win._manager._make_item(YT, "audio", str(TMP), title=TITLE)
    getattr(win._manager, pool_name).append(live)
    VALIDATED.clear()
    validate(win, INST, kind="audio")
    check(VALIDATED == [], f"a {pool_name} row blocks validation of the same format")
    check("Already downloading in audio format" in label(win),
          f"{pool_name}: duplicate notice: {label(win)!r}")
    VALIDATED.clear()
    validate(win, INST, kind="video")
    check(VALIDATED == [], f"a {pool_name} row accepts the other format locally")
    check("✓" in label(win) and "no re-validation" in label(win),
          f"{pool_name}: other format accepted: {label(win)!r}")
    getattr(win._manager, pool_name).remove(live)
    win._manager._completed.append(item)

# an ACTIVE (running) row too
live = win._manager._make_item(YT, "audio", str(TMP), title=TITLE)
placeholder_worker = object()          # _active is keyed by the Worker
win._manager._active[placeholder_worker] = live
VALIDATED.clear()
validate(win, INST2, kind="video")
check(VALIDATED == [], "a running download answers the other format as well")
del win._manager._active[placeholder_worker]
win._manager._completed.remove(item)

# ============ 7. the window is configurable, 0 disables it ==============
win._settings["validation_cache_minutes"] = 0
VALIDATED.clear()
validate(win, INST)
check(VALIDATED == [INST], "a zero-minute window always re-validates")
win._settings["validation_cache_minutes"] = 60
VALIDATED.clear()
validate(win, INST)
check(VALIDATED == [], "and the remembered window is honoured again")

# an entry older than the window is no proof
win._history.record(f"https://youtu.be/{ID}", "audio",
                    title="Ancient", now=time.time() - 61 * 60)
VALIDATED.clear()
validate(win, INST)
check(VALIDATED == [INST], "a proof older than the window expires")
win._on_validated(True, YT, TITLE)      # refresh it for the tests below

# ============ 8. it survives a restart ==================================
VALIDATED.clear()
win2 = new_window()
validate(win2, INST)
check(VALIDATED == [], "a fresh window knows the source from downloads.json")
check("✓" in label(win2) and "not re-validated" in label(win2),
      f"and says so: {label(win2)!r}")
check(win2._validated_title == TITLE, "the remembered title is the row title")
validate(win2, INST2, kind="video")
check(VALIDATED == [], "the other instance of the same id is the same video")

# ============ 9. pressing Download queues what was validated ============
DIALOGS.clear()
_validate_url = win2._validated_url
enter(win2, INST2, kind="video")
win2._run_validation()
win2.output_input.setText(str(TMP / "out"))
win2._on_download_now()
queued = win2._manager.queue_items()
check(len(queued) == 1, f"one item was queued: {[i.url for i in queued]}")
check(queued[0].url == _validate_url,
      "the queued URL is the validated one (routes are resolved later)")
check(queued[0].kind == "video", "in the format that was selected")
check(queued[0].output_dir == str(TMP / "out"), "to the chosen directory")

# ============ 10. the download path itself is still gated locally =======
VALIDATED.clear()
enter(win2, INST2, kind="audio")       # same source, other format, now queued
win2._run_validation()
check(VALIDATED == [], "the row just queued blocks re-validation")
check("Already downloading in video format" in label(win2)
      or "downloading video now" in label(win2),
      f"the notice names the live format: {label(win2)!r}")

# ============ 11. canonical source really is host-agnostic =============
from yt_downloader.manager import _canonical_source  # noqa: E402
for shape in (INST, INST2, YT, f"https://youtu.be/{ID}", ID,
              f"{HOST}/watch/{ID}", f"https://m.youtube.com/live/{ID}"):
    check(_canonical_source(shape) == ID, f"canonical: {shape!r} -> {ID}")
check(_canonical_source(VIMEO) == VIMEO,
      "an unrelated URL is its own canonical source")

# ============ 12. an invalid instance link says so =====================
VALIDATED.clear()
BAD = "https://yt.chocolatemoo53.com/watch?v=qqqqqqqqqqq"
validate(win2, BAD)
check(VALIDATED == [BAD], "an unknown source validates")
win2._on_validated(False, BAD, "invalid URL or video unavailable; "
                               "yt.chocolatemoo53.com did not answer")
check("✗" in label(win2) and "did not answer" in label(win2),
      f"both refusals reach the user: {label(win2)!r}")
check(not win2.download_btn.isEnabled(), "and Download stays disabled")
check(win2._validated_url is None, "nothing is queued-ready after a refusal")

# ============ 13. a download proof is worded differently ===============
win2._manager._queue.clear()          # the live row from section 9 would
win2._manager._failed.clear()         # answer before the history does
win2._history.record(f"https://youtu.be/{ID}", "audio", title="Done Deal",
                     destination=str(TMP / "f.mp4"))
VALIDATED.clear()
validate(win2, f"https://youtu.be/{ID}")
check(VALIDATED == [], "a completed download is still a proof")
check("downloaded" in label(win2),
      f"and is called 'downloaded', not 'validated': {label(win2)!r}")

# ============ 14. the Source field is clean, the hint lives below ========
check(win2.url_input.placeholderText() == "",
      "the Source field carries no placeholder text")
check(label(win2) == "Enter a URL or ID to validate" or "✓" in label(win2)
      or "✗" in label(win2) or "Already" in label(win2),
      "the hint underneath the field still speaks for itself")

# ============ 15. Help -> How to use… ===================================
menu_bar = win2.menuBar()
top_level = [a.text().replace("&", "") for a in menu_bar.actions()]
howto = None
for menu_action in menu_bar.actions():
    menu = menu_action.menu()
    if menu is None:
        continue
    for action in menu.actions():
        if action.text().replace("&", "").startswith("How to use"):
            howto = action
check(howto is not None, f"the Help menu offers a How to use entry: {top_level}")
DIALOGS.clear()
win2._show_how_to_use()
check(len(DIALOGS) == 1 and DIALOGS[0][0] == "information",
      "it opens an information dialog")
text = DIALOGS[0][2] if DIALOGS else ""
for needle in ("youtu.be", "video ID", "Invidious", "Shorts", "Vimeo",
               "Download"):
    check(needle in text, f"the overview mentions {needle!r}")

print()
print(f"checks: {len(PASS) + len(FAIL)}   passed: {len(PASS)}   failed: {len(FAIL)}")
print("RESULT: ALL PASS" if not FAIL else f"RESULT: {len(FAIL)} FAILURES")
sys.exit(0 if not FAIL else 1)
