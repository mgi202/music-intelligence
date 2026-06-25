"""
Ingest — pull library from YouTube Music (or CSV) into the ledger.

Usage:
    # Fetch from YTM
    python scripts/ingest.py --source ytm

    # Import from CSV
    python scripts/ingest.py --source csv --csv-file /path/to/library.csv

    # Dry run (shows what would be imported, no writes)
    python scripts/ingest.py --source ytm --dry-run
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from tqdm import tqdm

from app.db.init_db import init_db
from app.ingestion.ledger import ingest_tokens


def main():
    parser = argparse.ArgumentParser(description="Ingest library into Music Intelligence System")
    parser.add_argument("--source", choices=["ytm", "csv"], default="ytm",
                        help="Data source (default: ytm)")
    parser.add_argument("--csv-file", help="Path to CSV file (required for --source csv)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be imported without writing to DB")
    parser.add_argument("--db-path", default=None, help="Override SQLITE_PATH from .env")
    args = parser.parse_args()

    # Ensure DB is initialised
    init_db(args.db_path)

    print(f"Fetching library from {args.source.upper()}...")

    if args.source == "ytm":
        from app.ingestion.ytm_adapter import YouTubeMusicAdapter
        adapter = YouTubeMusicAdapter()
        try:
            tokens = adapter.fetch_library_snapshot()
        except Exception as e:
            print(f"ERROR: Failed to fetch from YTM: {e}")
            print("Ensure oauth.json exists. Run: python scripts/setup_ytm_oauth.py")
            sys.exit(1)

    elif args.source == "csv":
        if not args.csv_file:
            print("ERROR: --csv-file is required when --source csv")
            sys.exit(1)
        from app.ingestion.csv_adapter import CSVAdapter
        adapter = CSVAdapter(args.csv_file)
        tokens = adapter.fetch_library_snapshot()

    print(f"Fetched {len(tokens)} tracks from {args.source.upper()}")

    if args.dry_run:
        print("\nDRY RUN — sample (first 10 tracks):")
        for t in tokens[:10]:
            print(f"  [{t.track_pk}] {t.canonical_artist} — {t.canonical_title}")
        print(f"\n{len(tokens)} tracks would be ingested. No changes written.")
        return

    print("Writing to ledger...")
    new_count, updated_count = ingest_tokens(tokens)
    print(f"✓ Done: {new_count} new tracks, {updated_count} updated")

    # Seed utility playlists if this is the first run
    from app.playlists.utility import (
        seed_utility_playlists,
        seed_starter_tag_profiles,
        seed_example_rules,
    )
    seeded = seed_utility_playlists(target_platform="ytm", db_path=args.db_path)
    if seeded:
        print(f"✓ Seeded {seeded} utility playlist rules")

    examples = seed_example_rules(target_platform="ytm", db_path=args.db_path)
    if examples:
        print(f"✓ Seeded {examples} example rules (disabled — enable in the UI)")

    profiles = seed_starter_tag_profiles(db_path=args.db_path)
    if profiles:
        print(f"✓ Seeded {profiles} starter tag profiles")


if __name__ == "__main__":
    main()
