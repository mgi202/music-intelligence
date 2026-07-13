"""Phase 3 — kNN classification engine (app/tags/knn_classifier.py).

Deterministic 512-dim vectors: positives cluster on axis e0, negatives on e1.
Uses the seeded 'techno' profile (bpm 120–150, energy 0.55–1.0). Proves the
readiness gate, status banding, the margin gate, manual-tag protection, and
the accept/reject review actions.
"""

from __future__ import annotations

import json

from app.db.connection import db_conn, get_connection
from tests.conftest import insert_track

DIM = 512


def _axis(i: int, scale: float = 1.0) -> list[float]:
    v = [0.0] * DIM
    v[i] = scale
    return v


def _mix(a: float, b: float) -> list[float]:
    v = [0.0] * DIM
    v[0], v[1] = a, b
    return v


def _add_ref_track(conn, pk, artist, vector, profile, label):
    insert_track(conn, pk, canonical_artist=artist,
                 normalized_artist=artist.lower(),
                 match_status="audio_enriched")
    conn.execute(
        "INSERT INTO audio_features (track_pk, clap_vector_json) VALUES (?, ?)",
        (pk, json.dumps(vector)),
    )
    conn.execute(
        """INSERT INTO reference_track_labels
               (track_pk, profile_id, label_type, created_by)
           VALUES (?, ?, ?, 'manual')""",
        (pk, profile, label),
    )


def _seed_ready_profile(db, profile="techno"):
    """15 positives (3 artists, on e0) + 15 negatives (on e1)."""
    with db_conn(db) as conn:
        for i in range(15):
            artist = f"Artist {i % 3}"
            _add_ref_track(conn, f"pos{i}", artist, _axis(0), profile, "positive")
        for i in range(15):
            _add_ref_track(conn, f"neg{i}", f"Neg {i}", _axis(1), profile, "negative")


def _add_candidate(db, pk, vector, bpm=130.0, energy=0.8, valence=None,
                   **cols):
    with db_conn(db) as conn:
        insert_track(conn, pk, match_status="audio_enriched", **cols)
        conn.execute(
            "INSERT INTO audio_features (track_pk, clap_vector_json, bpm, "
            "energy, valence) VALUES (?, ?, ?, ?, ?)",
            (pk, json.dumps(vector), bpm, energy, valence),
        )


def _results(db, pk):
    conn = get_connection(db)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM classification_results WHERE track_pk = ?", (pk,)
        ).fetchall()]
    finally:
        conn.close()


# ── Readiness gate ────────────────────────────────────────────────────────────

def test_no_ready_profile_means_no_results(db):
    _add_candidate(db, "cand", _axis(0))
    from app.tags.knn_classifier import run_classification
    stats = run_classification(db_path=db)
    assert stats["scored"] == 0 and stats["profiles"] == 0


# ── Banding + evidence ────────────────────────────────────────────────────────

def test_clear_positive_scores_high_and_writes_tag(db):
    _seed_ready_profile(db)
    _add_candidate(db, "cand", _axis(0), bpm=130, energy=0.8)
    from app.tags.knn_classifier import run_classification
    stats = run_classification(db_path=db)
    assert stats["profiles"] >= 1
    res = [r for r in _results(db, "cand") if r["profile_id"] == "techno"]
    assert len(res) == 1
    r = res[0]
    assert r["status"] in ("auto_applied", "provisional")
    ev = json.loads(r["evidence_json"])
    assert ev["signals"]["knn_similarity_score"] > 0.95
    assert ev["signals"]["knn_margin_score"] > 0.9
    assert ev["signals"]["profile_feature_fit_score"] == 1.0
    assert ev["supporting_references"]
    # private_model tag written; run bookkeeping exists.
    conn = get_connection(db)
    tag = conn.execute(
        "SELECT * FROM track_tags WHERE track_pk='cand' AND tag='techno' "
        "AND tag_type='private_model'").fetchone()
    run = conn.execute("SELECT * FROM classification_runs").fetchone()
    conn.close()
    assert tag is not None
    assert run["completed_at"] is not None


def test_boundary_track_lands_in_review_queue(db):
    _seed_ready_profile(db)
    # Leans positive acoustically but fails the feature ranges → mid band.
    _add_candidate(db, "cand", _mix(0.8, 0.2), bpm=90, energy=0.2)
    from app.tags.knn_classifier import run_classification
    run_classification(db_path=db)
    res = [r for r in _results(db, "cand") if r["profile_id"] == "techno"]
    assert res and res[0]["status"] == "review_required"
    conn = get_connection(db)
    tag = conn.execute(
        "SELECT 1 FROM track_tags WHERE track_pk='cand' AND tag='techno'"
    ).fetchone()
    conn.close()
    assert tag is None  # review_required never writes a tag


def test_clear_negative_is_rejected(db):
    _seed_ready_profile(db)
    _add_candidate(db, "cand", _axis(1), bpm=90, energy=0.2)  # on the negative axis
    from app.tags.knn_classifier import run_classification
    run_classification(db_path=db)
    res = [r for r in _results(db, "cand") if r["profile_id"] == "techno"]
    assert res and res[0]["status"] == "rejected"


def test_status_banding_and_margin_gate():
    from app.tags.knn_classifier import _status_for
    assert _status_for(0.90, margin=0.5) == "auto_applied"
    assert _status_for(0.90, margin=0.05) == "provisional"   # margin gate
    assert _status_for(0.75, margin=0.9) == "provisional"
    assert _status_for(0.60, margin=0.9) == "review_required"
    assert _status_for(0.40, margin=0.9) == "rejected"


# ── Protection rules ──────────────────────────────────────────────────────────

def test_manual_tag_pair_is_skipped(db):
    _seed_ready_profile(db)
    _add_candidate(db, "cand", _axis(0))
    with db_conn(db) as conn:
        conn.execute(
            "INSERT INTO track_tags (track_pk, tag, tag_type, source, confidence) "
            "VALUES ('cand', 'techno', 'private_manual', 'manual', 1.0)")
    from app.tags.knn_classifier import run_classification
    run_classification(db_path=db)
    assert [r for r in _results(db, "cand") if r["profile_id"] == "techno"] == []


def test_sticky_near_miss_pair_is_skipped(db):
    _seed_ready_profile(db)
    _add_candidate(db, "cand", _axis(0))
    from app.tags.reference_manager import reject_suggestion
    reject_suggestion("cand", "techno", db_path=db)
    from app.tags.knn_classifier import run_classification
    run_classification(db_path=db)
    assert [r for r in _results(db, "cand") if r["profile_id"] == "techno"] == []


def test_second_run_does_not_duplicate(db):
    _seed_ready_profile(db)
    _add_candidate(db, "cand", _axis(0))
    from app.tags.knn_classifier import run_classification
    run_classification(db_path=db)
    run_classification(db_path=db)
    res = [r for r in _results(db, "cand") if r["profile_id"] == "techno"]
    assert len(res) == 1


# ── Review actions ────────────────────────────────────────────────────────────

def _force_review_row(db):
    _seed_ready_profile(db)
    _add_candidate(db, "cand", _mix(0.8, 0.2), bpm=90, energy=0.2)
    from app.tags.knn_classifier import run_classification
    run_classification(db_path=db)
    return [r for r in _results(db, "cand") if r["profile_id"] == "techno"][0]


def test_accept_writes_private_model_tag(db):
    row = _force_review_row(db)
    from app.tags.knn_classifier import accept_result, list_review_queue
    assert any(q["id"] == row["id"] for q in list_review_queue(db_path=db))
    out = accept_result(row["id"], db_path=db)
    assert out["status"] == "manual_override"
    conn = get_connection(db)
    tag = conn.execute(
        "SELECT tag_type FROM track_tags WHERE track_pk='cand' AND tag='techno'"
    ).fetchone()
    conn.close()
    assert tag["tag_type"] == "private_model"
    assert not any(q["id"] == row["id"] for q in list_review_queue(db_path=db))


def test_reject_records_sticky_near_miss(db):
    row = _force_review_row(db)
    from app.tags.knn_classifier import reject_result
    out = reject_result(row["id"], db_path=db)
    assert out["status"] == "rejected"
    conn = get_connection(db)
    nm = conn.execute(
        "SELECT created_by FROM reference_track_labels WHERE track_pk='cand' "
        "AND profile_id='techno' AND label_type='near_miss'").fetchone()
    conn.close()
    assert nm["created_by"] == "verdict_reject"


# ── API smoke ─────────────────────────────────────────────────────────────────

def test_classifications_endpoint(db):
    row = _force_review_row(db)
    from fastapi.testclient import TestClient
    from app.api.server import app
    cl = TestClient(app)
    results = cl.get("/api/classifications").json()["results"]
    assert any(r["id"] == row["id"] and "evidence" in r for r in results)
    assert cl.post(f"/api/classifications/{row['id']}/accept").status_code == 200
    assert cl.post("/api/classifications/999999/accept").status_code == 404
