"""
Label tracks as reference exemplars for tag profiles, via CLI.

Reference labels are the hand-curated training signal for the Stage 1 kNN
classifier. They are NOT tags: each carries a polarity (positive / negative /
near_miss) and is bound to a specific profile. A profile cannot auto-apply
until it has >= 15 positive AND >= 15 negative/near_miss references from
>= 3 distinct artists — check progress with --readiness.

Usage:
    # Mark a track as a POSITIVE exemplar for a profile
    python scripts/label_reference.py --track-pk "isrc:GBXXX001" \
        --profile peak-time-dark-techno --label positive

    # Negative / near-miss exemplar (with a note on why it's a near miss)
    python scripts/label_reference.py --track-pk "isrc:GBXXX002" \
        --profile peak-time-dark-techno --label near_miss \
        --notes "Right energy, wrong mood — too melodic"

    # Remove a label (all polarities for that profile, or one with --label)
    python scripts/label_reference.py --track-pk "isrc:GBXXX001" \
        --profile peak-time-dark-techno --remove

    # List labels for a track, or for a profile
    python scripts/label_reference.py --track-pk "isrc:GBXXX001" --list
    python scripts/label_reference.py --profile peak-time-dark-techno --list

    # Readiness — how close a profile is to auto-apply (or all profiles)
    python scripts/label_reference.py --profile peak-time-dark-techno --readiness
    python scripts/label_reference.py --readiness-all

    # List the available profiles to label against
    python scripts/label_reference.py --profiles
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from tabulate import tabulate


def main():
    p = argparse.ArgumentParser(description="Label reference exemplars for tag profiles")
    p.add_argument("--track-pk", help="Track primary key (e.g. isrc:GBXXX001)")
    p.add_argument("--profile", help="Profile id (see --profiles)")
    p.add_argument("--label", choices=["positive", "negative", "near_miss"],
                   help="Polarity of the exemplar")
    p.add_argument("--notes", help="Optional note (esp. useful for near_miss)")
    p.add_argument("--remove", action="store_true", help="Remove label(s) instead of adding")
    p.add_argument("--list", action="store_true", help="List labels for --track-pk or --profile")
    p.add_argument("--readiness", action="store_true", help="Readiness for --profile")
    p.add_argument("--readiness-all", action="store_true", help="Readiness for every profile")
    p.add_argument("--profiles", action="store_true", help="List available profiles")
    p.add_argument("--backfill", action="store_true",
                   help="Re-derive references from all existing manual tags (idempotent)")
    p.add_argument("--db-path", default=None, help="Override SQLITE_PATH from .env")
    args = p.parse_args()

    from app.tags.reference_manager import (
        add_reference_label, remove_reference_label,
        list_reference_labels, profile_readiness,
    )

    # ── Backfill ────────────────────────────────────────────────────────────
    if args.backfill:
        from app.tags.reference_manager import backfill_all_references
        r = backfill_all_references(db_path=args.db_path)
        print(f"Scanned {r['tracks_scanned']} tagged track(s): "
              f"{r['tracks_changed']} changed, +{r['labels_added']} / -{r['labels_removed']} labels")
        return

    # ── List profiles ───────────────────────────────────────────────────────
    if args.profiles:
        _list_profiles(args.db_path)
        return

    # ── Readiness (all) ─────────────────────────────────────────────────────
    if args.readiness_all:
        _readiness_all(args.db_path)
        return

    # ── Readiness (one) ─────────────────────────────────────────────────────
    if args.readiness:
        if not args.profile:
            print("ERROR: --readiness requires --profile (or use --readiness-all)")
            sys.exit(1)
        _print_readiness(profile_readiness(args.profile, db_path=args.db_path))
        return

    # ── List labels ─────────────────────────────────────────────────────────
    if args.list:
        if not (args.track_pk or args.profile):
            print("ERROR: --list requires --track-pk and/or --profile")
            sys.exit(1)
        rows = list_reference_labels(
            track_pk=args.track_pk, profile_id=args.profile,
            label_type=args.label, db_path=args.db_path,
        )
        if not rows:
            print("No reference labels found.")
            return
        headers = ["profile", "label", "artist", "title", "notes", "created"]
        table = [
            [r["profile_id"], r["label_type"], r["canonical_artist"][:24],
             r["canonical_title"][:32], (r["notes"] or "")[:30], r["created_at"][:19]]
            for r in rows
        ]
        print(tabulate(table, headers=headers, tablefmt="simple"))
        print(f"\n{len(rows)} label(s)")
        return

    # ── Add / remove require track + profile ────────────────────────────────
    if not (args.track_pk and args.profile):
        p.print_help()
        sys.exit(1)

    if args.remove:
        n = remove_reference_label(
            args.track_pk, args.profile, label_type=args.label, db_path=args.db_path,
        )
        scope = f"'{args.label}' " if args.label else ""
        print(f"✓ Removed {n} {scope}label(s) for {args.track_pk} / {args.profile}"
              if n else f"No matching label to remove.")
        return

    if not args.label:
        print("ERROR: --label is required to add (positive | negative | near_miss)")
        sys.exit(1)

    try:
        created = add_reference_label(
            args.track_pk, args.profile, args.label,
            notes=args.notes, db_path=args.db_path,
        )
    except ValueError as e:
        print(f"✗ {e}")
        sys.exit(1)

    if created:
        print(f"✓ {args.track_pk} labelled '{args.label}' for {args.profile}")
        _print_readiness(profile_readiness(args.profile, db_path=args.db_path))
    else:
        print(f"Already labelled '{args.label}' for {args.profile} — no change.")


def _print_readiness(r: dict):
    flag = "READY ✓" if r["ready"] else "not yet"
    print(f"\n{r['profile_id']} — {flag}")
    print(f"  positive:            {r['positive']:>3}  (need 15, {r['needs_positive']} to go)")
    print(f"  negative+near_miss:  {r['negative_plus_near_miss']:>3}  "
          f"(need 15, {r['needs_negative_or_near_miss']} to go)  "
          f"[neg {r['negative']}, near {r['near_miss']}]")
    print(f"  distinct pos artists:{r['distinct_positive_artists']:>3}  "
          f"(need 3, {r['needs_artists']} to go)")


def _readiness_all(db_path):
    from app.db.connection import get_connection
    conn = get_connection(db_path)
    try:
        ids = [row["profile_id"] for row in conn.execute(
            "SELECT profile_id FROM tag_profiles ORDER BY profile_id"
        ).fetchall()]
    finally:
        conn.close()
    if not ids:
        print("No profiles. Seed them with seed_starter_tag_profiles().")
        return
    from app.tags.reference_manager import profile_readiness
    rows = []
    for pid in ids:
        r = profile_readiness(pid, db_path=db_path)
        rows.append([pid, "✓" if r["ready"] else "",
                     r["positive"], r["negative_plus_near_miss"],
                     r["distinct_positive_artists"]])
    print(tabulate(rows, headers=["profile", "ready", "pos", "neg+near", "pos_artists"],
                   tablefmt="simple"))


def _list_profiles(db_path):
    from app.db.connection import get_connection
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT profile_id, taxonomy_layer, description "
            "FROM tag_profiles ORDER BY taxonomy_layer, profile_id"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        print("No profiles. Seed them with seed_starter_tag_profiles().")
        return
    table = [[r["profile_id"], r["taxonomy_layer"], (r["description"] or "")[:50]] for r in rows]
    print(tabulate(table, headers=["profile_id", "layer", "description"], tablefmt="simple"))


if __name__ == "__main__":
    main()
