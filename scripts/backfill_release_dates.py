"""
Backfill tracks.release_date from MusicBrainz for tracks that already have a
recording MBID but no date (the pipeline discarded MB dates before 2026-07-04).

One recording lookup per track at the anonymous MusicBrainz rate limit
(1 req/s), so ~5,700 candidates take ~95 minutes. Resume-safe: already-dated
tracks are never re-selected, so the script can be re-run after interruption.
Tracks whose lookup returns no dated release are left NULL and re-attempted on
any later run (harmless: this is a one-off drain, not a nightly job).

Usage:
    python scripts/backfill_release_dates.py [--limit N] [--dry-run]
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db.connection import db_conn
from app.enrichment import musicbrainz


def backfill(db_path: str | None = None, limit: int | None = None,
             dry_run: bool = False) -> dict:
    with db_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT track_pk, musicbrainz_recording_id
            FROM tracks
            WHERE musicbrainz_recording_id IS NOT NULL
              AND (release_date IS NULL OR release_date = '')
            ORDER BY track_pk
            """
        ).fetchall()
    if limit:
        rows = rows[:limit]

    summary = {"candidates": len(rows), "dated": 0, "no_date": 0, "errors": 0}
    print(f"Backfill: {len(rows)} candidate tracks", flush=True)

    for i, row in enumerate(rows, 1):
        pk, mbid = row["track_pk"], row["musicbrainz_recording_id"]
        try:
            date = musicbrainz.lookup_release_date(mbid)
        except Exception as e:  # noqa: BLE001 — count and continue
            summary["errors"] += 1
            print(f"  ERROR {pk} ({mbid}): {e}", flush=True)
            continue
        if date:
            summary["dated"] += 1
            if not dry_run:
                with db_conn(db_path) as conn:
                    conn.execute(
                        "UPDATE tracks SET release_date = ?, updated_at = ? "
                        "WHERE track_pk = ? "
                        "AND (release_date IS NULL OR release_date = '')",
                        (date, datetime.now(timezone.utc).isoformat(), pk),
                    )
        else:
            summary["no_date"] += 1
        if i % 100 == 0:
            print(f"  {i}/{len(rows)} — dated {summary['dated']}, "
                  f"no_date {summary['no_date']}, errors {summary['errors']}",
                  flush=True)

    print(f"Done: {summary}", flush=True)
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    result = backfill(limit=args.limit, dry_run=args.dry_run)
    sys.exit(1 if result["errors"] and not result["dated"] else 0)
