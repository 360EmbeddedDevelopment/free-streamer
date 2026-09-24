"""Drive the running Firefox from outside it, over Marionette.

A stream page hands you a player, not a stream: the video sits in a cross-origin
iframe behind a poster you are expected to click, and it fills a box in the
middle of the page rather than the screen. On a TV across the room there is
nobody to click either one.

Marionette is Firefox's own remote-control protocol - plain JSON over TCP, no
extra packages - so the browser we already launched can be driven from here.
This module is the transport plus the handful of page operations a site needs;
deciding which of them a page wants is each site class's business (see
streamplayer/sites/).
"""

from __future__ import annotations

import contextlib
import json
import socket
import time
from typing import Iterator

# Marionette's own default. Each profile gets a port derived from its path
# instead (see firefox.marionette_port): two kiosks sharing one would leave the
# second Firefox unable to bind it - it exits - while this client drove the
# first one's browser by mistake.
DEFAULT_PORT = 2828
CONNECT_TIMEOUT = 40.0
# How long one command may take to answer - separate from the connect timeout.
# A command can legitimately wait on the page: switching into an iframe blocks
# until that frame has loaded, and on an ad-heavy stream page pulling a large
# player over wifi that runs well past the ten seconds a local connect needs.
# With the connect's ten seconds applied to every reply, one slow frame switch
# timed out and cost the whole playback.
COMMAND_TIMEOUT = 60.0


class MarionetteError(RuntimeError):
    """Marionette refused a command, or never came up."""


# readyState is the honest answer to "is there anything to show?". paused=false
# only means nobody pressed pause: a video that is stalled with an empty buffer
# reports exactly that while the screen stays black, so the state a site class
# reasons about carries how much data the player actually has.
_STATE_JS = """
  const v = document.querySelector('video');
  if (!v) return JSON.stringify({gone: true});
  const r = v.getBoundingClientRect();
  let buffered = 0;
  try {
    if (v.buffered && v.buffered.length) buffered = v.buffered.end(v.buffered.length - 1);
  } catch (e) {}
  return JSON.stringify({
    paused: v.paused,
    time: v.currentTime,
    ready: v.readyState,
    network: v.networkState,
    buffered: buffered,
    width: v.videoWidth,
    error: v.error ? v.error.code : 0,
    fullscreen: !!document.fullscreenElement,
    x: r.x + r.width / 2,
    y: r.y + r.height / 2,
  });
"""

#: The button a player lays over its poster, in the order worth trying. Every
#: player names it differently, and one page can carry two players' markup, so
#: the first one actually on screen wins.
PLAY_BUTTONS = (
    ".jw-icon-display",       # JW Player
    ".play-wrapper",          # Clappr
    ".vjs-big-play-button",   # video.js
    "[aria-label*='play' i]",
    "[title*='play' i]",
)

#: readyState values worth naming, for the log line when a stream is slow.
READY_STATES = {
    0: "nothing yet",
    1: "metadata only",
    2: "current frame only",
    3: "buffering ahead",
    4: "buffered",
}

_PLAY_JS = """
  const v = document.querySelector('video');
  if (v && v.paused) { const p = v.play(); if (p && p.catch) p.catch(() => {}); }
"""

_FULLSCREEN_JS = """
  const v = document.querySelector('video');
  if (v && !document.fullscreenElement) { try { v.requestFullscreen(); } catch (e) {} }
"""


class Marionette:
    """The slice of Firefox's remote protocol a stream page needs.

    Framing is `<byte length>:<json>`, and every command is answered in order,
    so a request/reply pair needs no bookkeeping beyond a message id.
    """

    def __init__(self, port: int = DEFAULT_PORT, timeout: float = CONNECT_TIMEOUT):
        self.sock: socket.socket | None = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
                break
            except OSError:
                time.sleep(0.5)  # Firefox is still starting up
        if self.sock is None:
            raise MarionetteError(f"nothing listening on marionette port {port}")
        # create_connection's timeout sticks to the socket for every later read,
        # so replace it: short to connect, patient to answer.
        self.sock.settimeout(COMMAND_TIMEOUT)
        self.buf = b""
        self.msgid = 0
        self._recv()  # the server greets us first

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    # -- transport -------------------------------------------------------

    def _recv(self) -> list:
        assert self.sock is not None
        while b":" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise MarionetteError("marionette closed the connection")
            self.buf += chunk
        raw_len, _, rest = self.buf.partition(b":")
        length = int(raw_len)
        while len(rest) < length:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise MarionetteError("marionette closed the connection")
            rest += chunk
        self.buf = rest[length:]
        return json.loads(rest[:length])

    def cmd(self, name: str, params: dict | None = None):
        assert self.sock is not None
        self.msgid += 1
        payload = json.dumps([0, self.msgid, name, params or {}]).encode()
        self.sock.sendall(str(len(payload)).encode() + b":" + payload)
        msg = self._recv()
        if msg[2]:
            raise MarionetteError(f"{name}: {msg[2]}")
        return msg[3]

    def js(self, script: str):
        return self.cmd("WebDriver:ExecuteScript", {"script": script, "args": []})["value"]

    # -- page operations -------------------------------------------------

    def to_top(self) -> None:
        """Leave the session focused on the top-level document again."""
        handle = self.cmd("WebDriver:GetWindowHandle")["value"]
        self.cmd("WebDriver:SwitchToWindow", {"handle": handle})

    def has_video(self) -> bool:
        return bool(self.js("return !!document.querySelector('video')"))

    def _iframes(self) -> list:
        return self.cmd("WebDriver:FindElements", {"using": "tag name", "value": "iframe"})

    def _descend(self, path: tuple[int, ...]) -> bool:
        """Focus the frame reached by taking iframe `path` from the top document.

        SwitchToFrame only ever goes downward, so there is no walking back up a
        level: every frame is reached by starting at the top and descending
        again. Paths are indexes rather than elements because the handles from
        one descent are not valid on the next.
        """
        self.to_top()
        for index in path:
            frames = self._iframes()
            if index >= len(frames):  # the page rebuilt itself mid-search
                return False
            self.cmd("WebDriver:SwitchToFrame", {"element": _element_ref(frames[index])})
        return True

    def find_video_frame(self, timeout: float, max_depth: int = 1) -> bool:
        """Focus whichever frame owns a <video>. False if none appears in time.

        Searches the top document first, then its iframes, then theirs, out to
        `max_depth`. Shallowest first, so a site whose player sits in a plain
        top-level iframe is found before anything nested - the deeper levels
        cost nothing until a site actually needs them.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.to_top()
            if self.has_video():
                return True
            paths: list[tuple[int, ...]] = [()]
            for _level in range(max_depth):
                deeper: list[tuple[int, ...]] = []
                for path in paths:
                    if not self._descend(path):
                        continue
                    for index in range(len(self._iframes())):
                        child = path + (index,)
                        if not self._descend(child):
                            continue
                        if self.has_video():
                            return True
                        deeper.append(child)
                if not deeper:
                    break  # nothing left to open; wait and start over
                paths = deeper
            time.sleep(1.0)
        return False

    def click(self, x: float, y: float) -> None:
        """A synthesized pointer click - a trusted event, unlike element.click()."""
        self.cmd(
            "WebDriver:PerformActions",
            {
                "actions": [
                    {
                        "type": "pointer",
                        "id": "mouse",
                        "parameters": {"pointerType": "mouse"},
                        "actions": [
                            {"type": "pointerMove", "x": int(x), "y": int(y), "origin": "viewport"},
                            {"type": "pointerDown", "button": 0},
                            {"type": "pause", "duration": 60},
                            {"type": "pointerUp", "button": 0},
                        ],
                    }
                ]
            },
        )

    def video_state(self) -> dict:
        """paused/time/fullscreen and the video's centre, or {'gone': True}."""
        return json.loads(self.js(_STATE_JS))

    def play(self) -> None:
        self.js(_PLAY_JS)

    def request_fullscreen(self) -> None:
        self.js(_FULLSCREEN_JS)

    def click_play_button(self) -> bool:
        """Click the player's own play button. False when it hasn't got one.

        Better than clicking the middle of the video. A player like JW or
        Clappr manages its own pipeline and does not fetch a thing until its
        button is pressed - telling the bare <video> to play instead leaves the
        player sitting idle behind it, attached to a source it never loaded.

        Only a button actually on screen counts: players keep their markup
        around and shrink it to nothing once playback starts.
        """
        script = (
            "for (const sel of %s) {"
            "  for (const el of document.querySelectorAll(sel)) {"
            "    const r = el.getBoundingClientRect();"
            "    if (r.width > 8 && r.height > 8) {"
            "      return JSON.stringify({x: r.x + r.width / 2, y: r.y + r.height / 2});"
            "    }"
            "  }"
            "}"
            "return null;"
        ) % json.dumps(PLAY_BUTTONS)
        found = self.js(script)
        if not found:
            return False
        box = json.loads(found)
        self.click(box["x"], box["y"])
        return True

    def click_text(self, selector: str, text: str) -> bool:
        """Click the first element matching selector whose text contains `text`.

        For the chooser links a site puts in front of its player ("Server 2",
        "HD"). Returns False when nothing matched, so a caller can move on.
        """
        script = (
            "const t = %s.toLowerCase();"
            "const el = [...document.querySelectorAll(%s)]"
            "  .find(e => (e.textContent || '').toLowerCase().includes(t));"
            "if (!el) return null;"
            "const r = el.getBoundingClientRect();"
            "return JSON.stringify({x: r.x + r.width / 2, y: r.y + r.height / 2});"
        ) % (json.dumps(text), json.dumps(selector))
        found = self.js(script)
        if not found:
            return False
        box = json.loads(found)
        self.click(box["x"], box["y"])
        return True


def _element_ref(element) -> str:
    """Marionette returns an element as a one-key dict whose key names the spec."""
    if isinstance(element, dict):
        return next(v for k, v in element.items() if "element" in k.lower())
    return element


@contextlib.contextmanager
def session(port: int = DEFAULT_PORT, timeout: float = CONNECT_TIMEOUT) -> Iterator[Marionette]:
    """Connect to a running Firefox and start a WebDriver session on it."""
    mar = Marionette(port=port, timeout=timeout)
    try:
        mar.cmd("WebDriver:NewSession", {"capabilities": {}})
        yield mar
    finally:
        mar.close()
