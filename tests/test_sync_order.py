"""Rewrite-on-reorder playlist write (RC1 §S5)."""

import pytest

from app.ingestion.ytm_adapter import YouTubeMusicAdapter


class FakeClient:
    def __init__(self, existing):
        self.existing = list(existing)
        self.added = []
        self.removed = []

    def get_playlist(self, playlist_id, limit=None):
        return {"tracks": [{"videoId": v, "setVideoId": v} for v in self.existing]}

    def add_playlist_items(self, playlist_id, batch):
        self.added.extend(batch)

    def remove_playlist_items(self, playlist_id, tracks):
        self.removed.extend(t["videoId"] for t in tracks)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import app.ingestion.ytm_adapter as mod
    monkeypatch.setattr(mod, "_WRITE_DELAY_S", 0)


def _adapter(existing):
    a = YouTubeMusicAdapter()
    a._client = FakeClient(existing)
    return a


def test_identical_order_is_noop():
    a = _adapter(["x", "y", "z"])
    a.write_playlist("pl", ["x", "y", "z"])
    assert a._client.added == []
    assert a._client.removed == []


def test_reorder_triggers_clear_and_readd():
    a = _adapter(["x", "y", "z"])
    a.write_playlist("pl", ["z", "y", "x"])
    assert set(a._client.removed) == {"x", "y", "z"}
    assert a._client.added == ["z", "y", "x"]  # re-added in ranked order


def test_membership_change_replaces():
    a = _adapter(["x", "y"])
    a.write_playlist("pl", ["x", "y", "w"])
    assert set(a._client.removed) == {"x", "y"}
    assert a._client.added == ["x", "y", "w"]


def test_empty_existing_just_adds():
    a = _adapter([])
    a.write_playlist("pl", ["a", "b"])
    assert a._client.removed == []
    assert a._client.added == ["a", "b"]
