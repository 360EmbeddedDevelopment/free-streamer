"""Run one stream.py at a time, on behalf of whoever tapped a button.

Flask owns no playback logic: it shells out to stream.py exactly as a terminal
would. That is deliberate. Supervisor.install_signal_handlers() calls
signal.signal(), which only works on the main thread, so the supervisor cannot
be driven from a request thread - and a child process also keeps a browser
crash-loop away from the web server.

Only one stream can play anyway: the kiosk profile is locked to a single Firefox
(--no-remote), so switching streams means stopping the old child and *waiting*
for it to die before starting the next.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass

from streamplayer import firefox

from .catalog import REPO, Stream

STREAM_PY = os.path.join(REPO, "stream.py")
LOG_LINES = 300
# The supervisor gives Firefox 5s to go on SIGTERM before it kills it, so allow
# for that plus a moment to notice and exit.
STOP_GRACE = 8.0
GROUP_GRACE = 3.0

# State names the page renders.
IDLE = "idle"
STARTING = "starting"
PLAYING = "playing"
STOPPING = "stopping"
FAILED = "failed"

# What the child's own log says about how it is getting on. These are the lines
# streamplayer emits; matching them is what turns "a process is alive" into a
# status worth showing someone.
PLAYING_MARK = "playing fullscreen"
TROUBLE_MARKS = ("ERROR", "gave up", "no video on the page")


@dataclass
class _Current:
    stream: Stream
    proc: subprocess.Popen
    started: float
    # Which of the button's links this is, so the page can say "2 of 3".
    index: int = 0


class StreamManager:
    """The one running stream, and everything the page knows about it.

    Two locks, and the distinction matters. `_action` serializes play/stop and
    is held across process waits, so it can be busy for seconds at a time.
    `_state` only ever guards a few assignments. The log reader takes the second
    and never the first: if it could block on `_action` it would stop draining
    the child's pipe, and a full pipe stops the child exiting - which is exactly
    what stop() would be waiting for.
    """

    def __init__(self, profile: str | None = None):
        self._action = threading.Lock()
        self._state_lock = threading.Lock()
        # deque.append is atomic, so the reader needs no lock for the log itself.
        self._log: deque[str] = deque(maxlen=LOG_LINES)
        self._current: _Current | None = None
        self._state = IDLE
        self._message = ""
        self._last_exit: int | None = None
        # Which button is part-way through its links, and where it got to. One
        # pair rather than a map: starting a different stream resets everything
        # else, so only ever one button owns a position. It deliberately
        # outlives _current - a stream that just died is exactly when someone
        # taps again for the next link.
        self._cycle_id: str | None = None
        self._cycle_index = 0
        self.profile = profile or firefox.FIREFOX_PROFILE

    # -- queries ---------------------------------------------------------

    def status(self) -> dict:
        """A snapshot for the page. Never blocks on a running play/stop."""
        with self._state_lock:
            current = self._current
            state, message, last_exit = self._state, self._message, self._last_exit
            cycle_id, cycle_index = self._cycle_id, self._cycle_index
        return {
            "state": state,
            "message": message,
            "stream_id": current.stream.id if current else None,
            "label": current.stream.label if current else None,
            # The link actually playing, which is not the first one once a
            # button has been tapped more than once.
            "url": current.stream.url_at(current.index) if current else None,
            "url_index": current.index if current else None,
            "url_count": len(current.stream.urls) if current else None,
            # Where the cycling button has got to, whether or not it is still
            # playing, so the page can show what the next tap will do.
            "cycle_id": cycle_id,
            "cycle_index": cycle_index,
            "since": current.started if current else None,
            "elapsed": round(time.time() - current.started) if current else None,
            "pid": current.proc.pid if current else None,
            "exit_code": last_exit,
            "log": list(self._log),
        }

    # -- actions ---------------------------------------------------------

    def _next_index(self, stream: Stream) -> int:
        """Which of `stream`'s links the next press should play.

        Pressing the same button again steps along; pressing a different one
        starts that button at its first link and abandons whatever position the
        last button held. Stopping is not a reset: a stream that just failed is
        the usual reason to reach for the next link.

        Works the index out without recording it, so the caller can commit only
        once the stream is really running.
        """
        count = len(stream.urls) or 1
        with self._state_lock:
            if self._cycle_id != stream.id:
                return 0
            # The modulo is also what keeps a single-link button pinned to its
            # only link, and an index in range after the list was shortened.
            return (self._cycle_index + 1) % count

    def play(self, stream: Stream, defaults: dict | None = None) -> dict:
        """Switch to `stream`, stopping whatever is playing first.

        `defaults` are the Advanced tab's playback settings; the stream's own
        values win over them (see catalog.Stream.flags).
        """
        with self._action:
            self._stop_locked()

            stray = self.stray_firefox()
            if stray:
                self._set(
                    FAILED,
                    f"another Firefox (pid {stray}) is using the kiosk profile - "
                    "close it, or stop the ./stream.py running in a terminal",
                )
                return self.status()

            # Which link this tap plays: the next one along if this button is
            # already part-way through, otherwise back to the first. Worked out
            # here but not recorded until the child is actually running, so a
            # refusal below does not silently use up a link nobody saw.
            index = self._next_index(stream)

            self._log.clear()
            cmd = [sys.executable, STREAM_PY, *stream.flags(defaults, index)]
            self._log.append(f"$ ./stream.py {' '.join(cmd[2:])}")
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=REPO,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
                    bufsize=1,
                    # Its own process group, so the whole tree can be signalled
                    # together and a Ctrl-C on the web server does not reach the
                    # game on the TV.
                    start_new_session=True,
                )
            except OSError as exc:
                self._set(FAILED, f"could not start stream.py: {exc}")
                return self.status()

            with self._state_lock:
                self._current = _Current(
                    stream=stream, proc=proc, started=time.time(), index=index
                )
                # Recorded here, beside _current, so a poller can never catch a
                # new stream_id still paired with the old position.
                self._cycle_id, self._cycle_index = stream.id, index
                self._state = STARTING
                self._message = f"starting {stream.label}"
                self._last_exit = None
            threading.Thread(target=self._drain, args=(proc,), daemon=True).start()
            return self.status()

    def stop(self) -> dict:
        with self._action:
            self._stop_locked()
        return self.status()

    def shutdown(self) -> None:
        """Stop the stream when the web server itself is going away."""
        self.stop()

    # -- internals -------------------------------------------------------

    def _set(self, state: str, message: str) -> None:
        with self._state_lock:
            self._state = state
            self._message = message

    def _stop_locked(self) -> None:
        """Take the current stream down. Caller holds `_action`."""
        with self._state_lock:
            current = self._current
            self._current = None
        if current is None:
            self._set(IDLE, "")
            return

        proc = current.proc
        if proc.poll() is not None:
            self._finish(proc.returncode)
            return

        self._set(STOPPING, f"stopping {current.stream.label}")
        # SIGTERM to stream.py alone first: its supervisor then shuts Firefox
        # down the clean way, which leaves the profile unlocked for the next
        # stream. Escalating to the group is the fallback, not the opener.
        proc.terminate()
        code = self._wait(proc, STOP_GRACE)
        if code is None:
            self._log.append("stream.py did not exit, signalling its process group")
            self._signal_group(proc, signal.SIGTERM)
            code = self._wait(proc, GROUP_GRACE)
        if code is None:
            self._log.append("killing the process group")
            self._signal_group(proc, signal.SIGKILL)
            code = self._wait(proc, GROUP_GRACE)

        with self._state_lock:
            self._last_exit = code
            self._state = IDLE
            self._message = "stopped"

    def _finish(self, code: int | None) -> None:
        with self._state_lock:
            self._current = None
            self._last_exit = code
            self._state = FAILED if code else IDLE
            self._message = f"stream.py exited with code {code}" if code else "stopped"

    @staticmethod
    def _wait(proc: subprocess.Popen, timeout: float) -> int | None:
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    @staticmethod
    def _signal_group(proc: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except OSError:
            pass  # already gone

    def stray_firefox(self) -> int | None:
        """A Firefox on the kiosk profile that is not ours, if one exists.

        Two Firefoxes cannot share a profile: the second exits at once, which
        the supervisor reports as a bad URL. Catching it here says what is
        actually wrong - usually a ./stream.py still running in a terminal.
        """
        try:
            out = subprocess.run(
                # The "--" is required: the pattern itself starts with "--",
                # and pgrep would read it as an option of its own.
                ["pgrep", "-a", "-f", "--", f"--profile {self.profile}"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        # -f matches whole command lines, so anything merely *mentioning* the
        # profile path - a shell running pgrep, an editor, this very check in
        # another form - comes back too. Only a real browser counts.
        for line in out.splitlines():
            pid, _, command = line.partition(" ")
            if pid.isdigit() and "firefox" in command.split()[0]:
                return int(pid)
        return None

    def _drain(self, proc: subprocess.Popen) -> None:
        """Feed the child's log into the page, reading its state out of it.

        Runs for the life of one child. It must never take `_action` - see the
        class docstring.
        """
        if proc.stdout is not None:
            try:
                for raw in proc.stdout:
                    line = raw.rstrip("\n")
                    if not line.strip():
                        continue
                    self._log.append(line)
                    self._note_progress(proc, line)
            except (OSError, ValueError):
                pass  # pipe closed as the child exited
            finally:
                try:
                    proc.stdout.close()
                except (OSError, ValueError):
                    pass

        code = proc.wait()
        with self._state_lock:
            if self._current is not None and self._current.proc is proc:
                self._current = None
                self._last_exit = code
                self._state = FAILED if code else IDLE
                self._message = (
                    f"stream.py exited with code {code}" if code else "the stream ended"
                )

    def _note_progress(self, proc: subprocess.Popen, line: str) -> None:
        with self._state_lock:
            if self._current is None or self._current.proc is not proc:
                return  # a newer stream owns the page now
            if PLAYING_MARK in line:
                self._state = PLAYING
                self._message = f"playing {self._current.stream.label}"
            elif self._state in (STARTING, PLAYING) and any(m in line for m in TROUBLE_MARKS):
                self._message = line
