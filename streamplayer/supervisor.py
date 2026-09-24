"""Keep the browser running across stream drops.

A live feed ends for all sorts of uninteresting reasons - the CDN rotates a
token, the wifi blips, the source restarts its encoder, the page wedges. In each
case the fix is the same: build the launch again (which re-reads the page, so an
expired token is refreshed) and start over, backing off so a genuinely dead
source doesn't turn into a hot loop.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .firefox import Launch

log = logging.getLogger("stream")

# Long enough that a stream which played fine is treated as a drop, not a failure.
HEALTHY_RUNTIME = 60.0
BACKOFF_START = 2.0
BACKOFF_MAX = 30.0
# How long the player gets to exit on SIGTERM before it is killed.
TERM_GRACE = 5.0


class _NoiseFilter(threading.Thread):
    """Forwards a player's output, dropping lines that always mean nothing.

    Browsers log hundreds of harmless GTK, GL and network-probe lines per
    minute, which is enough to hide a real error scrolling past. Only patterns
    the player is known to emit for no reason are dropped, so anything
    unexpected still reaches the terminal.
    """

    def __init__(self, stream, patterns: tuple[str, ...]):
        super().__init__(daemon=True)
        self.stream = stream
        self.pattern = re.compile("|".join(patterns))
        self.suppressed = 0
        self._blank_after_drop = False

    def run(self) -> None:
        try:
            for raw in self.stream:
                line = raw.rstrip("\n")
                if self.pattern.search(line):
                    self.suppressed += 1
                    # The shader dumps put a blank line after each message.
                    self._blank_after_drop = True
                    continue
                if not line.strip() and self._blank_after_drop:
                    self.suppressed += 1
                    continue
                self._blank_after_drop = False
                sys.stderr.write(line + "\n")
                sys.stderr.flush()
        except (OSError, ValueError):
            pass  # pipe closed as the player exited
        finally:
            try:
                self.stream.close()
            except (OSError, ValueError):
                pass


class Supervisor:
    """Runs a player process, restarting it until told to stop."""

    def __init__(self, build_launch: Callable[[], "Launch"], retries: int = -1):
        """build_launch is called before every launch, so nothing is stale.

        It returns anything with `cmd` and `env` attributes (see firefox.Launch).
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
            except Exception as exc:  # building the launch failed - may be transient
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
            # -v means "show me everything", so the filter only runs without it.
            noise = getattr(launch, "noise", ()) if not log.isEnabledFor(logging.DEBUG) else ()
            piped = (
                {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "text": True, "errors": "replace"}
                if noise
                else {}
            )
            try:
                self.child = subprocess.Popen(launch.cmd, env=env, **piped)
            except OSError as exc:
                log.error("could not start player: %s", exc)
                return 1

            sieve = None
            if noise and self.child.stdout is not None:
                sieve = _NoiseFilter(self.child.stdout, noise)
                sieve.start()

            # Driving the page - clicking the poster, going fullscreen - runs
            # alongside the browser rather than blocking the wait() below, and
            # never takes the stream down on failure.
            if launch.post_start is not None:
                threading.Thread(target=launch.post_start, daemon=True).start()

            code = self.child.wait()
            if sieve is not None:
                sieve.join(timeout=2.0)
                if sieve.suppressed:
                    log.info(
                        "hid %d harmless player log lines (-v shows them)", sieve.suppressed
                    )
            ran_for = time.monotonic() - started
            self.child = None
            self._cancel_kill_timer()

            if self.stopping:
                break

            if code == 0 and ran_for < 5:
                # Exited immediately and cleanly. Retrying won't help, so the
                # only useful thing left is to say which of the two causes it
                # was - and one of them is knowable.
                from .firefox import profile_owner

                owner = profile_owner(getattr(launch, "profile", ""))
                if owner is not None:
                    log.error(
                        "another stream is already using this browser profile "
                        "(pid %d), so this one exited on the spot. Stop that one "
                        "first - the control panel's stop button, or: kill %d",
                        owner,
                        owner,
                    )
                else:
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
