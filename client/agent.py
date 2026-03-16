"""
CarStash — Pi Client Agent

Runs on the Raspberry Pi (or any remote client device).
Passive by design — the server initiates all transfers.

Endpoints:
  GET  /api/status                    — liveness + free space
  GET  /api/receive/<filename>/offset — bytes of partial .tmp already on disk
  PUT  /api/receive/<filename>        — receive / resume a file push
  GET  /api/files                     — list files on disk (for server's awareness)

Media server refresh is handled by the adapter in media_servers.py.
Set CARSTASH_MEDIA_SERVER in the environment before starting:
  plex | jellyfin | emby | kodi | none
"""


import os
import shutil
import logging
from pathlib import Path
from flask import Flask, jsonify, request, abort
from functools import wraps
from media_servers import get_adapter, SUPPORTED_SERVERS


MEDIA_DIR = os.environ.get("CARSTASH_MEDIA_DIR", "/mnt/carstash/media")
MIN_FREE_BYTES = int(os.environ.get("CARSTASH_MIN_FREE_GB", "2")) * 1024 ** 3
AUTH_TOKEN = os.environ.get("CARSTASH_AUTH_TOKEN")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s — %(message)s",
)
os.makedirs(MEDIA_DIR, exist_ok=True)
logger = logging.getLogger(__name__)

app = Flask(__name__)
media_adapter = get_adapter()
logger.info(f"Media server adapter: [{media_adapter.name}]")


# ── Auth Decorator ─────────────────────────────────────────────────────────────
def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if AUTH_TOKEN:
            token = request.headers.get("X-CarStash-Token")
            if not token or token != AUTH_TOKEN:
                logger.warning("Unauthorized request: missing or invalid token")
                abort(401, description="Unauthorized: missing or invalid token")
        return f(*args, **kwargs)
    return decorated


# ── Status ────────────────────────────────────────────────────────────────────

@app.route("/api/status", methods=["GET"])
@require_auth
def status():
    usage = shutil.disk_usage(MEDIA_DIR)
    files = _list_files()
    return jsonify({
        "ok": True,
        "media_server": media_adapter.name,
        "free_bytes": usage.free,
        "used_bytes": usage.used,
        "total_bytes": usage.total,
        "file_count": len(files),
        "media_dir": MEDIA_DIR,
    })


# ── Offset query (resume support) ─────────────────────────────────────────────

@app.route("/api/receive/<filename>/offset", methods=["GET"])
@require_auth
def get_offset(filename):
    safe_name = Path(filename).name
    final_path = os.path.join(MEDIA_DIR, safe_name)
    tmp_path = final_path + ".tmp"

    if os.path.exists(final_path):
        return jsonify({"offset": os.path.getsize(final_path), "complete": True})

    if os.path.exists(tmp_path):
        return jsonify({"offset": os.path.getsize(tmp_path), "complete": False, "tmp_exists": True})

    return jsonify({"offset": 0, "complete": False, "tmp_exists": False})


# ── File receiver ─────────────────────────────────────────────────────────────

@app.route("/api/receive/<filename>", methods=["PUT"])
@require_auth
def receive_file(filename):
    if not filename.endswith(".mp4"):
        abort(400, description="Only .mp4 files accepted")

    safe_name = Path(filename).name
    final_path = os.path.join(MEDIA_DIR, safe_name)
    tmp_path = final_path + ".tmp"

    # ── Parse Content-Range ───────────────────────────────────────────────────
    content_range = request.headers.get("Content-Range", "")
    offset = 0
    total_size = 0

    if content_range.startswith("bytes "):
        try:
            range_part, total_part = content_range[6:].split("/")
            offset = int(range_part.split("-")[0])
            total_size = int(total_part)
        except (ValueError, IndexError):
            abort(400, description=f"Malformed Content-Range: {content_range}")
    else:
        total_size = request.content_length or 0

    content_length = request.content_length or 0

    # ── Evict if needed ───────────────────────────────────────────────────────
    if content_length > 0:
        _evict_if_needed(content_length)

    # ── Validate offset matches disk ──────────────────────────────────────────
    if offset > 0:
        existing = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
        if existing != offset:
            logger.warning(
                f"Offset mismatch for {safe_name}: "
                f"server says {offset}, we have {existing} — restarting transfer"
            )
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            offset = 0

    # ── Write ─────────────────────────────────────────────────────────────────
    write_mode = "ab" if offset > 0 else "wb"
    action = f"resuming at byte {offset:,}" if offset > 0 else "starting fresh"
    logger.info(f"Receiving '{safe_name}' — {action} ({content_length / 1e6:.1f} MB incoming)")

    bytes_written = 0
    try:
        with open(tmp_path, write_mode) as f:
            for chunk in request.stream:
                f.write(chunk)
                bytes_written += len(chunk)

        tmp_size = os.path.getsize(tmp_path)

        if total_size > 0 and tmp_size < total_size:
            # More data expected in a future push
            logger.info(f"Partial receive: {tmp_size / 1e6:.1f} / {total_size / 1e6:.1f} MB")
            return jsonify({
                "ok":            True,
                "complete":      False,
                "bytes_written": bytes_written,
                "bytes_on_disk": tmp_size,
                "total_size":    total_size,
            })

        # Complete — atomically promote
        os.replace(tmp_path, final_path)
        logger.info(f"Transfer complete: {safe_name} ({total_size / 1e6:.1f} MB) ✓")

    except Exception as e:
        # Keep .tmp — good bytes survive for resume
        logger.error(f"Receive error for {safe_name}: {e}")
        abort(500, description=str(e))

    # ── Trigger media server refresh ──────────────────────────────────────────
    media_adapter.refresh_library()

    return jsonify({
        "ok": True,
        "complete": True,
        "filename": safe_name,
        "bytes_written": bytes_written,
        "path": final_path,
        "media_server": media_adapter.name,
    })


# ── File list ─────────────────────────────────────────────────────────────────

@app.route("/api/files", methods=["GET"])
@require_auth
def list_files():
    return jsonify({"files": _list_files(), "media_dir": MEDIA_DIR})


# ── Media server info ─────────────────────────────────────────────────────────

@app.route("/api/media-server", methods=["GET"])
@require_auth
def media_server_info():
    """Return the active media server adapter and available options."""
    return jsonify({
        "active":    media_adapter.name,
        "supported": SUPPORTED_SERVERS,
    })


# ── LRU eviction ─────────────────────────────────────────────────────────────

def _evict_if_needed(needed_bytes: int):
    required = needed_bytes + MIN_FREE_BYTES
    if shutil.disk_usage(MEDIA_DIR).free >= required:
        return

    files = sorted(_list_files(), key=lambda f: f["mtime"])
    for f in files:
        if shutil.disk_usage(MEDIA_DIR).free >= required:
            break
        try:
            os.remove(f["path"])
            logger.info(f"LRU evicted: {f['name']} ({f['size'] / 1e6:.0f} MB)")
        except Exception as e:
            logger.warning(f"Could not evict {f['path']}: {e}")


def _list_files() -> list[dict]:
    files = []
    for p in Path(MEDIA_DIR).rglob("*.mp4"):
        if p.is_file() and not p.name.endswith(".tmp"):
            stat = p.stat()
            files.append({
                "name": p.name,
                "path": str(p),
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            })
    return files


if __name__ == "__main__":
    # Use environment variable for host, default to 127.0.0.1 for safety
    import os
    app.run(host=os.environ.get("CARSTASH_AGENT_HOST", "127.0.0.1"), port=5001, debug=False)
