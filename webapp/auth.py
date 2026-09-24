"""The login that guards the Advanced tab.

Only the Advanced tab: playing a stream and editing the buttons stay open to
anyone on the wifi, the same as the play buttons always were. What is behind the
password is the stuff that can change how every stream launches, restart the
service, or turn the Pi off.

This is HTTP on a home LAN. The password crosses the wifi in the clear and sits
in config.json in plain text, so it should not be a password used anywhere else.
What is worth defending against - a housemate guessing, a script hammering the
form - is covered: constant-time comparison, and a lockout after a few misses.
"""

from __future__ import annotations

import functools
import hmac
import threading
import time

from flask import current_app, jsonify, request, session

from . import config

SESSION_KEY = "admin"
# Enough tries for a typo, few enough to make guessing pointless.
MAX_FAILURES = 5
LOCKOUT_SECONDS = 300

_lock = threading.Lock()
# address -> (failures, locked_until). In memory only: a restart forgives.
_failures: dict[str, tuple[int, float]] = {}


def _config() -> config.Config:
    return config.load(current_app.config["CONFIG_PATH"])


def _caller() -> str:
    return request.remote_addr or "unknown"


def lockout_remaining(address: str | None = None) -> int:
    """Seconds left on this caller's lockout, 0 when they may try again."""
    address = address or _caller()
    with _lock:
        _, until = _failures.get(address, (0, 0.0))
    return max(0, int(until - time.time()))


def _record_failure(address: str) -> None:
    with _lock:
        count, _ = _failures.get(address, (0, 0.0))
        count += 1
        until = time.time() + LOCKOUT_SECONDS if count >= MAX_FAILURES else 0.0
        _failures[address] = (count, until)


def _clear_failures(address: str) -> None:
    with _lock:
        _failures.pop(address, None)


def is_admin() -> bool:
    """True when this browser has logged in and credentials are still set.

    Checked against the file every time rather than trusted from the cookie:
    deleting the auth block in config.json should lock the tab immediately, not
    at the end of somebody's session.
    """
    return bool(session.get(SESSION_KEY)) and _config().has_credentials


def log_in(username: str, password: str) -> tuple[bool, str]:
    """Check credentials and start a session. Returns (ok, message)."""
    address = _caller()
    remaining = lockout_remaining(address)
    if remaining:
        return False, f"too many attempts - try again in {remaining // 60 + 1} minute(s)"

    cfg = _config()
    if not cfg.has_credentials:
        return False, "no login is configured in config.json"

    # compare_digest on both halves, so a wrong username costs the same as a
    # wrong password and neither leaks by timing.
    ok_user = hmac.compare_digest(username.strip(), cfg.username)
    ok_pass = hmac.compare_digest(password, cfg.password)
    if not (ok_user and ok_pass):
        _record_failure(address)
        left = MAX_FAILURES - _failures.get(address, (0, 0.0))[0]
        if left > 0:
            return False, f"wrong username or password ({left} attempt(s) left)"
        return False, f"too many attempts - locked for {LOCKOUT_SECONDS // 60} minutes"

    _clear_failures(address)
    session.permanent = True
    session[SESSION_KEY] = True
    return True, "signed in"


def log_out() -> None:
    session.pop(SESSION_KEY, None)


def require_admin(view):
    """Refuse the request unless this browser has logged in."""

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not is_admin():
            cfg = _config()
            reason = (
                "the Advanced tab is locked: no login is configured in config.json"
                if not cfg.has_credentials
                else "sign in to use the Advanced tab"
            )
            return jsonify({"error": reason, "configured": cfg.has_credentials}), 403
        return view(*args, **kwargs)

    return wrapped


def require_fetch(view):
    """Require the header a browser form cannot send cross-site.

    The session is a cookie, so without this a page on another site could POST
    to these routes and the browser would attach it. Every call from our own
    JavaScript sets the header; a cross-site <form> cannot.
    """

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if request.headers.get("X-Requested-With") != "stream-panel":
            return jsonify({"error": "missing X-Requested-With header"}), 400
        return view(*args, **kwargs)

    return wrapped
