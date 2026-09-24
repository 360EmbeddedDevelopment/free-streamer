"""ntv.st - a watch page that wraps its player in two more pages.

Nothing about the video itself is unusual: it is Clappr over HLS, sitting behind
a poster, and clicking that poster is the whole job. What the shared routine
cannot do here is *find* it. A watch page embeds /embed?t=<token>, that page
embeds an embed.st player, and the <video> only ever exists inside the innermost
of the three - two iframes down, where a one-level search never looks. That is
what frame_depth is for, and it is the only thing this site really needs.

The innermost page also injects a hidden 1x1 ad iframe every ten minutes and
removes it nine seconds later. It holds no video, so the search steps over it;
it is worth knowing about only when a frame dump shows a frame that was not
there a moment ago.
"""

from __future__ import annotations

from urllib.parse import urlparse

from .. import marionette
from .base import StreamSite, normalize_url


class NTV(StreamSite):
    name = "ntv"
    domains = ("ntv.st",)

    #: Only the kobra server's watch pages. The host on its own is not enough:
    #: the site also serves a home page and a listing page per server, and the
    #: other servers' watch pages carry no embed at all - nothing to find, and
    #: nothing to drive. Claiming those would buy a blank screen and a wait for
    #: the timeout instead of an honest "nothing here handles that".
    path_prefix = "/watch/kobra/"

    @classmethod
    def handles(cls, url: str) -> bool:
        """A kobra watch page, and nothing else on the domain.

        --site ntv still forces this class onto any URL, which is the way to
        try a server this does not claim yet.
        """
        if not super().handles(url):
            return False
        return urlparse(normalize_url(url)).path.startswith(cls.path_prefix)

    @classmethod
    def accepts(cls) -> str:
        return cls.domains[0] + cls.path_prefix

    #: The player sits two iframes down. The third level is slack - an
    #: unopened level costs nothing, and this chain is not ours to rely on.
    frame_depth = 3

    #: Three documents have to load before a <video> exists at all, and the
    #: last of them pulls its player and a peer-to-peer engine off public CDNs.
    #: On a cold profile that is comfortably slower than a plain embed.
    video_timeout = 90.0

    #: And the video element turns up well before the picture does: the player
    #: opens its playlist, and a peer-to-peer engine would rather find a peer
    #: than hit the CDN, so the first frame is a good few seconds behind the
    #: click. Waiting is the whole difference between a black box and a stream.
    play_timeout = 120.0

    def drive(self, page: marionette.Marionette) -> None:
        self.play_fullscreen(page)
