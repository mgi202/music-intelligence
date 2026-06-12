"""
Apply (or remove) private_manual tags via CLI.

private_manual tags are the highest-trust tier and are never overwritten
by automated classification.

Usage:
    # Apply a tag
    python scripts/apply_tag.py --track-pk "isrc:GBXXX001" --tag "peak-time-dark-techno"

    # Apply with a note
    python scripts/apply_tag.py --track-pk "isrc:GBXXX001" --tag "warehouse-industrial" --notes "Confirmed at Fabric"

    # Remove a tag
    python scripts/apply_tag.py --track-pk "isrc:GBXXX001" --tag "peak-time-dark-techno" --remove

    # List all tags for a track
    python scripts/apply_tag.py --track-pk "isrc:GBXXX001" --list

    # Search for all tracks with a tag
    python scripts/apply_tag.py --search "warehouse-industrial"

    # Find a track by artist+title (shows its track_pk)
    python scripts/apply_tag.py --find "Surgeon" "Body Hammer"
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from tabulate import tabulate


def main():
    parser = argparse.ArgumentParser(description="Apply/remove private_manual tags")
    parser.add_argument("--track-pk", help="Track primary key (e.g. isrc:GBXXX001)")
    parser.add_argument("--tag", help="Tag name to apply/remove")
    parser.add_argument("--notes", help="Optional notes when applying")
    parser.add_argument("--remove", action="store_true", help="Remove the tag instead of applying")
    parser.add_argument("--list", action="store_true", help="List all tags for the track")
    parser.add_argument("--search", metavar="TAG", help="Find all tracks with this tag")
    parser.add_argument("--find", nargs=2, metavar=("ARTIST", "TITLE"),
                        help="Find a track by artist and title")
    parser.add_argument("--db-path", default=None, help="Override SQLITE_PATH from .env")
    args = parser.parse_args()

    from app.tags.tag_manager import apply_tag, remove_tag, list_tags, search_tracks_by_tag

    # ── Find track by artist+title ──────────────────────────────────────────
    if args.find:
        artist, title = args.find
        _find_track(artist, title, args.db_path)
        return

    # ── Search by tag ───────────────────────────────────────────────────────
    if args.search:
        tracks = search_tracks_by_tag(args.search, db_path=args.db_path)
        if not tracks:
            print(f"No tracks found with tag: {args.search}")
            return
        headers = ["track_pk", "artist", "title", "type", "confidence"]
        rows = [
            [t["track_pk"], t["canonical_artist"][:30], t["canonical_title"][:40],
             t["tag_type"], f"{t['confidence']:.2f}"]
            for t in tracks
        ]
        print(tabulate(rows, headers=headers, tablefmt="simple"))
        print(f"\n{len(tracks)} tracks found")
        return

    # ── Operations that require --track-pk ─────────────────────────────────
    if not args.track_pk:
        parser.print_help()
        sys.exit(1)

    if args.list:
        tags = list_tags(args.track_pk, db_path=args.db_path)
        if not tags:
            print(f"No tags found for: {args.track_pk}")
            return
        headers = ["tag", "type", "source", "confidence", "created_at"]
        rows = [[t["tag"], t["tag_type"], t["source"],
                 f"{t['confidence']:.2f}", t["created_at"][:19]] for t in tags]
        print(f"\nTags for {args.track_pk}:")
        print(tabulate(rows, headers=headers, tablefmt="simple"))
        return

    if not args.tag:
        print("ERROR: --tag is required for apply/remove operations")
        sys.exit(1)

    if args.remove:
        removed = remove_tag(args.track_pk, args.tag, db_path=args.db_path)
        if removed:
            print(f"✓ Removed tag '{args.tag}' from {args.track_pk}")
        else:
            print(f"Tag '{args.tag}' not found (or not a private_manual tag) on {args.track_pk}")
    else:
        applied = apply_tag(args.track_pk, args.tag, notes=args.notes, db_path=args.db_path)
        if applied:
            print(f"✓ Applied tag '{args.tag}' to {args.track_pk}")
        else:
            print(f"Tag '{args.tag}' already applied to {args.track_pk}")


def _find_track(artist: str, title: str, db_path=None):
    """Find a track by fuzzy artist+title match."""
    from app.db.connection import get_connection
    conn = get_connection(db_path)
    try:
        rows = conn.execute("""
            SELECT track_pk, canonical_artist, canonical_title, match_status
            FROM tracks
            WHERE normalized_artist LIKE ?
               OR normalized_title LIKE ?
            LIMIT 20
        """, (f"%{artist.lower()}%", f"%{title.lower()}%")).fetchall()

        if not rows:
            # Try broader search
            rows = conn.execute("""
                SELECT track_pk, canonical_artist, canonical_title, match_status
                FROM tracks
                WHERE canonical_artist LIKE ?
                   OR canonical_title LIKE ?
                LIMIT 20
            """, (f"%{artist}%", f"%{title}%")).fetchall()

        if not rows:
            print(f"No tracks found matching artist='{artist}' title='{title}'")
            return

        headers = ["track_pk", "artist", "title", "status"]
        table_rows = [
            [r["track_pk"], r["canonical_artist"][:30], r["canonical_title"][:40], r["match_status"]]
            for r in rows
        ]
        print(tabulate(table_rows, headers=headers, tablefmt="simple"))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
