"""
tests/test_dispatcher.py
Unit tests for server/sync/dispatcher.py

Tests cover:
  - Pi reachability check
  - Resumable file push (Content-Range header construction)
  - Retry / backoff on connection drop
"""
import pytest
import requests
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# Heartbeat / reachability
# ---------------------------------------------------------------------------

class TestHeartbeat:
    def test_pi_reachable_returns_true_on_200(self):
        from server.sync.dispatcher import is_pi_reachable
        with patch("server.sync.dispatcher.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            assert is_pi_reachable("192.168.1.99") is True

    def test_pi_unreachable_returns_false_on_timeout(self):
        from server.sync.dispatcher import is_pi_reachable
        with patch("server.sync.dispatcher.requests.get") as mock_get:
            mock_get.side_effect = requests.exceptions.ConnectTimeout()
            assert is_pi_reachable("192.168.1.99") is False

    def test_pi_unreachable_returns_false_on_connection_error(self):
        from server.sync.dispatcher import is_pi_reachable
        with patch("server.sync.dispatcher.requests.get") as mock_get:
            mock_get.side_effect = requests.exceptions.ConnectionError()
            assert is_pi_reachable("192.168.1.99") is False


# ---------------------------------------------------------------------------
# Resumable push
# ---------------------------------------------------------------------------

class TestResumablePush:
    def test_content_range_header_set_on_resume(self, tmp_path):
        """When the Pi already has N bytes, the push should send Content-Range: bytes N-."""
        from server.sync.dispatcher import push_file

        # Create a dummy source file
        src = tmp_path / "movie.mp4"
        src.write_bytes(b"A" * 1024)

        with patch("server.sync.dispatcher.requests.put") as mock_put, \
             patch("server.sync.dispatcher.get_pi_offset", return_value=512):
            mock_put.return_value = MagicMock(status_code=200)
            push_file("192.168.1.99", str(src), "movie.mp4")

        headers = mock_put.call_args[1].get("headers", {})
        assert "Content-Range" in headers
        assert headers["Content-Range"].startswith("bytes 512-")

    def test_full_send_when_pi_has_zero_bytes(self, tmp_path):
        from server.sync.dispatcher import push_file

        src = tmp_path / "movie.mp4"
        src.write_bytes(b"B" * 512)

        with patch("server.sync.dispatcher.requests.put") as mock_put, \
             patch("server.sync.dispatcher.get_pi_offset", return_value=0):
            mock_put.return_value = MagicMock(status_code=200)
            push_file("192.168.1.99", str(src), "movie.mp4")

        headers = mock_put.call_args[1].get("headers", {})
        assert "Content-Range" in headers
        assert headers["Content-Range"].startswith("bytes 0-")

    def test_push_returns_false_on_connection_drop(self, tmp_path):
        from server.sync.dispatcher import push_file

        src = tmp_path / "movie.mp4"
        src.write_bytes(b"C" * 256)

        with patch("server.sync.dispatcher.requests.put") as mock_put, \
             patch("server.sync.dispatcher.get_pi_offset", return_value=0):
            mock_put.side_effect = requests.exceptions.ConnectionError("car drove away")
            result = push_file("192.168.1.99", str(src), "movie.mp4")

        assert result is False
