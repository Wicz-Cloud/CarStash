"""
PlexSync — Server Transcode Worker

Picks up "queued" items from SyncQueue and runs them through ffmpeg.
Runs as a single background thread — one encode at a time is fine for N150.

The N150's Intel N150 CPU handles libx264 fast preset at 720p comfortably.
Encoded files land in CACHE_DIR on the server; they're reused if the same
source is requested again (keyed by source path + quality).
"""

import hashlib
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from .queue import SyncQueue
from .transcode import Transcoder, probe

logger = logging.getLogger(__name__)

# Use system temp dir as default for cache, not hardcoded /tmp
CACHE_DIR = os.environ.get("PLEXSYNC_CACHE", os.path.join(tempfile.gettempdir(), "plexsync_cache"))
POLL_SLEEP = 5  # seconds to wait between queue checks when idle


def _cache_key(source_path: str, quality: str) -> str:
    stem = Path(source_path).stem
    # Use SHA-256 for filename hashing (not for security purposes)
    h = hashlib.sha256(f"{source_path}:{quality}".encode()).hexdigest()[:8]
    return f"{stem}_{h}.mp4"


class TranscodeWorker:
    def __init__(self, queue: SyncQueue, cache_dir: str = CACHE_DIR):
        self.queue = queue
        self.cache_dir = cache_dir
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._current_item_id: Optional[str] = None
        self._transcoder = Transcoder()
        os.makedirs(cache_dir, exist_ok=True)

    @property
    def current_item_id(self) -> Optional[str]:
        return self._current_item_id

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("Transcode worker started")

    def stop(self):
        self._running = False

    def cache_path_for(self, source_path: str, quality: str) -> str:
        return os.path.join(self.cache_dir, _cache_key(source_path, quality))

    def _loop(self):
        while self._running:
            item = self.queue.next_to_transcode()
            if item is None:
                time.sleep(POLL_SLEEP)
                continue

            self._current_item_id = item.id
            self.queue.set_state(item.id, "transcoding")

            try:
                output_path = self._transcode(item)
                size = os.path.getsize(output_path)
                self.queue.set_state(
                    item.id,
                    "ready",
                    transcoded_path=output_path,
                    size_bytes=size,
                )
            except Exception as e:
                self.queue.set_state(item.id, "failed", error=str(e))
                logger.error(f"[{item.id}] Transcode failed: {e}")
            finally:
                self._current_item_id = None

    def _transcode(self, item) -> str:
        """Transcode source file, using cached version if available."""
        cached = self.cache_path_for(item.source_path, item.quality)

        if os.path.exists(cached):
            logger.info(f"[{item.id}] Cache hit: {cached}")
            return cached

        if not os.path.exists(item.source_path):
            raise FileNotFoundError(f"Source not found: {item.source_path}")

        # Probe to decide if we even need to transcode
        probe(item.source_path)

        # Placeholder: actual transcode logic goes here
        # For now, raise if not supported
        raise NotImplementedError("Transcoding not implemented in this stub")
