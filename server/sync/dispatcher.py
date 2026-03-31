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
from typing import Optional

import requests

from .queue import QueueItem, SyncQueue

logger = logging.getLogger(__name__)

POLL_INTERVAL = 30  # seconds between reachability checks
PUSH_TIMEOUT = 10  # seconds for initial connection
STREAM_CHUNK = 256 * 1024  # 256 KB chunks during push
MAX_ATTEMPTS = 5  # give up after this many failed pushes
AUTH_TOKEN = os.environ.get("CARSTASH_AUTH_TOKEN", "")


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

    @property
    def pi_reachable(self) -> bool:
        return self._pi_reachable

    @property
    def pi_free_bytes(self) -> int:
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
        t = threading.Thread(target=self._cycle, daemon=True)
        t.start()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _loop(self):
        while self._running:
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
        Stream the transcoded file to the Pi, resuming from where we left off.

        Flow:
          1. Query Pi for how many bytes it already has (offset)
          2. If offset == file_size, it's already complete — mark done
          3. Seek to offset in the source file
          4. Send Content-Range header so Pi knows where to append
          5. Stream remaining bytes in chunks, updating progress
          6. On ConnectionError → mark interrupted, Pi keeps its .tmp for next time
        """
        path = item.transcoded_path
        if not path or not os.path.exists(path):
            self.queue.set_state(item.id, "failed", error="Transcoded file missing on server")
            return

        file_size = os.path.getsize(path)

        # ── Query resume offset ───────────────────────────────────────────────
        offset = self._query_offset(item.dest_filename)

        if offset == file_size:
            # Pi already has the complete file (e.g. server restarted after Pi received it)
            logger.info(f"[{item.id}] Pi already has complete file — marking done")
            self.queue.set_state(item.id, "done", push_progress=100.0)
            return

        if offset > 0:
            logger.info(
                f"[{item.id}] Resuming from byte {offset:,} of {file_size:,} "
                f"({offset/file_size*100:.1f}% already transferred)"
            )
        else:
            logger.info(f"[{item.id}] Starting fresh push of {item.name} ({file_size/1e6:.0f} MB)")

        remaining = file_size - offset

        # Check Pi has enough space for the remaining bytes
        if pi_free_bytes > 0 and remaining > pi_free_bytes:
            logger.warning(
                f"[{item.id}] Pi has {pi_free_bytes/1e9:.1f} GB free, "
                f"need {remaining/1e9:.1f} GB — Pi will evict old files"
            )

        # URL-encode filename
        safe_name = urllib.parse.quote(str(item.dest_filename), safe="")
        url = f"http://{self.pi_ip}:{self.pi_port}/api/receive/{safe_name}"
        self.queue.set_state(item.id, "pushing", push_attempts=item.push_attempts + 1)

        try:
            with open(path, "rb") as f:
                f.seek(offset)

                def _chunked_generator():
                    sent = 0
                    while True:
                        chunk = f.read(STREAM_CHUNK)
                        if not chunk:
                            break
                        sent += len(chunk)
                        # Progress accounts for bytes already on Pi from prior attempts
                        total_sent = offset + sent
                        progress = round(total_sent / file_size * 100, 1)
                        self.queue.update_push_progress(item.id, progress)
                        yield chunk

                # Sanitize header values to avoid header injection
                def _sanitize_header(v: str) -> str:
                    if v is None:
                        return ""
                    # strip control/newline characters
                    return re.sub(r"[\r\n]+", " ", str(v))[:200]

                headers = {
                    "Content-Range": f"bytes {offset}-{file_size - 1}/{file_size}",
                    "Content-Type": "application/octet-stream",
                    "X-Item-Id": _sanitize_header(item.id),
                    "X-Item-Name": _sanitize_header(item.name),
                    "X-CarStash-Token": AUTH_TOKEN,
                }

                resp = requests.put(
                    url,
                    data=_chunked_generator(),
                    headers=headers,
                    timeout=(PUSH_TIMEOUT, None),  # (connect_timeout, read_timeout=unlimited)
                )

            if resp.status_code == 200:
                self.queue.set_state(item.id, "done", push_progress=100.0)
                logger.info(f"[{item.id}] Push complete ✓")
            else:
                raise RuntimeError(f"Pi returned HTTP {resp.status_code}: {resp.text[:200]}")

        except requests.exceptions.ConnectionError:
            # Pi went away mid-transfer — its .tmp file has however many bytes arrived.
            # Next heartbeat will query the offset again and resume from there.
            self.queue.set_state(item.id, "interrupted", error="Connection lost mid-transfer — will resume")
            logger.warning(f"[{item.id}] Connection lost mid-push — will resume next heartbeat")

        except Exception as e:
            self.queue.set_state(item.id, "interrupted", error=str(e))
            logger.error(f"[{item.id}] Push failed: {e}")


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
    url = f"http://{pi_ip}:{pi_port}/api/receive/{dest_filename}/offset"
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
    remaining = file_size - offset
    url = f"http://{pi_ip}:{pi_port}/api/receive/{dest_filename}"
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
                    "Content-Type": "application/octet-stream",
                    "X-CarStash-Token": AUTH_TOKEN,
                },
                timeout=(PUSH_TIMEOUT, None),
            )
        return resp.status_code == 200
    except Exception:
        return False
