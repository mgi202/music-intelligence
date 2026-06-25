"""Worker stage units: listens import, metrics snapshot, prune (RC1 §S10)."""

from app.db.connection import db_conn, get_connection
from app.enrichment.listens_import import import_listenbrainz_listens
from app.observability import (
    snapshot_metrics,
    prune_processing_events,
    prune_playlist_snapshots,
)
from tests.conftest import insert_track, iso_days_ago


def _page(listens):
    return {"payload": {"listens": listens}}


def test_listens_import_is_idempotent(db, monkeypatch):
    monkeypatch.setenv("LISTENBRAINZ_USER", "tester")
    listens = [
        {"listened_at": 1000, "recording_msid": "m1",
         "track_metadata": {"track_name": "A", "artist_name": "X"}},
        {"listened_at": 2000, "recording_msid": "m2",
         "track_metadata": {"track_name": "B", "artist_name": "Y"}},
    ]
    fetch = lambda url, params, headers: _page(listens)  # noqa: E731

    n1 = import_listenbrainz_listens("tester", db_path=db, _fetch=fetch)
    n2 = import_listenbrainz_listens("tester", db_path=db, _fetch=fetch)
    assert n1 == 2
    assert n2 == 0  # UNIQUE(listened_at, recording_msid) dedups the re-import

    conn = get_connection(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM listens").fetchone()[0] == 2
    finally:
        conn.close()


def test_metrics_snapshot_writes_row(db):
    with db_conn(db) as conn:
        insert_track(conn, "p1", isrc="GB1", personal_rating=4)
        insert_track(conn, "p2")
        conn.execute(
            "INSERT INTO listens (listened_at, recording_msid, source) "
            "VALUES (1, 'z', 'listenbrainz')"
        )
    m = snapshot_metrics(db_path=db)
    assert m["total_tracks"] == 2
    assert m["rated"] == 1
    assert m["with_isrc"] == 1
    assert m["listens_total"] == 1

    conn = get_connection(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM metrics_snapshots").fetchone()[0] == 1
    finally:
        conn.close()


def test_prune_processing_events_boundary(db):
    with db_conn(db) as conn:
        insert_track(conn, "t1")
        # old non-error (deleted), old error (kept), recent (kept)
        conn.execute(
            "INSERT INTO processing_events (track_pk, event_type, status, created_at) "
            "VALUES ('t1','x','ok',?)", (iso_days_ago(120),))
        conn.execute(
            "INSERT INTO processing_events (track_pk, event_type, status, created_at) "
            "VALUES ('t1','x','error',?)", (iso_days_ago(120),))
        conn.execute(
            "INSERT INTO processing_events (track_pk, event_type, status, created_at) "
            "VALUES ('t1','x','ok',?)", (iso_days_ago(10),))

    deleted = prune_processing_events(db_path=db)
    assert deleted == 1

    conn = get_connection(db)
    try:
        remaining = conn.execute("SELECT status, created_at FROM processing_events").fetchall()
        statuses = sorted(r["status"] for r in remaining)
        assert statuses == ["error", "ok"]  # old error + recent ok survive
    finally:
        conn.close()


def test_prune_snapshots_boundary(db):
    with db_conn(db) as conn:
        conn.execute(
            "INSERT INTO playlist_rules (rule_id, playlist_name, target_platform, rule_json) "
            "VALUES ('r1','R','ytm','{}')"
        )
        conn.execute(
            "INSERT INTO playlist_snapshots (rule_id, taken_at, reason, track_pks_json, video_ids_json) "
            "VALUES ('r1', ?, 'pre_sync', '[]', '[]')", (iso_days_ago(70),))
        conn.execute(
            "INSERT INTO playlist_snapshots (rule_id, taken_at, reason, track_pks_json, video_ids_json) "
            "VALUES ('r1', ?, 'pre_sync', '[]', '[]')", (iso_days_ago(10),))
    deleted = prune_playlist_snapshots(db_path=db)
    assert deleted == 1
