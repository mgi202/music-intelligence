"""Dynamic subgenre-vocabulary expansion (app/tags/vocab_expansion.py).

Covers the 4 Jul rulings: tier quotas, the 30-track candidate floor,
coverage counting every effective tag type (incl. private_manual), aliased
tags as promotable candidates (spelling variants excluded), sticky
rejection, and — the architecture change — user-approved profiles surviving
reconcile_tag_profiles.
"""

from __future__ import annotations

import pytest

from app.db.connection import db_conn
from app.tags import vocab_expansion as vx
from tests.conftest import insert_track


def _tag(conn, pk, tag, tag_type="public", source="discogs"):
    conn.execute(
        "INSERT OR IGNORE INTO track_tags (track_pk, tag, tag_type, source) "
        "VALUES (?, ?, ?, ?)",
        (pk, tag, tag_type, source),
    )


def _seed_family_tracks(conn, family, n, start=0, extra_tag=None,
                        extra_type="public"):
    """n tracks all tagged with `family` (and optionally one more tag)."""
    for i in range(start, start + n):
        pk = f"{family[:2]}{i}"
        insert_track(conn, pk)
        _tag(conn, pk, family)
        if extra_tag:
            _tag(conn, pk, extra_tag, tag_type=extra_type)


def test_slots_for_tier_table():
    assert vx.slots_for(5000) == 8
    assert vx.slots_for(1000) == 8
    assert vx.slots_for(999) == 6
    assert vx.slots_for(300) == 6
    assert vx.slots_for(299) == 4
    assert vx.slots_for(100) == 4
    assert vx.slots_for(99) == 2
    assert vx.slots_for(30) == 2
    assert vx.slots_for(29) == 0
    assert vx.slots_for(0) == 0


def test_coverage_counts_all_effective_types(db):
    """private_manual tags raise family coverage — self-correcting
    favouritism: manual tagging lifts its family's tier."""
    with db_conn(db) as conn:
        _seed_family_tracks(conn, "techno", 20)                       # public
        _seed_family_tracks(conn, "techno", 15, start=20,
                            extra_tag=None)
        # 15 more tracks tagged techno ONLY by hand
        for i in range(50, 65):
            pk = f"man{i}"
            insert_track(conn, pk)
            _tag(conn, pk, "techno", tag_type="private_manual", source="user")
    with db_conn(db) as conn:
        cov = vx.family_coverage(conn)
    assert cov["techno"] == 50


def test_candidate_floor_and_slots(db):
    """A family at 30–99 coverage gets 2 slots; candidates under the
    30-track floor never surface."""
    with db_conn(db) as conn:
        # 40 techno tracks → tier 30–99 → 2 slots.
        # 3 candidate subgenre tags co-occurring on those tracks:
        for i in range(40):
            pk = f"te{i}"
            insert_track(conn, pk)
            _tag(conn, pk, "techno")
            if i < 35:
                _tag(conn, pk, "acid techno")     # 35 tracks — best candidate
            if i < 31:
                _tag(conn, pk, "hard techno")     # 31 — second
            if i < 30:
                _tag(conn, pk, "dub techno")      # 30 — meets floor, but no slot
            if i < 29:
                _tag(conn, pk, "bleep techno")    # 29 — UNDER floor
    res = vx.compute_suggestions(db)
    tags = {s["tag"] for s in res["new"]}
    assert tags == {"acid techno", "hard techno"}   # 2 slots, best-covered first
    assert res["slots"]["techno"] == 2
    assert res["coverage"]["techno"] == 40


def test_aliased_tags_promotable_spelling_variants_not(db):
    """Vocab-lock folds (indie rock→rock) are promotable; mechanical
    spelling variants (hip-hop→hip hop) are not."""
    with db_conn(db) as conn:
        # 120 rock tracks → 4 slots. 40 of them raw-tagged 'indie rock',
        # which the lock aliases to rock (so it's invisible in effective).
        for i in range(120):
            pk = f"ro{i}"
            insert_track(conn, pk)
            _tag(conn, pk, "rock")
        for i in range(40):
            _tag(conn, f"ro{i}", "indie rock")
        # 40 hip hop tracks with the 'hip-hop' spelling variant raw tag.
        for i in range(40):
            pk = f"hh{i}"
            insert_track(conn, pk)
            _tag(conn, pk, "hip hop")
            _tag(conn, pk, "hip-hop")
    res = vx.compute_suggestions(db)
    by_tag = {s["tag"]: s for s in res["new"]}
    assert "indie rock" in by_tag
    assert by_tag["indie rock"]["family"] == "rock"
    assert "hip-hop" not in by_tag
    with db_conn(db) as conn:
        row = conn.execute(
            "SELECT was_alias_to FROM vocab_suggestions WHERE tag = 'indie rock'"
        ).fetchone()
    assert row["was_alias_to"] == "rock"


def test_existing_profiles_never_suggested(db):
    """Tags that are already vocabulary profiles (any layer) are excluded."""
    with db_conn(db) as conn:
        _seed_family_tracks(conn, "house", 200, extra_tag="deep house")
    res = vx.compute_suggestions(db)
    assert "deep house" not in {s["tag"] for s in res["new"]}
    assert "house" not in {s["tag"] for s in res["new"]}


def _promote_to_profile(conn, tag, layer="family"):
    """Simulate a tag becoming a vocabulary profile out-of-band — the way
    'jungle' was promoted to its own genre after its suggestion was seeded."""
    conn.execute(
        """INSERT INTO tag_profiles
               (profile_id, tag_name, description, taxonomy_layer,
                sort_order, origin, created_at, updated_at)
           VALUES (?, ?, '', ?, 99, 'locked', '2026-01-01', '2026-01-01')""",
        (tag, tag, layer),
    )


def test_approve_idempotent_when_tag_already_a_profile(db):
    """Regression (jungle): a suggestion whose tag became a profile after it
    was seeded must NOT collide on the profile primary key when approved.
    That collision escaped as a 500 and left the row silently stuck."""
    with db_conn(db) as conn:
        _seed_family_tracks(conn, "techno", 150, extra_tag="acid techno")
    vx.compute_suggestions(db)
    sid = next(s for s in vx.list_suggestions("pending", db)
               if s["tag"] == "acid techno")["suggestion_id"]
    # Promote it out-of-band, then approve the now-stale suggestion.
    with db_conn(db) as conn:
        _promote_to_profile(conn, "acid techno", layer="family")
    out = vx.approve_suggestion(sid, db)               # must not raise
    assert out["status"] == "approved"
    assert out.get("already_in_vocab") is True
    with db_conn(db) as conn:                          # no duplicate profile
        assert conn.execute(
            "SELECT COUNT(*) FROM tag_profiles WHERE tag_name = 'acid techno'"
        ).fetchone()[0] == 1
    assert "acid techno" not in {s["tag"]
                                 for s in vx.list_suggestions("pending", db)}


def test_compute_self_heals_stale_pending_that_became_profile(db):
    """A pending suggestion whose tag has since become a profile is retired by
    the next scan (stale_resolved) instead of lingering un-approvable."""
    with db_conn(db) as conn:
        _seed_family_tracks(conn, "techno", 150, extra_tag="acid techno")
    vx.compute_suggestions(db)
    assert "acid techno" in {s["tag"] for s in vx.list_suggestions("pending", db)}
    with db_conn(db) as conn:
        _promote_to_profile(conn, "acid techno", layer="subgenre")
    res = vx.compute_suggestions(db)
    assert res["stale_resolved"] >= 1
    assert "acid techno" not in {s["tag"]
                                 for s in vx.list_suggestions("pending", db)}


def test_reject_sticky_across_recompute(db):
    with db_conn(db) as conn:
        _seed_family_tracks(conn, "techno", 150, extra_tag="acid techno")
    res = vx.compute_suggestions(db)
    sid = next(
        s for s in vx.list_suggestions("pending", db) if s["tag"] == "acid techno"
    )["suggestion_id"]
    vx.reject_suggestion(sid, db)
    res2 = vx.compute_suggestions(db)
    assert "acid techno" not in {s["tag"] for s in res2["new"]}
    assert vx.list_suggestions("pending", db) == []
    # Double-decide guards
    with pytest.raises(LookupError):
        vx.reject_suggestion(sid, db)
    with pytest.raises(LookupError):
        vx.approve_suggestion(sid, db)


def test_approve_creates_user_profile_and_unfolds_alias(db):
    with db_conn(db) as conn:
        for i in range(120):
            pk = f"ro{i}"
            insert_track(conn, pk)
            _tag(conn, pk, "rock")
        for i in range(40):
            _tag(conn, f"ro{i}", "indie rock")
    vx.compute_suggestions(db)
    sid = next(
        s for s in vx.list_suggestions("pending", db) if s["tag"] == "indie rock"
    )["suggestion_id"]
    out = vx.approve_suggestion(sid, db)
    assert out["profile_id"] == "indie rock"
    with db_conn(db) as conn:
        prof = conn.execute(
            "SELECT taxonomy_layer, origin FROM tag_profiles WHERE profile_id = 'indie rock'"
        ).fetchone()
        assert prof["taxonomy_layer"] == "subgenre"
        assert prof["origin"] == "user_approved"
        # The vocab-lock alias row is gone → the tag surfaces as itself again.
        assert conn.execute(
            "SELECT 1 FROM tag_vocabulary WHERE tag = 'indie rock'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT COUNT(*) FROM effective_track_tags "
            "WHERE tag = 'indie rock'"
        ).fetchone()[0] == 40
    # Approved tags never resurface as candidates (they're profiles now).
    res = vx.compute_suggestions(db)
    assert "indie rock" not in {s["tag"] for s in res["new"]}


def test_user_approved_profile_survives_reconcile(db):
    """THE architecture change: vocabulary is DB-authoritative for
    additions. reconcile_tag_profiles (runs on every deploy/init) must keep
    label-free user-approved profiles."""
    from app.playlists.utility import reconcile_tag_profiles

    with db_conn(db) as conn:
        _seed_family_tracks(conn, "techno", 150, extra_tag="acid techno")
    vx.compute_suggestions(db)
    sid = vx.list_suggestions("pending", db)[0]["suggestion_id"]
    vx.approve_suggestion(sid, db)

    summary = reconcile_tag_profiles(db)
    assert "acid techno" in summary["kept_user_approved"]
    assert "acid techno" not in summary["dropped"]
    with db_conn(db) as conn:
        assert conn.execute(
            "SELECT 1 FROM tag_profiles WHERE profile_id = 'acid techno'"
        ).fetchone() is not None


def test_pending_suggestions_occupy_slots(db):
    """A family's pending suggestions count against its quota — recompute
    doesn't stack unlimited proposals."""
    with db_conn(db) as conn:
        for i in range(60):
            pk = f"te{i}"
            insert_track(conn, pk)
            _tag(conn, pk, "techno")
            _tag(conn, pk, "acid techno")
            _tag(conn, pk, "hard techno")
            _tag(conn, pk, "dub techno")
    res = vx.compute_suggestions(db)
    assert len(res["new"]) == 2          # 2 slots at coverage 60
    res2 = vx.compute_suggestions(db)
    assert res2["new"] == []             # slots already occupied by pending
    assert res2["pending_total"] == 2


def test_api_endpoints(db):
    from fastapi.testclient import TestClient
    from app.api.server import app

    with db_conn(db) as conn:
        _seed_family_tracks(conn, "techno", 150, extra_tag="acid techno")

    client = TestClient(app)
    r = client.post("/api/vocab-suggestions/recompute")
    assert r.status_code == 200
    assert {s["tag"] for s in client.get("/api/vocab-suggestions").json()["suggestions"]} \
        == {"acid techno"}

    sid = client.get("/api/vocab-suggestions").json()["suggestions"][0]["suggestion_id"]
    r = client.post(f"/api/vocab-suggestions/{sid}/approve")
    assert r.status_code == 200
    assert r.json()["status"] == "approved"
    # Already decided → 409; unknown → 404.
    assert client.post(f"/api/vocab-suggestions/{sid}/approve").status_code == 409
    assert client.post("/api/vocab-suggestions/99999/reject").status_code == 404

    profs = client.get("/api/vocabulary/profiles").json()
    mine = [p for p in profs if p["profile_id"] == "acid techno"]
    assert mine and mine[0]["origin"] == "user_approved" and mine[0]["n"] == 150

    vocab = client.get("/api/vocabulary").json()
    at = next(v for v in vocab if v["tag"] == "acid techno")
    assert at["layer"] == "subgenre"
