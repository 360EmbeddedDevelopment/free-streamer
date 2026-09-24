"""Routes: play the buttons, edit them, and - behind a login - the rest.

Both JSON files are re-read on every request rather than held in memory, so an
edit from the settings page and an edit in vim over SSH are equally live and
neither needs a restart.
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, render_template, request

from streamplayer import sites

from . import actions, auth, catalog, config, unknown
from .auth import require_admin, require_fetch

bp = Blueprint("web", __name__)

# The id an ad-hoc link plays under. Reserved: the page decides which button is
# lit by comparing status.stream_id, so this must never match a real entry.
DIRECT_ID = "__direct__"


def _catalog() -> catalog.Catalog:
    return catalog.load(current_app.config["STREAMS_PATH"])


def _config() -> config.Config:
    return config.load(current_app.config["CONFIG_PATH"])


def _manager():
    return current_app.config["MANAGER"]


def _defaults() -> dict:
    return _config().defaults


def _save(streams: list[catalog.Stream], revision: str | None) -> catalog.Catalog:
    return catalog.save(streams, current_app.config["STREAMS_PATH"], revision=revision)


def _revision_of(body: dict) -> str | None:
    """The catalog version the editor had. None means "do not check"."""
    value = body.get("revision")
    return str(value) if value else None


# -- pages ---------------------------------------------------------------


@bp.get("/")
def index():
    cat = _catalog()
    return render_template(
        "index.html",
        catalog=cat,
        groups=cat.groups(),
        status=_manager().status(),
        defaults=_defaults(),
    )


@bp.get("/settings")
def settings():
    cat = _catalog()
    cfg = _config()
    return render_template(
        "settings.html",
        catalog=cat,
        config=cfg.public(),
        sites=[s.name for s in sites.SITES],
        admin=auth.is_admin(),
        actions=[
            {"name": name, "title": title, "confirm": confirm}
            for name, (title, _fn, confirm) in actions.ACTIONS.items()
        ],
    )


# -- playing -------------------------------------------------------------


@bp.get("/api/streams")
def api_streams():
    return jsonify(_catalog().as_dict(_defaults()))


@bp.get("/api/status")
def api_status():
    return jsonify(_manager().status())


@bp.post("/api/play/<stream_id>")
def api_play(stream_id: str):
    stream = _catalog().get(stream_id)
    if stream is None:
        # The file changed under a page someone had open.
        return jsonify({"error": f"no stream {stream_id!r} in streams.json"}), 404
    return jsonify(_manager().play(stream, _defaults()))


@bp.post("/api/play/direct")
@require_fetch
def api_play_direct():
    """Play a pasted link once, without it becoming a button.

    Unlike /api/play/<id> this writes to disk when it refuses, so it takes the
    same header guard the editing routes use.
    """
    body = request.get_json(silent=True) or {}
    url = str(body.get("url") or "").strip()
    try:
        # Same validation the settings editor gets: blank URLs, embedded spaces
        # and the label/handler derivation all come free.
        stream = catalog.from_form({"url": url, "id": DIRECT_ID})
    except catalog.SaveError as exc:
        return jsonify({"error": str(exc)}), 400

    if stream.handler is None:
        # Nothing in streamplayer/sites claims it, so stream.py would only fail
        # slowly. Park it for later instead of pretending.
        added = unknown.record(stream.url)
        host = unknown.host_of(stream.url) or stream.url
        return (
            jsonify(
                {
                    "error": f"unknown domain {host}",
                    "unknown": True,
                    "queued": added,
                    "host": host,
                }
            ),
            422,
        )

    return jsonify(_manager().play(stream, _defaults()))


@bp.post("/api/stop")
def api_stop():
    return jsonify(_manager().stop())


# -- editing the buttons -------------------------------------------------


@bp.post("/api/streams")
@require_fetch
def api_add():
    body = request.get_json(silent=True) or {}
    cat = _catalog()
    if cat.error:
        return jsonify({"error": f"fix {cat.path} first: {cat.error}"}), 409
    try:
        stream = catalog.from_form(body)
        if cat.get(stream.id):
            return jsonify({"error": f"there is already a stream called {stream.id!r}"}), 409
        saved = _save([*cat.streams, stream], _revision_of(body))
    except catalog.StaleWrite as exc:
        return jsonify({"error": str(exc)}), 409
    except catalog.SaveError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"stream": stream.as_dict(_defaults()), "catalog": saved.as_dict(_defaults())})


@bp.put("/api/streams/<stream_id>")
@require_fetch
def api_edit(stream_id: str):
    body = request.get_json(silent=True) or {}
    cat = _catalog()
    if cat.error:
        return jsonify({"error": f"fix {cat.path} first: {cat.error}"}), 409
    if cat.get(stream_id) is None:
        return jsonify({"error": f"no stream {stream_id!r} in streams.json"}), 404
    try:
        updated = catalog.from_form(body, existing_id=stream_id)
        if updated.id != stream_id and cat.get(updated.id):
            return jsonify({"error": f"there is already a stream called {updated.id!r}"}), 409
        streams = [updated if s.id == stream_id else s for s in cat.streams]
        saved = _save(streams, _revision_of(body))
    except catalog.StaleWrite as exc:
        return jsonify({"error": str(exc)}), 409
    except catalog.SaveError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"stream": updated.as_dict(_defaults()), "catalog": saved.as_dict(_defaults())})


@bp.delete("/api/streams/<stream_id>")
@require_fetch
def api_delete(stream_id: str):
    body = request.get_json(silent=True) or {}
    cat = _catalog()
    if cat.get(stream_id) is None:
        return jsonify({"error": f"no stream {stream_id!r} in streams.json"}), 404
    try:
        saved = _save([s for s in cat.streams if s.id != stream_id], _revision_of(body))
    except catalog.StaleWrite as exc:
        return jsonify({"error": str(exc)}), 409
    except catalog.SaveError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"catalog": saved.as_dict(_defaults())})


@bp.post("/api/streams/reorder")
@require_fetch
def api_reorder():
    body = request.get_json(silent=True) or {}
    order = body.get("order")
    if not isinstance(order, list):
        return jsonify({"error": '"order" must be a list of stream ids'}), 400

    cat = _catalog()
    by_id = {s.id: s for s in cat.streams}
    if sorted(map(str, order)) != sorted(by_id):
        # A reorder that adds or drops entries is a bug, not an edit.
        return jsonify({"error": "that order does not match the streams on file"}), 409
    try:
        saved = _save([by_id[str(i)] for i in order], _revision_of(body))
    except catalog.StaleWrite as exc:
        return jsonify({"error": str(exc)}), 409
    except catalog.SaveError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"catalog": saved.as_dict(_defaults())})


# -- the advanced tab ----------------------------------------------------


@bp.get("/api/admin/state")
def api_admin_state():
    cfg = _config()
    return jsonify(
        {
            **cfg.public(),
            "admin": auth.is_admin(),
            "locked_for": auth.lockout_remaining(),
        }
    )


@bp.post("/api/login")
@require_fetch
def api_login():
    body = request.get_json(silent=True) or {}
    ok, message = auth.log_in(str(body.get("username") or ""), str(body.get("password") or ""))
    if not ok:
        return jsonify({"error": message}), 401
    return jsonify({"message": message, "admin": True})


@bp.post("/api/logout")
@require_fetch
def api_logout():
    auth.log_out()
    return jsonify({"message": "signed out", "admin": False})


@bp.get("/api/admin/config")
@require_admin
def api_admin_config():
    return jsonify(_config().public())


@bp.put("/api/admin/config")
@require_fetch
@require_admin
def api_admin_save_config():
    body = request.get_json(silent=True) or {}
    try:
        saved = config.save_defaults(body.get("defaults") or {}, current_app.config["CONFIG_PATH"])
    except config.ConfigError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"message": "defaults saved", **saved.public()})


@bp.post("/api/admin/action/<name>")
@require_fetch
@require_admin
def api_admin_action(name: str):
    entry = actions.ACTIONS.get(name)
    if entry is None:
        return jsonify({"error": f"unknown action {name!r}"}), 404
    _title, _fn, needs_confirm = entry
    body = request.get_json(silent=True) or {}
    if needs_confirm and body.get("confirm") is not True:
        # Belt and braces: the page asks too, but the route will not do this on
        # a bare POST from a stray reload.
        return jsonify({"error": "this action needs an explicit confirmation"}), 400
    try:
        message = actions.run_action(name, _manager())
    except actions.ActionError as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"message": message})


@bp.get("/api/admin/unknown")
@require_admin
def api_admin_unknown():
    """Links the direct-link bar refused, waiting for a site class."""
    return jsonify({"entries": unknown.entries(), "path": unknown.PATH})


@bp.delete("/api/admin/unknown")
@require_fetch
@require_admin
def api_admin_unknown_clear():
    removed = unknown.clear()
    return jsonify(
        {
            "message": f"cleared {removed} link{'' if removed == 1 else 's'}",
            "entries": [],
            "path": unknown.PATH,
        }
    )


@bp.get("/api/admin/diagnostics")
@require_admin
def api_admin_diagnostics():
    return jsonify({"panes": actions.diagnostics(), "status": _manager().status()})


@bp.after_request
def _no_store(response):
    """Keep the API out of caches; a stale status or catalog is worse than none."""
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response
