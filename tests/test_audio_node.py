"""Phase 3 — compute-node claim/result API (app/audio/node_api.py).

Server-side tests mock the Mac node by posting canned payloads; the heavy ML
stack is never imported. Qdrant is monkeypatched (VectorStoreError / no-op).
Proves the Live-Proof gates: unknown-basis never claimed (LP1), features +
flags + status written (LP3), model bump marks stale without reprocessing
(LP7).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.db.connection import db_conn, get_connection
from tests.conftest import insert_track

TOKEN = {"X-Audio-Node-Token": "sekrit"}
VEC = [0.1] * 512


def _client():
    from app.api.server import app
    return TestClient(app)


def _seed_candidate(db, pk="t1", basis="official_preview", conf=0.95,
                    status="lawful_audio_candidate", claimed_at=None, **cols):
    conn = get_connection(db)
    if not conn.execute("SELECT 1 FROM tracks WHERE track_pk=?", (pk,)).fetchone():
        insert_track(conn, pk, match_status=status,
                     canonical_title="Warehouse Days",
                     canonical_artist="Deep Artist", **cols)
    cur = conn.execute(
        """INSERT INTO audio_source_candidates
               (track_pk, source_type, source_platform, source_url,
                confidence, lawful_basis, claimed_at)
           VALUES (?, 'official_preview', 'itunes', ?, ?, ?, ?)""",
        (pk, f"https://a.example/{pk}-{basis}.m4a", conf, basis, claimed_at),
    )
    cid = cur.lastrowid
    conn.commit(); conn.close()
    return cid


def _quiet_qdrant(monkeypatch, fail=False):
    from app.audio import vectors
    calls = []
    def fake_upsert(pk, vec, payload):
        if fail:
            raise vectors.VectorStoreError("qdrant down")
        calls.append((pk, len(vec), payload))
    monkeypatch.setattr(vectors, "upsert_track", fake_upsert)
    return calls


# ── Auth ──────────────────────────────────────────────────────────────────────

def test_claim_refuses_without_configured_token(db, monkeypatch):
    monkeypatch.delenv("AUDIO_NODE_TOKEN", raising=False)
    r = _client().post("/api/audio/claim", json={"batch": 4}, headers=TOKEN)
    assert r.status_code == 503


def test_claim_refuses_bad_token(db, monkeypatch):
    monkeypatch.setenv("AUDIO_NODE_TOKEN", "sekrit")
    r = _client().post("/api/audio/claim", json={"batch": 4},
                       headers={"X-Audio-Node-Token": "wrong"})
    assert r.status_code == 401


# ── LP1: the lawful gate at claim time ────────────────────────────────────────

def test_unknown_basis_is_never_claimed(db, monkeypatch):
    monkeypatch.setenv("AUDIO_NODE_TOKEN", "sekrit")
    # A track whose ONLY candidate has unknown basis — however confident.
    _seed_candidate(db, pk="dirty", basis="unknown", conf=0.99)
    r = _client().post("/api/audio/claim", json={"batch": 8}, headers=TOKEN)
    assert r.status_code == 200
    assert r.json()["jobs"] == []


def test_claim_picks_lawful_candidate_only(db, monkeypatch):
    monkeypatch.setenv("AUDIO_NODE_TOKEN", "sekrit")
    _seed_candidate(db, pk="t1", basis="unknown", conf=0.99)
    _seed_candidate(db, pk="t1", basis="official_preview", conf=0.93)
    jobs = _client().post("/api/audio/claim", json={"batch": 8},
                          headers=TOKEN).json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["lawful_basis"] == "official_preview"


def test_claim_leases_and_requeues_after_expiry(db, monkeypatch):
    monkeypatch.setenv("AUDIO_NODE_TOKEN", "sekrit")
    cid = _seed_candidate(db)
    cl = _client()
    assert len(cl.post("/api/audio/claim", json={"batch": 4},
                       headers=TOKEN).json()["jobs"]) == 1
    # Still leased → nothing to claim.
    assert cl.post("/api/audio/claim", json={"batch": 4},
                   headers=TOKEN).json()["jobs"] == []
    # Expire the lease (dead node) → re-queued.
    with db_conn(db) as conn:
        conn.execute("UPDATE audio_source_candidates SET claimed_at = "
                     "'2020-01-01T00:00:00+00:00' WHERE candidate_id = ?", (cid,))
    assert len(cl.post("/api/audio/claim", json={"batch": 4},
                       headers=TOKEN).json()["jobs"]) == 1


# ── LP3: result ingestion ─────────────────────────────────────────────────────

def _post_result(cl, cid, vec=VEC, clap_ver="clap-v1", status="ok"):
    return cl.post("/api/audio/result", headers=TOKEN, json={
        "candidate_id": cid, "status": status,
        "features": {"bpm": 128.0, "energy": 0.8, "valence": 0.3,
                     "danceability": 0.7},
        "camelot_key": "8A",
        "clap_vector": vec,
        "model_versions": {"essentia": "2.1", "keyfinder": "cli",
                           "clap": clap_ver, "extractor": "test/1"},
    })


def test_result_writes_features_flags_and_status(db, monkeypatch):
    monkeypatch.setenv("AUDIO_NODE_TOKEN", "sekrit")
    calls = _quiet_qdrant(monkeypatch)
    cid = _seed_candidate(db)
    r = _post_result(_client(), cid)
    assert r.status_code == 200
    assert r.json()["match_status"] == "audio_enriched"
    conn = get_connection(db)
    af = conn.execute("SELECT * FROM audio_features WHERE track_pk='t1'").fetchone()
    es = conn.execute("SELECT * FROM enrichment_state WHERE track_pk='t1'").fetchone()
    t = conn.execute("SELECT match_status FROM tracks WHERE track_pk='t1'").fetchone()
    c = conn.execute("SELECT claimed_at FROM audio_source_candidates "
                     "WHERE candidate_id=?", (cid,)).fetchone()
    conn.close()
    assert af["bpm"] == 128.0 and af["camelot_key"] == "8A"
    assert af["clap_vector_json"] is not None          # rebuildable store
    assert af["feature_model_status"] == "current"
    assert es["has_audio_features"] == 1 and es["has_clap_vector"] == 1
    assert t["match_status"] == "audio_enriched"
    assert c["claimed_at"] is None                     # lease released
    assert calls and calls[0][1] == 512                # Qdrant point upserted


def test_result_rejects_wrong_vector_length(db, monkeypatch):
    monkeypatch.setenv("AUDIO_NODE_TOKEN", "sekrit")
    cid = _seed_candidate(db)
    r = _post_result(_client(), cid, vec=[0.1] * 100)
    assert r.status_code == 400


def test_result_refuses_unknown_basis_candidate(db, monkeypatch):
    monkeypatch.setenv("AUDIO_NODE_TOKEN", "sekrit")
    cid = _seed_candidate(db, pk="dirty", basis="unknown")
    r = _post_result(_client(), cid)
    assert r.status_code == 400


def test_qdrant_outage_degrades_to_vector_failed(db, monkeypatch):
    monkeypatch.setenv("AUDIO_NODE_TOKEN", "sekrit")
    _quiet_qdrant(monkeypatch, fail=True)
    cid = _seed_candidate(db)
    r = _post_result(_client(), cid)
    assert r.json()["match_status"] == "vector_failed"
    conn = get_connection(db)
    af = conn.execute("SELECT clap_vector_json FROM audio_features "
                      "WHERE track_pk='t1'").fetchone()
    conn.close()
    assert af["clap_vector_json"] is not None  # still rebuildable from SQLite


def test_node_failure_marks_feature_failed(db, monkeypatch):
    monkeypatch.setenv("AUDIO_NODE_TOKEN", "sekrit")
    cid = _seed_candidate(db)
    r = _client().post("/api/audio/result", headers=TOKEN, json={
        "candidate_id": cid, "status": "failed", "error": "yt-dlp: no formats"})
    assert r.json()["match_status"] == "feature_failed"


# ── LP7: stale-first model policy ─────────────────────────────────────────────

def test_model_bump_marks_stale_without_reprocess(db, monkeypatch):
    monkeypatch.setenv("AUDIO_NODE_TOKEN", "sekrit")
    _quiet_qdrant(monkeypatch)
    cl = _client()
    cid1 = _seed_candidate(db, pk="old")
    _post_result(cl, cid1, clap_ver="clap-v1")
    cid2 = _seed_candidate(db, pk="new")
    _post_result(cl, cid2, clap_ver="clap-v2")
    conn = get_connection(db)
    old = conn.execute("SELECT feature_model_status, stale_reason FROM "
                       "audio_features WHERE track_pk='old'").fetchone()
    new = conn.execute("SELECT feature_model_status FROM audio_features "
                       "WHERE track_pk='new'").fetchone()
    conn.close()
    assert old["feature_model_status"] == "stale"
    assert "clap-v2" in old["stale_reason"]
    assert new["feature_model_status"] == "current"
    # No auto-reprocess: a FRESH claim hands out nothing (both enriched, no
    # lawful_audio_candidate left) — stale rows wait for the explicit mode.
    assert cl.post("/api/audio/claim", json={"batch": 8},
                   headers=TOKEN).json()["jobs"] == []
    # The explicit reprocess claim serves the stale track.
    jobs = cl.post("/api/audio/claim", json={"batch": 8, "reprocess": True},
                   headers=TOKEN).json()["jobs"]
    assert [j["track_pk"] for j in jobs] == ["old"]


# ── Prompt embeddings round-trip ──────────────────────────────────────────────

def test_prompt_embeddings_store_and_load(db, monkeypatch):
    monkeypatch.setenv("AUDIO_NODE_TOKEN", "sekrit")
    cl = _client()
    r = cl.post("/api/audio/prompt-embeddings", headers=TOKEN, json={
        "model_version": "clap-v1",
        "embeddings": [
            {"profile_id": "techno", "name": "techno", "kind": "positive",
             "query_text": "driving machine techno", "vector": VEC},
            {"profile_id": "techno", "name": "techno", "kind": "negative",
             "query_text": "acoustic folk", "vector": VEC},
        ],
    })
    assert r.status_code == 200 and r.json()["stored"] == 2
    from app.audio.node_api import load_prompt_embedding
    assert load_prompt_embedding("techno", "positive", db) == VEC
    assert load_prompt_embedding("techno", "missing-kind", db) is None
    # Bad vector length → 400.
    r = cl.post("/api/audio/prompt-embeddings", headers=TOKEN, json={
        "embeddings": [{"profile_id": "x", "kind": "positive",
                        "vector": [1.0, 2.0]}]})
    assert r.status_code == 400


def test_audio_prompts_listing_requires_token_and_returns_seeded(db, monkeypatch):
    monkeypatch.setenv("AUDIO_NODE_TOKEN", "sekrit")
    cl = _client()
    assert cl.get("/api/audio/prompts").status_code == 401
    profiles = cl.get("/api/audio/prompts", headers=TOKEN).json()["profiles"]
    ids = {p["profile_id"] for p in profiles}
    assert "techno" in ids and "peak-time" in ids  # prompt seed ran in init_db
