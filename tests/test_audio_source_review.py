"""Audio-source review queue (app/audio/source_review.py + API routes).

The human gate for weak_audio_candidate (0.55–0.91) sources: approve flips
unknown→manual_approved and hands the track to the compute node; reject is
sticky and drains the track to no_audio_source when nothing is left.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.db.connection import db_conn, get_connection
from tests.conftest import insert_track

TOKEN = {"X-Audio-Node-Token": "sekrit"}


def _client():
    from app.api.server import app
    return TestClient(app)


def _seed(db, pk="w1", basis="official_preview", conf=0.75,
          status="weak_audio_candidate", url=None, **cols):
    conn = get_connection(db)
    if not conn.execute("SELECT 1 FROM tracks WHERE track_pk=?", (pk,)).fetchone():
        insert_track(conn, pk, match_status=status,
                     canonical_title="Warehouse Days",
                     canonical_artist="Deep Artist", **cols)
    cur = conn.execute(
        """INSERT INTO audio_source_candidates
               (track_pk, source_type, source_platform, source_url,
                confidence, lawful_basis)
           VALUES (?, 'official_preview', 'itunes', ?, ?, ?)""",
        (pk, url or f"https://a.example/{pk}-{basis}-{conf}.m4a", conf, basis),
    )
    cid = cur.lastrowid
    conn.commit(); conn.close()
    return cid


def _track_status(db, pk):
    conn = get_connection(db)
    try:
        return conn.execute(
            "SELECT match_status FROM tracks WHERE track_pk=?", (pk,)
        ).fetchone()["match_status"]
    finally:
        conn.close()


# ── The review listing ───────────────────────────────────────────────────────

def test_review_lists_weak_tracks_with_unresolved_candidates(db):
    _seed(db, pk="w1", conf=0.7, personal_rating=4)
    _seed(db, pk="w1", conf=0.6)               # second candidate, same track
    _seed(db, pk="w2", conf=0.9)
    _seed(db, pk="strong", conf=0.95, status="lawful_audio_candidate")
    r = _client().get("/api/audio-sources/review")
    assert r.status_code == 200
    tracks = r.json()["tracks"]
    assert [t["track_pk"] for t in tracks] == ["w2", "w1"]  # best confidence first
    w1 = tracks[1]
    assert w1["personal_rating"] == 4
    assert "playlist_count" in w1
    assert [c["confidence"] for c in w1["candidates"]] == [0.7, 0.6]
    assert {"source_platform", "lawful_basis", "title_similarity",
            "artist_similarity", "duration_similarity",
            "source_url"} <= set(w1["candidates"][0])


def test_review_excludes_decided_candidates_and_drained_tracks(db):
    cid = _seed(db, pk="w1", conf=0.8)
    with db_conn(db) as conn:
        conn.execute("UPDATE audio_source_candidates SET rejected=1 "
                     "WHERE candidate_id=?", (cid,))
    assert _client().get("/api/audio-sources/review").json()["tracks"] == []


# ── Approve ──────────────────────────────────────────────────────────────────

def test_approve_flips_unknown_to_manual_approved_and_promotes_track(db):
    cid = _seed(db, pk="w1", basis="unknown", conf=0.7)
    r = _client().post(f"/api/audio-sources/{cid}/approve")
    assert r.status_code == 200
    body = r.json()
    assert body["lawful_basis"] == "manual_approved"
    assert body["track_status"] == "lawful_audio_candidate"
    assert _track_status(db, "w1") == "lawful_audio_candidate"


def test_approve_keeps_known_lawful_basis(db):
    cid = _seed(db, pk="w1", basis="official_preview", conf=0.8)
    body = _client().post(f"/api/audio-sources/{cid}/approve").json()
    assert body["lawful_basis"] == "official_preview"


def test_approve_is_single_shot(db):
    cid = _seed(db, pk="w1", conf=0.7)
    cl = _client()
    assert cl.post(f"/api/audio-sources/{cid}/approve").status_code == 200
    assert cl.post(f"/api/audio-sources/{cid}/approve").status_code == 409
    assert cl.post(f"/api/audio-sources/{cid}/reject").status_code == 409
    assert cl.post("/api/audio-sources/999999/approve").status_code == 404


def test_approved_candidate_is_claimable_by_the_node(db, monkeypatch):
    """The whole point: approval beats the 0.92 gate and the node claims it."""
    monkeypatch.setenv("AUDIO_NODE_TOKEN", "sekrit")
    cid = _seed(db, pk="w1", basis="unknown", conf=0.7)
    cl = _client()
    # Unclaimable while unknown-basis and weak.
    assert cl.post("/api/audio/claim", json={"batch": 8},
                   headers=TOKEN).json()["jobs"] == []
    cl.post(f"/api/audio-sources/{cid}/approve")
    jobs = cl.post("/api/audio/claim", json={"batch": 8},
                   headers=TOKEN).json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["candidate_id"] == cid
    assert jobs[0]["lawful_basis"] == "manual_approved"


def test_claim_prefers_the_human_approved_candidate(db, monkeypatch):
    """A higher-confidence unapproved sibling must not outrank the candidate
    the human actually chose."""
    monkeypatch.setenv("AUDIO_NODE_TOKEN", "sekrit")
    approved = _seed(db, pk="w1", basis="unknown", conf=0.6)
    _seed(db, pk="w1", basis="official_preview", conf=0.85)
    cl = _client()
    cl.post(f"/api/audio-sources/{approved}/approve")
    jobs = cl.post("/api/audio/claim", json={"batch": 8},
                   headers=TOKEN).json()["jobs"]
    assert [j["candidate_id"] for j in jobs] == [approved]


# ── Reject ───────────────────────────────────────────────────────────────────

def test_reject_last_candidate_drains_track_to_no_audio_source(db):
    cid = _seed(db, pk="w1", conf=0.7)
    r = _client().post(f"/api/audio-sources/{cid}/reject",
                       json={"reason": "wrong recording"})
    assert r.status_code == 200
    assert r.json()["track_status"] == "no_audio_source"
    assert _track_status(db, "w1") == "no_audio_source"
    conn = get_connection(db)
    row = conn.execute("SELECT rejected, rejection_reason FROM "
                       "audio_source_candidates WHERE candidate_id=?",
                       (cid,)).fetchone()
    conn.close()
    assert row["rejected"] == 1 and row["rejection_reason"] == "wrong recording"


def test_reject_with_sibling_left_keeps_track_in_review(db):
    cid = _seed(db, pk="w1", conf=0.7)
    _seed(db, pk="w1", conf=0.6)
    body = _client().post(f"/api/audio-sources/{cid}/reject").json()
    assert body["remaining_candidates"] == 1
    assert _track_status(db, "w1") == "weak_audio_candidate"


def test_rejected_candidate_is_never_reserved_and_survives_rediscovery(db):
    """Sticky: the review queue never re-offers it, and discovery's upsert
    leaves the rejected flag alone on a re-find of the same source_url."""
    url = "https://a.example/sticky.m4a"
    cid = _seed(db, pk="w1", conf=0.7, url=url)
    _seed(db, pk="w1", conf=0.6)  # sibling keeps the track in the queue
    cl = _client()
    cl.post(f"/api/audio-sources/{cid}/reject")
    tracks = cl.get("/api/audio-sources/review").json()["tracks"]
    assert all(c["candidate_id"] != cid
               for t in tracks for c in t["candidates"])
    # Re-discovery of the same URL (the upsert path) must not resurrect it.
    from app.enrichment.audio_source import _upsert_candidate
    with db_conn(db) as conn:
        same = _upsert_candidate(conn, "w1", {
            "source_type": "official_preview", "source_platform": "itunes",
            "source_url": url, "candidate_title": "Warehouse Days",
            "candidate_artist": "Deep Artist", "candidate_duration_ms": None,
            "candidate_isrc": None, "artist_similarity": 0.9,
            "title_similarity": 0.9, "duration_similarity": 0.0,
            "confidence": 0.8, "lawful_basis": "official_preview",
        })
        assert same == cid
        still = conn.execute("SELECT rejected FROM audio_source_candidates "
                             "WHERE candidate_id=?", (cid,)).fetchone()
        assert still["rejected"] == 1
