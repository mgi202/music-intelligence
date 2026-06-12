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
