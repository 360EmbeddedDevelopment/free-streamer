"""URLs nobody can drive yet, parked for later.

The direct-link bar accepts any link, but streamplayer can only drive a site it
has a class for (see streamplayer/sites/). When a link's domain is unclaimed
there is nothing useful to do with it *now* - so it is written here instead of
thrown away, and shows up in the Advanced tab as a to-do list for which site to
support next.

One line per URL, tab-separated, so the file stays readable in an editor and
parses in one split. Repeats are ignored rather than appended, which keeps a
stubbornly re-pasted link from burying everything else; the timestamp is
therefore when it was *first* seen.
"""

from __future__ import annotations

import os
from datetime import datetime
from urllib.parse import urlparse

from streamplayer.sites.base import normalize_url

from .catalog import REPO

PATH = os.path.join(REPO, "unknown-domains.txt")


def host_of(url: str) -> str:
    return (urlparse(normalize_url(url)).hostname or "").lower()


def entries(path: str = PATH) -> list[dict]:
    """Everything parked so far, oldest first. Missing file means nothing yet."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        return []
    except OSError:
        # An unreadable queue should not take the settings page down with it.
        return []

    out: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        parts = line.split("\t")
        # Tolerate a hand-edited file: anything short is still shown, just bare.
        seen = parts[0] if len(parts) > 2 else ""
        host = parts[1] if len(parts) > 2 else ""
        url = parts[-1]
        out.append({"seen": seen, "host": host or host_of(url), "url": url})
    return out


def record(url: str, path: str = PATH) -> bool:
    """Park `url`. True if it was new, False if it was already on the list."""
    url = url.strip()
    if not url:
        return False
    if any(e["url"] == url for e in entries(path)):
        return False

    line = f"{datetime.now().isoformat(timespec='seconds')}\t{host_of(url)}\t{url}\n"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line)
    return True


def clear(path: str = PATH) -> int:
    """Empty the list. Returns how many entries went."""
    count = len(entries(path))
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    return count
