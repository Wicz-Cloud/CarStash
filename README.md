# CarStash 🚗📼

**Server-initiated offline media sync for car-based Raspberry Pi setups.**

CarStash lets you load up a Raspberry Pi with transcoded, car-optimised movies and TV before a road trip — without touching the Pi. Queue up what you want from your home server's web UI, and CarStash handles transcoding, scheduling, and delivery automatically the next time the Pi is home on your network.

When the car drives away mid-transfer, CarStash resumes exactly where it left off next time the Pi connects. No re-encoding. No re-sending bytes already received.

---

## How it works

```
Home Server (N150/any)          WiFi / LAN              Car Pi (3B/4/any)
───────────────────────         ──────────         ───────────────────────
  Web UI + queue manager    →   heartbeat ping  →   passive agent
  Transcode (ffmpeg)        →   file push       →   resume-capable receiver
  Heartbeat poller          ←   confirm done    ←   media server refresh
```

1. You queue a movie from the server's web UI
2. Server transcodes it to a Pi/car-screen-optimised MP4 (cached for reuse)
3. Every 30 seconds, the server checks if the Pi is reachable on the LAN
4. When reachable, it pushes the file over HTTP with `Content-Range` resume support
5. If the connection drops (car drives away), the Pi keeps what it has
6. Next time the Pi is home, the transfer picks up from the last received byte
7. On completion, the Pi triggers a library scan on your media server

---

## Supported media servers (client side)

| Server      | Protocol        | Trigger method            |
|-------------|-----------------|---------------------------|
| **Plex**    | HTTP REST       | `GET /library/sections/{id}/refresh` |
| **Jellyfin**| HTTP REST       | `POST /Library/Refresh`   |
| **Emby**    | HTTP REST       | `POST /Library/Refresh`   |
| **Kodi**    | HTTP JSON-RPC   | `VideoLibrary.Scan`       |
| **None**    | —               | Files stored, no scan     |

---

## Requirements

### Server
- Python 3.10+
- `ffmpeg` installed (`sudo apt install ffmpeg`)
- Any Linux machine — designed for Intel N150 but runs anywhere

### Client (Car Pi)
- Raspberry Pi 3B or newer, 1 GB RAM minimum
- Python 3.10+
- One of: Plex, Jellyfin, Emby, Kodi — or none (raw file storage)

---

## Quick start

### 1. Clone

```bash
git clone https://github.com/yourname/carstash.git
cd carstash
pip install -r requirements.txt
```

### 2. Start the server (on your home machine)

```bash
cd server
PI_IP=192.168.1.xxx python app.py
# Web UI → http://localhost:5000
```

### 3. Start the client agent (on the Pi)

```bash
cd client
CARSTASH_MEDIA_SERVER=jellyfin \
MEDIA_SERVER_TOKEN=your_api_key \
python agent.py
```

Or install as a systemd service — see [`docs/INSTALL_CLIENT.md`](docs/INSTALL_CLIENT.md).

### 4. Queue something

Open `http://your-server-ip:5000`, browse to a file, and hit **Add to queue**.
CarStash transcodes it and pushes it to the Pi the next time it's reachable.

---

## Project structure

```
carstash/
├── server/
│   ├── app.py              # Flask server — web UI + REST API
│   └── sync/
│       ├── queue.py        # Persistent sync queue + state machine
│       ├── dispatcher.py   # Heartbeat poller + resumable file push
│       ├── worker.py       # Background transcode worker
│       └── transcode.py    # ffmpeg wrapper (Pi/car-screen optimised)
├── client/
│   ├── agent.py            # Pi client agent (Flask)
│   └── media_servers.py    # Adapters: Plex, Jellyfin, Emby, Kodi
├── docs/
│   ├── INSTALL_SERVER.md
│   ├── INSTALL_CLIENT.md
│   ├── MEDIA_SERVERS.md
│   ├── ARCHITECTURE.md
│   └── CONTRIBUTING.md
├── requirements.txt
├── .env.example
├── .gitignore
├── LICENSE
└── README.md
```

---

## Configuration

See [`.env.example`](.env.example) for all options.
Full documentation in [`docs/`](docs/).

---

## Transcode settings

CarStash transcodes on the **server**, not the Pi.
Default output: `H.264 / AAC / MP4 / 1280×720 / 30fps` — plays natively in Plex, Jellyfin, Emby, Kodi, and Chromium without re-transcoding on playback.

Quality presets: `small` | `balanced` (default) | `quality`

Already-compatible files are detected and skipped — no unnecessary re-encoding.

---

## License

MIT — see [`LICENSE`](LICENSE).
