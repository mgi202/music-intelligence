"""Compiler evidence: "Why is this track here?" (RC2 §T9)."""

import json

from app.db.connection import db_conn
from app.playlists.compiler import compile_playlist, compile_playlist_detailed
from tests.conftest import insert_track


def _setup(db, mode="mood"):
    with db_conn(db) as conn:
        for pk, rating in [("a", 4), ("b", None), ("c", 2)]:
            insert_track(conn, pk, personal_rating=rating)
            conn.execute("INSERT INTO track_tags (track_pk, tag, tag_type, source, confidence) "
                         "VALUES (?, 'techno', 'public', 'lastfm', 0.9)", (pk,))
        conn.execute(
            "INSERT INTO playlist_rules (rule_id, playlist_name, target_platform, rule_json, "
            "ranking_mode, enabled) VALUES ('r','R','ytm',?, ?, 1)",
            (json.dumps({"eligibility": {"tags_any": ["techno"]}}), mode),
        )


def test_components_sum_to_score(db):
    _setup(db, "mood")
    for d in compile_playlist_detailed("r", db_path=db):
        comp_sum = sum(d["evidence"]["score_components"].values())
        assert abs(comp_sum - d["score"]) < 1e-6


def test_matched_reflects_rule(db):
    _setup(db, "mood")
    det = compile_playlist_detailed("r", db_path=db)
    for d in det:
        assert d["evidence"]["matched"]["tags_any"] == ["techno"]
        assert d["evidence"]["rule"]["ranking_mode"] == "mood"
        assert d["evidence"]["rule"]["rule_id"] == "r"


def test_wrapper_matches_detailed_order(db):
    _setup(db, "mood")
    det = compile_playlist_detailed("r", db_path=db)
    assert compile_playlist("r", db_path=db) == [d["track_pk"] for d in det]
    # ranks are contiguous from 0
    assert [d["rank"] for d in det] == list(range(len(det)))


def test_discovery_components_sum(db):
    _setup(db, "discovery")
    for d in compile_playlist_detailed("r", db_path=db):
        comp_sum = sum(d["evidence"]["score_components"].values())
        assert abs(comp_sum - d["score"]) < 1e-6
