"""
Review live-test fixes + FE vocab manager (handoff 2026-07-05).

F3 — verdict_committed_at: a committed track never re-serves in either lens;
     undo (uncommit) restores it.
F4 — pop rap promoted to a locked subgenre profile; its old alias fold into
     hip hop is cleared and the spelling variant re-pointed.
F5 — vocab manager: create/rename/retire/restore/delete for personal +
     subgenre; user-defined rows and rename tombstones survive reconcile.
"""

from __future__ import annotations

import pytest

from app.db.connection import db_conn, get_connection
from tests.conftest import insert_track


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient
    from app.api.server import app
    return TestClient(app)


def _pub(conn, pk, tag, source="lastfm", confidence=0.5):
    conn.execute(
        "INSERT INTO track_tags (track_pk, tag, tag_type, source, confidence) "
        "VALUES (?, ?, 'public', ?, ?)",
        (pk, tag, source, confidence),
    )


def _manual(conn, pk, tag):
    conn.execute(
        "INSERT INTO track_tags (track_pk, tag, tag_type, source, confidence) "
        "VALUES (?, ?, 'private_manual', 'manual', 1.0)",
        (pk, tag),
    )


# ── F3: commit stamp ─────────────────────────────────────────────────────────

def _queue_pks(db, sort):
    from app.tags import verdict_queue as vq
    q = vq.build_queue(limit=50, db_path=db, sort=sort)
    return [t["pk"] for t in q["tracks"]], q["meta"]["eligible_total"]


def test_commit_retires_from_both_lenses_and_uncommit_restores(db):
    from app.tags import verdict_queue as vq
    with db_conn(db) as c:
        insert_track(c, "t1", ytm_track_id="v1")
        insert_track(c, "t2", ytm_track_id="v2")

    for sort in ("training", "newest"):
        pks, total = _queue_pks(db, sort)
        assert "t1" in pks and "t2" in pks
        assert total == 2

    # A commit with only a rating + family suggestion (the Outkast case):
    # nothing in the functional/personal exclusion fires, but the stamp must.
    vq.commit_track("t1", db_path=db)
    for sort in ("training", "newest"):
        pks, total = _queue_pks(db, sort)
        assert "t1" not in pks, f"committed track re-served in {sort} lens"
        assert total == 1

    # Undo → the track re-serves.
    vq.uncommit_track("t1", db_path=db)
    for sort in ("training", "newest"):
        pks, total = _queue_pks(db, sort)
        assert "t1" in pks
        assert total == 2


def test_commit_endpoints(client, db):
    with db_conn(db) as c:
        insert_track(c, "t1", ytm_track_id="v1")
    r = client.post("/api/tracks/t1/verdict/commit")
    assert r.status_code == 200 and r.json()["committed"] is True
    with db_conn(db) as c:
        assert c.execute(
            "SELECT verdict_committed_at FROM tracks WHERE track_pk='t1'"
        ).fetchone()[0]
    r = client.post("/api/tracks/t1/verdict/uncommit")
    assert r.status_code == 200 and r.json()["committed"] is False
    with db_conn(db) as c:
        assert c.execute(
            "SELECT verdict_committed_at FROM tracks WHERE track_pk='t1'"
        ).fetchone()[0] is None
    assert client.post("/api/tracks/nope/verdict/commit").status_code == 404


# ── F4: pop rap promotion ────────────────────────────────────────────────────

def test_pop_rap_profile_seeded(db):
    conn = get_connection(db)
    try:
        row = conn.execute(
            "SELECT taxonomy_layer, parent_family FROM tag_profiles "
            "WHERE profile_id = 'pop rap'"
        ).fetchone()
        assert row and row["taxonomy_layer"] == "subgenre"
        assert row["parent_family"] == "hip hop"
        # The raw tag must not be folded away; the spelling variant folds to it.
        assert conn.execute(
            "SELECT 1 FROM tag_vocabulary WHERE tag = 'pop rap'"
        ).fetchone() is None
        alias = conn.execute(
            "SELECT alias_to FROM tag_vocabulary WHERE tag = 'pop-rap'"
        ).fetchone()
        assert alias and alias["alias_to"] == "pop rap"
    finally:
        conn.close()


def test_promotion_clears_stale_alias_row(db):
    # A prod DB still carries the old pop rap → hip hop ruling; the promotion
    # sweep must delete it on the next reconcile.
    from app.tags.vocab_lock import reconcile_tag_vocabulary
    with db_conn(db) as c:
        c.execute(
            "INSERT INTO tag_vocabulary (tag, hidden, alias_to) "
            "VALUES ('pop rap', 0, 'hip hop') "
            "ON CONFLICT(tag) DO UPDATE SET alias_to='hip hop', hidden=0"
        )
    summary = reconcile_tag_vocabulary(db)
    assert summary["promotions_cleared"] == 1
    conn = get_connection(db)
    try:
        assert conn.execute(
            "SELECT 1 FROM tag_vocabulary WHERE tag = 'pop rap'"
        ).fetchone() is None
    finally:
        conn.close()


def test_pop_rap_suggestible_from_public_tags(db):
    from app.tags import verdict_queue as vq
    with db_conn(db) as c:
        insert_track(c, "t1", ytm_track_id="v1", canonical_artist="Outkast")
        _pub(c, "t1", "pop rap", source="lastfm", confidence=0.4)
    q = vq.build_queue(limit=10, db_path=db, sort="training")
    sugg = {s["profile_id"] for s in q["tracks"][0]["suggestions"]}
    assert "pop rap" in sugg


# ── F5: vocab manager ────────────────────────────────────────────────────────

def test_create_personal_profile_kebab_cased_and_visible(client, db):
    r = client.post("/api/vocabulary/profiles", json={
        "name": "Egg Floor 2", "layer": "personal",
        "description": "Second-floor kitchen at the Egg — social, warm.",
    })
    assert r.status_code == 200
    assert r.json()["profile_id"] == "egg-floor-2"

    profs = client.get("/api/reference/profiles").json()["profiles"]
    pers = [p for p in profs if p["taxonomy_layer"] == "personal"]
    # Appended at the end of the layer → gets the next free hotkey slot.
    assert pers[-1]["profile_id"] == "egg-floor-2"
    assert "Egg" in pers[-1]["description"]

    ready = client.get("/api/reference/readiness").json()["profiles"]
    mine = [p for p in ready if p["profile_id"] == "egg-floor-2"]
    assert mine and mine[0]["positive"] == 0 and not mine[0]["ready"]


def test_create_requires_description_and_unique_name(client, db):
    r = client.post("/api/vocabulary/profiles", json={
        "name": "beach", "layer": "personal", "description": " ",
    })
    assert r.status_code == 400
    r = client.post("/api/vocabulary/profiles", json={
        "name": "gym", "layer": "personal", "description": "clash with locked",
    })
    assert r.status_code == 409
    # functional stays code-locked — creating into it is rejected.
    r = client.post("/api/vocabulary/profiles", json={
        "name": "hyperpop", "layer": "functional", "description": "wrong layer",
    })
    assert r.status_code == 400
    # era went self-service (2026-07-06) — creating into it now succeeds.
    r = client.post("/api/vocabulary/profiles", json={
        "name": "y2k", "layer": "era", "description": "Millennial trance sheen",
    })
    assert r.status_code == 200
    assert r.json()["taxonomy_layer"] == "era"


def test_user_defined_profile_survives_reconcile(client, db):
    from app.playlists.utility import reconcile_tag_profiles
    client.post("/api/vocabulary/profiles", json={
        "name": "beach", "layer": "personal", "description": "Sea, sand, sun.",
    })
    summary = reconcile_tag_profiles(db)
    assert "beach" in summary["kept_user_approved"]
    assert "beach" not in summary["dropped"]
    conn = get_connection(db)
    try:
        assert conn.execute(
            "SELECT 1 FROM tag_profiles WHERE profile_id = 'beach'"
        ).fetchone()
    finally:
        conn.close()


def test_rename_migrates_labels_tags_and_tombstones_locked_id(client, db):
    from app.playlists.utility import reconcile_tag_profiles
    from app.tags.reference_manager import add_reference_label
    with db_conn(db) as c:
        insert_track(c, "t1", ytm_track_id="v1")
        _manual(c, "t1", "gym")
    add_reference_label("t1", "gym", "positive", db_path=db)

    r = client.post("/api/vocabulary/profiles/gym/rename",
                    json={"new_name": "egg floor 2"})
    assert r.status_code == 200
    body = r.json()
    assert body["profile_id"] == "egg-floor-2"
    assert body["labels_migrated"] == 1 and body["tags_migrated"] == 1

    conn = get_connection(db)
    try:
        assert conn.execute(
            "SELECT 1 FROM tag_profiles WHERE profile_id = 'gym'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM reference_track_labels "
            "WHERE profile_id = 'egg-floor-2' AND track_pk = 't1'"
        ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM track_tags WHERE track_pk = 't1' "
            "AND tag = 'egg-floor-2' AND tag_type = 'private_manual'"
        ).fetchone()
        # Old raw tag folds into the new name.
        assert conn.execute(
            "SELECT alias_to FROM tag_vocabulary WHERE tag = 'gym'"
        ).fetchone()["alias_to"] == "egg-floor-2"
    finally:
        conn.close()

    # The next init_db must NOT resurrect the locked 'gym'.
    summary = reconcile_tag_profiles(db)
    assert "gym" not in summary["inserted"]
    conn = get_connection(db)
    try:
        assert conn.execute(
            "SELECT 1 FROM tag_profiles WHERE profile_id = 'gym'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT user_defined FROM tag_profiles WHERE profile_id = 'egg-floor-2'"
        ).fetchone()["user_defined"] == 1
    finally:
        conn.close()


def test_rename_refuses_locked_layers_and_name_clashes(client, db):
    assert client.post("/api/vocabulary/profiles/warm-up/rename",
                       json={"new_name": "opener"}).status_code == 400
    assert client.post("/api/vocabulary/profiles/gym/rename",
                       json={"new_name": "drive"}).status_code == 409
    assert client.post("/api/vocabulary/profiles/nope/rename",
                       json={"new_name": "x"}).status_code == 404


def test_retire_hides_from_review_and_suggestions_restore_brings_back(client, db):
    from app.tags import verdict_queue as vq
    with db_conn(db) as c:
        insert_track(c, "t1", ytm_track_id="v1")
        _pub(c, "t1", "deep house", confidence=0.6)

    q = vq.build_queue(limit=10, db_path=db, sort="training")
    assert "deep house" in {s["profile_id"] for s in q["tracks"][0]["suggestions"]}

    assert client.post("/api/vocabulary/profiles/deep%20house/retire",
                       json={}).status_code == 200
    profs = client.get("/api/reference/profiles").json()["profiles"]
    assert "deep house" not in {p["profile_id"] for p in profs}
    ready = client.get("/api/reference/readiness").json()["profiles"]
    assert "deep house" not in {p["profile_id"] for p in ready}
    q = vq.build_queue(limit=10, db_path=db, sort="training")
    assert "deep house" not in {s["profile_id"] for s in q["tracks"][0]["suggestions"]}
    # Tags tab still lists it (with the retired stamp) so it can be restored.
    vocab = client.get("/api/vocabulary/profiles").json()
    row = [p for p in vocab if p["profile_id"] == "deep house"]
    assert row and row[0]["retired_at"]

    assert client.post("/api/vocabulary/profiles/deep%20house/restore",
                       json={}).status_code == 200
    profs = client.get("/api/reference/profiles").json()["profiles"]
    assert "deep house" in {p["profile_id"] for p in profs}


def test_retired_survives_reconcile_refresh(db, client):
    # Reconcile refreshes locked rows (description etc.) but must not clear
    # the retire stamp — a retired profile stays retired across deploys.
    from app.playlists.utility import reconcile_tag_profiles
    client.post("/api/vocabulary/profiles/host/retire", json={})
    reconcile_tag_profiles(db)
    conn = get_connection(db)
    try:
        assert conn.execute(
            "SELECT retired_at FROM tag_profiles WHERE profile_id = 'host'"
        ).fetchone()["retired_at"]
    finally:
        conn.close()


def test_delete_only_when_clean(client, db):
    client.post("/api/vocabulary/profiles", json={
        "name": "beach", "layer": "personal", "description": "Sea, sand, sun.",
    })
    with db_conn(db) as c:
        insert_track(c, "t1", ytm_track_id="v1")
        _manual(c, "t1", "beach")
    r = client.delete("/api/vocabulary/profiles/beach")
    assert r.status_code == 409  # has a manual tag → retire instead
    with db_conn(db) as c:
        c.execute("DELETE FROM track_tags WHERE tag = 'beach'")
    r = client.delete("/api/vocabulary/profiles/beach")
    assert r.status_code == 200
    conn = get_connection(db)
    try:
        assert conn.execute(
            "SELECT 1 FROM tag_profiles WHERE profile_id = 'beach'"
        ).fetchone() is None
    finally:
        conn.close()


def test_create_subgenre_with_family_and_alias_unfold(client, db):
    # Adding a subgenre whose raw tag the lock folded away (indie rock → rock)
    # must clear the fold so the tag surfaces as itself again.
    r = client.post("/api/vocabulary/profiles", json={
        "name": "indie rock", "layer": "subgenre",
        "description": "Guitar-band indie.", "parent_family": "rock",
    })
    assert r.status_code == 200
    conn = get_connection(db)
    try:
        assert conn.execute(
            "SELECT 1 FROM tag_vocabulary WHERE tag = 'indie rock'"
        ).fetchone() is None
        row = conn.execute(
            "SELECT parent_family, user_defined FROM tag_profiles "
            "WHERE profile_id = 'indie rock'"
        ).fetchone()
        assert row["parent_family"] == "rock" and row["user_defined"] == 1
    finally:
        conn.close()
    # Unknown family → 400.
    assert client.post("/api/vocabulary/profiles", json={
        "name": "hyperpop", "layer": "subgenre",
        "description": "x", "parent_family": "zzz",
    }).status_code == 400
