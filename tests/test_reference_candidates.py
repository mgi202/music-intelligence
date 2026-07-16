"""Profile-specific Training candidate ranking (2026-07-15 rework).

Before this rework /api/reference/candidates collapsed to the same generic
rated-newest list for every profile without tag matches. Now:
  1. vector path — Qdrant retrieval around the positive-exemplar centroid,
     interleaving likely-positives with boundary cases;
  2. fallback — graded per-profile term overlap (COUNT of matching effective
     tags across name + context terms + parent family).

Network-free: the vector store is monkeypatched.
"""

from __future__ import annotations

import json

import pytest

from app.db.connection import db_conn
from tests.conftest import insert_track


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient
    from app.api.server import app
    return TestClient(app)


def _profile(conn, pid, name, terms=None, family=None, layer="subgenre"):
    conn.execute(
        "INSERT INTO tag_profiles (profile_id, tag_name, taxonomy_layer, "
        "context_terms_json, parent_family) VALUES (?, ?, ?, ?, ?)",
        (pid, name, layer, json.dumps(terms or []), family),
    )


def _tag(conn, pk, tag, tag_type="public"):
    conn.execute(
        "INSERT INTO track_tags (track_pk, tag, tag_type, source, confidence) "
        "VALUES (?, ?, ?, 'test', 1.0)",
        (pk, tag, tag_type),
    )


def _label(conn, pk, pid, label_type="positive"):
    conn.execute(
        "INSERT INTO reference_track_labels (track_pk, profile_id, label_type) "
        "VALUES (?, ?, ?)",
        (pk, pid, label_type),
    )


def _pks(client, pid, limit=30):
    r = client.get(f"/api/reference/candidates?profile_id={pid}&limit={limit}")
    assert r.status_code == 200
    return [c["track_pk"] for c in r.json()["candidates"]]


# ── fallback ranking (no positive vectors) ──────────────────────────────────

def test_fallback_is_profile_specific(client, db):
    """Two profiles with disjoint terms must NOT get the same top candidate."""
    with db_conn(db) as c:
        _profile(c, "p_dub", "dubstyle-test", terms=["dubwise"])
        _profile(c, "p_gab", "gabber-test", terms=["hakken"])
        insert_track(c, "t_dub"); _tag(c, "t_dub", "dubwise")
        insert_track(c, "t_gab"); _tag(c, "t_gab", "hakken")
        insert_track(c, "t_plain", personal_rating=4)
    assert _pks(client, "p_dub")[0] == "t_dub"
    assert _pks(client, "p_gab")[0] == "t_gab"


def test_fallback_graded_overlap(client, db):
    """Two term hits outrank one; one outranks zero even against a rating."""
    with db_conn(db) as c:
        _profile(c, "p1", "styleone-test", terms=["alpha-term", "beta-term"])
        insert_track(c, "t_two"); _tag(c, "t_two", "alpha-term"); _tag(c, "t_two", "beta-term")
        insert_track(c, "t_one"); _tag(c, "t_one", "alpha-term")
        insert_track(c, "t_zero", personal_rating=4)
    got = _pks(client, "p1")
    assert got.index("t_two") < got.index("t_one") < got.index("t_zero")


def test_parent_family_counts_as_term(client, db):
    with db_conn(db) as c:
        _profile(c, "p1", "substyle-test", family="techno-fam-test")
        insert_track(c, "t_fam"); _tag(c, "t_fam", "techno-fam-test")
        insert_track(c, "t_none", personal_rating=4)
    assert _pks(client, "p1")[0] == "t_fam"


def test_labelled_tracks_excluded(client, db):
    with db_conn(db) as c:
        _profile(c, "p1", "excl-test", terms=["excl-term"])
        insert_track(c, "t_a"); _tag(c, "t_a", "excl-term")
        insert_track(c, "t_b"); _tag(c, "t_b", "excl-term")
        _label(c, "t_a", "p1", "positive")
    got = _pks(client, "p1")
    assert "t_a" not in got and "t_b" in got


# ── vector path ──────────────────────────────────────────────────────────────

def _patch_vectors(monkeypatch, pos_vec, hits):
    from app.audio import vectors
    monkeypatch.setattr(vectors, "load_vectors",
                        lambda pks, db_path=None: {pk: pos_vec for pk in pks})
    monkeypatch.setattr(vectors, "search_similar",
                        lambda vector, limit=20, exclude_track_pk=None: hits)


def test_vector_path_interleaves_likely_and_boundary(client, db, monkeypatch):
    """Retrieval [h1 h2 h3 h4] serves h1, h3, h2, h4 — likely positive,
    boundary, likely positive, boundary."""
    with db_conn(db) as c:
        _profile(c, "p1", "vec-test")
        insert_track(c, "t_ref")
        _label(c, "t_ref", "p1", "positive")
        for pk in ("h1", "h2", "h3", "h4"):
            insert_track(c, pk)
    hits = [{"track_pk": pk, "score": s, "payload": {}}
            for pk, s in (("h1", .9), ("h2", .8), ("h3", .6), ("h4", .5))]
    _patch_vectors(monkeypatch, [1.0, 0.0], hits)
    assert _pks(client, "p1")[:4] == ["h1", "h3", "h2", "h4"]


def test_vector_path_excludes_labelled_and_tops_up(client, db, monkeypatch):
    """Already-labelled hits are dropped; shortfall tops up from fallback."""
    with db_conn(db) as c:
        _profile(c, "p1", "vec-topup-test", terms=["topup-term"])
        insert_track(c, "t_ref"); _label(c, "t_ref", "p1", "positive")
        insert_track(c, "h1")
        insert_track(c, "t_meta"); _tag(c, "t_meta", "topup-term")
    hits = [{"track_pk": "h1", "score": .9, "payload": {}},
            {"track_pk": "t_ref", "score": .8, "payload": {}}]
    _patch_vectors(monkeypatch, [1.0, 0.0], hits)
    got = _pks(client, "p1")
    assert got[0] == "h1"
    assert "t_ref" not in got
    assert "t_meta" in got            # fallback top-up still profile-relevant


def test_vector_store_down_falls_back(client, db, monkeypatch):
    """Qdrant unreachable ⇒ silent fallback to metadata ranking."""
    from app.audio import vectors

    def boom(*a, **k):
        raise vectors.VectorStoreError("down")

    with db_conn(db) as c:
        _profile(c, "p1", "vec-down-test", terms=["down-term"])
        insert_track(c, "t_ref"); _label(c, "t_ref", "p1", "positive")
        insert_track(c, "t_meta"); _tag(c, "t_meta", "down-term")
    monkeypatch.setattr(vectors, "load_vectors",
                        lambda pks, db_path=None: {pk: [1.0] for pk in pks})
    monkeypatch.setattr(vectors, "search_similar", boom)
    assert _pks(client, "p1")[0] == "t_meta"
