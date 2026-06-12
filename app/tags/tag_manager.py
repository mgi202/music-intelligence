"""
Tag manager — apply, remove, list private_manual tags.

private_manual tags are the highest-trust tier. They are:
  - Never overwritten by automated classification
  - Persisted with tag_type='private_manual', source='manual'
  - Applied immediately; no confidence threshold required
  - Immutable via automation — only removable by explicit user action

This module is the single entry point for all manual tag operations.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app.db.connection import db_conn


def apply_tag(
    track_pk: str,
    tag: str,
    notes: str | None = None,
    db_path: str | None = None,
) -> bool:
    """
    Apply a private_manual tag to a track.

    Idempotent — if the tag already exists, returns False (no error).
    Returns True if a new tag was applied.
    """
    tag = tag.lower().strip()
    if not tag:
        raise ValueError("Tag cannot be empty")

    now = datetime.now(timezone.utc).isoformat()

    with db_conn(db_path) as conn:
        # Verify track exists
        if not conn.execute(
            "SELECT 1 FROM tracks WHERE track_pk = ?", (track_pk,)
        ).fetchone():
            raise ValueError(f"Track not found: {track_pk}")

        existing = conn.execute("""
            SELECT id FROM track_tags
            WHERE track_pk = ? AND tag = ? AND tag_type = 'private_manual'
        """, (track_pk, tag)).fetchone()

        if existing:
            return False  # Already applied

        conn.execute("""
            INSERT INTO track_tags (track_pk, tag, tag_type, source, confidence, evidence_json)
            VALUES (?, ?, 'private_manual', 'manual', 1.0, ?)
        """, (track_pk, tag, f'{{"notes": "{notes or ""}"}}' if notes else None))

        return True


def remove_tag(
    track_pk: str,
    tag: str,
    db_path: str | None = None,
) -> bool:
    """
    Remove a private_manual tag from a track.

    Only removes private_manual tags. Automated tags must be managed
    through the classification system, not directly.
    Returns True if a tag was removed.
    """
    tag = tag.lower().strip()

    with db_conn(db_path) as conn:
        result = conn.execute("""
            DELETE FROM track_tags
            WHERE track_pk = ? AND tag = ? AND tag_type = 'private_manual'
        """, (track_pk, tag))
        return result.rowcount > 0


def list_tags(
    track_pk: str,
    tag_type: str | None = None,
    db_path: str | None = None,
) -> list[dict]:
    """
    List all tags for a track, optionally filtered by tag_type.

    Returns a list of dicts with keys: tag, tag_type, source, confidence, created_at
    Ordered by tag priority (private_manual first) then alphabetically.
    """
    from app.db.connection import get_connection
    conn = get_connection(db_path)
    try:
        priority_order = "CASE tag_type " \
            "WHEN 'private_manual' THEN 0 " \
            "WHEN 'private_model' THEN 1 " \
            "WHEN 'audio_inferred' THEN 2 " \
            "WHEN 'context_inferred' THEN 3 " \
            "WHEN 'public' THEN 4 " \
            "ELSE 5 END"

        if tag_type:
            rows = conn.execute(f"""
                SELECT tag, tag_type, source, confidence, created_at
                FROM track_tags
                WHERE track_pk = ? AND tag_type = ?
                ORDER BY {priority_order}, tag ASC
            """, (track_pk, tag_type)).fetchall()
        else:
            rows = conn.execute(f"""
                SELECT tag, tag_type, source, confidence, created_at
                FROM track_tags
                WHERE track_pk = ?
                ORDER BY {priority_order}, tag ASC
            """, (track_pk,)).fetchall()

        return [dict(r) for r in rows]
    finally:
        conn.close()


def search_tracks_by_tag(
    tag: str,
    tag_type: str | None = None,
    db_path: str | None = None,
) -> list[dict]:
    """
    Find all tracks carrying a specific tag.

    Returns list of {track_pk, canonical_title, canonical_artist, tag_type, confidence}
    """
    from app.db.connection import get_connection
    conn = get_connection(db_path)
    try:
        tag = tag.lower().strip()
        if tag_type:
            rows = conn.execute("""
                SELECT t.track_pk, t.canonical_title, t.canonical_artist,
                       tt.tag_type, tt.confidence, tt.source
                FROM tracks t
                JOIN track_tags tt ON tt.track_pk = t.track_pk
                WHERE tt.tag = ? AND tt.tag_type = ?
                ORDER BY t.canonical_artist, t.canonical_title
            """, (tag, tag_type)).fetchall()
        else:
            rows = conn.execute("""
                SELECT t.track_pk, t.canonical_title, t.canonical_artist,
                       tt.tag_type, tt.confidence, tt.source
                FROM tracks t
                JOIN track_tags tt ON tt.track_pk = t.track_pk
                WHERE tt.tag = ?
                ORDER BY t.canonical_artist, t.canonical_title
            """, (tag,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def bulk_apply_tags(
    tags_by_track: dict[str, list[str]],
    db_path: str | None = None,
) -> int:
    """
    Apply multiple private_manual tags across multiple tracks.

    tags_by_track = {track_pk: [tag1, tag2, ...]}
    Returns total number of new tags applied.
    """
    total = 0
    for track_pk, tags in tags_by_track.items():
        for tag in tags:
            try:
                if apply_tag(track_pk, tag, db_path=db_path):
                    total += 1
            except ValueError:
                pass  # Track not found — skip
    return total
