"""The template every streaming website implements.

A stream site is more than a URL. Each one wraps its video differently - behind
a poster in a cross-origin iframe, behind a "server" chooser, behind a consent
dialog - and each may want prefs of its own in the kiosk profile. StreamSite is
the shape of that knowledge, so supporting a new site means writing one small
class rather than touching the launcher.

A subclass sets `name` and `domains`, then implements drive(): what to do once
the page is up. play_fullscreen() is the routine that works on a plain player
page - find the video in whichever frame owns it, click its poster, ask for
fullscreen - so a site that needs nothing special just calls it, and a site that
does can do its own steps first and then hand over.
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from typing import ClassVar
from urllib.parse import urlparse

from .. import firefox, marionette

log = logging.getLogger("stream")


def normalize_url(url: str) -> str:
    """Accept a link pasted without its scheme, e.g. example.com/match/123."""
    return url if "://" in url else "https://" + url.lstrip("/")


class StreamSite(ABC):
    """One streaming website, and how to get its video onto the TV."""

    #: Short identifier, also what --site accepts.
    name: ClassVar[str] = ""
    #: Hostnames this site owns. Subdomains of each are matched too.
    domains: ClassVar[tuple[str, ...]] = ()

    #: How long the page gets to produce a <video> element at all. The page
    #: loads, then the player boots, then hls.js opens the stream; ads and a
    #: cold cache make that slow, so wait well past what a warm run needs.
    video_timeout: ClassVar[float] = 60.0
    #: Once a video exists, play() can still be refused while hls.js buffers,
    #: so keep nudging the poster for this long rather than giving up on the
    #: first no. Only the clicking stops here - the waiting carries on.
    act_timeout: ClassVar[float] = 25.0
    #: How long the stream gets to go from "told to play" to actually playing.
    #: This is buffering time, not page-load time: the playlist has to open and
    #: a first frame has to decode, which on a busy source is not instant.
    play_timeout: ClassVar[float] = 60.0
    #: The readyState that counts as playable. 3 is HAVE_FUTURE_DATA - enough
    #: decoded to keep going, rather than 1's metadata or 2's single frame.
    ready_state: ClassVar[int] = 3
    #: Seconds between one press of the play button and the next. A player
    #: needs a moment to react, and pressing again too soon just toggles off
    #: what the last press turned on.
    click_interval: ClassVar[float] = 6.0
    #: Seconds between polls while waiting. Each one is a round trip to the
    #: browser, so this is slow enough to stay cheap and quick enough to catch
    #: the moment the picture arrives.
    poll_interval: ClassVar[float] = 1.5
    #: How many iframes deep the player may sit. 1 covers the usual case - a
    #: player embedded straight into the page. Raise it for a site that wraps
    #: its embed in another embed, and leave it alone otherwise: every extra
    #: level is more frames to open before the search gives up.
    frame_depth: ClassVar[int] = 1

    def __init__(self, url: str, cfg: firefox.Config):
        self.url = normalize_url(url)
        self.cfg = cfg

    def __str__(self) -> str:
        return self.name or type(self).__name__

    @classmethod
    def owns_host(cls, url: str) -> bool:
        """True when this site's domains cover the URL's host.

        Separate from handles() because a site may own a host and still refuse
        a page on it, and the difference is worth saying out loud: "that domain
        is mine but that page is not" is a different problem from "never heard
        of it".
        """
        host = (urlparse(normalize_url(url)).hostname or "").lower()
        return any(host == d or host.endswith("." + d) for d in cls.domains)

    @classmethod
    def handles(cls, url: str) -> bool:
        """True when this class is the one for `url`.

        The host is the whole test by default. A site that only drives part of
        its domain narrows this further, and says so in accepts().
        """
        return cls.owns_host(url)

    @classmethod
    def accepts(cls) -> str:
        """What this site takes, for --list-sites - its domains, unless a
        subclass claims something narrower than all of them."""
        return ", ".join(cls.domains)

    # -- hooks -----------------------------------------------------------
    #
    # Everything below has a working default. Override what this site needs.

    def page_url(self) -> str:
        """The URL to actually open - a chance to rewrite what was passed in."""
        return self.url

    def prefs(self) -> dict[str, object]:
        """Extra user.js prefs for the kiosk profile, on top of firefox.BASE_PREFS."""
        return {}

    @abstractmethod
    def drive(self, page: marionette.Marionette) -> None:
        """Get the video playing, called once the browser is up.

        Runs in a thread beside the browser and may block for as long as it
        needs. Raising is not fatal - it is logged and the page is left alone -
        so this is free to give up when a site does something unexpected.
        """

    # -- the launch ------------------------------------------------------

    def launch(self) -> firefox.Launch:
        """The kiosk command for this site. Called fresh before every attempt."""
        return firefox.command(
            self.page_url(),
            self.cfg,
            prefs=self.prefs(),
            drive=self.drive if self.cfg.autoplay else None,
        )

    # -- shared page routines --------------------------------------------

    def play_fullscreen(self, page: marionette.Marionette) -> bool:
        """Start the video and put it fullscreen. True when both worked.

        The generic routine for a player page: find the frame that owns the
        video, click its poster the way a person would, and ask for fullscreen.

        Clicking is the fast part; what follows it is not. The click only tells
        the player to start fetching, and the stream is not watchable until it
        has opened its playlist, filled a buffer and decoded a first frame. So
        the click is nudged for `act_timeout` and then left alone, while the
        real wait - for the video to report data and a clock that is moving -
        runs to `play_timeout`.
        """
        if not page.find_video_frame(self.video_timeout, self.frame_depth):
            log.warning("%s: no video on the page after %.0fs", self, self.video_timeout)
            return False

        started = time.monotonic()
        deadline = started + self.play_timeout
        nudge_until = started + self.act_timeout
        last_time: float | None = None
        said: str | None = None
        next_click = 0.0
        state: dict = {}

        while time.monotonic() < deadline:
            state = page.video_state()
            if state.get("gone"):
                break  # the player tore its own video out; nothing to drive

            if state.get("error"):
                log.warning(
                    "%s: the player gave up on this stream (media error %s) - "
                    "the source may be down, or another server may work",
                    self,
                    state["error"],
                )
                return False

            now = state.get("time", 0.0)
            # Two samples with a moving clock between them is the one signal
            # that cannot be faked by a player that merely accepted play().
            moving = last_time is not None and now > last_time + 0.01
            last_time = now
            ready = state.get("ready", 0) >= self.ready_state

            if not state.get("paused") and ready and moving:
                if state.get("fullscreen"):
                    log.info("%s: playing fullscreen (%.0fs)", self, time.monotonic() - started)
                    return True
                page.request_fullscreen()
            else:
                said = self._say_waiting(state, said)
                if state.get("paused"):
                    if state.get("ready"):
                        # The media is there and simply not running. Nothing to
                        # press - asking the element directly is enough, and is
                        # safe now that the player has what it needs.
                        page.play()
                    elif time.monotonic() < nudge_until and time.monotonic() >= next_click:
                        # Nothing has loaded at all, so the player has not been
                        # told to start. Press its own button if it has one -
                        # that is what makes it fetch; only a bare <video> with
                        # no player chrome wants the poster clicked instead.
                        if not page.click_play_button():
                            page.click(state["x"], state["y"])
                            page.play()
                        next_click = time.monotonic() + self.click_interval
                # Fullscreen is deliberately not asked for here. A video with
                # no picture fullscreens just as happily as one with, and then
                # the black box covers the page - the poster, the server list,
                # and whatever the site was trying to say about why it is not
                # playing. Fullscreen belongs after playback, not before it.
            time.sleep(self.poll_interval)

        self._report_giving_up(state)
        return False

    def _say_waiting(self, state: dict, said: str | None) -> str | None:
        """Log what the video is waiting on, once per change rather than per poll."""
        phase = marionette.READY_STATES.get(state.get("ready", 0), "unknown")
        if phase == said:
            return said
        log.info(
            "%s: waiting for the stream - %s%s",
            self,
            phase,
            "" if not state.get("buffered") else f", {state['buffered']:.0f}s buffered",
        )
        return phase

    def _report_giving_up(self, state: dict) -> None:
        ready = state.get("ready", 0)
        playing = not state.get("paused", True) and ready >= self.ready_state
        if playing and not state.get("fullscreen"):
            log.warning(
                "%s: playing, but fullscreen was refused - check that "
                "full-screen-api.allow-trusted-requests-only is false in the "
                "kiosk profile (./setup-firefox.sh sets it)",
                self,
            )
        elif not ready and state.get("network") == 2:
            # Attached and still "loading" with nothing to show. The player
            # opened its source and got no media back at all, which is the
            # shape of a feed that is off air rather than a slow one.
            log.warning(
                "%s: the player attached to the stream but no media ever "
                "arrived (%.0fs, nothing buffered) - that feed is most likely "
                "off air; try another server, or another stream",
                self,
                self.play_timeout,
            )
        elif ready and ready < self.ready_state:
            log.warning(
                "%s: the stream started loading but never buffered enough to "
                "play (%s after %.0fs) - the source is probably struggling, so "
                "try another server",
                self,
                marionette.READY_STATES.get(ready, ready),
                self.play_timeout,
            )
        else:
            log.warning(
                "%s: gave up with playing=%s fullscreen=%s (start it by hand, "
                "or pass --no-autoplay)",
                self,
                playing,
                bool(state.get("fullscreen")),
            )

    def log_state(self, page: marionette.Marionette) -> None:
        """Dump what the page looks like right now - for debugging a new site."""
        log.debug("%s: video state %s", self, json.dumps(page.video_state()))
