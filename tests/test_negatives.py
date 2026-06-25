"""Hard negatives + wrong-match (RC2 §T4)."""

import json

from app.db.connection import db_conn, get_connection
from app.playlists.compiler import compile_playlist
from tests.conftest import insert_track
from app.api import server


def _rule(conn, rule_id, eligibility, mode="mood"):
    conn.execute(
        "INSERT INTO playlist_rules (rule_id, playlist_name, target_platform, rule_json, "
        "ranking_mode, enabled) VALUES (?, ?, 'ytm', ?, ?, 1)",
        (rule_id, rule_id, json.dumps({"eligibility": eligibility}), mode),
    )


def _tag(conn, pk):
    conn.execute("INSERT INTO track_tags (track_pk, tag, tag_type, source, confidence) "
                 "VALUES (?, 'x', 'public', 'lastfm', 0.9)", (pk,))


def test_blocked_excluded_except_include_blocked(db):
    with db_conn(db) as conn:
        insert_track(conn, "ok")
        _tag(conn, "ok")
        insert_track(conn, "blk", blocked_from_playlists=1)
        _tag(conn, "blk")
        _rule(conn, "r_norm", {"tags_any": ["x"]})
        _rule(conn, "r_incl", {"tags_any": ["x"], "include_blocked": True})
    assert set(compile_playlist("r_norm", db_path=db)) == {"ok"}
    assert set(compile_playlist("r_incl", db_path=db)) == {"ok", "blk"}


def test_dnr_excluded_from_discovery_only(db):
    with db_conn(db) as conn:
        insert_track(conn, "dnr", do_not_recommend=1)
        _tag(conn, "dnr")
        _rule(conn, "r_mood", {"tags_any": ["x"]}, mode="mood")
        _rule(conn, "r_disc", {"tags_any": ["x"]}, mode="discovery")
    assert "dnr" in compile_playlist("r_mood", db_path=db)      # eligible elsewhere
    assert "dnr" not in compile_playlist("r_disc", db_path=db)  # not in discovery


def test_wrong_match_quarantines_and_logs(db, monkeypatch):
    from app.db import connection
    monkeypatch.setattr(connection, "_DEFAULT_DB", db)
    with db_conn(db) as conn:
        insert_track(conn, "wm", ytm_track_id="vid_bad")
        conn.execute("INSERT INTO track_aliases (alias_key, track_pk, alias_type) "
                     "VALUES ('ytm:vid_bad','wm','ytm')")

    out = server.wrong_match("wm")
    assert out["quarantined"] is True
    assert out["cleared_ytm_track_id"] == "vid_bad"

    conn = get_connection(db)
    try:
        row = conn.execute("SELECT match_status, ytm_track_id FROM tracks WHERE track_pk='wm'").fetchone()
        assert row["match_status"] == "quarantined"
        assert row["ytm_track_id"] is None
        # alias removed so the bad video can't re-resolve
        assert conn.execute("SELECT 1 FROM track_aliases WHERE alias_key='ytm:vid_bad'").fetchone() is None
        # review/audit entry written
        assert conn.execute(
            "SELECT 1 FROM processing_events WHERE event_type='wrong_match' AND track_pk='wm'"
        ).fetchone() is not None
    finally:
        conn.close()
