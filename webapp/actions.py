"""What the Advanced tab can do to this Pi, beyond editing settings.

Every action is a fixed argv looked up by name in ACTIONS below: the request
picks an action, it does not describe one, and nothing the browser sends is ever
interpolated into a command. The one place a shell is involved - the delayed
restart in _detach - builds its line from this module's own constants, quoted.

All of this is behind the Advanced login (see auth.py), power controls included.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import time

from .catalog import REPO

SERVICE = "stream-web.service"
STREAM_PY = os.path.join(REPO, "stream.py")
SETUP_SH = os.path.join(REPO, "setup-firefox.sh")

# Read-only commands whose output the Diagnostics pane shows.
DIAGNOSTICS: dict[str, tuple[str, list[str]]] = {
    "displays": ("HDMI output", ["python3", STREAM_PY, "--list-displays"]),
    "sites": ("Supported sites", ["python3", STREAM_PY, "--list-sites"]),
    "service": (
        "Web service",
        ["systemctl", "--user", "status", SERVICE, "--no-pager", "--lines=15"],
    ),
    "firefox": ("Firefox", ["firefox", "--version"]),
}


class ActionError(RuntimeError):
    """The action could not be run, worded for the person who clicked."""


def _run(cmd: list[str], timeout: float = 60.0) -> str:
    try:
        proc = subprocess.run(
            cmd, cwd=REPO, capture_output=True, text=True, errors="replace", timeout=timeout
        )
    except FileNotFoundError:
        raise ActionError(f"{cmd[0]} is not installed") from None
    except subprocess.TimeoutExpired:
        raise ActionError(f"{cmd[0]} took longer than {timeout:.0f}s and was given up on") from None
    except OSError as exc:
        raise ActionError(f"could not run {cmd[0]}: {exc}") from None

    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise ActionError(out.strip() or f"{cmd[0]} exited with code {proc.returncode}")
    return out.strip()


def _detach(cmd: list[str], delay: float = 2.0) -> None:
    """Run a command that outlives this request - and possibly this process.

    systemd-run puts it in a transient unit of its own, outside our cgroup, so
    stopping or restarting our own service cannot kill the command doing it.

    The delay is a sleep inside that unit rather than systemd-run's --on-active,
    which schedules a timer: a timer combined with --collect gets garbage
    collected mid-command, and the command then never finishes. Tested both
    ways; this is the one that works every time.

    The shell here only ever sees strings this module hard-codes - `cmd` comes
    from ACTIONS, never from a request - and they are quoted regardless.
    """
    script = f"sleep {delay:g}; exec " + " ".join(shlex.quote(part) for part in cmd)
    wrapper = ["systemd-run", "--user", "--collect", "--quiet", "/bin/sh", "-c", script]
    try:
        subprocess.run(wrapper, capture_output=True, text=True, timeout=15, check=True)
    except subprocess.CalledProcessError as exc:
        raise ActionError((exc.stderr or exc.stdout or "systemd-run refused").strip()) from None
    except (OSError, subprocess.SubprocessError) as exc:
        raise ActionError(f"could not schedule the command: {exc}") from None


def restart_web(manager) -> str:
    """Restart the panel's own service, a moment from now."""
    _detach(["systemctl", "--user", "restart", SERVICE])
    return (
        f"restarting {SERVICE} in a moment. Any playing stream stops with it; "
        "this page will reconnect on its own."
    )


def update_ublock(manager) -> str:
    """Re-run setup-firefox.sh, which pulls the current uBlock Origin."""
    if not os.access(SETUP_SH, os.X_OK):
        raise ActionError(f"{SETUP_SH} is missing or not executable")
    return _run([SETUP_SH], timeout=180.0)


def kill_firefox(manager) -> str:
    """Send a stray kiosk Firefox on its way, so the profile unlocks."""
    pid = manager.stray_firefox()
    if pid is None:
        return "no Firefox is holding the kiosk profile"
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return f"Firefox {pid} was already gone"
    except OSError as exc:
        raise ActionError(f"could not stop Firefox {pid}: {exc}") from None

    for _ in range(20):
        time.sleep(0.25)
        try:
            os.kill(pid, 0)
        except OSError:
            return f"stopped Firefox {pid}"
    return f"asked Firefox {pid} to stop, but it is still running"


def reboot(manager) -> str:
    _detach(["sudo", "-n", "systemctl", "reboot"], delay=2.0)
    return "rebooting - this page will come back when the Pi does"


def shutdown(manager) -> str:
    _detach(["sudo", "-n", "systemctl", "poweroff"], delay=2.0)
    return "shutting down - you will need the power button to bring it back"


# name -> (what it does, callable, whether the page must ask twice)
ACTIONS = {
    "restart-web": ("Restart the web service", restart_web, False),
    "update-ublock": ("Update uBlock Origin", update_ublock, False),
    "kill-firefox": ("Kill a stray Firefox", kill_firefox, False),
    "reboot": ("Reboot the Pi", reboot, True),
    "shutdown": ("Shut down the Pi", shutdown, True),
}


def run_action(name: str, manager) -> str:
    entry = ACTIONS.get(name)
    if entry is None:
        raise ActionError(f"unknown action {name!r}")
    return entry[1](manager)


def diagnostics() -> list[dict]:
    """Every diagnostic pane, each with its output or the reason there is none."""
    panes = []
    for key, (title, cmd) in DIAGNOSTICS.items():
        try:
            output = _run(cmd, timeout=30.0)
        except ActionError as exc:
            output = str(exc)
        panes.append({"id": key, "title": title, "output": output})
    return panes
