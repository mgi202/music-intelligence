"""Identity-aware ingest (RC1 §S4)."""

from app.db.connection import db_conn, get_connection
from app.ingestion.ledger import upsert_track, ingest_tokens
from tests.conftest import make_token


def _count(db, table):
    conn = get_connection(db)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_new_insert_stamps_last_seen(db):
    tok = make_token(isrc="GBAAA1111111", ytm_id="vid_a")
    with db_conn(db) as conn:
        assert upsert_track(tok, conn) is True
    conn = get_connection(db)
    try:
        row = conn.execute("SELECT last_seen_at, missing_since FROM tracks").fetchone()
        assert row["last_seen_at"] is not None
        assert row["missing_since"] is None
        # alias rows registered for both identities
        keys = {r["alias_key"] for r in conn.execute("SELECT alias_key FROM track_aliases")}
        assert "isrc:GBAAA1111111" in keys
        assert "ytm:vid_a" in keys
    finally:
        conn.close()


def test_alias_resolution_updates_not_inserts(db):
    # First sighting establishes the track + isrc alias.
    tok1 = make_token(title="Song One", isrc="GBBBB2222222", ytm_id="vid1")
    # Second sighting: same ISRC, different title/duration/videoId → must
    # resolve to the existing track via the isrc alias (update, not insert).
    tok2 = make_token(title="Totally Different", duration_ms=99999,
                      isrc="GBBBB2222222", ytm_id="vid2")
    with db_conn(db) as conn:
        assert upsert_track(tok1, conn) is True
        assert upsert_track(tok2, conn) is False
    assert _count(db, "tracks") == 1
    # The new videoId alias now also points at the same track.
    conn = get_connection(db)
    try:
        keys = {r["alias_key"] for r in conn.execute("SELECT alias_key FROM track_aliases")}
        assert "ytm:vid1" in keys and "ytm:vid2" in keys
    finally:
        conn.close()


def test_fuzzy_creates_review_not_merge(db):
    a = make_token(title="Drift", artist="Nuit", duration_ms=200000)
    b = make_token(title="Drift", artist="Nuit", duration_ms=201000)  # within 2000ms
    assert a.track_pk != b.track_pk
    with db_conn(db) as conn:
        assert upsert_track(a, conn) is True
        assert upsert_track(b, conn) is True  # inserted as NEW, not merged
    assert _count(db, "tracks") == 2  # never auto-merged
    conn = get_connection(db)
    try:
        rows = conn.execute(
            "SELECT * FROM dedup_review WHERE status='pending'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["reason"] == "ingest_fuzzy"
    finally:
        conn.close()


def test_far_duration_is_not_fuzzy(db):
    a = make_token(title="Drift", artist="Nuit", duration_ms=200000)
    b = make_token(title="Drift", artist="Nuit", duration_ms=210000)  # 10s apart
    with db_conn(db) as conn:
        upsert_track(a, conn)
        upsert_track(b, conn)
    assert _count(db, "tracks") == 2
    assert _count(db, "dedup_review") == 0


def test_missing_since_cleared_on_reappearance(db):
    tok = make_token(isrc="GBCCC3333333", ytm_id="vidc")
    with db_conn(db) as conn:
        upsert_track(tok, conn)
        conn.execute("UPDATE tracks SET missing_since = '2020-01-01T00:00:00'")
    # Re-ingest the same track → missing_since must clear, last_seen refresh.
    with db_conn(db) as conn:
        upsert_track(tok, conn)
    conn = get_connection(db)
    try:
        row = conn.execute("SELECT missing_since, last_seen_at FROM tracks").fetchone()
        assert row["missing_since"] is None
        assert row["last_seen_at"] is not None
    finally:
        conn.close()


def test_ingest_tokens_counts(db):
    toks = [
        make_token(title="A", isrc="GBDDD0000001"),
        make_token(title="B", isrc="GBDDD0000002"),
        make_token(title="A", isrc="GBDDD0000001"),  # dup of first
    ]
    new, updated = ingest_tokens(toks)
    # ingest_tokens uses the default (patched) DB path.
    assert new == 2 and updated == 1
