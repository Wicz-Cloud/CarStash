"""
tests/test_queue.py
Unit tests for server/sync/queue.py

Run with:
    pytest tests/ -v
"""
import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_queue(tmp_path):
    """Return a fresh SyncQueue instance pointing at a temp directory."""
    # Adjust the import path once server/sync/queue.py exists
    with patch("server.sync.queue.QUEUE_FILE", str(tmp_path / "queue.json")):
        from server.sync.queue import SyncQueue
        q = SyncQueue()
        yield q


# ---------------------------------------------------------------------------
# Basic queue operations
# ---------------------------------------------------------------------------

class TestSyncQueue:
    def test_queue_starts_empty(self, mock_queue):
        assert mock_queue.list_items() == []

    def test_add_item_appears_in_list(self, mock_queue):
        mock_queue.add("movie.mp4", "/media/movie.mp4")
        items = mock_queue.list_items()
        assert len(items) == 1
        assert items[0]["filename"] == "movie.mp4"

    def test_added_item_has_pending_status(self, mock_queue):
        mock_queue.add("movie.mp4", "/media/movie.mp4")
        item = mock_queue.list_items()[0]
        assert item["status"] == "pending"

    def test_remove_item(self, mock_queue):
        mock_queue.add("movie.mp4", "/media/movie.mp4")
        item_id = mock_queue.list_items()[0]["id"]
        mock_queue.remove(item_id)
        assert mock_queue.list_items() == []

    def test_remove_nonexistent_item_is_harmless(self, mock_queue):
        """Removing an ID that doesn't exist should not raise."""
        mock_queue.remove("nonexistent-id-123")

    def test_status_transition_pending_to_transferring(self, mock_queue):
        mock_queue.add("ep1.mp4", "/media/ep1.mp4")
        item_id = mock_queue.list_items()[0]["id"]
        mock_queue.set_status(item_id, "transferring")
        assert mock_queue.list_items()[0]["status"] == "transferring"

    def test_status_transition_to_done(self, mock_queue):
        mock_queue.add("ep1.mp4", "/media/ep1.mp4")
        item_id = mock_queue.list_items()[0]["id"]
        mock_queue.set_status(item_id, "done")
        assert mock_queue.list_items()[0]["status"] == "done"

    def test_queue_persistence(self, tmp_path):
        """Items written by one instance should be readable by a fresh instance."""
        queue_file = str(tmp_path / "queue.json")
        with patch("server.sync.queue.QUEUE_FILE", queue_file):
            from server.sync.queue import SyncQueue
            q1 = SyncQueue()
            q1.add("persist.mp4", "/media/persist.mp4")

            q2 = SyncQueue()
            assert len(q2.list_items()) == 1
            assert q2.list_items()[0]["filename"] == "persist.mp4"
