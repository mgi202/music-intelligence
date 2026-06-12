"""
Ledger writer — inserts or updates TrackTokens in the SQLite tracks table.

This is the canonical path from adapter output to database.
Deduplication: if a track_pk already exists, update the platform-specific fields
and updated_at only. Do not overwrite private tags or manual enrichment.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app.db.connection import db_conn
from app.ingestion.base import TrackToken


def upsert_track(token: TrackToken, conn: sqlite3.Connection) -> bool:
    """
    Insert or update a track in the ledger.

    Returns True if a new row was inserted, False if an existing row was updated.
    """
    now = datetime.now(timezone.utc).isoformat()

    existing = conn.execute(
        "SELECT track_pk FROM tracks WHERE track_pk = ?", (token.track_pk,)
    ).fetchone()

    if existing is None:
        conn.execute("""
            INSERT INTO tracks (
                track_pk, isrc, canonical_title, canonical_artist,
                normalized_title, normalized_artist, album_title,
                duration_ms, release_date, explicit,
                ytm_track_id, source_platform,
                match_status, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                'metadata_only', ?, ?
            )
        """, (
            token.track_pk,
            token.isrc,
            token.canonical_title,
            token.canonical_artist,
            token.normalized_title,
            token.normalized_artist,
            token.raw_album,
            token.duration_ms,
            token.release_date,
            1 if token.explicit else 0,
            token.platform_track_id if token.source_platform == "ytm" else None,
            token.source_platform,
            now,
            now,
        ))

        # Initialise enrichment_state row
        conn.execute("""
            INSERT OR IGNORE INTO enrichment_state (track_pk, updated_at)
            VALUES (?, ?)
        """, (token.track_pk, now))

        return True  # new row

    else:
        # Update platform-specific fields only — do not clobber enrichment data
        update_fields = {"updated_at": now}
        if token.source_platform == "ytm" and token.platform_track_id:
            update_fields["ytm_track_id"] = token.platform_track_id
        if token.isrc:
            update_fields["isrc"] = token.isrc

        set_clause = ", ".join(f"{k} = ?" for k in update_fields)
        values = list(update_fields.values()) + [token.track_pk]
        conn.execute(f"UPDATE tracks SET {set_clause} WHERE track_pk = ?", values)

        return False  # existing row updated


def ingest_tokens(tokens: list[TrackToken], batch_size: int = 500) -> tuple[int, int]:
    """
    Upsert a list of TrackTokens into the ledger.

    Returns (new_count, updated_count).
    """
    new_count = 0
    updated_count = 0

    with db_conn() as conn:
        for token in tokens:
            if not token.track_pk:
                continue  # Normalisation must have failed — skip
            is_new = upsert_track(token, conn)
            if is_new:
                new_count += 1
            else:
                updated_count += 1

    return new_count, updated_count
