"""Build the player command lines.

Two backends:

  mpv      - renders straight to KMS/DRM, so it needs no X or Wayland session.
             This is the default and the only one that reaches full 4K reliably.
  chromium - a kiosk browser inside a throwaway labwc session, for services that
             need a login and Widevine DRM (mpv cannot play those at all).
"""

from __future__ import annotations

import os
import shlex
import shutil
from dataclasses import dataclass, field

from . import display
from .display import Connector, best_mode, hdmi_audio_device
from .resolver import Resolved

KIOSK_PROFILE = os.path.expanduser("~/.config/stream-kiosk")


@dataclass
class Launch:
    """A command to run, plus env vars to merge over the inherited environment."""

    cmd: list[str]
    env: dict[str, str] = field(default_factory=dict)


class PlayerUnavailable(RuntimeError):
    """The requested player binary is not installed."""


@dataclass
class PlayerConfig:
    connector: Connector
    backend: str = display.BACKEND_DRM
    hwdec: str | None = None
    audio_device: str | None = None
    volume: int = 70
    max_height: int | None = 1080
    low_latency: bool = False
    interactive: bool = False
    drm_mode: str | None = None
    user_agent: str | None = None
    referer: str | None = None
    extra_args: list[str] = field(default_factory=list)

    def resolved_audio_device(self) -> str:
        return self.audio_device or hdmi_audio_device(self.connector)

    def output_name(self) -> str:
        return display.output_name(self.connector, self.backend)

    def resolved_hwdec(self) -> str:
        """Which decoder to offload to.

        mpv's "auto-safe" probes CUDA and VDPAU, which this board will never
        have - each attempt prints loader errors straight to stderr from inside
        libcuda/libvdpau, where mpv's own log level cannot reach them. So name
        the one path that can work instead: "drm" covers HEVC via the Pi's
        rpi-hevc-dec block and quietly falls back to software for H.264, which
        the Pi 5 has no hardware decoder for anyway.
        """
        if self.hwdec:
            return self.hwdec
        return "drm" if display.hevc_decoder_device() else "no"


def _output_args(cfg: PlayerConfig) -> list[str]:
    """The flags that put mpv's picture on the right screen, per display stack."""
    target = cfg.output_name()
    if cfg.backend == display.BACKEND_DRM:
        # Bare console: take the display over directly.
        args = ["--gpu-context=drm", f"--drm-connector={target}"]
        if cfg.drm_mode:
            args.append(f"--drm-mode={cfg.drm_mode}")
        return args

    context = "wayland" if cfg.backend == display.BACKEND_WAYLAND else "x11egl"
    # A compositor owns the screen, so mpv is just a fullscreen client on it.
    # --ontop keeps desktop panels and screensavers from covering the game.
    return [f"--gpu-context={context}", f"--fs-screen-name={target}", "--ontop"]


def mpv_command(media: Resolved, cfg: PlayerConfig) -> Launch:
    """mpv invocation that plays full-screen on the chosen HDMI output."""
    if not shutil.which("mpv"):
        raise PlayerUnavailable("mpv is not installed (try: sudo apt install mpv)")

    cmd = [
        "mpv",
        "--vo=gpu",
        *_output_args(cfg),
        "--fullscreen",
        f"--hwdec={cfg.resolved_hwdec()}",
        f"--audio-device={cfg.resolved_audio_device()}",
        f"--volume={cfg.volume}",
        # Buffer generously: a live feed that stalls briefly should ride it out.
        "--cache=yes",
        "--cache-secs=20",
        "--demuxer-max-bytes=64MiB",
        # Exit when the stream truly ends so the supervisor can re-resolve and retry.
        "--keep-open=no",
        "--idle=no",
        # Let ffmpeg reconnect on transient HTTP drops before mpv gives up.
        "--stream-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_delay_max=5",
        "--msg-level=all=warn",
    ]

    # Shrinks buffers to cut delay; makes a flaky feed stutter more, so opt-in only.
    if cfg.low_latency:
        cmd.append("--profile=low-latency")

    if not cfg.interactive:
        cmd += ["--no-input-default-bindings", "--input-conf=/dev/null"]

    if cfg.user_agent:
        cmd.append(f"--user-agent={cfg.user_agent}")
    if cfg.referer:
        cmd.append(f"--http-header-fields=Referer: {cfg.referer}")

    if media.audio:
        cmd.append(f"--audio-file={media.audio}")

    cmd += cfg.extra_args
    cmd.append("--")
    cmd.append(media.video)
    return Launch(cmd=cmd, env=display.session_env(cfg.backend))


def chromium_command(url: str, cfg: PlayerConfig) -> Launch:
    """Chromium in kiosk mode, for services whose DRM mpv cannot play.

    When a session is already running, Chromium just joins it as a kiosk window.
    On a bare console it cannot draw to KMS at all, so it gets wrapped in a
    throwaway labwc session - labwc runs one command and exits when it does,
    which is the lifecycle the supervisor expects.

    The profile directory persists, so a service login survives reboots. The first
    run needs a keyboard and mouse attached to sign in.
    """
    browser = shutil.which("chromium") or shutil.which("chromium-browser")
    if not browser:
        raise PlayerUnavailable("chromium is not installed")

    # Size to the panel, not to --max-height: that cap exists for mpv's software
    # decode, while the browser does its own scaling and should fill the screen.
    mode = best_mode(cfg.connector)
    if mode:
        width, height = (int(v) for v in mode.split("x"))
    else:
        height = 1080
        width = 1920

    on_x11 = cfg.backend == display.BACKEND_X11
    browser_args = [
        browser,
        "--kiosk",
        "--start-fullscreen",
        f"--window-size={width},{height}",
        "--autoplay-policy=no-user-gesture-required",
        f"--ozone-platform={'x11' if on_x11 else 'wayland'}",
        f"--user-data-dir={KIOSK_PROFILE}",
        # A kiosk has no one to dismiss these.
        "--no-first-run",
        "--disable-session-crashed-bubble",
        "--disable-infobars",
        "--noerrdialogs",
        url,
    ]
    env = display.session_env(cfg.backend)

    if cfg.backend != display.BACKEND_DRM:
        # A session is already running - just open a window on it.
        return Launch(cmd=browser_args, env=env)

    compositor = shutil.which("labwc") or shutil.which("cage") or shutil.which("wayfire")
    if not compositor:
        raise PlayerUnavailable(
            "Kiosk mode on a bare console needs a Wayland compositor "
            "(install labwc, cage, or wayfire), or start the desktop first."
        )
    if os.path.basename(compositor) == "cage":
        return Launch(cmd=[compositor, "--", *browser_args], env=env)

    # labwc / wayfire take a startup command as a single shell string.
    startup = " ".join(_shell_quote(a) for a in browser_args)
    return Launch(cmd=[compositor, "-s", startup], env=env)


def _shell_quote(arg: str) -> str:
    return shlex.quote(arg)


def describe_command(launch: Launch) -> str:
    """The launch as a copy-pasteable shell line, env prefix included."""
    prefix = [f"{k}={_shell_quote(v)}" for k, v in sorted(launch.env.items())]
    return " ".join(prefix + [_shell_quote(a) for a in launch.cmd])
