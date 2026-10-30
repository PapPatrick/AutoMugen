"""Flask routes for the local AutoMugen dashboard and OBS overlay."""

from __future__ import annotations

import copy
import json
import logging
import secrets
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, Response, current_app, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from automugen import Application, UserError, current_match
from broadcast import result_view, view as broadcast_view
from twitch import TwitchError


ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
MAX_REQUEST_BYTES = 256 * 1024
LOG = logging.getLogger("automugen")
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/overlay": ("overlay.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/broadcast.js": ("broadcast.js", "text/javascript; charset=utf-8"),
    "/auth/callback": ("auth.html", "text/html; charset=utf-8"),
    "/auth.js": ("auth.js", "text/javascript; charset=utf-8"),
    "/overlay.js": ("overlay.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


def _reject_json_constant(value: str):
    raise ValueError(f"Invalid JSON constant: {value}")


def create_app(application: Application, lock: threading.Lock, token: str) -> Flask:
    """Create the dashboard web app around an existing tournament application."""
    app = Flask(__name__, static_folder=None)
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_REQUEST_BYTES,
        SERVER_PORT=8765,
    )
    app.extensions["automugen.application"] = application
    app.extensions["automugen.lock"] = lock
    app.extensions["automugen.token"] = token

    def runtime() -> Application:
        return current_app.extensions["automugen.application"]

    def json_error(message: str, status: int) -> tuple[Response, int]:
        return jsonify(error=message), status

    @app.before_request
    def validate_request():
        port = current_app.config["SERVER_PORT"]
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if request.host not in allowed_hosts:
            return json_error("Only loopback dashboard hosts are allowed.", 403)

        if request.method != "POST":
            return None

        allowed_origins = {f"http://{host}" for host in allowed_hosts}
        origin = request.headers.get("Origin")
        supplied_token = request.headers.get("X-AutoMugen-Token", "")
        expected_token = current_app.extensions["automugen.token"]
        if (
            (origin is not None and origin not in allowed_origins)
            or not secrets.compare_digest(supplied_token, expected_token)
        ):
            return json_error("Invalid dashboard token or origin. Refresh the local dashboard.", 403)

        length = request.content_length
        if length is None or not 0 < length <= MAX_REQUEST_BYTES:
            raise UserError("Request body must be between 1 byte and 256 KiB.")
        if request.mimetype != "application/json":
            raise UserError("Expected application/json.")
        try:
            data = json.loads(request.get_data(cache=True), parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise UserError("Request body must contain valid JSON.") from exc
        if not isinstance(data, dict):
            raise UserError("Expected a JSON object.")
        g.action_data = data
        return None

    @app.after_request
    def apply_security_headers(response: Response) -> Response:
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'self'"
        )
        return response

    @app.errorhandler(UserError)
    def handle_user_error(error: UserError):
        return json_error(str(error), 400 if request.method == "POST" else 503)

    @app.errorhandler(TwitchError)
    def handle_twitch_error(error: TwitchError):
        return json_error(str(error), 400)

    @app.errorhandler(RequestEntityTooLarge)
    def handle_large_request(_error: RequestEntityTooLarge):
        return json_error("Request body must be between 1 byte and 256 KiB.", 400)

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException):
        return json_error(error.description, error.code or 500)

    def handle_operation_error(error: Exception):
        LOG.exception("Operation failed")
        return json_error(f"Operation failed: {error}. Check state before retrying.", 503)

    app.register_error_handler(OSError, handle_operation_error)
    app.register_error_handler(subprocess.SubprocessError, handle_operation_error)

    @app.errorhandler(Exception)
    def handle_unexpected_error(error: Exception):
        LOG.exception("Unexpected request failure")
        return json_error("Unexpected server error. Check the server console; no winner was inferred.", 500)

    @app.get("/api/state")
    def get_state():
        with current_app.extensions["automugen.lock"]:
            state = runtime().snapshot()
            state["token"] = current_app.extensions["automugen.token"]
        return jsonify(state)

    @app.get("/api/overlay")
    def get_overlay():
        with current_app.extensions["automugen.lock"]:
            instance = runtime()
            instance.poll()
            state = {
                "tournament": instance.state["tournament"],
                "current_match": current_match(instance.state["tournament"]),
                "launch": {key: instance.state["launch"][key] for key in ("status", "message")},
                "server_time": time.time(),
                "twitch": instance.predictions.public(),
                "broadcast": broadcast_view(instance.state),
                "result": result_view(instance.state),
                "portrait_revision": instance.state.get("portrait_revision", ""),
                "chat": instance.chat.public(),
            }
        return jsonify(state)

    @app.get("/api/portrait")
    def get_portrait():
        entry = request.args.get("id", "")
        with current_app.extensions["automugen.lock"]:
            instance = runtime()
            allowed_ids = {fighter["id"] for fighter in instance.roster["fighters"]}
            if instance.state["tournament"]:
                allowed_ids.update(fighter["id"] for fighter in instance.state["tournament"]["fighters"])
            if entry not in allowed_ids:
                return json_error("Unknown fighter portrait.", 404)
            portrait = instance.portraits.get(entry)
            if not portrait:
                return json_error(
                    instance.portraits.errors.get(entry, "Portrait unavailable."),
                    404,
                )
            return Response(portrait.read_bytes(), mimetype="image/png")

    @app.post("/api/action/<action>")
    def post_action(action: str):
        with current_app.extensions["automugen.lock"]:
            instance = runtime()
            data = g.action_data
            authorize_url = None
            authorized_provider = None
            prediction = instance.state.get("prediction")
            locked_chat = (
                prediction and prediction.get("economy") == "credits"
                and prediction["status"] == "LOCKED"
                and not instance.state.get("legacy_native_pending")
            )

            if action == "chat_login":
                if (instance.predictions.active() and not locked_chat) or instance.state["automation"]["enabled"]:
                    raise UserError(
                        "Pause launches and settle/refund open bets before reauthorizing chat."
                    )
                authorize_url = instance.chat.client.authorize_url(
                    f"http://localhost:{current_app.config['SERVER_PORT']}/auth/callback"
                )
            elif action == "twitch_callback":
                if (instance.predictions.active() and not locked_chat) or instance.state["automation"]["enabled"]:
                    raise UserError(
                        "Pause launches and settle/refund open bets before reauthorizing chat."
                    )
                instance.chat.client.authorize(data.get("access_token"), data.get("state"))
                if locked_chat:
                    instance.chat.reader.connect(instance.chat.client, expected_channel_id=prediction["broadcaster_id"])
                else:
                    instance.chat.reader.connect(instance.chat.client)
                authorized_provider = "chat"
                if not instance.predictions.active():
                    state = copy.deepcopy(instance.state)
                    state["twitch_error"] = None
                    instance.commit(state)
            else:
                instance.act(action, data)

            state = instance.snapshot()
            state["token"] = current_app.extensions["automugen.token"]
            if authorize_url:
                state["authorize_url"] = authorize_url
            if authorized_provider:
                state["authorization_provider"] = authorized_provider
        return jsonify(state)

    for path, (filename, content_type) in ASSETS.items():
        endpoint = "asset_" + (filename.replace(".", "_"))
        app.add_url_rule(
            path,
            endpoint=endpoint,
            view_func=lambda filename=filename, content_type=content_type: send_from_directory(
                PUBLIC,
                filename,
                mimetype=content_type.split(";", 1)[0],
            ),
            methods=["GET"],
        )

    return app
