#!/usr/bin/env python3
"""Route/recognition suite for yt_downloader.sources and the worker routes.

Hermetic: no network and no real yt-dlp. Every yt-dlp run is a fake process
that answers from a table keyed by URL, and every instance probe is stubbed —
a code path that unexpectedly reaches the network fails loudly instead.

Run:
    QT_QPA_PLATFORM=offscreen YTDOWNLOADER_NO_UPDATE=1 python tests/test_routes.py
"""

import io
import os
import sys
import tempfile
import types
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import QCoreApplication   # noqa: E402

app = QCoreApplication(sys.argv[:1])

from yt_downloader import sources as src    # noqa: E402
from yt_downloader import worker as w       # noqa: E402

PASS, FAIL = [], []


def check(cond, what):
    print(("PASS " if cond else "FAIL ") + str(what))
    (PASS if cond else FAIL).append(what)


ID = "kGeCeK85sUg"
INST = f"https://yt.chocolatemoo53.com/watch?v={ID}&t=15"
HOST = "https://yt.chocolatemoo53.com"
YT = f"https://www.youtube.com/watch?v={ID}"
MANIFEST = f"{HOST}/api/manifest/dash/id/{ID}"
VIMEO = "https://vimeo.com/76979871"
TITLE = "Jimmy Cliff - Many Rivers To Cross"


# =========================== 1. id extraction =============================
check(src.video_id_of(INST) == ID, "instance watch?v=ID&t=15 -> the id")
check(src.video_id_of(f"{HOST}/watch?v={ID}") == ID, "instance watch without params")
check(src.video_id_of(f"https://youtu.be/{ID}") == ID, "youtu.be link")
check(src.video_id_of(f"https://youtu.be/{ID}?si=xyz") == ID, "youtu.be with query")
check(src.video_id_of(f"https://m.youtube.com/shorts/{ID}") == ID, "shorts")
check(src.video_id_of(f"{HOST}/embed/{ID}") == ID, "embed on an instance")
check(src.video_id_of(f"https://www.youtube.com/live/{ID}") == ID, "live")
check(src.video_id_of(f"https://www.youtube.com/v/{ID}") == ID, "/v/ form")
check(src.video_id_of(f"{HOST}/watch/{ID}") == ID, "/watch/ID path form")
check(src.video_id_of(f"https://host/inv/watch?v={ID}") == ID, "instance under a subpath")
check(src.video_id_of(ID) == ID, "bare id")
check(src.video_id_of(f"{YT}&list=PLxyz") == ID, "watch inside a playlist link")
check(src.video_id_of(f"{HOST}/watch?v={ID}#frag") == ID, "id before a fragment")

for bad in ("", "   ", None, "https://example.com/page?x=1", VIMEO,
            "short", f"{ID}toolong", f"{HOST}/watch?v={ID}extra",
            f"{HOST}/watch?v=tooshort1"):
    check(src.video_id_of(bad) is None, f"no id extracted from {bad!r}")


# ========================= 2. host classification =========================
for good in ("youtube.com", "www.youtube.com", "m.youtube.com",
             "music.youtube.com", "gaming.youtube.com", "youtu.be",
             "www.youtube-nocookie.com", "googlevideo.com"):
    check(src.is_youtube_host(f"https://{good}/watch?v={ID}"),
          f"{good} is YouTube itself")
for bad_host in ("yt.chocolatemoo53.com", "inv.nadeko.net", "piped.kavin.rocks",
                 "youtube.com.evil.test", "notyoutube.com", "vimeo.com"):
    check(not src.is_youtube_host(f"https://{bad_host}/watch?v={ID}"),
          f"{bad_host} is NOT YouTube")
check(not src.is_youtube_host(""), "empty string is not YouTube")
check(not src.is_youtube_host(ID), "a bare id is not a host")


# ========================= 3. instance recognition ========================
check(src.instance_base(INST) == HOST, "instance recognised")
check(src.instance_base(f"https://host.example/inv/watch?v={ID}")
      == "https://host.example/inv", "subpath instance keeps its prefix")
check(src.instance_base(f"http://invidious.local/watch?v={ID}")
      == "http://invidious.local", "plain http instance")
check(src.instance_base(YT) is None, "YouTube's own link is not an instance")
check(src.instance_base(f"https://youtu.be/{ID}") is None, "youtu.be is not an instance")
check(src.instance_base(ID) is None, "a bare id has no instance")
check(src.instance_base(VIMEO) is None, "vimeo is not an instance")
check(src.instance_base(f"https://blog.example.com/?v={ID}") is None,
      "a random page with a v= parameter is not an instance")
check(src.instance_base(f"ftp://host/watch?v={ID}") is None, "only http(s)")
check(src.instance_base("not a url") is None, "junk input is not an instance")
check(src.instance_base("") is None, "empty is not an instance")

check(src.manifest_url(HOST, ID) == MANIFEST, "manifest URL shape")
check(src.api_url(HOST, ID) == f"{HOST}/api/v1/videos/{ID}", "API URL shape")
check(src.instance_watch_url(HOST, ID) == f"{HOST}/watch?v={ID}",
      "normalised instance watch URL")
check(src.host_of(HOST) == "yt.chocolatemoo53.com", "host_of strips the scheme")


# ============================= 4. routes ==================================
src.clear_route_cache()
r = src.routes(YT)
check(len(r) == 1 and r[0].kind == "youtube" and r[0].url == YT,
      "a YouTube link stays exactly one route, unchanged")
r = src.routes(ID)
check(len(r) == 1 and r[0].kind == "youtube" and r[0].url == YT,
      "a bare id becomes the canonical watch URL")
rs = src.routes(INST)
check([x.kind for x in rs] == ["youtube", "invidious"],
      "an instance link gets YouTube first, then the instance")
check(rs[0].url == YT, "primary route: the canonical YouTube URL")
check(rs[0].resolved == YT,
      "if YouTube answers, the item records the canonical URL")
check(rs[1].url == MANIFEST, "fallback route: the instance's DASH manifest")
check(rs[1].resolved == f"{HOST}/watch?v={ID}",
      "the instance route resolves to a clean instance watch URL")
check(rs[1].instance_base == HOST and rs[1].video_id == ID,
      "the instance route carries what the API probe needs")
check("yt.chocolatemoo53.com" in rs[1].label, "the instance route is named by host")
check(all(x.video_id == ID for x in rs), "both routes know the id")

r = src.routes(VIMEO)
check(len(r) == 1 and r[0].kind == "direct" and r[0].url == VIMEO,
      "a site yt-dlp extracts on its own stays a single direct route")

src.remember(ID, "invidious")
check([x.kind for x in src.routes(INST)] == ["invidious", "youtube"],
      "once the instance has served a video, the next attempt starts there")
src.remember(ID, "youtube")
check([x.kind for x in src.routes(INST)] == ["youtube", "invidious"],
      "and a remembered YouTube success restores the original order")
src.clear_route_cache()
check([x.kind for x in src.routes(INST)] == ["youtube", "invidious"],
      "clearing the cache restores the default order")
check(src.remembered(ID) is None, "remembered() is empty after a clear")
src.remember("", "youtube")
check(src.remembered("") is None, "remember() ignores an empty id")
for i in range(src._ROUTE_CACHE_MAX + 10):
    src.remember(f"id{i:011d}", "invidious")
check(len(src._ROUTE_CACHE) <= src._ROUTE_CACHE_MAX,
      f"the route cache stays bounded ({len(src._ROUTE_CACHE)})")
src.clear_route_cache()


# ====================== 5. instance API / fetch_json ======================
_real_fetch = src.fetch_json
GOOD = {"title": TITLE, "videoId": ID}

# What the stubbed instance API answers to a probe (None = "no proof").
API_ANSWER = None
HTTP = []          # every API probe the code attempted


def api(title, video_id=ID):
    """Script the instance API: a title, or None for 'the instance refused'."""
    global API_ANSWER
    if title is None:
        API_ANSWER = None
    else:
        API_ANSWER = {"videoId": video_id}
        if title:
            API_ANSWER["title"] = title


def _api_stub(url, timeout=10.0):
    HTTP.append(url)
    return API_ANSWER

src.fetch_json = lambda url, timeout=10.0: GOOD
check(src.instance_title(HOST, ID) == TITLE, "instance_title reads the API title")
src.fetch_json = lambda url, timeout=10.0: {**GOOD, "error": "Video not available"}
check(src.instance_title(HOST, ID) is None, "an API error means no proof")
src.fetch_json = lambda url, timeout=10.0: {**GOOD, "videoId": "aaaaaaaaaaa"}
check(src.instance_title(HOST, ID) is None,
      "an API answer about some other video is rejected")
src.fetch_json = lambda url, timeout=10.0: {"title": "   ", "videoId": ID}
check(src.instance_title(HOST, ID) is None, "a blank title is no proof")
src.fetch_json = lambda url, timeout=10.0: {"videoId": ID}
check(src.instance_title(HOST, ID) is None, "a title-less answer is no proof")
src.fetch_json = lambda url, timeout=10.0: None
check(src.instance_title(HOST, ID) is None, "an unreachable API is no proof")
src.fetch_json = lambda url, timeout=10.0: ["not", "a", "dict"]
check(src.instance_title(HOST, ID) is None, "a non-dict API answer is ignored")
src.fetch_json = _real_fetch


class Resp:
    def __init__(self, body, status=200):
        self._body, self.status = body, status

    def read(self, n=-1):
        return self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_real_urlopen = src.urllib.request.urlopen
try:
    src.urllib.request.urlopen = lambda req, timeout=None: Resp(
        b'{"title": "ok", "videoId": "x"}')
    out = src.fetch_json("https://nowhere.test/api")
    check(isinstance(out, dict) and out["title"] == "ok", "fetch_json parses JSON")

    src.urllib.request.urlopen = lambda req, timeout=None: Resp(b"not json")
    check(src.fetch_json("https://nowhere.test/api") is None, "non-JSON is tolerated")

    src.urllib.request.urlopen = lambda req, timeout=None: Resp(b"[]")
    check(src.fetch_json("https://nowhere.test/api") is None,
          "a JSON list is not a video dict")

    src.urllib.request.urlopen = lambda req, timeout=None: Resp(b"", 404)
    check(src.fetch_json("https://nowhere.test/api") is None, "404 -> None")

    def raise_for(make):
        def _go(req, timeout=None):
            raise make()
        return _go

    src.urllib.request.urlopen = raise_for(lambda: urllib.error.URLError("nope"))
    check(src.fetch_json("https://nowhere.test/api") is None, "URLError never escapes")
    src.urllib.request.urlopen = raise_for(lambda: OSError("socket died"))
    check(src.fetch_json("https://nowhere.test/api") is None, "OSError never escapes")
    src.urllib.request.urlopen = raise_for(lambda: ValueError("bad"))
    check(src.fetch_json("https://nowhere.test/api") is None, "ValueError never escapes")

    seen = {}

    def spy(req, timeout=None):
        seen["url"] = req.full_url
        seen["ua"] = req.get_header("User-agent") or ""
        seen["accept"] = req.get_header("Accept") or ""
        return Resp(b"{}")

    src.urllib.request.urlopen = spy
    src.fetch_json(f"{HOST}/api/v1/videos/{ID}", timeout=3.0)
    check(seen["url"].endswith(f"/api/v1/videos/{ID}"), "the request URL is the probe")
    check("ytdownloader" in seen["ua"], "the probe identifies itself (instances 403 blanks)")
    check("json" in seen["accept"], "the probe asks for JSON")
finally:
    src.urllib.request.urlopen = _real_urlopen
    src.fetch_json = _real_fetch


# ======================= 6. the filename templates ========================
tmp = tempfile.mkdtemp(prefix="ytd-routes-")
it = w.DownloadItem(YT, "audio", tmp)
check(w.instance_template_for(it) == os.path.join(tmp, "%(title)s [audio].%(ext)s"),
      "with no title known, the instance template is the ordinary one")
it.title = TITLE
check(w.instance_template_for(it) == os.path.join(tmp, f"{TITLE} [audio].%(ext)s"),
      "a known title goes into the filename literally")
it2 = w.DownloadItem(YT, "video", tmp, title="AC/DC – 100% Live\nReloaded")
tpl = w.instance_template_for(it2)
check(tpl.startswith(tmp) and "AC-DC" in tpl and "100%%" in tpl and "\n" not in tpl,
      f"separators, percents and newlines are neutralised: {tpl}")
check(tpl.endswith(" [video].%(ext)s"), "the video suffix is still per-format")
check(w.output_template_for(it2) == os.path.join(tmp, "%(title)s [video].%(ext)s"),
      "output_template_for is untouched by all of this")


# ==================== 7. the worker, with a stubbed yt-dlp ===============
CALLS = []          # argv of every fake yt-dlp run

# url -> (stdout lines, exit code); unknown url -> an "Unsupported URL" failure
RUNS = {}
DEFAULT_RUN = ([f"ERROR: Unsupported URL: ...\n"], 1)
VALID = [f"id:{ID}\n", f"title:{TITLE}\n"]


class FakeProc:
    def __init__(self, args, **kwargs):
        CALLS.append(list(args))
        lines, rc = RUNS.get(args[-1], DEFAULT_RUN)
        self.stdout = iter(lines)
        self._rc, self._killed = rc, False

    def wait(self, timeout=None):
        return self._rc

    def poll(self):
        return 0 if self._killed else None

    def terminate(self):
        self._killed = True


_real_popen = w.subprocess.Popen
_real_run = w.subprocess.run
_real_cmd = w.ytdlp_command
PROBES = []           # every ffprobe/other subprocess.run call


class _NoNetwork(Exception):
    pass


def _block_network(req, timeout=None):
    """Hard stop: a probe that was not stubbed means the test leaked online."""
    raise _NoNetwork(f"TEST REACHED THE NETWORK: {getattr(req, 'full_url', req)}")


# From here on, nothing may touch the network: sources.fetch_json is stubbed
# by every helper below, and urllib itself is wired to blow up loudly.
src.urllib.request.urlopen = _block_network


def fake_run(cmd, **kwargs):
    PROBES.append(list(cmd))
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")


w.subprocess.Popen = FakeProc
w.subprocess.run = fake_run
w.ytdlp_command = lambda: ["/fake/yt-dlp"]


def validate(text, stopped=False):
    """One validation, synchronously. Returns (ok, resolved, message)."""
    CALLS.clear()
    HTTP.clear()
    src.clear_route_cache()
    src.fetch_json = _api_stub
    worker = w.Worker()
    got = []
    worker.validated.connect(lambda *a: got.append(a))
    worker._mode = "validate"
    worker._url = worker._resolve_url(text)
    if stopped:
        worker._stopped = True
    worker._run_validation()
    return got[0] if got else (None, None, None)


def run_dl(item, resume=False, route=None):
    """One download attempt, synchronously. Returns (result, argv, statuses)."""
    CALLS.clear()
    HTTP.clear()
    PROBES.clear()
    src.clear_route_cache()
    src.fetch_json = _api_stub
    if route:
        video_id = src.video_id_of(item.url)
        if video_id:
            src.remember(video_id, route)
    worker = w.Worker()
    done, statuses = [], []
    worker.finished.connect(lambda *a: done.append(a))
    worker.status.connect(statuses.append)
    worker._mode = "download"
    worker._item = item
    worker._resume = resume
    worker._run_download()
    return (done[0] if done else (None, None, None)), list(CALLS), statuses


try:
    # ---------------- validation: plain YouTube is untouched ------------
    RUNS.clear()
    RUNS[YT] = (VALID, 0)
    ok, resolved, msg = validate(YT)
    check(ok is True and msg == TITLE, f"a YouTube link validates as before: {msg!r}")
    check(resolved == YT, "and resolves to the URL it was given")
    check(len(CALLS) == 1, "exactly one yt-dlp run for a plain YouTube link")
    check(not HTTP, "no instance probe for a link that is not an instance")
    check("youtube:player_client=android" in CALLS[0],
          "the YouTube route keeps its player_client tuning")
    check("--no-playlist" in CALLS[0] and "--socket-timeout" in CALLS[0],
          "the validation flags are unchanged")

    ok, resolved, msg = validate(ID)
    check(ok is True and resolved == YT, "a bare id validates through the watch URL")

    RUNS.clear()
    ok, resolved, msg = validate(YT)
    check(ok is False and msg == "invalid URL or video unavailable",
          f"a failing link reports the old generic message: {msg!r}")

    # ---------------- validation: the reported Invidious failure --------
    RUNS.clear()
    RUNS[YT] = (VALID, 0)
    api(TITLE)
    ok, resolved, msg = validate(INST)
    check(ok is True, "THE REPORTED BUG: an Invidious instance link validates")
    check(msg == TITLE, "with the real title")
    check(resolved == YT,
          f"and the item records the canonical YouTube URL: {resolved!r}")
    check(len(CALLS) == 1 and CALLS[0][-1] == YT,
          "the instance was not even asked — YouTube answered")
    check(not HTTP, "no API request when the YouTube route works")

    # ---------------- YouTube blocked, the instance rescues -------------
    RUNS.clear()                       # YouTube now fails
    api(TITLE)
    ok, resolved, msg = validate(INST)
    check(ok is True, "when YouTube refuses, the instance API proves the source")
    check(msg == TITLE, "and supplies its title")
    check(resolved == f"{HOST}/watch?v={ID}",
          f"the item then keeps pointing at the instance: {resolved!r}")
    check(HTTP == [src.api_url(HOST, ID)],
          "the probe was the instance's own /api/v1/videos/ID")
    check(src.remembered(ID) == "invidious", "the working route is remembered")

    # ---------------- API disabled, the manifest still serves -----------
    RUNS.clear()
    RUNS[MANIFEST] = (VALID, 0)
    api(None)
    ok, resolved, msg = validate(INST)
    check(ok is True and resolved == f"{HOST}/watch?v={ID}",
          "an instance with a disabled API is still proved by its manifest")
    check(len(CALLS) == 2 and CALLS[1][-1] == MANIFEST,
          "the manifest probe came after the API failed")

    # ---------------- both routes dead ----------------------------------
    RUNS.clear()
    api(None)
    ok, resolved, msg = validate(INST)
    check(ok is False, "a dead YouTube and a dead instance is an invalid link")
    check("chocolatemoo53" in msg and "invalid URL" in msg,
          f"the error names both routes: {msg!r}")
    check(resolved == INST, "a failed validation echoes back the text given")

    # ---------------- duplicates in the message collapse ----------------
    RUNS.clear()
    api(None)
    ok, resolved, msg = validate(f"{HOST}/watch?v={ID}")
    check(msg.count("invalid URL") == 1,
          f"identical complaints are not repeated: {msg!r}")
    check("chocolatemoo53" in msg, "and the instance's own refusal is still said")

    # ---------------- cancellation ---------------------------------------
    RUNS.clear()
    RUNS[YT] = (VALID, 0)
    ok, resolved, msg = validate(INST, stopped=True)
    check((ok, msg) == (False, "cancelled"), "a stopped validation says cancelled")
    check(resolved == INST, "and resolves to the text it was given")
    check(len(CALLS) == 1, "it does not go probing extra routes after the stop")

    # ---------------- no yt-dlp at all ----------------------------------
    w.ytdlp_command = lambda: []
    ok, resolved, msg = validate(INST)
    check(ok is False and msg == w.YTDLP_MISSING,
          "missing yt-dlp is reported, not hidden behind a route switch")
    check(not CALLS, "and nothing was launched")
    w.ytdlp_command = _real_cmd

    # ---------------- a direct (non-YouTube) link ----------------------
    RUNS.clear()
    RUNS[VIMEO] = ([f"id:76979871\n", "title:Some Vimeo\n"], 0)
    ok, resolved, msg = validate(VIMEO)
    check(ok is True and resolved == VIMEO and len(CALLS) == 1,
          "an unrelated site yt-dlp knows is untouched by the new routing")

    # ==================== download routes ===============================
    audio_dest = Path(tmp) / "x [audio].webm"
    audio_dest.write_bytes(b"data")
    PROGRESS = [f"[download] Destination: {audio_dest}\n",
                "[download] 100.0% of 1.00MiB at 1.00MiB/s ETA 0:00:01\n"]

    # plain YouTube: the status text must not change at all
    RUNS.clear()
    RUNS[YT] = (PROGRESS, 0)
    (ok, msg, dest), calls, statuses = run_dl(
        w.DownloadItem(YT, "audio", tmp, title=TITLE))
    check(ok is True and msg == "download complete",
          f"a plain YouTube download still succeeds: {msg}")
    check("Starting audio download ..." in statuses,
          "single-route status text is unchanged")
    check(len(calls) == 1 and calls[0][-1] == YT, "one run, one URL")

    # instance link, YouTube serves it
    RUNS.clear()
    RUNS[YT] = (PROGRESS, 0)
    (ok, msg, dest), calls, statuses = run_dl(
        w.DownloadItem(INST, "audio", tmp, title=TITLE))
    check(ok is True, "an Invidious link downloads (through YouTube)")
    check(len(calls) == 1 and calls[0][-1] == YT,
          "the instance manifest was not needed")
    check(any("Starting audio download from YouTube ..." in s for s in statuses),
          f"with a choice of routes the status says which: {statuses[:1]}")
    check(calls[0][-2] == os.path.join(tmp, "%(title)s [audio].%(ext)s"),
          "the YouTube route still lets yt-dlp name the file")
    check(src.remembered(ID) == "youtube", "the successful route is remembered")

    # instance link, YouTube fails, the manifest finishes it
    RUNS.clear()
    RUNS[MANIFEST] = (PROGRESS, 0)
    api(TITLE)
    item = w.DownloadItem(INST, "audio", tmp)      # title unknown on purpose
    (ok, msg, dest), calls, statuses = run_dl(item)
    check(ok is True, f"the instance route completes the download: {msg}")
    check(len(calls) == 2 and calls[0][-1] == YT and calls[1][-1] == MANIFEST,
          "YouTube was tried first, then the instance manifest")
    check(any("did not work — trying the instance" in s for s in statuses),
          "the switch is announced in the status line")
    check(calls[1][-2] == os.path.join(tmp, f"{TITLE} [audio].%(ext)s"),
          f"the instance run names the file after the real title: {calls[1][-2]}")
    check(item.title == TITLE, "and the row title came from the instance API")
    check(src.remembered(ID) == "invidious", "the route that worked is remembered")

    # the remembered route is used first on the next attempt
    RUNS.clear()
    RUNS[MANIFEST] = (PROGRESS, 0)
    RUNS[YT] = (PROGRESS, 1)             # YouTube still broken
    api(TITLE)
    (ok, msg, dest), calls, statuses = run_dl(
        w.DownloadItem(INST, "audio", tmp, title=TITLE), route="invidious")
    check(len(calls) == 1 and calls[0][-1] == MANIFEST,
          "a retry starts on the route that worked, not on the one that failed")
    check(ok is True, "and it succeeds without touching the dead route")

    # both routes fail -> the message names both
    RUNS.clear()
    api(None)
    (ok, msg, dest), calls, statuses = run_dl(
        w.DownloadItem(INST, "audio", tmp, title=TITLE))
    check(ok is False, "a link unavailable everywhere fails")
    check("YouTube" in msg and "chocolatemoo53" in msg,
          f"the failure names every route tried: {msg!r}")
    check(len(calls) == 2, "and both routes were actually attempted")

    # video kind on the instance route keeps the merge options
    RUNS.clear()
    mkv = Path(tmp) / "v [video].mkv"
    mkv.write_bytes(b"data")
    RUNS[MANIFEST] = ([f"[download] Destination: {mkv}\n"], 0)
    api(TITLE)
    (ok, msg, dest), calls, statuses = run_dl(
        w.DownloadItem(INST, "video", tmp, title=TITLE), route="invidious")
    check(ok is True, "the video route downloads through the instance too")
    check("bestvideo+bestaudio/best" in calls[0]
          and "--merge-output-format" in calls[0] and "mkv" in calls[0],
          "the video options are identical on both routes")
    check("-x" not in calls[0] and "--audio-format" not in calls[0],
          "video mode carries no audio-extraction flags")
    check(any(p and p[0] == "ffprobe" for p in PROBES),
          "a video download is still verified with ffprobe")

    # resume reaches every route
    RUNS.clear()
    RUNS[YT] = (PROGRESS, 0)
    (ok, msg, dest), calls, statuses = run_dl(
        w.DownloadItem(INST, "audio", tmp, title=TITLE), resume=True)
    check("-c" in calls[0], "the resume flag still reaches yt-dlp")

    # verification failure is still a failure (destination missing)
    RUNS.clear()
    RUNS[YT] = (["[download] 100.0% of 1.00MiB at 1.00MiB/s ETA 0:00:01\n",
                 "[download] Destination: /no/such/dir/gone [audio].webm\n"], 0)
    (ok, msg, dest), calls, statuses = run_dl(
        w.DownloadItem(YT, "audio", tmp, title=TITLE))
    check(ok is False and "verification failed" in msg,
          f"a run that names no real file is still caught: {msg!r}")

    # a launch failure does not burn the second route
    def boom(args, **kw):
        CALLS.append(list(args))
        raise FileNotFoundError("gone")

    w.subprocess.Popen = boom
    (ok, msg, dest), calls, statuses = run_dl(
        w.DownloadItem(INST, "audio", tmp, title=TITLE))
    check((ok, msg) == (False, w.YTDLP_MISSING),
          "a missing yt-dlp is reported instead of switching routes")
    check(len(calls) == 1, "and it does not try the instance route either")

    # cancelled download: one route, reported as cancelled
    w.subprocess.Popen = FakeProc
    RUNS.clear()
    RUNS[YT] = (PROGRESS, 0)
    src.clear_route_cache()
    CALLS.clear()
    worker = w.Worker()
    done = []
    worker.finished.connect(lambda *a: done.append(a))
    worker._mode = "download"
    worker._item = w.DownloadItem(INST, "audio", tmp, title=TITLE)
    worker._stopped = True
    worker._run_download()
    check(done and done[0][0] is False and done[0][1] == "cancelled",
          "a cancelled download is reported as cancelled")
    check(len(CALLS) == 1, "cancellation stops the route walk immediately")
finally:
    src.fetch_json = _real_fetch
    src.urllib.request.urlopen = _real_urlopen
    w.subprocess.Popen = _real_popen
    w.subprocess.run = _real_run
    w.ytdlp_command = _real_cmd

print()
print(f"checks: {len(PASS) + len(FAIL)}   passed: {len(PASS)}   failed: {len(FAIL)}")
print("RESULT: ALL PASS" if not FAIL else f"RESULT: {len(FAIL)} FAILURES")
sys.exit(0 if not FAIL else 1)
