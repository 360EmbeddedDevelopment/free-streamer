"""Keep the player running across stream drops, and leave the console usable on exit.

A live feed ends for all sorts of uninteresting reasons - the CDN rotates a token,
the wifi blips, the source restarts its encoder. In each case the fix is the same:
re-resolve the URL and start the player again, backing off so a genuinely dead
source doesn't turn into a hot loop.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .players import Launch

log = logging.getLogger("stream")

# Long enough that a stream which played fine is treated as a drop, not a failure.
HEALTHY_RUNTIME = 60.0
BACKOFF_START = 2.0
BACKOFF_MAX = 30.0
# How long the player gets to exit on SIGTERM before it is killed.
TERM_GRACE = 5.0


class Supervisor:
    """Runs a player process, restarting it until told to stop."""

    def __init__(self, build_launch: Callable[[], "Launch"], retries: int = -1):
        """build_launch is called before every launch so URLs are re-resolved fresh.

        It returns anything with `cmd` and `env` attributes (see players.Launch).
        retries: -1 runs forever, 0 means a single attempt, N allows N restarts.
        """
        self.build_launch = build_launch
        self.retries = retries
        self.child: subprocess.Popen | None = None
        self.stopping = False
        self._kill_timer: threading.Timer | None = None

    # -- signal handling -------------------------------------------------

    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, self._on_signal)

    def _on_signal(self, signum, _frame) -> None:
        name = signal.Signals(signum).name
        log.info("received %s, shutting down", name)
        self.stopping = True
        self._request_stop()

    def _request_stop(self) -> None:
        """Ask the player to exit, escalating to SIGKILL if it won't.

        Nothing here may block. This runs in a signal handler, which interrupts
        the main loop mid-wait() - and Popen.wait() is not reentrant, so a
        blocking wait here would deadlock against the one already in progress
        and only unwedge on timeout. terminate()/kill() are safe: they poll
        non-blockingly and then just send the signal. The main loop's own wait()
        does the reaping.
        """
        child = self.child
        if child is None or child.poll() is not None:
            return
        child.terminate()
        timer = threading.Timer(TERM_GRACE, self._force_kill, args=(child,))
        timer.daemon = True
        timer.start()
        self._kill_timer = timer

    def _force_kill(self, child: subprocess.Popen) -> None:
        if child.poll() is None:
            log.warning("player still running %.0fs after SIGTERM, sending SIGKILL", TERM_GRACE)
            try:
                child.kill()
            except OSError:
                pass

    def _cancel_kill_timer(self) -> None:
        if self._kill_timer is not None:
            self._kill_timer.cancel()
            self._kill_timer = None

    # -- main loop -------------------------------------------------------

    def run(self) -> int:
        """Block until the stream is done or we give up. Returns an exit code."""
        attempt = 0
        backoff = BACKOFF_START

        while not self.stopping:
            try:
                launch = self.build_launch()
            except Exception as exc:  # resolution failed - may be transient
                log.error("%s", exc)
                if not self._may_retry(attempt):
                    return 1
                attempt += 1
                if not self._sleep(backoff):
                    break
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue

            log.info("starting player (attempt %d)", attempt + 1)
            log.debug("command: %s", " ".join(launch.cmd))
            started = time.monotonic()

            env = {**os.environ, **launch.env} if launch.env else None
            try:
                self.child = subprocess.Popen(launch.cmd, env=env)
            except OSError as exc:
                log.error("could not start player: %s", exc)
                return 1

            code = self.child.wait()
            ran_for = time.monotonic() - started
            self.child = None
            self._cancel_kill_timer()

            if self.stopping:
                break

            if code == 0 and ran_for < 5:
                # Exited immediately and cleanly: almost always an empty playlist
                # or a URL the player silently refused. Retrying won't help.
                log.error("player exited immediately with no error - check the URL")
                return 1

            log.info("player exited with code %d after %.0fs", code, ran_for)

            if not self._may_retry(attempt):
                return 0 if code == 0 else code

            # A stream that played for a while then stopped is a drop, not a
            # broken config, so reset the backoff and reconnect promptly.
            if ran_for >= HEALTHY_RUNTIME:
                backoff = BACKOFF_START
                attempt = 0
            else:
                attempt += 1
                backoff = min(backoff * 2, BACKOFF_MAX)

            if not self._sleep(backoff):
                break

        return 0

    def _may_retry(self, attempt: int) -> bool:
        if self.retries < 0:
            return True
        if attempt >= self.retries:
            if self.retries > 0:
                log.error("giving up after %d retries", self.retries)
            return False
        return True

    def _sleep(self, seconds: float) -> bool:
        """Sleep, returning False if we were asked to stop while waiting."""
        log.info("reconnecting in %.0fs", seconds)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.stopping:
                return False
            time.sleep(0.25)
        return not self.stopping


def restore_console() -> None:
    """Undo what a KMS player does to the tty: hidden cursor, leftover frame."""
    if not sys.stdout.isatty():
        return
    # Show cursor, reset attributes.
    sys.stdout.write("\033[?25h\033[0m")
    sys.stdout.flush()
    try:
        subprocess.run(["stty", "sane"], check=False, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def setup_logging(verbose: bool = False, log_file: str | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        path = os.path.expanduser(log_file)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        handlers.append(logging.FileHandler(path))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )
