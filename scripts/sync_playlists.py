"""
Sync playlists — compile rules and write back to YouTube Music.

Usage:
    # Sync all enabled playlist rules
    python scripts/sync_playlists.py

    # Sync a specific rule
    python scripts/sync_playlists.py --rule-id utility__recently_added

    # Dry run — show what would be synced without writing to YTM
    python scripts/sync_playlists.py --dry-run

    # List all rules and their last sync time
    python scripts/sync_playlists.py --list
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()


def list_rules(db_path=None):
    from app.db.connection import get_connection
    conn = get_connection(db_path)
    try:
        rows = conn.execute("""
            SELECT rule_id, playlist_name, ranking_mode, enabled,
                   max_tracks, last_synced_at, target_playlist_id
            FROM playlist_rules
            ORDER BY rule_id
        """).fetchall()

        print(f"\nPlaylist rules ({len(rows)} total):")
        fmt = "  {:<40} {:<12} {:<10} {:<8} {}"
        print(fmt.format("Rule ID", "Mode", "Enabled", "MaxTracks", "Last Synced"))
        print("  " + "-" * 80)
        for r in rows:
            print(fmt.format(
                r["rule_id"][:39],
                r["ranking_mode"],
                "✓" if r["enabled"] else "✗",
                str(r["max_tracks"] or "∞"),
                r["last_synced_at"] or "never",
            ))
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Sync playlist rules to YouTube Music")
    parser.add_argument("--rule-id", help="Sync a specific rule only")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be synced without writing to YTM")
    parser.add_argument("--list", action="store_true",
                        help="List all rules and exit")
    parser.add_argument("--db-path", default=None, help="Override SQLITE_PATH from .env")
    args = parser.parse_args()

    if args.list:
        list_rules(args.db_path)
        return

    from app.ingestion.ytm_adapter import YouTubeMusicAdapter
    from app.playlists.sync import sync_playlist, sync_all_playlists

    adapter = YouTubeMusicAdapter()

    if args.rule_id:
        print(f"Syncing rule: {args.rule_id}")
        result = sync_playlist(args.rule_id, adapter, db_path=args.db_path, dry_run=args.dry_run)
        _print_result(result)
    else:
        print("Syncing all enabled playlist rules...")
        results = sync_all_playlists(adapter, db_path=args.db_path, dry_run=args.dry_run)
        for result in results:
            _print_result(result)
        synced = sum(1 for r in results if r.get("synced"))
        print(f"\n✓ {synced}/{len(results)} playlists synced successfully")


def _print_result(result: dict):
    status = "DRY RUN" if result.get("dry_run") else ("✓" if result.get("synced") else "✗")
    print(
        f"  {status} {result['rule_id']}: "
        f"{result.get('tracks_compiled', 0)} compiled, "
        f"{result.get('video_ids_resolved', 0)} resolved"
        + (f" → {result['playlist_id']}" if result.get("playlist_id") else "")
        + (f" [ERROR: {result['error']}]" if result.get("error") else "")
    )


if __name__ == "__main__":
    main()
