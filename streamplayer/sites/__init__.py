"""The streaming sites this player knows how to drive.

Add one by dropping a module in this package with a StreamSite subclass in it.
That is the whole job: the package is scanned at import, so there is no registry
to edit and nothing shared to touch. Matching against a URL is automatic.

That matters beyond convenience. Contributors are limited to adding files here
(see CONTRIBUTING in the README), so a hand-maintained list would have forced
every new site through this file - the one place the dispatch logic lives.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from urllib.parse import urlparse

from .. import firefox
from .base import StreamSite, normalize_url

log = logging.getLogger("stream")


def _discover() -> tuple[type[StreamSite], ...]:
    """Every concrete StreamSite in this package, in a stable order.

    A module that fails to import is logged and skipped rather than taken as
    fatal: one contributor's broken site should not stop the TV playing
    everything else.
    """
    found: dict[str, type[StreamSite]] = {}
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_") or info.name == "base":
            continue
        try:
            module = importlib.import_module(f"{__name__}.{info.name}")
        except Exception as exc:  # noqa: BLE001 - one bad file must not be fatal
            log.error("site module %r could not be imported, skipping it: %s", info.name, exc)
            continue
        for _label, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, StreamSite)
                and obj is not StreamSite
                and not inspect.isabstract(obj)
                and obj.__module__ == module.__name__  # not an import of someone else's class
            ):
                if not obj.name or not obj.domains:
                    log.error("site class %s sets no name/domains, skipping it", obj.__qualname__)
                    continue
                if obj.name in found and found[obj.name] is not obj:
                    log.error("two site classes both call themselves %r, keeping the first", obj.name)
                    continue
                found[obj.name] = obj
    return tuple(found[name] for name in sorted(found))


SITES: tuple[type[StreamSite], ...] = _discover()

__all__ = ["SITES", "StreamSite", "UnknownSite", "for_url", "by_name", "describe"]


class UnknownSite(RuntimeError):
    """No site class claims this URL."""


def for_url(url: str, cfg: firefox.Config) -> StreamSite:
    """The site handler for `url`, ready to launch."""
    for site in SITES:
        if site.handles(url):
            return site(url, cfg)
    host = urlparse(normalize_url(url)).hostname or url

    # A site can own the host and still turn the page down - only part of a
    # domain may hold a player. Saying "nothing handles example.com" while
    # listing the site that covers it as supported would send someone hunting for the wrong problem.
    owners = [s for s in SITES if s.owns_host(url)]
    if owners:
        raise UnknownSite(
            f"{', '.join(s.name for s in owners)} covers {host}, but not this page.\n"
            f"It takes {'; '.join(s.accepts() for s in owners)}.\n"
            "Pass --site NAME to open it with one anyway."
        )

    raise UnknownSite(
        f"no site class handles {host}. Supported: {', '.join(s.name for s in SITES)}.\n"
        "Pass --site NAME to open it with one of them anyway, or add a class in "
        "streamplayer/sites/."
    )


def by_name(name: str, url: str, cfg: firefox.Config) -> StreamSite:
    """The site handler called `name`, whatever the URL looks like."""
    for site in SITES:
        if site.name == name:
            return site(url, cfg)
    raise UnknownSite(f"unknown site {name!r}. Known: {', '.join(s.name for s in SITES)}.")


def describe() -> str:
    """One line per known site, for --list-sites."""
    return "\n".join(f"  {s.name:<14} {s.accepts()}" for s in SITES)
