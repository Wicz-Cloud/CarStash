"""
tests/test_media_servers.py
Unit tests for client/media_servers.py adapter layer.

Each adapter's trigger_scan() method is tested with a mocked HTTP session
so no real media servers are required.
"""
import pytest
import requests
from unittest.mock import MagicMock, patch

from client.media_servers import (
    PlexAdapter,
    JellyfinAdapter,
    EmbyAdapter,
    KodiAdapter,
    NullAdapter,
    get_adapter,
)


# ---------------------------------------------------------------------------
# NullAdapter (no media server configured)
# ---------------------------------------------------------------------------

class TestNullAdapter:
    def test_trigger_scan_returns_true(self):
        adapter = NullAdapter()
        assert adapter.trigger_scan() is True

    def test_trigger_scan_does_not_raise(self):
        adapter = NullAdapter()
        adapter.trigger_scan()  # should be a no-op


# ---------------------------------------------------------------------------
# PlexAdapter
# ---------------------------------------------------------------------------

class TestPlexAdapter:
    def test_trigger_scan_calls_correct_endpoint(self):
        adapter = PlexAdapter(host="192.168.1.10", port=32400, token="tok123")
        with patch("client.media_servers.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            result = adapter.trigger_scan()

        call_url = mock_get.call_args[0][0]
        assert "192.168.1.10" in call_url
        assert "32400" in call_url
        assert "refresh" in call_url.lower()
        assert result is True

    def test_trigger_scan_returns_false_on_http_error(self):
        adapter = PlexAdapter(host="192.168.1.10", port=32400, token="tok123")
        with patch("client.media_servers.requests.get") as mock_get:
            mock_get.side_effect = requests.RequestException("timeout")
            result = adapter.trigger_scan()
        assert result is False


# ---------------------------------------------------------------------------
# JellyfinAdapter
# ---------------------------------------------------------------------------

class TestJellyfinAdapter:
    def test_trigger_scan_uses_post(self):
        adapter = JellyfinAdapter(host="192.168.1.10", port=8096, token="jftok")
        with patch("client.media_servers.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=204)
            result = adapter.trigger_scan()

        assert mock_post.called
        assert result is True

    def test_trigger_scan_returns_false_on_error(self):
        adapter = JellyfinAdapter(host="192.168.1.10", port=8096, token="jftok")
        with patch("client.media_servers.requests.post") as mock_post:
            mock_post.side_effect = requests.RequestException("refused")
            result = adapter.trigger_scan()
        assert result is False


# ---------------------------------------------------------------------------
# KodiAdapter
# ---------------------------------------------------------------------------

class TestKodiAdapter:
    def test_trigger_scan_sends_json_rpc(self):
        adapter = KodiAdapter(host="192.168.1.10", port=8080)
        with patch("client.media_servers.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            adapter.trigger_scan()

        payload = mock_post.call_args[1]["json"]
        assert payload.get("method") == "VideoLibrary.Scan"

    def test_trigger_scan_returns_false_on_error(self):
        adapter = KodiAdapter(host="192.168.1.10", port=8080)
        with patch("client.media_servers.requests.post") as mock_post:
            mock_post.side_effect = requests.RequestException("refused")
            result = adapter.trigger_scan()
        assert result is False


# ---------------------------------------------------------------------------
# Factory: get_adapter()
# ---------------------------------------------------------------------------

class TestGetAdapter:
    @pytest.mark.parametrize("name,expected_cls", [
        ("plex",     PlexAdapter),
        ("jellyfin", JellyfinAdapter),
        ("emby",     EmbyAdapter),
        ("kodi",     KodiAdapter),
        ("none",     NullAdapter),
        ("",         NullAdapter),
    ])
    def test_factory_returns_correct_type(self, name, expected_cls):
        adapter = get_adapter(name)
        assert isinstance(adapter, expected_cls)

    def test_factory_unknown_name_returns_null(self):
        adapter = get_adapter("supersecretmediaserver")
        assert isinstance(adapter, NullAdapter)
