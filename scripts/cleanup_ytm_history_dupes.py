"""One-off cleanup for the ytm_history dedup hole (2026-07-05).

import_ytm_history re-imported the whole ~200-item YTM watch history on every
worker pass (the ±30-min guard only applied to rows that resolved to a
track_pk, and even resolved rows re-inserted every ~30 min), inflating
listens to ~24k ytm_history rows over only ~236 distinct tracks.

Collapse: keep ONE row per identity key per calendar day (UTC) — preferring a
resolved row (track_pk NOT NULL), then the earliest — and delete the rest.
Identity key = recording_msid ('ytm:<videoId>') when present, else
track_name|artist_name. Rows from other sources (lastfm, listenbrainz) are
never touched.

Usage:
    python scripts/cleanup_ytm_history_dupes.py [--db PATH] [--apply]

Dry-run by default: prints what would be deleted. Pass --apply to delete.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db.connection import db_conn  # noqa: E402

_RANKED = """
    SELECT listen_id,
           ROW_NUMBER() OVER (
               PARTITION BY
                   COALESCE(recording_msid,
                            IFNULL(track_name, '') || '|' || IFNULL(artist_name, '')),
                   date(listened_at, 'unixepoch')
               ORDER BY (track_pk IS NULL), listened_at, listen_id
           ) AS rn
    FROM listens
    WHERE source = 'ytm_history'
"""


def collapse_ytm_history_dupes(db_path: str | None = None, apply: bool = False) -> dict:
    """Collapse ytm_history duplicate listens. Returns a summary dict.

    Keeps, per (identity key, UTC day): the earliest resolved row if any,
    else the earliest row. Idempotent — a second run deletes nothing.
    """
    with db_conn(db_path) as conn:
        def counts():
            rows = conn.execute(
                "SELECT source, COUNT(*) AS n FROM listens GROUP BY source"
            ).fetchall()
            return {r["source"]: r["n"] for r in rows}

        before = counts()
        doomed = conn.execute(
            f"SELECT COUNT(*) AS n FROM ({_RANKED}) WHERE rn > 1"
        ).fetchone()["n"]

        if apply:
            conn.execute(
                f"DELETE FROM listens WHERE listen_id IN "
                f"(SELECT listen_id FROM ({_RANKED}) WHERE rn > 1)"
            )

        after = counts() if apply else None
        distinct_keys = conn.execute(
            "SELECT COUNT(DISTINCT COALESCE(recording_msid, "
            "IFNULL(track_name,'') || '|' || IFNULL(artist_name,''))) AS n "
            "FROM listens WHERE source = 'ytm_history'"
        ).fetchone()["n"]

    return {
        "before": before,
        "to_delete": doomed,
        "after": after,
        "distinct_ytm_keys": distinct_keys,
        "applied": apply,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None, help="DB path (default: app default)")
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry-run)")
    args = ap.parse_args()

    s = collapse_ytm_history_dupes(args.db, apply=args.apply)
    mode = "APPLIED" if s["applied"] else "DRY-RUN"
    print(f"[{mode}] listens by source before: {s['before']}")
    print(f"[{mode}] ytm_history rows to delete: {s['to_delete']}")
    if s["after"] is not None:
        print(f"[{mode}] listens by source after: {s['after']}")
    print(f"[{mode}] distinct ytm_history identity keys: {s['distinct_ytm_keys']}")


if __name__ == "__main__":
    main()
