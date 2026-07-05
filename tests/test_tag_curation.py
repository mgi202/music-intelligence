"""Tag curation + source-playlist membership (FE hardening, 2026-06-26).

Covers the effective_track_tags view (dedup / global hide / alias / per-track
reject), that reject survives re-enrichment, that playlist eligibility honours
all of it, the multi-tag filter API, the vocabulary + reject endpoints, and
source-playlist membership capture (adapter + ledger, delete-replace).

Network-free.
"""

from __future__ import annotations

import json

import pytest

from app.db.connection import db_conn
from tests.conftest import insert_track


# ── helpers ──────────────────────────────────────────────────────────────────

def _tag(conn, pk, tag, source, tag_type="public"):
    conn.execute(
        "INSERT INTO track_tags (track_pk, tag, tag_type, source, confidence) "
        "VALUES (?, ?, ?, ?, 1.0)",
        (pk, tag, tag_type, source),
    )


def _eff(db_path, pk):
    with db_conn(db_path) as c:
        return sorted(r["tag"] for r in c.execute(
            "SELECT tag FROM effective_track_tags WHERE track_pk = ?", (pk,)))


# ── effective view ───────────────────────────────────────────────────────────

# NOTE (2026-07-04): the locked vocab seed (app/tags/vocab_lock.py) now ships
# rulings for real tags (electronic hidden, disco/synthpop aliased, …) on every
# init. These tests exercise the curation MECHANISM, so they use tags outside
# the locked seed to stay independent of ruling changes.

def test_duplicate_tag_from_two_sources_collapses(db):
    with db_conn(db) as c:
        insert_track(c, "t1")
        _tag(c, "t1", "dub techno", "lastfm")
        _tag(c, "t1", "dub techno", "musicbrainz")
    assert _eff(db, "t1") == ["dub techno"]  # was "dub techno dub techno"


def test_global_hide_removes_tag_everywhere(db):
    with db_conn(db) as c:
        insert_track(c, "t1")
        _tag(c, "t1", "chart noise", "lastfm")
        _tag(c, "t1", "house", "lastfm")
        c.execute("INSERT INTO tag_vocabulary (tag, hidden) VALUES ('chart noise', 1)")
    assert _eff(db, "t1") == ["house"]


def test_alias_folds_spelling_variants(db):
    with db_conn(db) as c:
        insert_track(c, "t1")
        _tag(c, "t1", "dubtechno", "lastfm")
        _tag(c, "t1", "dub techno", "musicbrainz")
        c.execute("INSERT INTO tag_vocabulary (tag, alias_to) VALUES ('dubtechno', 'dub techno')")
    assert _eff(db, "t1") == ["dub techno"]


def test_per_track_reject_then_survives_reenrichment(db):
    with db_conn(db) as c:
        insert_track(c, "t1")
        _tag(c, "t1", "dubby", "lastfm")
        _tag(c, "t1", "house", "lastfm")
    assert _eff(db, "t1") == ["dubby", "house"]

    with db_conn(db) as c:
        c.execute("INSERT INTO track_tag_overrides (track_pk, tag, action) "
                  "VALUES ('t1', 'dubby', 'reject')")
    assert _eff(db, "t1") == ["house"]

    # Re-enrichment re-adds 'dubby' from a different source — must stay rejected.
    with db_conn(db) as c:
        _tag(c, "t1", "dubby", "discogs")
    assert _eff(db, "t1") == ["house"]


def test_manual_tag_outranks_public_in_type(db):
    with db_conn(db) as c:
        insert_track(c, "t1")
        _tag(c, "t1", "techno", "lastfm", "public")
        _tag(c, "t1", "techno", "manual", "private_manual")
    with db_conn(db) as c:
        rows = list(c.execute(
            "SELECT tag, tag_type FROM effective_track_tags WHERE track_pk='t1'"))
    assert len(rows) == 1
    assert rows[0]["tag_type"] == "private_manual"


# ── eligibility honours curation ─────────────────────────────────────────────

def _make_rule(conn, rule_id="r1", tags_any=None):
    conn.execute(
        "INSERT INTO playlist_rules (rule_id, playlist_name, target_platform, "
        "rule_json, ranking_mode, enabled) VALUES (?, ?, 'ytm', ?, 'mood', 1)",
        (rule_id, "Test PL", json.dumps({"eligibility": {"tags_any": tags_any or []}})),
    )


def test_eligibility_excludes_rejected_and_hidden(db):
    from app.playlists.compiler import compile_playlist
    with db_conn(db) as c:
        insert_track(c, "t1")
        _tag(c, "t1", "dubby", "lastfm")
        _make_rule(c, tags_any=["dubby"])
    assert compile_playlist("r1", db_path=db) == ["t1"]  # eligible

    with db_conn(db) as c:
        c.execute("INSERT INTO track_tag_overrides (track_pk, tag, action) "
                  "VALUES ('t1', 'dubby', 'reject')")
    assert compile_playlist("r1", db_path=db) == []  # reject removes eligibility

    with db_conn(db) as c:
        c.execute("DELETE FROM track_tag_overrides")
        c.execute("INSERT INTO tag_vocabulary (tag, hidden) VALUES ('dubby', 1)")
    assert compile_playlist("r1", db_path=db) == []  # global hide removes too


def test_eligibility_matches_aliased_tag(db):
    from app.playlists.compiler import compile_playlist
    with db_conn(db) as c:
        insert_track(c, "t1")
        _tag(c, "t1", "dubtechno", "lastfm")
        c.execute("INSERT INTO tag_vocabulary (tag, alias_to) VALUES ('dubtechno','dub techno')")
        _make_rule(c, tags_any=["dub techno"])  # rule uses canonical name
    assert compile_playlist("r1", db_path=db) == ["t1"]


# ── API: multi-tag filter + vocabulary + reject ──────────────────────────────

@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient
    from app.api.server import app
    return TestClient(app)


def test_api_multi_tag_filter_and_or(client, db):
    with db_conn(db) as c:
        insert_track(c, "a"); _tag(c, "a", "dub-techno", "lastfm"); _tag(c, "a", "gym-energy", "manual", "private_manual")
        insert_track(c, "b"); _tag(c, "b", "dub-techno", "lastfm")
    # AND: only the track with both
    r = client.get("/api/tracks?tags=dub-techno,gym-energy&tag_mode=and").json()
    assert [t["track_pk"] for t in r["tracks"]] == ["a"]
    # OR: both tracks
    r = client.get("/api/tracks?tags=dub-techno,gym-energy&tag_mode=or").json()
    assert sorted(t["track_pk"] for t in r["tracks"]) == ["a", "b"]


def test_api_hide_then_tag_disappears_from_filters_and_cards(client, db):
    with db_conn(db) as c:
        insert_track(c, "a"); _tag(c, "a", "electronic", "lastfm"); _tag(c, "a", "house", "lastfm")
    assert client.put("/api/vocabulary/electronic", json={"hidden": True}).status_code == 200
    # card no longer shows it
    r = client.get("/api/tracks").json()
    tags = [g["tag"] for g in r["tracks"][0]["tags"]]
    assert "electronic" not in tags and "house" in tags
    # /api/tags (filter chips) no longer lists it
    assert "electronic" not in [t["tag"] for t in client.get("/api/tags").json()]
    # but vocabulary admin still lists it so it can be un-hidden
    assert "electronic" in [v["tag"] for v in client.get("/api/vocabulary").json()]


def test_api_reject_and_unreject(client, db):
    with db_conn(db) as c:
        insert_track(c, "a"); _tag(c, "a", "dubby", "lastfm"); _tag(c, "a", "house", "lastfm")
    assert client.post("/api/tracks/a/tags/dubby/reject").status_code == 200
    tags = [g["tag"] for g in client.get("/api/tracks").json()["tracks"][0]["tags"]]
    assert tags == ["dubby"][:0] + ["house"]  # dubby gone
    # undo
    assert client.delete("/api/tracks/a/tags/dubby/reject").status_code == 200
    tags = sorted(g["tag"] for g in client.get("/api/tracks").json()["tracks"][0]["tags"])
    assert tags == ["dubby", "house"]


def test_api_alias_is_mutually_exclusive_with_hide(client, db):
    with db_conn(db) as c:
        insert_track(c, "a"); _tag(c, "a", "synthpop", "lastfm")
    # hide first
    client.put("/api/vocabulary/synthpop", json={"hidden": True})
    # then alias — should clear hidden
    client.put("/api/vocabulary/synthpop", json={"alias_to": "synth-pop"})
    row = [v for v in client.get("/api/vocabulary").json() if v["tag"] == "synthpop"][0]
    assert row["alias_to"] == "synth-pop" and row["hidden"] is False


# ── source-playlist membership ───────────────────────────────────────────────

def test_adapter_captures_membership_without_extra_fetch():
    from app.ingestion.ytm_adapter import YouTubeMusicAdapter

    def song(v):
        return {"videoId": v, "title": v, "artists": [{"name": "A"}],
                "album": {"name": "Al"}, "duration_seconds": 200}

    class FakeClient:
        def get_library_songs(self, limit=None): return [song("vid_a")]
        def get_liked_songs(self, limit=None): return {"tracks": []}
        def get_library_playlists(self, limit=None):
            return [{"playlistId": "P1", "title": "Discover Weekly Archive"}]
        def get_playlist(self, pid, limit=10000):
            return {"tracks": [song("vid_a"), song("vid_b")]}

    a = YouTubeMusicAdapter()
    a._client = FakeClient()
    a.fetch_library_snapshot()
    assert "P1" in a.last_playlist_memberships
    assert a.last_playlist_memberships["P1"]["name"] == "Discover Weekly Archive"
    # vid_a is on the library surface too, but membership still records it
    assert a.last_playlist_memberships["P1"]["video_ids"] == {"vid_a", "vid_b"}


def test_record_memberships_resolves_and_delete_replaces(db):
    from app.ingestion.ledger import record_source_memberships
    with db_conn(db) as c:
        insert_track(c, "pk_a", ytm_track_id="vid_a")
        insert_track(c, "pk_b", ytm_track_id="vid_b")
        insert_track(c, "pk_c", ytm_track_id="vid_c")

    record_source_memberships({"P1": {"name": "DWA", "video_ids": {"vid_a", "vid_b"}}})
    with db_conn(db) as c:
        rows = sorted(r["track_pk"] for r in c.execute(
            "SELECT track_pk FROM track_playlist_membership WHERE playlist_id='P1'"))
    assert rows == ["pk_a", "pk_b"]

    # vid_b removed on YTM, vid_c added — delete-replace must reflect it.
    record_source_memberships({"P1": {"name": "DWA", "video_ids": {"vid_a", "vid_c"}}})
    with db_conn(db) as c:
        rows = sorted(r["track_pk"] for r in c.execute(
            "SELECT track_pk FROM track_playlist_membership WHERE playlist_id='P1'"))
    assert rows == ["pk_a", "pk_c"]


def test_inbox_cards_carry_playlist_membership(client, db):
    # An unrated, untagged, unblocked track lands in the inbox; its YTM
    # playlist membership must ride along on the card.
    with db_conn(db) as c:
        insert_track(c, "pk_a", ytm_track_id="vid_a")  # unrated, no manual tag
        c.execute("INSERT INTO track_playlist_membership (track_pk, playlist_id, playlist_name) "
                  "VALUES ('pk_a', 'P1', 'Discover Weekly Archive')")
    r = client.get("/api/inbox").json()
    assert r["tracks"][0]["track_pk"] == "pk_a"
    assert r["tracks"][0]["playlists"][0]["playlist_name"] == "Discover Weekly Archive"


def test_complete_run_prunes_reassigned_playlist_id(db):
    # YTM reassigns a playlist's id between runs; a complete run must drop the
    # orphaned old-id rows so the duplicate chip disappears.
    from app.ingestion.ledger import record_source_memberships
    with db_conn(db) as c:
        insert_track(c, "pk_a", ytm_track_id="vid_a")
    record_source_memberships({"OLD": {"name": "Favorite Songs", "video_ids": {"vid_a"}}}, run_complete=True)
    record_source_memberships({"NEW": {"name": "Favorite Songs", "video_ids": {"vid_a"}}}, run_complete=True)
    with db_conn(db) as c:
        ids = sorted(r["playlist_id"] for r in c.execute(
            "SELECT DISTINCT playlist_id FROM track_playlist_membership"))
    assert ids == ["NEW"]


def test_incomplete_run_keeps_stale_rows(db):
    # A partial enumeration must NOT prune — it would wipe a transiently-failing
    # playlist's membership.
    from app.ingestion.ledger import record_source_memberships
    with db_conn(db) as c:
        insert_track(c, "pk_a", ytm_track_id="vid_a")
    record_source_memberships({"OLD": {"name": "X", "video_ids": {"vid_a"}}}, run_complete=True)
    record_source_memberships({"NEW": {"name": "X", "video_ids": {"vid_a"}}}, run_complete=False)
    with db_conn(db) as c:
        ids = sorted(r["playlist_id"] for r in c.execute(
            "SELECT DISTINCT playlist_id FROM track_playlist_membership"))
    assert ids == ["NEW", "OLD"]


def test_complete_run_keeps_genuine_same_name_playlists(db):
    # Two REAL playlists sharing a display name are refreshed in the same run,
    # so neither is stale — both survive the prune.
    from app.ingestion.ledger import record_source_memberships
    with db_conn(db) as c:
        insert_track(c, "pk_a", ytm_track_id="vid_a")
        insert_track(c, "pk_b", ytm_track_id="vid_b")
    record_source_memberships({
        "P1": {"name": "House", "video_ids": {"vid_a"}},
        "P2": {"name": "House", "video_ids": {"vid_b"}},
    }, run_complete=True)
    with db_conn(db) as c:
        ids = sorted(r["playlist_id"] for r in c.execute(
            "SELECT DISTINCT playlist_id FROM track_playlist_membership"))
    assert ids == ["P1", "P2"]


def test_adapter_marks_incomplete_on_playlist_fetch_failure():
    from app.ingestion.ytm_adapter import YouTubeMusicAdapter

    def song(v):
        return {"videoId": v, "title": v, "artists": [{"name": "A"}],
                "album": {"name": "Al"}, "duration_seconds": 200}

    class FakeClient:
        def get_library_songs(self, limit=None): return []
        def get_liked_songs(self, limit=None): return {"tracks": []}
        def get_library_playlists(self, limit=None):
            return [{"playlistId": "P1", "title": "OK"}, {"playlistId": "P2", "title": "Bad"}]
        def get_playlist(self, pid, limit=10000):
            if pid == "P2":
                raise RuntimeError("boom")
            return {"tracks": [song("vid_a")]}

    a = YouTubeMusicAdapter()
    a._client = FakeClient()
    a.fetch_library_snapshot()
    assert a.last_snapshot_complete is False           # a gap → prune suppressed
    assert "P1" in a.last_playlist_memberships
    assert "P2" not in a.last_playlist_memberships


def test_adapter_marks_complete_on_clean_run():
    from app.ingestion.ytm_adapter import YouTubeMusicAdapter

    class FakeClient:
        def get_library_songs(self, limit=None): return []
        def get_liked_songs(self, limit=None): return {"tracks": []}
        def get_library_playlists(self, limit=None):
            return [{"playlistId": "P1", "title": "OK"}]
        def get_playlist(self, pid, limit=10000):
            return {"tracks": [{"videoId": "v", "title": "t", "artists": [{"name": "A"}],
                                "album": {"name": "Al"}, "duration_seconds": 200}]}

    a = YouTubeMusicAdapter()
    a._client = FakeClient()
    a.fetch_library_snapshot()
    assert a.last_snapshot_complete is True


def test_api_source_playlist_filter(db):
    from fastapi.testclient import TestClient
    from app.api.server import app
    client = TestClient(app)
    with db_conn(db) as c:
        insert_track(c, "pk_a", ytm_track_id="vid_a")
        insert_track(c, "pk_b", ytm_track_id="vid_b")
        c.execute("INSERT INTO track_playlist_membership (track_pk, playlist_id, playlist_name) "
                  "VALUES ('pk_a', 'P1', 'DWA')")
    # filter to P1 returns only pk_a
    r = client.get("/api/tracks?source_playlist=P1").json()
    assert [t["track_pk"] for t in r["tracks"]] == ["pk_a"]
    # and the card carries the membership chip
    assert r["tracks"][0]["playlists"][0]["playlist_name"] == "DWA"
    # source-playlists listing
    pls = client.get("/api/source-playlists").json()
    assert pls[0]["playlist_id"] == "P1" and pls[0]["n"] == 1


def test_api_reference_profiles_vocabulary(client, db):
    """GET /api/reference/profiles returns the seeded profile vocabulary with a
    taxonomy_layer, grouped-friendly for the tap-palette."""
    from app.playlists.utility import seed_starter_tag_profiles
    seed_starter_tag_profiles(db)
    rows = client.get("/api/reference/profiles").json()["profiles"]
    # Locked vocab: 55 at the 2026-07-03 lock + pop rap (2026-07-05 one-off) = 56.
    assert len(rows) == 56
    layers = [r["taxonomy_layer"] for r in rows]
    assert layers == sorted(layers)  # ordered by layer for grouping
    from collections import Counter
    assert Counter(layers) == {"functional": 8, "personal": 7, "family": 11,
                               "subgenre": 25, "era": 5}
    assert all({"profile_id", "tag_name", "taxonomy_layer", "description"} <= r.keys() for r in rows)


def test_api_search_matches_tags(client, db):
    """The search bar (q=) matches effective tags, not just title/artist/album —
    and respects hide/alias via the view."""
    with db_conn(db) as c:
        insert_track(c, "a", canonical_title="Untitled", canonical_artist="Anon")
        _tag(c, "a", "dub-techno", "lastfm")
        insert_track(c, "b", canonical_title="Other", canonical_artist="Nobody")
        _tag(c, "b", "ambient", "lastfm")
    # substring of a tag finds the carrying track
    r = client.get("/api/tracks?q=dub").json()
    assert [t["track_pk"] for t in r["tracks"]] == ["a"]
    # hidden tag drops out of search (view-driven, same as filter chips)
    assert client.put("/api/vocabulary/dub-techno", json={"hidden": True}).status_code == 200
    assert client.get("/api/tracks?q=dub").json()["tracks"] == []
    # title search still works alongside
    assert [t["track_pk"] for t in client.get("/api/tracks?q=other").json()["tracks"]] == ["b"]
