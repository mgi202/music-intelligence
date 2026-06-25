"""Playlist snapshots + restore + retention (RC2 §T2)."""

import json

from app.db.connection import db_conn, get_connection
from app.playlists.sync import sync_playlist, restore_snapshot
from app.observability import prune_playlist_snapshots
from tests.conftest import insert_track, iso_days_ago, FakeAdapter


def _rule_with_tracks(db, n, rule_id="r", target_playlist_id="PL1"):
    with db_conn(db) as conn:
        conn.execute(
            """INSERT INTO playlist_rules
                   (rule_id, playlist_name, target_platform, rule_json,
                    ranking_mode, enabled, target_playlist_id)
               VALUES (?, 'P', 'ytm', ?, 'utility', 1, ?)""",
            (rule_id, json.dumps({"eligibility": {"tags_any": ["x"]}}), target_playlist_id),
        )
        for i in range(n):
            pk = f"t{i:02d}"
            insert_track(conn, pk, created_at=f"2020-01-{i+1:02d}T00:00:00")
            conn.execute(
                "INSERT INTO track_tags (track_pk, tag, tag_type, source, confidence) "
                "VALUES (?, 'x', 'public', 'lastfm', 0.9)", (pk,))


def _snap_count(db, reason=None):
    conn = get_connection(db)
    try:
        if reason:
            return conn.execute(
                "SELECT COUNT(*) FROM playlist_snapshots WHERE reason=?", (reason,)
            ).fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM playlist_snapshots").fetchone()[0]
    finally:
        conn.close()


def test_snapshot_on_change_not_on_noop(db):
    _rule_with_tracks(db, 3)
    ad = FakeAdapter(existing=[])
    r1 = sync_playlist("r", ad, db_path=db)
    assert r1["synced"] is True
    assert _snap_count(db, "pre_sync") == 1
    # identical re-sync → hash short-circuit, no new snapshot
    r2 = sync_playlist("r", ad, db_path=db)
    assert r2.get("skipped") == "unchanged"
    assert _snap_count(db, "pre_sync") == 1


def test_restore_pushes_correct_order(db):
    _rule_with_tracks(db, 0)  # rule only
    with db_conn(db) as conn:
        conn.execute(
            "INSERT INTO playlist_snapshots (rule_id, reason, track_pks_json, video_ids_json) "
            "VALUES ('r', 'pre_sync', '[]', ?)",
            (json.dumps(["v3", "v2", "v1"]),),
        )
        sid = conn.execute("SELECT snapshot_id FROM playlist_snapshots").fetchone()[0]
    ad = FakeAdapter(existing=["v1", "v2", "v3"])
    out = restore_snapshot(sid, ad, db_path=db)
    assert out["restored"] is True
    assert ad.writes[-1] == ["v3", "v2", "v1"]      # restored in snapshot order
    assert _snap_count(db, "pre_restore") == 1       # current state captured first


def test_60_day_prune(db):
    with db_conn(db) as conn:
        conn.execute("INSERT INTO playlist_rules (rule_id, playlist_name, target_platform, rule_json) "
                     "VALUES ('r','P','ytm','{}')")
        conn.execute("INSERT INTO playlist_snapshots (rule_id, taken_at, reason, track_pks_json, video_ids_json) "
                     "VALUES ('r', ?, 'pre_sync', '[]', '[]')", (iso_days_ago(70),))
        conn.execute("INSERT INTO playlist_snapshots (rule_id, taken_at, reason, track_pks_json, video_ids_json) "
                     "VALUES ('r', ?, 'pre_sync', '[]', '[]')", (iso_days_ago(20),))
    assert prune_playlist_snapshots(db_path=db) == 1
    assert _snap_count(db) == 1
