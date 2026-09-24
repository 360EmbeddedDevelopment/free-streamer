"""The LAN control panel: a button per stream, and one stream at a time.

Two JSON files behind it - streams.json for the buttons (editable by anyone on
the wifi, from the Streams tab) and config.json for the playback defaults and
the Advanced tab's login.
"""

from __future__ import annotations

import atexit
from datetime import timedelta

from flask import Flask

from . import config as appconfig
from .catalog import DEFAULT_PATH
from .manager import StreamManager

__version__ = "1.1.0"


def create_app(streams_path: str = DEFAULT_PATH, config_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["STREAMS_PATH"] = streams_path
    app.config["CONFIG_PATH"] = config_path or appconfig.DEFAULT_PATH

    # Signing key for the Advanced tab's session cookie. It lives in a file so
    # that restarting the service - something the Advanced tab itself can do -
    # does not sign you straight back out.
    app.secret_key = appconfig.secret_key()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    )

    # One manager for the whole process - it *is* the playback state. This is
    # also why serve.py runs with the reloader off: two processes would mean two
    # managers, each unaware of the other's Firefox.
    app.config["MANAGER"] = manager = StreamManager()

    from .views import bp

    app.register_blueprint(bp)
    atexit.register(manager.shutdown)
    return app
