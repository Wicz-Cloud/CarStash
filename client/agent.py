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

import json
import logging
import os
import shutil
import threading
from functools import wraps
from pathlib import Path

from flask import Flask, abort, jsonify, request
from media_servers import SUPPORTED_SERVERS, get_adapter

MEDIA_DIR = os.environ.get("CARSTASH_MEDIA_DIR", "/mnt/carstash/media")
MIN_FREE_BYTES = int(os.environ.get("CARSTASH_MIN_FREE_GB", "2")) * 1024**3
AUTH_TOKEN = os.environ.get("CARSTASH_AUTH_TOKEN")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s — %(message)s",
)
os.makedirs(MEDIA_DIR, exist_ok=True)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── Per-file locking (parallel stream support) ────────────────────────────────
_file_locks: dict[str, threading.Lock] = {}
_lock_registry = threading.Lock()


def _get_file_lock(name: str) -> threading.Lock:
    with _lock_registry:
        if name not in _file_locks:
            _file_locks[name] = threading.Lock()
        return _file_locks[name]


# ── Segment tracking (meta sidecar) ──────────────────────────────────────────

def _meta_path(tmp_path: str) -> str:
    return tmp_path + ".meta"


def _load_meta(tmp_path: str) -> dict:
    try:
        with open(_meta_path(tmp_path)) as f:
            return json.load(f)
    except Exception:
        return {"total": 0, "segments": []}


def _save_meta(tmp_path: str, meta: dict) -> None:
    with open(_meta_path(tmp_path), "w") as f:
        json.dump(meta, f)


def _merge_segments(segments: list) -> list:
    """Merge overlapping/adjacent [start, end) byte ranges."""
    if not segments:
        return []
    sorted_segs = sorted(segments, key=lambda s: s[0])
    merged = [list(sorted_segs[0])]
    for s, e in sorted_segs[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return merged


def _sequential_offset(meta: dict) -> int:
    """Return the largest contiguous covered range starting from byte 0."""
    segs = sorted(meta.get("segments", []), key=lambda s: s[0])
    off = 0
    for s, e in segs:
        if s <= off:
            off = max(off, e)
        else:
            break
    return off
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
    return jsonify(
        {
            "ok": True,
            "media_server": media_adapter.name,
            "free_bytes": usage.free,
            "used_bytes": usage.used,
            "total_bytes": usage.total,
            "file_count": len(files),
            "media_dir": MEDIA_DIR,
        }
    )


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
        meta = _load_meta(tmp_path)
        if meta.get("segments"):
            # Parallel transfer in progress — return contiguous sequential offset
            offset = _sequential_offset(meta)
        else:
            # Legacy sequential transfer — use file size
            offset = os.path.getsize(tmp_path)
        return jsonify({"offset": offset, "complete": False, "tmp_exists": True})

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

    # ── Parse Content-Range: "bytes start-end/total" ─────────────────────────
    content_range = request.headers.get("Content-Range", "")
    start_byte = 0
    end_byte = -1
    total_size = 0

    if content_range.startswith("bytes "):
        try:
            range_part, total_part = content_range[6:].split("/")
            start_byte = int(range_part.split("-")[0])
            end_byte = int(range_part.split("-")[1])
            total_size = int(total_part)
        except (ValueError, IndexError):
            abort(400, description=f"Malformed Content-Range: {content_range}")
    else:
        total_size = request.content_length or 0
        end_byte = total_size - 1

    content_length = request.content_length
    if not content_length and total_size > 0:
        content_length = end_byte - start_byte + 1

    if content_length:
        _evict_if_needed(content_length)

    file_lock = _get_file_lock(safe_name)

    # ── Initialize tmp file if needed ─────────────────────────────────────────
    # Only create/truncate when there is no in-progress transfer for this file.
    # Checking start_byte==0 is NOT sufficient — segments can arrive out of order,
    # so segment 0 could arrive after segment 2 has already written to the file.
    # We use the meta's total_size to decide: if a different file is in progress
    # (or nothing is), reset; if the same file is already in progress, just write.
    with file_lock:
        meta = _load_meta(tmp_path)
        existing_total = meta.get("total", 0)
        if not os.path.exists(tmp_path) or existing_total != total_size:
            with open(tmp_path, "wb"):
                pass  # create / truncate stale file
            _save_meta(tmp_path, {"total": total_size, "segments": []})

    # ── Write at the correct byte offset (supports parallel streams) ──────────
    # Use os.pwrite() instead of seek()+write() — pwrite is atomic for the
    # seek+write pair, so concurrent streams writing to non-overlapping byte
    # ranges cannot interleave and corrupt each other's data.
    action = f"bytes {start_byte:,}–{end_byte:,}" if end_byte >= 0 else f"byte {start_byte:,}"
    logger.info(f"Receiving '{safe_name}' — writing {action} of {total_size / 1e6:.1f} MB")

    bytes_written = 0
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            pos = start_byte
            for chunk in request.stream:
                n = 0
                while n < len(chunk):
                    written = os.pwrite(fd, chunk[n:], pos + n)
                    n += written
                bytes_written += len(chunk)
                pos += len(chunk)
        finally:
            os.close(fd)
    except Exception as e:
        logger.error(f"Receive error for {safe_name}: {e}")
        abort(500, description=str(e))

    # ── Update meta and check for completion ──────────────────────────────────
    is_complete = False
    seq_off = 0
    with file_lock:
        meta = _load_meta(tmp_path)
        actual_end = start_byte + bytes_written
        meta["segments"] = _merge_segments(meta.get("segments", []) + [[start_byte, actual_end]])
        seq_off = _sequential_offset(meta)
        is_complete = total_size > 0 and seq_off >= total_size
        if is_complete:
            os.replace(tmp_path, final_path)
            try:
                os.remove(_meta_path(tmp_path))
            except OSError:
                pass
            logger.info(f"Transfer complete: {safe_name} ({total_size / 1e6:.1f} MB) ✓")
        else:
            _save_meta(tmp_path, meta)
            if total_size > 0:
                logger.info(
                    f"Partial receive: {seq_off / 1e6:.1f} / {total_size / 1e6:.1f} MB "
                    f"({seq_off / total_size * 100:.1f}%)"
                )

    if is_complete:
        media_adapter.refresh_library()
        return jsonify(
            {
                "ok": True,
                "complete": True,
                "filename": safe_name,
                "bytes_written": bytes_written,
                "path": final_path,
                "media_server": media_adapter.name,
            }
        )

    return jsonify(
        {
            "ok": True,
            "complete": False,
            "bytes_written": bytes_written,
            "bytes_on_disk": seq_off,
            "total_size": total_size,
        }
    )


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
    return jsonify(
        {
            "active": media_adapter.name,
            "supported": SUPPORTED_SERVERS,
        }
    )


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
            files.append(
                {
                    "name": p.name,
                    "path": str(p),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                }
            )
    return files


if __name__ == "__main__":
    # Use environment variable for host, default to 127.0.0.1 for safety
    import os

    app.run(host=os.environ.get("CARSTASH_AGENT_HOST", "127.0.0.1"), port=5001, debug=False)
