"""One-off migration: era slot "modern" → "modern-sound" (2026-07-06).

The bare "modern" era profile collided with the vague public folksonomy tag
"modern" (Last.fm/Discogs put it on classical/jazz), so a Library filter on the
era also caught tracks that merely carried a public "modern" tag. The profile
was renamed "modern-sound" (matching its 70s/80s/90s/00s-sound siblings) and the
raw "modern" tag added to the hide-list.

reconcile (init_db) inserts the new "modern-sound" profile from the seed but
leaves the old "modern" profile in place because it still carries reference
labels. This script moves everything that hangs off "modern" onto "modern-sound"
and drops the old profile:

  - private_manual track_tags  (the actual era judgements, e.g. on "Lessons")
  - reference_track_labels      (positives + auto-derived opposing negatives)
  - classification_results
  - the leftover "modern" tag_profiles row

Public "modern" tags are deliberately NOT migrated — they stay `tag='modern'`
and are suppressed by the hide-list (that's the whole point). Run AFTER init_db
so "modern-sound" already exists. Idempotent — a second run does nothing.

Usage:
    python scripts/rename_modern_era.py [--db PATH] [--apply]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db.connection import db_conn  # noqa: E402

_OLD = "modern"
_NEW = "modern-sound"


def rename_modern(db_path: str | None = None, apply: bool = False) -> dict:
    with db_conn(db_path) as conn:
        def n(sql, *a):
            return conn.execute(sql, a).fetchone()[0]

        summary = {
            "new_profile_exists": n(
                "SELECT COUNT(*) FROM tag_profiles WHERE profile_id = ?", _NEW),
            "old_profile_exists": n(
                "SELECT COUNT(*) FROM tag_profiles WHERE profile_id = ?", _OLD),
            "manual_tags": n(
                "SELECT COUNT(*) FROM track_tags "
                "WHERE LOWER(tag) = ? AND tag_type = 'private_manual'", _OLD),
            "reference_labels": n(
                "SELECT COUNT(*) FROM reference_track_labels WHERE profile_id = ?", _OLD),
            "classification_results": n(
                "SELECT COUNT(*) FROM classification_results WHERE profile_id = ?", _OLD),
            "public_modern_tags_left_as_is": n(
                "SELECT COUNT(*) FROM track_tags "
                "WHERE LOWER(tag) = ? AND tag_type != 'private_manual'", _OLD),
            "applied": apply,
        }

        if apply:
            if not summary["new_profile_exists"]:
                raise RuntimeError(
                    f"{_NEW!r} profile missing — run init_db (reconcile) first")
            # Migrate reference labels + classifications (profile_id keyed).
            for tbl in ("reference_track_labels", "classification_results"):
                conn.execute(
                    f"UPDATE OR IGNORE {tbl} SET profile_id = ? WHERE profile_id = ?",
                    (_NEW, _OLD))
                conn.execute(f"DELETE FROM {tbl} WHERE profile_id = ?", (_OLD,))
            # Migrate the era judgements (private_manual tags carry the tag_name).
            conn.execute(
                "UPDATE OR IGNORE track_tags SET tag = ? "
                "WHERE LOWER(tag) = ? AND tag_type = 'private_manual'",
                (_NEW, _OLD))
            conn.execute(
                "DELETE FROM track_tags "
                "WHERE LOWER(tag) = ? AND tag_type = 'private_manual'", (_OLD,))
            # Drop the now-empty old profile.
            conn.execute("DELETE FROM tag_profiles WHERE profile_id = ?", (_OLD,))

    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Migrate era 'modern' → 'modern-sound'.")
    ap.add_argument("--db", default=None)
    ap.add_argument("--apply", action="store_true", help="Actually migrate")
    args = ap.parse_args()

    s = rename_modern(args.db, apply=args.apply)
    verb = "Migrated" if args.apply else "Would migrate"
    print(f"{_NEW} profile present: {bool(s['new_profile_exists'])} | "
          f"old {_OLD} profile present: {bool(s['old_profile_exists'])}")
    print(f"  {verb}: {s['manual_tags']} manual tag(s), "
          f"{s['reference_labels']} reference label(s), "
          f"{s['classification_results']} classification(s); then drop old profile")
    print(f"  public '{_OLD}' tags left as-is (hidden by the hide-list): "
          f"{s['public_modern_tags_left_as_is']}")
    if not args.apply:
        print("\nDry run — pass --apply to migrate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
