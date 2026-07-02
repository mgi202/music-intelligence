"""Tests for the per-track playback_video_id (own-front-end player)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import insert_track


def _client(db):
    from app.db.connection import get_connection
    conn = get_connection(db)
    insert_track(conn, "t1", canonical_artist="Marten Lou",
                 canonical_title="Your Body", ytm_track_id="-AfhakVBd2k")
    conn.commit(); conn.close()
    from app.api.server import app
    return TestClient(app)


def test_set_from_watch_url(db):
    cl = _client(db)
    r = cl.put("/api/tracks/t1/playback-version",
               json={"video": "https://music.youtube.com/watch?v=0rT9m-nMKJE"})
    assert r.status_code == 200
    assert r.json()["playback_video_id"] == "0rT9m-nMKJE"
    # Surfaced in the track payload for the player to prefer.
    t = [x for x in cl.get("/api/tracks").json()["tracks"] if x["track_pk"] == "t1"][0]
    assert t["playback_video_id"] == "0rT9m-nMKJE"
    assert t["ytm_track_id"] == "-AfhakVBd2k"  # original untouched


def test_set_from_raw_id_and_youtu_be(db):
    cl = _client(db)
    assert cl.put("/api/tracks/t1/playback-version",
                  json={"video": "0rT9m-nMKJE"}).json()["playback_video_id"] == "0rT9m-nMKJE"
    assert cl.put("/api/tracks/t1/playback-version",
                  json={"video": "https://youtu.be/0rT9m-nMKJE"}).json()["playback_video_id"] == "0rT9m-nMKJE"


def test_clear(db):
    cl = _client(db)
    cl.put("/api/tracks/t1/playback-version", json={"video": "0rT9m-nMKJE"})
    assert cl.put("/api/tracks/t1/playback-version", json={"video": ""}).json()["playback_video_id"] is None
    assert cl.put("/api/tracks/t1/playback-version", json={"video": None}).json()["playback_video_id"] is None


def test_unparseable_is_400(db):
    cl = _client(db)
    assert cl.put("/api/tracks/t1/playback-version", json={"video": "not a link"}).status_code == 400


def test_unknown_track_is_404(db):
    cl = _client(db)
    assert cl.put("/api/tracks/nope/playback-version",
                  json={"video": "0rT9m-nMKJE"}).status_code == 404
