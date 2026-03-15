# CarStash — Architecture

## Design principles

**The server initiates everything.** The Pi client is passive by design. It never calls home, never polls, never decides when to sync. This is intentional — the Pi may be in a moving car with an unreliable or absent connection. Relying on it to initiate transfers would mean missed syncs every time it powers on away from home.

**Resume over retry.** Dropped connections don't restart transfers. The Pi keeps every byte it receives. The server tracks progress per-item and resumes from the last confirmed byte on the next successful connection.

**Transcode once, push anywhere.** The server caches transcoded files keyed by source path + quality. Requesting the same movie twice doesn't re-encode it.

---

## Components

### Server (`server/`)

| Module | Role |
|--------|------|
| `app.py` | Flask app — serves the web UI and REST API, wires up background services |
| `sync/queue.py` | Persistent queue with state machine (`queued → transcoding → ready → pushing → done`) |
| `sync/worker.py` | Background thread — picks up `queued` items, runs ffmpeg, writes to cache |
| `sync/dispatcher.py` | Heartbeat poller — pings Pi every 30s, pushes next `ready` item when reachable |
| `sync/transcode.py` | ffmpeg wrapper — probes files, builds optimised encode commands, tracks progress |

### Client (`client/`)

| Module | Role |
|--------|------|
| `agent.py` | Flask app — three endpoints: status, offset query, file receive |
| `media_servers.py` | Adapter factory — Plex, Jellyfin, Emby, Kodi, None |

---

## Queue state machine

```
         ┌──────────┐
         │  queued  │  ← added via UI or API
         └────┬─────┘
              │ TranscodeWorker picks up
              ▼
       ┌─────────────┐
       │ transcoding │
       └──────┬──────┘
              │ ffmpeg complete
              ▼
          ┌───────┐
          │ ready │  ← cached MP4 exists on server
          └───┬───┘
              │ HeartbeatPoller: Pi reachable + push starts
              ▼
         ┌─────────┐
         │ pushing │
         └────┬────┘
        ┌─────┴──────┐
        │             │
        ▼             ▼
   ┌──────┐    ┌─────────────┐
   │ done │    │ interrupted │  ← connection dropped mid-push
   └──────┘    └──────┬──────┘
                      │ next heartbeat: resume from offset
                      └──────────────────┐
                                         ▼
                                     ┌───────┐
                                     │ ready │  (retry)
                                     └───────┘
                                         │ exceeded MAX_ATTEMPTS
                                         ▼
                                     ┌────────┐
                                     │ failed │  ← manual retry via UI
                                     └────────┘
```

---

## Resumable transfer protocol

CarStash uses plain HTTP with `Content-Range` headers — no rsync daemon, no SSH keys required.

### Before every push (server → Pi)

```
GET /api/receive/{filename}/offset
→ {"offset": 412876800, "complete": false, "tmp_exists": true}
```

The server seeks to byte 412876800 in the cached file and sends only the remainder.

### Push request

```
PUT /api/receive/{filename}
Content-Range: bytes 412876800-2147483647/2147483648
Content-Length: 1734606848
Content-Type: application/octet-stream
```

### Pi behaviour

- `offset == 0` → open `.tmp` in write mode (`wb`)
- `offset > 0`  → open `.tmp` in append mode (`ab`)
- All bytes received → atomic rename `.tmp` → `.mp4`
- Connection drops → `.tmp` stays on disk unchanged

### On the server side

- `ConnectionError` mid-push → item marked `interrupted`
- Next heartbeat: query offset again → resume from new position
- If Pi offset doesn't match server expectation → Pi wipes its `.tmp`, restarts cleanly

---

## LRU eviction

Before writing any incoming file, the Pi checks if free space minus `CARSTASH_MIN_FREE_GB` (default 2 GB) is sufficient. If not, it deletes the oldest files by `mtime` until enough space is available.

Files currently being transferred (`.tmp`) are never evicted.

---

## Media server adapters

All adapters implement a single method: `refresh_library() → bool`.

| Adapter | Mechanism |
|---------|-----------|
| Plex | `GET /library/sections/{section}/refresh` with `X-Plex-Token` header |
| Jellyfin | `POST /Library/Refresh` with `X-Emby-Token` header |
| Emby | `POST /Library/Refresh` with `api_key` query param |
| Kodi | `POST /jsonrpc` → `VideoLibrary.Scan` (JSON-RPC 2.0) |
| None | No-op — files stored, user points player manually |

Adapter failures are logged but never propagate — a failed library scan never fails a transfer.
