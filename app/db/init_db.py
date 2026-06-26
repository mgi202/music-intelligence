"""
Database initialisation script.

Reads schema.sql and executes it against the configured SQLite database.
Safe to run multiple times — all CREATE statements use IF NOT EXISTS.

Usage:
    python app/db/init_db.py
    python app/db/init_db.py --db-path /custom/path/library.db
"""

import argparse
import sqlite3
from pathlib import Path

from app.db.connection import get_connection

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def init_db(db_path: str | None = None) -> None:
    """Execute schema.sql against the database at db_path."""
    schema = SCHEMA_PATH.read_text()

    conn = get_connection(db_path)
    try:
        # executescript handles multi-statement SQL with comments correctly.
        # It auto-commits, which is fine for schema DDL.
        # Pragmas that are session-state (foreign_keys, journal_mode) are set
        # by get_connection() on every new connection anyway.
        # Migrations must run BEFORE the schema script: schema.sql may
        # reference columns (e.g. in CREATE INDEX) that pre-existing tables
        # don't have yet, since CREATE TABLE IF NOT EXISTS won't add them.
        _run_migrations(conn)
        conn.executescript(schema)
        # Backfills run AFTER schema.sql so the v2 tables they touch exist on
        # both fresh and upgraded databases. All backfills are idempotent.
        _run_backfills(conn)
        conn.commit()
        print(f"Database initialised: {db_path or 'default path'}")
        _report_tables(conn)
    finally:
        conn.close()


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Idempotent in-place migrations for DBs created before a schema change.

    CREATE TABLE IF NOT EXISTS does not alter existing tables, so columns
    added to schema.sql after first init must be applied here too.
    No-op on a fresh database (tables don't exist yet — schema.sql creates
    them in their final form).
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)")}
    if not existing:
        return  # fresh DB — schema.sql will create everything current

    # 2026-06-12: personal track ratings (1=like .. 4=perfect/moves me)
    if "personal_rating" not in existing:
        conn.execute(
            "ALTER TABLE tracks ADD COLUMN personal_rating INTEGER "
            "CHECK (personal_rating BETWEEN 1 AND 4)"
        )
        conn.execute("ALTER TABLE tracks ADD COLUMN rated_at TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_rating ON tracks(personal_rating)")
        print("Migration applied: tracks.personal_rating + tracks.rated_at")

    # ── Schema v2 (RC1 §S3): identity-aware ingest + takedown detection ──
    if "last_seen_at" not in existing:
        conn.execute("ALTER TABLE tracks ADD COLUMN last_seen_at TEXT")
        conn.execute("ALTER TABLE tracks ADD COLUMN missing_since TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tracks_missing_since ON tracks(missing_since)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tracks_last_seen_at ON tracks(last_seen_at)"
        )
        print("Migration applied (v2): tracks.last_seen_at + tracks.missing_since")

    # ── Schema v3 (RC2 §T1): hard negatives + inbox dismissal ──
    if "blocked_from_playlists" not in existing:
        conn.execute(
            "ALTER TABLE tracks ADD COLUMN blocked_from_playlists INTEGER NOT NULL DEFAULT 0"
        )
        conn.execute(
            "ALTER TABLE tracks ADD COLUMN do_not_recommend INTEGER NOT NULL DEFAULT 0"
        )
        print("Migration applied (v3): tracks.blocked_from_playlists + tracks.do_not_recommend")
    if "inbox_dismissed_at" not in existing:
        conn.execute("ALTER TABLE tracks ADD COLUMN inbox_dismissed_at TEXT")
        print("Migration applied (v3): tracks.inbox_dismissed_at")

    # playlist_rules: sync-safety columns (v3)
    pr_cols = {row["name"] for row in conn.execute("PRAGMA table_info(playlist_rules)")}
    if pr_cols and "last_synced_hash" not in pr_cols:
        conn.execute("ALTER TABLE playlist_rules ADD COLUMN last_synced_hash TEXT")
        conn.execute("ALTER TABLE playlist_rules ADD COLUMN sync_held_reason TEXT")
        print("Migration applied (v3): playlist_rules.last_synced_hash + sync_held_reason")

    # 2026-06-26: source-playlist write-back needs per-item ids for YTM removal.
    # The membership table may already exist (deployed 2026-06-26) without these
    # columns; CREATE TABLE IF NOT EXISTS won't add them, so ALTER here. PRAGMA on
    # a not-yet-created table returns empty → guarded skip (schema.sql makes it).
    tpm_cols = {row["name"] for row in conn.execute("PRAGMA table_info(track_playlist_membership)")}
    if tpm_cols and "set_video_id" not in tpm_cols:
        conn.execute("ALTER TABLE track_playlist_membership ADD COLUMN video_id TEXT")
        conn.execute("ALTER TABLE track_playlist_membership ADD COLUMN set_video_id TEXT")
        print("Migration applied: track_playlist_membership.video_id + set_video_id")

    # listens: source attribution (v3). Only relevant if a v2 listens table
    # already exists without the column; fresh installs get it from schema.sql.
    listens_cols = {row["name"] for row in conn.execute("PRAGMA table_info(listens)")}
    if listens_cols and "source" not in listens_cols:
        conn.execute(
            "ALTER TABLE listens ADD COLUMN source TEXT NOT NULL DEFAULT 'listenbrainz' "
            "CHECK (source IN ('listenbrainz','ytm_history'))"
        )
        print("Migration applied (v3): listens.source")

    # ── Schema v4 (2026-06-25): widen listens.source CHECK to allow 'lastfm'.
    # SQLite can't ALTER a CHECK in place, so rebuild the table. Guarded on the
    # stored DDL not already mentioning 'lastfm', so a second run (or a fresh
    # DB whose schema.sql already has it) is a no-op.
    listens_ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='listens'"
    ).fetchone()
    if listens_ddl and listens_ddl["sql"] and "'lastfm'" not in listens_ddl["sql"]:
        conn.executescript(
            """
            CREATE TABLE listens_new (
                listen_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                listened_at     INTEGER NOT NULL,
                track_pk        TEXT,
                recording_msid  TEXT, recording_mbid TEXT,
                track_name      TEXT, artist_name TEXT,
                raw_json        TEXT,
                source          TEXT NOT NULL DEFAULT 'listenbrainz'
                    CHECK (source IN ('listenbrainz','ytm_history','lastfm')),
                created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(listened_at, recording_msid)
            );
            INSERT INTO listens_new
                (listen_id, listened_at, track_pk, recording_msid, recording_mbid,
                 track_name, artist_name, raw_json, source, created_at)
            SELECT
                listen_id, listened_at, track_pk, recording_msid, recording_mbid,
                track_name, artist_name, raw_json, source, created_at
            FROM listens;
            DROP TABLE listens;
            ALTER TABLE listens_new RENAME TO listens;
            CREATE INDEX IF NOT EXISTS idx_listens_track ON listens(track_pk);
            """
        )
        print("Migration applied (v4): listens.source widened to allow 'lastfm'")


def _run_backfills(conn: sqlite3.Connection) -> None:
    """Idempotent data backfills, run after schema.sql.

    v2 (RC1 §S3): register identity aliases for every existing track from its
    ytm_track_id and isrc columns. INSERT OR IGNORE makes this safe to re-run
    and a no-op on a fresh database (no tracks yet).
    """
    rows = conn.execute(
        "SELECT track_pk, ytm_track_id, isrc FROM tracks "
        "WHERE ytm_track_id IS NOT NULL OR isrc IS NOT NULL"
    ).fetchall()
    for r in rows:
        if r["ytm_track_id"]:
            conn.execute(
                "INSERT OR IGNORE INTO track_aliases (alias_key, track_pk, alias_type) "
                "VALUES (?, ?, 'ytm')",
                (f"ytm:{r['ytm_track_id']}", r["track_pk"]),
            )
        if r["isrc"]:
            conn.execute(
                "INSERT OR IGNORE INTO track_aliases (alias_key, track_pk, alias_type) "
                "VALUES (?, ?, 'isrc')",
                (f"isrc:{r['isrc'].upper().strip()}", r["track_pk"]),
            )


def _report_tables(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    print(f"Tables ({len(rows)}):")
    for row in rows:
        count = conn.execute(f"SELECT COUNT(*) FROM {row['name']}").fetchone()[0]
        print(f"  {row['name']:40s}  {count} rows")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Initialise the Music Intelligence SQLite database.")
    parser.add_argument("--db-path", default=None, help="Override SQLITE_PATH from .env")
    args = parser.parse_args()
    init_db(args.db_path)
