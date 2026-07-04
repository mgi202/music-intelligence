"""Prefer-videos toggle: adapter counterpart lookup + lazy-resolve endpoint.

The adapter's get_official_video_counterpart wraps ytmusicapi's
get_watch_playlist `counterpart` parsing (the Song↔Video toggle). The endpoint
caches results on tracks.official_video_id and stamps
official_video_checked_at so each track is looked up at most once.
"""

import sqlite3
from unittest.mock import patch

import pytest

from app.ingestion.ytm_adapter import YouTubeMusicAdapter


class _FakeClient:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def get_watch_playlist(self, videoId, limit):
        self.calls.append(videoId)
        return self._response


def _adapter(response):
    a = YouTubeMusicAdapter.__new__(YouTubeMusicAdapter)   # skip auth in __init__
    a._client = _FakeClient(response)   # .client is a lazy-auth property over _client
    return a


def test_counterpart_returned_for_audio_track():
    a = _adapter({"tracks": [{
        "videoId": "audio123audio",
        "videoType": "MUSIC_VIDEO_TYPE_ATV",
        "counterpart": {"videoId": "video456video"},
    }]})
    assert a.get_official_video_counterpart("audio123audio") == "video456video"


def test_no_counterpart_when_base_is_already_a_video():
    a = _adapter({"tracks": [{
        "videoId": "omv789omv789",
        "videoType": "MUSIC_VIDEO_TYPE_OMV",
        "counterpart": {"videoId": "audio123audio"},
    }]})
    assert a.get_official_video_counterpart("omv789omv789") is None


def test_no_counterpart_key_means_none():
    a = _adapter({"tracks": [{
        "videoId": "audio123audio", "videoType": "MUSIC_VIDEO_TYPE_ATV",
    }]})
    assert a.get_official_video_counterpart("audio123audio") is None


def test_empty_watch_playlist_means_none():
    a = _adapter({"tracks": []})
    assert a.get_official_video_counterpart("audio123audio") is None


@pytest.fixture()
def client(db):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO tracks (track_pk, canonical_title, canonical_artist,"
        " source_platform, ytm_track_id) VALUES"
        " ('t1','A','X','ytm','aud00000001'), ('t2','B','Y','ytm',NULL)"
    )
    conn.commit()
    conn.close()
    from fastapi.testclient import TestClient
    from app.api import server
    return TestClient(server.app)


def test_endpoint_resolves_once_then_caches(client):
    fake = _adapter({"tracks": [{
        "videoId": "aud00000001", "videoType": "MUSIC_VIDEO_TYPE_ATV",
        "counterpart": {"videoId": "vid00000001"},
    }]})
    with patch("app.api.server._make_ytm_adapter", return_value=fake):
        r1 = client.get("/api/tracks/t1/official-video")
        assert r1.status_code == 200
        assert r1.json() == {"official_video_id": "vid00000001", "cached": False}
        r2 = client.get("/api/tracks/t1/official-video")
        assert r2.json() == {"official_video_id": "vid00000001", "cached": True}
    assert fake.client.calls == ["aud00000001"]   # exactly one live lookup


def test_endpoint_stamps_tracks_without_ytm_id(client):
    r = client.get("/api/tracks/t2/official-video")
    assert r.status_code == 200
    assert r.json()["official_video_id"] is None
    r2 = client.get("/api/tracks/t2/official-video")
    assert r2.json()["cached"] is True


def test_endpoint_does_not_stamp_on_ytm_failure(client):
    class _Boom:
        def get_official_video_counterpart(self, vid):
            raise RuntimeError("YTM down")
    with patch("app.api.server._make_ytm_adapter", return_value=_Boom()):
        r = client.get("/api/tracks/t1/official-video")
        assert r.status_code == 502
    ok = _adapter({"tracks": [{
        "videoId": "aud00000001", "videoType": "MUSIC_VIDEO_TYPE_ATV",
        "counterpart": {"videoId": "vid00000001"},
    }]})
    with patch("app.api.server._make_ytm_adapter", return_value=ok):
        r = client.get("/api/tracks/t1/official-video")
        assert r.json() == {"official_video_id": "vid00000001", "cached": False}
