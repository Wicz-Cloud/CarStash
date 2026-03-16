"""
CarStash — Media Server Adapters

Abstracts library refresh triggers across the four supported media servers.
The client agent calls `get_adapter()` and then `adapter.refresh_library()`
after a successful file receive — it doesn't need to know which server is running.

Supported servers:
  plex     — Plex Media Server  (port 32400)
  jellyfin — Jellyfin           (port 8096)
  emby     — Emby Server        (port 8096)
  kodi     — Kodi / XBMC        (port 8080, JSON-RPC)
  none     — No media server (CarStash stores files, you point your player manually)

Configuration via environment variables:
  CARSTASH_MEDIA_SERVER   = plex | jellyfin | emby | kodi | none
  MEDIA_SERVER_URL        = http://localhost:PORT   (overrides default port)
  MEDIA_SERVER_TOKEN      = API token / password
  MEDIA_SERVER_SECTION    = library section ID (Plex/Jellyfin/Emby)
  KODI_USER               = Kodi HTTP username (default: kodi)

All adapters are best-effort — a failed refresh never fails the file receive.
"""

import logging
import os
from abc import ABC, abstractmethod
from typing import Optional

import requests

logger = logging.getLogger(__name__)

TIMEOUT = 8  # seconds for all media server API calls


# ── Base adapter ──────────────────────────────────────────────────────────────


class MediaServerAdapter(ABC):
    name: str = "base"

    @abstractmethod
    def refresh_library(self) -> bool:
        """Trigger a library scan. Returns True on success."""
        ...

    def _safe_get(self, url: str, **kwargs) -> Optional[requests.Response]:
        try:
            return requests.get(url, timeout=TIMEOUT, **kwargs)
        except Exception as e:
            logger.warning(f"[{self.name}] GET {url} failed: {e}")
            return None

    def _safe_post(self, url: str, **kwargs) -> Optional[requests.Response]:
        try:
            return requests.post(url, timeout=TIMEOUT, **kwargs)
        except Exception as e:
            logger.warning(f"[{self.name}] POST {url} failed: {e}")
            return None


# ── Plex ──────────────────────────────────────────────────────────────────────


class PlexAdapter(MediaServerAdapter):
    """
    Plex Media Server
    Default port: 32400
    Auth: X-Plex-Token header
    Refresh endpoint: GET /library/sections/{section}/refresh
    """

    name = "plex"

    def __init__(self, host: str, port: int = 32400, token: str = "", section: str = "1"):
        self.host = host
        self.port = port
        self.token = token
        self.section = section
        self.url = f"http://{host}:{port}"

    def refresh_library(self) -> bool:
        if not self.token:
            logger.debug("[plex] No token set — skipping refresh")
            return False
        endpoint = f"{self.url}/library/sections/{self.section}/refresh"
        resp = self._safe_get(endpoint, headers={"X-Plex-Token": self.token})
        if resp and resp.status_code in (200, 204):
            logger.info("[plex] Library refresh triggered ✓")
            return True
        logger.warning(f"[plex] Refresh returned {resp.status_code if resp else 'no response'}")
        return False

    def trigger_scan(self) -> bool:
        return self.refresh_library()


# ── Jellyfin ──────────────────────────────────────────────────────────────────


class JellyfinAdapter(MediaServerAdapter):
    """
    Jellyfin Media Server
    Default port: 8096
    Auth: ?api_key=TOKEN query param (or X-Emby-Token header)
    Refresh endpoint: POST /Library/Refresh
    """

    name = "jellyfin"

    def __init__(self, host: str, port: int = 8096, token: str = "", section: str = ""):
        self.host = host
        self.port = port
        self.token = token
        self.section = section
        self.url = f"http://{host}:{port}"

    def refresh_library(self) -> bool:
        if not self.token:
            logger.debug("[jellyfin] No token set — skipping refresh")
            return False

        headers = {"X-Emby-Token": self.token, "Content-Type": "application/json"}

        if self.section:
            # Refresh a specific library folder by ID
            endpoint = f"{self.url}/Items/{self.section}/Refresh"
            resp = self._safe_post(
                endpoint, headers=headers, params={"Recursive": "true", "ImageRefreshMode": "Default"}
            )
        else:
            # Refresh all libraries
            endpoint = f"{self.url}/Library/Refresh"
            resp = self._safe_post(endpoint, headers=headers)

        if resp and resp.status_code in (200, 204):
            logger.info("[jellyfin] Library refresh triggered ✓")
            return True
        logger.warning(f"[jellyfin] Refresh returned {resp.status_code if resp else 'no response'}")
        return False

    def trigger_scan(self) -> bool:
        return self.refresh_library()


# ── Emby ──────────────────────────────────────────────────────────────────────


class EmbyAdapter(MediaServerAdapter):
    """
    Emby Media Server
    Default port: 8096
    Auth: api_key query param (same API shape as Jellyfin — they share heritage)
    Refresh endpoint: POST /Library/Refresh
    """

    name = "emby"

    def __init__(self, host: str, port: int = 8096, token: str = "", section: str = ""):
        self.host = host
        self.port = port
        self.token = token
        self.section = section
        self.url = f"http://{host}:{port}"

    def refresh_library(self) -> bool:
        if not self.token:
            logger.debug("[emby] No token set — skipping refresh")
            return False

        params = {"api_key": self.token}
        headers = {"Content-Type": "application/json"}

        if self.section:
            endpoint = f"{self.url}/Items/{self.section}/Refresh"
            resp = self._safe_post(endpoint, headers=headers, params={**params, "Recursive": "true"})
        else:
            endpoint = f"{self.url}/Library/Refresh"
            resp = self._safe_post(endpoint, headers=headers, params=params)

        if resp and resp.status_code in (200, 204):
            logger.info("[emby] Library refresh triggered ✓")
            return True
        logger.warning(f"[emby] Refresh returned {resp.status_code if resp else 'no response'}")
        return False

    def trigger_scan(self) -> bool:
        return self.refresh_library()


# ── Kodi ─────────────────────────────────────────────────────────────────────


class KodiAdapter(MediaServerAdapter):
    """
    Kodi (XBMC) via HTTP JSON-RPC API
    Default port: 8080
    Auth: HTTP Basic auth (username/password)
    Kodi is a local player, not a network server — it serves the machine it runs on.
    We trigger VideoLibrary.Scan so newly arrived files appear in Kodi's library.

    Kodi HTTP must be enabled:
      Settings → Services → Control → Allow remote control via HTTP
    """

    name = "kodi"

    def __init__(self, host: str, port: int = 8080, username: str = "kodi", password: str = ""):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.url = f"http://{host}:{port}"

    def refresh_library(self) -> bool:
        endpoint = f"{self.url}/jsonrpc"
        payload = {
            "jsonrpc": "2.0",
            "method": "VideoLibrary.Scan",
            "id": 1,
        }
        auth = (self.username, self.password) if self.password else None
        try:
            resp = requests.post(
                endpoint,
                json=payload,
                auth=auth,
                headers={"Content-Type": "application/json"},
                timeout=TIMEOUT,
            )
            if resp.status_code == 200:
                result = resp.json()
                if "error" not in result:
                    logger.info("[kodi] VideoLibrary.Scan triggered ✓")
                    return True
                logger.warning(f"[kodi] JSON-RPC error: {result.get('error')}")
        except Exception as e:
            logger.warning(f"[kodi] Refresh failed: {e}")
        return False

    def trigger_scan(self) -> bool:
        return self.refresh_library()


# ── Null adapter (no media server) ───────────────────────────────────────────


class NullAdapter(MediaServerAdapter):
    """
    No media server configured. Files are stored; user points their player manually.
    Refresh is a no-op.
    """

    name = "none"

    def refresh_library(self) -> bool:
        logger.debug("[none] No media server configured — skipping library refresh")
        return True

    def trigger_scan(self) -> bool:
        return self.refresh_library()


# ── Factory ───────────────────────────────────────────────────────────────────

DEFAULT_PORTS = {
    "plex": 32400,
    "jellyfin": 8096,
    "emby": 8096,
    "kodi": 8080,
}


def get_adapter(name: str = None) -> MediaServerAdapter:
    """
    Build the correct adapter from environment variables or a provided name.

    Args:
        name (str, optional): The media server type (e.g., 'plex', 'jellyfin').
            If not provided, uses the CARSTASH_MEDIA_SERVER environment variable.

    Returns:
        MediaServerAdapter: The appropriate adapter instance.
    """
    server_type = (name if name is not None else os.environ.get("CARSTASH_MEDIA_SERVER", "none")).lower().strip()
    token = os.environ.get("MEDIA_SERVER_TOKEN", "")
    section = os.environ.get("MEDIA_SERVER_SECTION", "")
    custom_host = os.environ.get("MEDIA_SERVER_HOST", "localhost")
    custom_port = os.environ.get("MEDIA_SERVER_PORT", "")
    kodi_user = os.environ.get("KODI_USER", "kodi")

    if server_type not in ("plex", "jellyfin", "emby", "kodi", "none"):
        logger.warning(f"Unknown CARSTASH_MEDIA_SERVER '{server_type}' — defaulting to none")
        server_type = "none"

    if server_type == "none":
        return NullAdapter()

    default_port = DEFAULT_PORTS[server_type]
    port = int(custom_port) if custom_port else default_port
    host = custom_host

    if server_type == "plex":
        return PlexAdapter(host=host, port=port, token=token, section=section or "1")

    if server_type == "jellyfin":
        return JellyfinAdapter(host=host, port=port, token=token, section=section)

    if server_type == "emby":
        return EmbyAdapter(host=host, port=port, token=token, section=section)

    if server_type == "kodi":
        return KodiAdapter(host=host, port=port, username=kodi_user, password=token)

    return NullAdapter()


SUPPORTED_SERVERS = {
    "plex": {
        "name": "Plex Media Server",
        "default_port": 32400,
        "token_hint": "Settings → Plex Web → Account → Authorized Devices → token in URL",
        "section_hint": "Library section numeric ID (visible in Plex web URL)",
    },
    "jellyfin": {
        "name": "Jellyfin",
        "default_port": 8096,
        "token_hint": "Dashboard → API Keys → + (create new key)",
        "section_hint": "Optional: specific library item ID from Jellyfin API",
    },
    "emby": {
        "name": "Emby",
        "default_port": 8096,
        "token_hint": "Dashboard → API Keys → + (create new key)",
        "section_hint": "Optional: specific library item ID from Emby API",
    },
    "kodi": {
        "name": "Kodi",
        "default_port": 8080,
        "token_hint": "Settings → Services → Control → HTTP password",
        "section_hint": "Not used for Kodi (full VideoLibrary.Scan is always triggered)",
    },
    "none": {
        "name": "None (manual)",
        "default_port": None,
        "token_hint": "Not required",
        "section_hint": "Not required",
    },
}
