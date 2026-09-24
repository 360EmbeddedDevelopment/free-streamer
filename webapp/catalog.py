"""streams.json - the list of streams the web page puts buttons on.

The file is read fresh on every request, so nothing here caches: an edit from
the settings page and an edit in vim over SSH are equally live. A typo in the
JSON must not take the page down with it, so load() returns whatever it could
parse plus an error string for the page to show, rather than raising.

Each entry is a stream page URL and a label for its button; everything else maps
onto a flag stream.py already has, and anything left out is inherited from the
defaults in config.json.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from urllib.parse import urlparse

from streamplayer import sites

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(REPO, "streams.json")

# Keys an entry may carry. Anything else is surfaced as a warning: a typo like
# "ulr" would otherwise silently do nothing.
KNOWN_KEYS = {
    "id",
    "label",
    "url",
    "urls",
    "group",
    "note",
    "site",
    "autoplay",
    "retries",
    "firefox_args",
}

# The same idea for a quick link, which carries far less: somewhere to go, what
# to call it, and a line about what is there.
LINK_KEYS = {"url", "label", "note"}


@dataclass(frozen=True)
class Stream:
    """One button on the page, and the stream.py invocation behind it."""

    id: str
    label: str
    # Every link this button can play, in order, never empty. One button holds
    # several because these sources die constantly: the next tap tries the next
    # link rather than making someone edit the file.
    urls: tuple[str, ...]
    group: str = ""
    note: str = ""
    site: str | None = None
    # None means "inherit the Advanced tab's default" for both of these, which is
    # why they are tri-state rather than plain values.
    autoplay: bool | None = None
    retries: int | None = None
    firefox_args: tuple[str, ...] = ()
    # Which site class drives each link, None where nothing claims it. Parallel
    # to urls, because one button's links may belong to different sites.
    handlers: tuple[str | None, ...] = ()

    @property
    def url(self) -> str:
        """The first link. What "the stream's URL" means outside a cycle."""
        return self.urls[0]

    @property
    def handler(self) -> str | None:
        return self.handlers[0] if self.handlers else None

    @property
    def supported(self) -> bool:
        """True when at least one link has a site class to drive it."""
        return any(self.handlers)

    @property
    def unsupported_links(self) -> int:
        """How many links nothing claims. A button can be part-way supported."""
        return sum(1 for one in self.handlers if not one)

    def url_at(self, index: int) -> str:
        """The link at `index`, wrapping round.

        The wrap is what keeps a one-link button behaving exactly as it always
        has, and keeps an index in range after someone shortens the list.
        """
        return self.urls[index % len(self.urls)]

    def handler_at(self, index: int) -> str | None:
        return self.handlers[index % len(self.handlers)] if self.handlers else None

    def resolved(self, defaults: dict | None = None) -> dict:
        """This entry's settings with the global defaults filled in."""
        defaults = defaults or {}
        autoplay = self.autoplay
        if autoplay is None:
            autoplay = bool(defaults.get("autoplay", True))
        retries = self.retries
        if retries is None:
            retries = defaults.get("retries")
        return {
            "autoplay": autoplay,
            "retries": retries,
            "backend": defaults.get("backend") or "",
            "profile": defaults.get("profile") or "",
            "firefox_args": list(self.firefox_args) or list(defaults.get("firefox_args") or []),
        }

    def flags(self, defaults: dict | None = None, index: int = 0) -> list[str]:
        """The stream.py arguments for this entry, URL last.

        `index` picks which of the button's links to play; `site`, like every
        other setting here, belongs to the whole button rather than one link.
        """
        settings = self.resolved(defaults)
        args: list[str] = []
        if self.site:
            args += ["--site", self.site]
        if not settings["autoplay"]:
            args.append("--no-autoplay")
        if settings["retries"] is not None:
            args += ["--retries", str(settings["retries"])]
        # "auto" is stream.py's own default; passing it changes nothing.
        if settings["backend"] and settings["backend"] != "auto":
            args += ["--backend", settings["backend"]]
        if settings["profile"]:
            args += ["--profile", settings["profile"]]
        for extra in settings["firefox_args"]:
            args += ["--firefox-arg", extra]
        args.append(self.url_at(index))
        return args

    def as_dict(self, defaults: dict | None = None) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            # Both shapes: the editor works in links, the rest of the page still
            # asks about "the" URL.
            "url": self.url,
            "urls": list(self.urls),
            "group": self.group,
            "note": self.note,
            "handler": self.handler,
            "handlers": list(self.handlers),
            "supported": self.supported,
            "site": self.site,
            # The raw value, so the editor can show "inherit"...
            "autoplay": self.autoplay,
            "retries": self.retries,
            "firefox_args": list(self.firefox_args),
            # ...and the effective one, so the page can badge it correctly.
            "effective": self.resolved(defaults),
        }

    def as_entry(self) -> dict:
        """Back to the JSON shape, omitting anything left at its default."""
        entry: dict = {"id": self.id, "label": self.label}
        # One link stays spelled "url", so saving from the settings page leaves
        # an ordinary file looking exactly as it did.
        if len(self.urls) == 1:
            entry["url"] = self.urls[0]
        else:
            entry["urls"] = list(self.urls)
        if self.group:
            entry["group"] = self.group
        if self.note:
            entry["note"] = self.note
        if self.site:
            entry["site"] = self.site
        if self.autoplay is not None:
            entry["autoplay"] = self.autoplay
        if self.retries is not None:
            entry["retries"] = self.retries
        if self.firefox_args:
            entry["firefox_args"] = list(self.firefox_args)
        return entry


@dataclass(frozen=True)
class Link:
    """A way out to a site, for browsing rather than playing.

    Deliberately not a Stream: it has no id, no site class and nothing to drive,
    because it never reaches the TV. It opens on whatever phone or laptop is
    looking at the panel, so someone can read a site's listings and bring a link
    back to the direct-play box.
    """

    label: str
    url: str
    #: Shown as the chip's tooltip - there is no room for it inline.
    note: str = ""


@dataclass
class Catalog:
    """What the file yielded: the streams, plus anything wrong with it."""

    streams: list[Stream] = field(default_factory=list)
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    # Kept apart from `warnings` on purpose. The settings page reads `warnings`
    # out of as_dict() and flashes each one as a red error that never clears -
    # so a typo in a link would sit there accusing a button edit of failing,
    # on a page that cannot even edit links. Only the index page shows these.
    link_warnings: list[str] = field(default_factory=list)
    path: str = DEFAULT_PATH
    # Top-level keys that are not "streams" - the _comment in the shipped file,
    # and anything else someone adds - kept so a save from the page preserves them.
    extra: dict = field(default_factory=dict)
    # The "links" key, parsed for the page to render. The raw value stays in
    # `extra` as well, on purpose: that is what carries it untouched through a
    # save from the settings page, which rewrites only "streams".
    links: tuple[Link, ...] = ()
    # Version tag for optimistic concurrency: the editor sends back what it
    # loaded, and a save is refused if the file has moved on since.
    revision: str = ""

    @property
    def all_warnings(self) -> list[str]:
        """Everything wrong with the file, for the one banner on the index page."""
        return self.warnings + self.link_warnings

    def get(self, stream_id: str) -> Stream | None:
        return next((s for s in self.streams if s.id == stream_id), None)

    def groups(self) -> list[tuple[str, list[Stream]]]:
        """Streams by group, in first-seen order, ungrouped ones last."""
        ordered: dict[str, list[Stream]] = {}
        for stream in self.streams:
            ordered.setdefault(stream.group, []).append(stream)
        named = [(g, s) for g, s in ordered.items() if g]
        rest = [(g, s) for g, s in ordered.items() if not g]
        return named + rest

    def as_dict(self, defaults: dict | None = None) -> dict:
        return {
            "path": self.path,
            "error": self.error,
            "warnings": self.warnings,
            "revision": self.revision,
            "streams": [s.as_dict(defaults) for s in self.streams],
        }


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "stream"


def _label_from_url(url: str) -> str:
    parsed = urlparse(url if "://" in url else "https://" + url)
    return (parsed.hostname or url) + parsed.path.rstrip("/")


def _handler_for(url: str) -> str | None:
    """Name of the site class that claims this URL, for the page to show."""
    for site in sites.SITES:
        if site.handles(url):
            return site.name
    return None


def _read_urls(raw: dict) -> tuple[list[str], list[str]]:
    """The links an entry carries, and anything worth saying about them.

    Two spellings are accepted: "urls" for a button that cycles, and the older
    single "url", so a file written before buttons could cycle - and the direct
    play box, which still posts one link - read exactly as they always did.
    Blanks are dropped rather than played.
    """
    notes: list[str] = []
    listed = raw.get("urls")
    if isinstance(listed, str):
        # The editor posts a textarea, one link per line.
        listed = listed.splitlines()
    if listed is None:
        items: list = []
    elif isinstance(listed, list):
        items = listed
    else:
        notes.append(f'"urls" must be a list of links, ignoring {listed!r}')
        items = []

    urls = [text for text in (str(item).strip() for item in items) if text]
    single = str(raw.get("url") or "").strip()
    if single and urls:
        notes.append('both "url" and "urls" given, using "urls"')
    elif single:
        urls = [single]
    # The same link twice would just mean tapping twice to reach the same dead
    # source. Dropped quietly rather than refused: it is unambiguous what was
    # meant, and complaining would block an otherwise fine save.
    return list(dict.fromkeys(urls)), notes


def _parse_entry(raw: dict, index: int, taken: set[str], warn: list[str]) -> Stream | None:
    where = f"entry {index + 1}"
    if not isinstance(raw, dict):
        warn.append(f"{where}: expected an object, got {type(raw).__name__}")
        return None

    urls, notes = _read_urls(raw)
    for note in notes:
        warn.append(f"{where}: {note}")
    if not urls:
        warn.append(f"{where}: no \"url\", skipped")
        return None

    for key in sorted(set(raw) - KNOWN_KEYS):
        warn.append(f"{where}: unknown key {key!r} ignored")

    label = str(raw.get("label") or "").strip() or _label_from_url(urls[0])

    stream_id = str(raw.get("id") or "").strip() or _slug(label)
    if stream_id in taken:
        # Two buttons with one id would make the second unreachable.
        suffix = 2
        while f"{stream_id}-{suffix}" in taken:
            suffix += 1
        warn.append(f"{where}: duplicate id {stream_id!r}, using {stream_id}-{suffix}")
        stream_id = f"{stream_id}-{suffix}"
    taken.add(stream_id)

    retries = raw.get("retries")
    if retries is not None and not isinstance(retries, int):
        warn.append(f"{where}: \"retries\" must be a whole number, ignoring {retries!r}")
        retries = None

    extra = raw.get("firefox_args") or []
    if not isinstance(extra, list):
        warn.append(f"{where}: \"firefox_args\" must be a list, ignoring {extra!r}")
        extra = []

    site = raw.get("site")
    if site and site not in {s.name for s in sites.SITES}:
        warn.append(f"{where}: unknown site {site!r}, letting the URL decide")
        site = None

    # Absent means "inherit the Advanced tab's default", which is not the same
    # as an explicit false, so don't collapse the two.
    autoplay = raw.get("autoplay")
    if autoplay is not None:
        autoplay = bool(autoplay)

    return Stream(
        id=stream_id,
        label=label,
        urls=tuple(urls),
        group=str(raw.get("group") or "").strip(),
        note=str(raw.get("note") or "").strip(),
        site=site,
        autoplay=autoplay,
        retries=retries,
        firefox_args=tuple(str(a) for a in extra),
        handlers=tuple(site or _handler_for(one) for one in urls),
    )


def _parse_link(raw: object, index: int, warn: list[str]) -> Link | None:
    """One entry of the top-level "links" list, or None if it is unusable.

    A bare string is the short way to write one; the object form adds a label
    and a note. Bad entries are warned about and dropped, the way a bad stream
    is, so one typo does not cost the whole row.
    """
    where = f"link {index + 1}"
    if isinstance(raw, str):
        raw = {"url": raw}
    if not isinstance(raw, dict):
        warn.append(f"{where}: expected a link or an object, got {type(raw).__name__}")
        return None

    for key in sorted(set(raw) - LINK_KEYS):
        warn.append(f"{where}: unknown key {key!r} ignored")

    url = str(raw.get("url") or "").strip()
    if not url:
        warn.append(f"{where}: no \"url\", skipped")
        return None

    scheme = (urlparse(url).scheme or "").lower()
    if not scheme:
        # "example.com" written without a scheme is an address, not a path.
        # Left alone it would become href="example.com", which the browser resolves
        # against the panel itself and lands on a 404.
        url = "https://" + url.lstrip("/")
        scheme = "https"
    # These become an href on a page anyone on the wifi can open. Only the two
    # schemes that mean "another website" are worth rendering - a javascript:
    # link pasted in from a bookmarklet should fail here and say so, rather
    # than quietly turn into a chip that runs it. (An address with a port and
    # no scheme, "localhost:8080", parses its host as the scheme and is refused
    # here; write it with http:// in front.)
    if scheme not in ("http", "https"):
        warn.append(f"{where}: only http and https links open, skipping {scheme!r}")
        return None

    return Link(
        label=str(raw.get("label") or "").strip() or _label_from_url(url),
        url=url,
        note=str(raw.get("note") or "").strip(),
    )


def _revision(path: str) -> str:
    """A cheap version tag for the file: mtime and size."""
    try:
        st = os.stat(path)
    except OSError:
        return ""
    return f"{st.st_mtime_ns}-{st.st_size}"


def load(path: str = DEFAULT_PATH) -> Catalog:
    """Read the catalog. Never raises - a bad file comes back as .error."""
    revision = _revision(path)
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return Catalog(error=f"{path} does not exist yet", path=path)
    except json.JSONDecodeError as exc:
        return Catalog(error=f"{os.path.basename(path)} line {exc.lineno}: {exc.msg}", path=path)
    except OSError as exc:
        return Catalog(error=f"could not read {path}: {exc}", path=path)

    # A bare list is the obvious thing to write by hand, so accept it too.
    entries = data.get("streams") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return Catalog(error='expected {"streams": [ ... ]}', path=path)

    warnings: list[str] = []
    taken: set[str] = set()
    streams = [
        stream
        for index, raw in enumerate(entries)
        if (stream := _parse_entry(raw, index, taken, warnings)) is not None
    ]
    extra = {k: v for k, v in data.items() if k != "streams"} if isinstance(data, dict) else {}

    # Read out of `extra` rather than `data`: it is already guarded against the
    # bare-list form of the file, and leaving the raw value there is what keeps
    # a save from the settings page from dropping it.
    # Links are parsed here and nowhere else. _validate() must never learn to do
    # it: save() refuses any write that produces a warning, so a typo in a link
    # would start blocking every button edit from the settings page.
    listed = extra.get("links")
    link_warnings: list[str] = []
    links: tuple[Link, ...] = ()
    if isinstance(listed, list):
        links = tuple(
            link
            for index, raw in enumerate(listed)
            if (link := _parse_link(raw, index, link_warnings)) is not None
        )
    elif listed is not None:
        link_warnings.append(f'"links" must be a list of links, ignoring {listed!r}')

    return Catalog(
        streams=streams,
        warnings=warnings,
        link_warnings=link_warnings,
        path=path,
        extra=extra,
        links=links,
        revision=revision,
    )


# -- writing ------------------------------------------------------------
#
# The page can now edit the catalog, so there is a second writer besides the
# text editor over SSH. Everything below exists to keep those two from ruining
# each other's work: one lock, one atomic replace, one backup, and a revision
# check so a stale browser tab cannot overwrite a hand-edit it never saw.

_write_lock = threading.Lock()


class SaveError(RuntimeError):
    """The save was refused. The message is meant for the person editing."""


class StaleWrite(SaveError):
    """The file changed since the editor loaded it."""


def save(streams: list[Stream], path: str = DEFAULT_PATH, *, revision: str | None = None) -> Catalog:
    """Write `streams` to the catalog file and return what it reads back as.

    `revision` is the value the editor was given when it loaded; passing it
    turns a blind overwrite into a refusal when the file has moved on. Pass None
    only for a write that is not editing something a person was looking at.
    """
    with _write_lock:
        current = load(path)
        if revision is not None and current.revision and revision != current.revision:
            raise StaleWrite(
                f"{os.path.basename(path)} changed on disk since this page loaded it "
                "- reload and make the change again"
            )

        entries = [s.as_entry() for s in streams]
        document = dict(current.extra)
        document["streams"] = entries
        body = json.dumps(document, indent=2, ensure_ascii=False) + "\n"

        # Parse what we are about to write, exactly as a reader would. A save
        # that would come back with warnings is a save worth refusing.
        check = _validate(body, path)
        if check.error:
            raise SaveError(check.error)
        if check.warnings:
            raise SaveError("; ".join(check.warnings))

        if os.path.exists(path):
            try:
                shutil.copy2(path, path + ".bak")
            except OSError as exc:
                raise SaveError(f"could not back up {path}: {exc}") from None

        directory = os.path.dirname(os.path.abspath(path))
        try:
            # Same directory, so the replace below is atomic on this filesystem.
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".streams-", suffix=".json")
            try:
                with os.fdopen(fd, "w") as fh:
                    fh.write(body)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
        except OSError as exc:
            raise SaveError(f"could not write {path}: {exc}") from None

        return load(path)


def _validate(body: str, path: str) -> Catalog:
    """Parse a document we are about to write, the way load() would read it."""
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        return Catalog(error=f"line {exc.lineno}: {exc.msg}", path=path)
    warnings: list[str] = []
    taken: set[str] = set()
    streams = [
        stream
        for index, raw in enumerate(data.get("streams", []))
        if (stream := _parse_entry(raw, index, taken, warnings)) is not None
    ]
    return Catalog(streams=streams, warnings=warnings, path=path)


def from_form(body: dict, *, existing_id: str | None = None) -> Stream:
    """Build a Stream from what the editor posted. Raises SaveError on nonsense.

    The editor is the untrusted side here: everything is re-checked, and the
    same rules the file parser uses apply, so an entry added on the page and one
    typed into the file behave identically.
    """
    if not isinstance(body, dict):
        raise SaveError("expected a JSON object")

    urls, notes = _read_urls(body)
    if notes:
        raise SaveError("; ".join(notes))
    if not urls:
        raise SaveError("a URL is required")
    for one in urls:
        if " " in one:
            raise SaveError(f"that URL contains a space: {one}")

    label = str(body.get("label") or "").strip() or _label_from_url(urls[0])
    stream_id = str(body.get("id") or existing_id or "").strip() or _slug(label)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", stream_id):
        raise SaveError("an id may only hold letters, digits, dot, dash and underscore")

    site = body.get("site") or None
    if site and site not in {s.name for s in sites.SITES}:
        raise SaveError(f"unknown site {site!r}")

    autoplay = body.get("autoplay")
    if autoplay not in (None, True, False):
        raise SaveError('"autoplay" must be true, false, or left to inherit')

    retries = body.get("retries")
    if retries in ("", None):
        retries = None
    else:
        try:
            retries = int(retries)
        except (TypeError, ValueError):
            raise SaveError('"retries" must be a whole number (-1 keeps trying)') from None

    extra = body.get("firefox_args") or []
    if isinstance(extra, str):
        extra = extra.split()
    if not isinstance(extra, list):
        raise SaveError('"firefox_args" must be a list')

    return Stream(
        id=stream_id,
        label=label,
        urls=tuple(urls),
        group=str(body.get("group") or "").strip(),
        note=str(body.get("note") or "").strip(),
        site=site,
        autoplay=autoplay,
        retries=retries,
        firefox_args=tuple(str(a).strip() for a in extra if str(a).strip()),
        handlers=tuple(site or _handler_for(one) for one in urls),
    )
