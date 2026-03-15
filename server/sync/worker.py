"""
PlexSync — Server Transcode Worker

Picks up "queued" items from SyncQueue and runs them through ffmpeg.
Runs as a single background thread — one encode at a time is fine for N150.

The N150's Intel N150 CPU handles libx264 fast preset at 720p comfortably.
Encoded files land in CACHE_DIR on the server; they're reused if the same
source is requested again (keyed by source path + quality).
"""

import threading
import time
import hashlib
import os
import shutil
import logging
from pathlib import Path
from typing import Optional

from sync.queue import SyncQueue
from sync.transcode import Transcoder, probe, QUALITY_PRESETS

logger = logging.getLogger(__name__)

CACHE_DIR   = os.environ.get("PLEXSYNC_CACHE", "/tmp/plexsync_cache")
POLL_SLEEP  = 5     # seconds to wait between queue checks when idle


def _cache_key(source_path: str, quality: str) -> str:
    """Stable filename for a transcoded file in the cache."""
    h = hashlib.md5(f"{source_path}:{quality}".encode()).hexdigest()[:12]
    stem = Path(source_path).stem
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

    # ── Internal ──────────────────────────────────────────────────────────────

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
                self.queue.set_state(item.id, "ready",
                                     transcoded_path=output_path,
                                     size_bytes=size)
                logger.info(f"[{item.id}] Transcode done → {output_path} ({size/1e6:.0f} MB)")
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
        info = probe(item.source_path)
        if info.is_already_compatible:
            # Just copy into cache so the push path is uniform
            logger.info(f"[{item.id}] Already compatible — copying to cache")
            shutil.copy2(item.source_path, cached)
            return cached

        # Run ffmpeg
        done = threading.Event()
        last_progress = [0.0]

        def _on_progress(job):
            last_progress[0] = job.progress
            if job.status in ("done", "error", "skipped"):
                done.set()

        self._transcoder.submit(
            job_id=item.id,
            input_path=item.source_path,
            output_path=cached,
            quality=item.quality,
            on_progress=_on_progress,
            skip_if_compatible=False,   # we already checked above
        )

        done.wait()
        transcode_job = self._transcoder.get_job(item.id)

        if transcode_job and transcode_job.status == "error":
            raise RuntimeError(transcode_job.error or "Unknown transcode error")

        return cached
