"""Detect the HDMI output and the session that owns it.

The Pi exposes each HDMI port as a DRM connector under /sys/class/drm. The
names are not stable across boots or board revisions, so the connected port -
and the session env a browser needs to reach it - is discovered at runtime.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

DRM_PATH = "/sys/class/drm"
MODE_RE = re.compile(r"^(\d+)x(\d+)")

# Which display stack owns the screen. Firefox needs one of the first two: it
# draws as a client of a compositor or an X server and cannot take over a bare
# console, which is what BACKEND_DRM means.
BACKEND_X11 = "x11"
BACKEND_WAYLAND = "wayland"
BACKEND_DRM = "drm"


class NoDisplayError(RuntimeError):
    """No HDMI output has a display attached."""


@dataclass
class Connector:
    name: str  # DRM connector name, e.g. "HDMI-A-2"
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


def connected_connectors() -> list[Connector]:
    """Every HDMI port with a display on it - for asking which one is the TV."""
    return [c for c in connectors() if _read(os.path.join(c.path, "status")) == "connected"]


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
    """Name of a running Wayland display, or None.

    $WAYLAND_DISPLAY is only believed if its socket actually exists. The name
    depends on the compositor and the order things started - wayfire and labwc
    do not agree - so an inherited or hard-coded value can easily point at a
    socket nobody created, and trusting it would aim the browser at nothing.
    """
    named = os.environ.get("WAYLAND_DISPLAY")
    if named and os.path.exists(os.path.join(_runtime_dir(), named)):
        return named
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
    """Which display stack is running, if any.

    A compositor holds DRM master exclusively, so finding one means the screen
    is already owned and a browser can be a client of it. BACKEND_DRM is the
    bare-console case: nothing owns the screen, and Firefox cannot draw there.
    """
    if wayland_socket():
        return BACKEND_WAYLAND
    if x_display():
        return BACKEND_X11
    return BACKEND_DRM


def session_env(backend: str) -> dict[str, str]:
    """Env vars the browser needs to reach the session, for SSH-launched runs."""
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


_WLR_MODE = re.compile(r"^\s+(\d+)x(\d+) px, ([\d.]+) Hz(.*)$")
_X_OUTPUT = re.compile(r"^(\S+) connected")
_X_MODE = re.compile(r"^\s+(\d+)x(\d+)i?\s+(.*)$")


def _output_modes(name: str, backend: str, env: dict[str, str]) -> list[tuple[int, int, str, bool]]:
    """(width, height, refresh as the tool prints it, is-current) for one output."""
    tool = ["wlr-randr"] if backend == BACKEND_WAYLAND else ["xrandr", "--query"]
    out = subprocess.run(tool, capture_output=True, text=True, env={**os.environ, **env}, timeout=10).stdout
    modes: list[tuple[int, int, str, bool]] = []
    mine = False
    for line in out.splitlines():
        if line and not line[0].isspace():
            head = _X_OUTPUT.match(line) if backend == BACKEND_X11 else None
            mine = (head.group(1) if head else line.split()[0]) == name
            continue
        if not mine:
            continue
        if backend == BACKEND_WAYLAND:
            m = _WLR_MODE.match(line)
            if m:
                modes.append((int(m.group(1)), int(m.group(2)), m.group(3), "current" in m.group(4)))
        else:
            m = _X_MODE.match(line)
            if m:
                w, h = int(m.group(1)), int(m.group(2))
                for rate in m.group(3).split():
                    modes.append((w, h, rate.rstrip("*+"), "*" in rate))
    return modes


def set_mode(conn: Connector, backend: str, mode: str) -> str:
    """Switch the output to `mode` ("1920x1080") before a stream starts.

    Returns what happened, for the log. Never raises: a screen left at the
    wrong resolution plays badly, but a screen that fails to switch should not
    stop the stream. Leaves the screen alone when it is already at that size -
    every switch blanks the TV for a moment - and picks the best refresh at or
    below 60Hz, since a bare size can land on a TV's 24Hz film mode.
    """
    m = MODE_RE.match(mode or "")
    if not m:
        return f"not switching: {mode!r} is not a WIDTHxHEIGHT mode"
    if backend not in (BACKEND_WAYLAND, BACKEND_X11):
        return "not switching: no display session to switch"
    tool = "wlr-randr" if backend == BACKEND_WAYLAND else "xrandr"
    if not shutil.which(tool):
        return f"not switching: {tool} is not installed (sudo apt install {tool})"

    width, height = int(m.group(1)), int(m.group(2))
    name, env = output_name(conn, backend), session_env(backend)
    try:
        modes = _output_modes(name, backend, env)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"not switching: could not read the modes ({exc})"
    if any(w == width and h == height and cur for w, h, _, cur in modes):
        return f"{name} already at {mode}"
    same = [(float(r), r) for w, h, r, _ in modes if w == width and h == height]
    if not same:
        return f"not switching: {name} does not offer {mode}"
    # 50-60Hz suits broadcast sport. Failing that, the lowest rate above it (a
    # 120Hz mode still shows every frame), and only then something slower -
    # 24Hz would judder badly with 60fps football.
    broadcast = [x for x in same if 49.0 <= x[0] <= 60.5]
    faster = [x for x in same if x[0] > 60.5]
    rate = (max(broadcast) if broadcast else min(faster) if faster else max(same))[1]

    cmd = ([tool, "--output", name, "--mode", f"{width}x{height}@{rate}"] if backend == BACKEND_WAYLAND
           else [tool, "--output", name, "--mode", f"{width}x{height}", "--rate", rate])
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, **env}, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"not switching: {tool} failed ({exc})"
    if done.returncode != 0:
        return f"not switching: {tool} said {done.stderr.strip() or done.returncode}"
    return f"switched {name} to {width}x{height} at {float(rate):.2f}Hz"


def output_name(conn: Connector, backend: str) -> str:
    """The connector's name in the given display stack.

    X drops the DRM connector-type letter: DRM's HDMI-A-2 is X's HDMI-2.
    Wayland compositors keep the DRM name as-is. Informational here - Firefox
    has no output-selection flag; the compositor places its window.
    """
    if backend == BACKEND_X11:
        return conn.name.replace("HDMI-A-", "HDMI-")
    return conn.name


def describe() -> str:
    """Human-readable summary of the detected display setup."""
    lines = []
    for conn in connectors():
        status = _read(os.path.join(conn.path, "status"))
        mode = best_mode(conn) or "-"
        lines.append(f"  {conn.name}: {status} (best mode {mode})")
    return "\n".join(lines) or "  no HDMI connectors found"
