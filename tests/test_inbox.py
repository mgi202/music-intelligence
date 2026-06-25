"""Inbox definition truth table (RC2 §T5)."""

import json

from app.db.connection import db_conn
from app.playlists.compiler import compile_playlist
from tests.conftest import insert_track


def test_inbox_definition(db):
    with db_conn(db) as conn:
        # qualifies: unrated, no manual tag, not dismissed, not blocked
        insert_track(conn, "in")
        # disqualifiers, one each
        insert_track(conn, "rated", personal_rating=2)
        insert_track(conn, "dismissed", inbox_dismissed_at="2024-01-01T00:00:00")
        insert_track(conn, "blocked", blocked_from_playlists=1)
        insert_track(conn, "tagged")
        conn.execute("INSERT INTO track_tags (track_pk, tag, tag_type, source, confidence) "
                     "VALUES ('tagged','mine','private_manual','manual',1.0)")
        conn.execute(
            "INSERT INTO playlist_rules (rule_id, playlist_name, target_platform, rule_json, "
            "ranking_mode, enabled) VALUES ('inbox','Inbox','ytm',?, 'utility', 1)",
            (json.dumps({"eligibility": {"in_inbox": True}}),),
        )
    assert set(compile_playlist("inbox", db_path=db)) == {"in"}


def test_rating_removes_from_inbox(db):
    with db_conn(db) as conn:
        insert_track(conn, "a")
        conn.execute(
            "INSERT INTO playlist_rules (rule_id, playlist_name, target_platform, rule_json, "
            "ranking_mode, enabled) VALUES ('inbox','Inbox','ytm',?, 'utility', 1)",
            (json.dumps({"eligibility": {"in_inbox": True}}),),
        )
    assert compile_playlist("inbox", db_path=db) == ["a"]
    with db_conn(db) as conn:
        conn.execute("UPDATE tracks SET personal_rating=4 WHERE track_pk='a'")
    assert compile_playlist("inbox", db_path=db) == []
