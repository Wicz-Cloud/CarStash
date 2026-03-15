# Media Server Setup

CarStash supports four media servers on the client (Pi) side. Set `CARSTASH_MEDIA_SERVER` to the one you use. If you don't use a media server, set it to `none` — files will be stored and you can point your player at the directory manually.

---

## Plex

**Default port:** 32400

### Get your API token

1. Sign in to Plex Web at `http://your-pi-ip:32400/web`
2. Browse to any media item
3. Click the `···` menu → **Get Info** → **View XML**
4. Copy the `X-Plex-Token` value from the URL

### Get your library section ID

1. Go to `http://your-pi-ip:32400/library/sections?X-Plex-Token=YOUR_TOKEN`
2. Find your Movies library — note its `key` value (e.g. `1`)

### Environment variables

```env
CARSTASH_MEDIA_SERVER=plex
MEDIA_SERVER_URL=http://localhost:32400
MEDIA_SERVER_TOKEN=your_plex_token
MEDIA_SERVER_SECTION=1
```

---

## Jellyfin

**Default port:** 8096

### Get your API key

1. Open Jellyfin dashboard → **Administration → API Keys**
2. Click **+** to create a new key
3. Give it a name (e.g. `carstash`) and copy the generated key

### Get your library ID (optional)

If you want to refresh only your Movies library instead of all libraries:

1. Go to **Administration → Libraries**
2. Click your Movies library → note the item ID in the URL
3. Set `MEDIA_SERVER_SECTION` to that ID

Leave `MEDIA_SERVER_SECTION` blank to refresh all libraries.

### Environment variables

```env
CARSTASH_MEDIA_SERVER=jellyfin
MEDIA_SERVER_URL=http://localhost:8096
MEDIA_SERVER_TOKEN=your_jellyfin_api_key
MEDIA_SERVER_SECTION=
```

---

## Emby

**Default port:** 8096

Emby and Jellyfin share the same API shape (Jellyfin was forked from Emby). The setup process is identical.

### Get your API key

1. Open Emby dashboard → **Advanced → API Keys**
2. Click **New API Key**
3. Give it a name and copy the key

### Environment variables

```env
CARSTASH_MEDIA_SERVER=emby
MEDIA_SERVER_URL=http://localhost:8096
MEDIA_SERVER_TOKEN=your_emby_api_key
MEDIA_SERVER_SECTION=
```

---

## Kodi

**Default port:** 8080

Kodi is a local media player, not a network server. CarStash triggers `VideoLibrary.Scan` via Kodi's HTTP JSON-RPC API so new files appear in the library automatically after a transfer.

### Enable HTTP control in Kodi

1. **Settings → Services → Control**
2. Enable **Allow remote control via HTTP**
3. Set a port (default: 8080)
4. Optionally set a username and password

### Environment variables

```env
CARSTASH_MEDIA_SERVER=kodi
MEDIA_SERVER_URL=http://localhost:8080
KODI_USER=kodi
MEDIA_SERVER_TOKEN=your_kodi_http_password
```

Leave `MEDIA_SERVER_TOKEN` blank if you haven't set a password.

> **Note:** `MEDIA_SERVER_SECTION` is not used for Kodi — `VideoLibrary.Scan` always scans all sources.

---

## None (no media server)

Files are stored in `CARSTASH_MEDIA_DIR` with no library scan triggered. Point your media player at that directory manually.

```env
CARSTASH_MEDIA_SERVER=none
CARSTASH_MEDIA_DIR=/mnt/carstash/media
```

---

## Troubleshooting

**Refresh not triggering:**
- Check the agent log for `[adapter_name]` lines after a completed transfer
- Verify the media server is running on the Pi: `curl http://localhost:PORT`
- Confirm the token is correct — most servers return HTTP 401 for bad tokens

**Kodi not scanning:**
- Confirm HTTP control is enabled in Kodi settings
- Test manually: `curl -X POST http://localhost:8080/jsonrpc -d '{"jsonrpc":"2.0","method":"VideoLibrary.Scan","id":1}'`

**Files appear in directory but not in the media server UI:**
- The library scan may take 30–60 seconds to complete
- Ensure the media server's library path includes `CARSTASH_MEDIA_DIR`
