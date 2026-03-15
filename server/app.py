"""
PlexSync Server — Flask app
Runs on the N150 home server.
Serves the management UI and REST API.
Starts the transcode worker and heartbeat poller as background threads.
"""

import os
import shutil
import logging
from flask import Flask, jsonify, request, render_template, abort

from sync.queue import SyncQueue
from sync.worker import TranscodeWorker
from sync.dispatcher import HeartbeatPoller
from sync.transcode import probe, system_check, QUALITY_PRESETS

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

# ── Config from env ───────────────────────────────────────────────────────────
PI_IP       = os.environ.get("PI_IP",       "192.168.1.100")
PI_PORT     = int(os.environ.get("PI_PORT", "5001"))
STATE_FILE  = os.environ.get("PLEXSYNC_STATE", "queue_state.json")
CACHE_DIR   = os.environ.get("PLEXSYNC_CACHE", "/tmp/plexsync_cache")

# ── App + services ────────────────────────────────────────────────────────────
app = Flask(__name__)
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


@app.before_request
def _start_services():
    """Start background threads on first request (avoids issues with Flask reloader)."""
    global _started
    if not globals().get("_started"):
        worker.start()
        poller.start()
        globals()["_started"] = True


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
    """
    Add a file to the sync queue.
    Body: {
      "source_path": "/mnt/plex/Movies/Moana.mkv",
      "name":        "Moana",          # optional, defaults to filename
      "quality":     "balanced",       # optional
      "priority":    0                 # optional, higher = sooner
    }
    """
    data = request.json or {}
    source_path = data.get("source_path")
    if not source_path:
        abort(400, description="Missing 'source_path'")
    if not os.path.exists(source_path):
        abort(404, description=f"File not found: {source_path}")

    quality = data.get("quality", "balanced")
    if quality not in QUALITY_PRESETS:
        abort(400, description=f"Invalid quality. Choose: {list(QUALITY_PRESETS)}")

    name = data.get("name") or os.path.splitext(os.path.basename(source_path))[0]
    item = queue.add(
        source_path=source_path,
        name=name,
        quality=quality,
        priority=int(data.get("priority", 0)),
    )

    # Kick the poller immediately in case Pi is already reachable
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
                    "name":   e.name,
                    "path":   e.path,
                    "is_dir": e.is_dir(follow_symlinks=False),
                    "size":   e.stat().st_size if e.is_file() else 0,
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
        "pi_reachable":    poller.pi_reachable,
        "pi_ip":           PI_IP,
        "pi_free_bytes":   poller.pi_free_bytes,
        "transcode_system": system_check(),
        "server_disk": {
            "total": disk.total,
            "used":  disk.used,
            "free":  disk.free,
            "pct":   round(disk.used / disk.total * 100, 1),
        },
        "current_transcode": worker.current_item_id,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
