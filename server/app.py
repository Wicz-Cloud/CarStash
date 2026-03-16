

import os
import shutil
import logging
from flask import Flask, jsonify, request, abort, render_template
from flask_cors import CORS
from logging.handlers import TimedRotatingFileHandler
try:
    from colorlog import ColoredFormatter
except ImportError:
    # Fallback if colorlog is not installed
    class ColoredFormatter(logging.Formatter):
        pass
from sync.worker import TranscodeWorker
from sync.dispatcher import HeartbeatPoller
from sync.transcode import probe, system_check
from sync.queue import SyncQueue


"""
CarStash Server — Flask app
Runs on the N150 home server.
Serves the management UI and REST API.
Starts the transcode worker and heartbeat poller as background threads.
"""

STATE_FILE = "/tmp/carstash_state.json"
CACHE_DIR = "/tmp/carstash_cache"
PI_IP = "127.0.0.1"
PI_PORT = 5001

LOG_DIR = os.environ.get("CARSTASH_LOG_DIR", "/mnt/carstash/logs")
MEDIA_DIR = os.environ.get("CARSTASH_MEDIA_DIR", "/mnt/carstash/media")
MIN_FREE_BYTES = int(os.environ.get("CARSTASH_MIN_FREE_GB", "2")) * 1024 ** 3
AUTH_TOKEN = os.environ.get("CARSTASH_AUTH_TOKEN")

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(MEDIA_DIR, exist_ok=True)

app = Flask(__name__)
CORS(app)


def setup_logging():
    log_path = os.path.join(LOG_DIR, "carstash-server.log")
    handler = TimedRotatingFileHandler(log_path, when="midnight", backupCount=7)
    formatter = ColoredFormatter(
        "%(log_color)s%(asctime)s %(levelname)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        log_colors={
            'DEBUG': 'cyan',
            'INFO': 'green',
            'WARNING': 'yellow',
            'ERROR': 'red',
            'CRITICAL': 'bold_red',
        }
    )
    handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)
    root_logger.info("Logging initialized")

setup_logging()
logger = logging.getLogger(__name__)


# ── App + services ────────────────────────────────────────────────────────────

queue = SyncQueue(state_path=STATE_FILE)
worker = TranscodeWorker(queue=queue, cache_dir=CACHE_DIR)
poller = HeartbeatPoller(
    queue=queue,
    pi_ip=PI_IP,
    pi_port=PI_PORT,
    on_status_change=lambda reachable: logger.info(
        f"Pi {'ONLINE ✓' if reachable else 'OFFLINE ✗'}"
    ),
)


        # ── Frontend ──────────────────────────────────────────────────────────────────


@app.route("/")
def index():
    return render_template("index.html")


# ── Queue management ──────────────────────────────────────────────────────────


@app.route("/api/queue", methods=["GET"])
def list_queue():
    return jsonify([i.to_dict() for i in queue.list_all()])


@app.route("/api/queue/stats", methods=["GET"])
def queue_stats():
    return jsonify(queue.stats())


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


@app.route("/api/queue/<item_id>", methods=["GET"])
def get_queue_item(item_id):
    item = queue.get(item_id)
    if not item:
        abort(404)
    return jsonify(item.to_dict())


@app.route("/api/queue/<item_id>", methods=["DELETE"])
def remove_queue_item(item_id):
    queue.remove(item_id)
    return jsonify({"ok": True})


@app.route("/api/queue/<item_id>/retry", methods=["POST"])

def retry_item(item_id):
    """Reset a failed item back to queued."""
    item = queue.get(item_id)
    if not item:
        abort(404)
    if item.state not in ("failed", "interrupted"):
        abort(409, description=f"Item is {item.state}, can only retry failed/interrupted")
    target = "queued" if not item.transcoded_path else "ready"
    queue.set_state(item_id, target, error=None, push_progress=0.0)
    poller.force_poll()
    return jsonify(queue.get(item_id).to_dict())



# ── File browser (for adding items via UI) ────────────────────────────────────

@app.route("/api/browse", methods=["GET"])
def browse():
    path = request.args.get("path", "/")
    try:
        entries = []
        with os.scandir(path) as it:
            for e in sorted(it, key=lambda x: (not x.is_dir(), x.name.lower())):
                entries.append({
                    "name": e.name,
                    "path": e.path,
                    "is_dir": e.is_dir(follow_symlinks=False),
                    "size": e.stat().st_size if e.is_file() else 0,
                })
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


        # ── System status ─────────────────────────────────────────────────────────────


        @app.route("/api/system", methods=["GET"])
        def system_status():
            disk = shutil.disk_usage("/")
            return jsonify({
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
            })


        # ── Health check (used by CI smoke test) ─────────────────────────────────────


        @app.route("/health", methods=["GET"])
        def health():
            """Lightweight liveness probe -- returns 200 as long as the server is up."""
            return jsonify({"status": "ok", "service": "carstash-server"}), 200


        if __name__ == "__main__":
            # Use environment variable for host, default to 127.0.0.1 for safety
            app.run(host=os.environ.get("PLEXSYNC_SERVER_HOST", "127.0.0.1"), port=5000, debug=False)



# ── Health check (used by CI smoke test) ─────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    """Lightweight liveness probe -- returns 200 as long as the server is up."""
    return jsonify({"status": "ok", "service": "carstash-server"}), 200



if __name__ == "__main__":
    # Use environment variable for host, default to 127.0.0.1 for safety
    app.run(host=os.environ.get("PLEXSYNC_SERVER_HOST", "127.0.0.1"), port=5000, debug=False)
