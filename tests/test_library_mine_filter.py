"""'Mine only' Library filter (2026-07-17): tag_source=manual restricts tag
matching + facet counts to hand-applied (private_manual) tags, and the track
list to manually-tagged or rated tracks. Composes with the rating filter and
carries through the Artists/Labels views."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.db.connection import get_connection
from tests.conftest import insert_track


def _client():
    from app.api.server import app
    return TestClient(app)


def _seed(db):
    """Four tracks straddling the manual/public/rated boundaries:

    mine_tagged   — manual 'disco-funk' tag, unrated
    flooded       — 'disco-funk' from Last.fm only (the wrong-tag flood case)
    rated_only    — no tags at all, rated ★★★
    neither       — public 'techno' tag, unrated (not Matthias's curation)
    """
    conn = get_connection(db)
    insert_track(conn, "mine_tagged", canonical_title="Mine")
    insert_track(conn, "flooded", canonical_title="Flooded")
    insert_track(conn, "rated_only", canonical_title="Rated", personal_rating=3)
    insert_track(conn, "neither", canonical_title="Neither")
    conn.executemany(
        "INSERT INTO track_tags (track_pk, tag, tag_type, source) VALUES (?, ?, ?, ?)",
        [("mine_tagged", "disco-funk", "private_manual", "user"),
         ("flooded", "disco-funk", "public", "lastfm"),
         ("neither", "techno", "public", "lastfm")],
    )
    conn.commit(); conn.close()


def _pks(resp):
    return {t["track_pk"] for t in resp.json()["tracks"]}


# ── /api/tracks ──────────────────────────────────────────────────────────────

def test_tag_chip_matches_only_manual_tags_under_mine(db):
    _seed(db)
    cl = _client()
    # Without the toggle, the public tag floods the genre view (the problem).
    assert _pks(cl.get("/api/tracks?tag=disco-funk")) == {"mine_tagged", "flooded"}
    # With it, a chip means "tracks *I* put in this style".
    assert _pks(cl.get("/api/tracks?tag=disco-funk&tag_source=manual")) == {"mine_tagged"}
    # Multi-select OR mode honours the restriction too.
    assert _pks(cl.get("/api/tracks?tags=disco-funk,techno&tag_mode=or&tag_source=manual")) \
        == {"mine_tagged"}


def test_mine_without_chips_is_manual_or_rated(db):
    _seed(db)
    r = _client().get("/api/tracks?tag_source=manual")
    assert _pks(r) == {"mine_tagged", "rated_only"}
    assert r.json()["total"] == 2


def test_mine_composes_with_rating_filter(db):
    _seed(db)
    cl = _client()
    # Rated variant: only the ★★★ track survives both conditions.
    assert _pks(cl.get("/api/tracks?tag_source=manual&rating=3")) == {"rated_only"}
    # Unrated + mine → the manually-tagged track.
    assert _pks(cl.get("/api/tracks?tag_source=manual&rating=0")) == {"mine_tagged"}


# ── /api/tags (facet counts) ─────────────────────────────────────────────────

def test_facet_counts_follow_the_restriction(db):
    _seed(db)
    cl = _client()
    plain = {t["tag"]: t["n"] for t in cl.get("/api/tags").json()}
    assert plain["disco-funk"] == 2 and plain["techno"] == 1
    mine = {t["tag"]: t["n"] for t in cl.get("/api/tags?tag_source=manual").json()}
    # Only the hand-applied application counts; purely-public tags drop out.
    assert mine == {"disco-funk": 1}


# ── Artists / Labels views ───────────────────────────────────────────────────

def _seed_entities(db):
    conn = get_connection(db)
    conn.executemany(
        "INSERT INTO artists (artist_id, name) VALUES (?, ?)",
        [("a1", "Curated Artist"), ("a2", "Untouched Artist")],
    )
    conn.executemany(
        "INSERT INTO track_artists (track_pk, artist_id, role, position) VALUES (?, ?, ?, ?)",
        [("mine_tagged", "a1", "primary", 0), ("rated_only", "a1", "primary", 0),
         ("flooded", "a1", "primary", 0), ("neither", "a2", "primary", 0)],
    )
    conn.executemany(
        "INSERT INTO labels (label_id, name) VALUES (?, ?)",
        [("l1", "Curated Label"), ("l2", "Untouched Label")],
    )
    conn.executemany(
        "INSERT INTO track_labels (track_pk, label_id) VALUES (?, ?)",
        [("mine_tagged", "l1"), ("flooded", "l1"), ("neither", "l2")],
    )
    conn.commit(); conn.close()


def test_artists_view_counts_only_mine_tracks(db):
    _seed(db); _seed_entities(db)
    cl = _client()
    plain = {a["artist_id"]: a for a in cl.get("/api/artists").json()["artists"]}
    assert plain["a1"]["track_count"] == 3 and "a2" in plain
    mine = {a["artist_id"]: a for a in cl.get("/api/artists?tag_source=manual").json()["artists"]}
    # a1 keeps only its manually-tagged + rated tracks; a2 vanishes entirely.
    assert mine["a1"]["track_count"] == 2
    assert mine["a1"]["rated_count"] == 1 and mine["a1"]["loved_count"] == 1
    assert "a2" not in mine


def test_labels_view_counts_only_mine_tracks(db):
    _seed(db); _seed_entities(db)
    cl = _client()
    plain = {l["label_id"]: l for l in cl.get("/api/labels").json()["labels"]}
    assert plain["l1"]["track_count"] == 2 and "l2" in plain
    mine = {l["label_id"]: l for l in cl.get("/api/labels?tag_source=manual").json()["labels"]}
    assert mine["l1"]["track_count"] == 1
    assert "l2" not in mine


# ── Guardrails ───────────────────────────────────────────────────────────────

def test_bad_tag_source_rejected(db):
    _seed(db)
    assert _client().get("/api/tracks?tag_source=public").status_code == 422
