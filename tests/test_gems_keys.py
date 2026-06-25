"""Forgotten-gems eligibility keys + YTM history seeding (RC2 §T6)."""

import json
from datetime import datetime, timezone

from app.db.connection import db_conn, get_connection
from app.playlists.compiler import compile_playlist
from app.enrichment.listens_import import import_ytm_history
from tests.conftest import insert_track, iso_days_ago


def _now_ts():
    return int(datetime.now(timezone.utc).timestamp())


def _rule(conn, rule_id, eligibility):
    conn.execute(
        "INSERT INTO playlist_rules (rule_id, playlist_name, target_platform, rule_json, "
        "ranking_mode, enabled) VALUES (?, ?, 'ytm', ?, 'mood', 1)",
        (rule_id, rule_id, json.dumps({"eligibility": eligibility}),),
    )


def _tag(conn, pk):
    conn.execute("INSERT INTO track_tags (track_pk, tag, tag_type, source, confidence) "
                 "VALUES (?, 'x', 'public', 'lastfm', 0.9)", (pk,))


def test_last_played_before_days_includes_zero_listens(db):
    now = _now_ts()
    with db_conn(db) as conn:
        for pk in ("zero", "recent", "old"):
            insert_track(conn, pk)
            _tag(conn, pk)
        conn.execute("INSERT INTO listens (listened_at, track_pk, recording_msid, source) "
                     "VALUES (?, 'recent', 'r1', 'listenbrainz')", (now,))
        conn.execute("INSERT INTO listens (listened_at, track_pk, recording_msid, source) "
                     "VALUES (?, 'old', 'r2', 'listenbrainz')", (now - 100 * 86400,))
        _rule(conn, "gems", {"tags_any": ["x"], "last_played_before_days": 90})
    pks = set(compile_playlist("gems", db_path=db))
    assert "zero" in pks   # zero-listen tracks match
    assert "old" in pks    # last listen > 90d ago
    assert "recent" not in pks


def test_never_playlisted(db):
    with db_conn(db) as conn:
        insert_track(conn, "never"); _tag(conn, "never")
        insert_track(conn, "played"); _tag(conn, "played")
        conn.execute("INSERT INTO playlist_rules (rule_id, playlist_name, target_platform, rule_json) "
                     "VALUES ('hist','H','ytm','{}')")
        conn.execute("INSERT INTO playlist_snapshots (rule_id, reason, track_pks_json, video_ids_json) "
                     "VALUES ('hist','pre_sync', ?, '[]')", (json.dumps(["played"]),))
        _rule(conn, "np", {"tags_any": ["x"], "never_playlisted": True})
    pks = set(compile_playlist("np", db_path=db))
    assert pks == {"never"}


def test_added_before_days(db):
    with db_conn(db) as conn:
        insert_track(conn, "oldadd", created_at=iso_days_ago(400)); _tag(conn, "oldadd")
        insert_track(conn, "newadd", created_at=iso_days_ago(5)); _tag(conn, "newadd")
        _rule(conn, "ab", {"tags_any": ["x"], "added_before_days": 180})
    pks = set(compile_playlist("ab", db_path=db))
    assert pks == {"oldadd"}


class _HistAdapter:
    class _Client:
        def __init__(self, items): self._items = items
        def get_history(self): return self._items
    def __init__(self, items): self.client = self._Client(items)


def test_ytm_history_dedup_within_30min(db):
    now = _now_ts()
    with db_conn(db) as conn:
        insert_track(conn, "t1", ytm_track_id="v1")
        conn.execute("INSERT INTO track_aliases (alias_key, track_pk, alias_type) "
                     "VALUES ('ytm:v1','t1','ytm')")
        insert_track(conn, "t2", ytm_track_id="v2")
        conn.execute("INSERT INTO track_aliases (alias_key, track_pk, alias_type) "
                     "VALUES ('ytm:v2','t2','ytm')")
        # existing listen for t1 ~now → its history sighting must be deduped
        conn.execute("INSERT INTO listens (listened_at, track_pk, recording_msid, source) "
                     "VALUES (?, 't1', 'pre', 'listenbrainz')", (now,))
    adapter = _HistAdapter([
        {"videoId": "v1", "title": "A", "artists": [{"name": "X"}]},
        {"videoId": "v2", "title": "B", "artists": [{"name": "Y"}]},
    ])
    inserted = import_ytm_history(adapter, db_path=db)
    assert inserted == 1  # v1 deduped (within 30 min), v2 added

    conn = get_connection(db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM listens WHERE source='ytm_history'"
        ).fetchone()[0] == 1
    finally:
        conn.close()
