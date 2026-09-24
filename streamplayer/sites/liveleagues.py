"""liveleagues.me - the site this player was built and tested against.

Its match pages are the plain case the shared routine already handles: the
video sits in a cross-origin iframe behind a poster, and once clicked it plays
in a box in the middle of an ad-heavy page. Clicking that poster and asking for
fullscreen is the whole job, so drive() is one call - uBlock (installed by
setup-firefox.sh) deals with the rest of the page.

Anything this site starts needing that others do not - a server chooser, a
consent dialog, a URL rewrite - belongs here rather than in the base class.
"""

from __future__ import annotations

from .. import marionette
from .base import StreamSite


class LiveLeagues(StreamSite):
    name = "liveleagues"
    domains = ("liveleagues.me",)

    def drive(self, page: marionette.Marionette) -> None:
        self.play_fullscreen(page)
