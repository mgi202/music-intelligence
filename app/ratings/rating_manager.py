"""
Rating manager — set, clear, and query personal track ratings.

Personal ratings are the single highest-trust signal in the system,
above even private_manual tags:

    1 = like a bit
    2 = really like
    3 = love
    4 = perfect / moves me
    NULL = unrated (absence of a rating is NOT a judgement)

Rules:
  - Only explicit user action may write a rating. No automated process
    (classification, enrichment, sync) may ever call set_rating().
  - Every rating change is logged to processing_events for history,
    so taste drift can be analysed later (vision item 2).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.db.connection import db_conn, get_connection

RATING_LABELS = {1: "like", 2: "really like", 3: "love", 4: "perfect"}


def set_rating(track_pk: str, rating: int, db_path: str | None = None) -> dict:
    """
    Set a personal rating (1-4) on a track. Idempotent.

    Returns {track_pk, rating, previous_rating}.
    Raises ValueError on unknown track or out-of-range rating.
    """
    if rating not in RATING_LABELS:
        raise ValueError(f"Rating must be 1-4, got {rating}")

    now = datetime.now(timezone.utc).isoformat()

    with db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT personal_rating FROM tracks WHERE track_pk = ?", (track_pk,)
        ).fetchone()
        if not row:
            raise ValueError(f"Track not found: {track_pk}")

        previous = row["personal_rating"]
        conn.execute(
            "UPDATE tracks SET personal_rating = ?, rated_at = ?, updated_at = ? WHERE track_pk = ?",
            (rating, now, now, track_pk),
        )
        conn.execute(
            """INSERT INTO processing_events (track_pk, event_type, status, message, payload_json)
               VALUES (?, 'rating_set', 'ok', ?, ?)""",
            (
                track_pk,
                f"Rating set to {rating} ({RATING_LABELS[rating]})",
                json.dumps({"previous": previous, "new": rating}),
            ),
        )
        return {"track_pk": track_pk, "rating": rating, "previous_rating": previous}


def clear_rating(track_pk: str, db_path: str | None = None) -> bool:
    """Remove a rating (back to unrated). Returns True if one was cleared."""
    now = datetime.now(timezone.utc).isoformat()

    with db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT personal_rating FROM tracks WHERE track_pk = ?", (track_pk,)
        ).fetchone()
        if not row:
            raise ValueError(f"Track not found: {track_pk}")
        if row["personal_rating"] is None:
            return False

        conn.execute(
            "UPDATE tracks SET personal_rating = NULL, rated_at = NULL, updated_at = ? WHERE track_pk = ?",
            (now, track_pk),
        )
        conn.execute(
            """INSERT INTO processing_events (track_pk, event_type, status, message, payload_json)
               VALUES (?, 'rating_cleared', 'ok', 'Rating cleared', ?)""",
            (track_pk, json.dumps({"previous": row["personal_rating"], "new": None})),
        )
        return True


def rating_summary(db_path: str | None = None) -> dict:
    """Counts per rating tier plus unrated. For dashboards/healthcheck."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT COALESCE(personal_rating, 0) AS r, COUNT(*) AS n
               FROM tracks GROUP BY personal_rating"""
        ).fetchall()
        counts = {row["r"]: row["n"] for row in rows}
        return {
            "unrated": counts.get(0, 0),
            "like": counts.get(1, 0),
            "really_like": counts.get(2, 0),
            "love": counts.get(3, 0),
            "perfect": counts.get(4, 0),
        }
    finally:
        conn.close()
