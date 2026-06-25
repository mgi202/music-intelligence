"""Track merge routine (RC1 §S8)."""

from app.db.connection import db_conn, get_connection
from app.ingestion.merge import merge_tracks
from tests.conftest import insert_track


def _setup_pair(db):
    """keep = older track (few fields), dup = newer with rating/tags/extra ids."""
    with db_conn(db) as conn:
        insert_track(conn, "keep", created_at="2020-01-01T00:00:00",
                     isrc=None, album_title=None, duration_ms=None)
        insert_track(conn, "dup", created_at="2021-01-01T00:00:00",
                     isrc="GBZZZ9999999", album_title="Dup Album", duration_ms=180000,
                     personal_rating=4, rated_at="2021-06-01T00:00:00",
                     ytm_track_id="vid_dup")
        # tags + alias + a listen on the dup
        conn.execute(
            "INSERT INTO track_tags (track_pk, tag, tag_type, source, confidence) "
            "VALUES ('dup','techno','public','lastfm',0.9)"
        )
        conn.execute(
            "INSERT INTO track_aliases (alias_key, track_pk, alias_type) "
            "VALUES ('ytm:vid_dup','dup','ytm')"
        )
        conn.execute(
            "INSERT INTO listens (listened_at, track_pk, recording_msid, source) "
            "VALUES (1000, 'dup', 'msid_x', 'listenbrainz')"
        )


def test_merge_migrates_and_deletes_dup(db):
    _setup_pair(db)
    with db_conn(db) as conn:
        merge_tracks("keep", "dup", conn)

    conn = get_connection(db)
    try:
        # dup row deleted
        assert conn.execute("SELECT 1 FROM tracks WHERE track_pk='dup'").fetchone() is None
        # tags moved
        tags = conn.execute("SELECT tag FROM track_tags WHERE track_pk='keep'").fetchall()
        assert any(t["tag"] == "techno" for t in tags)
        # listen reassigned
        assert conn.execute(
            "SELECT track_pk FROM listens WHERE recording_msid='msid_x'"
        ).fetchone()["track_pk"] == "keep"
        # rating inherited (keep had none)
        keep = conn.execute("SELECT * FROM tracks WHERE track_pk='keep'").fetchone()
        assert keep["personal_rating"] == 4
        # NULL-fill of scalar fields from dup
        assert keep["isrc"] == "GBZZZ9999999"
        assert keep["album_title"] == "Dup Album"
        assert keep["duration_ms"] == 180000
        assert keep["ytm_track_id"] == "vid_dup"
        # merged_pk back-reference alias + moved ytm alias point to keep
        merged = conn.execute(
            "SELECT track_pk FROM track_aliases WHERE alias_key='pk:dup'"
        ).fetchone()
        assert merged["track_pk"] == "keep"
        assert conn.execute(
            "SELECT track_pk FROM track_aliases WHERE alias_key='ytm:vid_dup'"
        ).fetchone()["track_pk"] == "keep"
        # merge event logged
        assert conn.execute(
            "SELECT 1 FROM processing_events WHERE event_type='merge' AND track_pk='keep'"
        ).fetchone() is not None
    finally:
        conn.close()


def test_merge_does_not_overwrite_nonnull(db):
    with db_conn(db) as conn:
        insert_track(conn, "keep", created_at="2020-01-01T00:00:00",
                     isrc="GBKEEP111111", personal_rating=2)
        insert_track(conn, "dup", created_at="2021-01-01T00:00:00",
                     isrc="GBDUP2222222", personal_rating=4)
        merge_tracks("keep", "dup", conn)
    conn = get_connection(db)
    try:
        keep = conn.execute("SELECT * FROM tracks WHERE track_pk='keep'").fetchone()
        assert keep["isrc"] == "GBKEEP111111"   # non-NULL not overwritten
        assert keep["personal_rating"] == 2     # keep's rating wins
    finally:
        conn.close()
