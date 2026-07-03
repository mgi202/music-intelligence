"""Job 3 — artists & labels backfill (nightly, incremental, CPU-only).

The enrichment pipeline already captures MusicBrainz artist credits and
labels inline (_capture_artists_labels), so MB-matched tracks fill in as the
drain progresses. What that leaves behind is every track MusicBrainz never
matched: for those the only artist data persisted anywhere is
tracks.canonical_artist, so this job derives a name-keyed primary artist row
from it. No new API calls; upserts keyed on mbid where present else
normalised name (same _entity_id scheme as the pipeline); existing ids are
never re-keyed (INSERT OR IGNORE only).

Labels have NO persisted source for historic tracks (the Discogs label field
was discarded before 2026-07-03; MB labels are only captured inline) — so
there is nothing to backfill for labels in v1. Going forward the pipeline
captures Discogs labels too.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.db.connection import db_conn
from app.enrichment.pipeline import _entity_id
from app.jobs import runs

JOB_NAME = "artists_labels_backfill"


def run_backfill(db_path: str | None = None) -> dict:
    """Derive primary artists for tracks with no track_artists row.

    Incremental: after the first full sweep, only tracks created or
    (enrichment-)updated since the stored cursor are examined — but a track
    that gained MB credits meanwhile is naturally skipped by the
    no-track_artists-row filter.
    """
    detail = runs.get_detail(JOB_NAME, db_path)
    cursor = detail.get("cursor")  # ISO of last sweep, None = full sweep
    now = datetime.now(timezone.utc).isoformat()

    where_incremental = ""
    params: list = []
    if cursor:
        # datetime() normalises mixed timestamp formats (CURRENT_TIMESTAMP is
        # space-separated, isoformat uses 'T') for a correct comparison; >=
        # because it truncates sub-second precision (rescans are OR IGNOREd).
        where_incremental = """
              AND (datetime(t.created_at) >= datetime(?) OR EXISTS (
                       SELECT 1 FROM enrichment_state es
                       WHERE es.track_pk = t.track_pk
                         AND datetime(es.updated_at) >= datetime(?)))"""
        params = [cursor, cursor]

    inserted_artists = 0
    linked = 0
    with db_conn(db_path) as conn:
        rows = conn.execute(
            f"""SELECT t.track_pk, t.canonical_artist
                FROM tracks t
                WHERE t.canonical_artist IS NOT NULL AND t.canonical_artist != ''
                  AND NOT EXISTS (SELECT 1 FROM track_artists ta
                                  WHERE ta.track_pk = t.track_pk)
                  {where_incremental}""",
            params,
        ).fetchall()

        for r in rows:
            name = r["canonical_artist"].strip()
            if not name:
                continue
            artist_id = _entity_id(name, None)
            cur = conn.execute(
                "INSERT OR IGNORE INTO artists (artist_id, name) VALUES (?, ?)",
                (artist_id, name),
            )
            inserted_artists += cur.rowcount
            cur = conn.execute(
                "INSERT OR IGNORE INTO track_artists (track_pk, artist_id, role, position) "
                "VALUES (?, ?, 'primary', 0)",
                (r["track_pk"], artist_id),
            )
            linked += cur.rowcount

        totals = {
            "artists": conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0],
            "track_artists": conn.execute("SELECT COUNT(*) FROM track_artists").fetchone()[0],
            "labels": conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0],
            "track_labels": conn.execute("SELECT COUNT(*) FROM track_labels").fetchone()[0],
        }

    runs.merge_detail(JOB_NAME, {"cursor": now}, db_path)
    return {
        "examined": len(rows),
        "artists_inserted": inserted_artists,
        "tracks_linked": linked,
        "totals": totals,
    }
