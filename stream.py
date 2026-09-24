#!/usr/bin/env python3
"""Play a stream page full-screen on the HDMI-attached TV.

Every stream is opened in a kiosk Firefox driven from here: the page is loaded,
the video found, clicked and put fullscreen, and the whole thing restarted if it
dies. Which site the URL belongs to decides how that is done - see
streamplayer/sites/.

Examples:

    # A match page on a supported site (see --list-sites)
    ./stream.py 'https://example.com/some-match'

    # Show what would run, without launching anything
    ./stream.py --dry-run 'https://example.com/some-match'

    # Which sites are supported, and which HDMI port has the TV
    ./stream.py --list-sites
    ./stream.py --list-displays

Point it at streams you are authorized to watch.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from streamplayer import __version__, display, firefox, sites, supervisor

# This Pi's own answers about its screen, written by install.sh (the "display"
# section of the same config.json the control panel uses). Nothing about any
# one Pi or TV lives in the code: a clone on another Pi asks its installer.
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def saved_display() -> dict:
    """{"connector": ..., "mode": ...} from config.json, or {} if unset."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            section = json.load(fh).get("display")
    except (OSError, ValueError, AttributeError):
        return {}
    return section if isinstance(section, dict) else {}

log = logging.getLogger("stream")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="stream.py",
        description="Play a stream page full-screen on a Raspberry Pi HDMI output.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run --list-sites for supported sites, --list-displays for the TV.",
    )
    p.add_argument("url", nargs="?", help="stream page URL on a supported site (see --list-sites)")
    p.add_argument(
        "--site",
        help="force a site handler instead of matching on the URL's domain "
        "(see --list-sites)",
    )
    p.add_argument(
        "--connector",
        help="HDMI connector to use, e.g. HDMI-A-1 (default: the one install.sh "
        "saved, else whichever has a display on it)",
    )
    p.add_argument(
        "--mode",
        help="switch the screen to WIDTHxHEIGHT before playing, or 'native' to "
        "leave it alone (default: what install.sh saved)",
    )
    p.add_argument(
        "--backend",
        choices=("auto", "x11", "wayland"),
        default="auto",
        help="display stack to reach the screen through (default: auto-detect)",
    )
    p.add_argument(
        "--profile",
        default=firefox.FIREFOX_PROFILE,
        help=f"kiosk Firefox profile (default: {firefox.FIREFOX_PROFILE})",
    )
    p.add_argument(
        "--retries",
        type=int,
        default=-1,
        help="restarts after a drop: -1 forever (default), 0 none, N at most N",
    )
    p.add_argument(
        "--no-autoplay",
        action="store_false",
        dest="autoplay",
        help="leave the video for you to start and fullscreen by hand",
    )
    p.add_argument(
        "--firefox-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="pass an extra argument to firefox (repeatable)",
    )
    p.add_argument("--log-file", help="also append logs to this file")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the browser command without launching it",
    )
    p.add_argument(
        "--list-sites",
        action="store_true",
        help="show the streaming sites this player knows how to drive",
    )
    p.add_argument(
        "--list-displays",
        action="store_true",
        help="show HDMI connectors, their status, and the display backend",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


BACKEND_NOTES = {
    "x11": "an X session owns the display; Firefox draws as a client of it",
    "wayland": "a Wayland compositor owns the display; Firefox draws as a client",
    "drm": "no session detected - Firefox cannot draw to a bare console",
}


def list_sites() -> int:
    print("Supported sites:")
    print(sites.describe())
    return 0


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
    print(f"Display backend: {backend} - {BACKEND_NOTES[backend]}")
    print(f"Output name here: {display.output_name(conn, backend)}")
    env = display.session_env(backend)
    if env:
        print("Session env: " + " ".join(f"{k}={v}" for k, v in sorted(env.items())))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    supervisor.setup_logging(verbose=args.verbose, log_file=args.log_file)

    if args.list_sites:
        return list_sites()
    if args.list_displays:
        return list_displays()

    if not args.url:
        build_parser().print_usage(sys.stderr)
        print("stream.py: error: a URL is required (or use --list-sites)", file=sys.stderr)
        return 2

    saved = saved_display()
    try:
        conn = display.get_connector(args.connector or saved.get("connector") or None)
    except display.NoDisplayError as exc:
        log.error("%s", exc)
        return 1

    backend = display.detect_backend() if args.backend == "auto" else args.backend
    mode = args.mode if args.mode is not None else (saved.get("mode") or "")
    if mode == "native":
        mode = ""

    cfg = firefox.Config(
        connector=conn,
        backend=backend,
        profile=args.profile,
        autoplay=args.autoplay,
        extra_args=args.firefox_arg,
    )

    try:
        site = (
            sites.by_name(args.site, args.url, cfg)
            if args.site
            else sites.for_url(args.url, cfg)
        )
    except sites.UnknownSite as exc:
        log.error("%s", exc)
        return 1

    log.info("%s on %s via %s", site, cfg.output_name(), backend)

    # Before the browser, never under it: changing mode beneath a playing video
    # leaves the compositor presenting frames badly. Once per run is enough -
    # the supervisor's restarts reuse the same screen.
    if mode and args.dry_run:
        log.info("would switch %s to %s first", cfg.output_name(), mode)
    elif mode:
        log.info("%s", display.set_mode(conn, backend, mode))

    if args.dry_run:
        try:
            launch = site.launch()
        except firefox.FirefoxUnavailable as exc:
            log.error("%s", exc)
            return 1
        print(firefox.describe_command(launch))
        return 0

    # site.launch() is called before every attempt, not once: a page URL can
    # carry a token that has expired by the time we reconnect.
    sup = supervisor.Supervisor(site.launch, retries=args.retries)
    sup.install_signal_handlers()
    try:
        return sup.run()
    except firefox.FirefoxUnavailable as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
