#!/usr/bin/env python3
"""Play a stream full-screen on the HDMI-attached TV.

Examples:

    # A direct HLS feed, auto-detecting the HDMI port and audio device
    ./stream.py 'https://example.com/live/stream.m3u8'

    # A page yt-dlp can resolve, capped at 1080p, printing the command only
    ./stream.py --dry-run 'https://www.youtube.com/watch?v=...'

    # A service that needs a login, via the kiosk browser
    ./stream.py --player chromium 'https://www.nfl.com/plus/'

Point it at sources you are authorized to watch: your own cameras, an IPTV
subscription, YouTube, or a streaming service you have an account with.
"""

from __future__ import annotations

import argparse
import logging
import sys

from streamplayer import __version__, display, players, resolver, supervisor

log = logging.getLogger("stream")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="stream.py",
        description="Play a stream full-screen on a Raspberry Pi HDMI output.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run --list-displays to see which HDMI port has a TV attached.",
    )
    p.add_argument("url", nargs="?", help="stream URL, or a page yt-dlp can resolve")
    p.add_argument(
        "--player",
        choices=("mpv", "chromium"),
        default="mpv",
        help="mpv renders straight to HDMI (default); chromium is for login-gated services",
    )
    p.add_argument(
        "--connector",
        help="DRM connector to use, e.g. HDMI-A-2 (default: the connected one)",
    )
    p.add_argument(
        "--backend",
        choices=("auto", "x11", "wayland", "drm"),
        default="auto",
        help="display stack to render through (default: auto-detect; drm needs a bare console)",
    )
    p.add_argument(
        "--audio-device",
        help="mpv audio device (default: the HDMI port's ALSA card)",
    )
    p.add_argument(
        "--hwdec",
        help="mpv hwdec mode (default: drm when the Pi's HEVC decoder exists, else no)",
    )
    p.add_argument(
        "--max-height",
        type=int,
        default=1080,
        help="cap vertical resolution (default: 1080; the Pi 5 decodes H.264 in software)",
    )
    p.add_argument(
        "--full-res",
        action="store_const",
        const=None,
        dest="max_height",
        help="no resolution cap - use for HEVC 4K sources",
    )
    p.add_argument("--volume", type=int, default=70, help="mpv start volume (default: 70)")
    p.add_argument(
        "--drm-mode",
        help="force a video mode, e.g. 3840x2160 (--backend drm only; default: mpv picks)",
    )
    p.add_argument(
        "--retries",
        type=int,
        default=-1,
        help="restarts after a drop: -1 forever (default), 0 none, N at most N",
    )
    p.add_argument(
        "--low-latency",
        action="store_true",
        help="cut buffering for lower delay; more stutter on a flaky feed",
    )
    p.add_argument(
        "--interactive",
        action="store_true",
        help="keep mpv's keyboard bindings (q to quit, space to pause)",
    )
    p.add_argument("--user-agent", help="override the HTTP User-Agent")
    p.add_argument("--referer", help="send an HTTP Referer header")
    p.add_argument(
        "--mpv-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="pass an extra argument to mpv (repeatable)",
    )
    p.add_argument("--log-file", help="also append logs to this file")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve the URL and print the player command without playing",
    )
    p.add_argument(
        "--list-displays",
        action="store_true",
        help="show HDMI connectors, their status, and the detected audio device",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


BACKEND_NOTES = {
    "x11": "an X session owns the display; mpv plays as a fullscreen client",
    "wayland": "a Wayland compositor owns the display; mpv plays as a client",
    "drm": "no session detected; mpv renders straight to KMS (needs the console)",
}


def list_displays() -> int:
    print("HDMI connectors:")
    print(display.describe())
    try:
        conn = display.find_connected_connector()
    except display.NoDisplayError as exc:
        print(f"\n{exc}")
        return 1
    backend = display.detect_backend()
    print(f"\nActive: {conn.name}")
    print(f"Best mode: {display.best_mode(conn) or 'unknown'}")
    print(f"Audio device: {display.hdmi_audio_device(conn)}")
    print(f"Display backend: {backend} - {BACKEND_NOTES[backend]}")
    print(f"Output name here: {display.output_name(conn, backend)}")
    env = display.session_env(backend)
    if env:
        print("Session env: " + " ".join(f"{k}={v}" for k, v in sorted(env.items())))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    supervisor.setup_logging(verbose=args.verbose, log_file=args.log_file)

    if args.list_displays:
        return list_displays()

    if not args.url:
        build_parser().print_usage(sys.stderr)
        print("stream.py: error: a URL is required (or use --list-displays)", file=sys.stderr)
        return 2

    try:
        conn = display.get_connector(args.connector)
    except display.NoDisplayError as exc:
        log.error("%s", exc)
        return 1

    backend = display.detect_backend() if args.backend == "auto" else args.backend

    cfg = players.PlayerConfig(
        connector=conn,
        backend=backend,
        hwdec=args.hwdec,
        audio_device=args.audio_device,
        volume=args.volume,
        max_height=args.max_height,
        low_latency=args.low_latency,
        interactive=args.interactive,
        drm_mode=args.drm_mode,
        user_agent=args.user_agent,
        referer=args.referer,
        extra_args=args.mpv_arg,
    )

    log.info(
        "output %s via %s, audio %s",
        cfg.output_name(),
        backend,
        cfg.resolved_audio_device(),
    )

    def build_launch() -> players.Launch:
        """Called before each launch, so expiring stream tokens get refreshed."""
        if args.player == "chromium":
            return players.chromium_command(args.url, cfg)
        media = resolver.resolve(args.url, max_height=args.max_height)
        if not media.direct:
            log.info("resolved via yt-dlp%s", " (separate audio)" if media.audio else "")
        return players.mpv_command(media, cfg)

    if args.dry_run:
        try:
            launch = build_launch()
        except (resolver.UnsupportedSource, players.PlayerUnavailable) as exc:
            log.error("%s", exc)
            return 1
        print(players.describe_command(launch))
        return 0

    sup = supervisor.Supervisor(build_launch, retries=args.retries)
    sup.install_signal_handlers()
    try:
        return sup.run()
    except players.PlayerUnavailable as exc:
        log.error("%s", exc)
        return 1
    finally:
        supervisor.restore_console()


if __name__ == "__main__":
    sys.exit(main())
