"""One-off cleanup for the bass-family dissolution (2026-07-06).

The bass-music lineage (jungle, drum and bass, dubstep, uk garage, breakbeat)
was promoted from subgenre to family and the old "bass" umbrella family was
dissolved (Matthias — "jungle, drum and bass and dubstep are genres, not
subgenres"). The promotions and jungle's creation are handled by
reconcile_tag_profiles from the updated seed; the four promoted profiles keep
their profile_id, so their reference labels and manual tags survive untouched.

reconcile only drops a leftover profile when it is LABEL-FREE. On prod "bass"
carried one auto-created near_miss (a Verdict-Queue "not bass" rejection), so
reconcile would keep it lingering. This script removes that stale row: the
near_miss references a profile that no longer exists, so it is meaningless.

Usage:
    python scripts/dissolve_bass_family.py [--db PATH] [--apply]

Dry-run by default: prints what would be deleted. Pass --apply to delete.
Idempotent — a second run deletes nothing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db.connection import db_conn  # noqa: E402

_DISSOLVED = "bass"


def dissolve_bass(db_path: str | None = None, apply: bool = False) -> dict:
    """Delete the dissolved 'bass' profile and any labels/results hanging off it.

    Only touches profile_id='bass' — the promoted families (drum and bass etc.)
    keep their own profile_id and are never referenced here.
    """
    with db_conn(db_path) as conn:
        def one(sql):
            return conn.execute(sql, (_DISSOLVED,)).fetchone()[0]

        summary = {
            "profile_exists": one(
                "SELECT COUNT(*) FROM tag_profiles WHERE profile_id = ?"),
            "reference_labels": one(
                "SELECT COUNT(*) FROM reference_track_labels WHERE profile_id = ?"),
            "classification_results": one(
                "SELECT COUNT(*) FROM classification_results WHERE profile_id = ?"),
            "applied": apply,
        }

        if apply:
            conn.execute(
                "DELETE FROM reference_track_labels WHERE profile_id = ?",
                (_DISSOLVED,))
            conn.execute(
                "DELETE FROM classification_results WHERE profile_id = ?",
                (_DISSOLVED,))
            conn.execute(
                "DELETE FROM tag_profiles WHERE profile_id = ?", (_DISSOLVED,))

    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Dissolve the 'bass' family profile.")
    ap.add_argument("--db", default=None, help="Path to library.db")
    ap.add_argument("--apply", action="store_true", help="Actually delete")
    args = ap.parse_args()

    s = dissolve_bass(args.db, apply=args.apply)
    verb = "Deleted" if args.apply else "Would delete"
    print(f"bass profile rows: {s['profile_exists']}")
    print(f"  {verb}: {s['reference_labels']} reference label(s), "
          f"{s['classification_results']} classification result(s), "
          f"{s['profile_exists']} profile row(s)")
    if not args.apply:
        print("\nDry run — pass --apply to delete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
