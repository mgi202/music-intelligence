"""Phase 3 — ranking integration: audio BOOSTS, never GATES (LP6 regression).

A metadata-only track must always rank with its audio terms neutral; a track
whose features fit the rule's boost ranges must outrank it in dj_mix.
"""

from __future__ import annotations

import json

from app.db.connection import db_conn
from app.playlists.compiler import compile_playlist, compile_playlist_detailed
from tests.conftest import insert_track


def _rule(conn, rule_id="djm", mode="dj_mix", boosts=None):
    rule = {"eligibility": {}, "audio_boosts": boosts or
            {"bpm_min": 120, "bpm_max": 132, "energy_min": 0.6, "energy_max": 1.0}}
    conn.execute(
        "INSERT INTO playlist_rules (rule_id, playlist_name, target_platform, "
        "rule_json, ranking_mode, enabled) VALUES (?, ?, 'ytm', ?, ?, 1)",
        (rule_id, "DJ Mix", json.dumps(rule), mode),
    )


def _features(conn, pk, bpm, energy, camelot="8A", vector=None):
    conn.execute(
        "INSERT INTO audio_features (track_pk, bpm, energy, camelot_key, "
        "clap_vector_json) VALUES (?, ?, ?, ?, ?)",
        (pk, bpm, energy, camelot, json.dumps(vector) if vector else None),
    )


def test_metadata_only_track_still_ranks_in_dj_mix(db):
    """THE regression guard: no audio data can never mean exclusion."""
    with db_conn(db) as conn:
        insert_track(conn, "enriched", match_status="audio_enriched")
        _features(conn, "enriched", bpm=126, energy=0.8)
        insert_track(conn, "bare")   # metadata_only, no audio_features row
        _rule(conn)
    pks = compile_playlist("djm", db_path=db)
    assert "bare" in pks                      # never dropped
    assert "enriched" in pks
    assert pks.index("enriched") < pks.index("bare")  # audio boosts ranking


def test_dj_mix_prefers_bpm_and_energy_fit(db):
    with db_conn(db) as conn:
        insert_track(conn, "fit", match_status="audio_enriched")
        _features(conn, "fit", bpm=126, energy=0.8, camelot="8A")
        insert_track(conn, "offtempo", match_status="audio_enriched")
        _features(conn, "offtempo", bpm=90, energy=0.2, camelot="8A")
        _rule(conn)
    detailed = compile_playlist_detailed("djm", db_path=db)
    scores = {d["track_pk"]: d["score"] for d in detailed}
    assert scores["fit"] > scores["offtempo"]
    comp = next(d for d in detailed if d["track_pk"] == "fit")
    assert "bpm" in comp["evidence"]["score_components"]
    assert "camelot" in comp["evidence"]["score_components"]


def test_mood_mode_keeps_metadata_only_tracks(db):
    with db_conn(db) as conn:
        insert_track(conn, "a", personal_rating=4)
        insert_track(conn, "b")
        conn.execute(
            "INSERT INTO playlist_rules (rule_id, playlist_name, target_platform, "
            "rule_json, ranking_mode, enabled) VALUES ('m', 'Mood', 'ytm', ?, 'mood', 1)",
            (json.dumps({"eligibility": {}}),),
        )
    pks = compile_playlist("m", db_path=db)
    assert set(pks) == {"a", "b"}


def test_vector_component_uses_profile_prompt_embedding(db):
    """A stored positive-prompt embedding steers the dj_mix vector term."""
    vec_on = [1.0] + [0.0] * 511
    vec_off = [0.0, 1.0] + [0.0] * 510
    with db_conn(db) as conn:
        insert_track(conn, "on", match_status="audio_enriched")
        _features(conn, "on", bpm=126, energy=0.8, vector=vec_on)
        insert_track(conn, "off", match_status="audio_enriched")
        _features(conn, "off", bpm=126, energy=0.8, vector=vec_off)
        conn.execute(
            "INSERT INTO track_tags (track_pk, tag, tag_type, source, confidence) "
            "VALUES ('on', 'techno', 'private_manual', 'manual', 1.0)")
        conn.execute(
            "INSERT INTO track_tags (track_pk, tag, tag_type, source, confidence) "
            "VALUES ('off', 'techno', 'private_manual', 'manual', 1.0)")
        conn.execute(
            "INSERT INTO vector_query_profiles (profile_id, name, query_text, "
            "embedding_json) VALUES ('techno::positive', 'techno', 'p', ?)",
            (json.dumps(vec_on),),
        )
        rule = {"eligibility": {"tags_any": ["techno"]},
                "audio_boosts": {"bpm_min": 120, "bpm_max": 132}}
        conn.execute(
            "INSERT INTO playlist_rules (rule_id, playlist_name, target_platform, "
            "rule_json, ranking_mode, enabled) VALUES ('v', 'V', 'ytm', ?, 'dj_mix', 1)",
            (json.dumps(rule),),
        )
    detailed = compile_playlist_detailed("v", db_path=db)
    scores = {d["track_pk"]: d["score"] for d in detailed}
    assert scores["on"] > scores["off"]


def test_vectors_helpers_roundtrip(db):
    """point_id determinism + SQLite vector load + cosine sanity."""
    from app.audio import vectors
    assert vectors.point_id("pk:x") == vectors.point_id("pk:x")
    assert vectors.point_id("pk:x") != vectors.point_id("pk:y")
    assert abs(vectors.cosine([1, 0], [1, 0]) - 1.0) < 1e-9
    assert abs(vectors.cosine([1, 0], [0, 1])) < 1e-9
    vec = [0.5] * 512
    with db_conn(db) as conn:
        insert_track(conn, "t")
        conn.execute(
            "INSERT INTO audio_features (track_pk, clap_vector_json) VALUES (?, ?)",
            ("t", json.dumps(vec)),
        )
    assert vectors.load_vector("t", db) == vec
    assert vectors.load_vectors(["t", "missing"], db) == {"t": vec}
