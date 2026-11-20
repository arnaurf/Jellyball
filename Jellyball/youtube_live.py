"""YouTube live channels: a channel whose one source is a YouTube live stream.

The channel stores the YouTube page URL (as its query, category "youtube").
Instead of searching the providers, a scrape asks yt-dlp for the stream's
current HLS playlist; that playlist URL is signed and expires after a few
hours, so every rescrape (and every failover once it stops answering)
resolves it again. The HLS stream then plays through the normal channel
session like any provider stream, so it can be watched on its own or used as
a Multi-View member (e.g. the corner window of a picture-in-picture view).

Only YouTube URLs are accepted: yt-dlp understands many sites and local
paths, and the dashboard must not become a way to make the server fetch
arbitrary addresses.
"""

import asyncio
import os
import re
import time
import urllib.parse
from typing import Dict, List, Optional

from config import LOGGER
from network_safety import bounded_float, validate_http_url

YOUTUBE_CATEGORY = "youtube"
YOUTUBE_GROUP_TITLE = "YouTube"
YOUTUBE_PROVIDER = "YouTube"

# YouTube serves a live stream as an HLS master playlist whose video variants
# point at separate audio-only renditions (EXT-X-MEDIA TYPE=AUDIO); there are
# no single formats with both any more. The session plays such a master
# itself (it remuxes the chosen variant with its audio track), so the master
# playlist is what a channel gets. This selector only has to match something
# so yt-dlp hands back the formats that carry the master's URL.
YOUTUBE_FORMAT = "bv*[protocol^=m3u8]+ba[protocol^=m3u8]/b[protocol^=m3u8]"
YOUTUBE_RESOLVE_TIMEOUT = bounded_float(os.getenv("YOUTUBE_RESOLVE_TIMEOUT", "45"), 45.0, 5.0, 300.0)
# One warning per URL per this many seconds while it keeps failing (an
# off-air channel is resolved again on every rescrape).
_WARN_COOLDOWN_SECONDS = 900.0
_LAST_WARNED: Dict[str, float] = {}

_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}
_VIDEO_ID = r"[A-Za-z0-9_-]{11}"
_NAME = r"[A-Za-z0-9_.%-]{1,100}"


def normalize_youtube_url(value: str) -> Optional[str]:
    """The canonical https URL of a YouTube live stream or a channel's live
    page, or None for anything else. Accepted: youtu.be/<id>,
    youtube.com/watch?v=<id>, /live/<id>, and a channel's /@handle,
    /channel/<id>, /c/<name> or /user/<name> (with or without /live; a
    channel URL always means its current live stream)."""
    text = str(value or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or parts.port not in (None, 80, 443) or parts.username:
        return None
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/")
    if host == "youtu.be":
        match = re.fullmatch(rf"/({_VIDEO_ID})", path)
        return f"https://www.youtube.com/watch?v={match.group(1)}" if match else None
    if host not in _HOSTS:
        return None
    if path == "/watch":
        video_id = (urllib.parse.parse_qs(parts.query).get("v") or [""])[0]
        return f"https://www.youtube.com/watch?v={video_id}" if re.fullmatch(_VIDEO_ID, video_id) else None
    match = re.fullmatch(rf"/live/({_VIDEO_ID})", path)
    if match:
        return f"https://www.youtube.com/watch?v={match.group(1)}"
    match = re.fullmatch(rf"/(@{_NAME}|channel/{_NAME}|c/{_NAME}|user/{_NAME})(?:/live|/streams)?", path)
    if match:
        return f"https://www.youtube.com/{match.group(1)}/live"
    return None


def is_youtube_channel(data: dict) -> bool:
    return str(data.get("category") or "") == YOUTUBE_CATEGORY


def _warn(url: str, message: str, *args) -> None:
    now = time.monotonic()
    if now - _LAST_WARNED.get(url, -_WARN_COOLDOWN_SECONDS) >= _WARN_COOLDOWN_SECONDS:
        _LAST_WARNED[url] = now
        LOGGER.warning(message, *args)
    else:
        LOGGER.debug(message, *args)


class _QuietLogger:
    """yt-dlp's own messages go to our debug log instead of the console."""

    def debug(self, msg: str) -> None:
        LOGGER.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        LOGGER.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        LOGGER.debug("yt-dlp warning: %s", msg)

    def error(self, msg: str) -> None:
        LOGGER.debug("yt-dlp error: %s", msg)


def _extract_sync(url: str) -> dict:
    import yt_dlp  # late: an optional dependency for everything but this channel kind

    options = {
        "format": YOUTUBE_FORMAT,
        "noplaylist": True,
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "logger": _QuietLogger(),
        "socket_timeout": 15,
    }
    with yt_dlp.YoutubeDL(options) as ydl:  # type: ignore[arg-type]
        return ydl.extract_info(url, download=False) or {}


def _hls_url(info: dict) -> str:
    """The live stream's HLS master playlist (every quality, with its audio
    renditions), else a single HLS format that carries both video and audio."""
    for fmt in (*(info.get("requested_formats") or ()), info, *(info.get("formats") or ())):
        if isinstance(fmt, dict) and str(fmt.get("protocol") or "").startswith("m3u8") and fmt.get("manifest_url"):
            return str(fmt["manifest_url"])
    if str(info.get("protocol") or "").startswith("m3u8") and info.get("acodec") != "none":
        return str(info.get("url") or "")
    return ""


async def resolve_live_stream(page_url: str, channel_name: str = "") -> List[dict]:
    """The stream candidate for a YouTube live URL ([] when it is not live,
    yt-dlp is missing, or YouTube refused)."""
    url = normalize_youtube_url(page_url)
    if not url:
        _warn(page_url, "YouTube channel %s has an unsupported URL", channel_name or page_url)
        return []
    try:
        info = await asyncio.wait_for(asyncio.to_thread(_extract_sync, url), timeout=YOUTUBE_RESOLVE_TIMEOUT)
    except ImportError:
        _warn(url, "YouTube channel %s needs yt-dlp: pip install -r requirements.txt", channel_name or url)
        return []
    except asyncio.TimeoutError:
        _warn(url, "YouTube channel %s: resolving %s timed out", channel_name or url, url)
        return []
    except Exception as exc:  # noqa: BLE001 - yt-dlp raises many types; none may break a scrape
        # yt-dlp's message says why (not live yet, private, needs sign-in, ...).
        _warn(url, "YouTube channel %s: %s", channel_name or url, str(exc).replace("ERROR: ", "")[:300])
        return []
    if not info.get("is_live"):
        _warn(
            url, "YouTube channel %s is not live (status=%s); it is checked again on the next rescrape",
            channel_name or url, info.get("live_status") or "unknown",
        )
        return []
    stream_url = validate_http_url(_hls_url(info))
    if not stream_url:
        _warn(url, "YouTube channel %s: yt-dlp returned no usable HLS stream", channel_name or url)
        return []
    _LAST_WARNED.pop(url, None)
    LOGGER.info("YouTube channel %s resolved to its live HLS stream", channel_name or url)
    return [{
        "url": stream_url,
        "referer": "https://www.youtube.com/",
        "origin": "https://www.youtube.com",
        "provider": YOUTUBE_PROVIDER,
        "match_score": 100,
        "match_title": str(info.get("title") or channel_name or "YouTube live"),
        "match_url": url,
        "discovery_method": "yt-dlp",
    }]
