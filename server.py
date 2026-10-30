"""Loopback-only AutoMugen dashboard server and background scheduler."""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
from pathlib import Path
import secrets
import threading
import time

from werkzeug.serving import make_server

from automugen import Application, Store, UserError, validate_state
from web import create_app


ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("automugen")
SCHEDULER_INTERVAL = 0.2


class Server:
    """Own the threaded HTTP server and serialize state-changing work."""

    def __init__(self, port: int, app: Application):
        self.app = app
        self.lock = threading.Lock()
        self.token = secrets.token_urlsafe(32)
        self._stop_scheduler = threading.Event()
        self._scheduler_thread: threading.Thread | None = None
        self.flask_app = create_app(app, self.lock, self.token)
        self.httpd = make_server("127.0.0.1", port, self.flask_app, threaded=True)
        self.flask_app.config["SERVER_PORT"] = self.server_port

    @property
    def server_port(self) -> int:
        return self.httpd.server_port

    def serve_forever(self, poll_interval: float = SCHEDULER_INTERVAL) -> None:
        self._stop_scheduler.clear()
        self._scheduler_thread = threading.Thread(
            target=self._run_scheduler,
            args=(poll_interval,),
            name="automugen-scheduler",
            daemon=True,
        )
        self._scheduler_thread.start()
        try:
            self.httpd.serve_forever(poll_interval=poll_interval)
        finally:
            self._stop_scheduler.set()
            self._scheduler_thread.join(timeout=2)

    def _run_scheduler(self, interval: float) -> None:
        while not self._stop_scheduler.wait(interval):
            with self.lock:
                try:
                    self.app.tick()
                except Exception:
                    self._handle_scheduler_failure()

    def _handle_scheduler_failure(self) -> None:
        LOG.exception("Automatic runner failed; stopping further automatic launches")
        state = copy.deepcopy(self.app.state)
        state["automation"] = {
            "enabled": False,
            "message": "Automatic runner failed. Check the server console and review the current round.",
        }
        if state.get("portrait_update", {}).get("status") == "running":
            state["portrait_update"]["status"] = "failed"
            state["portrait_update"]["message"] = (
                "Image update failed unexpectedly. Check the server console, then retry Update roster & portraits."
            )
        try:
            self.app.commit(state)
        except OSError:
            # A storage failure must still stop this process's scheduler.
            self.app.state["automation"] = state["automation"]
            LOG.exception("Cannot persist automatic-runner failure")

    def shutdown(self) -> None:
        self._stop_scheduler.set()
        self.httpd.shutdown()

    def server_close(self) -> None:
        self._stop_scheduler.set()
        self.httpd.server_close()
        thread = self._scheduler_thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)


def acquire_lock(folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    file = (folder / "server.lock").open("a+b")
    file.write(b"\0")
    file.flush()
    file.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        file.close()
        raise UserError("Another AutoMugen server is using this data folder.") from exc
    return file


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "runtime")
    parser.add_argument(
        "--restore-backup",
        action="store_true",
        help="Explicitly restore the last valid backup before starting",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    handle = None
    server = None
    app = None
    try:
        handle = acquire_lock(args.data_dir)
        store = Store(args.data_dir / "state.json")
        if args.restore_backup:
            backup = store.path.with_suffix(".backup.json")
            payload = backup.read_text(encoding="utf-8")
            validate_state(json.loads(payload))
            if store.path.exists():
                recovery = store.path.with_name(f"state.displaced-{time.time_ns()}.json")
                recovery.write_bytes(store.path.read_bytes())
            Store._atomic(store.path, payload)
            LOG.warning("Explicitly restored state.backup.json; inspect the bracket before proceeding.")
        app = Application(store)
        server = Server(args.port, app)
        LOG.info(
            "Dashboard: http://127.0.0.1:%s/ | OBS: http://127.0.0.1:%s/overlay",
            server.server_port,
            server.server_port,
        )
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("Server stopped. Any launched game is left open; restart uses explicit recovery.")
    except (UserError, OSError, ValueError) as exc:
        LOG.error("%s", exc)
        raise SystemExit(1) from exc
    finally:
        if server:
            server.shutdown()
            server.server_close()
        if app:
            app.close_observer()
            app.chat.reader.close()
        if handle:
            handle.close()


if __name__ == "__main__":
    main()
