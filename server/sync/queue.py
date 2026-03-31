"""
PlexSync — Server Queue Manager

Tracks every sync request from queued → transcoding → ready → pushing → done.
Persisted to JSON so the server can restart without losing state.

State machine per item:
  queued → transcoding → ready → pushing → done
                                         ↘ failed (retryable)
                                  interrupted → ready (auto-retry on next heartbeat)
"""

import json
import logging
import os
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# Default queue state file (for test patching)
QUEUE_FILE = "queue_state.json"

STATES = ("queued", "transcoding", "ready", "pushing", "done", "failed", "interrupted")


@dataclass
class QueueItem:
    id: str
    source_path: str  # absolute path on server
    name: str  # display name
    dest_filename: str  # filename to use on Pi (always .mp4)
    quality: str = "balanced"
    state: str = "queued"
    priority: int = 0  # higher = pushed first
    transcoded_path: Optional[str] = None  # server-side optimized file
    size_bytes: int = 0
    push_attempts: int = 0
    push_progress: float = 0.0  # 0.0–100.0 during active push
    transcode_progress: float = 0.0  # 0.0–100.0 during transcode
    error: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    done_at: Optional[str] = None

    def to_dict(self):
        return asdict(self)

    def touch(self):
        self.updated_at = datetime.now().isoformat()


class SyncQueue:
    def list_items(self) -> list:
        """Return all items as dicts with id, filename, and status (for tests)."""
        with self._lock:
            result = []
            for item in self._items.values():
                status = getattr(item, "state", "")
                # Map internal states to test-expected values
                if status == "queued":
                    status = "pending"
                elif status == "pushing":
                    status = "transferring"
                result.append(
                    {
                        "id": item.id,
                        "filename": getattr(item, "dest_filename", getattr(item, "name", "")),
                        "status": status,
                    }
                )
            return result

    def set_status(self, item_id: str, status: str, **kwargs):
        """Alias for set_state to match test expectations. Maps 'transferring' to 'pushing'."""
        if status == "transferring":
            status = "pushing"
        return self.set_state(item_id, status, **kwargs)

    def __init__(self, state_path: str = None):
        # Always use QUEUE_FILE unless overridden (for test patching)
        self.state_path = state_path if state_path is not None else QUEUE_FILE
        self._items = {}
        self._lock = threading.Lock()
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self):
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path) as f:
                    for d in json.load(f).get("items", []):
                        item = QueueItem(**d)
                        # In-flight push/transcode at startup → retry-safe state
                        if item.state in ("pushing", "transcoding"):
                            item.state = "interrupted" if item.transcoded_path else "queued"
                            item.touch()
                            logger.info(f"[{item.id}] Reset {d['state']} → {item.state} after restart")
                        # Transcoded file disappeared (e.g. cache dir cleared) → re-transcode
                        elif item.state in ("ready", "interrupted") and item.transcoded_path and not os.path.exists(item.transcoded_path):
                            logger.warning(f"[{item.id}] Transcoded file missing ({item.transcoded_path}) — re-queuing")
                            item.state = "queued"
                            item.transcoded_path = None
                            item.push_progress = 0.0
                            item.touch()
                        self._items[item.id] = item
                counts = {s: sum(1 for i in self._items.values() if i.state == s) for s in STATES if any(i.state == s for i in self._items.values())}
                logger.info(f"Loaded {len(self._items)} queue items — {counts}")
            except Exception as e:
                logger.error(f"Failed to load queue state: {e}")

    def _save(self):
        data = {"items": [i.to_dict() for i in self._items.values()]}
        state_dir = os.path.dirname(self.state_path) or "."
        tmp = None
        with tempfile.NamedTemporaryFile(mode="w", dir=state_dir, delete=False) as tf:
            tmp = tf.name
            json.dump(data, tf, indent=2)
        try:
            try:
                os.chmod(tmp, 0o600)
            except Exception:
                pass
            os.replace(tmp, self.state_path)
            tmp = None  # replace succeeded — nothing to clean up
        except Exception:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            raise

    # ── Public API ────────────────────────────────────────────────────────────

    def add(self, source_path: str, name: str, quality: str = "balanced", priority: int = 0) -> QueueItem:
        with self._lock:
            item_id = str(uuid.uuid4())[:8]
            basename = os.path.splitext(os.path.basename(source_path))[0]
            item = QueueItem(
                id=item_id,
                source_path=source_path,
                name=name,
                dest_filename=f"{basename}.mp4",
                quality=quality,
                priority=priority,
            )
            self._items[item_id] = item
            self._save()
            logger.info(f"Queued [{item_id}] {name}")
            return item

    def remove(self, item_id: str):
        with self._lock:
            if item_id in self._items:
                del self._items[item_id]
                self._save()

    def get(self, item_id: str) -> Optional[QueueItem]:
        with self._lock:
            return self._items.get(item_id)

    def list_all(self) -> list[QueueItem]:
        with self._lock:
            return sorted(self._items.values(), key=lambda i: (-i.priority, i.created_at))

    def next_to_transcode(self) -> Optional[QueueItem]:
        """Return highest-priority item still needing transcoding."""
        with self._lock:
            candidates = [i for i in self._items.values() if i.state == "queued"]
            if not candidates:
                return None
            return max(candidates, key=lambda i: i.priority)

    def next_to_push(self) -> Optional[QueueItem]:
        """Return highest-priority item ready to push (or interrupted last time)."""
        with self._lock:
            candidates = [i for i in self._items.values() if i.state in ("ready", "interrupted")]
            if not candidates:
                return None
            # Sort by priority only — no secondary key that starves interrupted items
            return max(candidates, key=lambda i: i.priority)

    def set_state(self, item_id: str, state: str, **kwargs):
        if state not in STATES:
            raise ValueError(f"Unknown state: {state!r}")
        with self._lock:
            item = self._items.get(item_id)
            if item:
                item.state = state
                for k, v in kwargs.items():
                    setattr(item, k, v)
                item.touch()
                if state == "done":
                    item.done_at = datetime.now().isoformat()
                self._save()

    def update_push_progress(self, item_id: str, progress: float):
        """Called frequently during a push — updates in memory only, no disk write."""
        item = self._items.get(item_id)
        if item:
            item.push_progress = progress
            item.touch()

    def stats(self) -> dict:
        with self._lock:
            counts = {s: 0 for s in STATES}
            for item in self._items.values():
                counts[item.state] = counts.get(item.state, 0) + 1
            return counts
