# Server Installation

The CarStash server runs on your home machine — the one that stays powered on and holds your media library. It handles the web UI, transcoding, and pushing files to the Pi.

## Requirements

- Linux (tested on Ubuntu/Debian; any distro works)
- Python 3.10+
- ffmpeg + ffprobe

```bash
sudo apt update
sudo apt install ffmpeg python3 python3-pip
```

## Install

```bash
git clone https://github.com/yourname/carstash.git
cd carstash
pip install -r requirements.txt
```

## Configure

Copy the example env file and edit it:

```bash
cp .env.example .env
nano .env
```

Key variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `PI_IP` | `192.168.1.100` | IP address of the Pi on your LAN |
| `PI_PORT` | `5001` | Port the Pi agent listens on |
| `PLEXSYNC_CACHE` | `/tmp/carstash_cache` | Where transcoded files are cached |
| `PLEXSYNC_STATE` | `queue_state.json` | Queue state persistence file |

## Run

```bash
cd server
PI_IP=192.168.1.xxx python app.py
```

Web UI is available at `http://localhost:5000`.

## Run as a systemd service (optional)

Create `/etc/systemd/system/carstash-server.service`:

```ini
[Unit]
Description=CarStash Server
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/carstash/server/app.py
WorkingDirectory=/opt/carstash/server
EnvironmentFile=/opt/carstash/.env
Restart=always
RestartSec=10
User=your_username

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now carstash-server
```

## Transcode cache

Transcoded files are stored in `PLEXSYNC_CACHE`. They persist across restarts so the same movie is never re-encoded. You can safely delete files from the cache — they'll be re-created on the next push attempt.

The cache does **not** auto-evict on the server. Monitor disk usage and prune manually if needed:

```bash
# List cached files by size
du -sh /tmp/carstash_cache/* | sort -h
```
