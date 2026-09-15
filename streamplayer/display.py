"""Detect the HDMI output and its matching audio device.

The Pi exposes each HDMI port as a DRM connector under /sys/class/drm and as a
separate ALSA card (vc4hdmi0 / vc4hdmi1). Neither name is stable across boots or
board revisions, so everything here is discovered at runtime.
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
from dataclasses import dataclass

DRM_PATH = "/sys/class/drm"
MODE_RE = re.compile(r"^(\d+)x(\d+)")

# How mpv must talk to the display. Whatever already owns the screen wins: DRM
# master is exclusive, so if X or a Wayland compositor is running, mpv has to
# render as a client of it rather than taking the display over.
BACKEND_X11 = "x11"
BACKEND_WAYLAND = "wayland"
BACKEND_DRM = "drm"


class NoDisplayError(RuntimeError):
    """No HDMI output has a display attached."""


@dataclass
class Connector:
    name: str  # mpv's --drm-connector value, e.g. "HDMI-A-2"
    path: str  # /sys/class/drm/card1-HDMI-A-2
    index: int  # the 2 in HDMI-A-2

    @property
    def modes(self) -> list[str]:
        try:
            with open(os.path.join(self.path, "modes")) as fh:
                return [line.strip() for line in fh if line.strip()]
        except OSError:
            return []


def _read(path: str) -> str:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def connectors() -> list[Connector]:
    """Every HDMI connector the kernel knows about, in port order."""
    found = []
    for path in sorted(glob.glob(os.path.join(DRM_PATH, "card*-HDMI-A-*"))):
        name = os.path.basename(path).split("-", 1)[1]  # card1-HDMI-A-2 -> HDMI-A-2
        index = int(name.rsplit("-", 1)[1])
        found.append(Connector(name=name, path=path, index=index))
    return sorted(found, key=lambda c: c.index)


def find_connected_connector() -> Connector:
    """The first HDMI port with a display plugged into it."""
    all_ports = connectors()
    for conn in all_ports:
        if _read(os.path.join(conn.path, "status")) == "connected":
            return conn
    names = ", ".join(c.name for c in all_ports) or "none found"
    raise NoDisplayError(
        f"No HDMI display detected (checked: {names}). Is the TV on and the cable seated? "
        "Pass --connector to override detection."
    )


def get_connector(name: str | None) -> Connector:
    """Look up a connector by name, or auto-detect when name is None."""
    if name is None:
        return find_connected_connector()
    for conn in connectors():
        if conn.name == name:
            return conn
    known = ", ".join(c.name for c in connectors()) or "none found"
    raise NoDisplayError(f"Unknown connector {name!r}. Available: {known}")


def best_mode(conn: Connector, max_height: int | None = None) -> str | None:
    """Highest-resolution mode the display advertises, at or below max_height.

    Interlaced modes are skipped - progressive is always the better choice here.
    """
    best: tuple[int, str] | None = None
    for mode in conn.modes:
        m = MODE_RE.match(mode)
        if not m or mode.endswith("i"):
            continue
        width, height = int(m.group(1)), int(m.group(2))
        if max_height is not None and height > max_height:
            continue
        area = width * height
        if best is None or area > best[0]:
            best = (area, f"{width}x{height}")
    return best[1] if best else None


def _runtime_dir() -> str:
    return os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"


def wayland_socket() -> str | None:
    """Name of a running Wayland display, or None."""
    if os.environ.get("WAYLAND_DISPLAY"):
        return os.environ["WAYLAND_DISPLAY"]
    # Over SSH the env is empty, so look for the socket the compositor left.
    for path in sorted(glob.glob(os.path.join(_runtime_dir(), "wayland-[0-9]*"))):
        if not path.endswith(".lock"):
            return os.path.basename(path)
    return None


def x_display() -> str | None:
    """An X display we can reach, e.g. ":0", or None."""
    if os.environ.get("DISPLAY"):
        return os.environ["DISPLAY"]
    for path in sorted(glob.glob("/tmp/.X11-unix/X[0-9]*")):
        return ":" + os.path.basename(path)[1:]
    return None


def detect_backend() -> str:
    """Which display stack mpv should target.

    Wayland and X are checked first because a running compositor holds DRM
    master exclusively - mpv's drm output only works when nothing else owns the
    screen (a bare console boot).
    """
    if wayland_socket():
        return BACKEND_WAYLAND
    if x_display():
        return BACKEND_X11
    return BACKEND_DRM


def session_env(backend: str) -> dict[str, str]:
    """Env vars a player needs to reach the session, for SSH-launched runs."""
    env: dict[str, str] = {}
    if backend == BACKEND_WAYLAND:
        sock = wayland_socket()
        if sock:
            env["WAYLAND_DISPLAY"] = sock
        env["XDG_RUNTIME_DIR"] = _runtime_dir()
    elif backend == BACKEND_X11:
        disp = x_display()
        if disp:
            env["DISPLAY"] = disp
        if "XAUTHORITY" not in os.environ:
            xauth = os.path.expanduser("~/.Xauthority")
            if os.path.exists(xauth):
                env["XAUTHORITY"] = xauth
    return env


def output_name(conn: Connector, backend: str) -> str:
    """The connector's name in the given display stack.

    X drops the DRM connector-type letter: DRM's HDMI-A-2 is X's HDMI-2.
    Wayland compositors keep the DRM name as-is.
    """
    if backend == BACKEND_X11:
        return conn.name.replace("HDMI-A-", "HDMI-")
    return conn.name


def _alsa_hdmi_cards() -> list[str]:
    """ALSA card names for the vc4 HDMI outputs, in card-number order."""
    cards = []
    for path in sorted(glob.glob("/proc/asound/card*/id")):
        name = _read(path)
        if name.startswith("vc4hdmi"):
            cards.append(name)
    return cards


def _pipewire_hdmi_sink() -> str | None:
    """An mpv audio-device string for the PipeWire/Pulse HDMI sink, if one exists."""
    try:
        out = subprocess.run(
            ["mpv", "--audio-device=help"],
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        m = re.search(r"'((?:pipewire|pulse)/[^']*hdmi[^']*)'", line)
        if m:
            return m.group(1)
    return None


def hdmi_audio_device(conn: Connector) -> str:
    """Best-guess mpv --audio-device for audio over this HDMI port.

    Prefers the ALSA card whose number matches the connector index (HDMI-A-1 ->
    vc4hdmi0), since that addresses the hardware directly. The mapping is a
    convention rather than a guarantee, so verify with speaker-test once:

        speaker-test -D hdmi:CARD=vc4hdmi1,DEV=0 -c 2 -t wav
    """
    cards = _alsa_hdmi_cards()
    wanted = f"vc4hdmi{conn.index - 1}"
    if wanted in cards:
        return f"alsa/hdmi:CARD={wanted},DEV=0"
    if cards:
        return f"alsa/hdmi:CARD={cards[0]},DEV=0"
    return _pipewire_hdmi_sink() or "auto"


def hevc_decoder_device() -> str | None:
    """Path of the Pi's HEVC hardware decoder, if the kernel exposes one.

    The Pi 5 has an HEVC block (rpi-hevc-dec) but no H.264 one, so this is the
    only codec that can be offloaded here.
    """
    for path in sorted(glob.glob("/dev/video*")):
        node = os.path.basename(path)
        name = _read(f"/sys/class/video4linux/{node}/name")
        if "hevc" in name.lower():
            return path
    return None


def describe() -> str:
    """Human-readable summary of the detected display setup."""
    lines = []
    for conn in connectors():
        status = _read(os.path.join(conn.path, "status"))
        mode = best_mode(conn) or "-"
        lines.append(f"  {conn.name}: {status} (best mode {mode})")
    return "\n".join(lines) or "  no HDMI connectors found"
