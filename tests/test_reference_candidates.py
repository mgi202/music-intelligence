"""Profile-specific Training candidate ranking.

15 Jul rework: vector path + graded term-overlap fallback. 18 Jul v2 (active
learning, app/tags/training_candidates.py): per-exemplar retrieval (no
centroids), margin-based boundary cases shaped by negatives, deficit-adaptive
likely/boundary interleave, artist-gate-aware ordering, and the
classifier-uncertainty feed.

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


# ── v2 active learning (2026-07-18) ─────────────────────────────────────────

def _patch_vector_index(monkeypatch, vecs, min_score=-1.0):
    """Fake store: load_vectors serves `vecs`; search_similar ranks the whole
    index by cosine to the query, keeping hits above min_score."""
    from app.audio import vectors

    def fake_search(vector, limit=20, exclude_track_pk=None):
        hits = [{"track_pk": pk, "score": vectors.cosine(vector, v), "payload": {}}
                for pk, v in vecs.items()]
        hits = [h for h in hits if h["score"] > min_score]
        return sorted(hits, key=lambda h: -h["score"])[:limit]

    monkeypatch.setattr(vectors, "load_vectors",
                        lambda pks, db_path=None: {pk: vecs[pk] for pk in pks
                                                   if pk in vecs})
    monkeypatch.setattr(vectors, "search_similar", fake_search)


def test_margin_boundary_uses_negatives(client, db, monkeypatch):
    """A small-margin track (sim_pos ≈ sim_neg, both non-trivial) leads the
    boundary stream; a far-from-everything track sinks behind it."""
    with db_conn(db) as c:
        _profile(c, "p1", "margin-test")
        insert_track(c, "t_pos"); _label(c, "t_pos", "p1", "positive")
        insert_track(c, "t_neg"); _label(c, "t_neg", "p1", "negative")
        for pk in ("posy", "amb", "far"):
            insert_track(c, pk)
    _patch_vector_index(monkeypatch, {
        "t_pos": [1.0, 0.0, 0.0],
        "t_neg": [0.0, 1.0, 0.0],
        "posy":  [1.0, 0.0, 0.0],            # sim_pos 1, sim_neg 0 → likely
        "amb":   [0.707, 0.707, 0.0],        # ≈.707 to both → true boundary
        "far":   [0.3, 0.2, 0.933],          # margin .1 but sim_neg < floor → sinks
    })
    got = _pks(client, "p1")
    assert got[0] == "posy"                  # likely-positive stream leads L,B mix
    assert got.index("amb") < got.index("far")


def test_per_exemplar_retrieval_covers_all_wings(client, db, monkeypatch):
    """Multi-modal profile: a track near wing B must be retrieved even though
    the centroid of all positives points at wing A's side (regression for the
    centroid collapse)."""
    with db_conn(db) as c:
        _profile(c, "p1", "wing-test")
        insert_track(c, "pa"); _label(c, "pa", "p1", "positive")   # wing A
        insert_track(c, "pb"); _label(c, "pb", "p1", "positive")   # wing B
        insert_track(c, "t_a"); insert_track(c, "t_b")
    # Strict store: only near-exact matches (cos > .9) come back, so a
    # centroid query ([.707,0,.707]-ish) would return NEITHER wing's tracks.
    _patch_vector_index(monkeypatch, {
        "pa":  [1.0, 0.0, 0.0],
        "pb":  [0.0, 0.0, 1.0],
        "t_a": [0.99, 0.0, 0.1],
        "t_b": [0.1, 0.0, 0.99],
    }, min_score=0.9)
    got = _pks(client, "p1")
    assert "t_a" in got and "t_b" in got


def test_artist_gate_new_artist_candidates_lead(client, db):
    """15/15 but only 2 distinct positive artists: new-artist candidates go to
    the top; a same-artist candidate is deprioritised even when newer."""
    with db_conn(db) as c:
        _profile(c, "p1", "gate-test", terms=["gate-term"])
        for i in range(15):
            artist = f"gate-a{i % 2}"
            insert_track(c, f"g_pos{i}", canonical_artist=artist,
                         normalized_artist=artist)
            _label(c, f"g_pos{i}", "p1", "positive")
        for i in range(15):
            insert_track(c, f"g_neg{i}", canonical_artist=f"gn{i}",
                         normalized_artist=f"gn{i}")
            _label(c, f"g_neg{i}", "p1", "negative")
        insert_track(c, "c_same", canonical_artist="gate-a0",
                     normalized_artist="gate-a0",
                     created_at="2026-07-01T00:00:00Z")
        _tag(c, "c_same", "gate-term")
        insert_track(c, "c_new", canonical_artist="Fresh",
                     normalized_artist="fresh",
                     created_at="2026-01-01T00:00:00Z")
        _tag(c, "c_new", "gate-term")
    got = _pks(client, "p1")
    assert got.index("c_new") < got.index("c_same")


def test_mix_pattern_adapts_to_deficit():
    """Deficit-adaptive interleave: short on positives → mostly likely;
    negatives short (or armed) → mostly boundary; both short → balanced."""
    from app.tags.training_candidates import _interleave, _mix_pattern

    def r(ready, pos, neg):
        return {"ready": ready, "needs_positive": pos,
                "needs_negative_or_near_miss": neg}

    assert _mix_pattern(r(False, 10, 0)).count("L") == 3
    assert _mix_pattern(r(False, 0, 10)).count("B") == 3
    assert _mix_pattern(r(True, 0, 0)).count("B") == 3
    assert _mix_pattern(r(False, 5, 5)) == ["L", "B"]

    # The pattern is honoured while both streams have items, and a dry
    # stream yields to the other instead of stalling.
    out = _interleave(["l1", "l2"], ["b1", "b2", "b3"], ["B", "B", "B", "L"], 10)
    assert out == ["b1", "b2", "b3", "l1", "l2"]


def test_classifier_uncertainty_feed(client, db):
    """Near-threshold nightly results surface with provenance, excluding
    exemplars, committed tracks, and tracks whose LATEST result left the band."""
    def _cls(c, pk, conf):
        c.execute(
            "INSERT INTO classification_results (run_id, track_pk, profile_id, "
            "tag, confidence, status) VALUES ('run1', ?, 'p1', 'unsure-test', ?, "
            "'review_required')", (pk, conf))

    with db_conn(db) as c:
        _profile(c, "p1", "unsure-test")
        insert_track(c, "cu_in"); _cls(c, "cu_in", 0.60)
        insert_track(c, "cu_lab"); _cls(c, "cu_lab", 0.60)
        _label(c, "cu_lab", "p1", "near_miss")
        insert_track(c, "cu_comm", verdict_committed_at="2026-07-01T00:00:00Z")
        _cls(c, "cu_comm", 0.60)
        insert_track(c, "cu_hi"); _cls(c, "cu_hi", 0.90)
        insert_track(c, "cu_flip"); _cls(c, "cu_flip", 0.60); _cls(c, "cu_flip", 0.90)

    r = client.get("/api/reference/candidates?profile_id=p1&limit=30")
    assert r.status_code == 200
    cands = r.json()["candidates"]
    flagged = {c["track_pk"] for c in cands
               if c.get("provenance") == "classifier_unsure"}
    assert flagged == {"cu_in"}
    assert cands[0]["track_pk"] == "cu_in"   # boundary stream leads
    assert "cu_lab" not in {c["track_pk"] for c in cands}
