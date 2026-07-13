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


# ── Locked measurement set (2026-07-13) ──────────────────────────────────────

EXTENDED_PAYLOAD = {
    "features": {"bpm": 128.0, "energy": 0.8, "valence": 0.31, "arousal": 0.7,
                 "acousticness": 0.05, "instrumentalness": 0.92,
                 "danceability": 0.7, "onset_rate": 3.4, "key_strength": 0.81,
                 "dissonance": 0.44, "spectral_centroid": 1834.2,
                 "approachability": 0.62, "engagement": 0.58},
    "beat_positions": [0.5, 0.97, 1.44],
    "chords": {"segments": [{"chord": "Am", "start": 0.0, "end": 7.4}],
               "summary": ["Am", "F"]},
    "hpcp": [0.1] * 12,
    "predictions": {
        "genre": {"Electronic---Techno": 0.55, "Electronic---House": 0.18,
                  "Rock---Indie Rock": 0.02},
        "moodtheme": {"dark": 0.35, "hypnotic": 0.22, "driving": 0.16,
                      "summer": 0.04},
        "mood": {"aggressive": 0.71, "happy": 0.12},
        "instrument": {"synthesizer": 0.66, "drums": 0.3, "violin": 0.01},
    },
    "structure": {"intro_seconds": 14.5, "outro_seconds": 28.0,
                  "breakdown_count": 2, "first_drop_seconds": 61.0,
                  "peak_energy_position": 0.64, "energy_stability": 0.72,
                  "energy_slope_signed": 0.18, "energy_rise_score": 0.4,
                  "energy_drop_score": 0.35, "beat_grid_confidence": 0.8,
                  "structure_confidence": 0.5},
}


def _post_extended(cl, cid, essentia_ver="effnet-bs64-1+heads-1"):
    return cl.post("/api/audio/result", headers=TOKEN, json={
        "candidate_id": cid, "status": "ok", "camelot_key": "8A",
        "clap_vector": VEC,
        "model_versions": {"essentia": essentia_ver, "clap": "clap-v1",
                           "extractor": "test/2"},
        **EXTENDED_PAYLOAD,
    })


def test_extended_result_writes_scalars_jsons_and_structure(db, monkeypatch):
    monkeypatch.setenv("AUDIO_NODE_TOKEN", "sekrit")
    _quiet_qdrant(monkeypatch)
    cid = _seed_candidate(db)
    r = _post_extended(_client(), cid)
    assert r.status_code == 200
    conn = get_connection(db)
    af = conn.execute("SELECT * FROM audio_features WHERE track_pk='t1'").fetchone()
    st = conn.execute("SELECT * FROM track_structure WHERE track_pk='t1'").fetchone()
    conn.close()
    assert af["onset_rate"] == 3.4 and af["key_strength"] == 0.81
    assert af["dissonance"] == 0.44 and af["spectral_centroid"] == 1834.2
    assert af["approachability"] == 0.62 and af["engagement"] == 0.58
    import json as _json
    assert _json.loads(af["beat_positions_json"]) == [0.5, 0.97, 1.44]
    assert _json.loads(af["chords_json"])["summary"] == ["Am", "F"]
    assert len(_json.loads(af["hpcp_json"])) == 12
    assert "genre" in _json.loads(af["model_predictions_json"])  # audit copy
    assert st is not None
    assert st["intro_seconds"] == 14.5 and st["first_drop_seconds"] == 61.0
    assert st["breakdown_count"] == 2 and st["extractor_version"] == "test/2"


def test_predictions_become_audio_inferred_tags(db, monkeypatch):
    monkeypatch.setenv("AUDIO_NODE_TOKEN", "sekrit")
    _quiet_qdrant(monkeypatch)
    cid = _seed_candidate(db)
    r = _post_extended(_client(), cid)
    assert r.json()["tags_written"] > 0
    conn = get_connection(db)
    rows = conn.execute(
        "SELECT tag, source, confidence FROM track_tags "
        "WHERE track_pk='t1' AND tag_type='audio_inferred'").fetchall()
    conn.close()
    tags = {r["tag"]: r for r in rows}
    # Genre labels are normalised to their style part, threshold 0.15 applies.
    assert "techno" in tags and tags["techno"]["source"] == "essentia:genre"
    assert "house" in tags
    assert "indie-rock" not in tags          # 0.02 < threshold
    # Moodtheme over threshold lands; 'summer' (0.04) does not.
    assert "dark" in tags and "hypnotic" in tags and "summer" not in tags
    # Binary mood heads use the higher 0.60 threshold.
    assert "aggressive" in tags and "happy" not in tags
    # Instruments over 0.20 land.
    assert "synthesizer" in tags and "drums" in tags and "violin" not in tags
    assert tags["techno"]["confidence"] == 0.55


def test_reprocess_replaces_stale_audio_inferred_tags(db, monkeypatch):
    monkeypatch.setenv("AUDIO_NODE_TOKEN", "sekrit")
    _quiet_qdrant(monkeypatch)
    cl = _client()
    cid = _seed_candidate(db)
    _post_extended(cl, cid)
    # Second pass: model no longer predicts 'house' — the old tag must go.
    payload = {
        "candidate_id": cid, "status": "ok", "clap_vector": VEC,
        "model_versions": {"essentia": "effnet-bs64-1+heads-1",
                           "clap": "clap-v1", "extractor": "test/2"},
        "features": {"bpm": 128.0},
        "predictions": {"genre": {"Electronic---Techno": 0.61}},
    }
    cl.post("/api/audio/result", headers=TOKEN, json=payload)
    conn = get_connection(db)
    tags = {r["tag"] for r in conn.execute(
        "SELECT tag FROM track_tags WHERE track_pk='t1' "
        "AND tag_type='audio_inferred'")}
    conn.close()
    assert tags == {"techno"}


def test_audio_inferred_never_shadows_manual_tag(db, monkeypatch):
    monkeypatch.setenv("AUDIO_NODE_TOKEN", "sekrit")
    _quiet_qdrant(monkeypatch)
    cid = _seed_candidate(db)
    with db_conn(db) as conn:
        conn.execute(
            "INSERT INTO track_tags (track_pk, tag, tag_type, source, confidence) "
            "VALUES ('t1', 'techno', 'private_manual', 'manual', 1.0)")
    _post_extended(_client(), cid)
    conn = get_connection(db)
    rows = conn.execute(
        "SELECT tag_type FROM track_tags WHERE track_pk='t1' AND tag='techno'"
    ).fetchall()
    conn.close()
    assert [r["tag_type"] for r in rows] == ["private_manual"]


def test_essentia_model_bump_marks_stale(db, monkeypatch):
    monkeypatch.setenv("AUDIO_NODE_TOKEN", "sekrit")
    _quiet_qdrant(monkeypatch)
    cl = _client()
    cid1 = _seed_candidate(db, pk="old")
    _post_extended(cl, cid1, essentia_ver="effnet-bs64-1+heads-1")
    cid2 = _seed_candidate(db, pk="new")
    _post_extended(cl, cid2, essentia_ver="effnet-bs64-2+heads-2")
    conn = get_connection(db)
    old = conn.execute("SELECT feature_model_status, stale_reason FROM "
                       "audio_features WHERE track_pk='old'").fetchone()
    conn.close()
    assert old["feature_model_status"] == "stale"
    assert "essentia model version bump" in old["stale_reason"]


def test_plain_result_without_new_fields_still_works(db, monkeypatch):
    """An older node payload (no structure/predictions) must ingest as before."""
    monkeypatch.setenv("AUDIO_NODE_TOKEN", "sekrit")
    _quiet_qdrant(monkeypatch)
    cid = _seed_candidate(db)
    r = _post_result(_client(), cid)
    assert r.status_code == 200 and r.json()["tags_written"] == 0
    conn = get_connection(db)
    st = conn.execute("SELECT 1 FROM track_structure WHERE track_pk='t1'").fetchone()
    conn.close()
    assert st is None


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
