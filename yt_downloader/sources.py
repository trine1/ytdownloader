"""Recognising the same video across YouTube, Invidious and friends.

yt-dlp understands youtube.com and youtu.be. It has **no Invidious
extractor**, so a link to any of the hundreds of public Invidious (or Piped)
instances — ``https://<instance>/watch?v=ID`` — falls through to the generic
extractor and dies with ``Unsupported URL``, even though the video is exactly
the one YouTube serves under the same 11-character id.

This module turns "some text naming a video" into an ordered list of *routes*
yt-dlp can actually use:

``youtube``
    The canonical watch URL. Preferred: yt-dlp's own extractor knows the
    format catalogue, titles, durations and quirks of YouTube.

``invidious``
    The instance the user linked: its DASH manifest for the media
    (``/api/manifest/dash/id/ID``, which the generic extractor reads happily)
    and its JSON API (``/api/v1/videos/ID``) for the title. Used when YouTube
    itself refuses — a geo-block, an age gate, a bot check, an IP YouTube
    dislikes — which is precisely why people reach for an instance.

Recognition is by URL **shape**, never by a list of hosts, so every instance
works automatically, including ones that do not exist yet. Which route
succeeded last for a given video id is remembered for the lifetime of the
process, so a blocked YouTube is not re-probed on every retry.

Nothing here raises: a probe that cannot reach its host returns ``None`` and
the caller falls back to the other route.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

# A YouTube video id is exactly 11 characters of the base64-ish alphabet.
BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

_ID = r"[A-Za-z0-9_-]{11}"

# Every URL shape that carries a video id. None of them is anchored to a
# host: that is the point — an Invidious instance uses YouTube's own shapes.
VIDEO_ID_PATTERNS = tuple(
    re.compile(pattern) for pattern in (
        rf"[?&]v=({_ID})(?:&|\?|#|$)",
        rf"youtu\.be/({_ID})(?:[/?#]|$)",
        rf"/(?:shorts|embed|live|v|watch)/({_ID})(?:[/?#]|$)",
    )
)

# The path segments that make a foreign host look like a video front-end
# rather than a random site that happens to have a "v" query parameter.
_VIDEO_PATH_RE = re.compile(r"/(?:watch|shorts|embed|live|v)(?:/|\.|$)")

# Hosts that *are* YouTube (or its CDN) — never treated as an instance.
_YOUTUBE_HOSTS = frozenset({
    "youtube.com", "youtu.be", "youtube-nocookie.com",
    "googlevideo.com", "ytimg.com", "ggpht.com",
})
_YOUTUBE_SUFFIXES = (".youtube.com", ".youtube-nocookie.com", ".ytimg.com")

# Some instances are hosted under a subpath (https://host/invidious/watch?...).
# JSON probes are small; this only guards against a host answering with a
# whole web page instead.
_MAX_JSON_BYTES = 4_000_000

_USER_AGENT = "Mozilla/5.0 (compatible; ytdownloader)"

# video id -> the route kind that worked for it last (bounded, FIFO)
_ROUTE_CACHE: Dict[str, str] = {}
_ROUTE_CACHE_MAX = 512


@dataclass(frozen=True)
class Route:
    """One way of fetching a given video."""

    kind: str             # "youtube" | "invidious" | "direct"
    url: str              # the URL handed to yt-dlp
    resolved: str         # the URL the app stores as the item's source
    video_id: str = ""
    instance_base: str = ""   # scheme://host[/subpath] of the instance
    label: str = "the source"  # human phrase for status lines and errors

    @property
    def is_youtube(self) -> bool:
        return self.kind == "youtube"


def video_id_of(text: str) -> Optional[str]:
    """The 11-character id behind any known URL shape, or a bare id."""
    text = (text or "").strip()
    if not text:
        return None
    if BARE_ID_RE.match(text):
        return text
    for res in VIDEO_ID_PATTERNS:
        m = res.search(text)
        if m:
            return m.group(1)
    return None


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def is_youtube_host(url: str) -> bool:
    """True for youtube.com and its own subdomains/CDN hosts."""
    host = (urlsplit(url or "").hostname or "").lower().rstrip(".")
    if not host:
        return False
    return host in _YOUTUBE_HOSTS or host.endswith(_YOUTUBE_SUFFIXES)


def instance_base(url: str) -> Optional[str]:
    """``scheme://host[/subpath]`` when *url* names a video on a foreign host.

    That is how an Invidious (or Piped, or any future) instance is recognised:
    a YouTube-shaped video URL whose host is not YouTube. Returns None for
    bare ids, YouTube's own links and anything without a video path shape, so
    unrelated sites keep their existing behaviour.
    """
    text = (url or "").strip()
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    if is_youtube_host(text) or not video_id_of(text):
        return None
    m = _VIDEO_PATH_RE.search(parts.path)
    if m is None:
        return None
    prefix = parts.path[:m.start()]        # keep any subpath the instance
    return urlunsplit((parts.scheme, parts.netloc, prefix, "", ""))


def manifest_url(base: str, video_id: str) -> str:
    """The instance's DASH manifest — yt-dlp reads it through ``generic``."""
    return f"{base}/api/manifest/dash/id/{video_id}"


def api_url(base: str, video_id: str) -> str:
    return f"{base}/api/v1/videos/{video_id}"


def instance_watch_url(base: str, video_id: str) -> str:
    return f"{base}/watch?v={video_id}"


def host_of(base: str) -> str:
    """Just the hostname, for status lines ("… on the instance (host)")."""
    return urlsplit(base).netloc or base


def fetch_json(url: str, timeout: float = 10.0) -> Optional[dict]:
    """GET *url* expecting JSON; None on any failure. Never raises."""
    request = urllib.request.Request(
        url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200) or 200
            if status >= 400:
                return None
            raw = response.read(_MAX_JSON_BYTES)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def instance_title(base: str, video_id: str,
                   timeout: float = 10.0) -> Optional[str]:
    """Title of *video_id* according to the instance, or None.

    The instance's HTML page is often behind a bot challenge while its API is
    not, so this is the cheap and reliable way to prove a source when
    YouTube's own endpoint refuses us.
    """
    info = fetch_json(api_url(base, video_id), timeout=timeout)
    if not isinstance(info, dict) or info.get("error"):
        return None
    title = str(info.get("title") or "").strip()
    if not title:
        return None
    listed = str(info.get("videoId") or "").strip()
    if listed and listed != video_id:
        return None                 # not the video we asked for
    return title


def routes(text: str) -> List[Route]:
    """Ordered candidate routes for the user's *text*.

    YouTube first, unless this process already knows the instance is the one
    that works for this id. A URL yt-dlp can handle directly (a non-YouTube
    site that has its own extractor, e.g. vimeo) stays a single ``direct``
    route, exactly as before.
    """
    raw = (text or "").strip()
    video_id = video_id_of(raw)
    if video_id is None:
        return [Route("direct", raw, raw, label="the source")]

    base = instance_base(raw)
    youtube_url = raw if is_youtube_host(raw) else watch_url(video_id)
    youtube = Route("youtube", youtube_url, youtube_url, video_id,
                    "", "YouTube")
    if base is None:
        return [youtube]

    instance = Route(
        "invidious",
        manifest_url(base, video_id),
        instance_watch_url(base, video_id),
        video_id,
        base,
        f"the instance ({host_of(base)})",
    )
    if _ROUTE_CACHE.get(video_id) == "invidious":
        return [instance, youtube]
    return [youtube, instance]


def remember(video_id: str, kind: str) -> None:
    """Record which route served *video_id* (so retries start there)."""
    if not video_id:
        return
    if kind not in _ROUTE_CACHE and len(_ROUTE_CACHE) >= _ROUTE_CACHE_MAX:
        _ROUTE_CACHE.pop(next(iter(_ROUTE_CACHE)))
    _ROUTE_CACHE[video_id] = kind


def remembered(video_id: str) -> Optional[str]:
    return _ROUTE_CACHE.get(video_id)


def clear_route_cache() -> None:
    """Forget remembered routes (tests, or a manual "Check again")."""
    _ROUTE_CACHE.clear()
