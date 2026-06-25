"""
LTCtoLV1 — built-in web remote.

A small Flask server that runs in a background thread and exposes the same
actions the tk UI does, plus a Server-Sent Events (SSE) stream so the browser
can react in real time to TC, LV1 status, cue fires, and so on.

Design notes:
  - All state goes through AppController. The web server is just a thin
    transport layer that translates HTTP → controller methods.
  - Bound to 0.0.0.0 — the LAN-trust model fits since the LV1 console host
    is already trusted infrastructure. No auth on purpose.
  - SSE clients each get their own queue.Queue. The controller's event bus
    pushes events into every queue; the SSE generator yields them as
    text/event-stream chunks. TC events are throttled to 10 Hz upstream
    so SSE traffic stays bounded even on a high-frame-rate stream.
  - Threaded WSGI dev server is fine for this use case (≤ 5 phones / tablets
    in a control room). No need for gunicorn / uvicorn here.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from typing import Any, Dict, Optional

from app_controller import AppController


# Hook the desktop UI registers so the web remote can pull the host
# window to the front. None when no tk window exists (pure headless,
# not currently a supported runtime mode but the wiring tolerates it).
_show_ui_hook: Optional["Callable[[], None]"] = None


def set_show_ui_hook(fn) -> None:
    """Register a callable that brings the desktop window to the foreground.
    Called by the MainWindow at construction time; invoked by the
    /api/window/show endpoint."""
    global _show_ui_hook
    _show_ui_hook = fn


# Where bundled static assets live. PyInstaller unpacks data files under
# sys._MEIPASS at runtime; from source we use the repo's web/ directory.
def _web_root() -> str:
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "web")  # type: ignore[attr-defined]
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


# Per-client SSE queue. Older messages are dropped on overflow rather than
# blocking the controller emitter thread.
_SSE_QUEUE_MAX = 200


class _SSEClient:
    """One subscriber that wants a real-time event stream."""

    def __init__(self) -> None:
        self.q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=_SSE_QUEUE_MAX)
        self.closed = False

    def push(self, event: Dict[str, Any]) -> None:
        if self.closed:
            return
        try:
            self.q.put_nowait(event)
        except queue.Full:
            # Drop the oldest event to make room — better than wedging.
            try:
                self.q.get_nowait()
                self.q.put_nowait(event)
            except Exception:
                pass


class WebServer:
    """Wraps a Flask app + its background thread."""

    def __init__(self, controller: AppController, host: str = "0.0.0.0", port: int = 8080) -> None:
        try:
            from flask import Flask, Response, jsonify, request, send_from_directory
        except ImportError as exc:
            raise RuntimeError(
                "Flask is required for the web remote. Install it with: pip install flask"
            ) from exc

        self.ctl = controller
        self.host = host
        self.port = port
        self._thread: Optional[threading.Thread] = None
        self._clients: list[_SSEClient] = []
        self._clients_lock = threading.Lock()
        self._unsub: Optional[Any] = None
        self._server: Any = None

        web_dir = _web_root()
        app = Flask(
            __name__,
            static_folder=os.path.join(web_dir, "static"),
            static_url_path="/static",
        )

        # ─── Static SPA root ─────────────────────────────────────────────
        @app.route("/")
        def index() -> Any:
            return send_from_directory(web_dir, "index.html")

        @app.route("/favicon.ico")
        def favicon() -> Any:
            ico = os.path.join(os.path.dirname(web_dir), "ltctolv1.ico")
            if os.path.isfile(ico):
                return send_from_directory(os.path.dirname(ico), "ltctolv1.ico")
            return ("", 204)

        # ─── State snapshot ──────────────────────────────────────────────
        @app.route("/api/state")
        def get_state() -> Any:
            return jsonify(self.ctl.snapshot())

        # ─── Transport ───────────────────────────────────────────────────
        @app.route("/api/run/start", methods=["POST"])
        def run_start() -> Any:
            data = request.get_json(silent=True) or {}
            try:
                device_index = int(data["device_index"])
                channel = int(data["channel"])  # 1-based
                sample_rate = int(data["sample_rate"])
                block_size = int(data["block_size"])
            except (KeyError, ValueError, TypeError):
                return jsonify({"ok": False, "error": "device_index, channel, sample_rate, block_size required"}), 400
            device_label = data.get("device_label") or None
            ok, err = self.ctl.start_capture(
                device_index=device_index,
                channel_zero_based=max(0, channel - 1),
                sample_rate=sample_rate,
                block_size=block_size,
                device_label=device_label,
            )
            return jsonify({"ok": ok, "error": err})

        @app.route("/api/run/stop", methods=["POST"])
        def run_stop() -> Any:
            self.ctl.stop_capture()
            return jsonify({"ok": True})

        # ─── LV1 ─────────────────────────────────────────────────────────
        @app.route("/api/lv1/discover", methods=["POST"])
        def lv1_discover() -> Any:
            started = self.ctl.start_discovery()
            return jsonify({"ok": True, "scanning": started})

        @app.route("/api/lv1/connect", methods=["POST"])
        def lv1_connect() -> Any:
            data = request.get_json(silent=True) or {}
            host = (data.get("host") or "").strip()
            try:
                port = int(data.get("port") or 0)
            except (ValueError, TypeError):
                port = 0
            if not host or port <= 0:
                target = self.ctl.resolve_target(host, port)
                if target is None:
                    return jsonify({"ok": False, "error": "Pick a host:port or use a discovery result"}), 400
                host, port = target
            self.ctl.lv1_connect(host, port)
            return jsonify({"ok": True, "host": host, "port": port})

        @app.route("/api/lv1/disconnect", methods=["POST"])
        def lv1_disconnect() -> Any:
            self.ctl.lv1_disconnect_async()
            return jsonify({"ok": True})

        @app.route("/api/lv1/recall", methods=["POST"])
        def lv1_recall() -> Any:
            data = request.get_json(silent=True) or {}
            try:
                idx = int(data["index"])
            except (KeyError, ValueError, TypeError):
                return jsonify({"ok": False, "error": "index required"}), 400
            ok, err = self.ctl.lv1_recall_scene(idx)
            if ok:
                name = self.ctl.scene_catalog.get(idx, "(unknown)")
                self.ctl.set_status(f"Recalled scene [{idx}] {name}")
            return jsonify({"ok": ok, "error": err})

        # ─── Cues ────────────────────────────────────────────────────────
        @app.route("/api/cues", methods=["POST"])
        def cues_add() -> Any:
            data = request.get_json(silent=True) or {}
            cue = self.ctl.add_cue(
                label=str(data.get("label") or ""),
                timecode=str(data.get("timecode") or "00:00:00:00"),
                scene_name=str(data.get("scene_name") or ""),
                scene_index=_opt_int(data.get("scene_index")),
                enabled=bool(data.get("enabled", True)),
            )
            return jsonify({"ok": True, "cue_id": cue.id})

        @app.route("/api/cues/<int:cue_id>", methods=["PATCH"])
        def cues_update(cue_id: int) -> Any:
            data = request.get_json(silent=True) or {}
            updates: Dict[str, Any] = {}
            if "label" in data:
                updates["label"] = str(data["label"])
            if "timecode" in data:
                updates["timecode"] = str(data["timecode"])
            if "scene_name" in data:
                updates["scene_name"] = str(data["scene_name"])
            if "scene_index" in data:
                updates["scene_index"] = _opt_int(data["scene_index"])
            if "enabled" in data:
                updates["enabled"] = bool(data["enabled"])
            ok = self.ctl.update_cue(cue_id, **updates)
            return jsonify({"ok": ok})

        @app.route("/api/cues/<int:cue_id>", methods=["DELETE"])
        def cues_remove(cue_id: int) -> Any:
            return jsonify({"ok": self.ctl.remove_cue(cue_id)})

        @app.route("/api/cues/<int:cue_id>/move", methods=["POST"])
        def cues_move(cue_id: int) -> Any:
            data = request.get_json(silent=True) or {}
            direction = str(data.get("direction") or "").lower()
            if direction == "up":
                return jsonify({"ok": self.ctl.move_cue_up(cue_id)})
            if direction == "down":
                return jsonify({"ok": self.ctl.move_cue_down(cue_id)})
            return jsonify({"ok": False, "error": "direction must be 'up' or 'down'"}), 400

        @app.route("/api/cues/<int:cue_id>/toggle", methods=["POST"])
        def cues_toggle(cue_id: int) -> Any:
            new_val = self.ctl.toggle_cue_enabled(cue_id)
            if new_val is None:
                return jsonify({"ok": False, "error": "cue not found"}), 404
            return jsonify({"ok": True, "enabled": new_val})

        @app.route("/api/cues/<int:cue_id>/assign", methods=["POST"])
        def cues_assign(cue_id: int) -> Any:
            data = request.get_json(silent=True) or {}
            try:
                scene_index = int(data["scene_index"])
            except (KeyError, ValueError, TypeError):
                return jsonify({"ok": False, "error": "scene_index required"}), 400
            return jsonify({"ok": self.ctl.assign_scene_to_cue(cue_id, scene_index)})

        @app.route("/api/cues/<int:cue_id>/fire", methods=["POST"])
        def cues_fire(cue_id: int) -> Any:
            ok, err = self.ctl.test_fire_cue(cue_id)
            return jsonify({"ok": ok, "error": err})

        @app.route("/api/cues/tap", methods=["POST"])
        def cues_tap() -> Any:
            cue = self.ctl.tap_at_current_tc()
            if cue is None:
                return jsonify({"ok": False, "error": "No current timecode"}), 409
            return jsonify({"ok": True, "cue_id": cue.id})

        @app.route("/api/cues/reset", methods=["POST"])
        def cues_reset() -> Any:
            self.ctl.reset_fired()
            return jsonify({"ok": True})

        @app.route("/api/cues/revalidate", methods=["POST"])
        def cues_revalidate() -> Any:
            results = self.ctl.revalidate()
            issues = [
                {"cue_id": v.cue_id, "cue_label": v.cue_label,
                 "scene_name": v.scene_name, "status": v.resolution.status,
                 "suggestion": v.resolution.suggestion_name}
                for v in results
                if v.resolution.status in ("MISSING", "EMPTY")
            ]
            return jsonify({"ok": True, "issues": issues})

        @app.route("/api/cues/new", methods=["POST"])
        def cues_new() -> Any:
            self.ctl.new_cue_list()
            return jsonify({"ok": True})

        @app.route("/api/cues/open", methods=["POST"])
        def cues_open() -> Any:
            data = request.get_json(silent=True) or {}
            path = (data.get("path") or "").strip()
            if not path:
                return jsonify({"ok": False, "error": "path required"}), 400
            ok, err, _was_midi = self.ctl.load_cue_file(path)
            return jsonify({"ok": ok, "error": err})

        @app.route("/api/cues/recent")
        def cues_recent() -> Any:
            return jsonify({"recent_files": self.ctl._recent_files_payload()})

        @app.route("/api/cues/recent", methods=["DELETE"])
        def cues_recent_clear() -> Any:
            self.ctl.clear_recent()
            return jsonify({"ok": True})

        @app.route("/api/cues/default_dir")
        def cues_default_dir() -> Any:
            from models import default_projects_dir
            return jsonify({"path": default_projects_dir()})

        @app.route("/api/cues/save", methods=["POST"])
        def cues_save() -> Any:
            """Save the current cue list.
            Body forms (all optional):
              {}                      → save to current_file (Save)
              {"path": "..."}         → save to an absolute path (legacy)
              {"name": "foo"}         → save into default projects dir, with
                                        .ltcv1 appended if missing (the web
                                        UI's Save prompt uses this form)."""
            data = request.get_json(silent=True) or {}
            path = data.get("path") or None
            name = (data.get("name") or "").strip() or None
            if name and not path:
                from models import CUE_FILE_EXTENSION, ensure_projects_dir
                # Strip any directory separators the user typed — the whole
                # point of this code path is "name only, fixed folder".
                safe = os.path.basename(name)
                if not safe:
                    return jsonify({"ok": False, "error": "Invalid file name"}), 400
                low = safe.lower()
                if not low.endswith(CUE_FILE_EXTENSION) and not low.endswith(".json"):
                    safe += CUE_FILE_EXTENSION
                path = os.path.join(ensure_projects_dir(), safe)
            ok, err = self.ctl.save_cue_file(path)
            return jsonify({"ok": ok, "error": err, "path": path if ok else None})

        @app.route("/api/cues/download")
        def cues_download() -> Any:
            payload = [self.ctl.cue_to_dict(c) for c in self.ctl.cue_list.cues]
            body = json.dumps(payload, indent=2, ensure_ascii=False)
            from flask import Response as _R
            fname = os.path.basename(self.ctl.current_file) if self.ctl.current_file else "cues.json"
            return _R(
                body,
                mimetype="application/json",
                headers={"Content-Disposition": f'attachment; filename="{fname}"'},
            )

        @app.route("/api/cues/upload", methods=["POST"])
        def cues_upload() -> Any:
            data = request.get_json(silent=True)
            if not isinstance(data, list):
                # Try a multipart upload with a file field instead.
                if "file" in request.files:
                    try:
                        data = json.loads(request.files["file"].read().decode("utf-8"))
                    except Exception as exc:  # noqa: BLE001
                        return jsonify({"ok": False, "error": f"Invalid JSON: {exc}"}), 400
                else:
                    return jsonify({"ok": False, "error": "Expected a JSON array body or a 'file' multipart field"}), 400
            ok, err = self.ctl.load_cue_list_from_data(data)
            return jsonify({"ok": ok, "error": err})

        # ─── Audio device introspection ──────────────────────────────────
        @app.route("/api/audio/devices")
        def audio_devices() -> Any:
            devs = self.ctl.audio_devices()
            return jsonify({"devices": devs})

        @app.route("/api/audio/refresh", methods=["POST"])
        def audio_refresh() -> Any:
            devs = self.ctl.refresh_audio_devices()
            return jsonify({"devices": devs})

        @app.route("/api/audio/channels")
        def audio_channels() -> Any:
            try:
                idx = int(request.args.get("device_index", ""))
            except ValueError:
                return jsonify({"ok": False, "error": "device_index required"}), 400
            names = self.ctl.channel_names(idx)
            return jsonify({"channels": names})

        # ─── Window control (host UI) ────────────────────────────────────
        @app.route("/api/window/show", methods=["POST"])
        def window_show() -> Any:
            if _show_ui_hook is None:
                return jsonify({"ok": False, "error": "Desktop UI not available"}), 409
            try:
                _show_ui_hook()
            except Exception as exc:  # noqa: BLE001
                return jsonify({"ok": False, "error": str(exc)}), 500
            return jsonify({"ok": True})

        # ─── Settings ────────────────────────────────────────────────────
        @app.route("/api/settings", methods=["PATCH"])
        def settings_update() -> Any:
            data = request.get_json(silent=True) or {}
            self.ctl.update_settings(**data)
            self.ctl.save_settings()
            return jsonify({"ok": True})

        # ─── SSE event stream ────────────────────────────────────────────
        @app.route("/api/events")
        def events() -> Any:
            from flask import Response as _R
            client = _SSEClient()
            with self._clients_lock:
                self._clients.append(client)

            # Push an immediate "hello" + state snapshot so freshly-connected
            # browsers don't have to wait for the next event.
            client.push({"name": "snapshot", "payload": self.ctl.snapshot()})

            def _stream():
                try:
                    while not client.closed:
                        try:
                            evt = client.q.get(timeout=15.0)
                        except queue.Empty:
                            # SSE keep-alive — comment lines are ignored by EventSource
                            yield ": ping\n\n"
                            continue
                        if evt.get("name") == "_close":
                            # Server is shutting down — close cleanly.
                            return
                        data_json = json.dumps(evt.get("payload", {}), default=_json_default)
                        yield f"event: {evt.get('name', 'message')}\ndata: {data_json}\n\n"
                finally:
                    client.closed = True
                    with self._clients_lock:
                        try:
                            self._clients.remove(client)
                        except ValueError:
                            pass

            return _R(_stream(), mimetype="text/event-stream", headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # disable proxy buffering
            })

        self.app = app

    # ─── Lifecycle ──────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        # Subscribe to controller events and fan them out to every SSE client.
        self._unsub = self.ctl.subscribe(self._on_event)
        self._thread = threading.Thread(
            target=self._serve, name="WebServer", daemon=True
        )
        self._thread.start()

    def _serve(self) -> None:
        try:
            from werkzeug.serving import make_server
            self._server = make_server(self.host, self.port, self.app, threaded=True)
            print(f"[web] listening on http://{self.host}:{self.port}/")
            self._server.serve_forever()
        except Exception as exc:  # noqa: BLE001
            print(f"[web] failed to start: {exc}")
            self.ctl.set_status(f"Web remote failed: {exc}", warn=True)

    def stop(self) -> None:
        try:
            if self._unsub:
                self._unsub()
                self._unsub = None
        except Exception:
            pass
        # Wake every open SSE generator so its finally{} block runs and the
        # werkzeug thread exits — otherwise server.shutdown() would block on
        # idle keep-alive connections.
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for c in clients:
            c.closed = True
            try:
                c.q.put_nowait({"name": "_close", "payload": {}})
            except Exception:
                pass
        try:
            if self._server:
                self._server.shutdown()
                self._server = None
        except Exception:
            pass

    # ─── Event fan-out ──────────────────────────────────────────────────

    def _on_event(self, name: str, payload: Dict[str, Any]) -> None:
        evt = {"name": name, "payload": payload}
        with self._clients_lock:
            # Cull any clients whose stream closed since the last dispatch.
            # The generator's finally{} also removes them but it can lag if
            # the connection hangs without being closed cleanly.
            self._clients = [c for c in self._clients if not c.closed]
            clients = list(self._clients)
        for c in clients:
            c.push(evt)


# ─── Helpers ────────────────────────────────────────────────────────────


def _opt_int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _json_default(o: Any) -> Any:
    # Best-effort fallback for dataclasses, etc.
    if hasattr(o, "__dict__"):
        return o.__dict__
    return str(o)
