"""Artists & labels as first-class surfaces (2026-07-16).

Covers the ranked entity listings, the follow toggles (Phase 5 hook), the
Library's artist/label track filters, the label payload on /api/tracks, and
the followed-column migration's idempotency.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.db.connection import get_connection
from tests.conftest import insert_track


def _client():
    from app.api.server import app
    return TestClient(app)


def _seed_world(db):
    """Two artists and two labels over four tracks with mixed ratings."""
    conn = get_connection(db)
    insert_track(conn, "t1", canonical_title="One", personal_rating=4)
    insert_track(conn, "t2", canonical_title="Two", personal_rating=3)
    insert_track(conn, "t3", canonical_title="Three", personal_rating=1)
    insert_track(conn, "t4", canonical_title="Four")  # unrated
    conn.executemany(
        "INSERT INTO artists (artist_id, name, musicbrainz_artist_id) VALUES (?, ?, ?)",
        [("mbid:aa", "Deep Artist", "aa"), ("name:bb", "Side Act", None)],
    )
    conn.executemany(
        "INSERT INTO track_artists (track_pk, artist_id, role, position) VALUES (?, ?, ?, ?)",
        [("t1", "mbid:aa", "primary", 0), ("t2", "mbid:aa", "primary", 0),
         ("t3", "name:bb", "primary", 0), ("t4", "name:bb", "primary", 0),
         ("t2", "name:bb", "featured", 1)],
    )
    conn.executemany(
        "INSERT INTO labels (label_id, name) VALUES (?, ?)",
        [("name:l1", "Defected"), ("name:l2", "Obscure Tapes")],
    )
    conn.executemany(
        "INSERT INTO track_labels (track_pk, label_id, catalogue_number) VALUES (?, ?, ?)",
        [("t1", "name:l1", "DFTD001"), ("t2", "name:l1", None),
         ("t4", "name:l2", "OBS-99")],
    )
    conn.commit(); conn.close()


# ── Entity listings ──────────────────────────────────────────────────────────

def test_artists_ranked_by_loved_then_counts(db):
    _seed_world(db)
    artists = _client().get("/api/artists").json()["artists"]
    assert [a["artist_id"] for a in artists] == ["mbid:aa", "name:bb"]
    aa = artists[0]
    assert aa["loved_count"] == 2 and aa["rated_count"] == 2 and aa["track_count"] == 2
    assert aa["has_mbid"] == 1 and artists[1]["has_mbid"] == 0
    # Side Act: t3 (1★) + t4 (unrated) + featured on t2 (3★).
    bb = artists[1]
    assert bb["track_count"] == 3 and bb["rated_count"] == 2 and bb["loved_count"] == 1


def test_labels_ranked_and_searchable(db):
    _seed_world(db)
    cl = _client()
    labels = cl.get("/api/labels").json()["labels"]
    assert [l["name"] for l in labels] == ["Defected", "Obscure Tapes"]
    assert labels[0]["loved_count"] == 2 and labels[0]["track_count"] == 2
    hits = cl.get("/api/labels?q=obscure").json()["labels"]
    assert [l["name"] for l in hits] == ["Obscure Tapes"]


# ── Follow toggles (Phase 5 hook) ────────────────────────────────────────────

def test_follow_toggle_roundtrip_and_404(db):
    _seed_world(db)
    cl = _client()
    r = cl.post("/api/labels/name:l1/follow", json={"followed": True})
    assert r.status_code == 200 and r.json()["followed"] is True
    assert cl.get("/api/labels?followed_only=true").json()["labels"][0]["name"] == "Defected"
    cl.post("/api/labels/name:l1/follow", json={"followed": False})
    assert cl.get("/api/labels?followed_only=true").json()["labels"] == []
    assert cl.post("/api/artists/mbid:aa/follow", json={"followed": True}).status_code == 200
    assert cl.post("/api/artists/nope/follow", json={"followed": True}).status_code == 404


# ── Library filters + payload ────────────────────────────────────────────────

def test_tracks_filter_by_label_and_artist(db):
    _seed_world(db)
    cl = _client()
    by_label = cl.get("/api/tracks?label=name:l1").json()
    assert {t["track_pk"] for t in by_label["tracks"]} == {"t1", "t2"}
    by_artist = cl.get("/api/tracks?artist=name:bb").json()
    assert {t["track_pk"] for t in by_artist["tracks"]} == {"t2", "t3", "t4"}


def test_tracks_payload_carries_labels_with_catalogue_number(db):
    _seed_world(db)
    tracks = {t["track_pk"]: t for t in
              _client().get("/api/tracks").json()["tracks"]}
    assert tracks["t1"]["labels"] == [
        {"label_id": "name:l1", "name": "Defected", "catalogue_number": "DFTD001"}]
    assert tracks["t3"]["labels"] == []


# ── Migration idempotency ────────────────────────────────────────────────────

def test_followed_migration_is_idempotent(db):
    """Fresh DBs get `followed` from schema.sql; a pre-16-Jul DB gains it via
    the guarded ALTER, and re-running init_db is a no-op."""
    from app.db.init_db import init_db
    conn = get_connection(db)
    for table in ("artists", "labels"):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert "followed" in cols
        # Simulate the pre-migration shape, then let init_db re-add it.
        conn.execute(f"ALTER TABLE {table} DROP COLUMN followed")
    conn.commit(); conn.close()
    init_db(db)
    init_db(db)  # second run must not error on the now-present column
    conn = get_connection(db)
    for table in ("artists", "labels"):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert "followed" in cols
    conn.close()
