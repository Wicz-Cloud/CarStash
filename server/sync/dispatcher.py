"""
PlexSync — Heartbeat Poller & Push Dispatcher

Runs on the server as a background thread.
Every POLL_INTERVAL seconds it checks if the Pi is reachable, then
pushes the next queued file if one is ready.

Pi reachability:
  HTTP GET http://{pi_ip}:{pi_port}/api/status
  Expects: 200 JSON {"ok": true, "free_bytes": N}
  If the request times out or fails → Pi is unreachable, skip.

Resumable file push:
  Before sending, server queries the Pi for how many bytes it already has:
    GET /api/receive/<filename>/offset → {"offset": N, "tmp_exists": bool}

  Server then seeks to byte N in the file and sends only the remainder:
    PUT /api/receive/<filename>
    Content-Range: bytes {offset}-{end}/{total}
    Content-Length: {total - offset}

  Pi appends to its existing .tmp file from that offset.
  On completion, Pi atomically renames .tmp → final file.

  If the car drives away mid-transfer:
    - Pi keeps the .tmp with however many bytes arrived
    - Server marks item "interrupted"
    - Next heartbeat: server queries offset again, resumes from there
    - No bytes are re-sent, no re-transcode needed
"""

import logging
import os
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests

from .queue import QueueItem, SyncQueue

logger = logging.getLogger(__name__)

POLL_INTERVAL = 30  # seconds between reachability checks
PUSH_TIMEOUT = 10  # seconds for initial connection
STREAM_CHUNK = 4 * 1024 * 1024  # 4 MB chunks during push
MAX_ATTEMPTS = 5  # give up after this many failed pushes
AUTH_TOKEN = os.environ.get("CARSTASH_AUTH_TOKEN", "")
PARALLEL_STREAMS = int(os.environ.get("CARSTASH_PARALLEL_STREAMS", "2"))
MIN_PARALLEL_BYTES = 64 * 1024 * 1024  # only parallelize files > 64 MB remaining


class _SegmentReader:
    """
    File-like wrapper that reads exactly `limit` bytes from an open file handle,
    calling on_chunk(n) after each read.

    Using a file-like object (with read()) instead of a generator ensures that
    requests/urllib3 sends a fixed-length body using Content-Length rather than
    Transfer-Encoding: chunked.  Chunked encoding causes Flask/Werkzeug on the Pi
    to raise "Invalid chunk header" when the TCP stream is cut mid-chunk.
    """

    def __init__(self, f, limit: int, on_chunk=None):
        self._f = f
        self._remaining = limit
        self._on_chunk = on_chunk

    def read(self, size=-1):
        if self._remaining <= 0:
            return b""
        to_read = self._remaining if size < 0 else min(size, self._remaining)
        to_read = min(to_read, STREAM_CHUNK)  # cap so progress is reported every 4 MB
        chunk = self._f.read(to_read)
        if chunk:
            self._remaining -= len(chunk)
            if self._on_chunk:
                self._on_chunk(len(chunk))
        return chunk


def _sanitize_header(v: str) -> str:
    """Strip control/newline characters from header values."""
    if v is None:
        return ""
    return re.sub(r"[\r\n]+", " ", str(v))[:200]


class HeartbeatPoller:
    def __init__(
        self,
        queue: SyncQueue,
        pi_ip: str,
        pi_port: int = 5001,
        on_status_change=None,  # callback(reachable: bool)
    ):
        self.queue = queue
        self.pi_ip = pi_ip
        self.pi_port = pi_port
        self.on_status_change = on_status_change

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._pi_reachable = False
        self._pi_free_bytes: int = 0
        self._lock = threading.Lock()
        self._cycle_lock = threading.Lock()  # prevents concurrent _cycle() runs

    @property
    def pi_reachable(self) -> bool:
        with self._lock:
            return self._pi_reachable

    @property
    def pi_free_bytes(self) -> int:
        with self._lock:
            return self._pi_free_bytes

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info(f"Heartbeat poller started — targeting {self.pi_ip}:{self.pi_port}")

    def stop(self):
        self._running = False

    def force_poll(self):
        """Trigger an immediate poll cycle (e.g. after a new item is queued)."""
        def _run():
            # Non-blocking acquire: if a cycle is already running, skip this one
            if self._cycle_lock.acquire(blocking=False):
                try:
                    self._cycle()
                finally:
                    self._cycle_lock.release()
            else:
                logger.debug("Poll already in progress — skipping forced cycle")

        threading.Thread(target=_run, daemon=True).start()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _loop(self):
        while self._running:
            with self._cycle_lock:
                self._cycle()
            time.sleep(POLL_INTERVAL)

    def _cycle(self):
        reachable, free_bytes = self._ping_pi()

        # Notify on state change
        with self._lock:
            changed = reachable != self._pi_reachable
            self._pi_reachable = reachable
            self._pi_free_bytes = free_bytes

        if changed and self.on_status_change:
            self.on_status_change(reachable)

        if not reachable:
            logger.debug(f"Pi {self.pi_ip} unreachable — skipping push cycle")
            return

        # Pi is up — push the next ready item
        item = self.queue.next_to_push()
        if item is None:
            logger.debug("Pi reachable but nothing in queue to push")
            return

        if item.push_attempts >= MAX_ATTEMPTS:
            self.queue.set_state(item.id, "failed", error=f"Exceeded {MAX_ATTEMPTS} push attempts")
            logger.error(f"[{item.id}] Giving up after {MAX_ATTEMPTS} attempts")
            return

        self._push_item(item, free_bytes)

    def _ping_pi(self) -> tuple[bool, int]:
        """Returns (reachable, free_bytes). free_bytes=0 if not reachable."""
        url = f"http://{self.pi_ip}:{self.pi_port}/api/status"
        try:
            resp = requests.get(url, timeout=5, headers={"X-CarStash-Token": AUTH_TOKEN})
            if resp.status_code == 200:
                data = resp.json()
                return True, data.get("free_bytes", 0)
        except Exception:
            pass
        return False, 0

    def _query_offset(self, filename: str) -> int:
        """
        Ask the Pi how many bytes of this file it already has on disk.
        Returns 0 if the file doesn't exist yet (fresh transfer).
        Returns N if a .tmp partial file exists from a previous interrupted push.
        """
        # Ensure filename is safely encoded to avoid path injection
        safe_name = urllib.parse.quote(str(filename), safe="")
        url = f"http://{self.pi_ip}:{self.pi_port}/api/receive/{safe_name}/offset"
        try:
            resp = requests.get(url, timeout=5, headers={"X-CarStash-Token": AUTH_TOKEN})
            if resp.status_code == 200:
                return int(resp.json().get("offset", 0))
        except Exception:
            pass
        return 0

    def _push_item(self, item: QueueItem, pi_free_bytes: int):
        """
        Push the transcoded file to the Pi using parallel streams for large files,
        falling back to a single stream for small files or resumptions from near the end.
        """
        path = item.transcoded_path
        if not path or not os.path.exists(path):
            self.queue.set_state(item.id, "failed", error="Transcoded file missing on server")
            return

        file_size = os.path.getsize(path)
        offset = self._query_offset(item.dest_filename)

        if offset == file_size:
            logger.info(f"[{item.id}] Pi already has complete file — marking done")
            self.queue.set_state(item.id, "done", push_progress=100.0)
            return

        remaining = file_size - offset

        if offset > 0:
            logger.info(
                f"[{item.id}] Resuming from byte {offset:,} of {file_size:,} "
                f"({offset / file_size * 100:.1f}% already transferred)"
            )
        else:
            logger.info(f"[{item.id}] Starting push of {item.name} ({file_size / 1e6:.0f} MB)")

        if pi_free_bytes > 0 and remaining > pi_free_bytes:
            logger.warning(
                f"[{item.id}] Pi has {pi_free_bytes / 1e9:.1f} GB free, "
                f"need {remaining / 1e9:.1f} GB — Pi will evict old files"
            )

        safe_name = urllib.parse.quote(str(item.dest_filename), safe="")
        url = f"http://{self.pi_ip}:{self.pi_port}/api/receive/{safe_name}"
        initial_progress = round(offset / file_size * 100, 1) if file_size > 0 else 0.0
        self.queue.set_state(item.id, "pushing", push_attempts=item.push_attempts + 1, push_progress=initial_progress)

        # Choose number of parallel streams based on remaining data size
        n_streams = min(PARALLEL_STREAMS, max(1, remaining // MIN_PARALLEL_BYTES))

        if n_streams > 1:
            self._push_parallel(item, path, file_size, offset, url, n_streams)
        else:
            self._push_sequential(item, path, file_size, offset, url)

    def _push_sequential(self, item: QueueItem, path: str, file_size: int, offset: int, url: str):
        """Send the file as a single HTTP stream (used for small files or final segments)."""
        try:
            with open(path, "rb") as f:
                f.seek(offset)

                sent = [0]

                def _on_chunk(n: int):
                    sent[0] += n
                    self.queue.update_push_progress(
                        item.id, round((offset + sent[0]) / file_size * 100, 1)
                    )

                resp = requests.put(
                    url,
                    data=_SegmentReader(f, file_size - offset, _on_chunk),
                    headers={
                        "Content-Range": f"bytes {offset}-{file_size - 1}/{file_size}",
                        "Content-Length": str(file_size - offset),
                        "Content-Type": "application/octet-stream",
                        "X-Item-Id": _sanitize_header(item.id),
                        "X-Item-Name": _sanitize_header(item.name),
                        "X-CarStash-Token": AUTH_TOKEN,
                    },
                    timeout=(PUSH_TIMEOUT, None),
                )

            if resp.status_code == 200:
                self.queue.set_state(item.id, "done", push_progress=100.0)
                logger.info(f"[{item.id}] Push complete ✓")
            else:
                raise RuntimeError(f"Pi returned HTTP {resp.status_code}: {resp.text[:200]}")

        except requests.exceptions.ConnectionError:
            self.queue.set_state(item.id, "interrupted", error="Connection lost mid-transfer — will resume")
            logger.warning(f"[{item.id}] Connection lost mid-push — will resume next heartbeat")
        except Exception as e:
            self.queue.set_state(item.id, "interrupted", error=str(e))
            logger.error(f"[{item.id}] Push failed: {e}")

    def _push_parallel(
        self,
        item: QueueItem,
        path: str,
        file_size: int,
        offset: int,
        url: str,
        n_streams: int,
    ):
        """
        Push the file using N parallel TCP streams.

        Each stream sends a distinct, non-overlapping byte range via Content-Range.
        The Pi must support random-access writes (agent.py ≥ parallel-capable version).
        """
        remaining = file_size - offset
        seg_size = remaining // n_streams

        # Build segment list: [(start, end_exclusive), ...]
        segments = []
        pos = offset
        for i in range(n_streams):
            end = file_size if i == n_streams - 1 else pos + seg_size
            segments.append((pos, end))
            pos = end

        logger.info(
            f"[{item.id}] Parallel push — {n_streams} streams, "
            f"{remaining / 1e6:.0f} MB remaining"
        )

        # Shared progress counter — bytes sent in THIS push call (not including prior offset)
        total_sent = [0]
        sent_lock = threading.Lock()
        errors: list[tuple[str, str]] = []

        def _send_segment(seg_start: int, seg_end: int):
            seg_len = seg_end - seg_start
            seg_sent = [0]  # bytes sent by THIS segment (for rollback on error)
            try:
                with open(path, "rb") as f:
                    f.seek(seg_start)

                    def _on_chunk(n: int):
                        seg_sent[0] += n
                        with sent_lock:
                            total_sent[0] += n
                            progress = round((offset + total_sent[0]) / file_size * 100, 1)
                        self.queue.update_push_progress(item.id, progress)

                    resp = requests.put(
                        url,
                        data=_SegmentReader(f, seg_len, _on_chunk),
                        headers={
                            "Content-Range": f"bytes {seg_start}-{seg_end - 1}/{file_size}",
                            "Content-Length": str(seg_len),
                            "Content-Type": "application/octet-stream",
                            "X-Item-Id": _sanitize_header(item.id),
                            "X-Item-Name": _sanitize_header(item.name),
                            "X-CarStash-Token": AUTH_TOKEN,
                        },
                        timeout=(PUSH_TIMEOUT, None),
                    )

                if resp.status_code != 200:
                    raise RuntimeError(f"Pi returned HTTP {resp.status_code}: {resp.text[:200]}")

            except requests.exceptions.ConnectionError as exc:
                with sent_lock:
                    total_sent[0] = max(0, total_sent[0] - seg_sent[0])
                    errors.append(("connection", str(exc)))
            except Exception as exc:
                with sent_lock:
                    total_sent[0] = max(0, total_sent[0] - seg_sent[0])
                    errors.append(("error", str(exc)))

        with ThreadPoolExecutor(max_workers=n_streams) as executor:
            futures = {executor.submit(_send_segment, s, e): (s, e) for s, e in segments}
            for future in as_completed(futures):
                future.result()  # surface any unhandled exceptions not caught inside

        if not errors:
            self.queue.set_state(item.id, "done", push_progress=100.0)
            logger.info(f"[{item.id}] Parallel push complete ✓ ({n_streams} streams)")
        elif any(e[0] == "connection" for e in errors):
            self.queue.set_state(item.id, "interrupted", error="Connection lost mid-transfer — will resume")
            logger.warning(f"[{item.id}] Connection lost during parallel push — will resume")
        else:
            self.queue.set_state(item.id, "interrupted", error=errors[0][1])
            logger.error(f"[{item.id}] Parallel push failed: {errors[0][1]}")


def is_pi_reachable(pi_ip: str, pi_port: int = 5001) -> bool:
    """Standalone function for testability: returns True if Pi responds to /api/status."""
    url = f"http://{pi_ip}:{pi_port}/api/status"
    try:
        resp = requests.get(url, timeout=5, headers={"X-CarStash-Token": AUTH_TOKEN})
        return resp.status_code == 200
    except Exception:
        return False


def get_pi_offset(pi_ip: str, dest_filename: str, pi_port: int = 5001) -> int:
    """Standalone function to query the Pi for the current offset for a file."""
    safe_name = urllib.parse.quote(dest_filename, safe="")
    url = f"http://{pi_ip}:{pi_port}/api/receive/{safe_name}/offset"
    try:
        resp = requests.get(url, timeout=5, headers={"X-CarStash-Token": AUTH_TOKEN})
        if resp.status_code == 200:
            return int(resp.json().get("offset", 0))
    except Exception:
        pass
    return 0


def push_file(pi_ip: str, src_path: str, dest_filename: str, pi_port: int = 5001) -> bool:
    """Standalone function to push a file to the Pi, resuming from the correct offset."""
    file_size = os.path.getsize(src_path)
    offset = get_pi_offset(pi_ip, dest_filename, pi_port)
    if offset >= file_size:
        return True  # Pi already has the complete file
    safe_name = urllib.parse.quote(dest_filename, safe="")
    url = f"http://{pi_ip}:{pi_port}/api/receive/{safe_name}"
    try:
        with open(src_path, "rb") as f:
            f.seek(offset)

            def _chunked_generator():
                while True:
                    chunk = f.read(STREAM_CHUNK)
                    if not chunk:
                        break
                    yield chunk

            resp = requests.put(
                url,
                data=_chunked_generator(),
                headers={
                    "Content-Range": f"bytes {offset}-{file_size - 1}/{file_size}",
                    "Content-Length": str(file_size - offset),
                    "Content-Type": "application/octet-stream",
                    "X-CarStash-Token": AUTH_TOKEN,
                },
                timeout=(PUSH_TIMEOUT, None),
            )
        return resp.status_code == 200
    except Exception:
        return False
