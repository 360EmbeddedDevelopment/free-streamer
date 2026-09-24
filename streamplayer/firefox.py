"""Launch the kiosk Firefox that plays a stream page on the TV.

Firefox is the only player here. These pages are ad-heavy and their video is
locked inside a player rather than served as a stream, so what is needed is a
real browser with a real blocker in it. Firefox takes the full uBlock Origin
rather than the cut-down MV3 build, and it fullscreens natively under wayfire -
which is why it is the one player here.

The browser is only half the job: see marionette.py for driving the page it
opens, and sites/ for what each stream site needs done to it.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import shlex
import shutil
import time
import zlib
from dataclasses import dataclass, field
from typing import Callable

from . import display, marionette
from .display import Connector

log = logging.getLogger("stream")

# Firefox gets its own profile rather than the one used for ordinary browsing:
# a supervised kiosk needs --no-remote (so a restart is a fresh window, not a
# tab handed to a running Firefox that then exits straight away), and two
# instances cannot share a profile.
# STREAM_FIREFOX_PROFILE overrides it, matching setup-firefox.sh, so a second
# profile can be built and driven without disturbing the one on the TV.
FIREFOX_PROFILE = os.environ.get("STREAM_FIREFOX_PROFILE") or os.path.expanduser(
    "~/.config/stream-firefox"
)

# Lines Firefox prints on every run that mean nothing is wrong. The supervisor
# drops these unless -v is passed, because a few hundred of them per minute bury
# the errors that do matter. Anything not listed here still prints.
#
# The cursor-theme flood is a real bug, just not ours: the PiXflat theme this
# desktop uses ships cursors/default as a symlink to a left_ptr it does not
# contain, so every cursor lookup a page makes fails and logs. Repair it with
#   gsettings set org.gnome.desktop.interface cursor-theme Adwaita
# if you would rather have working cursors than silence.
BROWSER_NOISE = (
    r"Gdk-Message: .*(cursor theme|Unable to load)",
    r"g_object_ref: assertion 'G_IS_OBJECT",
    r"GLib-GObject-CRITICAL",
    # Firefox probing WebGL against the Pi's V3D driver, then shrugging.
    r"GetShaderInfoLog\(\)|GetShaderSource\(\)",
    r"GL_EXT_shader_texture_lod",
    r"^void main\(\) \{\}$",
    r"Couldn't sanitize GL_RENDERER",
    # Firefox children noticing the parent is gone, during our own shutdown.
    r"Exiting due to channel error\.",
)


class FirefoxUnavailable(RuntimeError):
    """Firefox is not installed, or cannot reach a display."""


@dataclass
class Launch:
    """A command to run, plus env vars to merge over the inherited environment."""

    cmd: list[str]
    env: dict[str, str] = field(default_factory=dict)
    # The profile this command uses. Carried so that a launch which dies on the
    # spot can be explained rather than guessed at (see profile_owner).
    profile: str = ""
    # Regexes for output worth hiding; empty means pass the browser's output through.
    noise: tuple[str, ...] = ()
    # Called once Firefox is running, in a thread of its own: the page still has
    # to be driven (see marionette.py). Never fatal - a page nobody drove is
    # still a page someone can click by hand.
    post_start: Callable[[], None] | None = None


@dataclass
class Config:
    """Everything the launcher needs that is not the URL."""

    connector: Connector
    backend: str = display.BACKEND_WAYLAND
    profile: str = FIREFOX_PROFILE
    # False leaves the video for a human to start and fullscreen.
    autoplay: bool = True
    extra_args: list[str] = field(default_factory=list)

    def output_name(self) -> str:
        return display.output_name(self.connector, self.backend)


def binary() -> str:
    """Path to Firefox, or raise."""
    found = shutil.which("firefox") or shutil.which("firefox-esr")
    if not found:
        raise FirefoxUnavailable("firefox is not installed (try: sudo apt install firefox)")
    return found


def marionette_port(profile: str) -> int:
    """A port derived from the profile path, stable across runs."""
    return marionette.DEFAULT_PORT + zlib.crc32(os.path.abspath(profile).encode()) % 64


def profile_owner(profile: str) -> int | None:
    """The pid of a Firefox already holding this profile, or None.

    Firefox points the profile's `lock` symlink at <host>:+<pid> for as long as
    it owns it, and a second Firefox told to use a locked profile exits at once,
    cleanly, having printed nothing at all. That is indistinguishable from a URL
    the browser refused unless somebody comes and looks here - so we look.
    """
    if not profile:
        return None
    try:
        target = os.readlink(os.path.join(profile, "lock"))
    except OSError:
        return None  # no lock, or a regular file: nobody is holding it
    _, _, pid = target.rpartition("+")
    try:
        owner = int(pid)
    except ValueError:
        return None
    try:
        os.kill(owner, 0)
    except ProcessLookupError:
        return None  # stale lock from a browser that died badly
    except PermissionError:
        pass  # alive, just not ours to signal
    return owner


# Rewritten on every launch rather than appended to, so changing a value here
# cannot leave a stale duplicate behind in user.js. The pattern matches blocks
# earlier versions wrote too, so a profile built before a rename is cleaned up
# rather than left carrying two sets of prefs.
_BLOCK_START = "// --- managed by streamplayer/firefox.py ---"
_BLOCK_END = "// --- end managed block ---"
_MANAGED_RE = re.compile(
    r"// --- managed by streamplayer/.*?// --- end[^\n]*---\n?", re.DOTALL
)

# Prefs every launch needs, whatever the site.
#
# requestFullscreen() normally requires a click to borrow permission from, and a
# synthesized click does grant that - but only sometimes: the player's own
# handler can consume the activation first, and it expires in seconds. The pref
# is the deterministic route, so rather than trust that setup-firefox.sh has
# been re-run since this feature landed, write it on every launch.
#
# WebRTC is off because stream pages use it against the viewer. Some players
# carry a peer-to-peer engine that turns every viewer into a seeder: measured
# here, the Pi uploaded 2.5-7.7 Mbit/s of the stream to strangers, over the same
# wifi radio as the download, growing as more peers found it. That upload cost
# ~10 points of CPU and made frame drops spiky (8.7% worst 10s window, against
# 3.7% without). The engine falls back to plain HTTP when WebRTC is absent, and
# a TV has no video calls to break.
BASE_PREFS: dict[str, object] = {
    "full-screen-api.allow-trusted-requests-only": False,
    "media.peerconnection.enabled": False,
}


def ensure_prefs(profile: str, extra: dict[str, object] | None = None) -> int:
    """Write the prefs this launch needs into the profile. Returns its port.

    `extra` is whatever the site asked for on top of BASE_PREFS, so a site that
    needs, say, a permission pre-denied can have it without a setup step.
    """
    port = marionette_port(profile)
    prefs: dict[str, object] = {
        **BASE_PREFS,
        **(extra or {}),
        "marionette.port": port,
    }
    lines = [_BLOCK_START]
    lines += [f"user_pref({json.dumps(k)}, {json.dumps(v)});" for k, v in prefs.items()]
    lines += [_BLOCK_END, ""]
    block = "\n".join(lines)

    path = os.path.join(profile, "user.js")
    try:
        existing = ""
        if os.path.exists(path):
            with open(path) as fh:
                existing = fh.read()
        existing = _MANAGED_RE.sub("", existing)
        if existing and not existing.endswith("\n"):
            existing += "\n"
        os.makedirs(profile, exist_ok=True)
        with open(path, "w") as fh:
            fh.write(existing + block)
        log.debug("prefs written to %s, marionette port %d", path, port)
    except OSError as exc:
        log.warning("could not write %s (%s)", path, exc)
    return port


def command(
    url: str,
    cfg: Config,
    *,
    prefs: dict[str, object] | None = None,
    drive: Callable[[marionette.Marionette], None] | None = None,
) -> Launch:
    """Firefox in kiosk mode on the TV, with `drive` run against the page.

    Firefox honours a fullscreen request under wayfire, so it needs no Xwayland
    detour. It has no output-selection flag of its own - with one screen
    connected the compositor puts it on the TV regardless.
    """
    browser = binary()

    if cfg.backend == display.BACKEND_DRM:
        raise FirefoxUnavailable(
            "no desktop session found, and Firefox cannot draw to a bare console. "
            "Start the desktop (sudo systemctl set-default graphical.target; reboot), "
            "or run this from a session that owns the screen."
        )

    env = display.session_env(cfg.backend)
    if cfg.backend == display.BACKEND_WAYLAND:
        env["MOZ_ENABLE_WAYLAND"] = "1"

    cmd = [
        browser,
        "--kiosk",
        # Keep this instance off any Firefox the user has open by hand: without
        # it the URL is handed to that process and this one exits immediately,
        # which the supervisor would read as a crash and retry.
        "--no-remote",
        "--profile",
        cfg.profile,
    ]

    post_start = None
    if drive is not None:
        # Marionette is how the page gets driven; see marionette.py.
        cmd.append("--marionette")
        port = ensure_prefs(cfg.profile, prefs)
        post_start = functools.partial(_drive, port, drive)
    else:
        ensure_prefs(cfg.profile, prefs)

    cmd += cfg.extra_args
    cmd.append(url)
    return Launch(
        cmd=cmd, env=env, noise=BROWSER_NOISE, post_start=post_start, profile=cfg.profile
    )


def _drive(port: int, drive: Callable[[marionette.Marionette], None]) -> None:
    """Run a site's page driver against the browser that just started.

    Nothing here is allowed to take the stream down with it: every failure is
    logged and shrugged off, leaving a playable page someone can still click.

    A timeout gets one more go on a fresh session. The page is usually fine -
    one command was just slow while it loaded - but a reply that lands late
    would answer the wrong request on the old socket, so reconnecting is the
    only safe way to carry on. A driver is safe to run twice: it finds the
    video, and only clicks what is not already playing.
    """
    for attempt in (1, 2):
        try:
            with marionette.session(port) as page:
                drive(page)
            return
        except TimeoutError as exc:
            if attempt == 1:
                log.warning("page control: a command timed out (%s) - reconnecting to try again", exc)
                time.sleep(1.0)  # let Firefox retire the old session before a new one
                continue
            log.warning("page control: lost the marionette connection (%s)", exc)
        except marionette.MarionetteError as exc:
            log.warning("page control: %s", exc)
        except OSError as exc:
            log.warning("page control: lost the marionette connection (%s)", exc)
        except Exception as exc:  # a site's own driver, which must not be fatal either
            log.warning("page control: %s failed (%s)", getattr(drive, "__qualname__", drive), exc)
        return


def describe_command(launch: Launch) -> str:
    """The launch as a copy-pasteable shell line, env prefix included."""
    prefix = [f"{k}={shlex.quote(v)}" for k, v in sorted(launch.env.items())]
    return " ".join(prefix + [shlex.quote(a) for a in launch.cmd])
