"""Source-playlist write-back: remove a track from the user's own YTM
playlists, remove-from-all (with read-only isolation + block), undo via re-add,
the inbox playlist filter, and setVideoId capture at ingest. Network-free — the
YTM adapter is a stub.
"""

from __future__ import annotations

import pytest

from app.db.connection import db_conn
from tests.conftest import insert_track


class StubYTM:
    """Stand-in YTM adapter. Records remove/add calls; configured ids raise."""

    def __init__(self, readonly=()):
        self.removed = []   # list of (playlist_id, items)
        self.added = []     # list of (playlist_id, video_ids)
        self.readonly = set(readonly)

    def remove_from_playlist(self, playlist_id, items):
        if playlist_id in self.readonly:
            raise RuntimeError("playlist is read-only")
        self.removed.append((playlist_id, items))
        return "STATUS_SUCCEEDED"

    def add_to_playlist(self, playlist_id, video_ids):
        self.added.append((playlist_id, list(video_ids)))
        return {"playlistEditResults": [{"videoId": video_ids[0], "setVideoId": "newset_" + video_ids[0]}]}


def _membership(conn, pk, playlist_id, name, video_id, set_video_id):
    conn.execute(
        "INSERT INTO track_playlist_membership "
        "(track_pk, playlist_id, playlist_name, source, video_id, set_video_id) "
        "VALUES (?, ?, ?, 'ytm', ?, ?)",
        (pk, playlist_id, name, video_id, set_video_id),
    )


# ── unit: source_edit logic ──────────────────────────────────────────────────

def test_remove_from_playlist_calls_ytm_with_both_ids_and_drops_row(db):
    from app.playlists.source_edit import remove_track_from_playlist
    with db_conn(db) as c:
        insert_track(c, "pk_a", ytm_track_id="vid_a")
        _membership(c, "pk_a", "P1", "Favorite Songs", "vid_a", "setA")
    stub = StubYTM()
    r = remove_track_from_playlist("pk_a", "P1", stub, db_path=db)
    assert r["playlist_name"] == "Favorite Songs"
    assert stub.removed == [("P1", [{"videoId": "vid_a", "setVideoId": "setA"}])]
    with db_conn(db) as c:
        assert c.execute(
            "SELECT COUNT(*) FROM track_playlist_membership WHERE track_pk='pk_a'"
        ).fetchone()[0] == 0


def test_remove_unknown_membership_raises(db):
    from app.playlists.source_edit import remove_track_from_playlist
    with db_conn(db) as c:
        insert_track(c, "pk_a", ytm_track_id="vid_a")
    with pytest.raises(ValueError):
        remove_track_from_playlist("pk_a", "P1", StubYTM(), db_path=db)


def test_remove_all_isolates_readonly_and_blocks(db):
    from app.playlists.source_edit import remove_track_from_all_playlists
    with db_conn(db) as c:
        insert_track(c, "pk_a", ytm_track_id="vid_a")
        _membership(c, "pk_a", "P1", "Good", "vid_a", "s1")
        _membership(c, "pk_a", "P2", "ReadOnly", "vid_a", "s2")
    stub = StubYTM(readonly={"P2"})
    r = remove_track_from_all_playlists("pk_a", stub, db_path=db)
    assert [x["playlist_id"] for x in r["removed"]] == ["P1"]
    assert [x["playlist_id"] for x in r["failed"]] == ["P2"]
    assert r["blocked"] is True
    with db_conn(db) as c:
        assert c.execute(
            "SELECT blocked_from_playlists FROM tracks WHERE track_pk='pk_a'"
        ).fetchone()[0] == 1
        ids = sorted(x[0] for x in c.execute(
            "SELECT playlist_id FROM track_playlist_membership WHERE track_pk='pk_a'"))
    assert ids == ["P2"]  # failed removal keeps its row


def test_add_recreates_membership_with_new_setvideoid(db):
    from app.playlists.source_edit import add_track_to_playlist
    with db_conn(db) as c:
        insert_track(c, "pk_a", ytm_track_id="vid_a")
    stub = StubYTM()
    r = add_track_to_playlist("pk_a", "P1", "Favorite Songs", stub, db_path=db)
    assert r["added"] is True
    assert stub.added == [("P1", ["vid_a"])]
    with db_conn(db) as c:
        row = c.execute(
            "SELECT playlist_name, video_id, set_video_id FROM track_playlist_membership "
            "WHERE track_pk='pk_a' AND playlist_id='P1'").fetchone()
    assert row["playlist_name"] == "Favorite Songs"
    assert row["video_id"] == "vid_a"
    assert row["set_video_id"] == "newset_vid_a"


# ── API endpoints ────────────────────────────────────────────────────────────

@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient
    from app.api.server import app
    return TestClient(app)


def test_api_remove_endpoint_drops_chip(client, db, monkeypatch):
    import app.api.server as server
    monkeypatch.setattr(server, "_make_ytm_adapter", lambda: StubYTM())
    with db_conn(db) as c:
        insert_track(c, "pk_a", ytm_track_id="vid_a")
        _membership(c, "pk_a", "P1", "Favorite Songs", "vid_a", "setA")
    r = client.post("/api/tracks/pk_a/playlists/P1/remove")
    assert r.status_code == 200 and r.json()["playlist_name"] == "Favorite Songs"
    assert client.get("/api/tracks").json()["tracks"][0]["playlists"] == []


def test_api_remove_readonly_returns_502(client, db, monkeypatch):
    import app.api.server as server
    monkeypatch.setattr(server, "_make_ytm_adapter", lambda: StubYTM(readonly={"P1"}))
    with db_conn(db) as c:
        insert_track(c, "pk_a", ytm_track_id="vid_a")
        _membership(c, "pk_a", "P1", "RO", "vid_a", "s")
    assert client.post("/api/tracks/pk_a/playlists/P1/remove").status_code == 502


def test_api_remove_all_endpoint(client, db, monkeypatch):
    import app.api.server as server
    monkeypatch.setattr(server, "_make_ytm_adapter", lambda: StubYTM())
    with db_conn(db) as c:
        insert_track(c, "pk_a", ytm_track_id="vid_a")
        _membership(c, "pk_a", "P1", "A", "vid_a", "s1")
        _membership(c, "pk_a", "P2", "B", "vid_a", "s2")
    r = client.post("/api/tracks/pk_a/playlists/remove-all").json()
    assert len(r["removed"]) == 2 and r["blocked"] is True


def test_inbox_source_playlist_filter(client, db):
    with db_conn(db) as c:
        insert_track(c, "pk_a")
        insert_track(c, "pk_b")
        c.execute("INSERT INTO track_playlist_membership (track_pk, playlist_id, playlist_name) "
                  "VALUES ('pk_a', 'P1', 'DWA')")
    r = client.get("/api/inbox?source_playlist=P1").json()
    assert [t["track_pk"] for t in r["tracks"]] == ["pk_a"]


# ── ingest capture ───────────────────────────────────────────────────────────

def test_record_memberships_stores_set_video_id(db):
    from app.ingestion.ledger import record_source_memberships
    with db_conn(db) as c:
        insert_track(c, "pk_a", ytm_track_id="vid_a")
    record_source_memberships(
        {"P1": {"name": "DWA", "video_ids": {"vid_a"}, "items": {"vid_a": "setA"}}},
        run_complete=True,
    )
    with db_conn(db) as c:
        row = c.execute(
            "SELECT video_id, set_video_id FROM track_playlist_membership WHERE track_pk='pk_a'"
        ).fetchone()
    assert row["video_id"] == "vid_a" and row["set_video_id"] == "setA"


# ── removal log + persistent undo ────────────────────────────────────────────

def test_remove_logs_and_list_shows_it(db):
    from app.playlists.source_edit import remove_track_from_playlist, list_recent_removals
    with db_conn(db) as c:
        insert_track(c, "pk_a", canonical_title="Track A", canonical_artist="Artist A", ytm_track_id="vid_a")
        _membership(c, "pk_a", "P1", "Favorite Songs", "vid_a", "setA")
    r = remove_track_from_playlist("pk_a", "P1", StubYTM(), db_path=db)
    assert r["removal_id"] > 0
    log = list_recent_removals(days=14, db_path=db)
    assert len(log) == 1
    assert log[0]["track_title"] == "Track A"
    assert log[0]["playlist_name"] == "Favorite Songs"
    assert log[0]["kind"] == "single"


def test_undo_removal_readds_marks_undone_and_drops_from_list(db):
    from app.playlists.source_edit import (
        remove_track_from_playlist, undo_removal, list_recent_removals)
    with db_conn(db) as c:
        insert_track(c, "pk_a", ytm_track_id="vid_a")
        _membership(c, "pk_a", "P1", "Favorite Songs", "vid_a", "setA")
    stub = StubYTM()
    rid = remove_track_from_playlist("pk_a", "P1", stub, db_path=db)["removal_id"]
    out = undo_removal(rid, stub, db_path=db)
    assert out["undone"] is True
    assert stub.added == [("P1", ["vid_a"])]          # re-added on YTM
    # membership row recreated, log row marked undone → gone from the list
    with db_conn(db) as c:
        assert c.execute(
            "SELECT COUNT(*) FROM track_playlist_membership WHERE track_pk='pk_a' AND playlist_id='P1'"
        ).fetchone()[0] == 1
    assert list_recent_removals(days=14, db_path=db) == []


def test_remove_all_logs_each_playlist_as_kind_all(db):
    from app.playlists.source_edit import remove_track_from_all_playlists, list_recent_removals
    with db_conn(db) as c:
        insert_track(c, "pk_a", ytm_track_id="vid_a")
        _membership(c, "pk_a", "P1", "A", "vid_a", "s1")
        _membership(c, "pk_a", "P2", "B", "vid_a", "s2")
    remove_track_from_all_playlists("pk_a", StubYTM(), db_path=db)
    log = list_recent_removals(days=14, db_path=db)
    assert len(log) == 2 and all(r["kind"] == "all" for r in log)


def test_prune_removal_log_drops_old(db):
    from app.playlists.source_edit import prune_removal_log, list_recent_removals
    import datetime as dt
    with db_conn(db) as c:
        insert_track(c, "pk_a", ytm_track_id="vid_a")
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=90)).isoformat()
        c.execute("INSERT INTO playlist_removal_log "
                  "(track_pk, playlist_id, playlist_name, source, kind, removed_at) "
                  "VALUES ('pk_a','P1','Old','ytm','single',?)", (old,))
    assert prune_removal_log(days=60, db_path=db) == 1
    assert list_recent_removals(days=90, db_path=db) == []


def test_api_removals_list_and_undo(client, db, monkeypatch):
    import app.api.server as server
    stub = StubYTM()
    monkeypatch.setattr(server, "_make_ytm_adapter", lambda: stub)
    with db_conn(db) as c:
        insert_track(c, "pk_a", canonical_title="T", canonical_artist="A", ytm_track_id="vid_a")
        _membership(c, "pk_a", "P1", "Favorite Songs", "vid_a", "setA")
    rid = client.post("/api/tracks/pk_a/playlists/P1/remove").json()["removal_id"]
    listing = client.get("/api/removals").json()
    assert [x["id"] for x in listing] == [rid]
    assert client.post(f"/api/removals/{rid}/undo").json()["undone"] is True
    assert client.get("/api/removals").json() == []   # gone after undo


def test_adapter_captures_set_video_id():
    from app.ingestion.ytm_adapter import YouTubeMusicAdapter

    def song(v, sv):
        return {"videoId": v, "setVideoId": sv, "title": v, "artists": [{"name": "A"}],
                "album": {"name": "Al"}, "duration_seconds": 200}

    class FC:
        def get_library_songs(self, limit=None): return []
        def get_liked_songs(self, limit=None): return {"tracks": []}
        def get_library_playlists(self, limit=None): return [{"playlistId": "P1", "title": "DWA"}]
        def get_playlist(self, pid, limit=10000): return {"tracks": [song("vid_a", "setA")]}

    a = YouTubeMusicAdapter()
    a._client = FC()
    a.fetch_library_snapshot()
    assert a.last_playlist_memberships["P1"]["items"] == {"vid_a": "setA"}
