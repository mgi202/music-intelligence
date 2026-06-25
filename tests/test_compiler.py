"""Playlist compiler eligibility (RC1 §S9 + RC2 keys)."""

import json

from app.db.connection import db_conn
from app.playlists.compiler import compile_playlist
from tests.conftest import insert_track


def _rule(conn, rule_id, eligibility, ranking_mode="utility", max_tracks=None):
    conn.execute(
        """INSERT INTO playlist_rules
               (rule_id, playlist_name, target_platform, rule_json,
                ranking_mode, enabled, max_tracks)
           VALUES (?, ?, 'ytm', ?, ?, 1, ?)""",
        (rule_id, rule_id, json.dumps({"eligibility": eligibility}), ranking_mode, max_tracks),
    )


def _tag(conn, pk, tag):
    conn.execute(
        "INSERT INTO track_tags (track_pk, tag, tag_type, source, confidence) "
        "VALUES (?, ?, 'public', 'lastfm', 0.9)",
        (pk, tag),
    )


def _setup(db):
    with db_conn(db) as conn:
        insert_track(conn, "t_techno", match_status="metadata_enriched")
        _tag(conn, "t_techno", "techno")
        _tag(conn, "t_techno", "deep")
        insert_track(conn, "t_pop", match_status="metadata_enriched")
        _tag(conn, "t_pop", "pop")
        insert_track(conn, "t_rated", match_status="metadata_enriched", personal_rating=3)
        _tag(conn, "t_rated", "techno")
        insert_track(conn, "t_quar", match_status="quarantined")
        _tag(conn, "t_quar", "techno")
        insert_track(conn, "t_missing", match_status="metadata_enriched",
                     missing_since="2024-01-01T00:00:00")
        _tag(conn, "t_missing", "techno")


def test_tags_any_excludes_quarantined_by_default(db):
    _setup(db)
    with db_conn(db) as conn:
        _rule(conn, "r_any", {"tags_any": ["techno"]})
    pks = set(compile_playlist("r_any", db_path=db))
    assert "t_techno" in pks and "t_rated" in pks and "t_missing" in pks
    assert "t_pop" not in pks
    assert "t_quar" not in pks  # quarantined excluded by default


def test_tags_all(db):
    _setup(db)
    with db_conn(db) as conn:
        _rule(conn, "r_all", {"tags_all": ["techno", "deep"]})
    pks = set(compile_playlist("r_all", db_path=db))
    assert pks == {"t_techno"}


def test_tags_none(db):
    _setup(db)
    with db_conn(db) as conn:
        _rule(conn, "r_none", {"tags_none": ["pop"]})
    pks = set(compile_playlist("r_none", db_path=db))
    assert "t_pop" not in pks
    assert "t_techno" in pks


def test_min_rating(db):
    _setup(db)
    with db_conn(db) as conn:
        _rule(conn, "r_rating", {"tags_any": ["techno"], "min_rating": 3})
    pks = set(compile_playlist("r_rating", db_path=db))
    assert pks == {"t_rated"}


def test_quarantine_explicit_include(db):
    _setup(db)
    with db_conn(db) as conn:
        _rule(conn, "r_quar", {"source_status_any": ["quarantined"]})
    pks = set(compile_playlist("r_quar", db_path=db))
    assert pks == {"t_quar"}


def test_missing_from_platform(db):
    _setup(db)
    with db_conn(db) as conn:
        _rule(conn, "r_missing", {"missing_from_platform": True})
    pks = set(compile_playlist("r_missing", db_path=db))
    assert pks == {"t_missing"}
