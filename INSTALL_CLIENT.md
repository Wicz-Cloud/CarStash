# Client Installation (Raspberry Pi)

The CarStash client agent runs on your car Pi. It's a small Flask app that does two things: answers the server's liveness pings, and receives file pushes. It never initiates outbound connections.

## Requirements

- Raspberry Pi 3B or newer, 1 GB RAM minimum
- Raspberry Pi OS Lite (Bookworm 64-bit) recommended
- Python 3.10+

```bash
sudo apt update
sudo apt install python3 python3-pip
```

## Install

```bash
git clone https://github.com/yourname/carstash.git
cd carstash
pip install -r requirements.txt
```

## Configure

Set environment variables before running. The most important one is `CARSTASH_MEDIA_SERVER`.

| Variable | Default | Description |
|----------|---------|-------------|
| `CARSTASH_MEDIA_SERVER` | `none` | `plex` / `jellyfin` / `emby` / `kodi` / `none` |
| `CARSTASH_MEDIA_DIR` | `/mnt/carstash/media` | Where received files are stored |
| `CARSTASH_MIN_FREE_GB` | `2` | Minimum GB to keep free (LRU eviction threshold) |
| `MEDIA_SERVER_URL` | auto | Override media server URL (e.g. `http://localhost:8096`) |
| `MEDIA_SERVER_TOKEN` | — | API token or password |
| `MEDIA_SERVER_SECTION` | — | Library section ID (Plex/Jellyfin/Emby) |
| `KODI_USER` | `kodi` | Kodi HTTP username |

See [`docs/MEDIA_SERVERS.md`](MEDIA_SERVERS.md) for per-server token setup.

## Run manually

```bash
cd client
CARSTASH_MEDIA_SERVER=jellyfin \
MEDIA_SERVER_TOKEN=your_api_key \
python agent.py
```

## Install as a systemd service (recommended)

The agent should start automatically on boot so it's ready before the car connects to your home WiFi.

```bash
sudo cp /path/to/carstash/client/carstash-agent.service /etc/systemd/system/
sudo nano /etc/systemd/system/carstash-agent.service   # set your env vars
sudo systemctl daemon-reload
sudo systemctl enable --now carstash-agent
```

The service file at `client/carstash-agent.service`:

```ini
[Unit]
Description=CarStash Client Agent
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/carstash/client/agent.py
WorkingDirectory=/home/pi/carstash/client
Restart=always
RestartSec=10
User=pi
Environment=CARSTASH_MEDIA_SERVER=jellyfin
Environment=CARSTASH_MEDIA_DIR=/mnt/carstash/media
Environment=CARSTASH_MIN_FREE_GB=2
Environment=MEDIA_SERVER_URL=http://localhost:8096
Environment=MEDIA_SERVER_TOKEN=your_token_here
Environment=MEDIA_SERVER_SECTION=

[Install]
WantedBy=multi-user.target
```

## Verify it's working

From the server machine:

```bash
curl http://<pi-ip>:5001/api/status
```

Expected response:

```json
{
  "ok": true,
  "media_server": "jellyfin",
  "free_bytes": 28000000000,
  "file_count": 3,
  "media_dir": "/mnt/carstash/media"
}
```

## Storage location

Point `CARSTASH_MEDIA_DIR` at whatever drive your media server watches. Example for Jellyfin:

```
CARSTASH_MEDIA_DIR=/mnt/carstash/media
# In Jellyfin: add /mnt/carstash/media as a Movies library folder
```

CarStash uses LRU eviction to stay within `CARSTASH_MIN_FREE_GB` — oldest files are removed automatically when space runs low.
