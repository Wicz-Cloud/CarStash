import json
import logging
import os
import re
import shutil
import tempfile
import time
from logging.handlers import TimedRotatingFileHandler

from flask import Flask, Response, abort, jsonify, request, stream_with_context
from flask_cors import CORS
from flask import send_from_directory

try:
    from colorlog import ColoredFormatter
except ImportError:

    class ColoredFormatter(logging.Formatter):
        pass


from .sync.dispatcher import HeartbeatPoller
from .sync.queue import SyncQueue
from .sync.transcode import probe, system_check
from .sync.worker import TranscodeWorker

STATE_FILE = os.environ.get("CARSTASH_STATE_FILE", os.path.join(tempfile.gettempdir(), "carstash_state.json"))
CACHE_DIR = os.environ.get("CARSTASH_CACHE", os.path.join(tempfile.gettempdir(), "carstash_cache"))
PI_IP = os.environ.get("PI_IP", "127.0.0.1")
PI_PORT = 5001
LOG_DIR = os.environ.get("CARSTASH_LOG_DIR", "/mnt/carstash/logs")
MEDIA_DIR = os.environ.get("CARSTASH_MEDIA_DIR", "/mnt/carstash/media")
MIN_FREE_BYTES = int(os.environ.get("CARSTASH_MIN_FREE_GB", "2")) * 1024**3
AUTH_TOKEN = os.environ.get("CARSTASH_AUTH_TOKEN")


def _ensure_dir(path, fallback_subdir):
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except OSError:
        fallback = os.path.join(tempfile.gettempdir(), fallback_subdir)
        os.makedirs(fallback, exist_ok=True)
        return fallback


# Ensure log and media directories exist; fall back to temp dirs if /mnt isn't writable
LOG_DIR = _ensure_dir(LOG_DIR, "carstash_logs")
MEDIA_DIR = _ensure_dir(MEDIA_DIR, "carstash_media")

app = Flask(__name__)
CORS(app)


class _PollingFilter(logging.Filter):
    """Drop noisy high-frequency HTTP access log entries from Werkzeug."""
    _BORING = (
        '"GET /api/queue HTTP',
        '"GET /api/system HTTP',
        '"GET /api/logs HTTP',
        '"GET /api/logs/stream HTTP',
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(p in msg for p in self._BORING)


def setup_logging():
    log_path = os.path.join(LOG_DIR, "carstash-server.log")
    handler = TimedRotatingFileHandler(log_path, when="midnight", backupCount=7)

    # Plain formatter for the file — colorlog adds ANSI codes that pollute the file
    plain_formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(plain_formatter)
    handler.addFilter(_PollingFilter())

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)

    # Suppress noisy Werkzeug access logs from the console too
    logging.getLogger("werkzeug").addFilter(_PollingFilter())

    root_logger.info("=== CarStash server starting ===")
    root_logger.info(f"Log: {log_path} | Media: {MEDIA_DIR} | Cache: {CACHE_DIR}")


setup_logging()
logger = logging.getLogger(__name__)

_ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text)


queue = SyncQueue(state_path=STATE_FILE)
worker = TranscodeWorker(queue=queue, cache_dir=CACHE_DIR)
poller = HeartbeatPoller(
    queue=queue,
    pi_ip=PI_IP,
    pi_port=PI_PORT,
    on_status_change=lambda reachable: logger.info(f"Pi {'ONLINE ✓' if reachable else 'OFFLINE ✗'}"),
)

poller.start()
worker.start()

@app.route("/api/queue", methods=["POST"])
def add_to_queue():
    """Add a file to the sync queue."""
    data = request.json or {}
    source_path = data.get("source_path")
    name = data.get("name")
    quality = data.get("quality")
    if not source_path or not name or not quality:
        abort(400, description="Missing required fields")
    item = queue.add(
        source_path=source_path,
        name=name,
        quality=quality,
        priority=int(data.get("priority", 0)),
    )
    poller.force_poll()
    return jsonify(item.to_dict()), 201



@app.route("/api/queue/<item_id>", methods=["DELETE"])
def delete_queue_item(item_id):
    """Remove an item from the sync queue."""
    if item_id not in queue._items:
        abort(404, description="Item not found")
    queue.remove(item_id)
    return jsonify({"ok": True}), 200


@app.route("/api/queue/<item_id>/retry", methods=["POST"])
def retry_queue_item(item_id):
    """Reset a failed or interrupted item so it will be pushed again on next heartbeat."""
    item = queue.get(item_id)
    if item is None:
        abort(404, description="Item not found")
    if item.state not in ("failed", "interrupted", "done"):
        abort(400, description=f"Item is '{item.state}', only failed/interrupted/done items can be retried")
    # If transcoded file is gone, fall back to re-transcode from source
    new_state = "interrupted" if (item.transcoded_path and os.path.exists(item.transcoded_path)) else "queued"
    queue.set_state(item_id, new_state, push_attempts=0, error=None)
    poller.force_poll()
    return jsonify({"ok": True, "new_state": new_state}), 200

@app.route("/api/browse", methods=["GET"])
def browse():
    path = request.args.get("path", "/")
    try:
        entries = []
        with os.scandir(path) as it:
            for e in sorted(it, key=lambda x: (not x.is_dir(), x.name.lower())):
                entries.append(
                    {
                        "name": e.name,
                        "path": e.path,
                        "is_dir": e.is_dir(follow_symlinks=False),
                        "size": e.stat().st_size if e.is_file() else 0,
                    }
                )
        return jsonify({"path": path, "entries": entries})
    except PermissionError:
        abort(403)
    except FileNotFoundError:
        abort(404)


@app.route("/api/probe", methods=["POST"])
def probe_file():
    data = request.json or {}
    path = data.get("path")
    if not path or not os.path.exists(path):
        abort(404, description="File not found")
    try:
        return jsonify(probe(path).to_dict())
    except Exception as e:
        abort(500, description=str(e))


@app.route("/api/system", methods=["GET"])
def system_status():
    disk = shutil.disk_usage("/")
    return jsonify(
        {
            "pi_reachable": poller.pi_reachable,
            "pi_ip": PI_IP,
            "pi_free_bytes": poller.pi_free_bytes,
            "transcode_system": system_check(),
            "server_disk": {
                "total": disk.total,
                "used": disk.used,
                "free": disk.free,
                "pct": round(disk.used / disk.total * 100, 1),
            },
            "current_transcode": worker.current_item_id,
        }
    )


# ── Log endpoints ─────────────────────────────────────────────────────────────
@app.route("/api/logs", methods=["GET"])
def get_logs():
    """Return the last N lines of the server log."""
    n = min(int(request.args.get("n", 200)), 1000)
    log_path = os.path.join(LOG_DIR, "carstash-server.log")
    try:
        with open(log_path, "r", errors="replace") as f:
            lines = f.readlines()
        return jsonify({"lines": [_strip_ansi(l.rstrip()) for l in lines[-n:] if l.strip()]})
    except FileNotFoundError:
        return jsonify({"lines": []})


@app.route("/api/logs/stream")
def stream_logs():
    """SSE endpoint — streams new log lines as they are written."""
    log_path = os.path.join(LOG_DIR, "carstash-server.log")

    def generate():
        # Send the last 100 historical lines first
        try:
            with open(log_path, "r", errors="replace") as f:
                lines = f.readlines()
            for line in lines[-500:]:
                cleaned = _strip_ansi(line.rstrip())
                if cleaned:
                    yield f"data: {json.dumps(cleaned)}\n\n"
        except FileNotFoundError:
            yield f"data: {json.dumps('[log file not found yet]')}\n\n"

        # Tail live
        try:
            with open(log_path, "r", errors="replace") as f:
                f.seek(0, 2)  # seek to end
                while True:
                    line = f.readline()
                    if line:
                        cleaned = _strip_ansi(line.rstrip())
                        if cleaned:
                            yield f"data: {json.dumps(cleaned)}\n\n"
                    else:
                        time.sleep(0.3)
                        yield ": keep-alive\n\n"
        except GeneratorExit:
            pass

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Health check (used by CI smoke test) ─────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    """Lightweight liveness probe -- returns 200 as long as the server is up."""
    return jsonify({"status": "ok", "service": "carstash-server"}), 200

@app.route("/api/queue", methods=["GET"])
def list_queue():
    return jsonify([i.to_dict() for i in queue.list_all()])

@app.route("/")
def index():
    return send_from_directory(os.path.dirname(__file__), "carstash.html")

if __name__ == "__main__":
    app.run(host=os.environ.get("PLEXSYNC_SERVER_HOST", "127.0.0.1"), port=5000, debug=False)
