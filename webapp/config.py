"""config.json - the login for the Advanced tab, and the playback defaults.

Same contract as catalog.py: never raise, hand a broken file back as an error
string for the page to show. Two halves, with different owners:

  auth      - typed in by hand, never written back by the app, never sent to a
              browser. No credentials means the Advanced tab stays locked.
  defaults  - the settings the Advanced tab edits, applied to every stream that
              does not override them.

The password sits in this file in plain text, which is why config.json is
gitignored and config.example.json is the one committed.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import tempfile
import threading
from dataclasses import dataclass, field

from streamplayer import display, firefox

from .catalog import REPO

DEFAULT_PATH = os.path.join(REPO, "config.json")
EXAMPLE_PATH = os.path.join(REPO, "config.example.json")
# Signing key for the session cookie. Kept out of config.json so the app never
# has to rewrite the file the password lives in, and kept on disk rather than
# generated per run so restarting the service - which the Advanced tab can do -
# does not log you straight back out.
SECRET_PATH = os.path.join(REPO, ".webapp-secret")

BACKENDS = ("auto", "wayland", "x11")

FACTORY_DEFAULTS: dict = {
    "autoplay": True,
    "retries": -1,
    "backend": "auto",
    "profile": "",
    "firefox_args": [],
}

_write_lock = threading.Lock()


class ConfigError(RuntimeError):
    """A rejected change to config.json, worded for whoever is editing."""


@dataclass
class Config:
    defaults: dict = field(default_factory=lambda: dict(FACTORY_DEFAULTS))
    username: str = ""
    password: str = ""
    error: str | None = None
    path: str = DEFAULT_PATH
    # Everything that is not "defaults", kept so a save preserves it - the auth
    # block above all.
    extra: dict = field(default_factory=dict)

    @property
    def has_credentials(self) -> bool:
        """Whether an Advanced login is possible at all."""
        return bool(self.username and self.password)

    def public(self) -> dict:
        """What a browser may see: never the credentials themselves."""
        return {
            "defaults": self.defaults,
            "configured": self.has_credentials,
            "error": self.error,
            "path": self.path,
            "exists": os.path.exists(self.path),
            "example": EXAMPLE_PATH,
            "backends": list(BACKENDS),
            "profile_default": firefox.FIREFOX_PROFILE,
            "detected_backend": display.detect_backend(),
        }


def load(path: str = DEFAULT_PATH) -> Config:
    """Read config.json. Never raises; a bad file comes back as .error."""
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        # Not an error: the panel works fine without one, just with the Advanced
        # tab locked and the factory defaults in force.
        return Config(path=path)
    except json.JSONDecodeError as exc:
        return Config(error=f"{os.path.basename(path)} line {exc.lineno}: {exc.msg}", path=path)
    except OSError as exc:
        return Config(error=f"could not read {path}: {exc}", path=path)

    if not isinstance(data, dict):
        return Config(error='expected an object, like {"auth": {...}}', path=path)

    auth = data.get("auth")
    username = password = ""
    error = None
    if isinstance(auth, dict):
        username = str(auth.get("username") or "").strip()
        password = str(auth.get("password") or "")
        if bool(username) != bool(password):
            error = '"auth" needs both a username and a password'
    elif auth is not None:
        error = '"auth" must be an object with a username and a password'

    raw_defaults = data.get("defaults")
    defaults = dict(FACTORY_DEFAULTS)
    if isinstance(raw_defaults, dict):
        try:
            defaults = normalize_defaults(raw_defaults)
        except ConfigError as exc:
            error = error or f'"defaults": {exc}'
    elif raw_defaults is not None:
        error = error or '"defaults" must be an object'

    return Config(
        defaults=defaults,
        username=username,
        password=password,
        error=error,
        path=path,
        extra={k: v for k, v in data.items() if k != "defaults"},
    )


def normalize_defaults(raw: dict) -> dict:
    """Validate a defaults block, filling in anything absent. Raises ConfigError."""
    if not isinstance(raw, dict):
        raise ConfigError("expected an object")
    out = dict(FACTORY_DEFAULTS)

    if "autoplay" in raw:
        if not isinstance(raw["autoplay"], bool):
            raise ConfigError('"autoplay" must be true or false')
        out["autoplay"] = raw["autoplay"]

    if "retries" in raw and raw["retries"] not in ("", None):
        try:
            out["retries"] = int(raw["retries"])
        except (TypeError, ValueError):
            raise ConfigError('"retries" must be a whole number (-1 keeps trying)') from None

    if raw.get("backend"):
        backend = str(raw["backend"]).strip()
        if backend not in BACKENDS:
            raise ConfigError(f'"backend" must be one of {", ".join(BACKENDS)}')
        out["backend"] = backend

    if "profile" in raw:
        profile = str(raw["profile"] or "").strip()
        if profile and not os.path.isabs(os.path.expanduser(profile)):
            raise ConfigError('"profile" must be an absolute path')
        out["profile"] = os.path.expanduser(profile) if profile else ""

    if "firefox_args" in raw:
        extra = raw["firefox_args"] or []
        if isinstance(extra, str):
            extra = extra.split()
        if not isinstance(extra, list):
            raise ConfigError('"firefox_args" must be a list')
        out["firefox_args"] = [str(a).strip() for a in extra if str(a).strip()]

    return out


def save_defaults(defaults: dict, path: str = DEFAULT_PATH) -> Config:
    """Write the defaults block, leaving auth and anything else untouched."""
    checked = normalize_defaults(defaults)
    with _write_lock:
        current = load(path)
        if current.error and not os.path.exists(path):
            raise ConfigError(current.error)

        document = dict(current.extra)  # auth, comments, anything else
        document["defaults"] = checked
        body = json.dumps(document, indent=2, ensure_ascii=False) + "\n"

        directory = os.path.dirname(os.path.abspath(path))
        try:
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".json")
            try:
                with os.fdopen(fd, "w") as fh:
                    fh.write(body)
                    fh.flush()
                    os.fsync(fh.fileno())
                # The file holds a password: keep it off other users' eyes.
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
        except OSError as exc:
            raise ConfigError(f"could not write {path}: {exc}") from None

    return load(path)


def secret_key(path: str = SECRET_PATH) -> bytes:
    """The session signing key, created on first use."""
    try:
        with open(path, "rb") as fh:
            key = fh.read().strip()
            if len(key) >= 32:
                return key
    except OSError:
        pass

    key = secrets.token_bytes(48)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
    except OSError:
        # Read-only checkout, say. Sessions then last until the next restart,
        # which is a worse experience but not a broken one.
        pass
    return key
