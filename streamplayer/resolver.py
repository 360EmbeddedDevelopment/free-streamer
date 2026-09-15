"""Turn a page URL into a directly playable media URL via yt-dlp.

Live stream URLs carry short-lived tokens, so resolution happens fresh on every
launch and on every retry rather than being cached.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from urllib.parse import urlparse

MEDIA_SUFFIXES = (".m3u8", ".mpd", ".mp4", ".mkv", ".ts", ".webm", ".mov", ".flv")
MEDIA_SCHEMES = ("rtsp", "rtmp", "rtmps", "udp", "srt", "file")

RESOLVE_TIMEOUT = 90


class UnsupportedSource(RuntimeError):
    """yt-dlp could not extract a playable stream from this URL."""


@dataclass
class Resolved:
    """A playable video URL, plus a separate audio URL when the source splits them."""

    video: str
    audio: str | None = None
    direct: bool = False  # True when the input was already a media URL


def is_media_url(url: str) -> bool:
    """True when the URL points straight at media and needs no extraction."""
    parsed = urlparse(url)
    if parsed.scheme in MEDIA_SCHEMES:
        return True
    return parsed.path.lower().endswith(MEDIA_SUFFIXES)


def format_selector(max_height: int | None) -> str:
    """A yt-dlp format string capped at max_height, preferring separate streams."""
    if max_height is None:
        return "bestvideo+bestaudio/best"
    cap = f"[height<=?{max_height}]"
    return f"bestvideo{cap}+bestaudio/best{cap}/best"


def resolve(url: str, max_height: int | None = 1080, timeout: int = RESOLVE_TIMEOUT) -> Resolved:
    """Resolve url to something mpv can play.

    Direct media URLs pass through untouched. Everything else goes to yt-dlp,
    which prints one URL per selected stream - video first, then audio when the
    source serves them separately.
    """
    if is_media_url(url):
        return Resolved(video=url, direct=True)

    cmd = [
        "yt-dlp",
        "--quiet",
        "--no-warnings",
        "--get-url",
        "--format",
        format_selector(max_height),
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise UnsupportedSource(
            "yt-dlp is not installed, so only direct media URLs "
            "(.m3u8/.mpd/.mp4/rtsp://) can be played."
        ) from None
    except subprocess.TimeoutExpired:
        raise UnsupportedSource(f"yt-dlp timed out after {timeout}s resolving {url}") from None

    urls = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if proc.returncode != 0 or not urls:
        detail = (proc.stderr or proc.stdout).strip() or f"exit code {proc.returncode}"
        raise UnsupportedSource(
            f"yt-dlp could not extract a stream from {url}\n{detail}\n\n"
            "If this is a service that needs a login, try --player chromium instead."
        )

    if len(urls) >= 2:
        return Resolved(video=urls[0], audio=urls[1])
    return Resolved(video=urls[0])
