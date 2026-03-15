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